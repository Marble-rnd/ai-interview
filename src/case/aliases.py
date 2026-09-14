"""
Query expansion for family-law vocabulary.

Attorneys ask for "the financial disclosures"; the data says `FL-140`,
"Preliminary Declaration of Disclosure", "PDD". BM25 cannot bridge that gap
and neither can a small embedding model reliably. A short, explicit alias map
does, and it is auditable in a way a learned expansion is not.

This is deliberately shallow domain knowledge — form numbers and the phrases
that name them. Anything requiring legal judgement stays out.
"""

from __future__ import annotations

import re

# Each entry is a cluster of surface forms that mean the same thing. Matching
# any member expands the query with all of them.
_CLUSTERS: list[tuple[str, ...]] = [
    # California Judicial Council family-law forms (case_002).
    ("fl-100", "fl100", "petition for dissolution", "petition marriage", "summons petition"),
    ("fl-105", "fl105", "uccjea", "child custody jurisdiction declaration"),
    ("fl-110", "fl110", "summons family law"),
    ("fl-120", "fl120", "response to petition", "respondent's response"),
    ("fl-130a", "fl130a", "resolution bundle"),
    (
        "fl-140",
        "fl140",
        "declaration of disclosure",
        "preliminary declaration of disclosure",
        "pdd",
        "financial disclosure",
        "financial disclosures",
    ),
    (
        "fl-142",
        "fl142",
        "schedule of assets and debts",
        "assets and debts",
    ),
    (
        "fl-150",
        "fl150",
        "income and expense declaration",
        "income and expense",
        "i&e",
    ),
    ("fl-170", "fl170", "declaration for default", "uncontested dissolution declaration"),
    ("fl-180", "fl180", "judgment", "judgment of dissolution"),
    ("fl-190", "fl190", "notice of entry of judgment"),
    ("fl-191", "fl191", "child support case registry", "case registry form"),
    ("fl-141", "fl141", "declaration regarding service of disclosure"),
    # Florida practice (case_001).
    ("uccjea", "uniform child custody jurisdiction and enforcement act affidavit"),
    ("standing family law order", "sflo", "administrative order"),
    ("return of service", "proof of service", "affidavit of service", "served"),
    ("notice of limited appearance", "limited appearance"),
    ("mandatory disclosure", "mandatory disclosures", "rule 12.285"),
    ("clerk default", "default", "motion for default"),
    # Cross-jurisdiction concepts that appear under many names in the data.
    ("kickoff call", "ckc", "case kickoff", "kick off call"),
    ("legal strategy session", "lss", "strategy consultation"),
    ("opposing party", "op", "respondent", "other party"),
    ("marital settlement agreement", "msa", "settlement agreement"),
    ("filing fee", "court fee", "court costs", "filing cost"),
    ("pay stub", "paystub", "paycheck", "proof of income", "proof of current income"),
    ("tax return", "1040", "income tax return"),
    ("bank statement", "account statement", "credit union statement"),
    ("mediation", "mediator", "half day mediation"),
]

# Built once: every surface form → the full cluster it belongs to.
_INDEX: dict[str, tuple[str, ...]] = {}
for cluster in _CLUSTERS:
    for member in cluster:
        _INDEX.setdefault(member, cluster)

# `FL 150`, `fl-150`, `FL150` all normalize to `fl-150`.
_FORM_CODE = re.compile(r"\b(fl)[\s\-_]?(\d{2,3}[a-z]?)\b", re.I)

_MAX_TERMS = 12


def normalize_query(query: str) -> str:
    """Fold form-code spellings so the lookup sees one canonical shape."""
    return _FORM_CODE.sub(lambda m: f"{m.group(1).lower()}-{m.group(2).lower()}", query.lower())


def expand(query: str) -> list[str]:
    """Return extra terms implied by the query, longest phrases first.

    The original query is not included; callers append these to it.
    """
    normalized = normalize_query(query)
    found: list[str] = []
    for surface, cluster in _INDEX.items():
        if surface in normalized:
            found.extend(term for term in cluster if term not in normalized)
    # Preserve order, drop duplicates, prefer the more specific phrases.
    seen: set[str] = set()
    unique = [t for t in found if not (t in seen or seen.add(t))]
    unique.sort(key=lambda t: -len(t))
    return unique[:_MAX_TERMS]


def expanded_query(query: str) -> str:
    """The query plus its implied vocabulary, for the sparse index."""
    extra = expand(query)
    return f"{query} {' '.join(extra)}".strip() if extra else query
