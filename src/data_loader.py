"""
Utilities for loading case data from disk.

Each case directory contains:
  - case-timeline.csv: Chronological log of all case activity
  - documents/: Raw case documents (PDFs, DOCXs, images)
"""

import csv
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


def load_timeline(case_dir: str | Path) -> list[dict]:
    """Load case-timeline.csv as a list of dicts, sorted by activity_created_at."""
    path = Path(case_dir) / "case-timeline.csv"
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    rows.sort(key=lambda r: r.get("activity_created_at", ""))
    return rows


def list_documents(case_dir: str | Path) -> list[str]:
    """Return filenames of all documents in the case's documents/ directory."""
    docs_dir = Path(case_dir) / "documents"
    if not docs_dir.exists():
        return []
    return sorted(f.name for f in docs_dir.iterdir() if f.is_file())


def list_cases(data_dir: str | Path = "case_data") -> list[str]:
    """Return case directory names available under data_dir."""
    base = Path(data_dir)
    return sorted(
        d.name for d in base.iterdir() if d.is_dir() and (d / "case-timeline.csv").exists()
    )
