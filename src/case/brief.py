"""
The deterministic case brief.

Status questions ("where does this case stand?", "what are we waiting on?")
are arithmetic over structured events, not a reading comprehension problem.
Computing them here means the answer is the same every time, costs nothing,
and cannot be hallucinated — the model gets a filled-in brief and only has to
explain it.

The deadline arithmetic is the part an attorney actually wants: served on
2026-09-04, twenty-day window, closed 2026-09-24, no response on record.
Every deadline carries the events it was derived from.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .documents import CatalogEntry, DocumentText
from .models import CaseBrief, ContactSummary, Deadline, NormalizedEvent
from .roster import Roster

CONTACT_CHANNELS = ("email", "sms", "call", "chat")

# Days a respondent has to answer after being served, by state. These are the
# standard windows for the two jurisdictions in the bundled cases; anything
# not listed produces no computed deadline rather than a guess.
_RESPONSE_WINDOW_DAYS = {
    "florida": (20, "Fla. Fam. L. R. P. 12.140 — 20 days after service"),
    "california": (30, "Cal. Fam. Code / FL-120 — 30 days after service"),
}

_INTAKE_FIELDS = {
    "Case Type": "matter_type",
    "Name": "client_name",
    "Opposing Party": "opposing_party",
    "Children": "children_raw",
    "Petitioner or Respondent": "client_role",
    "Case Number": "court_case_number",
}

# On a filed document the number is captioned `Case No.: 2486DR7193`. This is
# authoritative — it is what the clerk assigned.
_DOC_CASE_NUMBER = re.compile(r"Case\s*(?:No|Number)\.?\s*:\s*([A-Z0-9-]{6,})", re.I)
# On e-filing notification emails: `SERVICE OF COURT DOCUMENT CASE NUMBER <x>`.
# Weaker: these strings are portal identifiers and do not always match the
# docket number, so a document beats an email.
_COURT_NUMBER = re.compile(r"CASE NUMBER\s+([A-Z0-9]{8,})\b")
# Court captions in filed petitions: `IN AND FOR MANATEE COUNTY, FLORIDA`.
_CAPTION_COUNTY = re.compile(r"IN AND FOR\s+([A-Z .']+?)\s+COUNTY,\s+([A-Z ]+)", re.I)
# California Judicial Council header: `SUPERIOR COURT OF CALIFORNIA, COUNTY OF <x>`.
_CAPTION_CA = re.compile(
    r"SUPERIOR COURT OF ([A-Z]+),\s*COUNTY OF\s*([A-Za-z .']{0,30}?)\s*(?:\n|$)", re.I
)

# Blank Judicial Council forms leave the caption unfilled, so the next line is
# the field label rather than a county. Matching those would invent a
# jurisdiction out of a form template.
_FORM_LABELS = {
    "branch name",
    "case number",
    "street address",
    "mailing address",
    "city and zip code",
    "petitioner",
    "respondent",
    "name",
}


def _clean_county(value: str | None) -> str | None:
    text = " ".join((value or "").split()).strip(" .:")
    if not text or text.lower() in _FORM_LABELS or len(text) < 3:
        return None
    return text.title()


def _parse_intake_notes(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        field = _INTAKE_FIELDS.get(key.strip())
        if field and value.strip():
            out[field] = value.strip()
    return out


def _court_from_documents(texts: dict[str, DocumentText]) -> str | None:
    """Read the court caption off a filed document.

    The intake funnel and the intake call disagree with each other in
    case_001 (Hillsborough vs Sarasota); the petition itself says Manatee.
    A filed document outranks both — the data dictionary warns that
    transcripts carry transcription errors.
    """
    for name, text in texts.items():
        if not any(k in name.lower() for k in ("petition", "summons", "fl_100", "fl100")):
            continue
        head = "\n".join(p.text for p in text.pages[:2])[:4000]
        # (pattern, county group, state group) — Florida-style caption first,
        # then the California Judicial Council header.
        for pattern, county_group, state_group in (
            (_CAPTION_COUNTY, 1, 2),
            (_CAPTION_CA, 2, 1),
        ):
            match = pattern.search(head)
            if match is None:
                continue
            county = _clean_county(match.group(county_group))
            if county:
                return f"{county} County, {match.group(state_group).title().strip()}"
    return None


def _case_number_from_documents(texts: dict[str, DocumentText]) -> str | None:
    """The docket number as captioned on a filed document.

    In case_001 this only exists in the *scanned* summons, which is exactly
    why the OCR pass at ingest earns its cost.
    """
    for name, text in texts.items():
        if not any(k in name.lower() for k in ("summons", "petition", "order", "service")):
            continue
        head = "\n".join(p.text for p in text.pages[:2])[:4000]
        if match := _DOC_CASE_NUMBER.search(head):
            value = match.group(1).strip()
            # Blank templates caption the field with nothing after it.
            if value and not value.lower().startswith("division"):
                return value
    return None


def _contact_summary(events: list[NormalizedEvent], now: datetime) -> ContactSummary:
    """Human touchpoints only — automated mail and templated SMS excluded."""
    human = [
        e for e in events if e.channel in CONTACT_CHANNELS and not e.is_automated and e.when <= now
    ]
    inbound = [e for e in human if e.direction == "inbound"]
    outbound = [e for e in human if e.direction == "outbound"]

    summary = ContactSummary()
    if inbound:
        summary.last_inbound = inbound[-1].when
        summary.last_inbound_ref = inbound[-1].ref
        summary.days_since_client_spoke = (now - inbound[-1].when).days
    if outbound:
        summary.last_outbound_human = outbound[-1].when
        summary.last_outbound_human_ref = outbound[-1].ref
        summary.days_since_we_reached_out = (now - outbound[-1].when).days

    # How many times we have reached out since the client last replied.
    cutoff = summary.last_inbound
    summary.unanswered_outbound_streak = (
        sum(1 for e in outbound if cutoff is None or e.when > cutoff) if outbound else 0
    )
    return summary


def _service_of_process(events: list[NormalizedEvent]) -> NormalizedEvent | None:
    """The event where the opposing party was successfully served."""
    for event in events:
        if event.activity_type != "Serving":
            continue
        if str(event.facts.get("Attempt status", "")).upper() == "SUCCEEDED":
            return event
    return None


def _deadlines(
    events: list[NormalizedEvent], jurisdiction: str | None, now: datetime
) -> list[Deadline]:
    out: list[Deadline] = []
    served = _service_of_process(events)
    state = (jurisdiction or "").lower()
    window = next((v for k, v in _RESPONSE_WINDOW_DAYS.items() if k in state), None)

    if served and window:
        days, authority = window
        due = served.occurs_at + timedelta(days=days)
        # Did the opposing party respond? Nothing in either case does, but the
        # check must exist rather than being assumed.
        responded = any(
            "answer" in (e.headline or "").lower() and e.activity_type == "Documents"
            for e in events
            if e.when > served.occurs_at
        )
        out.append(
            Deadline(
                name="Respondent's answer due",
                due=due,
                basis=(
                    f"Opposing party served {served.occurs_at.date()}; {days}-day window "
                    f"({authority}). Computed — verify against local rules."
                ),
                basis_refs=[served.ref],
                status="met" if responded else ("elapsed" if now > due else "running"),
                days_remaining=None if responded else (due - now).days,
            )
        )
    elif served:
        out.append(
            Deadline(
                name="Respondent's answer due",
                due=None,
                basis=(
                    f"Opposing party served {served.occurs_at.date()}, but the response "
                    "window for this jurisdiction is not configured."
                ),
                basis_refs=[served.ref],
                status="unknown",
            )
        )
    return out


def _requests(events: list[NormalizedEvent]) -> tuple[list[dict], list[dict]]:
    """Latest status per requested item.

    A request that was *cancelled* is not outstanding — the team withdrew it.
    Conflating cancelled with pending would report work the client does not
    in fact owe us.
    """
    latest: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.activity_type != "Data Requests":
            continue
        items = event.facts.get("Items requested") or []
        if isinstance(items, str):
            items = [items]
        status = str(event.facts.get("Status") or "Unknown")
        for item in items:
            latest[str(item)] = {
                "item": str(item),
                "status": status,
                "as_of": event.when.isoformat(),
                "ref": event.ref,
            }

    outstanding = [r for r in latest.values() if r["status"].lower() in ("pending", "created")]
    settled = [r for r in latest.values() if r["status"].lower() not in ("pending", "created")]
    outstanding.sort(key=lambda r: r["as_of"])
    settled.sort(key=lambda r: r["as_of"])
    return outstanding, settled


def _open_tickets(events: list[NormalizedEvent]) -> list[dict]:
    """Unresolved support tickets are things the case owes somebody.

    Tickets carry a running status, so only the final state of each ticket
    number counts — a ticket reopened and then solved is not outstanding.
    """
    latest: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.activity_sub_type != "Ticket":
            continue
        number = str(event.facts.get("Ticket number") or event.ref)
        latest[number] = {
            "item": f"Ticket {number}: {event.facts.get('Objective') or event.facts.get('Type') or 'support request'}",
            "status": str(event.facts.get("Status") or "Unknown"),
            "as_of": event.when.isoformat(),
            "ref": event.ref,
        }
    return [
        t for t in latest.values() if t["status"].lower() in ("new", "open", "pending", "escalated")
    ]


def _services(events: list[NormalizedEvent]) -> list[dict]:
    services: dict[str, dict[str, Any]] = {}
    for event in events:
        name = event.facts.get("Service") or event.facts.get("Service type")
        if not name or event.activity_type != "Service":
            continue
        entry = services.setdefault(str(name), {"name": str(name), "events": 0})
        entry["events"] += 1
        entry["last_action"] = event.headline
        entry["as_of"] = event.when.isoformat()
        if (percent := event.facts.get("Service % complete")) is not None:
            entry["percent_complete"] = percent
    return sorted(services.values(), key=lambda s: s.get("as_of", ""))


def _milestone(events: list[NormalizedEvent]) -> tuple[str | None, int | None]:
    latest = None
    for event in events:
        if event.activity_sub_type == "Milestone Update":
            latest = event
    if latest is None:
        return None, None
    step = latest.facts.get("Milestone step") or latest.facts.get("Milestone")
    percent = latest.facts.get("Service % complete")
    return (str(step) if step else None, percent if isinstance(percent, int) else None)


def build_brief(
    case_slug: str,
    events: list[NormalizedEvent],
    catalog: list[CatalogEntry],
    roster: Roster,
    rows: list[dict],
    document_texts: dict[str, DocumentText] | None = None,
    now: datetime | None = None,
) -> CaseBrief:
    """Assemble the brief from normalized events plus the document catalog."""
    now = now or datetime.now(timezone.utc)
    document_texts = document_texts or {}

    intake: dict[str, str] = {}
    state = county = None
    for event in events:
        notes = event.facts.get("Intake notes")
        if isinstance(notes, str) and notes:
            intake.update(_parse_intake_notes(notes))
        form = event.facts.get("Intake form")
        if isinstance(form, dict):
            state = state or form.get("State") or form.get("State display text")
            county = county or form.get("County")

    jurisdiction = _court_from_documents(document_texts)
    if not jurisdiction:
        jurisdiction = ", ".join(str(p).title() for p in (county, state) if p) or None
    elif state and str(state).lower() not in jurisdiction.lower():
        jurisdiction = f"{jurisdiction} (intake recorded {str(state).title()})"

    court_number = _case_number_from_documents(document_texts) or intake.get("court_case_number")
    if not court_number:
        # The number arrives on an e-filing notification, where it sits in the
        # subject line as often as the body.
        for event in events:
            haystack = " ".join(
                [event.headline or "", event.body or "", str(event.facts.get("Subject") or "")]
            )
            if match := _COURT_NUMBER.search(haystack):
                court_number = match.group(1)

    children = []
    if raw := intake.get("children_raw"):
        children = [c.strip() for c in re.split(r"[;,]", raw) if c.strip()]

    status_event = None
    for event in events:
        if event.activity_type == "Case" and event.activity_sub_type == "Status_Change":
            status_event = event
    case_status = status_event.headline if status_event else None

    # "Case completed" means the *purchased service* was delivered, and in
    # case_001 five follow-on services open in the same minute. Reporting the
    # bare transition would tell an attorney the matter is over when it is
    # actively running, so the follow-on work is spelled out alongside it.
    status_detail = None
    if status_event and "completed" in (case_status or "").lower():
        # The follow-on services are written in the same batch as the
        # status row, so their timestamps straddle it by fractions of a
        # second; a small window catches all of them.
        cutoff = status_event.when - timedelta(seconds=60)
        opened = [
            str(e.facts.get("Service") or e.facts.get("Service type"))
            for e in events
            if e.activity_type == "Service"
            and "service created" in (e.headline or "").lower()
            and e.when >= cutoff
            and (e.facts.get("Service") or e.facts.get("Service type"))
        ]
        if opened:
            unique = sorted(set(opened))
            status_detail = (
                f"The initial service was delivered, but {len(unique)} follow-on "
                f"service(s) opened at the same time and are still active: "
                f"{', '.join(unique)}."
            )

    milestone, percent = _milestone(events)
    outstanding, completed = _requests(events)
    outstanding = outstanding + _open_tickets(events)

    upcoming = [
        {
            "ref": e.ref,
            "when": e.occurs_at.isoformat(),
            "what": e.headline,
            "note": "internal Marble meeting — not a court date",
        }
        for e in events
        if e.is_scheduled and e.occurs_at > now
    ]

    dated = [e.when for e in events]
    return CaseBrief(
        case_id=events[0].case_id if events else "",
        case_slug=case_slug,
        now=now,
        client_name=roster.client_name or intake.get("client_name"),
        opposing_party=intake.get("opposing_party"),
        children=children,
        jurisdiction=jurisdiction,
        matter_type=intake.get("matter_type"),
        court_case_number=court_number,
        case_status=case_status,
        status_detail=status_detail,
        services=_services(events),
        current_milestone=milestone,
        percent_complete=percent,
        contact=_contact_summary(events, now),
        deadlines=_deadlines(events, jurisdiction, now),
        outstanding_requests=outstanding,
        completed_requests=completed,
        upcoming_events=upcoming,
        event_count=len(events),
        document_count=sum(1 for c in catalog if c.disk_name),
        first_activity=min(dated) if dated else None,
        last_activity=max(dated) if dated else None,
    )
