"""
run_ingest.py — Cron entry point for the daily LR4 ingest job.

Each run fetches the last 7 days of LR4 activity, stores new events in
litterbot.db, and appends a one-line summary to ingest.log.

Recommended crontab entry (runs at 03:00 local time every day):

    0 3 * * * /path/to/python /path/to/run_ingest.py

To edit the crontab:
    crontab -e

To verify it was saved:
    crontab -l

The script uses absolute paths derived from __file__ so it works regardless
of the working directory cron uses when invoking it.
"""
from __future__ import annotations

import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

_BASE_DIR = Path(__file__).parent
# Ensure local modules are importable when invoked from cron with a bare path
if str(_BASE_DIR) not in sys.path:
    sys.path.insert(0, str(_BASE_DIR))

LOG_PATH = _BASE_DIR / "ingest.log"


def _log(message: str) -> None:
    """Write a timestamped log line to both stdout and ingest.log."""
    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {message}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main() -> None:
    _log("Ingest started.")
    try:
        from ingest import run
        result = run()

        for robot in result.get("robots", []):
            _log(
                f"  Robot '{robot['name']}' ({robot['serial']}): "
                f"fetched={robot['fetched']}, new_inserted={robot['inserted']}"
            )

        for err in result.get("errors", []):
            _log(f"  ERROR: {err}")

        if not result.get("robots") and not result.get("errors"):
            _log("  No robots processed.")

        total = result.get("total_inserted", 0)
        _log(f"Ingest complete — {total} new event(s) stored.")

    except SystemExit as exc:
        _log(f"FATAL: {exc}")
        sys.exit(1)
    except Exception:
        _log(f"UNHANDLED EXCEPTION:\n{traceback.format_exc()}")
        sys.exit(1)


if __name__ == "__main__":
    main()
