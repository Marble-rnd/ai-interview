"""
Listwise reranking with Claude.

Fusion gets the right chunks into the candidate pool; reranking decides which
of them actually answer the question. It is the largest single precision win
in the retrieval stack, and it is the one part of reranking a small local
cross-encoder does worse — judging whether a passage answers a legal question
is a reading task, not a lexical-similarity task.

Listwise rather than pointwise: the model sees all candidates at once and
orders them, which costs one call instead of N and lets it compare.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from pydantic import BaseModel, Field

from .. import config
from ..case.retrieval import Hit
from .llm import LLM

logger = logging.getLogger(__name__)

# Enough of a chunk for a relevance judgement without paying for the whole
# thing; the full text is fetched later for whatever survives.
PREVIEW_CHARS = 700

_SYSTEM = (
    "You rank excerpts from a legal case file by how well each one answers a "
    "specific question an attorney asked.\n\n"
    "Rank by whether the excerpt contains the facts needed to answer, not by "
    "topical similarity. An excerpt that mentions the subject but carries no "
    "answer is not relevant. Prefer primary records (the filed document, the "
    "actual message) over summaries or notifications about them.\n\n"
    "Return the ids of the relevant excerpts, best first. Omit irrelevant ones "
    "entirely — returning fewer is correct when few are relevant."
)


class Ranking(BaseModel):
    relevant_ids: list[int] = Field(
        description="Candidate ids, most relevant first. Omit irrelevant candidates."
    )


def rerank(
    query: str,
    hits: Sequence[Hit],
    llm: LLM,
    *,
    top_k: int = 6,
    model: str | None = None,
) -> list[Hit]:
    """Reorder `hits` by relevance to `query`, keeping at most `top_k`.

    Falls back to the fused order if the model is unavailable or returns
    nothing usable — reranking is an improvement, never a dependency.
    """
    if len(hits) <= 1:
        return list(hits)[:top_k]

    lines = []
    for index, hit in enumerate(hits):
        chunk = hit.chunk
        stamp = chunk.when.strftime("%Y-%m-%d") if chunk.when else "undated"
        preview = " ".join(chunk.indexed_text().split())[:PREVIEW_CHARS]
        lines.append(f"[{index}] ({stamp}) {chunk.citation_label()}\n{preview}")

    prompt = (
        f"<question>{query}</question>\n\n"
        f"<candidates>\n{chr(10).join(lines)}\n</candidates>\n\n"
        f"Return at most {top_k} ids, best first."
    )

    ranking = llm.structured(
        Ranking,
        system=_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        model=model or config.MODEL,
        effort=config.EFFORT_RERANK,
        max_tokens=1000,
    )
    if ranking is None or not ranking.relevant_ids:
        logger.debug("Rerank produced nothing; keeping fused order")
        return list(hits)[:top_k]

    seen: set[int] = set()
    ordered: list[Hit] = []
    for index in ranking.relevant_ids:
        if 0 <= index < len(hits) and index not in seen:
            seen.add(index)
            hit = hits[index]
            hit.signals = {**hit.signals, "rerank": len(ordered) + 1}
            ordered.append(hit)
    for position, hit in enumerate(ordered, start=1):
        hit.rank = position
    return ordered[:top_k]
