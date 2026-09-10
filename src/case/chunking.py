"""
Split events and documents into retrievable chunks.

Three strategies, because the content has three very different shapes:

* Most events are a sentence or a short email — one chunk, never split, so a
  citation points at the whole event.
* Call transcripts run to 38,000 characters. Splitting them mid-sentence
  destroys the speaker context, so they break on speaker turns.
* Documents are paginated (one is 85 pages). They break on page boundaries
  with a little overlap, and every chunk carries its page range so a citation
  can open the actual page.
"""

from __future__ import annotations

import re

from .documents import CatalogEntry, DocumentText
from .models import Chunk, NormalizedEvent

# Rough chars-per-token for English prose. Used only for chunk sizing, so an
# approximation beats paying for a tokenizer call per chunk.
CHARS_PER_TOKEN = 4

EVENT_CHUNK_TOKENS = 1800
DOCUMENT_CHUNK_TOKENS = 600
DOCUMENT_OVERLAP_TOKENS = 90

# `Agent: ...` / `User: ...` / `EndUser: ...` line starts.
_SPEAKER_TURN = re.compile(r"^(?=[A-Z][A-Za-z ]{1,20}:\s)", re.M)


def _limit(tokens: int) -> int:
    return tokens * CHARS_PER_TOKEN


def _pack(segments: list[str], limit: int, joiner: str = "") -> list[str]:
    """Greedily pack segments into blocks no larger than `limit` characters.

    A single oversized segment is hard-split rather than dropped.
    """
    blocks: list[str] = []
    current = ""
    for segment in segments:
        if len(segment) > limit:
            if current:
                blocks.append(current)
                current = ""
            for start in range(0, len(segment), limit):
                blocks.append(segment[start : start + limit])
            continue
        candidate = f"{current}{joiner}{segment}" if current else segment
        if len(candidate) > limit and current:
            blocks.append(current)
            current = segment
        else:
            current = candidate
    if current:
        blocks.append(current)
    return blocks


def chunk_event(event: NormalizedEvent) -> list[Chunk]:
    """One chunk per event, unless the body is a long transcript."""
    facts = "\n".join(
        f"{key}: {value}" for key, value in event.facts.items() if value not in (None, "", [], {})
    )
    header = event.headline
    if event.doc_ref and event.doc_ref.category:
        header = f"{header} ({event.doc_ref.category})"

    body = event.body or ""
    base = f"{header}\n{facts}".strip()
    limit = _limit(EVENT_CHUNK_TOKENS)

    def make(text: str, part: int, total: int) -> Chunk:
        return Chunk(
            chunk_id=f"{event.case_id or 'case'}:{event.ref}:{part}",
            case_id=event.case_id,
            source="event",
            event_ref=event.ref,
            event_raw_id=event.raw_id,
            when=event.when,
            title=event.headline,
            text=text,
            part=part,
            part_count=total,
            low_fidelity=event.low_fidelity,
        )

    if len(base) + len(body) <= limit:
        combined = f"{base}\n\n{body}".strip() if body else base
        return [make(combined, 0, 1)]

    # Long body: split on speaker turns when it looks like a transcript,
    # otherwise on blank lines.
    segments = [s for s in _SPEAKER_TURN.split(body) if s.strip()]
    if len(segments) <= 1:
        segments = [s for s in body.split("\n\n") if s.strip()]
    blocks = _pack(segments, limit - len(base) - 4, joiner="")

    total = len(blocks)
    return [make(f"{base}\n\n{block}".strip(), index, total) for index, block in enumerate(blocks)]


def chunk_document(entry: CatalogEntry, text: DocumentText) -> list[Chunk]:
    """Page-aware chunks with overlap, each tagged with its page range.

    Overlap is carried from the tail of the previous chunk so a fact that
    straddles a page break is retrievable from either side.
    """
    limit = _limit(DOCUMENT_CHUNK_TOKENS)
    overlap = _limit(DOCUMENT_OVERLAP_TOKENS)
    name = entry.disk_name or entry.display_name

    chunks: list[Chunk] = []
    current = ""
    first_page: int | None = None
    last_page: int | None = None

    def flush() -> None:
        nonlocal current, first_page, last_page
        if not current.strip() or first_page is None:
            current, first_page, last_page = "", None, None
            return
        index = len(chunks)
        chunks.append(
            Chunk(
                chunk_id=f"{entry.display_name}:{name}:{index}",
                case_id="",
                source="document",
                document_name=name,
                page_start=first_page,
                page_end=last_page,
                title=entry.display_name,
                text=current.strip(),
                part=index,
                part_count=0,  # set once the total is known
            )
        )
        tail = current[-overlap:] if overlap else ""
        current = tail
        first_page = last_page

    for page in text.pages:
        page_text = page.text.strip()
        if not page_text:
            continue
        for paragraph in _pack([p for p in page_text.split("\n") if p.strip()], limit, joiner="\n"):
            if len(current) + len(paragraph) > limit and current.strip():
                flush()
            current = f"{current}\n{paragraph}" if current else paragraph
            first_page = page.page if first_page is None else first_page
            last_page = page.page
    flush()

    total = len(chunks)
    for chunk in chunks:
        chunk.part_count = total
        chunk.chunk_id = f"doc:{name}:{chunk.part}"
    return chunks
