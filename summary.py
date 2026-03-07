"""
summary.py — Compute and persist daily_summary rows from raw_events.

recompute() is called after every ingest run (and during CSV migration) to
keep daily_summary in sync with raw_events.

Row types written to daily_summary for each (date, robot_serial):
  cat_name IS NULL   → robot-level aggregate: clean_cycles, dfi, other_events, timestamps
  cat_name = <name>  → per-cat: cat_detects, cat weight stats
  cat_name = 'Unknown' → unclassified cat-detect events
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set


CAT_DETECT_ACTIONS: frozenset = frozenset({"ROBOT_CAT_DETECT", "CAT_DETECTED"})
CLEAN_CYCLE_ACTIONS: frozenset = frozenset({"ROBOT_CLEAN_CYCLE_COMPLETE", "CLEAN_CYCLE_COMPLETE", "CCC"})


def _parse_ts(ts_str: Optional[str]) -> Optional[datetime]:
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def recompute(
    conn: sqlite3.Connection,
    dates: Set[str],
    robot_serials: Set[str],
    cat_profiles: List[Dict],
) -> None:
    """
    Delete and re-insert daily_summary rows for every (date, robot_serial) pair.
    Must be called inside a transaction (caller holds the connection context).

    cat_profiles: list of {id, name, min_weight_lbs, max_weight_lbs}
    """
    cat_by_id: Dict[int, str] = {p["id"]: p["name"] for p in cat_profiles}

    for date in sorted(dates):
        for serial in sorted(robot_serials):
            # Wipe existing summary rows for this date/robot so recompute is idempotent.
            conn.execute(
                "DELETE FROM daily_summary WHERE date = ? AND robot_serial = ?",
                (date, serial),
            )

            events = conn.execute(
                """
                SELECT robot_name, timestamp_utc, action, cat_weight_lbs,
                       dfi_level_percent, is_dfi_full, cat_id
                FROM raw_events
                WHERE date(timestamp_utc) = ? AND robot_serial = ?
                ORDER BY timestamp_utc ASC
                """,
                (date, serial),
            ).fetchall()

            if not events:
                continue

            robot_name = events[0]["robot_name"]

            # Aggregate counters
            clean_cycles = 0
            total_cat_detects = 0
            other_events_count = 0
            dfi_levels: List[float] = []
            dfi_full_count = 0
            timestamps: List[datetime] = []

            # Per-cat buckets: (cat_id|None, cat_name) → list of valid weights
            cat_detect_counts: Dict = defaultdict(int)
            cat_weight_buckets: Dict = defaultdict(list)

            for e in events:
                action = (e["action"] or "").strip().upper()

                if action in CLEAN_CYCLE_ACTIONS:
                    clean_cycles += 1
                elif action in CAT_DETECT_ACTIONS:
                    total_cat_detects += 1
                    cat_id = e["cat_id"]
                    cat_name = cat_by_id.get(cat_id, "Unknown") if cat_id is not None else "Unknown"
                    key = (cat_id, cat_name)
                    cat_detect_counts[key] += 1
                    w = e["cat_weight_lbs"]
                    if w is not None and w > 0:
                        cat_weight_buckets[key].append(float(w))
                else:
                    other_events_count += 1

                dfi = e["dfi_level_percent"]
                if dfi is not None:
                    dfi_levels.append(float(dfi))

                if e["is_dfi_full"]:
                    dfi_full_count += 1

                ts = _parse_ts(e["timestamp_utc"])
                if ts:
                    timestamps.append(ts)

            dfi_level_end = dfi_levels[-1] if dfi_levels else None
            first_ts = timestamps[0].isoformat() if timestamps else None
            last_ts = timestamps[-1].isoformat() if timestamps else None
            active_hours = 0.0
            if len(timestamps) >= 2:
                active_hours = round(
                    (timestamps[-1] - timestamps[0]).total_seconds() / 3600, 2
                )

            # Insert aggregate (robot-level) row — cat_name IS NULL
            conn.execute(
                """
                INSERT INTO daily_summary (
                    date, robot_serial, robot_name, cat_id, cat_name,
                    clean_cycles, cat_detects, cat_weight_avg_lbs, cat_weight_min_lbs,
                    cat_weight_max_lbs, dfi_level_end_pct, dfi_full_events,
                    other_events, first_event_utc, last_event_utc, active_hours
                ) VALUES (?, ?, ?, NULL, NULL, ?, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?)
                """,
                (
                    date, serial, robot_name,
                    clean_cycles, total_cat_detects,
                    dfi_level_end, dfi_full_count, other_events_count,
                    first_ts, last_ts, active_hours,
                ),
            )

            # Insert per-cat rows (named cats + Unknown bucket)
            for (cat_id, cat_name), count in cat_detect_counts.items():
                weights = cat_weight_buckets.get((cat_id, cat_name), [])
                avg_w = round(sum(weights) / len(weights), 2) if weights else None
                min_w = round(min(weights), 2) if weights else None
                max_w = round(max(weights), 2) if weights else None
                conn.execute(
                    """
                    INSERT INTO daily_summary (
                        date, robot_serial, robot_name, cat_id, cat_name,
                        clean_cycles, cat_detects, cat_weight_avg_lbs, cat_weight_min_lbs,
                        cat_weight_max_lbs, dfi_level_end_pct, dfi_full_events,
                        other_events, first_event_utc, last_event_utc, active_hours
                    ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, NULL, 0, 0, NULL, NULL, 0.0)
                    """,
                    (date, serial, robot_name, cat_id, cat_name, count, avg_w, min_w, max_w),
                )
