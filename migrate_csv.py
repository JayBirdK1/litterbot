"""
migrate_csv.py — One-time import of historical CSV files into litterbot.db.

Reads all litterbot_activity_YYYY-MM-DD.csv files and inserts their rows into
raw_events using INSERT OR IGNORE (safe to re-run — duplicates are skipped).
Recomputes daily_summary for all affected dates after import.

Usage:
    python migrate_csv.py                   # searches same directory as this script
    python migrate_csv.py /path/to/csvdir   # searches the given directory
"""
from __future__ import annotations

import csv
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

_BASE_DIR = Path(__file__).parent

from db import init_db, get_connection
from cats import load_profiles, sync_to_db, classify_weight
from summary import recompute, CAT_DETECT_ACTIONS


def _parse_float(value: Optional[str]) -> Optional[float]:
    if not value or value.strip() in ("", "None", "null"):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_bool(value: Optional[str]) -> Optional[bool]:
    if not value or value.strip() in ("", "None", "null"):
        return None
    return value.strip().lower() in ("true", "1", "yes")


def _import_file(
    path: Path,
    conn,
    cat_profiles: List[Dict],
    fetched_at: str,
) -> Tuple[int, int]:
    """
    Import rows from one CSV file into raw_events.
    Returns (rows_read, rows_inserted).
    """
    rows_read = 0
    rows_inserted = 0

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows_read += 1
            timestamp = (row.get("timestamp_utc") or row.get("timestamp") or "").strip()
            action = (row.get("action") or "").strip()
            robot_serial = (row.get("robot_serial") or "unknown").strip()

            if not timestamp or not action:
                continue

            weight = _parse_float(row.get("cat_weight_lbs"))
            cat_id: Optional[int] = None
            if action.upper() in CAT_DETECT_ACTIONS:
                cat_id, _ = classify_weight(weight, cat_profiles)

            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO raw_events (
                    robot_serial, robot_name, timestamp_utc, action, action_label,
                    cat_detect, cat_detect_label, cat_weight_lbs, dfi_level_percent,
                    is_dfi_full, odometer_clean_cycles, cat_id, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    robot_serial,
                    (row.get("robot_name") or "").strip() or None,
                    timestamp,
                    action,
                    row.get("action_label") or None,
                    row.get("cat_detect") or None,
                    row.get("cat_detect_label") or None,
                    weight,
                    _parse_float(row.get("dfi_level_percent")),
                    1 if _parse_bool(row.get("is_dfi_full")) else 0,
                    row.get("odometer_clean_cycles") or None,
                    cat_id,
                    fetched_at,
                ),
            )
            rows_inserted += cursor.rowcount

    return rows_read, rows_inserted


def main() -> None:
    search_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else _BASE_DIR
    csv_files = sorted(search_dir.glob("litterbot_activity_????-??-??.csv"))

    if not csv_files:
        print(f"No litterbot_activity_YYYY-MM-DD.csv files found in {search_dir.resolve()}")
        sys.exit(0)

    print(f"Found {len(csv_files)} CSV file(s) to import.")

    init_db()
    conn = get_connection()

    cat_profiles = load_profiles()
    if cat_profiles:
        print(f"Loaded {len(cat_profiles)} cat profile(s) from cats.toml.")
        with conn:
            sync_to_db(conn, cat_profiles)
    else:
        print("No valid cats.toml found — events imported without cat classification.")
        print("Edit cats.toml then re-run to apply classification.")

    fetched_at = datetime.now(tz=timezone.utc).isoformat()
    total_read = 0
    total_inserted = 0
    affected_dates: Set[str] = set()

    with conn:
        for csv_path in csv_files:
            read, inserted = _import_file(csv_path, conn, cat_profiles, fetched_at)
            total_read += read
            total_inserted += inserted
            print(f"  {csv_path.name}: {read} read, {inserted} new rows inserted")

            # Extract date from filename: litterbot_activity_YYYY-MM-DD.csv
            stem = csv_path.stem
            parts = stem.rsplit("_", 1)
            if len(parts) == 2 and len(parts[1]) == 10:
                affected_dates.add(parts[1])

    print(f"\nTotal: {total_read} rows read, {total_inserted} new rows inserted.")

    if not affected_dates:
        print("No valid dates detected — daily_summary not updated.")
        conn.close()
        return

    # Resolve affected robot serials from the database
    placeholders = ",".join("?" * len(affected_dates))
    rows = conn.execute(
        f"SELECT DISTINCT robot_serial FROM raw_events "
        f"WHERE date(timestamp_utc) IN ({placeholders})",
        list(affected_dates),
    ).fetchall()
    affected_serials: Set[str] = {r["robot_serial"] for r in rows}

    if affected_serials:
        print(f"Recomputing daily_summary for {len(affected_dates)} date(s), "
              f"{len(affected_serials)} robot(s)...")
        with conn:
            recompute(conn, affected_dates, affected_serials, cat_profiles)
        print("Daily summary updated.")

    conn.close()
    print("Migration complete.")


if __name__ == "__main__":
    main()
