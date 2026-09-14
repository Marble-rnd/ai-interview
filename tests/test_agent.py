"""
Tests for the agent layer: guardrails, the deterministic lint, tool schemas,
retrieval filters, and the state reducers.

None of these call a model. Everything a model call would tell us here is
better checked exactly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.agent.graph import (
    _BARE_INTERNAL_ID,
    _dates_in_text,
    _known_dates,
    _needs_plan,
    guardrail_in,
    lint_draft,
)
from src.agent.state import RetrievalHint, merge_by_key
from src.agent.tools import TOOL_SCHEMAS
from src.case.aliases import expand, expanded_query, normalize_query
from src.case.chunking import chunk_event
from src.case.models import EvidenceItem
from src.case.retrieval import Filters, Retriever, tokenize

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


def _deps(events=(), catalog=(), deadlines=()):
    return SimpleNamespace(
        case_slug="case_001",
        events=list(events),
        catalog=list(catalog),
        brief=SimpleNamespace(deadlines=list(deadlines)),
        now=NOW,
    )


def _config(deps):
    return {"configurable": {"deps": deps}}


# --------------------------------------------------------------------------
# Guardrail
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "What is the client's SSN?",
        "Give me his social security number",
        "Tell me the client's passport number",
        "What's her driver's license number?",
    ],
)
def test_identifier_requests_are_refused(question):
    out = guardrail_in({"question": question}, _config(_deps()))
    assert out["refusal"]
    assert out["refusal_reason"] == "identifier_request"


@pytest.mark.parametrize(
    "question",
    [
        # Over-refusing an attorney's research question is the worse failure,
        # so the guardrail has to stay narrow.
        "Did the client file a notice of social security number?",
        "Has the driver's license been uploaded?",
        "What documents are still outstanding?",
        "Summarize the last two weeks",
        "Who is the opposing party?",
    ],
)
def test_ordinary_research_questions_are_not_refused(question):
    out = guardrail_in({"question": question}, _config(_deps()))
    assert out["refusal"] is None


def test_reidentification_is_refused():
    out = guardrail_in(
        {"question": "What is this person's real identity outside the case?"}, _config(_deps())
    )
    assert out["refusal_reason"] == "reidentification"


# --------------------------------------------------------------------------
# The deterministic lint
# --------------------------------------------------------------------------


def _event(ref, when, headline="Something", raw_id="x"):
    return SimpleNamespace(
        ref=ref,
        when=when,
        occurs_at=when,
        headline=headline,
        raw_id=raw_id,
        body=None,
        low_fidelity=False,
    )


def test_lint_rejects_a_citation_to_a_nonexistent_event():
    deps = _deps(events=[_event("E001", NOW)])
    problems = lint_draft("The petition was filed [E999].", deps, {})
    assert any("E999" in p for p in problems)


def test_lint_accepts_a_citation_to_a_real_event():
    deps = _deps(events=[_event("E001", NOW)])
    assert lint_draft("It happened [E001].", deps, {}) == []


def test_lint_rejects_an_invented_date():
    deps = _deps(events=[_event("E001", NOW)])
    problems = lint_draft("It was filed on 2024-01-15.", deps, {})
    assert any("2024-01-15" in p for p in problems)


def test_lint_allows_a_date_present_in_retrieved_evidence():
    deps = _deps(events=[_event("E001", NOW)])
    evidence = {
        "c1": EvidenceItem(
            chunk_id="c1",
            ref="E001",
            label="Email",
            source="event",
            excerpt="E-Filed 2026-08-31 at 5:42 PM.",
        )
    }
    assert lint_draft("It was e-filed 2026-08-31.", deps, evidence) == []


def test_lint_allows_a_date_the_evidence_writes_in_another_notation():
    """The record says "9/9"; the answer says 2026-09-09. Same date."""
    deps = _deps(events=[_event("E001", NOW)])
    evidence = {
        "c1": EvidenceItem(
            chunk_id="c1",
            ref="E001",
            label="SMS",
            source="event",
            excerpt="confirm the appointment for Wednesday 9/9 at 11 AM",
        )
    }
    assert lint_draft("He asked to move it to 2026-09-09.", deps, evidence) == []


def test_lint_allows_a_computed_window_boundary():
    """ "The last two weeks" requires naming a date the record never states."""
    deps = _deps(events=[_event("E001", NOW)])
    start = (NOW - timedelta(days=14)).date().isoformat()
    assert lint_draft(f"Between {start} and today, the petition was filed.", deps, {}) == []


def test_dates_in_text_reads_the_notations_this_record_uses():
    found = _dates_in_text("filed 08/31/2026, served Sept 4, reply due 9/24", 2026)
    assert {"2026-08-31", "2026-09-04", "2026-09-24"} <= found


def test_known_dates_includes_computed_deadlines():
    due = datetime(2026, 9, 24, tzinfo=timezone.utc)
    deps = _deps(events=[_event("E001", NOW)], deadlines=[SimpleNamespace(due=due)])
    assert "2026-09-24" in _known_dates(deps, {})


def test_lint_flags_an_asserted_hearing_but_not_a_denial():
    deps = _deps(events=[_event("E001", NOW)])
    assert any("hearing" in p for p in lint_draft("A hearing is set for next month.", deps, {}))
    for denial in [
        "No court hearing appears anywhere in this record.",
        "The kickoff call is an internal meeting, not a court hearing.",
        "Nothing is scheduled; there is no hearing on file.",
    ]:
        assert not any("hearing" in p for p in lint_draft(denial, deps, {})), denial


def test_lint_flags_identifier_shaped_numbers():
    deps = _deps(events=[_event("E001", NOW)])
    assert any("Social Security" in p for p in lint_draft("It is 123-45-6789.", deps, {}))


def test_bare_internal_id_lint_ignores_filenames():
    """A 24-hex id inside a real filename is not a leaked identifier."""
    assert not _BARE_INTERNAL_ID.search("affidavit-6a96dd2324ccd082e3ab6d35.pdf")
    assert _BARE_INTERNAL_ID.search("owner 6a96dd2324ccd082e3ab6d35 handled it")


def test_lint_rejects_citing_a_document_absent_from_the_snapshot():
    deps = _deps(
        events=[_event("E001", NOW)],
        catalog=[
            SimpleNamespace(
                disk_name=None, display_name="Pay stub", timeline_file_name="paystub_09_05.pdf"
            )
        ],
    )
    problems = lint_draft("His income is on [paystub_09_05.pdf p.1].", deps, {})
    assert any("not present in this snapshot" in p for p in problems)


# --------------------------------------------------------------------------
# Planner heuristic
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "Summarize the key events in the last two weeks.",
        "Where does this case stand?",
        "What did we file and what is still outstanding?",
    ],
)
def test_complex_questions_get_a_plan(question):
    assert _needs_plan(question)


@pytest.mark.parametrize(
    "question",
    ["When was the petition filed?", "Who is the opposing party?", "Has the client paid?"],
)
def test_simple_lookups_skip_the_planner(question):
    """Tool use is the plan; an extra round trip here is pure latency."""
    assert not _needs_plan(question)


# --------------------------------------------------------------------------
# Tool schemas
# --------------------------------------------------------------------------


def test_tool_schemas_are_well_formed():
    names = set()
    for tool in TOOL_SCHEMAS:
        assert tool["name"] not in names, f"duplicate tool {tool['name']}"
        names.add(tool["name"])
        assert len(tool["description"]) > 60, f"{tool['name']} needs a real description"
        schema = tool["input_schema"]
        assert schema["type"] == "object"
        for required in schema["required"]:
            assert required in schema["properties"], f"{tool['name']}.{required}"


def test_tool_schemas_stay_under_the_union_parameter_limit():
    """The API rejects a tool set with more than 16 union-typed parameters,
    which is what `strict: true` forces optional filters to become."""
    unions = sum(
        1
        for tool in TOOL_SCHEMAS
        for spec in tool["input_schema"]["properties"].values()
        if isinstance(spec.get("type"), list)
    )
    assert unions <= 16


def test_no_tool_takes_a_case_argument():
    """Case isolation is structural: there must be no argument by which the
    model could reach a different matter."""
    for tool in TOOL_SCHEMAS:
        for name in tool["input_schema"]["properties"]:
            assert "case" not in name.lower(), f"{tool['name']}.{name}"


# --------------------------------------------------------------------------
# Retrieval plumbing
# --------------------------------------------------------------------------


def test_tokenizer_keeps_form_codes_intact():
    """Splitting FL-150 into "fl" and "150" makes every form look alike."""
    tokens = tokenize("draft of FL-150 and fl 140")
    assert "fl-150" in tokens
    assert "fl-140" in tokens


def test_alias_expansion_bridges_the_paraphrase_gap():
    expanded = expanded_query("preliminary declaration of disclosure")
    assert "fl-140" in expanded
    assert "fl-150" in expand("income and expense declaration")


def test_normalize_query_folds_form_code_spellings():
    for spelling in ["FL 150", "fl-150", "FL_150", "fl150"]:
        assert "fl-150" in normalize_query(f"the {spelling} form")


def test_filters_run_before_any_scoring(loaded):
    events = loaded["case_001"]["events"]
    chunks = [c for e in events for c in chunk_event(e)]
    retriever = Retriever(chunks, None, events)

    filters = Filters(directions=["inbound"], exclude_automated=True)

    # The predicate is applied to the corpus before anything is scored, so
    # the candidate pool itself is already constrained.
    allowed = retriever._allowed(filters)
    by_ref = {e.ref: e for e in events}
    assert allowed
    assert all(by_ref[chunks[i].event_ref].direction == "inbound" for i in allowed)

    hits = retriever.search("documents", k=50, mode="bm25", filters=filters)
    assert hits
    for hit in hits:
        event = by_ref[hit.chunk.event_ref]
        assert event.direction == "inbound"
        assert not event.is_automated


def test_date_filter_is_exact(loaded):
    events = loaded["case_001"]["events"]
    chunks = [c for e in events for c in chunk_event(e)]
    retriever = Retriever(chunks, None, events)
    cutoff = datetime(2026, 9, 1, tzinfo=timezone.utc)
    hits = retriever.search("case", k=50, mode="bm25", filters=Filters(date_from=cutoff))
    assert hits
    assert all(h.chunk.when >= cutoff for h in hits)


# --------------------------------------------------------------------------
# State reducers
# --------------------------------------------------------------------------


def test_merge_by_key_is_idempotent_across_a_retry():
    """A retry re-fetches overlapping evidence. Appending would duplicate it,
    inflating the citation pack, the token bill and the precision metric."""
    first = {"chunk-a": {"n": 1}, "chunk-b": {"n": 2}}
    second = {"chunk-b": {"n": 2}, "chunk-c": {"n": 3}}
    merged = merge_by_key(first, second)
    assert set(merged) == {"chunk-a", "chunk-b", "chunk-c"}
    assert merge_by_key(merged, second) == merged


def test_retrieval_hint_signature_detects_a_repeat():
    a = RetrievalHint(keywords=["service", "affidavit"], activity_types=["Serving"])
    b = RetrievalHint(keywords=["affidavit", "service"], activity_types=["Serving"])
    assert a.signature() == b.signature()
    assert RetrievalHint().is_empty()
    assert not a.is_empty()
