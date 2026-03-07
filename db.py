"""
db.py — SQLite schema initialisation and connection helpers.

Three tables:
  cat_profiles  — identity + weight band per cat (loaded from cats.toml)
  raw_events    — every LR4 activity event, deduplicated by (robot_serial, timestamp_utc, action)
  daily_summary — pre-aggregated daily stats per robot and per cat

Call init_db() once on startup (idempotent).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "litterbot.db"


def get_connection(path: Path = DB_PATH) -> sqlite3.Connection:
    """Open (or create) the SQLite database and return a connection."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_connection_ro(path: Path = DB_PATH) -> sqlite3.Connection:
    """Open the database read-only (for the Flask API server)."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path: Path = DB_PATH) -> None:
    """Create all tables and indexes if they do not already exist (idempotent)."""
    conn = get_connection(path)
    with conn:
        conn.executescript("""
            -- Cat identity configuration (populated from cats.toml on each ingest run)
            CREATE TABLE IF NOT EXISTS cat_profiles (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                name           TEXT    NOT NULL,
                min_weight_lbs REAL    NOT NULL,
                max_weight_lbs REAL    NOT NULL
            );

            -- Every raw LR4 activity event.
            -- UNIQUE(robot_serial, timestamp_utc, action) enforces deduplication:
            -- INSERT OR IGNORE silently skips duplicates on re-run.
            CREATE TABLE IF NOT EXISTS raw_events (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                robot_serial          TEXT    NOT NULL,
                robot_name            TEXT,
                timestamp_utc         TEXT    NOT NULL,
                action                TEXT    NOT NULL,
                action_label          TEXT,
                cat_detect            TEXT,
                cat_detect_label      TEXT,
                cat_weight_lbs        REAL,
                dfi_level_percent     REAL,
                is_dfi_full           INTEGER,
                odometer_clean_cycles INTEGER,
                cat_id                INTEGER REFERENCES cat_profiles(id),
                fetched_at            TEXT    NOT NULL,
                UNIQUE(robot_serial, timestamp_utc, action)
            );

            -- Pre-aggregated daily stats.
            -- Two row types per (date, robot_serial):
            --   cat_name IS NULL  → robot-level aggregate (clean_cycles, dfi, etc.)
            --   cat_name = <name> → per-cat stats (cat_detects, weights)
            --   cat_name = 'Unknown' → unclassified cat-detect events
            --
            -- Uniqueness is enforced via expression index using COALESCE so that
            -- the NULL aggregate row and named cat rows cannot conflict.
            CREATE TABLE IF NOT EXISTS daily_summary (
                date               TEXT    NOT NULL,
                robot_serial       TEXT    NOT NULL,
                robot_name         TEXT,
                cat_id             INTEGER REFERENCES cat_profiles(id),
                cat_name           TEXT,
                clean_cycles       INTEGER NOT NULL DEFAULT 0,
                cat_detects        INTEGER NOT NULL DEFAULT 0,
                cat_weight_avg_lbs REAL,
                cat_weight_min_lbs REAL,
                cat_weight_max_lbs REAL,
                dfi_level_end_pct  REAL,
                dfi_full_events    INTEGER NOT NULL DEFAULT 0,
                other_events       INTEGER NOT NULL DEFAULT 0,
                first_event_utc    TEXT,
                last_event_utc     TEXT,
                active_hours       REAL    NOT NULL DEFAULT 0.0
            );

            -- Expression index treats NULL cat_name as '' so aggregate and
            -- Unknown rows can coexist without a PRIMARY KEY on nullable columns.
            CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_summary_key
            ON daily_summary(date, robot_serial, COALESCE(cat_name, ''));
        """)
    conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Database initialised: {DB_PATH.resolve()}")
