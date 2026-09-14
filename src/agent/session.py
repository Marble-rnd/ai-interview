"""
A conversation with the agent about one case.

Holds the loaded artifact, the tool set bound to it, and the conversation.
Multi-turn works because each turn is replayed the prior turns as plain
text, so follow-ups like "and what about the response deadline?" resolve
against what was already said. The LangGraph checkpointer is scoped to a
single turn and exists for the validator retry cycle, not for memory.

Every turn writes a JSONL trace — stage timings, tool calls with their
arguments, tokens in/out/cached, cache hit rate, cost — which is what the
eval harness reads for its latency and spend numbers.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .. import config
from ..case.retrieval import Retriever
from ..ingest.pipeline import load_artifact
from .graph import build_graph
from .llm import LLM, Usage
from .prompts import build_system_prefix
from .state import Deps
from .tools import CaseTools


class Answer(BaseModel):
    question: str
    answer: str
    verdict: str = "answered"
    confidence: str = "medium"
    citations: list[dict[str, Any]] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    lint: list[str] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    stage_timings: dict[str, float] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    latency_ms: float = 0.0
    planned: bool = False
    attempts: int = 1
    run_id: str = ""

    @property
    def cited(self) -> list[dict[str, Any]]:
        return [c for c in self.citations if c.get("cited")]


_GRAPH = None


def _graph() -> Any:
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    return _GRAPH


class CaseSession:
    """One attorney, one case, many questions."""

    def __init__(
        self,
        case_slug: str,
        *,
        thread_id: str | None = None,
        rerank: bool = True,
        now: datetime | None = None,
        trace_dir: Path | None = None,
    ) -> None:
        self.case_slug = case_slug
        self.thread_id = thread_id or f"{case_slug}-{uuid.uuid4().hex[:8]}"
        self.now = now or config.now()
        self.rerank = rerank
        self.trace_dir = trace_dir if trace_dir is not None else config.RUNS_DIR

        artifact = load_artifact(case_slug)
        self.manifest = artifact["manifest"]
        self.events = artifact["events"]
        self.chunks = artifact["chunks"]
        self.brief = artifact["brief"]
        self.catalog = artifact["catalog"]
        self.roster = artifact["roster"]

        self.retriever = Retriever(self.chunks, artifact["embeddings"], self.events)
        self.system_prefix = build_system_prefix(self.brief, self.events, self.catalog, self.now)
        # Plain text turns only; provider blocks never cross a turn boundary.
        self.history: list[dict[str, str]] = []

    # -- asking -----------------------------------------------------------

    def ask(
        self,
        question: str,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> Answer:
        started = time.perf_counter()
        run_id = uuid.uuid4().hex[:12]

        usage = Usage()
        llm = LLM(usage)
        tools = CaseTools(
            case_slug=self.case_slug,
            brief=self.brief,
            events=self.events,
            chunks=self.chunks,
            catalog=self.catalog,
            retriever=self.retriever,
            llm=llm,
            rerank_enabled=self.rerank,
            now=self.now,
            on_event=on_event,
        )
        deps = Deps(
            case_slug=self.case_slug,
            tools=tools,
            llm=llm,
            brief=self.brief,
            events=self.events,
            catalog=self.catalog,
            now=self.now,
            system_prefix=self.system_prefix,
            on_event=on_event,
        )

        # One checkpoint thread per *turn*, not per conversation. The
        # checkpointer's job here is the within-turn validator retry cycle;
        # conversation memory is the explicit `history` list below. Sharing
        # one thread across turns made LangGraph carry per-turn channels
        # forward, so turn 2 inherited turn 1's evidence (contaminating its
        # citation pack) and replayed a `RetrievalHint` back as a plain dict.
        turn_thread = f"{self.thread_id}:{len(self.history) // 2}"
        state = _graph().invoke(
            {"question": question, "history": list(self.history)},
            config={
                "configurable": {"deps": deps, "thread_id": turn_thread},
                "recursion_limit": 30,
            },
        )

        answer = Answer(
            question=question,
            answer=state.get("answer", ""),
            verdict=state.get("verdict", "answered"),
            confidence=state.get("confidence", "medium"),
            citations=state.get("citations", []),
            gaps=state.get("gaps", []),
            lint=state.get("lint", []),
            tool_calls=list((state.get("tool_calls") or {}).values()),
            stage_timings=state.get("stage_timings", {}),
            usage=usage.to_dict(),
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
            planned=bool(state.get("planned")),
            attempts=int(state.get("attempt", 1)),
            run_id=run_id,
        )

        self.history.append({"role": "user", "content": question})
        self.history.append({"role": "assistant", "content": answer.answer})
        self._write_trace(answer)
        return answer

    # -- observability -----------------------------------------------------

    def _write_trace(self, answer: Answer) -> None:
        if self.trace_dir is None:
            return
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "run_id": answer.run_id,
            "thread_id": self.thread_id,
            "case_slug": self.case_slug,
            "at": datetime.now(timezone.utc).isoformat(),
            "now": self.now.isoformat(),
            "question": answer.question,
            "verdict": answer.verdict,
            "confidence": answer.confidence,
            "planned": answer.planned,
            "attempts": answer.attempts,
            "latency_ms": answer.latency_ms,
            "stage_timings": answer.stage_timings,
            "usage": answer.usage,
            "tool_calls": [
                {
                    "tool": call["tool"],
                    "arguments": call["arguments"],
                    "latency_ms": call["latency_ms"],
                    "result_chars": call["result_chars"],
                    "error": call["error"],
                }
                for call in answer.tool_calls
            ],
            "citations": [
                {
                    "ref": c["ref"],
                    "source": c["source"],
                    "document_name": c["document_name"],
                    "page_start": c["page_start"],
                    "cited": c["cited"],
                    "retrieved_by": c["retrieved_by"],
                }
                for c in answer.citations
            ],
            "gaps": answer.gaps,
            "lint": answer.lint,
            "retrieval": {
                "rerank": self.rerank,
                "dense": self.retriever.dense_available,
                "chunks": len(self.chunks),
            },
            "answer": answer.answer,
        }
        path = self.trace_dir / f"{self.case_slug}.jsonl"
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def reset(self) -> None:
        """Start a fresh conversation against the same case."""
        self.history.clear()
        self.thread_id = f"{self.case_slug}-{uuid.uuid4().hex[:8]}"
