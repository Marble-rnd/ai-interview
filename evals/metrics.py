"""
Scoring primitives.

Everything here is deterministic and runs without a model, which is what
makes tier 1 of the harness safe to run on every commit: no flaky judge call
can turn the suite red, and a retrieval regression is caught by numbers that
do not move on their own.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RankingScore:
    """Recall, precision and MRR for one ranked retrieval."""

    recall: float | None = 0.0
    precision: float | None = 0.0
    mrr: float | None = 0.0
    hits: int = 0
    gold: int = 0
    retrieved: int = 0
    first_hit_rank: int | None = None
    missed: list[str] = field(default_factory=list)


def score_ranking(retrieved: Sequence[str], gold: Iterable[str], k: int) -> RankingScore:
    """Score a ranked list of identifiers against a gold set.

    MRR uses the rank of the first gold item, which is the metric that moves
    when reranking works: fusion decides whether the right chunk is in the
    pool at all, reranking decides whether it is at the top.
    """
    gold_set = {g for g in gold if g}
    top = list(retrieved)[:k]
    if not gold_set:
        return RankingScore(retrieved=len(top), recall=None, precision=None, mrr=None)

    found = [item for item in top if item in gold_set]
    first_rank = next((i + 1 for i, item in enumerate(top) if item in gold_set), None)
    return RankingScore(
        recall=len(set(found)) / len(gold_set),
        precision=(len(found) / len(top)) if top else 0.0,
        mrr=(1.0 / first_rank) if first_rank else 0.0,
        hits=len(set(found)),
        gold=len(gold_set),
        retrieved=len(top),
        first_hit_rank=first_rank,
        missed=sorted(gold_set - set(found)),
    )


@dataclass
class ToolScore:
    """Did the agent reach for the right tools, with the right arguments?"""

    recall: float = 0.0
    precision: float = 0.0
    arg_correctness: float | None = None
    used: list[str] = field(default_factory=list)
    expected: list[str] = field(default_factory=list)
    missing_args: list[str] = field(default_factory=list)
    duplicate_calls: int = 0
    total_calls: int = 0


def score_tools(
    calls: Sequence[dict[str, Any]],
    gold_tools: Sequence[str],
    gold_tool_args: dict[str, Sequence[str]] | None = None,
) -> ToolScore:
    """Score tool selection.

    `gold_tools` is a set of *acceptable* tools rather than a required
    sequence: several routes to the same fact are legitimate, so recall here
    means "reached for at least one sensible tool", and precision is only
    meaningful when a gold set was specified.
    """
    used = [c["tool"] for c in calls]
    used_set = set(used)
    gold_set = set(gold_tools)

    signatures = [f"{c['tool']}:{sorted((c.get('arguments') or {}).items())}" for c in calls]
    duplicates = len(signatures) - len(set(signatures))

    score = ToolScore(
        used=sorted(used_set),
        expected=sorted(gold_set),
        duplicate_calls=duplicates,
        total_calls=len(calls),
    )
    if gold_set:
        score.recall = len(used_set & gold_set) / len(gold_set)
        score.precision = (len(used_set & gold_set) / len(used_set)) if used_set else 0.0

    if gold_tool_args:
        required: list[tuple[str, str]] = [
            (tool, arg) for tool, args in gold_tool_args.items() for arg in args
        ]
        satisfied = 0
        for tool, arg in required:
            calls_for_tool = [c for c in calls if c["tool"] == tool]
            if any((c.get("arguments") or {}).get(arg) not in (None, "") for c in calls_for_tool):
                satisfied += 1
            else:
                score.missing_args.append(f"{tool}.{arg}")
        score.arg_correctness = (satisfied / len(required)) if required else None
    return score


@dataclass
class CitationScore:
    # None, not 0.0, when the item has no gold citation set: an item that
    # cannot be scored must not drag the mean down as if it scored zero.
    precision: float | None = None
    recall: float | None = None
    cited: list[str] = field(default_factory=list)
    gold: list[str] = field(default_factory=list)
    spurious: list[str] = field(default_factory=list)


def score_citations(
    citations: Sequence[dict[str, Any]],
    gold_events: Sequence[str],
    gold_documents: Sequence[str],
) -> CitationScore:
    """Precision and recall over what the answer actually cited.

    Distinct from retrieval recall: retrieval asks whether the evidence was
    *found*, citation asks whether the answer *used* it. Separating the two
    tells you whether to fix the index or the prompt.
    """
    gold = {g for g in list(gold_events) + list(gold_documents) if g}
    cited = {(c.get("document_name") or c.get("ref")) for c in citations if c.get("cited")}
    cited = {c for c in cited if c}
    if not gold:
        return CitationScore(cited=sorted(cited), gold=[])

    matched = cited & gold
    return CitationScore(
        precision=(len(matched) / len(cited)) if cited else 0.0,
        recall=len(matched) / len(gold),
        cited=sorted(cited),
        gold=sorted(gold),
        spurious=sorted(cited - gold),
    )


def check_assertions(answer: str, must: Sequence[str], must_not: Sequence[str]) -> dict[str, Any]:
    """Regex assertions against the answer text, case-insensitively."""
    failed_required = [p for p in must if not re.search(p, answer, re.I | re.S)]
    failed_forbidden = [p for p in must_not if re.search(p, answer, re.I | re.S)]
    return {
        "passed": not failed_required and not failed_forbidden,
        "missing": failed_required,
        "forbidden_present": failed_forbidden,
    }


def mean(values: Iterable[float | None]) -> float | None:
    """Mean over the values that exist. None when nothing is scoreable, so a
    metric with no applicable items renders as "—" rather than 0%."""
    present = [v for v in values if v is not None]
    return round(statistics.fmean(present), 4) if present else None


def percentile(values: Sequence[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((p / 100) * (len(ordered) - 1)))))
    return round(ordered[index], 1)
