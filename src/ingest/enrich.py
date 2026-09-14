"""
Contextual Retrieval: situate each chunk before indexing it.

A chunk taken out of an 85-page tax return or a six-page affidavit loses the
context that makes it findable — "line 22b: 41,300" means nothing on its own.
Anthropic's Contextual Retrieval technique fixes this by having a model write
a one- or two-sentence blurb placing the chunk in its document, prepending it
to the chunk, and indexing the combination. It is the single largest accuracy
win available here, and it is affordable because it happens once, offline.

Two things keep the cost down:

* The document is sent once per document with a cache breakpoint on it, so
  every chunk after the first reads the context from cache at a tenth of the
  price.
* Blurbs are cached to disk by chunk hash, so re-ingest is free.

Chunks that already carry their own context — a normalized event, which
begins with an actor, a date and a headline — are skipped. They do not need a
model to explain what they are.
"""

from __future__ import annotations

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import config
from ..agent.llm import LLM, cached
from ..case.models import Chunk

logger = logging.getLogger(__name__)

MAX_WORKERS = 8

# Enough of the document for the model to place a chunk, without paying to
# cache a whole tax return on every document.
MAX_CONTEXT_CHARS = 60_000

_SYSTEM = (
    "You situate excerpts from legal case documents so they can be found by search. "
    "Reply with one or two plain sentences naming what the excerpt is, which document "
    "and section it comes from, and which parties, dates, amounts or form numbers it "
    "concerns. Use only what is in the document. No preamble, no quotes, no commentary."
)


def _blurb_key(document_name: str, text: str) -> str:
    digest = hashlib.sha256(f"{document_name}\x00{text}".encode()).hexdigest()
    return digest[:20]


def _load_cache(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def contextualize_document(
    chunks: list[Chunk],
    document_text: str,
    llm: LLM,
    cache_path: Path,
    *,
    model: str | None = None,
) -> int:
    """Attach a `context_blurb` to every chunk of one document, in place.

    Returns the number of blurbs generated (cache hits excluded).
    """
    if not chunks:
        return 0

    cache = _load_cache(cache_path)
    document_name = chunks[0].document_name or chunks[0].title
    context = document_text[:MAX_CONTEXT_CHARS]
    generated = 0
    failures: list[str] = []

    # The document is the stable prefix; the chunk is the volatile suffix.
    # Putting the breakpoint here is what makes per-chunk calls cheap.
    system = [cached(f'{_SYSTEM}\n\n<document name="{document_name}">\n{context}\n</document>')]

    def describe(chunk: Chunk) -> None:
        nonlocal generated
        key = _blurb_key(document_name, chunk.text)
        if (hit := cache.get(key)) is not None:
            chunk.context_blurb = hit
            return
        prompt = (
            "Situate this excerpt within the document above.\n\n"
            f"<excerpt>\n{chunk.text[:6000]}\n</excerpt>"
        )
        try:
            blurb = llm.text(
                system=system,
                messages=[{"role": "user", "content": prompt}],
                model=model or config.ENRICHMENT_MODEL,
                effort="low",
                max_tokens=200,
            )
        except Exception as exc:
            failures.append(f"{type(exc).__name__}: {exc}")
            return
        blurb = " ".join(blurb.split())
        if blurb:
            chunk.context_blurb = blurb
            cache[key] = blurb
            generated += 1

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        list(pool.map(describe, chunks))

    if failures:
        # Loud on purpose: a swallowed 400 here produces an index that looks
        # built but has no contextual signal at all.
        logger.error(
            "Contextualization failed for %d/%d chunks of %s. First error: %s",
            len(failures),
            len(chunks),
            document_name,
            failures[0],
        )

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=0, sort_keys=True), encoding="utf-8")
    return generated


def structural_blurb(chunk: Chunk, document_display_name: str, category: str | None) -> str:
    """Deterministic fallback when enrichment is disabled or unavailable.

    Weaker than a written blurb but free, and still far better than an
    unlabelled fragment: it names the document, its category and the page.
    """
    where = ""
    if chunk.page_start is not None:
        where = (
            f", page {chunk.page_start}"
            if chunk.page_end in (None, chunk.page_start)
            else f", pages {chunk.page_start}–{chunk.page_end}"
        )
    kind = f" ({category})" if category else ""
    return f"Excerpt from {document_display_name}{kind}{where}."
