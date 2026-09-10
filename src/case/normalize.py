"""
Turn raw timeline rows into `NormalizedEvent`s.

Rather than one adapter per `activity_sub_type` (33 combos in case_001 alone,
and the data dictionary documents nine more that appear in neither of our two
cases), events are handled by **shape**: a message, a document, a form
request, a milestone, a data entry, a calendar booking, a serving job, a note.

Lookup order is `(type, subtype)` → `type` → a loud default, so an unknown
event type from a future case degrades to something readable instead of
crashing ingestion.

The rules in `case_data/TIMELINE-REFERENCE.md` are applied here, once:
automated-vs-human traffic, transcript fidelity, generic-content types whose
real payload is in `extra_details`, and PII redaction.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

from ..data_loader import parse_extra_details, parse_timestamp
from .models import Actor, Direction, DocRef, NormalizedEvent

# --------------------------------------------------------------------------
# Text hygiene
# --------------------------------------------------------------------------

# `content` stores newlines as the two characters backslash-n, never as real
# newlines (2772 occurrences across the two cases; zero real newlines).
_UNESCAPE = ((r"\n", "\n"), (r"\"", '"'), (r"\t", "\t"))

# Secrets are encrypted at source (`vinny::hkdf-aes-gcm-v1::<base64>`). The
# ciphertext carries no information but would pollute the search index, so it
# is replaced with a marker that still tells the agent the field is on file.
_ENCRYPTED = re.compile(r"\b[a-z][a-z0-9_]*::[a-z0-9-]+::[A-Za-z0-9+/=]{12,}")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_SENSITIVE_KEY = re.compile(r"ssn|socialsecurity|social_security|taxid|passport", re.I)


def unescape(text: str | None) -> str:
    if not text:
        return ""
    for src, dst in _UNESCAPE:
        text = text.replace(src, dst)
    return text


def redact(text: str) -> str:
    """Strip secrets before anything is indexed, logged, or sent to a model."""
    text = _ENCRYPTED.sub("[ENCRYPTED — not readable]", text)
    return _SSN.sub("[REDACTED:SSN]", text)


def clean(text: str | None) -> str:
    """Unescape, redact, and collapse runaway blank lines."""
    out = redact(unescape(text))
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def _shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------
# Rendering internal field ids as English
# --------------------------------------------------------------------------

# The data dictionary is explicit: `data_points` keys are internal field ids
# and must be translated, never quoted. The generic camelCase splitter handles
# most of the 54 keys seen; these are the ones it would get wrong.
_KEY_OVERRIDES = {
    "clientSSN": "Client SSN",
    "clientProxyEmail": "Client proxy email",
    "CAHealthInsurance": "California health insurance",
    "CAInsuranceName": "California insurance carrier",
    "aiSharedDebtsDivision": "Shared debts division (AI-extracted)",
    "nameMaiden": "Maiden name",
    "nameMaidenRestore": "Restore maiden name",
    "oldAddy": "Previous address",
    "oldAddyDate": "Previous address since",
    "oldAddyWho": "Previous address — who lived there",
    "addyDate": "Address since",
}

_ACRONYMS = {"ssn", "ca", "ai", "dob", "id", "usa", "llc"}


def humanize_key(key: str) -> str:
    """`opposingPartyBestTimesToFind` → `Opposing party best times to find`."""
    if key in _KEY_OVERRIDES:
        return _KEY_OVERRIDES[key]
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", key)
    spaced = spaced.replace("_", " ").strip()
    words = [w.upper() if w.lower() in _ACRONYMS else w.lower() for w in spaced.split()]
    if not words:
        return key
    first = words[0]
    return " ".join([first if first.isupper() else first.capitalize(), *words[1:]])


def humanize_value(value: Any, depth: int = 0) -> Any:
    """Render a value for display, unwrapping the JSON-in-JSON this data uses.

    Several `data_points` values are JSON *strings* holding lists of objects
    (e.g. `children`), so one level of re-parsing is attempted.
    """
    if value is None or value == "":
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if depth == 0 and stripped[:1] in "[{" and stripped[-1:] in "]}":
            try:
                return humanize_value(json.loads(stripped), depth + 1)
            except json.JSONDecodeError:
                pass
        return redact(unescape(value))
    if isinstance(value, dict):
        return {
            humanize_key(k): humanize_value(v, depth + 1)
            for k, v in value.items()
            if v not in (None, "", [], {})
        }
    if isinstance(value, list):
        rendered = [humanize_value(v, depth + 1) for v in value]
        return [v for v in rendered if v not in (None, "", [], {})]
    return value


def _facts(source: dict, mapping: dict[str, str]) -> dict[str, Any]:
    """Pull a curated subset of `extra_details` under natural-language names."""
    out: dict[str, Any] = {}
    for raw_key, label in mapping.items():
        value = source.get(raw_key)
        if _SENSITIVE_KEY.search(raw_key):
            value = "[REDACTED]" if value else None
        rendered = humanize_value(value)
        if rendered not in (None, "", [], {}):
            out[label] = rendered
    return out


# --------------------------------------------------------------------------
# Actor resolution
# --------------------------------------------------------------------------

_STAFF_ROLES = {"attorney", "paralegal", "admin", "cc", "co-counsel", "staff", "lp"}
_INTERNAL_ROLES = _STAFF_ROLES | {"internal user", "agent", "owner"}
_CLIENT_ROLES = {"client", "external user", "enduser", "end user", "customer"}
_SYSTEM_ROLES = {"system", "automatic", "auto", "bot", "ai"}


def resolve_actor(row: dict, extra: dict) -> Actor:
    """Work out who acted, from whichever role field this shape uses.

    The declared role wins over the presence of a user id: an inbound call
    from the client carries an `int_user_marble_id` for the staff member who
    handled it, but the *actor* is the client. Getting this backwards would
    make "when did the client last reach out" answer with our own outreach.

    Ids are opaque; `roster.py` assigns human-readable labels afterwards, once
    every event in the case has been seen.
    """
    internal = (row.get("int_user_marble_id") or "").strip()
    external = (row.get("ext_user_marble_id") or "").strip()
    direction = (row.get("direction") or "").strip().lower()

    role = None
    for field in ("sender_role", "created_by_role", "who_uploaded", "owner_role", "creator_type"):
        value = extra.get(field)
        if isinstance(value, str) and value.strip():
            role = value.strip()
            break

    role_lower = (role or "").lower()
    if role_lower in _SYSTEM_ROLES:
        side = "system"
    elif role_lower in _CLIENT_ROLES:
        side = "client"
    elif role_lower in _INTERNAL_ROLES:
        side = "staff"
    elif direction == "inbound":
        side = "client"
    elif internal:
        side = "staff"
    else:
        side = "system"

    if side == "client":
        label = "the client"
    elif side == "system":
        label = "the system"
    elif role_lower in _STAFF_ROLES:
        label = f"the {role_lower}"
    else:
        label = "a staff member"

    # The id must belong to the side that acted. A row can carry both (an
    # inbound client call also names the staff member who handled it), and
    # picking the wrong one merges distinct people in the roster.
    actor_id = internal if side == "staff" else external if side == "client" else None
    return Actor(actor_id=actor_id or None, side=side, role=role, label=label)


def _direction(row: dict) -> Direction:
    value = (row.get("direction") or "").strip().lower()
    return value if value in ("inbound", "outbound", "internal") else "none"  # type: ignore[return-value]


# --------------------------------------------------------------------------
# Adapter registry
# --------------------------------------------------------------------------

# An adapter returns the shape-specific parts; common fields (ids, timestamps,
# actor, direction) are filled in by `normalize_row`.
Adapter = Callable[[dict, dict], dict]

_BY_PAIR: dict[tuple[str, str], Adapter] = {}
_BY_TYPE: dict[str, Adapter] = {}


def adapter(*keys: str) -> Callable[[Adapter], Adapter]:
    """Register an adapter for `"Type/Subtype"` pairs or a bare `"Type"`."""

    def register(fn: Adapter) -> Adapter:
        for key in keys:
            if "/" in key:
                head, _, tail = key.partition("/")
                _BY_PAIR[(head, tail)] = fn
            else:
                _BY_TYPE[key] = fn
        return fn

    return register


def _lookup(activity_type: str, sub_type: str) -> tuple[Adapter, bool]:
    """Resolve the adapter for an event. The bool reports whether it was the
    default — the ingest report surfaces that count so unhandled shapes in a
    new case are visible rather than silent."""
    if (fn := _BY_PAIR.get((activity_type, sub_type))) is not None:
        return fn, False
    if (fn := _BY_TYPE.get(activity_type)) is not None:
        return fn, False
    return _default_adapter, True


# --------------------------------------------------------------------------
# Shape adapters
# --------------------------------------------------------------------------


@adapter("Communications/Email")
def _email(row: dict, extra: dict) -> dict:
    body = clean(row.get("content"))
    subject = extra.get("subject") or ""
    # Some sources prefix the body with `Subject: ...\nBody: ...`.
    if not subject and body.startswith("Subject:"):
        head, _, rest = body.partition("\n")
        subject = head.removeprefix("Subject:").strip()
        body = rest.removeprefix("Body:").strip()
    elif body.startswith("Subject:"):
        _, _, rest = body.partition("\n")
        body = rest.removeprefix("Body:").strip()

    # Braze-sourced mail with a `flow_name` is marketing automation, not
    # attorney correspondence — it must not count as client contact.
    automated = extra.get("source") == "braze" or bool(extra.get("flow_name"))
    label = subject or _shorten(body, 60) or "(no subject)"
    return {
        "channel": "email",
        "headline": f"Email — {label}",
        "body": body,
        "is_automated": automated,
        "facts": _facts(
            extra,
            {
                "subject": "Subject",
                "from_email": "From",
                "recipients": "To",
                "carbon_copies": "CC",
                "sender_role": "Sender role",
                "recipient_role": "Recipient role",
                "attachments": "Attachments",
                "flow_name": "Automated flow",
                "is_delivery_failed": "Delivery failed",
            },
        ),
    }


@adapter("Communications/SMS")
def _sms(row: dict, extra: dict) -> dict:
    body = clean(row.get("content"))
    # A `template_name` (SMS-Magic / Braze) marks a templated blast.
    automated = bool(extra.get("template_name")) or extra.get("source") == "braze"
    return {
        "channel": "sms",
        "headline": f"SMS — {_shorten(body, 70) or '(empty)'}",
        "body": body,
        "is_automated": automated,
        "facts": _facts(
            extra,
            {
                "template_name": "Template",
                "sms_status": "Delivery status",
                "sender_role": "Sender role",
                "recipient_role": "Recipient role",
                "provider": "Provider",
            },
        ),
    }


@adapter("Communications/Call")
def _call(row: dict, extra: dict) -> dict:
    body = clean(row.get("content"))
    start = parse_timestamp(row.get("start_date"))
    end = parse_timestamp(row.get("end_date"))
    duration = extra.get("duration_ms")
    seconds = (duration / 1000) if isinstance(duration, (int, float)) else None
    if seconds is None and start and end:
        seconds = (end - start).total_seconds()

    # A call with no transcript still happened. The data dictionary is
    # explicit that empty content must not be read as "nothing occurred".
    silent = not body and bool(seconds)
    minutes = f"{seconds / 60:.0f} min" if seconds else "duration unknown"
    headline = f"Phone call ({minutes})"
    if silent:
        headline += " — no transcript captured"
    return {
        "channel": "call",
        "headline": headline,
        "body": body or None,
        "low_fidelity": bool(body),
        "has_silent_body": silent,
        "facts": _facts(
            extra,
            {
                "outcome": "Outcome",
                "sentiment_score": "Sentiment score",
                "answered_at": "Answered at",
                "initial_direction": "Initiated by",
                "service_name": "Service",
            },
        )
        | ({"Duration": minutes} if seconds else {}),
    }


@adapter("Communications/Voice AI")
def _voice_ai(row: dict, extra: dict) -> dict:
    body = clean(row.get("content"))
    summary = clean(extra.get("call_summary") or "")
    outcome = extra.get("call_outcome") or extra.get("agent_state") or "unknown outcome"
    return {
        "channel": "call",
        "headline": f"AI voice call — {outcome}",
        "body": summary or body or None,
        # An AI agent is not a human touchpoint.
        "is_automated": True,
        "low_fidelity": bool(body),
        "facts": _facts(
            extra,
            {
                "call_outcome": "Outcome",
                "agent_state": "Agent state",
                "call_status": "Call status",
                "voicemail": "Voicemail",
                "user_sentiment": "Client sentiment",
                "intake_call_summary": "Intake summary",
                "formatted_intake_summary": "Intake detail",
                "child_names_and_age": "Children",
                "has_key_date": "Key date reported",
                "represented_by_attorney": "Already represented",
            },
        )
        | ({"Transcript": _shorten(body, 400)} if body and summary else {}),
    }


@adapter("Communications/Chat")
def _chat(row: dict, extra: dict) -> dict:
    body = clean(row.get("content"))
    entries = extra.get("conversation_entries")
    if not body and entries:
        body = clean(json.dumps(entries)[:4000])
    return {
        "channel": "chat",
        "headline": f"Live chat — {_shorten(body, 70) or '(no transcript)'}",
        "body": body or None,
        "low_fidelity": True,
        "facts": _facts(extra, {"channel_name": "Channel", "accepted_date": "Accepted at"}),
    }


@adapter("Communications/Ticket")
def _ticket(row: dict, extra: dict) -> dict:
    body = clean(row.get("content"))
    number = extra.get("ticket_number") or "?"
    status = extra.get("status") or "unknown"
    kind = extra.get("type") or extra.get("record_type_name") or "Support"
    return {
        "channel": "portal",
        "headline": f"Support ticket {number} ({kind}) — {status}",
        "body": body or None,
        "facts": _facts(
            extra,
            {
                "ticket_number": "Ticket number",
                "type": "Type",
                "status": "Status",
                "outcome": "Outcome",
                "priority": "Priority",
                "support_ticket_reason": "Reason",
                "ticket_objective": "Objective",
                "internal_notes": "Internal notes",
                "owner_role": "Owner role",
                "closed_date": "Closed",
                "refund_amount": "Refund amount",
                "refund_processed": "Refund processed",
            },
        ),
    }


@adapter("Documents")
def _document(row: dict, extra: dict) -> dict:
    action = {"Document_Upload": "uploaded", "Document_Rename": "renamed"}.get(
        row.get("activity_sub_type", ""), (row.get("activity_sub_type") or "changed").lower()
    )
    display = extra.get("document_display_name") or extra.get("document_name") or "Document"
    file_name = extra.get("file_name")
    summary = clean(extra.get("summary") or extra.get("extended_summary") or "")
    who = extra.get("who_uploaded")
    doc = DocRef(
        display_name=str(display),
        file_name=file_name,
        category=extra.get("document_category"),
        who_uploaded=who,
        summary=summary or None,
        file_size=extra.get("file_size") if isinstance(extra.get("file_size"), int) else None,
    )
    return {
        "channel": "document",
        "headline": f"Document {action}: {file_name or display}",
        "body": summary or None,
        "doc_ref": doc,
        "facts": _facts(
            extra,
            {
                "document_display_name": "Document type",
                "document_category": "Category",
                "who_uploaded": "Uploaded by",
                "file_name": "File name",
                "document_description": "Description",
                "classification": "Classification",
                "is_duplicate": "Duplicate",
            },
        ),
    }


@adapter("Data Requests", "Service/Service Draft")
def _request(row: dict, extra: dict) -> dict:
    """Form / document requests.

    `Service Draft` and the `Drafts` sub-type are *form* workflows, not
    document drafting — the naming is a trap the data dictionary calls out.
    """
    status = extra.get("status") or extra.get("service_drafts_status") or "unknown"
    requested = [n for n in (extra.get("documents_requested_name") or []) if n]
    what = ", ".join(requested) if requested else (extra.get("service_name") or "form")
    kind = row.get("activity_sub_type") or "Request"
    is_form_workflow = kind in ("Drafts", "Service Draft")
    noun = "Form workflow" if is_form_workflow else "Document request"
    return {
        "channel": "portal",
        "headline": f"{noun} ({kind}) — {what} — {status}",
        "body": clean(extra.get("data_request_description") or "") or None,
        "facts": _facts(
            extra,
            {
                "status": "Status",
                "documents_requested_name": "Items requested",
                "data_request_description": "Description",
                "creator_description": "Requested by",
                "service_type_name": "Service",
                "request_created_at": "Requested at",
                "service_drafts_status": "Draft status",
                "activity": "Activity",
            },
        )
        | {"Workflow kind": "form/questionnaire" if is_form_workflow else "document upload"},
    }


@adapter(
    "Service",
    "Case/Status_Change",
    "Marbles",
    "Repeat",
    "Opportunity",
)
def _lifecycle(row: dict, extra: dict) -> dict:
    """Case, service, milestone and onboarding-checklist transitions."""
    body = clean(row.get("content"))
    sub = row.get("activity_sub_type") or ""
    activity_type = row.get("activity_type") or ""

    if activity_type == "Marbles":
        # Client-portal onboarding checklist — engagement signal, not a legal event.
        kind = str(extra.get("type") or "step").replace("_", " ").lower()
        change = extra.get("update_type") or "changed"
        headline = f"Client onboarding step '{kind}' {change}"
    elif sub == "Milestone Update":
        headline = (
            f"Milestone '{extra.get('milestone_name')}' — {extra.get('milestone_step')} "
            f"({extra.get('milestone_percent_completed')}%)"
        )
    elif activity_type == "Service":
        # The action lives on its own line; pull it out rather than relying on
        # a truncated body, which drops it for long service names.
        action_match = re.search(r"^Action:\s*(.+)$", body, re.M)
        action = action_match.group(1).strip() if action_match else _shorten(body, 60)
        headline = f"Service '{extra.get('service_name') or ''}' — {action}"
    elif activity_type == "Opportunity":
        headline = f"Sales opportunity — {extra.get('stage_name') or _shorten(body, 60)}"
    else:
        headline = _shorten(body, 90) or f"{activity_type} {sub}"

    return {
        "channel": "system",
        "headline": headline,
        "body": body or None,
        "is_automated": True,
        "facts": _facts(
            extra,
            {
                "status": "Status",
                "previous_status": "Previous status",
                "sub_status": "Sub-status",
                "service_name": "Service",
                "service_type_name": "Service type",
                "service_status": "Service status",
                "is_express_service": "Express (autopilot) service",
                "milestone_name": "Milestone",
                "milestone_step": "Milestone step",
                "milestone_percent_completed": "Milestone % complete",
                "service_percent_completed": "Service % complete",
                "case_start_date": "Case start date",
                "stage_name": "Sales stage",
                "type": "Checklist step",
                "update_type": "Change",
                "outcome": "Outcome",
                "attorney_notes": "Attorney notes",
                "internal_summary": "Internal summary",
                "key_date": "Key date",
            },
        ),
    }


@adapter("Data Entries")
def _data_entry(row: dict, extra: dict) -> dict:
    """Questionnaire submissions.

    `content` is always the literal string "Client data updated"; everything
    of value is in `extra_details.data_points`, whose keys are internal field
    ids that must be rendered in English.
    """
    points = extra.get("data_points") or {}
    facts: dict[str, Any] = {}
    if isinstance(points, dict):
        for key, value in points.items():
            if _SENSITIVE_KEY.search(key):
                facts[humanize_key(key)] = "[REDACTED — on file]" if value else None
                continue
            rendered = humanize_value(value)
            if rendered not in (None, "", [], {}):
                facts[humanize_key(key)] = rendered
    facts = {k: v for k, v in facts.items() if v is not None}
    fields = ", ".join(list(facts)[:6]) or "no readable fields"
    return {
        "channel": "portal",
        "headline": f"Client data recorded — {fields}",
        "body": None,
        "facts": facts,
    }


@adapter("Calendar Event", "Scheduling")
def _calendar(row: dict, extra: dict) -> dict:
    """Internal Marble meetings.

    These are **never** court hearings. The data dictionary warns that
    conflating the two falsely inflates urgency, so the headline says what it
    is and the fact set never uses hearing vocabulary.
    """
    body = clean(row.get("content"))
    title = extra.get("meeting_title") or extra.get("request_title") or "Internal meeting"
    status = extra.get("status") or "requested"
    return {
        "channel": "portal",
        "headline": f"Internal meeting (not a court hearing): {title} — {status}",
        "body": body or None,
        "facts": _facts(
            extra,
            {
                "meeting_title": "Meeting",
                "meeting_type": "Meeting type",
                "status": "Booking status",
                "bookee_timezone": "Client timezone",
                "notes": "Notes",
                "request_notes": "Request notes",
            },
        )
        | {"Event nature": "internal Marble call — not a court date"},
    }


@adapter("Serving")
def _serving(row: dict, extra: dict) -> dict:
    """Process-server jobs. Many rows per job as the status ladder advances."""
    body = clean(row.get("content"))
    name = extra.get("recipient_name")
    if isinstance(name, dict):
        recipient = " ".join(
            str(name.get(p) or "").strip() for p in ("first", "middle", "last")
        ).strip()
        recipient = " ".join(recipient.split())
    else:
        recipient = str(name or "").strip()

    status = extra.get("status") or "unknown"
    attempt = extra.get("attempt_status")
    number = extra.get("current_attempt_number")
    bits = [f"Service of process — {status}"]
    if attempt:
        bits.append(f"attempt {number}: {attempt}")
    if recipient:
        bits.append(f"on {recipient}")

    addresses = extra.get("recipient_addresses")
    return {
        "channel": "system",
        "headline": " · ".join(bits),
        "body": body or None,
        "facts": _facts(
            extra,
            {
                "status": "Job status",
                "attempt_status": "Attempt status",
                "current_attempt_number": "Attempt number",
                "proof_serve_status": "Proof status",
                "recipient_type": "Recipient type",
                "attempt_description": "Attempt notes",
                "job_created_at": "Job created",
            },
        )
        | ({"Recipient": recipient} if recipient else {})
        | ({"Service addresses": humanize_value(addresses)} if addresses else {}),
    }


@adapter("Finance")
def _finance(row: dict, extra: dict) -> dict:
    body = clean(row.get("content"))
    cents = None
    match = re.search(r"Amount\(cents\):\s*(\d+)", body)
    if match:
        cents = int(match.group(1))
    amount = f"${cents / 100:,.2f}" if cents is not None else "amount unknown"
    event = extra.get("event")
    status = extra.get("status") or (event.get("status") if isinstance(event, dict) else None)
    return {
        "channel": "system",
        "headline": f"Payment — {amount} ({status or 'status unknown'})",
        "body": body or None,
        "facts": ({"Amount": amount} if cents is not None else {})
        | _facts(extra, {"status": "Status", "created_at": "Created at"}),
    }


@adapter(
    "Case Update",
    "Case/Intake notes update",
    "Intake Event",
    "Legal Cortex",
    "Feedback",
    "Qualifications Engine",
)
def _note(row: dict, extra: dict) -> dict:
    """Human- and AI-written notes, intake records and satisfaction surveys."""
    body = clean(row.get("content"))
    activity_type = row.get("activity_type") or ""
    sub = row.get("activity_sub_type") or ""

    if activity_type == "Case Update":
        header = extra.get("content_header")
        who = extra.get("created_by_role") or "staff"
        headline = f"Case note ({who}): {header or _shorten(body, 70)}"
    elif activity_type == "Feedback":
        rating = extra.get("rating")
        skipped = extra.get("is_skip")
        headline = f"Client feedback ({sub}) — " + (
            "skipped" if skipped else f"{rating}/5" if rating else "submitted"
        )
    elif activity_type == "Intake Event":
        headline = f"Intake — {_shorten(body, 70)}"
    elif activity_type == "Legal Cortex":
        headline = f"AI assistant session ({sub}) — {_shorten(body, 70)}"
    elif activity_type == "Qualifications Engine":
        headline = "Qualification questionnaire completed"
    else:
        headline = _shorten(body, 90) or f"{activity_type} {sub}"

    facts = _facts(
        extra,
        {
            "content_header": "Heading",
            "created_by_role": "Written by",
            "waiting_for": "Waiting for",
            "is_externally_visible": "Visible to client",
            "rating": "Rating",
            "is_skip": "Survey skipped",
            "questions": "Survey answers",
            "intake_notes": "Intake notes",
            "funnel_parameters": "Intake form",
            "case_jurisdiction": "Jurisdiction",
            "selected_service_code": "Service selected",
            "message_count": "Messages in session",
            "current_bucket": "Qualification stage",
        },
    )
    return {
        "channel": "portal",
        "headline": headline,
        "body": body or None,
        "facts": facts,
    }


def _default_adapter(row: dict, extra: dict) -> dict:
    """Fallback for event shapes we have never seen.

    The data dictionary documents nine types absent from both bundled cases,
    and a future case will bring more. Rather than dropping them, surface the
    content plus every non-trivial `extra_details` scalar under a humanized
    name — degraded, but readable and searchable.
    """
    body = clean(row.get("content"))
    facts = {}
    for key, value in extra.items():
        if key in ("origin_id", "origin_platform", "source", "source_key"):
            continue
        if _SENSITIVE_KEY.search(key):
            facts[humanize_key(key)] = "[REDACTED — on file]" if value else None
            continue
        rendered = humanize_value(value)
        if isinstance(rendered, (str, int, float, bool)) and rendered not in (None, ""):
            facts[humanize_key(key)] = rendered
    label = f"{row.get('activity_type')}/{row.get('activity_sub_type')}"
    return {
        "channel": "system",
        "headline": _shorten(body, 90) or label,
        "body": body or None,
        "facts": {k: v for k, v in facts.items() if v is not None},
    }


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

# Tolerance before `start_date` being later than `activity_created_at` counts
# as a genuine booking rather than clock skew between source systems.
_SCHEDULED_TOLERANCE_SECONDS = 120


def normalize_row(row: dict, ref: str) -> tuple[NormalizedEvent, bool]:
    """Normalize one raw row. Returns the event and whether the default
    adapter had to be used."""
    activity_type = (row.get("activity_type") or "Unknown").strip()
    sub_type = (row.get("activity_sub_type") or "").strip()
    extra = parse_extra_details(row.get("extra_details"))

    fn, used_default = _lookup(activity_type, sub_type)
    try:
        shape = fn(row, extra)
    except Exception:  # a malformed row must not take down the case
        shape = _default_adapter(row, extra)
        used_default = True

    timestamps = row.get("_ts") or {}
    when = timestamps.get("activity_created_at") or parse_timestamp(row.get("activity_created_at"))
    occurs_at = timestamps.get("start_date") or parse_timestamp(row.get("start_date")) or when
    ends_at = timestamps.get("end_date") or parse_timestamp(row.get("end_date"))
    if when is None:
        when = occurs_at or datetime.min

    scheduled = bool(
        occurs_at and when and (occurs_at - when).total_seconds() > _SCHEDULED_TOLERANCE_SECONDS
    )

    actor = resolve_actor(row, extra)
    # Anything with no human on our side of it is automation. This catches
    # system notifications that carry no `template_name`/`flow_name` but are
    # plainly not a person reaching out ("We have filed your petition...").
    automated = bool(shape.get("is_automated")) or actor.side == "system"

    event = NormalizedEvent(
        raw_id=row.get("id") or ref,
        ref=ref,
        case_id=row.get("case_id") or "",
        when=when,
        occurs_at=occurs_at or when,
        ends_at=ends_at,
        activity_type=activity_type,
        activity_sub_type=sub_type,
        channel=shape.get("channel", "system"),
        direction=_direction(row),
        actor=actor,
        is_automated=automated,
        low_fidelity=bool(shape.get("low_fidelity")),
        is_scheduled=scheduled,
        has_silent_body=bool(shape.get("has_silent_body")),
        headline=shape.get("headline") or f"{activity_type}/{sub_type}",
        body=shape.get("body"),
        facts=shape.get("facts") or {},
        doc_ref=shape.get("doc_ref"),
    )
    return event, used_default


def normalize_case(rows: list[dict]) -> tuple[list[NormalizedEvent], dict[str, Any]]:
    """Normalize a whole case, in chronological order.

    The report counts rows that fell through to the default adapter and lists
    the shapes involved, so an unhandled event type in a new case shows up in
    the ingest output instead of silently degrading retrieval.
    """
    events: list[NormalizedEvent] = []
    unhandled: dict[str, int] = {}

    for position, row in enumerate(rows, start=1):
        event, used_default = normalize_row(row, ref=f"E{position:03d}")
        events.append(event)
        if used_default:
            key = f"{event.activity_type}/{event.activity_sub_type}"
            unhandled[key] = unhandled.get(key, 0) + 1

    report = {
        "event_count": len(events),
        "unhandled_count": sum(unhandled.values()),
        "unhandled_shapes": dict(sorted(unhandled.items(), key=lambda kv: -kv[1])),
        "automated_count": sum(1 for e in events if e.is_automated),
        "low_fidelity_count": sum(1 for e in events if e.low_fidelity),
        "scheduled_count": sum(1 for e in events if e.is_scheduled),
    }
    return events, report
