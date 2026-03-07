# Litter-Robot 4 Health Tracker & Dashboard

A self-contained local system that automatically collects Litter-Robot 4 activity
data, stores it in a SQLite database, and serves a rolling 30-day web dashboard
at `http://127.0.0.1:8080`.

---

## How it works

```
LR4 API  ──►  run_ingest.py  ──►  litterbot.db  ──►  app.py  ──►  browser
(daily cron)    (fetch + classify)   (SQLite)       (Flask)     (dashboard)
```

1. A daily cron job runs `run_ingest.py`, which fetches the last 7 days of LR4
   activity and stores new events in `litterbot.db`.
2. Each ROBOT_CAT_DETECT event is classified to a named cat based on weight bands
   defined in `cats.toml`.
3. `app.py` serves a local web dashboard that auto-loads and refreshes every
   5 minutes.

---

## Prerequisites

- Python 3.8 or higher
- A Litter-Robot 4 account (LR_USERNAME / LR_PASSWORD)
- pip

---

## Installation

```bash
# 1. Clone or copy this directory to your machine
cd /path/to/litterbot

# 2. (Recommended) Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate      # macOS / Linux
# .venv\Scripts\activate       # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set your credentials in .env (never commit this file)
cp .env.example .env           # if .env.example is provided
#   -- or create .env manually:
echo 'LR_USERNAME="your@email.com"' >> .env
echo 'LR_PASSWORD="yourpassword"'   >> .env

# 5. Configure your cat(s) — see section below
#    Edit cats.toml before running the ingest for the first time.
```

---

## Configuring cats (cats.toml)

The LR4 does not identify cats by name. `cats.toml` maps weight readings to cat
identities. Open `cats.toml` and define one `[[cats]]` block per cat:

```toml
[[cats]]
name = "Whiskers"
min_weight_lbs = 8.0
max_weight_lbs = 11.0
```

**Two-cat example:**

```toml
[[cats]]
name = "Whiskers"
min_weight_lbs = 8.0
max_weight_lbs = 11.0

[[cats]]
name = "Shadow"
min_weight_lbs = 12.0
max_weight_lbs = 15.0
```

Rules:
- Bands must **not overlap**. Any weight in an overlap zone is recorded as
  "Unknown".
- `min_weight_lbs` must be strictly less than `max_weight_lbs`.
- Names are case-sensitive and must be unique.

To apply changes to historical data after editing `cats.toml`, run:

```bash
python migrate_csv.py
```

(or simply wait — the next ingest run will reclassify future events automatically)

---

## First run — migrate historical CSV data (optional)

If you have existing `litterbot_activity_YYYY-MM-DD.csv` files from earlier runs
of `fetch_litterbot_activity.py`, import them into the database with:

```bash
python migrate_csv.py
```

This is idempotent — safe to re-run. It searches the current directory for CSV
files matching the `litterbot_activity_YYYY-MM-DD.csv` pattern.

---

## Running the ingest manually

```bash
python run_ingest.py
```

Output is written to stdout and appended to `ingest.log`.

---

## Automating the ingest with cron

Run `crontab -e` and add:

```cron
# Fetch LR4 activity every day at 03:00
0 3 * * * /full/path/to/python /full/path/to/litterbot/run_ingest.py
```

Replace `/full/path/to/python` with the path from `which python3` (or the path
inside your virtualenv: `/path/to/litterbot/.venv/bin/python`).

Replace `/full/path/to/litterbot/run_ingest.py` with the absolute path to this
file.

To verify the crontab was saved:

```bash
crontab -l
```

Logs are written to `ingest.log` in the project directory.

---

## Starting the dashboard server

```bash
python app.py
```

Then open **http://127.0.0.1:8080** in your browser.

The dashboard auto-refreshes every 5 minutes. Use the REFRESH button to reload
immediately.

To use a different port:

```bash
PORT=9000 python app.py
```

---

## File reference

| File | Purpose |
|---|---|
| `app.py` | Flask web server — serves dashboard and JSON API |
| `dashboard.html` | Single-page dashboard (served by Flask, not opened directly) |
| `ingest.py` | LR4 API fetch, cat classification, SQLite persistence |
| `run_ingest.py` | Cron entry point — wraps ingest.py with logging |
| `migrate_csv.py` | One-time import of historical CSV files |
| `db.py` | SQLite schema initialisation and connection helpers |
| `cats.py` | cats.toml loader and weight-band classification logic |
| `summary.py` | Daily summary recomputation from raw_events |
| `cats.toml` | Cat weight band configuration (edit this) |
| `litterbot.db` | SQLite database (created on first run, not in git) |
| `ingest.log` | Append-only ingest run log (not in git) |
| `.env` | LR4 credentials (never commit) |

---

## API endpoints

The Flask server exposes two JSON endpoints used by the dashboard:

| Endpoint | Description |
|---|---|
| `GET /api/summary` | Rolling 30-day daily stats (robot-level + per-cat breakdown) |
| `GET /api/cats` | Configured cat profiles from the database |

---

## Troubleshooting

**Dashboard shows "No data recorded yet"**
Run `python run_ingest.py` and check `ingest.log` for errors.

**Ingest fails with "credentials required"**
Ensure `LR_USERNAME` and `LR_PASSWORD` are set in `.env` or the environment.

**Weight readings show as Unknown**
The weight fell outside all defined bands in `cats.toml`, or no weight was
recorded for that visit. Adjust the `min_weight_lbs` / `max_weight_lbs` values
and re-run `python migrate_csv.py` to reclassify historical data.

**pylitterbot API error**
The pylitterbot library uses an unofficial, reverse-engineered API. If the LR4
cloud API changes, fetches may fail until the library is updated. Check for a
newer version: `pip install --upgrade pylitterbot`.
