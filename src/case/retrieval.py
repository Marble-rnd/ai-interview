"""
Hybrid retrieval over the chunk index.

Six modes exist so the eval can prove which parts earn their keep rather
than assuming them:

| mode              | what it is                                              |
|-------------------|---------------------------------------------------------|
| `bm25`            | sparse over the raw chunk text                            |
| `bm25_contextual` | sparse over Claude's blurb + the chunk (isolates the blurb)|
| `dense`           | local ONNX vectors over blurb + chunk                     |
| `hybrid_raw`      | RRF fusion of raw BM25 + dense (ablation)                 |
| `hybrid`          | RRF fusion of contextual BM25 + dense (default)           |
| `hybrid_rerank`   | hybrid, then a Claude listwise rerank                     |

Fusion is Reciprocal Rank Fusion rather than a weighted score sum: BM25
scores are unbounded and cosine similarity is bounded, so combining the raw
numbers compares incommensurable scales. RRF works on ranks and needs no
tuning.

Exact filters are a separate path. "Everything inbound in the last two weeks"
is a `where` clause, not a similarity problem, and routing it through a
ranker would turn a precise answer into an approximate one.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Literal

import numpy as np
from pydantic import BaseModel, Field
from rank_bm25 import BM25Okapi

from . import embeddings
from .aliases import expanded_query, normalize_query
from .models import Chunk, NormalizedEvent

logger = logging.getLogger(__name__)

Mode = Literal["bm25", "bm25_contextual", "dense", "hybrid_raw", "hybrid", "hybrid_rerank"]
ALL_MODES: tuple[Mode, ...] = (
    "bm25",
    "bm25_contextual",
    "dense",
    "hybrid_raw",
    "hybrid",
    "hybrid_rerank",
)

# Standard RRF constant. Damps the influence of any single ranker's top slot.
RRF_K = 60

# How many candidates each signal contributes before fusion.
CANDIDATE_DEPTH = 40

_TOKEN = re.compile(r"[a-z0-9][a-z0-9'&.-]*")


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens, with form codes folded to one token.

    `FL-150` must survive tokenization intact; splitting it into `fl` and
    `150` is what makes every California form look alike to BM25.
    """
    return _TOKEN.findall(normalize_query(text).replace("-", "_").replace("_", "-"))


class Hit(BaseModel):
    chunk: Chunk
    score: float
    rank: int = 0
    # Which signals surfaced this chunk, and at what rank in each. Reported in
    # traces so a retrieval miss can be attributed to a specific retriever.
    signals: dict[str, int] = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True}


class Filters(BaseModel):
    """Exact predicates applied before any scoring."""

    source: Literal["event", "document", "any"] = "any"
    activity_types: list[str] = Field(default_factory=list)
    activity_sub_types: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)
    directions: list[str] = Field(default_factory=list)
    date_from: datetime | None = None
    date_to: datetime | None = None
    exclude_automated: bool = False
    document_names: list[str] = Field(default_factory=list)


class Retriever:
    """Sparse + dense retrieval over one case's chunks.

    The vector store is a plain normalized numpy matrix: a few thousand rows
    of 384 floats, where cosine similarity is one matrix-vector product.
    Introducing a vector database at this size would add operational surface
    without changing a single result.
    """

    def __init__(
        self,
        chunks: Sequence[Chunk],
        vectors: np.ndarray | None = None,
        events: Sequence[NormalizedEvent] = (),
    ) -> None:
        self.chunks = list(chunks)
        self.vectors = vectors
        self.events_by_ref = {e.ref: e for e in events}

        self._bm25_raw: BM25Okapi | None = None
        self._bm25_contextual: BM25Okapi | None = None

    # -- indexes are built lazily so an unused mode costs nothing ---------

    @property
    def bm25_raw(self) -> BM25Okapi:
        if self._bm25_raw is None:
            self._bm25_raw = BM25Okapi([tokenize(c.text) for c in self.chunks])
        return self._bm25_raw

    @property
    def bm25_contextual(self) -> BM25Okapi:
        if self._bm25_contextual is None:
            self._bm25_contextual = BM25Okapi([tokenize(c.indexed_text()) for c in self.chunks])
        return self._bm25_contextual

    @property
    def dense_available(self) -> bool:
        return self.vectors is not None and len(self.vectors) == len(self.chunks)

    # -- filtering --------------------------------------------------------

    def _allowed(self, filters: Filters | None) -> set[int] | None:
        """Indices surviving the exact predicates, or None for 'everything'."""
        if filters is None:
            return None
        allowed: set[int] = set()
        for index, chunk in enumerate(self.chunks):
            if filters.source != "any" and chunk.source != filters.source:
                continue
            if filters.document_names and chunk.document_name not in filters.document_names:
                continue
            if filters.date_from and (chunk.when is None or chunk.when < filters.date_from):
                continue
            if filters.date_to and (chunk.when is None or chunk.when > filters.date_to):
                continue

            event = self.events_by_ref.get(chunk.event_ref or "")
            if event is None:
                # Document chunks pass any event-shaped predicate only when
                # none was requested.
                if (
                    filters.activity_types
                    or filters.activity_sub_types
                    or filters.channels
                    or filters.directions
                    or filters.exclude_automated
                ):
                    continue
            else:
                if filters.activity_types and event.activity_type not in filters.activity_types:
                    continue
                if (
                    filters.activity_sub_types
                    and event.activity_sub_type not in filters.activity_sub_types
                ):
                    continue
                if filters.channels and event.channel not in filters.channels:
                    continue
                if filters.directions and event.direction not in filters.directions:
                    continue
                if filters.exclude_automated and event.is_automated:
                    continue
            allowed.add(index)
        return allowed

    # -- individual signals -----------------------------------------------

    def _rank_sparse(
        self, index: BM25Okapi, query: str, allowed: set[int] | None, depth: int
    ) -> list[int]:
        scores = index.get_scores(tokenize(query))
        order = np.argsort(scores)[::-1]
        out = []
        for i in order:
            i = int(i)
            if scores[i] <= 0:
                break
            if allowed is not None and i not in allowed:
                continue
            out.append(i)
            if len(out) >= depth:
                break
        return out

    def _rank_dense(self, query: str, allowed: set[int] | None, depth: int) -> list[int]:
        if not self.dense_available:
            return []
        vector = embeddings.embed([query], is_query=True)
        if vector is None:
            return []
        scores = self.vectors @ vector[0]
        order = np.argsort(scores)[::-1]
        out = []
        for i in order:
            i = int(i)
            if allowed is not None and i not in allowed:
                continue
            out.append(i)
            if len(out) >= depth:
                break
        return out

    @staticmethod
    def _fuse(rankings: dict[str, list[int]]) -> dict[int, tuple[float, dict[str, int]]]:
        """Reciprocal Rank Fusion over per-signal rankings."""
        fused: dict[int, tuple[float, dict[str, int]]] = {}
        for name, ranking in rankings.items():
            for position, index in enumerate(ranking):
                score, signals = fused.get(index, (0.0, {}))
                fused[index] = (
                    score + 1.0 / (RRF_K + position + 1),
                    {**signals, name: position + 1},
                )
        return fused

    # -- public API --------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        k: int = 8,
        mode: Mode = "hybrid",
        filters: Filters | None = None,
        depth: int = CANDIDATE_DEPTH,
        expand: bool = True,
    ) -> list[Hit]:
        """Retrieve the top `k` chunks for a query."""
        allowed = self._allowed(filters)
        if allowed is not None and not allowed:
            return []

        # Alias expansion only helps the lexical side; the dense model already
        # handles paraphrase and extra tokens dilute the query vector.
        sparse_query = expanded_query(query) if expand else query

        if mode == "bm25":
            ranking = {"bm25": self._rank_sparse(self.bm25_raw, sparse_query, allowed, depth)}
        elif mode == "bm25_contextual":
            ranking = {
                "bm25c": self._rank_sparse(self.bm25_contextual, sparse_query, allowed, depth)
            }
        elif mode == "dense":
            ranking = {"dense": self._rank_dense(query, allowed, depth)}
        elif mode == "hybrid_raw":
            # Ablation: fuse the *raw* sparse index instead of the contextual
            # one. Kept because the measurement here is counter-intuitive and
            # worth being able to re-check — see the note below.
            ranking = {
                "bm25": self._rank_sparse(self.bm25_raw, sparse_query, allowed, depth),
                "dense": self._rank_dense(query, allowed, depth),
            }
        else:
            # Default for `hybrid` and `hybrid_rerank`.
            #
            # Claude's ingest-time context blurbs make BM25 *worse* on its own
            # (recall 69% -> 62%, MRR 0.65 -> 0.58): every blurb repeats the
            # party names, the court and the matter type, so each document
            # chunk starts matching every query. Fused with the dense signal
            # the same text is the best configuration measured (MRR 0.70,
            # precision 22%), because RRF works on ranks and the dense side
            # rewards the extra context rather than being diluted by it.
            ranking = {
                "bm25c": self._rank_sparse(self.bm25_contextual, sparse_query, allowed, depth),
                "dense": self._rank_dense(query, allowed, depth),
            }
        if "dense" in ranking and not ranking["dense"]:
            ranking.pop("dense")

        fused = self._fuse(ranking)
        ordered = sorted(fused.items(), key=lambda kv: -kv[1][0])
        hits = [
            Hit(chunk=self.chunks[index], score=score, signals=signals)
            for index, (score, signals) in ordered[
                : max(k, depth if mode == "hybrid_rerank" else k)
            ]
        ]
        for position, hit in enumerate(hits, start=1):
            hit.rank = position
        return hits[:k] if mode != "hybrid_rerank" else hits

    def all_chunks_for(
        self, *, event_ref: str | None = None, document: str | None = None
    ) -> list[Chunk]:
        return [
            c
            for c in self.chunks
            if (event_ref is None or c.event_ref == event_ref)
            and (document is None or c.document_name == document)
        ]


def dedupe_by_event(hits: Iterable[Hit], limit: int) -> list[Hit]:
    """Keep the best chunk per event/document so one long transcript cannot
    crowd out every other source."""
    seen: set[str] = set()
    out: list[Hit] = []
    for hit in hits:
        key = hit.chunk.event_ref or hit.chunk.document_name or hit.chunk.chunk_id
        if key in seen:
            continue
        seen.add(key)
        out.append(hit)
        if len(out) >= limit:
            break
    return out
