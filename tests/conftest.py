from __future__ import annotations

from pathlib import Path

import pytest

from src.case.documents import build_catalog
from src.case.normalize import normalize_case
from src.case.roster import apply_roster, build_roster
from src.data_loader import list_cases, load_timeline

CASE_DATA = Path(__file__).resolve().parents[1] / "case_data"
CASES = list_cases(CASE_DATA)


@pytest.fixture(scope="session")
def case_dirs() -> list[Path]:
    return [CASE_DATA / name for name in CASES]


@pytest.fixture(scope="session")
def loaded() -> dict[str, dict]:
    """Normalize both bundled cases once for the whole test session."""
    out: dict[str, dict] = {}
    for name in CASES:
        case_dir = CASE_DATA / name
        rows = load_timeline(case_dir)
        events, report = normalize_case(rows)
        roster = build_roster(rows, events)
        apply_roster(events, roster)
        out[name] = {
            "dir": case_dir,
            "rows": rows,
            "events": events,
            "report": report,
            "roster": roster,
            "catalog": build_catalog(case_dir, events),
        }
    return out
