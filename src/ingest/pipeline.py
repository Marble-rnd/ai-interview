"""
Offline ingestion: raw case directory in, versioned index artifact out.

Nothing at query time reads a CSV or opens a PDF. That split is what makes
this shape work at scale — ingestion is O(case) and embarrassingly parallel,
the expensive model work (OCR, contextual blurbs) is paid once and amortized
over every future question, and serving a case is a file read.

The artifact is self-describing. `manifest.json` records the hash of every
source file, the models and prompt version used, and the counts, so a stale
or partially-built index is detectable rather than silently wrong, and
re-ingesting an unchanged case is a no-op.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from .. import config
from ..agent.llm import LLM, Usage
from ..case import embeddings
from ..case.brief import build_brief
from ..case.chunking import chunk_document, chunk_event
from ..case.documents import CatalogEntry, DocumentText, build_catalog, extract
from ..case.models import CaseBrief, Chunk, NormalizedEvent
from ..case.normalize import normalize_case
from ..case.roster import Roster, apply_roster, build_roster
from ..data_loader import load_timeline
from .enrich import contextualize_document, structural_blurb
from .ocr import file_hash, ocr_document

logger = logging.getLogger(__name__)

# Bumped when the enrichment prompt changes in a way that invalidates blurbs.
PROMPT_VERSION = "1"


class IngestResult(dict):
    """Manifest-shaped summary of one ingest run."""


def _source_hashes(case_dir: Path) -> dict[str, str]:
    hashes = {"case-timeline.csv": file_hash(case_dir / "case-timeline.csv")}
    docs = case_dir / "documents"
    if docs.exists():
        for path in sorted(docs.iterdir()):
            if path.is_file() and not path.name.startswith("."):
                hashes[f"documents/{path.name}"] = file_hash(path)
    return hashes


def _is_current(out_dir: Path, sources: dict[str, str], flags: dict[str, Any]) -> bool:
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.exists():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return (
        manifest.get("sources") == sources
        and manifest.get("index_version") == config.INDEX_VERSION
        and manifest.get("prompt_version") == PROMPT_VERSION
        and manifest.get("flags", {}).get("enriched") == flags["enriched"]
        and manifest.get("flags", {}).get("dense") == flags["dense"]
    )


def _read_documents(
    case_dir: Path, catalog: list[CatalogEntry], llm: LLM, cache_dir: Path
) -> tuple[dict[str, DocumentText], int]:
    """Extract text for every readable document, OCRing the image-only ones."""
    texts: dict[str, DocumentText] = {}
    ocr_pages = 0
    for entry in catalog:
        if not entry.readable or not entry.disk_name:
            continue
        path = case_dir / "documents" / entry.disk_name
        try:
            if entry.text_status == "scanned":
                text = ocr_document(path, llm, cache_dir / "ocr")
                ocr_pages += sum(1 for p in text.pages if p.text)
            else:
                text = extract(path)
        except Exception as exc:
            logger.warning("Could not read %s: %s", entry.disk_name, exc)
            continue
        texts[entry.disk_name] = text
    return texts, ocr_pages


def _build_chunks(
    events: list[NormalizedEvent],
    catalog: list[CatalogEntry],
    texts: dict[str, DocumentText],
    llm: LLM,
    cache_dir: Path,
    *,
    enrich: bool,
) -> tuple[list[Chunk], int]:
    chunks: list[Chunk] = [c for event in events for c in chunk_event(event)]

    blurbs_written = 0
    for entry in catalog:
        text = texts.get(entry.disk_name or "")
        if text is None:
            continue
        doc_chunks = chunk_document(entry, text)
        if not doc_chunks:
            continue
        if enrich:
            blurbs_written += contextualize_document(
                doc_chunks,
                "\n\n".join(p.text for p in text.pages),
                llm,
                cache_dir / "blurbs.json",
            )
        for chunk in doc_chunks:
            if not chunk.context_blurb:
                chunk.context_blurb = structural_blurb(chunk, entry.display_name, entry.category)
        chunks.extend(doc_chunks)
    return chunks, blurbs_written


def _write_jsonl(path: Path, rows: list[Any]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(row.model_dump_json() + "\n")


def ingest_case(
    case_slug: str,
    *,
    llm: LLM | None = None,
    force: bool = False,
    enrich: bool = True,
    dense: bool = True,
    now: datetime | None = None,
) -> IngestResult:
    """Build (or refresh) the index artifact for one case."""
    started = time.perf_counter()
    case_dir = config.case_dir(case_slug)
    out_dir = config.artifact_dir(case_slug)
    cache_dir = config.INDEX_DIR / case_slug / "cache"

    dense = dense and embeddings.available()
    flags = {"enriched": enrich, "dense": dense}
    sources = _source_hashes(case_dir)

    if not force and _is_current(out_dir, sources, flags):
        manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
        manifest["skipped"] = True
        return IngestResult(manifest)

    llm = llm or LLM(Usage())
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    rows = load_timeline(case_dir)
    events, report = normalize_case(rows)
    roster = build_roster(rows, events)
    apply_roster(events, roster)

    catalog = build_catalog(case_dir, events)
    texts, ocr_pages = _read_documents(case_dir, catalog, llm, cache_dir)
    chunks, blurbs = _build_chunks(events, catalog, texts, llm, cache_dir, enrich=enrich)

    brief = build_brief(case_slug, events, catalog, roster, rows, texts, now=now or config.now())

    # Dense vectors over the same text the sparse index sees, so the two
    # signals are comparable and the ablation is honest.
    vectors = embeddings.embed([c.indexed_text() for c in chunks]) if dense else None
    if vectors is not None:
        np.save(out_dir / "embeddings.npy", vectors)
    elif (out_dir / "embeddings.npy").exists():
        (out_dir / "embeddings.npy").unlink()

    _write_jsonl(out_dir / "events.jsonl", events)
    _write_jsonl(out_dir / "chunks.jsonl", chunks)
    (out_dir / "brief.json").write_text(brief.model_dump_json(indent=1), encoding="utf-8")
    (out_dir / "roster.json").write_text(roster.model_dump_json(indent=1), encoding="utf-8")
    (out_dir / "catalog.json").write_text(
        json.dumps([c.model_dump() for c in catalog], indent=1), encoding="utf-8"
    )

    manifest = IngestResult(
        {
            "case_slug": case_slug,
            "case_id": events[0].case_id if events else "",
            "index_version": config.INDEX_VERSION,
            "prompt_version": PROMPT_VERSION,
            "built_at": datetime.now(timezone.utc).isoformat(),
            "sources": sources,
            "flags": flags,
            "models": {
                "enrichment": config.ENRICHMENT_MODEL if enrich else None,
                "ocr": config.MODEL if ocr_pages else None,
                "embedding": embeddings.MODEL_NAME if vectors is not None else None,
            },
            "counts": {
                "events": len(events),
                "chunks": len(chunks),
                "event_chunks": sum(1 for c in chunks if c.source == "event"),
                "document_chunks": sum(1 for c in chunks if c.source == "document"),
                "documents_on_disk": sum(1 for c in catalog if c.disk_name),
                "documents_absent": sum(1 for c in catalog if not c.disk_name),
                "ocr_pages": ocr_pages,
                "blurbs_generated": blurbs,
                "embedding_dim": int(vectors.shape[1]) if vectors is not None else None,
            },
            "normalize_report": report,
            "ingest_usage": llm.usage.to_dict(),
            "build_seconds": round(time.perf_counter() - started, 1),
            "skipped": False,
        }
    )
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


def load_artifact(case_slug: str) -> dict[str, Any]:
    """Read a built artifact back. Raises if the case has not been ingested."""
    out_dir = config.artifact_dir(case_slug)
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"No index for '{case_slug}'. Run: uv run python -m src.ingest --case {case_slug}"
        )

    def read_jsonl(name: str, model: type) -> list[Any]:
        with open(out_dir / name, encoding="utf-8") as handle:
            return [model.model_validate_json(line) for line in handle if line.strip()]

    vectors = None
    if (out_dir / "embeddings.npy").exists():
        vectors = np.load(out_dir / "embeddings.npy")

    return {
        "manifest": json.loads(manifest_path.read_text(encoding="utf-8")),
        "events": read_jsonl("events.jsonl", NormalizedEvent),
        "chunks": read_jsonl("chunks.jsonl", Chunk),
        "brief": CaseBrief.model_validate_json(
            (out_dir / "brief.json").read_text(encoding="utf-8")
        ),
        "roster": Roster.model_validate_json((out_dir / "roster.json").read_text(encoding="utf-8")),
        "catalog": [
            CatalogEntry.model_validate(c)
            for c in json.loads((out_dir / "catalog.json").read_text(encoding="utf-8"))
        ],
        "embeddings": vectors,
    }
