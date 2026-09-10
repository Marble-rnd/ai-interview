"""
Graph state.

Two decisions here matter more than they look.

**Reducers merge by key, they do not append.** When the validator sends the
executor back for another attempt, the second attempt re-fetches chunks the
first already had. `operator.add` would duplicate them, inflating the
evidence pack shown to the user, the token bill, and the citation-precision
metric the eval reports. Keying evidence by chunk id and tool calls by
(name, arguments) makes retries idempotent — and gives duplicate-call
detection for the tool-efficiency metric for free.

**Provider messages never enter state.** The executor rebuilds its own
message list per attempt from structured inputs. Carrying raw message blocks
across a retry is how you end up sending a `tool_use` block with no matching
`tool_result` and getting a hard 400, and it collides with the append-only
constraints newer models place on replayed thinking blocks.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, Field

from ..case.documents import CatalogEntry
from ..case.models import CaseBrief, EvidenceItem, NormalizedEvent
from .llm import LLM
from .tools import CaseTools

Verdict = Literal["answered", "partial", "unknown", "refused"]
Confidence = Literal["high", "medium", "low"]


def merge_by_key(left: dict | None, right: dict | None) -> dict:
    """Right-biased dict merge. Idempotent across executor retries."""
    return {**(left or {}), **(right or {})}


# -- structured outputs from the LLM stages --------------------------------


class Plan(BaseModel):
    sub_questions: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    date_from: str | None = None
    date_to: str | None = None
    needs_documents: bool = False


class RetrievalHint(BaseModel):
    """A typed instruction for a retry.

    Typed rather than free prose: an unconstrained "try harder" hint makes
    the second attempt wander, and there is no way to detect that the hint
    repeated itself.
    """

    keywords: list[str] = Field(default_factory=list)
    activity_types: list[str] = Field(default_factory=list)
    documents: list[str] = Field(default_factory=list)
    date_from: str | None = None
    date_to: str | None = None

    def signature(self) -> str:
        return "|".join(
            [
                ",".join(sorted(self.keywords)),
                ",".join(sorted(self.activity_types)),
                ",".join(sorted(self.documents)),
                self.date_from or "",
                self.date_to or "",
            ]
        )

    def is_empty(self) -> bool:
        return not (self.keywords or self.activity_types or self.documents)


class Validation(BaseModel):
    """The validator's report.

    `lint_problems` and `unsupported_claims` are kept apart on purpose.
    Lint is exact — a citation either resolves to a real event or it does
    not — so it gates the retry and is safe to show an attorney. The model's
    `unsupported_claims` are a judgement call made against a truncated
    evidence pack, so they calibrate confidence but never force a retry and
    never reach the user as caveats. Mixing the two produced retries on
    correct, well-cited answers.
    """

    grounded: bool = True
    answers_question: bool = True
    unsupported_claims: list[str] = Field(default_factory=list)
    missing_retrieval: RetrievalHint = Field(default_factory=RetrievalHint)
    confidence: Confidence = "medium"
    notes: str | None = None

    # Populated by us, not by the model.
    lint_problems: list[str] = Field(default_factory=list)


@dataclass
class Deps:
    """Runtime objects, passed through the graph config rather than state.

    They are not serializable and must not be checkpointed; state holds only
    plain data so a conversation can be persisted and resumed.
    """

    case_slug: str
    tools: CaseTools
    llm: LLM
    brief: CaseBrief
    events: Sequence[NormalizedEvent]
    catalog: Sequence[CatalogEntry]
    now: datetime
    system_prefix: str
    max_attempts: int = 2
    trace: dict[str, Any] = field(default_factory=dict)
    # Optional progress channel. The UI subscribes to this so an attorney
    # sees "reading standing_family_law_order.pdf" rather than a spinner —
    # for a multi-step agent that is more informative than streaming tokens.
    on_event: Callable[[str, dict[str, Any]], None] | None = None

    def emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self.on_event is not None:
            # A broken subscriber (a closed SSE connection, say) must never
            # take down the run that is producing the answer.
            with contextlib.suppress(Exception):
                self.on_event(kind, payload)


class AgentState(TypedDict, total=False):
    # Inputs
    case_slug: str
    question: str
    history: list[dict[str, str]]

    # 1 guardrail
    refusal: str | None
    refusal_reason: str | None

    # 2 context
    brief_text: str

    # 3 planner
    plan: Plan | None
    planned: bool

    # 4 tool_fetcher
    prefetched: list[str]

    # 5 executor / 6 validator loop
    attempt: int
    draft: str
    hint: RetrievalHint | None
    hint_history: list[str]
    validation: Validation | None
    tool_calls: Annotated[dict[str, dict[str, Any]], merge_by_key]
    evidence: Annotated[dict[str, EvidenceItem], merge_by_key]

    # 7 evidence / 8 verdict / 9 output
    citations: list[dict[str, Any]]
    verdict: Verdict
    confidence: Confidence
    gaps: list[str]
    answer: str
    lint: list[str]

    # Observability
    stage_timings: Annotated[dict[str, float], merge_by_key]
