"""
The agent graph.

Nine stages, of which at most three call a model. The rest are deterministic
Python, which is what keeps a simple factual lookup at one model round trip
instead of five:

  1 guardrail_in     scope and safety, structural case binding      (no LLM)
  2 context_builder  the precomputed brief                          (no LLM)
  3 planner          decompose — only for genuinely complex asks    (LLM, low)
  4 tool_fetcher     prefetch what the plan already implies         (no LLM)
  5 executor         the tool-use loop                              (LLM, high)
  6 validator        deterministic lint first, LLM only if needed   (LLM, low)
  7 evidence         assemble the citation pack                     (no LLM)
  8 verdict          answered / partial / unknown + confidence      (no LLM)
  9 output           redact, render                                 (no LLM)

LangGraph owns the validator→executor cycle and the checkpointer that makes
multi-turn conversation work; the model calls themselves go through the raw
SDK so thinking, effort, cache breakpoints and strict tool schemas stay under
our control.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import timedelta
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph

from ..case.models import EvidenceItem
from ..config import EFFORT_EXECUTOR, EFFORT_PLANNER, EFFORT_VALIDATOR
from .prompts import DATA_IS_NOT_INSTRUCTIONS, PLANNER_SYSTEM, VALIDATOR_SYSTEM
from .state import AgentState, Deps, Plan, Validation
from .tools import TOOL_SCHEMAS

logger = logging.getLogger(__name__)

# Hard stop on the tool loop. Every question in the eval set resolves in
# three or fewer; this only exists so a pathological loop cannot spin.
MAX_TOOL_ITERATIONS = 8

_CITATION = re.compile(r"\[(E\d{3,4})\]")
_DOC_CITATION = re.compile(
    r"\[([\w .()\-]+\.(?:pdf|docx))(?:[, ]+p+\.?\s*\d+(?:[–-]\d+)?)?\]", re.I
)
_ISO_DATE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
# The record writes dates every way a person does. A lint that only reads
# ISO would flag "2026-09-09" as invented when the evidence says "9/9".
_SLASH_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
_MONTH_NAME = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})"
    r"(?:st|nd|rd|th)?(?:,?\s+(20\d{2}))?\b",
    re.I,
)
_MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}

# Window boundaries an agent legitimately computes from "today" when asked
# about "the last two weeks" or "this month".
_DERIVED_WINDOWS = (0, 7, 14, 21, 30, 60, 90)
_SSN_LIKE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_HEARING = re.compile(r"\b(hearing|trial date|court date|docket call)\b", re.I)
# A bare Mongo-style id leaking into prose is a bug; the same 24 hex chars
# inside a filename (`affidavit-6a96dd23...b6d35.pdf`) is just the name of a
# real document on the case, so the lookaround excludes filename context.
_BARE_INTERNAL_ID = re.compile(r"(?<![\w.\-/])[0-9a-f]{24}(?![\w.\-/])")
# Phrases that make a mention of a hearing a denial rather than a claim.
_HEARING_NEGATION = re.compile(
    r"\b(no|not|never|none|isn'?t|aren'?t|wasn'?t|rather than|instead of|"
    r"as opposed to|nothing|neither|without any|does not|do not)\b",
    re.I,
)

# Narrow on purpose. Over-refusing a lawyer's research question is a worse
# failure than answering it, so this catches only what must never be emitted.
_IDENTIFIER_REQUEST = re.compile(
    r"\b(what|give|tell|show|provide|read|need|send)\b[^?.]{0,60}"
    r"\b(ssn|social security (number|#)|tax id|passport number|driver'?s licen[cs]e number)\b",
    re.I,
)
_REIDENTIFY = re.compile(
    r"\b(real (name|identity|person)|who (is|are) (they|this person) really|"
    r"de-?anonymi[sz]e|re-?identif)\w*\b",
    re.I,
)

# Complexity heuristic for whether planning is worth a round trip.
_PLAN_TRIGGERS = re.compile(
    r"\b(summari[sz]e|summary|compare|timeline|overview|walk me through|"
    r"everything|all of the|both|and also|as well as|what.*and what|"
    r"bring me up to speed|catch me up|status of|where (do|does) .{0,20}stand)\b",
    re.I,
)


def _deps(config: RunnableConfig) -> Deps:
    deps = (config or {}).get("configurable", {}).get("deps")
    if deps is None:
        raise RuntimeError("Agent graph invoked without Deps in config.configurable")
    return deps


def _timed(name: str, started: float) -> dict[str, float]:
    return {name: round((time.perf_counter() - started) * 1000, 1)}


# --------------------------------------------------------------------------
# 1. Guardrail (in)
# --------------------------------------------------------------------------


def guardrail_in(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Scope check.

    Cross-case isolation is not enforced here — it is structural. `CaseTools`
    is constructed bound to a single case artifact and no tool takes a case
    argument, so there is no request the model could make that reaches
    another matter.
    """
    started = time.perf_counter()
    deps = _deps(config)
    question = (state.get("question") or "").strip()

    refusal = None
    reason = None
    if not question:
        refusal, reason = "Ask me something about this case and I'll look it up.", "empty"
    elif _IDENTIFIER_REQUEST.search(question):
        refusal = (
            "I can't provide Social Security numbers or other government identifiers. "
            "I can tell you whether one is on file for this case, and where it was recorded."
        )
        reason = "identifier_request"
    elif _REIDENTIFY.search(question):
        refusal = (
            "The people in this case file are recorded under protected identities and "
            "I can't help connect them to anyone outside it. I can answer anything about "
            "what happened on the matter itself."
        )
        reason = "reidentification"

    return {
        "case_slug": deps.case_slug,
        "refusal": refusal,
        "refusal_reason": reason,
        "attempt": 0,
        "hint_history": [],
        "stage_timings": _timed("guardrail_in", started),
    }


def after_guardrail(state: AgentState) -> str:
    return "refused" if state.get("refusal") else "continue"


# --------------------------------------------------------------------------
# 2. Context builder
# --------------------------------------------------------------------------


def context_builder(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """The brief is computed at ingest, so this stage is a lookup.

    It lives in the cached system prefix rather than being re-sent per turn.
    """
    started = time.perf_counter()
    deps = _deps(config)
    deps.emit("stage", {"stage": "context_builder", "label": "Loading the case brief"})
    return {
        "brief_text": deps.system_prefix,
        "stage_timings": _timed("context_builder", started),
    }


# --------------------------------------------------------------------------
# 3. Planner
# --------------------------------------------------------------------------


def _needs_plan(question: str) -> bool:
    """Planning costs a round trip; spend it only when it can pay for itself.

    A single factual lookup ("when was the petition filed?") does not benefit
    from a plan — tool use *is* the plan — and paying an extra Opus turn on a
    10k-token prefix to produce a hint the executor would derive anyway is
    pure latency.
    """
    if _PLAN_TRIGGERS.search(question):
        return True
    if question.count("?") > 1:
        return True
    return len(question.split()) > 28


def planner(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    started = time.perf_counter()
    deps = _deps(config)
    deps.emit("stage", {"stage": "planner", "label": "Planning the lookup"})
    question = state["question"]

    if not _needs_plan(question):
        return {"plan": None, "planned": False, "stage_timings": _timed("planner", started)}

    plan = deps.llm.structured(
        Plan,
        system=PLANNER_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Today is {deps.now:%Y-%m-%d}.\n"
                    f"Available tools: {', '.join(t['name'] for t in TOOL_SCHEMAS)}\n\n"
                    f"Attorney's question: {question}"
                ),
            }
        ],
        effort=EFFORT_PLANNER,
        max_tokens=1200,
    )
    return {"plan": plan, "planned": True, "stage_timings": _timed("planner", started)}


# --------------------------------------------------------------------------
# 4. Tool fetcher
# --------------------------------------------------------------------------


def tool_fetcher(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Deterministic prefetch.

    This stage does NOT narrow the tool schema list. Prompt caching matches a
    prefix rendered as tools → system → messages, so varying the tool set per
    question would invalidate the ~10k-token cached prefix on every turn —
    far more expensive than the few hundred tokens of unused schema it saves.

    What it does instead is run the cheap deterministic lookups the plan
    already implies, so the executor starts with evidence in hand.
    """
    started = time.perf_counter()
    deps = _deps(config)
    deps.emit("stage", {"stage": "tool_fetcher", "label": "Prefetching what the plan implies"})
    plan = state.get("plan")
    prefetched: list[str] = []

    wanted = set(plan.tools) if plan else set()
    if plan and plan.date_from or plan and plan.date_to:
        wanted.add("get_events")

    if "get_contact_history" in wanted:
        result, _ = deps.tools.dispatch("get_contact_history", {})
        prefetched.append(f"get_contact_history:\n{result}")
    if "get_outstanding_items" in wanted:
        result, _ = deps.tools.dispatch("get_outstanding_items", {})
        prefetched.append(f"get_outstanding_items:\n{result}")
    if plan and (plan.date_from or plan.date_to):
        result, _ = deps.tools.dispatch(
            "get_events",
            {"date_from": plan.date_from, "date_to": plan.date_to, "limit": 60, "order": "asc"},
        )
        prefetched.append(f"get_events (window from the plan):\n{result}")

    return {
        "prefetched": prefetched,
        "tool_calls": {
            f"{c['tool']}:{sorted(c['arguments'].items())}": c for c in deps.tools.calls
        },
        "stage_timings": _timed("tool_fetcher", started),
    }


# --------------------------------------------------------------------------
# 5. Executor
# --------------------------------------------------------------------------


def _build_messages(state: AgentState, deps: Deps) -> list[dict[str, Any]]:
    """Rebuild the executor's messages from structured inputs.

    Provider message blocks are never carried in graph state: a retry that
    replays a `tool_use` block without its matching `tool_result` is a hard
    400, and replayed thinking blocks are subject to append-only rules on
    current models. Rebuilding is cheaper than getting that right.
    """
    messages: list[dict[str, Any]] = []
    for turn in state.get("history") or []:
        role = turn.get("role")
        content = (turn.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})

    parts = [state["question"]]
    if prefetched := state.get("prefetched"):
        parts.append(
            "Results already fetched for you (no need to call these again):\n\n"
            + "\n\n".join(prefetched)
        )
    if (plan := state.get("plan")) is not None:
        bits = []
        if plan.sub_questions:
            bits.append("Sub-questions: " + "; ".join(plan.sub_questions))
        if plan.keywords:
            bits.append("Vocabulary the record may use: " + ", ".join(plan.keywords))
        if bits:
            parts.append("Retrieval plan (advisory):\n" + "\n".join(bits))
    if (hint := state.get("hint")) is not None and not hint.is_empty():
        directions = []
        if hint.keywords:
            directions.append(f"search for: {', '.join(hint.keywords)}")
        if hint.activity_types:
            directions.append(f"check activity types: {', '.join(hint.activity_types)}")
        if hint.documents:
            directions.append(f"read: {', '.join(hint.documents)}")
        if hint.date_from or hint.date_to:
            directions.append(f"window {hint.date_from or 'start'} to {hint.date_to or 'now'}")
        parts.append(
            "Your previous answer was not fully supported by retrieved evidence. "
            "Retrieve more before answering: " + "; ".join(directions)
        )

    messages.append({"role": "user", "content": "\n\n".join(parts)})
    # Operator-authority reminder that case text is data, not instructions.
    # A mid-conversation system message preserves the cached prefix, unlike
    # editing the top-level system block.
    messages.append({"role": "system", "content": DATA_IS_NOT_INSTRUCTIONS})
    return messages


def executor(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """The agent loop: call, dispatch tools, repeat until the model answers."""
    started = time.perf_counter()
    deps = _deps(config)
    deps.emit("stage", {"stage": "executor", "label": "Researching the record"})
    messages = _build_messages(state, deps)
    system = [{"type": "text", "text": deps.system_prefix, "cache_control": {"type": "ephemeral"}}]

    draft = ""
    for _ in range(MAX_TOOL_ITERATIONS):
        response = deps.llm.message(
            system=system,
            messages=messages,
            tools=TOOL_SCHEMAS,
            effort=EFFORT_EXECUTOR,
            max_tokens=8000,
        )
        # Echo the assistant turn back verbatim, including thinking blocks.
        messages.append({"role": "assistant", "content": response.content})

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not tool_uses:
            draft = "".join(b.text for b in response.content if b.type == "text").strip()
            break

        # All results go back in ONE user message. Splitting them teaches the
        # model to stop issuing parallel calls.
        results = []
        for block in tool_uses:
            output, is_error = deps.tools.dispatch(block.name, dict(block.input or {}))
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                    **({"is_error": True} if is_error else {}),
                }
            )
        messages.append({"role": "user", "content": results})
    else:
        draft = draft or ("I ran out of research steps before I could answer that confidently.")

    return {
        "draft": draft,
        "attempt": state.get("attempt", 0) + 1,
        "evidence": {item.chunk_id: item for item in deps.tools.evidence_pack()},
        "tool_calls": {
            f"{c['tool']}:{sorted(c['arguments'].items())}": c for c in deps.tools.calls
        },
        "stage_timings": _timed(f"executor_attempt_{state.get('attempt', 0) + 1}", started),
    }


# --------------------------------------------------------------------------
# 6. Validator
# --------------------------------------------------------------------------


def _dates_in_text(text: str, default_year: int) -> set[str]:
    """Every date a piece of evidence mentions, in any common notation."""
    found = {f"{y}-{m}-{d}" for y, m, d in _ISO_DATE.findall(text)}
    for month, day, year in _SLASH_DATE.findall(text):
        m, d = int(month), int(day)
        if not (1 <= m <= 12 and 1 <= d <= 31):
            continue
        y = int(year) if year else default_year
        if y < 100:
            y += 2000
        found.add(f"{y:04d}-{m:02d}-{d:02d}")
    for name, day, year in _MONTH_NAME.findall(text):
        m = _MONTHS[name[:3].lower()]
        y = int(year) if year else default_year
        found.add(f"{y:04d}-{m:02d}-{int(day):02d}")
    return found


def _known_dates(deps: Deps, evidence: dict[str, EvidenceItem]) -> set[str]:
    """Every date the answer is allowed to assert.

    Anything else is either invented or arithmetic the agent did not show.
    """
    dates = {e.when.date().isoformat() for e in deps.events}
    dates |= {e.occurs_at.date().isoformat() for e in deps.events}
    for deadline in deps.brief.deadlines:
        if deadline.due:
            dates.add(deadline.due.date().isoformat())

    # "Summarize the last two weeks" requires naming the start of the window.
    # That is arithmetic on a known clock, not an invented date.
    for days in _DERIVED_WINDOWS:
        dates.add((deps.now - timedelta(days=days)).date().isoformat())

    year = deps.now.year
    for item in evidence.values():
        dates |= _dates_in_text(item.excerpt, year)
        dates |= _dates_in_text(item.label, year)
    return dates


def lint_draft(draft: str, deps: Deps, evidence: dict[str, EvidenceItem]) -> list[str]:
    """Deterministic checks that cannot themselves hallucinate.

    These run before any model-based validation because they are exact: a
    citation either resolves or it does not.
    """
    problems: list[str] = []

    valid_refs = {e.ref for e in deps.events}
    for ref in set(_CITATION.findall(draft)):
        if ref not in valid_refs:
            problems.append(f"cites {ref}, which is not an event on this case")

    readable_docs = {e.disk_name for e in deps.catalog if e.disk_name}
    absent_docs = {
        (e.timeline_file_name or e.display_name) for e in deps.catalog if not e.disk_name
    }
    for name in set(_DOC_CITATION.findall(draft)):
        if name in readable_docs:
            continue
        if name in absent_docs:
            problems.append(f"cites {name}, which is recorded but not present in this snapshot")
        elif not any(name.lower() == d.lower() for d in readable_docs):
            problems.append(f"cites document {name}, which is not on this case")

    known = _known_dates(deps, evidence)
    for year, month, day in set(_ISO_DATE.findall(draft)):
        stamp = f"{year}-{month}-{day}"
        if stamp not in known:
            problems.append(f"states the date {stamp}, which appears in no retrieved evidence")

    # The reference data explicitly warns that calling an internal meeting a
    # hearing falsely inflates urgency. Only *assertive* use is a problem —
    # "this is not a court hearing" is exactly the phrasing we want, so each
    # mention is checked for a negation in its immediate neighbourhood.
    for match in _HEARING.finditer(draft):
        window = draft[max(0, match.start() - 90) : match.end() + 40]
        if not _HEARING_NEGATION.search(window):
            problems.append(
                "asserts a hearing or court date; this record contains none, "
                "only internal Marble meetings"
            )
            break

    if _SSN_LIKE.search(draft):
        problems.append("contains something shaped like a Social Security number")

    if _BARE_INTERNAL_ID.search(draft):
        problems.append("contains a raw internal user or record id")

    return problems


def validator(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Lint first; ask a model only about what a regex cannot see."""
    started = time.perf_counter()
    deps = _deps(config)
    deps.emit("stage", {"stage": "validator", "label": "Checking the answer against the evidence"})
    draft = state.get("draft") or ""
    evidence = state.get("evidence") or {}

    problems = lint_draft(draft, deps, evidence)

    validation = Validation(
        grounded=not problems,
        answers_question=True,
        lint_problems=list(problems),
        confidence="high" if not problems else "low",
    )

    # The lint proves nothing about claims that carry no date or citation, so
    # a cheap model pass covers the rest — but only when there is a
    # substantive answer to check, and only while it can still change the
    # outcome. On the final attempt no retry is possible, so a ~10s call that
    # can only relabel confidence is not worth the attorney's wait.
    is_final_attempt = state.get("attempt", 1) >= 2
    if len(draft) > 200 and evidence and not is_final_attempt:
        # Evidence the draft actually cites goes first and keeps more of its
        # text: judging a citation against a 600-character stub is how a
        # correct, well-sourced answer gets marked unsupported.
        cited_refs = set(_CITATION.findall(draft))
        cited_docs = {n.lower() for n in _DOC_CITATION.findall(draft)}

        def _cited_first(item: EvidenceItem) -> tuple[int, Any]:
            is_cited = item.ref in cited_refs or (item.document_name or "").lower() in cited_docs
            return (0 if is_cited else 1, item.when or deps.now)

        pack = "\n\n".join(
            f"[{item.ref}] {item.label}\n{item.excerpt[:1400]}"
            for item in sorted(evidence.values(), key=_cited_first)[:40]
        )
        judged = deps.llm.structured(
            Validation,
            system=VALIDATOR_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"<question>{state['question']}</question>\n\n"
                        f"<draft_answer>\n{draft}\n</draft_answer>\n\n"
                        f"<retrieved_evidence>\n{pack}\n</retrieved_evidence>"
                    ),
                }
            ],
            effort=EFFORT_VALIDATOR,
            max_tokens=1500,
        )
        if judged is not None:
            validation = Validation(
                grounded=judged.grounded and not problems,
                answers_question=judged.answers_question,
                unsupported_claims=judged.unsupported_claims,
                missing_retrieval=judged.missing_retrieval,
                confidence=("low" if problems else judged.confidence),
                notes=judged.notes,
                lint_problems=list(problems),
            )

    if is_final_attempt and not problems:
        # Retrieved evidence plus a clean lint is the best signal available
        # without another model call.
        validation.confidence = "high" if len(evidence) >= 2 else "medium"

    hint = validation.missing_retrieval
    signature = hint.signature()
    history = list(state.get("hint_history") or [])
    repeated = signature in history
    if signature and not hint.is_empty():
        history.append(signature)

    return {
        "validation": validation,
        "hint": None if (hint.is_empty() or repeated) else hint,
        "hint_history": history,
        "stage_timings": _timed(f"validator_attempt_{state.get('attempt', 1)}", started),
    }


def after_validator(state: AgentState) -> str:
    """Retry only when a retry could plausibly help.

    Gated on the exact checks, not the model's opinion. The LLM validator
    sees a truncated evidence pack and will call a correct, well-cited claim
    unsupported; acting on that doubled latency on answers that were already
    right. It still lowers confidence — it just cannot send us round again.
    """
    validation = state.get("validation")
    if validation is None:
        return "accept"
    if state.get("attempt", 1) >= 2:  # one retry, never more
        return "accept"

    must_retry = bool(validation.lint_problems) or not validation.answers_question
    if not must_retry:
        return "accept"

    hint = state.get("hint")
    # Without a new, non-repeating instruction the second attempt just
    # rewrites the first. Accept and report the weakness instead.
    return "retry" if hint is not None and not hint.is_empty() else "accept"


# --------------------------------------------------------------------------
# 7. Evidence
# --------------------------------------------------------------------------


def evidence_stage(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Assemble the citation pack, ordered as the answer cites it."""
    started = time.perf_counter()
    deps = _deps(config)
    deps.emit("stage", {"stage": "evidence_stage", "label": "Assembling citations"})
    evidence = dict(state.get("evidence") or {})
    draft = state.get("draft") or ""

    cited_refs = set(_CITATION.findall(draft))
    cited_docs = {name.lower() for name in _DOC_CITATION.findall(draft)}

    # The timeline skeleton and the brief sit in the cached system prefix, so
    # the model can cite an event it never had to fetch — which is the point
    # of putting them there, but it means those citations arrive with no
    # evidence attached. Resolve them here, or the attorney sees a bare
    # reference and the eval under-counts citation recall.
    by_ref = {event.ref: event for event in deps.events}
    for ref in cited_refs:
        key = f"event:{ref}"
        if key in evidence or any(item.ref == ref for item in evidence.values()):
            continue
        event = by_ref.get(ref)
        if event is None:
            continue
        evidence[key] = EvidenceItem(
            chunk_id=key,
            ref=ref,
            label=f"{event.headline} — {event.when:%Y-%m-%d}",
            source="event",
            when=event.when,
            excerpt=(event.body or event.headline)[:1200],
            event_raw_id=event.raw_id,
            low_fidelity=event.low_fidelity,
            retrieved_by=["timeline_skeleton"],
        )

    by_doc = {e.disk_name.lower(): e for e in deps.catalog if e.disk_name}
    for name in cited_docs:
        entry = by_doc.get(name)
        if entry is None or any(
            (item.document_name or "").lower() == name for item in evidence.values()
        ):
            continue
        key = f"doc:{name}"
        evidence[key] = EvidenceItem(
            chunk_id=key,
            ref=entry.disk_name or name,
            label=entry.display_name,
            source="document",
            excerpt=entry.summary or f"{entry.display_name} ({entry.page_count or '?'} pages)",
            document_name=entry.disk_name,
            retrieved_by=["document_catalog"],
        )

    # Collapse duplicates so the attorney sees one source per thing, not one
    # per chunk. An event can arrive twice (as a whole event from get_events
    # and as a retrieved chunk from search_case), and a single document
    # routinely contributes five or six overlapping page ranges.
    best: dict[str, EvidenceItem] = {}
    pages: dict[str, set[int]] = {}
    for item in evidence.values():
        key = f"doc:{item.document_name}" if item.source == "document" else f"ev:{item.ref}"
        if item.page_start is not None:
            pages.setdefault(key, set()).add(item.page_start)
        current = best.get(key)
        if current is None:
            best[key] = item
            continue
        keep, drop = (
            (item, current) if len(item.excerpt) > len(current.excerpt) else (current, item)
        )
        keep.retrieved_by = sorted(set(keep.retrieved_by) | set(drop.retrieved_by))
        # Land the reader on the earliest cited page rather than an arbitrary one.
        if drop.page_start is not None and (
            keep.page_start is None or drop.page_start < keep.page_start
        ):
            keep.page_start, keep.page_end = drop.page_start, drop.page_end
        best[key] = keep

    citations: list[dict[str, Any]] = []
    for key, item in sorted(best.items(), key=lambda kv: (kv[1].when is None, kv[1].when)):
        seen_pages = sorted(pages.get(key, set()))
        was_cited = item.ref in cited_refs or (item.document_name or "").lower() in cited_docs
        citations.append(
            {
                "chunk_id": item.chunk_id,
                "ref": item.ref,
                "label": (
                    f"{item.document_name} · "
                    + ("pages " if len(seen_pages) > 1 else "page ")
                    + ", ".join(str(p) for p in seen_pages)
                    if item.source == "document" and seen_pages
                    else item.label
                ),
                "source": item.source,
                "when": item.when.isoformat() if item.when else None,
                "excerpt": item.excerpt,
                "document_name": item.document_name,
                "page_start": item.page_start,
                "page_end": item.page_end,
                "pages": seen_pages,
                "event_raw_id": item.event_raw_id,
                "low_fidelity": item.low_fidelity,
                "retrieved_by": item.retrieved_by,
                "cited": was_cited,
            }
        )
    citations.sort(key=lambda c: (not c["cited"], c["when"] or ""))
    return {"citations": citations, "stage_timings": _timed("evidence", started)}


# --------------------------------------------------------------------------
# 8. Verdict
# --------------------------------------------------------------------------


def verdict_stage(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    started = time.perf_counter()
    validation = state.get("validation")
    draft = state.get("draft") or ""
    cited = [c for c in (state.get("citations") or []) if c["cited"]]

    # Only the exact checks become user-facing caveats. The model's
    # groundedness opinion shapes confidence, but showing it verbatim
    # surfaced correct, cited statements as if they were problems.
    gaps: list[str] = list(validation.lint_problems) if validation else []

    says_unknown = bool(
        re.search(
            r"\b(no|nothing|not)\b[^.]{0,50}\b(record|on file|in the file|shows?)\b", draft, re.I
        )
    )

    unverified = bool(validation and validation.unsupported_claims)

    if validation and not validation.answers_question:
        verdict, confidence = "partial", "low"
    elif says_unknown and not cited:
        # Reporting an absence *with* evidence is a real answer and falls
        # through to "answered"; only an unevidenced "I can't tell" is
        # genuinely unknown.
        verdict, confidence = "unknown", (validation.confidence if validation else "medium")
    elif gaps:
        verdict, confidence = "partial", "low"
    else:
        verdict = "answered"
        confidence = validation.confidence if validation else "medium"
        if unverified and confidence == "high":
            confidence = "medium"
        if not cited and len(draft) > 200:
            confidence = "medium"

    return {
        "verdict": verdict,
        "confidence": confidence,
        "gaps": gaps,
        "stage_timings": _timed("verdict", started),
    }


# --------------------------------------------------------------------------
# 9. Output
# --------------------------------------------------------------------------


def output_stage(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Final redaction and rendering.

    PII is already stripped at ingest, before anything reaches the index or
    the model. This is defence in depth, not the control.
    """
    started = time.perf_counter()
    answer = state.get("draft") or ""
    lint: list[str] = []

    if _SSN_LIKE.search(answer):
        answer = _SSN_LIKE.sub("[REDACTED]", answer)
        lint.append("redacted an identifier-shaped number from the answer")
    if match := _BARE_INTERNAL_ID.search(answer):
        answer = _BARE_INTERNAL_ID.sub("[internal id]", answer)
        lint.append(f"masked a raw internal id ({match.group(0)[:6]}…)")

    return {"answer": answer.strip(), "lint": lint, "stage_timings": _timed("output", started)}


def refusal_stage(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    return {
        "answer": state.get("refusal") or "",
        "verdict": "refused",
        "confidence": "high",
        "citations": [],
        "gaps": [],
        "lint": [],
    }


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


# State carries our own Pydantic models (evidence items, the validation
# report). The checkpointer refuses to deserialize unregistered types in
# newer LangGraph, so the modules they live in are declared rather than left
# to a deprecation warning.
_SERDE = JsonPlusSerializer(allowed_msgpack_modules={"src.case.models", "src.agent.state"})


def build_graph(checkpointer: Any | None = None) -> Any:
    """Compile the graph. One instance is reused across conversations."""
    graph = StateGraph(AgentState)

    graph.add_node("guardrail_in", guardrail_in)
    graph.add_node("context_builder", context_builder)
    graph.add_node("planner", planner)
    graph.add_node("tool_fetcher", tool_fetcher)
    graph.add_node("executor", executor)
    graph.add_node("validator", validator)
    graph.add_node("evidence", evidence_stage)
    graph.add_node("verdict", verdict_stage)
    graph.add_node("output", output_stage)
    graph.add_node("refused", refusal_stage)

    graph.add_edge(START, "guardrail_in")
    graph.add_conditional_edges(
        "guardrail_in", after_guardrail, {"continue": "context_builder", "refused": "refused"}
    )
    graph.add_edge("refused", END)
    graph.add_edge("context_builder", "planner")
    graph.add_edge("planner", "tool_fetcher")
    graph.add_edge("tool_fetcher", "executor")
    graph.add_edge("executor", "validator")
    graph.add_conditional_edges(
        "validator", after_validator, {"retry": "executor", "accept": "evidence"}
    )
    graph.add_edge("evidence", "verdict")
    graph.add_edge("verdict", "output")
    graph.add_edge("output", END)

    return graph.compile(checkpointer=checkpointer or InMemorySaver(serde=_SERDE))
