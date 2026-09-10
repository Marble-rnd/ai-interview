"""
Resolve opaque user ids to people.

`int_user_marble_id` / `ext_user_marble_id` are meaningless strings, but the
data dictionary guarantees they are *stable* — the same id is the same person
across the whole case. Clustering them once at ingest lets every answer say
"Dana Whitfield (attorney)" instead of leaking a UUID into legal prose.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

from pydantic import BaseModel, Field

from ..data_loader import parse_extra_details
from .models import NormalizedEvent

# `sender_role` records the role of whichever party the row is *about*, so a
# staff member's rows sometimes carry "Client". Role voting ignores it unless
# it is the only signal present.
_AMBIGUOUS_ROLES = {"client", "external user", "system", "automatic"}

_ROLE_ORDER = ["Attorney", "Paralegal", "Admin"]

_EMAIL_FIELDS = ("from_email", "int_user_email", "attorneyEmail")
_NAME_FIELDS = ("attorneyName",)


class Person(BaseModel):
    actor_id: str
    side: str = "staff"
    role: str | None = None
    name: str | None = None
    email: str | None = None
    event_count: int = 0
    label: str = "a staff member"


class Roster(BaseModel):
    people: dict[str, Person] = Field(default_factory=dict)
    client_name: str | None = None
    attorney_name: str | None = None

    def label_for(self, actor_id: str | None, fallback: str) -> str:
        person = self.people.get(actor_id or "")
        return person.label if person else fallback


def _name_from_email(email: str) -> str | None:
    """`quinn.h@themarbleway.com` → `Quinn H.`

    Marble proxy addresses append digits (`dana.wh9273@...`), which are
    stripped before the local part is titled.
    """
    local = email.split("@", 1)[0]
    local = re.sub(r"\d+$", "", local)
    parts = [p for p in re.split(r"[._-]+", local) if p]
    if not parts:
        return None
    rendered = [p.capitalize() if len(p) > 1 else f"{p.upper()}." for p in parts]
    return " ".join(rendered) or None


def _vote_role(roles: Counter) -> str | None:
    """Pick a person's role, preferring an unambiguous signal."""
    strong = Counter({r: n for r, n in roles.items() if r.lower() not in _AMBIGUOUS_ROLES})
    pool = strong or roles
    if not pool:
        return None
    # Seniority breaks ties so a mixed Attorney/Admin signal reads "Attorney".
    best = max(pool.items(), key=lambda kv: (kv[1], -_rank(kv[0])))
    return best[0]


def _rank(role: str) -> int:
    for i, known in enumerate(_ROLE_ORDER):
        if role.lower() == known.lower():
            return i
    return len(_ROLE_ORDER)


def build_roster(rows: list[dict], events: list[NormalizedEvent]) -> Roster:
    """Cluster ids into people, using role votes plus any name/email evidence."""
    roles: dict[str, Counter] = defaultdict(Counter)
    emails: dict[str, Counter] = defaultdict(Counter)
    counts: Counter = Counter()

    attorney_name: str | None = None
    client_bits: dict[str, Any] = {}

    for row in rows:
        extra = parse_extra_details(row.get("extra_details"))
        internal = (row.get("int_user_marble_id") or "").strip()

        points = extra.get("data_points")
        if isinstance(points, dict):
            for field in _NAME_FIELDS:
                if isinstance(points.get(field), str) and points[field].strip():
                    attorney_name = points[field].strip()
            for field in ("firstName", "lastName"):
                if isinstance(points.get(field), str) and points[field].strip():
                    client_bits[field] = points[field].strip()

        # Not every case fills the questionnaire name fields (case_001 does
        # not), so fall back to the intake record, which always has them.
        funnel = extra.get("funnel_parameters")
        if isinstance(funnel, dict):
            for src, dst in (("firstname", "firstName"), ("lastname", "lastName")):
                value = funnel.get(src)
                if isinstance(value, str) and value.strip():
                    client_bits.setdefault(dst, value.strip())
        notes = extra.get("intake_notes")
        if isinstance(notes, str) and "full_name" not in client_bits:
            match = re.search(r"^Name:\s*(.+)$", notes, re.M)
            if match and match.group(1).strip():
                client_bits["full_name"] = match.group(1).strip()

        if not internal:
            continue
        counts[internal] += 1
        for field in ("sender_role", "created_by_role", "owner_role"):
            value = extra.get(field)
            if isinstance(value, str) and value.strip():
                roles[internal][value.strip()] += 1
        # Only trust the sender address when this row was actually sent by
        # the internal user, not merely associated with them.
        if extra.get("sender_role") not in (None, "Client", "System"):
            for field in _EMAIL_FIELDS:
                value = extra.get(field)
                if isinstance(value, str) and "@" in value:
                    emails[internal][value] += 1

    client_name = (
        " ".join(p for p in (client_bits.get("firstName"), client_bits.get("lastName")) if p)
        or client_bits.get("full_name")
        or None
    )

    people: dict[str, Person] = {}
    for actor_id, count in counts.items():
        role = _vote_role(roles[actor_id])
        email = emails[actor_id].most_common(1)[0][0] if emails[actor_id] else None
        name = None
        if role and role.lower() == "attorney" and attorney_name:
            name = attorney_name
        elif email:
            name = _name_from_email(email)

        if name and role:
            label = f"{name} ({role.lower()})"
        elif name:
            label = name
        elif role:
            label = f"the {role.lower()}"
        else:
            label = "a staff member"

        people[actor_id] = Person(
            actor_id=actor_id,
            side="staff",
            role=role,
            name=name,
            email=email,
            event_count=count,
            label=label,
        )

    # The client is a single external id for the whole case.
    external = Counter(
        e.actor.actor_id for e in events if e.actor.side == "client" and e.actor.actor_id
    )
    for actor_id, count in external.items():
        people[actor_id] = Person(
            actor_id=actor_id,
            side="client",
            role="Client",
            name=client_name,
            event_count=count,
            label=f"{client_name} (client)" if client_name else "the client",
        )

    return Roster(people=people, client_name=client_name, attorney_name=attorney_name)


def apply_roster(events: list[NormalizedEvent], roster: Roster) -> None:
    """Rewrite actor labels in place, now that every id has been seen."""
    for event in events:
        person = roster.people.get(event.actor.actor_id or "")
        if person is None:
            continue
        event.actor.label = person.label
        if person.role and not event.actor.role:
            event.actor.role = person.role
