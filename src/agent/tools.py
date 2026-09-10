"""
The tool surface the agent works through.

Eight tools, constructed **bound to one case artifact**. There is no case
argument on any of them, so cross-case leakage is structurally impossible
rather than something a guardrail has to catch.

Two design choices matter here:

* **Filters and search are different tools.** "All inbound SMS in the last
  two weeks" is an exact predicate; routing it through a ranker turns a
  complete answer into a plausible one. The descriptions steer the model to
  `get_events` for anything expressible as a filter and reserve `search_case`
  for genuine keyword/semantic lookup.
* **The tool list is static.** Prompt caching matches on a prefix rendered as
  `tools → system → messages`, so narrowing the tool set per question would
  invalidate the cached prefix on every turn — far more expensive than the
  handful of unused schemas it would save.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import config
from ..case.documents import CatalogEntry, DocumentText, extract
from ..case.models import CaseBrief, Chunk, EvidenceItem, NormalizedEvent
from ..case.retrieval import Filters, Hit, Retriever, dedupe_by_event
from .llm import LLM
from .rerank import rerank

MAX_EXCERPT = 1200
MAX_ROWS = 60


# --------------------------------------------------------------------------
# Schema helpers
# --------------------------------------------------------------------------


def _optional(kind: str, description: str, **extra: Any) -> dict[str, Any]:
    """An optional tool parameter.

    These schemas deliberately do not use `strict: true`. Strict mode requires
    every property to appear in `required`, so optional filters have to be
    expressed as explicit `["string", "null"]` unions — and this tool surface
    has twenty of them, which the API rejects outright:

        Schemas contains too many parameters with union types (20 ...).
        Reduce the number of nullable or union-typed parameters (limit: 16).

    Plain optional parameters are also easier for the model to read than a
    wall of null unions. Argument hygiene is enforced in `dispatch` instead:
    nulls are dropped, types are coerced, and every handler tolerates a
    missing key.
    """
    return {"type": kind, "description": description, **extra}


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(required),
        },
    }


_DATE = "ISO-8601 date or datetime, e.g. 2026-08-27 or 2026-08-27T14:00:00Z. Omit for no bound."

TOOL_SCHEMAS: list[dict[str, Any]] = [
    _tool(
        "get_case_brief",
        "The precomputed case brief: parties, jurisdiction, court case number, matter "
        "status, services, computed deadlines, outstanding items, upcoming internal "
        "meetings, and how long since each side last made contact. Start here for any "
        "status, progress, deadline or 'what are we waiting on' question — it is "
        "computed deterministically and is cheaper and more reliable than searching.",
        {},
    ),
    _tool(
        "get_events",
        "Filter the timeline exactly. Use this whenever the question can be expressed "
        "as a predicate — a date range ('the last two weeks'), a channel ('emails'), a "
        "direction ('what did the client send us'), or an activity type. Returns a "
        "complete, chronologically ordered slice, so it is the correct tool for "
        "summarising a period or listing every instance of something. Prefer it over "
        "search_case whenever both would work.",
        {
            "activity_type": _optional(
                "string",
                "Exact activity_type, e.g. Communications, Documents, Serving, Service, "
                "Data Requests, Case Update, Calendar Event.",
            ),
            "activity_sub_type": _optional(
                "string", "Exact activity_sub_type, e.g. Email, SMS, Call, Document_Upload."
            ),
            "channel": _optional(
                "string", "One of: email, sms, call, chat, portal, document, system."
            ),
            "direction": _optional(
                "string",
                "inbound (client to us), outbound (us to client), internal, or none.",
            ),
            "date_from": _optional("string", _DATE),
            "date_to": _optional("string", _DATE),
            "exclude_automated": _optional(
                "boolean",
                "True drops templated SMS, Braze marketing mail and system "
                "notifications, leaving only messages a person actually sent. Use this "
                "for any question about real contact with the client.",
            ),
            "limit": _optional("integer", "Max rows, default 25, cap 60."),
            "order": _optional("string", "'asc' (oldest first) or 'desc'. Default desc."),
        },
    ),
    _tool(
        "search_case",
        "Hybrid keyword + semantic search across timeline events and the full text of "
        "every document in the case, including pages recovered by OCR from scanned "
        "filings. Use it when you need to find something by wording or subject rather "
        "than by a filter — a topic, a name, a form number, an amount.",
        {
            "query": {"type": "string", "description": "What to look for, in natural language."},
            "k": _optional("integer", "Number of results, default 8, cap 20."),
            "source": _optional(
                "string", "'event', 'document' or 'any' (default) to restrict the corpus."
            ),
            "date_from": _optional("string", _DATE),
            "date_to": _optional("string", _DATE),
            "exclude_automated": _optional("boolean", "Drop automated traffic. Default false."),
        },
        required=["query"],
    ),
    _tool(
        "get_event_detail",
        "The full record for one timeline event: complete message body or transcript, "
        "every structured field, actor and timing. Use after get_events or search_case "
        "when a summary line is not enough.",
        {"ref": {"type": "string", "description": "Event reference such as E142."}},
        required=["ref"],
    ),
    _tool(
        "list_documents",
        "Every document on the case. Reports for each one whether its text is "
        "available, whether it had to be recovered by OCR, and — importantly — whether "
        "the timeline records it as uploaded but the file is not present in this "
        "snapshot. Never claim to have read a document listed as absent.",
        {
            "category": _optional(
                "string", "Filter by category, e.g. Financials, Initial Draft, Legal."
            )
        },
    ),
    _tool(
        "read_document",
        "Read a document. With a query, returns the passages that answer it, each "
        "tagged with its page number; with a page range, returns those pages verbatim. "
        "Documents are never returned whole — one file here is 85 pages — so ask a "
        "question of it or name the pages you want.",
        {
            "name": {"type": "string", "description": "Document filename from list_documents."},
            "query": _optional("string", "What to look for inside the document."),
            "page_from": _optional("integer", "First page (1-indexed)."),
            "page_to": _optional("integer", "Last page, inclusive."),
        },
        required=["name"],
    ),
    _tool(
        "get_contact_history",
        "Genuine back-and-forth with the client, automated traffic removed, newest "
        "first, with the gap in days between each exchange. Use for 'when did we last "
        "hear from them', 'have they responded', or 'has this gone quiet'.",
        {
            "limit": _optional("integer", "How many exchanges, default 15."),
            "include_automated": _optional(
                "boolean", "True also shows templated and system messages. Default false."
            ),
        },
    ),
    _tool(
        "get_outstanding_items",
        "What the case is still waiting on: document requests that are still pending, "
        "unresolved support tickets, and scheduled internal meetings. Requests that "
        "were cancelled are reported separately — the team withdrew them, so they are "
        "not something the client still owes.",
        {},
    ),
]


# --------------------------------------------------------------------------
# Rendering helpers
# --------------------------------------------------------------------------


def _clip(text: str, limit: int = MAX_EXCERPT) -> str:
    """Trim to `limit`, at word boundaries on both ends.

    Chunk overlap means a passage often starts mid-word ("er ____ Signature
    Desmond Fairweather"), which reads like a bug in a source card.
    """
    text = text.strip()
    if text and not text[0].isspace():
        head, _, rest = text.partition(" ")
        # Only a fragment — lowercase, no sentence punctuation — is dropped.
        if rest and head.islower() and len(head) <= 3 and not head.endswith((".", ":", ")")):
            text = rest.lstrip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _parse_date(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _event_line(event: NormalizedEvent) -> str:
    stamp = event.when.strftime("%Y-%m-%d %H:%M")
    bits = [f"[{event.ref}] {stamp} · {event.activity_type}/{event.activity_sub_type}"]
    if event.direction != "none":
        bits.append(event.direction)
    bits.append(f"by {event.actor.label}")
    if event.is_automated:
        bits.append("(automated)")
    return " · ".join(bits) + f"\n  {event.headline}"


def _event_detail(event: NormalizedEvent) -> str:
    lines = [_event_line(event)]
    if event.is_scheduled:
        lines.append(
            f"  Scheduled for: {event.occurs_at.isoformat()} "
            "(internal Marble meeting — not a court date)"
        )
    if event.has_silent_body:
        lines.append("  No transcript was captured, but the call did take place.")
    if event.low_fidelity:
        lines.append(
            "  Source is a call transcript: the gist is reliable, but names, "
            "addresses and numbers in it may be mis-transcribed — do not quote them."
        )
    if event.facts:
        rendered = json.dumps(event.facts, default=str, ensure_ascii=False)
        lines.append(f"  Details: {_clip(rendered, 2000)}")
    if event.body:
        lines.append(f"  Content:\n{_clip(event.body, 4000)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# The bound tool set
# --------------------------------------------------------------------------


class CaseTools:
    """Tools for exactly one case. Construct per conversation."""

    def __init__(
        self,
        *,
        case_slug: str,
        brief: CaseBrief,
        events: Sequence[NormalizedEvent],
        chunks: Sequence[Chunk],
        catalog: Sequence[CatalogEntry],
        retriever: Retriever,
        llm: LLM,
        rerank_enabled: bool = True,
        now: datetime | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.case_slug = case_slug
        self.brief = brief
        self.events = list(events)
        self.by_ref = {e.ref: e for e in self.events}
        self.chunks = list(chunks)
        self.catalog = list(catalog)
        self.retriever = retriever
        self.llm = llm
        self.rerank_enabled = rerank_enabled
        self.now = now or config.now()
        self.on_event = on_event

        # Provenance for everything any tool has surfaced this turn, keyed so
        # a retry cannot double-count the same chunk.
        self.evidence: dict[str, EvidenceItem] = {}
        self.calls: list[dict[str, Any]] = []
        self._doc_cache: dict[str, DocumentText] = {}

    # -- evidence ---------------------------------------------------------

    def _record_hit(self, hit: Hit, tool: str) -> EvidenceItem:
        chunk = hit.chunk
        existing = self.evidence.get(chunk.chunk_id)
        if existing:
            if tool not in existing.retrieved_by:
                existing.retrieved_by.append(tool)
            return existing
        item = EvidenceItem(
            chunk_id=chunk.chunk_id,
            ref=chunk.event_ref or chunk.document_name or chunk.chunk_id,
            label=chunk.citation_label(),
            source=chunk.source,
            when=chunk.when,
            excerpt=_clip(chunk.text),
            event_raw_id=chunk.event_raw_id,
            document_name=chunk.document_name,
            page_start=chunk.page_start,
            page_end=chunk.page_end,
            low_fidelity=chunk.low_fidelity,
            retrieved_by=[tool],
            score=hit.score,
        )
        self.evidence[chunk.chunk_id] = item
        return item

    def _record_event(self, event: NormalizedEvent, tool: str) -> None:
        key = f"event:{event.ref}"
        existing = self.evidence.get(key)
        if existing:
            if tool not in existing.retrieved_by:
                existing.retrieved_by.append(tool)
            return
        # Compose the same text the index sees. `Data Entries` and `Serving`
        # rows have a generic body and carry everything in `facts`, so an
        # excerpt built from the body alone would omit the very fact the
        # answer cites.
        parts = [event.headline]
        if event.body:
            parts.append(event.body)
        parts += [f"{k}: {v}" for k, v in event.facts.items() if v not in (None, "", [], {})]

        self.evidence[key] = EvidenceItem(
            chunk_id=key,
            ref=event.ref,
            label=f"{event.headline} — {event.when.strftime('%Y-%m-%d')}",
            source="event",
            when=event.when,
            excerpt=_clip("\n".join(str(p) for p in parts)),
            event_raw_id=event.raw_id,
            low_fidelity=event.low_fidelity,
            retrieved_by=[tool],
        )

    # -- tools ------------------------------------------------------------

    def get_case_brief(self) -> str:
        brief = self.brief
        lines = [
            f"CASE BRIEF — {brief.case_slug} (as of {self.now:%Y-%m-%d})",
            f"Client: {brief.client_name or 'unknown'} · Opposing party: "
            f"{brief.opposing_party or 'unknown'}",
            f"Matter: {brief.matter_type or 'unknown'} · Jurisdiction: "
            f"{brief.jurisdiction or 'unknown'} · Court case no.: "
            f"{brief.court_case_number or 'not yet on record'}",
        ]
        if brief.children:
            lines.append(f"Children: {', '.join(brief.children)}")
        lines.append(f"Case status: {brief.case_status or 'unknown'}")
        if brief.status_detail:
            lines.append(f"  Note: {brief.status_detail}")
        if brief.current_milestone:
            lines.append(
                f"Current milestone: {brief.current_milestone}"
                + (f" ({brief.percent_complete}% of service)" if brief.percent_complete else "")
            )
        if brief.services:
            lines.append("Services on the case:")
            lines += [f"  - {s['name']}: {s.get('last_action', '')}" for s in brief.services]

        contact = brief.contact
        lines.append("Contact (human messages only, automation excluded):")
        lines.append(
            f"  Client last contacted us: "
            f"{contact.last_inbound:%Y-%m-%d %H:%M} ({contact.days_since_client_spoke}d ago, "
            f"{contact.last_inbound_ref})"
            if contact.last_inbound
            else "  Client has never contacted us."
        )
        lines.append(
            f"  We last contacted the client: "
            f"{contact.last_outbound_human:%Y-%m-%d %H:%M} "
            f"({contact.days_since_we_reached_out}d ago, {contact.last_outbound_human_ref})"
            if contact.last_outbound_human
            else "  We have never contacted the client."
        )
        if contact.unanswered_outbound_streak:
            lines.append(
                f"  Unanswered outbound messages since their last reply: "
                f"{contact.unanswered_outbound_streak}"
            )

        if brief.deadlines:
            lines.append("Computed deadlines:")
            for deadline in brief.deadlines:
                due = f"{deadline.due:%Y-%m-%d}" if deadline.due else "unknown"
                remaining = (
                    f", {deadline.days_remaining}d remaining"
                    if deadline.days_remaining is not None
                    else ""
                )
                lines.append(f"  - {deadline.name}: due {due} [{deadline.status}{remaining}]")
                lines.append(f"    Basis: {deadline.basis} (from {', '.join(deadline.basis_refs)})")
        else:
            lines.append("Computed deadlines: none derivable from the record.")

        if brief.outstanding_requests:
            lines.append("Outstanding:")
            lines += [
                f"  - {r['item']} [{r['status']}, {r['ref']}]" for r in brief.outstanding_requests
            ]
        else:
            lines.append("Outstanding: nothing pending on record.")

        if brief.upcoming_events:
            lines.append("Upcoming scheduled items (internal Marble meetings, NOT court dates):")
            lines += [
                f"  - {e['when'][:16]} {e['what']} [{e['ref']}]" for e in brief.upcoming_events
            ]
        else:
            lines.append("Upcoming scheduled items: none.")

        lines.append(
            f"Volume: {brief.event_count} timeline events, {brief.document_count} documents "
            f"on file; activity from {brief.first_activity:%Y-%m-%d} to "
            f"{brief.last_activity:%Y-%m-%d}."
        )
        lines.append(
            "No court hearing or trial date appears anywhere in this record. "
            "Calendar entries are internal Marble calls."
        )
        return "\n".join(lines)

    def get_events(self, **kwargs: Any) -> str:
        limit = min(int(kwargs.get("limit") or 25), MAX_ROWS)
        order = (kwargs.get("order") or "desc").lower()
        date_from, date_to = (
            _parse_date(kwargs.get("date_from")),
            _parse_date(kwargs.get("date_to")),
        )
        exclude_automated = bool(kwargs.get("exclude_automated"))

        selected = []
        for event in self.events:
            if (t := kwargs.get("activity_type")) and event.activity_type.lower() != t.lower():
                continue
            if (
                s := kwargs.get("activity_sub_type")
            ) and event.activity_sub_type.lower() != s.lower():
                continue
            if (c := kwargs.get("channel")) and event.channel != c.lower():
                continue
            if (d := kwargs.get("direction")) and event.direction != d.lower():
                continue
            if date_from and event.when < date_from:
                continue
            if date_to and event.when > date_to:
                continue
            if exclude_automated and event.is_automated:
                continue
            selected.append(event)

        total = len(selected)
        ordered = selected if order == "asc" else list(reversed(selected))
        shown = ordered[:limit]
        for event in shown:
            self._record_event(event, "get_events")

        if not shown:
            return "No events match those filters. The filters were applied exactly, so this is a real absence, not a search miss."
        header = f"{total} event(s) match; showing {len(shown)} ({order}):"
        return header + "\n\n" + "\n\n".join(_event_line(e) for e in shown)

    def search_case(self, **kwargs: Any) -> str:
        query = str(kwargs.get("query") or "").strip()
        if not query:
            return "search_case needs a query."
        k = min(int(kwargs.get("k") or 8), 20)
        filters = Filters(
            source=(kwargs.get("source") or "any"),
            date_from=_parse_date(kwargs.get("date_from")),
            date_to=_parse_date(kwargs.get("date_to")),
            exclude_automated=bool(kwargs.get("exclude_automated")),
        )
        mode = "hybrid_rerank" if self.rerank_enabled else "hybrid"
        hits = self.retriever.search(query, k=k, mode=mode, filters=filters)
        if mode == "hybrid_rerank":
            hits = rerank(query, hits, self.llm, top_k=k)
        hits = dedupe_by_event(hits, k)

        if not hits:
            return f"Nothing in this case matches '{query}'."

        blocks = []
        for hit in hits:
            item = self._record_hit(hit, "search_case")
            chunk = hit.chunk
            tag = f"[{chunk.event_ref}]" if chunk.event_ref else f"[doc:{chunk.document_name}]"
            stamp = f"{chunk.when:%Y-%m-%d}" if chunk.when else "undated"
            caution = (
                "\n  (call transcript — do not quote names/addresses verbatim)"
                if chunk.low_fidelity
                else ""
            )
            blocks.append(f"{tag} {stamp} · {item.label}{caution}\n{_clip(chunk.text, 900)}")
        return f"{len(blocks)} result(s) for '{query}':\n\n" + "\n\n".join(blocks)

    def get_event_detail(self, **kwargs: Any) -> str:
        ref = str(kwargs.get("ref") or "").strip().upper()
        event = self.by_ref.get(ref)
        if event is None:
            return f"No event {ref} on this case. References look like E042."
        self._record_event(event, "get_event_detail")
        return _event_detail(event)

    def list_documents(self, **kwargs: Any) -> str:
        category = (kwargs.get("category") or "").strip().lower()
        entries = [
            e for e in self.catalog if not category or category in (e.category or "").lower()
        ]
        if not entries:
            return "No documents match."

        present = [e for e in entries if e.disk_name]
        absent = [e for e in entries if not e.disk_name]
        lines = [f"{len(present)} document(s) available to read:"]
        for entry in present:
            note = (
                " (text recovered by OCR — scanned original)"
                if entry.text_status == "scanned"
                else ""
            )
            pages = f", {entry.page_count}p" if entry.page_count else ""
            lines.append(
                f"  - {entry.disk_name}{pages}{note}\n"
                f"      {entry.display_name}"
                + (f" · {entry.category}" if entry.category else "")
                + (f" · uploaded by {entry.who_uploaded}" if entry.who_uploaded else "")
                + (f" · {entry.uploaded_at[:10]}" if entry.uploaded_at else "")
            )
        if absent:
            lines.append(
                f"\n{len(absent)} document(s) the timeline records as uploaded but which are "
                "NOT included in this snapshot — they exist on the case, but their contents "
                "cannot be read here:"
            )
            for entry in absent:
                lines.append(
                    f"  - {entry.timeline_file_name or entry.display_name}"
                    + (f" · {entry.category}" if entry.category else "")
                    + (f" · uploaded by {entry.who_uploaded}" if entry.who_uploaded else "")
                )
        return "\n".join(lines)

    def _document_text(self, entry: CatalogEntry) -> DocumentText | None:
        assert entry.disk_name
        if entry.disk_name in self._doc_cache:
            return self._doc_cache[entry.disk_name]
        # Scanned documents were OCR'd at ingest and live in the chunk index;
        # re-extracting here would return blank pages.
        if entry.text_status == "scanned":
            return None
        path = Path(config.case_dir(self.case_slug)) / "documents" / entry.disk_name
        try:
            text = extract(path)
        except Exception:
            return None
        self._doc_cache[entry.disk_name] = text
        return text

    def read_document(self, **kwargs: Any) -> str:
        name = str(kwargs.get("name") or "").strip()
        entry = next(
            (
                e
                for e in self.catalog
                if e.disk_name == name
                or (e.disk_name or "").lower() == name.lower()
                or (e.timeline_file_name or "").lower() == name.lower()
                or e.display_name.lower() == name.lower()
            ),
            None,
        )
        if entry is None:
            available = ", ".join(e.disk_name for e in self.catalog if e.disk_name)
            return f"No document named '{name}'. Available: {available}"
        if not entry.disk_name:
            return (
                f"'{entry.timeline_file_name or entry.display_name}' is recorded on the "
                "timeline as uploaded, but the file is not included in this case snapshot, "
                "so its contents cannot be read. Say so rather than inferring what it says."
            )

        page_from, page_to = kwargs.get("page_from"), kwargs.get("page_to")
        if page_from:
            text = self._document_text(entry)
            if text is None:
                # Fall back to the OCR'd chunks for scanned documents.
                pages = [
                    c
                    for c in self.chunks
                    if c.document_name == entry.disk_name
                    and c.page_start is not None
                    and c.page_start >= int(page_from)
                    and c.page_start <= int(page_to or page_from)
                ]
                if not pages:
                    return f"No text available for those pages of {entry.disk_name}."
                for chunk in pages:
                    self._record_hit(Hit(chunk=chunk, score=1.0), "read_document")
                return (
                    f"{entry.disk_name}, pages {page_from}–{page_to or page_from}:\n\n"
                    + "\n\n".join(f"[p. {c.page_start}] {c.text}" for c in pages)
                )
            selected = [
                p for p in text.pages if int(page_from) <= p.page <= int(page_to or page_from)
            ]
            if not selected:
                return (
                    f"{entry.disk_name} has {len(text.pages)} page(s); that range is out of bounds."
                )
            body = "\n\n".join(f"[p. {p.page}]\n{p.text}" for p in selected if p.text)
            return f"{entry.disk_name}, pages {page_from}–{page_to or page_from}:\n\n{_clip(body, 12000)}"

        query = str(kwargs.get("query") or "").strip()
        if not query:
            query = entry.display_name
        hits = self.retriever.search(
            query,
            k=6,
            mode="hybrid",
            filters=Filters(source="document", document_names=[entry.disk_name]),
        )
        if not hits:
            return (
                f"Nothing in {entry.disk_name} matches '{query}'. "
                f"The document has {entry.page_count or '?'} page(s); "
                "you can request a page range instead."
            )
        blocks = []
        for hit in hits:
            self._record_hit(hit, "read_document")
            pages = (
                f"p. {hit.chunk.page_start}"
                if hit.chunk.page_end in (None, hit.chunk.page_start)
                else f"pp. {hit.chunk.page_start}–{hit.chunk.page_end}"
            )
            blocks.append(f"[{entry.disk_name}, {pages}]\n{_clip(hit.chunk.text, 1500)}")
        source = " (text recovered by OCR)" if entry.text_status == "scanned" else ""
        return f"Passages from {entry.disk_name}{source} matching '{query}':\n\n" + "\n\n".join(
            blocks
        )

    def get_contact_history(self, **kwargs: Any) -> str:
        limit = min(int(kwargs.get("limit") or 15), MAX_ROWS)
        include_automated = bool(kwargs.get("include_automated"))
        conversation = [
            e
            for e in self.events
            if e.channel in ("email", "sms", "call", "chat")
            and (include_automated or not e.is_automated)
        ]
        if not conversation:
            return "No contact of that kind on record."

        recent = conversation[-limit:]
        lines = []
        previous: datetime | None = None
        for event in recent:
            gap = f" (+{(event.when - previous).days}d)" if previous else ""
            previous = event.when
            who = (
                "client → us"
                if event.direction == "inbound"
                else ("us → client" if event.direction == "outbound" else event.direction)
            )
            flag = " [automated]" if event.is_automated else ""
            lines.append(
                f"[{event.ref}] {event.when:%Y-%m-%d %H:%M}{gap} · {event.channel} · {who} · "
                f"{event.actor.label}{flag}\n  {event.headline}"
            )
            self._record_event(event, "get_contact_history")

        contact = self.brief.contact
        summary = [
            f"{len(conversation)} exchange(s) on record; showing the last {len(recent)}, "
            "oldest first."
        ]
        if not include_automated:
            summary.append("Automated and templated messages are excluded.")
        if contact.days_since_client_spoke is not None:
            summary.append(
                f"The client last reached out {contact.days_since_client_spoke} day(s) ago; "
                f"we last reached out {contact.days_since_we_reached_out} day(s) ago."
            )
        return " ".join(summary) + "\n\n" + "\n\n".join(lines)

    def get_outstanding_items(self) -> str:
        brief = self.brief
        lines = []
        if brief.outstanding_requests:
            lines.append("Still outstanding:")
            lines += [
                f"  - {r['item']} — {r['status']} (as of {r['as_of'][:10]}, {r['ref']})"
                for r in brief.outstanding_requests
            ]
        else:
            lines.append("Nothing is recorded as outstanding.")

        cancelled = [r for r in brief.completed_requests if r["status"].lower() == "canceled"]
        done = [r for r in brief.completed_requests if r["status"].lower() != "canceled"]
        if done:
            lines.append("\nCompleted:")
            lines += [f"  - {r['item']} ({r['as_of'][:10]}, {r['ref']})" for r in done]
        if cancelled:
            lines.append("\nCancelled by the team — the client does NOT still owe these:")
            lines += [f"  - {r['item']} ({r['as_of'][:10]}, {r['ref']})" for r in cancelled]

        if brief.upcoming_events:
            lines.append("\nScheduled ahead (internal Marble meetings, not court dates):")
            lines += [
                f"  - {e['when'][:16]} {e['what']} [{e['ref']}]" for e in brief.upcoming_events
            ]
        for deadline in brief.deadlines:
            if deadline.status == "running" and deadline.due:
                lines.append(
                    f"\nDeadline running: {deadline.name} due {deadline.due:%Y-%m-%d} "
                    f"({deadline.days_remaining}d). {deadline.basis}"
                )
        return "\n".join(lines)

    # -- dispatch ---------------------------------------------------------

    @property
    def _handlers(self) -> dict[str, Callable[..., str]]:
        return {
            "get_case_brief": lambda **_: self.get_case_brief(),
            "get_events": self.get_events,
            "search_case": self.search_case,
            "get_event_detail": self.get_event_detail,
            "list_documents": self.list_documents,
            "read_document": self.read_document,
            "get_contact_history": self.get_contact_history,
            "get_outstanding_items": lambda **_: self.get_outstanding_items(),
        }

    def dispatch(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """Run a tool. Returns (result text, is_error).

        Errors come back as text with `is_error` set rather than raising: the
        model can recover from "no such document" far better than the loop can
        recover from an exception.
        """
        handler = self._handlers.get(name)
        if handler is None:
            return f"Unknown tool '{name}'.", True
        if self.on_event:
            # Progress reporting is best-effort; a dropped UI connection must
            # not abort the tool call.
            with contextlib.suppress(Exception):
                self.on_event("tool", {"tool": name, "arguments": arguments or {}})
        # Null-valued optional parameters are dropped so handlers see absence
        # rather than None sentinels.
        cleaned = {k: v for k, v in (arguments or {}).items() if v is not None}
        started = datetime.now(timezone.utc)
        try:
            result = handler(**cleaned)
            error = False
        except Exception as exc:
            result, error = f"{type(exc).__name__}: {exc}", True
        self.calls.append(
            {
                "tool": name,
                "arguments": cleaned,
                "latency_ms": round(
                    (datetime.now(timezone.utc) - started).total_seconds() * 1000, 1
                ),
                "result_chars": len(result),
                "error": error,
            }
        )
        return result, error

    def evidence_pack(self) -> list[EvidenceItem]:
        return sorted(self.evidence.values(), key=lambda e: (e.when is None, e.when))
