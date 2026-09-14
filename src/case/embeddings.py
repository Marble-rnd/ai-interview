"""
Dense vectors.

Anthropic ships no embeddings endpoint (its recommended partner is Voyage),
and this project has neither a Voyage nor an OpenAI key. Claude does the work
that actually moves retrieval accuracy — contextual blurbs at ingest and
listwise reranking at query time — but it cannot emit a vector, so the dense
signal comes from a small local ONNX model instead.

`bge-small-en-v1.5` is 384-dimensional and about 45 MB, with no torch
dependency. If it cannot be fetched (an offline reviewer, a locked-down CI
box), retrieval degrades to contextual BM25 + Claude reranking rather than
failing — and the eval reports both configurations, so the dependency has to
earn its place.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import numpy as np

logger = logging.getLogger(__name__)

MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIMENSIONS = 384

# bge models are trained with an asymmetric prefix on the query side only.
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

_MODEL = None
_UNAVAILABLE = False


def available() -> bool:
    """Whether dense retrieval can run in this environment."""
    return _load() is not None


def _load():
    global _MODEL, _UNAVAILABLE
    if _MODEL is not None or _UNAVAILABLE:
        return _MODEL
    try:
        from fastembed import TextEmbedding

        _MODEL = TextEmbedding(model_name=MODEL_NAME)
    except Exception as exc:  # missing package, no network, no disk cache
        logger.warning(
            "Dense embeddings unavailable (%s: %s). Falling back to sparse retrieval.",
            type(exc).__name__,
            exc,
        )
        _UNAVAILABLE = True
        return None
    return _MODEL


def embed(texts: Sequence[str], *, is_query: bool = False) -> np.ndarray | None:
    """Embed texts as L2-normalized float32 rows, or None if unavailable.

    Normalizing here means cosine similarity is a plain dot product, so the
    store stays a numpy matrix with no index structure to maintain.
    """
    model = _load()
    if model is None or not texts:
        return None
    prepared = [f"{QUERY_PREFIX}{t}" for t in texts] if is_query else list(texts)
    vectors = np.asarray(list(model.embed(prepared)), dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.maximum(norms, 1e-12)
