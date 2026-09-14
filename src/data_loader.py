"""
Raw I/O for case data on disk.

This module is deliberately thin: it reads bytes off disk and parses the
primitive types (timestamps, the JSON-inside-CSV `extra_details` column).
All semantics — what an event *means*, who the actor is, whether a message was
automated — live in `src/case/normalize.py`.

Each case directory contains:
  - case-timeline.csv: chronological log of all case activity
  - documents/: raw case documents (PDFs, DOCXs, images)
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

TIMELINE_COLUMNS = [
    "id",
    "start_date",
    "end_date",
    "insert_timestamp",
    "update_timestamp",
    "activity_created_at",
    "case_id",
    "int_user_marble_id",
    "ext_user_marble_id",
    "activity_type",
    "activity_sub_type",
    "direction",
    "content",
    "extra_details",
    "source",
]

# Timestamp columns that callers are expected to compare or sort on. The
# warehouse bookkeeping columns (insert_/update_timestamp) are excluded: the
# data dictionary says to ignore them for the case narrative.
TIMESTAMP_COLUMNS = ("activity_created_at", "start_date", "end_date")

# A single `content` cell already reaches ~38k characters (a call transcript)
# and `extra_details` blobs are comparable. csv's default field limit is
# 131_072 bytes, so a slightly larger transcript in a future case would raise
# `_csv.Error: field larger than field limit`. Raise it once, at import.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse a timeline timestamp into a timezone-aware UTC datetime.

    Timeline timestamps are naive ISO-8601 (`2026-08-19T12:46:01`, sometimes
    with microseconds). They are UTC in the source systems, so naive values are
    stamped as UTC rather than guessed at.
    """
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    # Tolerate the `+00` offset shorthand seen in some extra_details fields.
    if text.endswith("+00"):
        text = text[:-3] + "+00:00"
    text = text.replace(" ", "T", 1) if " " in text and "T" not in text else text
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def parse_extra_details(value: str | None) -> dict:
    """Parse the `extra_details` JSON-inside-CSV column.

    Returns `{}` for empty or malformed values — a single bad row must never
    take down ingestion of a whole case.
    """
    if not value or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def load_timeline(case_dir: str | Path) -> list[dict]:
    """Load case-timeline.csv, sorted chronologically.

    Sorting is on the parsed `activity_created_at` (the data dictionary's
    chronological key), with `id` as a tiebreaker so the order is stable across
    runs. Rows keep their raw string values; parsed timestamps are attached
    under `_ts` so callers do not re-parse.
    """
    path = Path(case_dir) / "case-timeline.csv"
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    for row in rows:
        row["_ts"] = {col: parse_timestamp(row.get(col)) for col in TIMESTAMP_COLUMNS}

    epoch = datetime.min.replace(tzinfo=timezone.utc)
    rows.sort(key=lambda r: (r["_ts"]["activity_created_at"] or epoch, r.get("id") or ""))
    return rows


def list_documents(case_dir: str | Path) -> list[str]:
    """Return filenames of all documents in the case's documents/ directory."""
    docs_dir = Path(case_dir) / "documents"
    if not docs_dir.exists():
        return []
    return sorted(f.name for f in docs_dir.iterdir() if f.is_file() and not f.name.startswith("."))


def document_path(case_dir: str | Path, filename: str) -> Path | None:
    """Resolve a document filename to a path inside the case, or None.

    Rejects anything that escapes the documents/ directory.
    """
    docs_dir = (Path(case_dir) / "documents").resolve()
    candidate = (docs_dir / filename).resolve()
    if not candidate.is_file() or docs_dir not in candidate.parents:
        return None
    return candidate


def list_cases(data_dir: str | Path = "case_data") -> list[str]:
    """Return case directory names available under data_dir."""
    base = Path(data_dir)
    if not base.exists():
        return []
    return sorted(
        d.name for d in base.iterdir() if d.is_dir() and (d / "case-timeline.csv").exists()
    )
