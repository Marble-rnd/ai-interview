"""
Vision OCR for image-only PDFs.

Six documents in case_001 have no text layer at all — the issued summons, the
return of service, the standing family law order. Without OCR they are
invisible to retrieval, and they hold facts that appear nowhere else: the
docket number `2486DR7193` and the twenty-day response window are only
printed on the scanned summons.

OCR runs once, at ingest, keyed by file content hash, so re-ingesting a case
costs nothing and two byte-identical packets are read a single time.
"""

from __future__ import annotations

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..agent.llm import LLM
from ..case.documents import DocumentText, PageText, render_page

logger = logging.getLogger(__name__)

PROMPT = (
    "Transcribe every piece of text on this page of a legal document, verbatim, "
    "in reading order. Preserve headings, party names, case numbers, dates, "
    "addresses and form field labels exactly as printed. Do not summarize, "
    "correct, or comment. If the page is blank, reply exactly: [BLANK PAGE]"
)

# Rendering above ~2x buys no accuracy on these filings and costs tokens.
RENDER_SCALE = 2.0
MAX_WORKERS = 6


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()[:16]


def ocr_document(
    path: Path,
    llm: LLM,
    cache_dir: Path,
    *,
    max_pages: int | None = None,
) -> DocumentText:
    """OCR a scanned PDF, caching the result by file content hash."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{file_hash(path)}.json"

    if cache_file.exists():
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
        return DocumentText(
            name=path.name,
            kind="pdf",
            pages=[PageText(**p) for p in payload["pages"]],
            is_scanned=True,
            ocr_applied=True,
        )

    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    try:
        total = len(pdf)
    finally:
        pdf.close()
    page_numbers = list(range(1, (min(total, max_pages) if max_pages else total) + 1))

    def read(number: int) -> PageText:
        try:
            image = render_page(path, number, scale=RENDER_SCALE)
            text = llm.read_images([image], PROMPT)
        except Exception as exc:
            logger.warning("OCR failed for %s p%s: %s", path.name, number, exc)
            return PageText(page=number, text="")
        return PageText(page=number, text="" if text.strip() == "[BLANK PAGE]" else text.strip())

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        pages = sorted(pool.map(read, page_numbers), key=lambda p: p.page)

    cache_file.write_text(
        json.dumps({"source": path.name, "pages": [p.model_dump() for p in pages]}, indent=1),
        encoding="utf-8",
    )
    return DocumentText(name=path.name, kind="pdf", pages=pages, is_scanned=True, ocr_applied=True)
