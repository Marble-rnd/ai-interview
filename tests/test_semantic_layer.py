"""
Tests for the semantic layer.

These encode the rules from `case_data/TIMELINE-REFERENCE.md` that would
otherwise only live in a prompt, plus the generalization claim: every event
shape in the bundled cases is handled, and an unseen shape degrades instead
of crashing.
"""

from __future__ import annotations

import pytest

from src.case.documents import _fingerprint, _score, build_catalog, extract, render_page
from src.case.normalize import (
    _lookup,
    clean,
    humanize_key,
    normalize_case,
    normalize_row,
    redact,
)
from src.case.roster import build_roster
from src.data_loader import load_timeline, parse_extra_details, parse_timestamp

# --------------------------------------------------------------------------
# Adapter coverage — the generalization claim
# --------------------------------------------------------------------------


def test_every_shape_in_the_bundled_cases_has_a_real_adapter(loaded):
    """No event in either case may fall through to the default adapter."""
    for name, case in loaded.items():
        report = case["report"]
        assert report["unhandled_count"] == 0, (
            f"{name} fell back to the default adapter for: {report['unhandled_shapes']}"
        )


def test_unknown_event_shape_degrades_instead_of_crashing():
    """The data dictionary documents nine event types that appear in neither
    bundled case. A future case must not break ingestion."""
    row = {
        "id": "synthetic-1",
        "case_id": "c1",
        "activity_created_at": "2026-09-01T10:00:00",
        "start_date": "2026-09-01T10:00:00",
        "end_date": "",
        "int_user_marble_id": "staff-1",
        "ext_user_marble_id": "",
        "activity_type": "Task",
        "activity_sub_type": "Legal_Task",
        "direction": "",
        "content": "Legal Task: [Open]\\nDraft the mediation memo",
        "extra_details": '{"status":"Open","subject":"Mediation memo","due_date":"2026-09-20"}',
        "source": "timeline",
    }
    adapter, used_default = _lookup("Task", "Legal_Task")
    assert used_default is True

    event, was_default = normalize_row(row, ref="E001")
    assert was_default is True
    # Degraded, but still readable and searchable.
    assert "mediation memo" in event.searchable_text().lower()
    assert event.facts.get("Status") == "Open"
    assert event.facts.get("Due date") == "2026-09-20"
    assert "\n" in event.body  # the literal \n escape was decoded


def test_malformed_extra_details_does_not_break_a_row():
    row = {
        "id": "synthetic-2",
        "case_id": "c1",
        "activity_created_at": "2026-09-01T10:00:00",
        "start_date": "2026-09-01T10:00:00",
        "end_date": "",
        "int_user_marble_id": "",
        "ext_user_marble_id": "client-1",
        "activity_type": "Communications",
        "activity_sub_type": "Email",
        "direction": "Inbound",
        "content": "Subject: Hello\\nBody: hi there",
        "extra_details": "{not valid json",
        "source": "timeline",
    }
    event, _ = normalize_row(row, ref="E002")
    assert event.headline.startswith("Email — Hello")
    assert event.body == "hi there"


# --------------------------------------------------------------------------
# Text hygiene and PII
# --------------------------------------------------------------------------


def test_literal_backslash_n_is_decoded():
    """`content` stores newlines as two characters, never as real newlines."""
    assert clean(r"line one\nline two") == "line one\nline two"


def test_encrypted_secrets_are_redacted_before_indexing():
    blob = "vinny::hkdf-aes-gcm-v1::N3ayTI0xEdkYMyfn::8jrg/UeMZVeaAvPHrbUo1g=="
    assert "vinny" not in redact(blob)
    assert "ENCRYPTED" in redact(blob)


def test_plaintext_ssn_is_redacted():
    assert redact("his ssn is 123-45-6789 ok") == "his ssn is [REDACTED:SSN] ok"


def test_client_ssn_never_reaches_the_index(loaded):
    """The real `clientSSN` rows in case_001 must not leak ciphertext or a
    number into anything the model or the search index sees."""
    seen_ssn_field = False
    for case in loaded.values():
        for event in case["events"]:
            text = event.searchable_text()
            assert "hkdf-aes-gcm" not in text
            for key, value in event.facts.items():
                if "ssn" in key.lower():
                    seen_ssn_field = True
                    assert value == "[REDACTED — on file]"
    assert seen_ssn_field, "expected at least one SSN field in the bundled cases"


def test_internal_field_ids_are_rendered_in_english():
    assert humanize_key("opposingPartyBestTimesToFind") == "Opposing party best times to find"
    assert humanize_key("petitionerMarriageQuestion") == "Petitioner marriage question"
    assert humanize_key("clientSSN") == "Client SSN"
    assert humanize_key("CAHealthInsurance") == "California health insurance"


def test_data_entries_expose_payload_not_the_generic_content(loaded):
    """`content` is always "Client data updated"; the value is in data_points."""
    events = loaded["case_001"]["events"]
    entries = [e for e in events if e.activity_type == "Data Entries" and e.facts]
    assert entries
    for event in entries:
        assert "client data updated" not in event.headline.lower()
        # Keys are humanized, never raw camelCase ids.
        assert not any(key[0].islower() and " " not in key for key in event.facts)


# --------------------------------------------------------------------------
# Reference-doc rules encoded as behaviour
# --------------------------------------------------------------------------


def test_automated_traffic_is_flagged(loaded):
    """Braze `flow_name` mail and templated SMS are not human contact."""
    for case in loaded.values():
        for event in case["events"]:
            if event.facts.get("Automated flow") or event.facts.get("Template"):
                assert event.is_automated


def test_human_contact_excludes_every_system_sender(loaded):
    for case in loaded.values():
        human = [
            e
            for e in case["events"]
            if e.channel in ("email", "sms", "call", "chat") and not e.is_automated
        ]
        assert human, "expected some genuine human contact"
        assert all(e.actor.side != "system" for e in human)


def test_last_client_contact_matches_hand_verified_ground_truth(loaded):
    """Verified by hand against the raw CSV before the pipeline existed."""
    expected = {
        "case_001": ("2026-09-08T19:04:49", "email"),
        "case_002": ("2026-09-08T16:05:42", "sms"),
    }
    for name, (stamp, channel) in expected.items():
        inbound = [
            e
            for e in loaded[name]["events"]
            if e.direction == "inbound"
            and not e.is_automated
            and e.channel in ("email", "sms", "call", "chat")
        ]
        last = inbound[-1]
        assert last.when.isoformat().startswith(stamp)
        assert last.channel == channel


def test_calendar_events_are_never_described_as_hearings(loaded):
    """The data dictionary warns that conflating the two inflates urgency."""
    for case in loaded.values():
        for event in case["events"]:
            if event.activity_type in ("Calendar Event", "Scheduling"):
                assert "not a court" in event.headline.lower()
                assert "hearing" not in event.headline.lower().replace("not a court hearing", "")


def test_untranscribed_calls_are_not_reported_as_nothing_happening(loaded):
    """A call with empty content but a real duration still happened."""
    silent = [
        e
        for case in loaded.values()
        for e in case["events"]
        if e.channel == "call" and e.has_silent_body
    ]
    assert silent, "expected at least one call with no transcript"
    for event in silent:
        assert "no transcript" in event.headline.lower()
        assert event.duration_seconds


def test_transcripts_are_marked_low_fidelity(loaded):
    """Transcription errors mean names/addresses must not be quoted from these."""
    for case in loaded.values():
        transcribed = [e for e in case["events"] if e.channel == "call" and e.body]
        assert transcribed
        assert all(e.low_fidelity for e in transcribed)


def test_future_dated_bookings_are_marked_scheduled(loaded):
    """case_002's kickoff call sits at 2026-10-02, after it was created."""
    scheduled = [e for e in loaded["case_002"]["events"] if e.is_scheduled]
    assert scheduled
    assert any(e.occurs_at.isoformat().startswith("2026-10-02") for e in scheduled)


def test_form_workflows_are_not_confused_with_document_drafting(loaded):
    """`Service Draft` / `Data Requests/Drafts` are questionnaires."""
    for case in loaded.values():
        for event in case["events"]:
            if event.activity_sub_type in ("Drafts", "Service Draft"):
                assert event.facts.get("Workflow kind") == "form/questionnaire"


# --------------------------------------------------------------------------
# Actors
# --------------------------------------------------------------------------


def test_inbound_client_message_is_attributed_to_the_client(loaded):
    """An inbound call carries the staff id of whoever handled it; the actor
    is still the client."""
    for case in loaded.values():
        inbound = [
            e for e in case["events"] if e.direction == "inbound" and e.channel in ("call", "email")
        ]
        assert inbound
        assert all(e.actor.side in ("client", "system") for e in inbound)


def test_roster_resolves_people_and_never_leaks_ids(loaded):
    expected = {"case_001": "Idris Sorlie", "case_002": "Mateo Lindqvist"}
    for name, client_name in expected.items():
        roster = loaded[name]["roster"]
        assert roster.client_name == client_name
        assert roster.attorney_name == "Dana Whitfield"
        attorneys = [p for p in roster.people.values() if (p.role or "") == "Attorney"]
        assert attorneys, f"{name} has no attorney in the roster"
        for person in roster.people.values():
            assert person.actor_id not in person.label


def test_actor_ids_are_not_shared_between_staff_and_client(loaded):
    """A row naming both parties must not merge them into one person."""
    for case in loaded.values():
        sides: dict[str, set[str]] = {}
        for event in case["events"]:
            if event.actor.actor_id:
                sides.setdefault(event.actor.actor_id, set()).add(event.actor.side)
        assert all(len(v) == 1 for v in sides.values())


# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("timeline_name", "disk_name", "should_match"),
    [
        ("INITIAL DRAFT - FL-150.pdf", "initial_draft_fl_150.pdf", True),
        # Form numbers are identity: these must never be conflated.
        ("INITIAL DRAFT - FL-150.pdf", "initial_draft_fl_140.pdf", False),
        ("INITIAL DRAFT - FL-105.pdf", "initial_draft_fl_150.pdf", False),
        (
            "INITIAL DRAFT - FL-130A (Resolution Bundle).pdf",
            "initial_draft_fl_130a_resolution_bundle.pdf",
            True,
        ),
        # E-filing job numbers are transaction ids, not identity.
        (
            "074038651 Notice of Limited Appearance.pdf",
            "notice_of_limited_appearance_filed.pdf",
            True,
        ),
        ("Idris Sorlie - UCCJEA.docx", "idris_sorlie_uccjea.docx", True),
        ("Idris Sorlie - UCCJEA.docx", "idris_sorlie_divorce_petition.docx", False),
    ],
)
def test_document_name_matching(timeline_name, disk_name, should_match):
    score = _score(timeline_name, disk_name)
    assert (score >= 70) is should_match, f"score={score} fp={_fingerprint(timeline_name)!r}"


def test_catalog_matches_every_disk_file_exactly_once(loaded):
    for name, case in loaded.items():
        disk_names = [e.disk_name for e in case["catalog"] if e.disk_name]
        assert len(disk_names) == len(set(disk_names)), f"{name} matched a file twice"
        from src.data_loader import list_documents

        assert set(disk_names) == set(list_documents(case["dir"]))


def test_fl_forms_map_to_their_own_file(loaded):
    catalog = {e.timeline_file_name: e.disk_name for e in loaded["case_002"]["catalog"]}
    assert catalog["INITIAL DRAFT - FL-100.pdf"] == "initial_draft_fl_100.pdf"
    assert catalog["INITIAL DRAFT - FL-150.pdf"] == "initial_draft_fl_150.pdf"
    assert catalog["INITIAL DRAFT - FL-140.pdf"] == "initial_draft_fl_140.pdf"
    # FL-105 was drafted per the timeline but is not in this snapshot.
    assert catalog["INITIAL DRAFT - FL-105.pdf"] is None


def test_uploads_absent_from_the_snapshot_are_reported_not_hidden(loaded):
    absent = [e for e in loaded["case_002"]["catalog"] if e.disk_name is None]
    names = {e.timeline_file_name for e in absent}
    assert "INITIAL DRAFT - FL-105.pdf" in names
    # The client's financial disclosures are recorded but not bundled.
    assert any("Payslip" in (n or "") for n in names)
    assert all(e.text_status == "absent" for e in absent)


def test_repeat_uploads_collapse_to_one_entry(loaded):
    """case_002 re-drafts the whole FL family; each form is one document."""
    entry = next(
        e for e in loaded["case_002"]["catalog"] if e.disk_name == "initial_draft_fl_100.pdf"
    )
    assert len(entry.event_refs) == 2


def test_scanned_pdfs_are_detected(loaded):
    catalog = loaded["case_001"]["catalog"]
    statuses = {e.disk_name: e.text_status for e in catalog if e.disk_name}
    assert statuses["issued_summons_sorlie.pdf"] == "scanned"
    assert statuses["standing_family_law_order.pdf"] == "scanned"
    assert statuses["return_of_service_affidavit.pdf"] == "scanned"
    # ...and text-layer PDFs are not mislabelled.
    assert statuses["notice_of_related_cases_filed.pdf"] == "text"


def test_docx_extraction_reads_the_petition(loaded):
    path = loaded["case_001"]["dir"] / "documents" / "idris_sorlie_divorce_petition.docx"
    text = extract(path).pages[0].text
    assert "TWELFTH JUDICIAL CIRCUIT" in text.upper()
    assert "MANATEE COUNTY" in text.upper()


def test_pdf_page_rendering_produces_a_png(loaded):
    path = loaded["case_001"]["dir"] / "documents" / "issued_summons_sorlie.pdf"
    png = render_page(path, 1, scale=1.0)
    assert png.startswith(b"\x89PNG")
    with pytest.raises(IndexError):
        render_page(path, 99)


# --------------------------------------------------------------------------
# Loader
# --------------------------------------------------------------------------


def test_timeline_is_sorted_chronologically_on_parsed_datetimes(loaded):
    for case in loaded.values():
        stamps = [r["_ts"]["activity_created_at"] for r in case["rows"]]
        assert stamps == sorted(stamps)
        assert all(s.tzinfo is not None for s in stamps)


def test_timestamp_parsing_handles_the_shapes_in_this_data():
    assert parse_timestamp("2026-08-19T12:46:01").hour == 12
    assert parse_timestamp("2026-08-05T21:54:54.496000").microsecond == 496000
    assert parse_timestamp("2026-08-19 15:01:34.352+00").tzinfo is not None
    assert parse_timestamp("") is None
    assert parse_timestamp("not a date") is None


def test_extra_details_parsing_never_raises():
    assert parse_extra_details("") == {}
    assert parse_extra_details("{bad") == {}
    assert parse_extra_details('{"a":1}') == {"a": 1}
