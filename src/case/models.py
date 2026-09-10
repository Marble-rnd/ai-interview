"""
The case domain model.

One `NormalizedEvent` shape replaces 30+ raw CSV row shapes. Everything
downstream — retrieval, tools, the agent, the UI — reads these types and never
touches a raw row, so the rules in `case_data/TIMELINE-REFERENCE.md` are
enforced in exactly one place.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Side = Literal["staff", "client", "system", "external"]
Channel = Literal["email", "sms", "call", "chat", "portal", "document", "system"]
Direction = Literal["inbound", "outbound", "internal", "none"]


class Actor(BaseModel):
    """Who did this.

    `int_user_marble_id` / `ext_user_marble_id` are opaque but stable, so ids
    are clustered per case and rendered through `label`. Raw ids must never
    reach an answer.
    """

    actor_id: str | None = None
    side: Side = "system"
    role: str | None = None
    label: str = "the system"

    @property
    def is_human(self) -> bool:
        return self.side in ("staff", "client", "external")


class DocRef(BaseModel):
    """A document as the timeline describes it, plus where it actually lives.

    The `documents/` folder holds a subset of what was uploaded, under
    cleaned-up filenames, so `disk_path` is often None even when the upload
    genuinely happened. Tools must report that honestly rather than implying
    the file is missing from the case.
    """

    display_name: str
    file_name: str | None = None
    category: str | None = None
    who_uploaded: str | None = None
    summary: str | None = None
    file_size: int | None = None
    disk_path: str | None = None
    text_status: Literal["text", "scanned", "absent", "unsupported"] = "absent"
    page_count: int | None = None


class NormalizedEvent(BaseModel):
    """One thing that happened on the case."""

    # Identity. `raw_id` is the stable key used by the index and eval gold
    # sets; `ref` is the short human-readable handle used in citations.
    raw_id: str
    ref: str
    case_id: str

    # Time. `when` is activity_created_at, the chronological key. `occurs_at`
    # is start_date, which is in the future for scheduled events.
    when: datetime
    occurs_at: datetime
    ends_at: datetime | None = None

    activity_type: str
    activity_sub_type: str
    channel: Channel = "system"
    direction: Direction = "none"
    actor: Actor = Field(default_factory=Actor)

    # Automated marketing/templated traffic. Excluded from "when did we last
    # contact the client" — the difference between a real touchpoint and a
    # Braze blast.
    is_automated: bool = False

    # Text derived from a call transcript. Transcription errors mean names,
    # addresses and numbers from these events must never be quoted verbatim.
    low_fidelity: bool = False

    # A booking/scheduled event whose `occurs_at` is later than `when`.
    is_scheduled: bool = False

    # True when the raw row carried no usable text but the event still
    # happened (e.g. an untranscribed call with a real duration).
    has_silent_body: bool = False

    headline: str
    body: str | None = None
    facts: dict[str, Any] = Field(default_factory=dict)
    doc_ref: DocRef | None = None

    # Set during ingest enrichment; prepended to chunks for BM25 + embedding.
    context_blurb: str | None = None

    @property
    def duration_seconds(self) -> float | None:
        if self.ends_at is None:
            return None
        return max(0.0, (self.ends_at - self.occurs_at).total_seconds())

    def searchable_text(self) -> str:
        """The text the sparse and dense indexes see."""
        parts = [self.context_blurb, self.headline, self.body]
        parts += [f"{k}: {v}" for k, v in self.facts.items() if v not in (None, "", [], {})]
        if self.doc_ref:
            parts += [self.doc_ref.display_name, self.doc_ref.category, self.doc_ref.summary]
        return "\n".join(str(p) for p in parts if p)

    def digest_line(self) -> str:
        """One line for the always-resident timeline skeleton."""
        flags = []
        if self.is_automated:
            flags.append("auto")
        if self.is_scheduled:
            flags.append("scheduled")
        if self.doc_ref:
            flags.append("doc")
        if self.low_fidelity:
            flags.append("transcript")
        stamp = self.when.strftime("%Y-%m-%d %H:%M")
        kind = f"{self.activity_type}/{self.activity_sub_type}"
        head = " ".join(self.headline.split())[:110]
        tail = f" [{','.join(flags)}]" if flags else ""
        return f"{self.ref} | {stamp} | {kind} | {self.actor.label} | {head}{tail}"


class Chunk(BaseModel):
    """A retrievable unit: a whole short event, a slice of a long transcript,
    or a page-range of a document."""

    chunk_id: str
    case_id: str
    source: Literal["event", "document"]

    # Provenance — exactly one of these paths is populated.
    event_ref: str | None = None
    event_raw_id: str | None = None
    document_name: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    part: int = 0
    part_count: int = 1

    when: datetime | None = None
    title: str
    text: str
    context_blurb: str | None = None
    low_fidelity: bool = False

    def indexed_text(self) -> str:
        """Contextual Retrieval: the blurb is prepended before indexing so a
        chunk carries its situating context into both BM25 and the embedding."""
        return f"{self.context_blurb}\n\n{self.text}" if self.context_blurb else self.text

    def citation_label(self) -> str:
        if self.source == "document":
            pages = ""
            if self.page_start is not None:
                pages = (
                    f", p. {self.page_start}"
                    if self.page_end in (None, self.page_start)
                    else f", pp. {self.page_start}–{self.page_end}"
                )
            return f"{self.document_name}{pages}"
        stamp = self.when.strftime("%Y-%m-%d") if self.when else "undated"
        part = f" ({self.part + 1}/{self.part_count})" if self.part_count > 1 else ""
        return f"{self.title} — {stamp}{part}"


class Deadline(BaseModel):
    """A computed date obligation. Only ever derived from evidence on the
    case; never invented, and never sourced from a Calendar Event."""

    name: str
    due: datetime | None = None
    basis: str
    basis_refs: list[str] = Field(default_factory=list)
    status: Literal["running", "elapsed", "met", "unknown"] = "unknown"
    days_remaining: int | None = None


class ContactSummary(BaseModel):
    last_inbound: datetime | None = None
    last_inbound_ref: str | None = None
    last_outbound_human: datetime | None = None
    last_outbound_human_ref: str | None = None
    days_since_client_spoke: int | None = None
    days_since_we_reached_out: int | None = None
    unanswered_outbound_streak: int = 0


class CaseBrief(BaseModel):
    """Everything an attorney wants before asking a question.

    Computed deterministically at ingest so status questions are answered from
    arithmetic, not from the model's reading of 200 rows.
    """

    case_id: str
    case_slug: str
    now: datetime

    client_name: str | None = None
    opposing_party: str | None = None
    children: list[str] = Field(default_factory=list)
    jurisdiction: str | None = None
    matter_type: str | None = None
    court_case_number: str | None = None

    case_status: str | None = None
    # Context that stops a bare status transition being read as the whole
    # truth (e.g. "Case completed" while five services are still open).
    status_detail: str | None = None
    services: list[dict[str, Any]] = Field(default_factory=list)
    current_milestone: str | None = None
    percent_complete: int | None = None

    contact: ContactSummary = Field(default_factory=ContactSummary)
    deadlines: list[Deadline] = Field(default_factory=list)
    outstanding_requests: list[dict[str, Any]] = Field(default_factory=list)
    completed_requests: list[dict[str, Any]] = Field(default_factory=list)
    upcoming_events: list[dict[str, Any]] = Field(default_factory=list)

    event_count: int = 0
    document_count: int = 0
    first_activity: datetime | None = None
    last_activity: datetime | None = None


class EvidenceItem(BaseModel):
    """A retrieved chunk as it appears in the citation pack shown to the user."""

    chunk_id: str
    ref: str
    label: str
    source: Literal["event", "document"]
    when: datetime | None = None
    excerpt: str
    event_raw_id: str | None = None
    document_name: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    low_fidelity: bool = False
    retrieved_by: list[str] = Field(default_factory=list)
    score: float | None = None
