"""
cats.py — Load cat profiles from cats.toml and classify weight readings.

Cat profiles drive the classification of ROBOT_CAT_DETECT events into named
cats or "Unknown" when the weight falls outside all defined bands.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

CATS_TOML_PATH = Path(__file__).parent / "cats.toml"

# Type alias
CatProfile = Dict  # {id: int, name: str, min_weight_lbs: float, max_weight_lbs: float}


def load_profiles(path: Path = CATS_TOML_PATH) -> List[CatProfile]:
    """
    Parse cats.toml and return a list of validated cat profile dicts.
    Each dict: {id (1-indexed), name, min_weight_lbs, max_weight_lbs}.
    Returns [] if the file does not exist, is empty, or cannot be parsed.
    Prints warnings to stderr for skipped or overlapping entries.
    """
    if not path.exists():
        return []

    if sys.version_info >= (3, 11):
        import tomllib
    else:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            print(
                "Warning: tomli not installed. Run: pip install tomli",
                file=sys.stderr,
            )
            return []

    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except Exception as exc:
        print(f"Warning: could not parse cats.toml: {exc}", file=sys.stderr)
        return []

    profiles: List[CatProfile] = []
    for i, cat in enumerate(data.get("cats", []), start=1):
        name = (cat.get("name") or "").strip()
        if not name:
            print(f"Warning: cats.toml entry {i} has no name — skipped.", file=sys.stderr)
            continue
        try:
            lo = float(cat["min_weight_lbs"])
            hi = float(cat["max_weight_lbs"])
        except (KeyError, TypeError, ValueError):
            print(
                f"Warning: cat '{name}' missing or invalid weight fields — skipped.",
                file=sys.stderr,
            )
            continue
        if lo >= hi:
            print(
                f"Warning: cat '{name}' has min_weight_lbs >= max_weight_lbs — skipped.",
                file=sys.stderr,
            )
            continue
        profiles.append({"id": i, "name": name, "min_weight_lbs": lo, "max_weight_lbs": hi})

    # Warn on overlapping bands (does not skip — overlapping weights become Unknown)
    for i, a in enumerate(profiles):
        for b in profiles[i + 1 :]:
            if a["min_weight_lbs"] <= b["max_weight_lbs"] and b["min_weight_lbs"] <= a["max_weight_lbs"]:
                print(
                    f"Warning: weight bands overlap between '{a['name']}' "
                    f"({a['min_weight_lbs']}–{a['max_weight_lbs']} lbs) and "
                    f"'{b['name']}' ({b['min_weight_lbs']}–{b['max_weight_lbs']} lbs). "
                    "Readings in the overlap zone will be recorded as Unknown.",
                    file=sys.stderr,
                )

    return profiles


def classify_weight(
    weight: Optional[float],
    profiles: List[CatProfile],
) -> Tuple[Optional[int], Optional[str]]:
    """
    Return (cat_id, cat_name) if weight falls within exactly one cat band.
    Returns (None, None) for None/zero weight, no match, or ambiguous match.
    """
    if weight is None or weight <= 0:
        return None, None
    matches = [p for p in profiles if p["min_weight_lbs"] <= weight <= p["max_weight_lbs"]]
    if len(matches) == 1:
        p = matches[0]
        return p["id"], p["name"]
    return None, None


def sync_to_db(conn, profiles: List[CatProfile]) -> None:
    """
    Upsert cat_profiles from the given profiles list.
    Profiles absent from the new list are removed after nullifying their
    cat_id references in child tables, preserving historical data for cats
    that still exist.
    Uses stable 1-indexed IDs so foreign keys in raw_events remain valid.
    Must be called inside a transaction.
    """
    new_ids = {p["id"] for p in profiles}

    if new_ids:
        placeholders = ",".join("?" * len(new_ids))
        id_list = list(new_ids)
        # Nullify references only for cats being removed, then delete them
        conn.execute(
            f"UPDATE raw_events SET cat_id = NULL WHERE cat_id NOT IN ({placeholders})",
            id_list,
        )
        conn.execute(
            f"UPDATE daily_summary SET cat_id = NULL WHERE cat_id NOT IN ({placeholders})",
            id_list,
        )
        conn.execute(
            f"DELETE FROM cat_profiles WHERE id NOT IN ({placeholders})",
            id_list,
        )
    else:
        conn.execute("UPDATE raw_events SET cat_id = NULL")
        conn.execute("UPDATE daily_summary SET cat_id = NULL")
        conn.execute("DELETE FROM cat_profiles")

    for p in profiles:
        conn.execute(
            """
            INSERT INTO cat_profiles (id, name, min_weight_lbs, max_weight_lbs)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name           = excluded.name,
                min_weight_lbs = excluded.min_weight_lbs,
                max_weight_lbs = excluded.max_weight_lbs
            """,
            (p["id"], p["name"], p["min_weight_lbs"], p["max_weight_lbs"]),
        )
