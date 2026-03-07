"""
ingest.py — Fetch LR4 activity from the pylitterbot API and persist to SQLite.

Credentials are read from (in priority order):
  1. LR_USERNAME / LR_PASSWORD environment variables already set
  2. A .env file in the same directory as this script

On each run:
  1. Load cat profiles from cats.toml → sync to cat_profiles table
  2. Connect to LR4 API, fetch 7-day history for all LR4 robots
  3. Classify ROBOT_CAT_DETECT events by weight band
  4. Insert events via INSERT OR IGNORE (deduplication)
  5. Recompute daily_summary for all newly-touched dates

DISCLAIMER: pylitterbot is an unofficial, reverse-engineered API.
It may break at any time without notice.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_BASE_DIR = Path(__file__).parent

from db import init_db, get_connection, DB_PATH
from cats import load_profiles, sync_to_db, classify_weight
from summary import recompute, CAT_DETECT_ACTIONS

_CATS_TOML = _BASE_DIR / "cats.toml"
_ENV_FILE = _BASE_DIR / ".env"

# Human-readable labels for LR4 robotStatus values
_STATUS_LABELS: Dict[str, str] = {
    "ROBOT_IDLE": "Idle",
    "ROBOT_CAT_DETECT": "Cat Detected",
    "ROBOT_CLEAN_CYCLE": "Clean Cycle In Progress",
    "ROBOT_CLEAN_CYCLE_COMPLETE": "Clean Cycle Complete",
    "ROBOT_DRAWER_FULL": "Drawer Full",
    "ROBOT_DRAWER_FULL_CYCLE": "Drawer Full (still cycling)",
    "ROBOT_PINCH_DETECT": "Pinch Detected",
    "ROBOT_BONNET_REMOVED": "Bonnet Removed",
    "ROBOT_PAUSED": "Paused",
    "ROBOT_OFF": "Off",
    "ROBOT_FAULT": "Fault",
}

_CAT_DETECT_LABELS: Dict[str, str] = {
    "CAT_DETECT_CLEAR": "Clear",
    "CAT_DETECT_TIMING": "Timing",
    "CAT_DETECT_PRESENT": "Present",
}


# ─── Credential loading ───────────────────────────────────────────────────────

def _load_env_file(path: Path) -> None:
    """Minimal .env file loader — sets os.environ for KEY=VALUE lines."""
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            # Strip optional 'export ' prefix
            if line.startswith("export "):
                line = line[7:]
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def get_credentials() -> Tuple[str, str]:
    """
    Return (username, password) from environment or .env file.
    Raises SystemExit if credentials are missing.
    """
    _load_env_file(_ENV_FILE)
    username = os.environ.get("LR_USERNAME", "").strip()
    password = os.environ.get("LR_PASSWORD", "").strip()
    if not username or not password:
        raise SystemExit(
            "Error: LR_USERNAME and LR_PASSWORD must be set in the environment or .env file."
        )
    return username, password


# ─── Event extraction ─────────────────────────────────────────────────────────

def _get_attr(*names: str, obj, default=None):
    """Return the first non-None attribute found on obj from the given names."""
    for name in names:
        val = getattr(obj, name, None)
        if val is not None:
            return val
    return default


def _to_str(val) -> Optional[str]:
    if val is None:
        return None
    if hasattr(val, "value"):
        return str(val.value)
    if hasattr(val, "name"):
        return str(val.name)
    return str(val)


def _extract_event(
    entry,
    fetched_at: str,
    cat_profiles: List[Dict],
) -> Optional[Dict]:
    """
    Extract a raw_events row dict from a pylitterbot Activity entry.
    Returns None if the entry has no usable timestamp (cannot be stored).
    """
    ts_raw = _get_attr("timestamp", "time", obj=entry)
    if isinstance(ts_raw, datetime):
        ts_utc = (
            ts_raw.astimezone(timezone.utc)
            if ts_raw.tzinfo
            else ts_raw.replace(tzinfo=timezone.utc)
        )
        timestamp_str = ts_utc.isoformat()
    elif ts_raw is not None:
        timestamp_str = str(ts_raw)
    else:
        return None  # no timestamp — skip

    action_raw = _get_attr(
        "action", "robot_status", "robotStatus", "unit_status", "unitStatus", obj=entry
    )
    action = _to_str(action_raw) or ""
    action_upper = action.upper()

    cat_detect_raw = _get_attr("cat_detect", "catDetect", obj=entry)
    cat_detect = _to_str(cat_detect_raw)

    cat_weight_raw = _get_attr("cat_weight", "catWeight", obj=entry)
    cat_weight: Optional[float] = float(cat_weight_raw) if cat_weight_raw is not None else None

    dfi_raw = _get_attr("dfi_level_percent", "DFILevelPercent", obj=entry)
    dfi: Optional[float] = float(dfi_raw) if dfi_raw is not None else None

    is_dfi_full_raw = _get_attr("is_dfi_full", "isDFIFull", obj=entry)
    odometer_raw = _get_attr("odometer_clean_cycles", "odometerCleanCycles", obj=entry)

    # Classify weight to cat only on cat-detect events
    cat_id: Optional[int] = None
    if action_upper in CAT_DETECT_ACTIONS:
        cat_id, _ = classify_weight(cat_weight, cat_profiles)

    return {
        "timestamp_utc": timestamp_str,
        "action": action,
        "action_label": _STATUS_LABELS.get(action_upper, action) if action else None,
        "cat_detect": cat_detect,
        "cat_detect_label": _CAT_DETECT_LABELS.get(cat_detect or "", cat_detect) if cat_detect else None,
        "cat_weight_lbs": cat_weight,
        "dfi_level_percent": dfi,
        "is_dfi_full": 1 if is_dfi_full_raw else 0,
        "odometer_clean_cycles": int(odometer_raw) if odometer_raw is not None else None,
        "cat_id": cat_id,
        "fetched_at": fetched_at,
    }


# ─── Core async fetch ─────────────────────────────────────────────────────────

async def _fetch_and_store(
    username: str,
    password: str,
    db_path: Path,
    cat_profiles: List[Dict],
) -> Dict:
    """
    Connect to the LR4 API, pull 7-day history for all LR4 robots,
    insert new events into SQLite, and recompute daily_summary.

    Returns a result dict suitable for logging.
    """
    try:
        from pylitterbot import Account
        from pylitterbot.robot.litterrobot4 import LitterRobot4
    except ImportError:
        raise SystemExit("pylitterbot is not installed. Run: pip install pylitterbot")

    result: Dict = {
        "robots": [],
        "total_fetched": 0,
        "total_inserted": 0,
        "errors": [],
    }

    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=7)
    fetched_at = datetime.now(tz=timezone.utc).isoformat()

    # Tracks which (robot_serial → dates) had new events inserted
    affected: Dict[str, set] = {}

    conn = get_connection(db_path)
    account = Account()

    try:
        await account.connect(username=username, password=password, load_robots=True)

        lr4_robots = [r for r in account.robots if isinstance(r, LitterRobot4)]
        if not lr4_robots:
            result["errors"].append(
                "No LitterRobot4 found on account. "
                f"({len(account.robots)} total robot(s) found, none are LitterRobot4)"
            )
            return result

        for robot in lr4_robots:
            name: str = getattr(robot, "name", "Unknown")
            serial: str = getattr(robot, "serial", getattr(robot, "id", "unknown"))

            try:
                raw_history = await robot.get_activity_history()
            except Exception as exc:
                err = f"{serial}: get_activity_history() failed: {exc}"
                result["errors"].append(err)
                raw_history = []

            in_window = 0
            inserted = 0
            robot_dates: set = set()

            with conn:
                for entry in raw_history:
                    event = _extract_event(entry, fetched_at, cat_profiles)
                    if event is None:
                        continue

                    # Parse the stored timestamp back to check the cutoff window
                    try:
                        ts = datetime.fromisoformat(
                            event["timestamp_utc"].replace("Z", "+00:00")
                        )
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                    except ValueError:
                        continue

                    if ts < cutoff:
                        continue

                    in_window += 1
                    cursor = conn.execute(
                        """
                        INSERT OR IGNORE INTO raw_events (
                            robot_serial, robot_name, timestamp_utc, action, action_label,
                            cat_detect, cat_detect_label, cat_weight_lbs, dfi_level_percent,
                            is_dfi_full, odometer_clean_cycles, cat_id, fetched_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            serial, name,
                            event["timestamp_utc"],
                            event["action"], event["action_label"],
                            event["cat_detect"], event["cat_detect_label"],
                            event["cat_weight_lbs"], event["dfi_level_percent"],
                            event["is_dfi_full"], event["odometer_clean_cycles"],
                            event["cat_id"], event["fetched_at"],
                        ),
                    )
                    if cursor.rowcount:
                        inserted += 1
                        robot_dates.add(ts.date().isoformat())

            result["total_fetched"] += in_window
            result["total_inserted"] += inserted
            result["robots"].append({
                "serial": serial,
                "name": name,
                "fetched": in_window,
                "inserted": inserted,
            })

            if robot_dates:
                affected[serial] = robot_dates

        # Recompute daily_summary for every (robot, date) that received new events
        if affected:
            all_dates: set = set()
            for dates in affected.values():
                all_dates.update(dates)
            with conn:
                recompute(conn, all_dates, set(affected.keys()), cat_profiles)

    finally:
        await account.disconnect()
        conn.close()

    return result


# ─── Public entry point ───────────────────────────────────────────────────────

def run(db_path: Path = DB_PATH, cats_path: Path = _CATS_TOML) -> Dict:
    """
    Synchronous entry point called by run_ingest.py.
    Loads credentials and cat profiles, then runs the async fetch pipeline.
    Returns the result dict for logging.
    """
    username, password = get_credentials()

    init_db(db_path)

    cat_profiles = load_profiles(cats_path)
    if cat_profiles:
        conn = get_connection(db_path)
        with conn:
            sync_to_db(conn, cat_profiles)
        conn.close()

    return asyncio.run(_fetch_and_store(username, password, db_path, cat_profiles))
