# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with this repository.

## Commands

```bash
# Install dependencies (Python 3.8+)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run the dashboard server (http://127.0.0.1:8080)
python app.py
PORT=9000 python app.py   # custom port

# Run ingest manually (requires LR_USERNAME / LR_PASSWORD in .env or env)
python run_ingest.py

# Initialize or re-initialize the database schema
python db.py

# Import historical CSV files (named litterbot_activity_YYYY-MM-DD.csv)
# and/or recompute daily_summary after editing cats.toml
python migrate_csv.py
python migrate_csv.py /path/to/csvdir   # specify directory

# Set up daily cron job (runs at 03:00, uses absolute paths — works from any cwd)
# 0 3 * * * /path/to/.venv/bin/python /path/to/run_ingest.py
```

Ingest results (including errors) are appended to `ingest.log` in the project root.

## Architecture

```
LR4 API  ──►  run_ingest.py  ──►  litterbot.db  ──►  app.py  ──►  browser
(daily cron)   (fetch+classify)    (SQLite/WAL)    (Flask)    (dashboard.html)
```

The system is a single-machine, no-auth local app. There is no test suite.

### Data flow

1. `run_ingest.py` is the cron entry point; it calls `ingest.run()` and logs to `ingest.log`.
2. `ingest.py` does the heavy lifting:
   - Loads cat weight-band profiles from `cats.toml` via `cats.py` and syncs them to `cat_profiles`.
   - Authenticates with the pylitterbot API (unofficial, may break on upstream changes) and fetches 7-day history per LR4 robot.
   - Inserts events into `raw_events` with `INSERT OR IGNORE` for deduplication on `(robot_serial, timestamp_utc, action)`.
   - Calls `summary.recompute()` for every date that received new events.
3. `summary.py` deletes and re-inserts `daily_summary` rows (idempotent). Two row types per `(date, robot_serial)`: `cat_name IS NULL` for robot-level aggregates, `cat_name = <name>` for per-cat stats.
4. `app.py` serves `dashboard.html` and two read-only JSON endpoints (`/api/summary` — rolling 30-day window, `/api/cats`). It opens the database in `mode=ro` to avoid write conflicts with ingest.

### Database schema (SQLite, WAL mode)

- **`cat_profiles`** — cat name + weight band, populated from `cats.toml` on every ingest run. IDs are stable 1-indexed positions in the TOML array; foreign keys in child tables depend on this stability.
- **`raw_events`** — every LR4 activity event, deduplicated by `UNIQUE(robot_serial, timestamp_utc, action)`.
- **`daily_summary`** — pre-aggregated stats; uniqueness enforced via expression index `COALESCE(cat_name, '')` so the NULL aggregate row and named-cat rows coexist.

### Cat classification

`cats.py` maps weight readings on `ROBOT_CAT_DETECT` events to a named cat using non-overlapping bands in `cats.toml`. A weight matching zero or multiple bands is stored as `Unknown`. `sync_to_db()` nullifies foreign-key references before removing deleted cat profiles to preserve historical data.

`cats.toml` format — each entry is a `[[cats]]` array item with `name`, `min_weight_lbs`, and `max_weight_lbs`. Cat IDs are their 1-indexed position in the array; reordering entries changes IDs and invalidates historical `cat_id` references.

### Credentials

`LR_USERNAME` and `LR_PASSWORD` are read from environment variables or a `.env` file (never committed). The `.env` loader in `ingest.py` is minimal — it only sets variables not already in the environment, and strips surrounding quotes.
