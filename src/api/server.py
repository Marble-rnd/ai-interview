"""
HTTP API behind the chat UI.

`uv run uvicorn src.api.server:app --reload`

Answers stream as server-sent events. What streams is *progress*, not tokens:
a multi-step agent that says "searching the timeline", "reading
standing_family_law_order.pdf, page 4" tells an attorney more about what it
is doing than a token-by-token reveal of the final sentence would, and it
matches the actual shape of the work.

The evidence endpoints exist so a citation is checkable rather than
decorative — every claim opens to the timeline record or the rendered page
of the document it came from.
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import config
from ..agent.session import CaseSession
from ..case.documents import render_page
from ..data_loader import list_cases
from ..ingest.pipeline import load_artifact

STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Case Research Agent", version="1.0")

# One session per conversation thread. A real deployment would put this
# behind a store with eviction; for a single-attorney tool an in-process map
# is honest about what it is.
_SESSIONS: dict[str, CaseSession] = {}
_LOCK = threading.Lock()


class AskRequest(BaseModel):
    case: str
    question: str
    thread_id: str | None = None


def _session(case_slug: str, thread_id: str) -> CaseSession:
    with _LOCK:
        session = _SESSIONS.get(thread_id)
        if session is None or session.case_slug != case_slug:
            session = CaseSession(case_slug, thread_id=thread_id)
            _SESSIONS[thread_id] = session
        return session


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


# --------------------------------------------------------------------------
# Cases
# --------------------------------------------------------------------------


@app.get("/api/cases")
def get_cases() -> list[dict[str, Any]]:
    out = []
    for slug in list_cases(config.CASE_DATA_DIR):
        try:
            artifact = load_artifact(slug)
        except FileNotFoundError:
            out.append({"slug": slug, "indexed": False})
            continue
        brief = artifact["brief"]
        out.append(
            {
                "slug": slug,
                "indexed": True,
                "client": brief.client_name,
                "opposing_party": brief.opposing_party,
                "matter": brief.matter_type,
                "jurisdiction": brief.jurisdiction,
                "events": brief.event_count,
                "documents": brief.document_count,
                "last_activity": brief.last_activity.isoformat() if brief.last_activity else None,
            }
        )
    return out


@app.get("/api/cases/{slug}/brief")
def get_brief(slug: str) -> dict[str, Any]:
    try:
        artifact = load_artifact(slug)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    brief = artifact["brief"].model_dump(mode="json")
    brief["documents"] = [
        {
            "disk_name": entry.disk_name,
            "display_name": entry.display_name,
            "category": entry.category,
            "pages": entry.page_count,
            "text_status": entry.text_status,
        }
        for entry in artifact["catalog"]
    ]
    brief["manifest"] = artifact["manifest"]["counts"]
    return brief


# --------------------------------------------------------------------------
# Asking
# --------------------------------------------------------------------------


def _run_and_stream(session: CaseSession, question: str) -> Iterator[str]:
    """Drive the blocking agent on a worker thread, relaying its progress."""
    events: queue.Queue[tuple[str, Any] | None] = queue.Queue()
    result: dict[str, Any] = {}

    def on_event(kind: str, payload: dict[str, Any]) -> None:
        events.put((kind, payload))

    def work() -> None:
        try:
            answer = session.ask(question, on_event=on_event)
            result["answer"] = answer
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            events.put(None)

    worker = threading.Thread(target=work, daemon=True)
    worker.start()

    while True:
        item = events.get()
        if item is None:
            break
        kind, payload = item
        yield _sse(kind, payload)

    worker.join()
    if error := result.get("error"):
        yield _sse("error", {"message": error})
        return

    answer = result["answer"]
    yield _sse(
        "answer",
        {
            "run_id": answer.run_id,
            "thread_id": session.thread_id,
            "answer": answer.answer,
            "verdict": answer.verdict,
            "confidence": answer.confidence,
            "citations": answer.citations,
            "gaps": answer.gaps,
            "lint": answer.lint,
            "tool_calls": answer.tool_calls,
            "stage_timings": answer.stage_timings,
            "usage": answer.usage,
            "latency_ms": answer.latency_ms,
            "planned": answer.planned,
            "attempts": answer.attempts,
        },
    )


@app.post("/api/ask")
async def ask(request: AskRequest) -> StreamingResponse:
    thread_id = request.thread_id or f"{request.case}-{uuid.uuid4().hex[:8]}"
    try:
        session = _session(request.case, thread_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc

    async def stream() -> Any:
        loop = asyncio.get_running_loop()
        generator = _run_and_stream(session, request.question)
        while True:
            # The agent is synchronous; stepping it in a worker keeps the
            # event loop free to flush each chunk as it arrives.
            chunk = await loop.run_in_executor(None, lambda: next(generator, None))
            if chunk is None:
                break
            yield chunk

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/reset")
def reset(request: AskRequest) -> dict[str, str]:
    with _LOCK:
        session = _SESSIONS.pop(request.thread_id or "", None)
    if session:
        session.reset()
    return {"status": "ok"}


# --------------------------------------------------------------------------
# Evidence — what makes a citation checkable
# --------------------------------------------------------------------------


@app.get("/api/cases/{slug}/events/{ref}")
def get_event(slug: str, ref: str) -> dict[str, Any]:
    try:
        artifact = load_artifact(slug)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    event = next((e for e in artifact["events"] if e.ref == ref.upper()), None)
    if event is None:
        raise HTTPException(404, f"no event {ref}")
    payload = event.model_dump(mode="json")
    payload["duration_seconds"] = event.duration_seconds
    return payload


@app.get("/api/cases/{slug}/documents/{name}/page/{page}.png")
def get_document_page(slug: str, name: str, page: int) -> Response:
    """Render the actual page a citation points at.

    A citation an attorney can click through to the page image is proof; a
    citation they have to take on trust is decoration.
    """
    path = config.case_dir(slug) / "documents" / name
    if not path.is_file() or path.suffix.lower() != ".pdf":
        raise HTTPException(404, f"no renderable PDF named {name}")
    try:
        png = render_page(path, page, scale=1.6)
    except IndexError as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=3600"})


@app.get("/api/cases/{slug}/documents/{name}/text")
def get_document_text(slug: str, name: str, page: int | None = None) -> dict[str, Any]:
    """Extracted text for a document, including OCR'd scans."""
    try:
        artifact = load_artifact(slug)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    chunks = [c for c in artifact["chunks"] if c.document_name == name]
    if not chunks:
        raise HTTPException(404, f"no indexed text for {name}")
    if page is not None:
        chunks = [
            c
            for c in chunks
            if c.page_start is not None and c.page_start <= page <= (c.page_end or c.page_start)
        ]
    entry = next((e for e in artifact["catalog"] if e.disk_name == name), None)
    return {
        "name": name,
        "pages": entry.page_count if entry else None,
        "text_status": entry.text_status if entry else "unknown",
        "passages": [
            {"page_start": c.page_start, "page_end": c.page_end, "text": c.text} for c in chunks
        ],
    }


# --------------------------------------------------------------------------
# Static
# --------------------------------------------------------------------------

if STATIC.exists():
    app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")
