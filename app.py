"""
app.py — Flask local web server for the Litter-Robot 4 Health Dashboard.

Start the server:
    python app.py

Binds to http://127.0.0.1:8080 by default.
Override the port with the PORT environment variable:
    PORT=9000 python app.py

Routes:
    GET /              → serves dashboard.html
    GET /api/summary   → rolling 30-day daily stats (per-robot + per-cat)
    GET /api/cats      → configured cat profiles from cat_profiles table
"""
from __future__ import annotations

import os
import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Flask, Response, jsonify

_BASE_DIR = Path(__file__).parent
DB_PATH = _BASE_DIR / "litterbot.db"
DASHBOARD_PATH = _BASE_DIR / "dashboard.html"

app = Flask(__name__)


# ─── DB helpers ───────────────────────────────────────────────────────────────

def _db_exists() -> bool:
    return DB_PATH.exists()


def _get_conn() -> sqlite3.Connection:
    """Open the database in read-only mode. Raises sqlite3.OperationalError if unavailable."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index() -> Response:
    """Serve the single-page dashboard HTML."""
    return Response(
        DASHBOARD_PATH.read_text(encoding="utf-8"),
        mimetype="text/html; charset=utf-8",
    )


@app.route("/api/cats")
def api_cats() -> Response:
    """
    Return all configured cat profiles.

    Response (200):
        [{"id": 1, "name": "Whiskers", "min_weight_lbs": 8.0, "max_weight_lbs": 11.0}, ...]

    Returns [] if the database does not exist yet.
    Returns {"error": "..."} with status 500 on database error.
    """
    if not _db_exists():
        return jsonify([])

    try:
        conn = _get_conn()
        try:
            rows = conn.execute(
                "SELECT id, name, min_weight_lbs, max_weight_lbs "
                "FROM cat_profiles ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/summary")
def api_summary() -> Response:
    """
    Return rolling 30-day daily summary data.

    Response (200):
    {
      "days": [
        {
          "date": "2026-02-06",
          "robot_serial": "LR4-xxx",
          "robot_name": "...",
          "clean_cycles": 5,
          "cat_detects": 8,
          "dfi_level_end_pct": 45.0,
          "dfi_full_events": 0,
          "other_events": 12,
          "first_event_utc": "...",
          "last_event_utc": "...",
          "active_hours": 18.5,
          "cats": [
            {
              "cat_id": 1,
              "cat_name": "Whiskers",
              "cat_detects": 8,
              "cat_weight_avg_lbs": 9.2,
              "cat_weight_min_lbs": 8.8,
              "cat_weight_max_lbs": 9.6
            }
          ]
        },
        ...
      ]
    }

    Only days with data are returned; the frontend fills gaps for the full 30-day window.
    Returns {"days": []} if the database does not exist yet.
    Returns {"error": "..."} with status 500 on database error.
    """
    if not _db_exists():
        return jsonify({"days": []})

    try:
        conn = _get_conn()
        try:
            today = date.today()
            start = (today - timedelta(days=29)).isoformat()
            end = today.isoformat()

            rows = conn.execute(
                """
                SELECT date, robot_serial, robot_name, cat_id, cat_name,
                       clean_cycles, cat_detects, cat_weight_avg_lbs, cat_weight_min_lbs,
                       cat_weight_max_lbs, dfi_level_end_pct, dfi_full_events,
                       other_events, first_event_utc, last_event_utc, active_hours
                FROM daily_summary
                WHERE date BETWEEN ? AND ?
                ORDER BY date ASC, robot_serial ASC, cat_name ASC
                """,
                (start, end),
            ).fetchall()
        finally:
            conn.close()

        # Group rows into one entry per (date, robot_serial).
        # Rows with cat_name IS NULL are aggregate (robot-level) rows.
        # Rows with cat_name set are per-cat rows.
        grouped: Dict[tuple, Dict[str, Any]] = defaultdict(lambda: {
            "date": None,
            "robot_serial": None,
            "robot_name": None,
            "clean_cycles": 0,
            "cat_detects": 0,
            "dfi_level_end_pct": None,
            "dfi_full_events": 0,
            "other_events": 0,
            "first_event_utc": None,
            "last_event_utc": None,
            "active_hours": 0.0,
            "cats": [],
        })

        for r in rows:
            key = (r["date"], r["robot_serial"])
            entry = grouped[key]

            if r["cat_name"] is None:
                # Aggregate (robot-level) row
                entry["date"] = r["date"]
                entry["robot_serial"] = r["robot_serial"]
                entry["robot_name"] = r["robot_name"]
                entry["clean_cycles"] = r["clean_cycles"]
                entry["cat_detects"] = r["cat_detects"]
                entry["dfi_level_end_pct"] = r["dfi_level_end_pct"]
                entry["dfi_full_events"] = r["dfi_full_events"]
                entry["other_events"] = r["other_events"]
                entry["first_event_utc"] = r["first_event_utc"]
                entry["last_event_utc"] = r["last_event_utc"]
                entry["active_hours"] = r["active_hours"]
            else:
                # Per-cat row
                if entry["date"] is None:
                    entry["date"] = r["date"]
                    entry["robot_serial"] = r["robot_serial"]
                    entry["robot_name"] = r["robot_name"]
                entry["cats"].append({
                    "cat_id": r["cat_id"],
                    "cat_name": r["cat_name"],
                    "cat_detects": r["cat_detects"],
                    "cat_weight_avg_lbs": r["cat_weight_avg_lbs"],
                    "cat_weight_min_lbs": r["cat_weight_min_lbs"],
                    "cat_weight_max_lbs": r["cat_weight_max_lbs"],
                })

        days: List[Dict] = [v for v in grouped.values() if v["date"] is not None]
        days.sort(key=lambda d: (d["date"], d["robot_serial"] or ""))

        return jsonify({"days": days})

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ─── Startup ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    host = "127.0.0.1"
    print(f"Litter-Robot Dashboard → http://{host}:{port}")
    app.run(host=host, port=port, debug=False)
