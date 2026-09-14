"""Central configuration: paths, model settings, and the injectable clock."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]

# Always load by explicit path: `find_dotenv()` inspects the caller's stack
# frame and raises when the caller is stdin (piped scripts, some runners).
load_dotenv(ROOT / ".env")

CASE_DATA_DIR = Path(os.getenv("CASE_DATA_DIR", ROOT / "case_data"))
INDEX_DIR = Path(os.getenv("INDEX_DIR", ROOT / "index"))
RUNS_DIR = Path(os.getenv("RUNS_DIR", ROOT / "runs"))

# Bumped whenever ingestion changes in a way that invalidates existing
# artifacts. The manifest records it so a stale index is detectable.
INDEX_VERSION = "v1"

MODEL = os.getenv("CASE_AGENT_MODEL", "claude-opus-5")
# A small model is enough for mechanical ingest work (contextual blurbs,
# headline normalization) that is graded by retrieval metrics, not prose.
ENRICHMENT_MODEL = os.getenv("CASE_ENRICHMENT_MODEL", "claude-haiku-4-5")

# Effort per call site. The executor is the only place that needs depth.
EFFORT_EXECUTOR = "high"
EFFORT_PLANNER = "low"
EFFORT_RERANK = "low"
EFFORT_VALIDATOR = "low"
EFFORT_JUDGE = "high"

# USD per million tokens, used for the cost line in traces and eval reports.
PRICING = {
    "claude-opus-5": {"input": 5.00, "output": 25.00},
    "claude-sonnet-5": {"input": 2.00, "output": 10.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
}
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25

# The bundled snapshots end 2026-09-08. Pinning "now" keeps relative questions
# ("the last two weeks") reproducible across eval runs; override for live use.
_PINNED = os.getenv("CASE_AGENT_NOW", "2026-09-10T12:00:00+00:00")


def now() -> datetime:
    """The current time, pinned by default so evals are deterministic."""
    if _PINNED:
        parsed = datetime.fromisoformat(_PINNED)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def case_dir(case_slug: str) -> Path:
    return CASE_DATA_DIR / case_slug


def artifact_dir(case_slug: str) -> Path:
    return INDEX_DIR / case_slug / INDEX_VERSION
