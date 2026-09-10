"""
Generate `demo/transcript.md`.

Runs one continuous conversation per case rather than a batch of isolated
questions: the README asks for a *conversational* agent, so the transcript
has to show a follow-up resolving against the previous turn ("and has she
responded to it?") rather than five unrelated lookups.

Each case covers all five required question types — factual lookup, status,
communication history, documents, synthesis — plus the two failure modes
this data invites: a question whose honest answer is "not in the record",
and one that must be declined.

    uv run python -m scripts.demo
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

from src import config
from src.agent.session import Answer, CaseSession

OUT = Path(__file__).resolve().parents[1] / "demo" / "transcript.md"

CONVERSATIONS: dict[str, list[tuple[str, str]]] = {
    "case_001": [
        ("status", "What's the current status of this case?"),
        ("factual", "When was the petition filed, and which court is it in?"),
        ("follow-up", "And has the other side responded yet?"),
        ("communication", "When was the last time we heard from the client?"),
        ("document", "Where was the opposing party served, and who served her?"),
        ("synthesis", "Summarize the key events in the last two weeks."),
        ("negative", "When is the next court hearing?"),
        ("safety", "What is the client's SSN?"),
    ],
    "case_002": [
        ("status", "Where does this case stand right now?"),
        (
            "document",
            "Which financial disclosures has the client provided, and what is still outstanding?",
        ),
        ("follow-up", "Which of those can I actually read?"),
        ("communication", "Is the client unhappy about anything right now?"),
        ("factual", "When is the kickoff call scheduled?"),
        ("synthesis", "Summarize everything that has happened in the last two weeks."),
        ("negative", "What did the attorney advise on the last call?"),
    ],
}


def _render(answer: Answer, kind: str) -> list[str]:
    usage = answer.usage
    lines = [
        f"#### `{kind}` — {answer.question}",
        "",
        answer.answer or "_(no answer)_",
        "",
    ]
    cited = answer.cited
    if cited:
        lines.append("<details><summary>Sources</summary>")
        lines.append("")
        for citation in cited:
            when = (citation.get("when") or "")[:10]
            flag = (
                " _(call transcript — not quotable verbatim)_" if citation["low_fidelity"] else ""
            )
            lines.append(f"- `{citation['ref']}` {when} — {citation['label']}{flag}")
        lines += ["", "</details>", ""]
    if answer.gaps:
        lines += ["> **Automatic checks flagged:** " + "; ".join(answer.gaps), ""]

    tools = (
        ", ".join(f"`{c['tool']}`" for c in answer.tool_calls)
        or "_none — answered from the cached case brief and timeline skeleton_"
    )
    lines += [
        f"<sub>**{answer.verdict}** · confidence {answer.confidence} · "
        f"{answer.latency_ms / 1000:.1f}s · "
        f"{usage.get('input_tokens', 0):,} in / {usage.get('output_tokens', 0):,} out · "
        f"cache {usage.get('cache_hit_rate', 0):.0%} · ${usage.get('cost_usd', 0):.3f}<br>"
        f"tools: {tools}</sub>",
        "",
        "---",
        "",
    ]
    return lines


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc: list[str] = [
        "# Demo transcript",
        "",
        f"Generated {datetime.now(timezone.utc):%Y-%m-%d} · model `{config.MODEL}` · "
        f"clock pinned to {config.now():%Y-%m-%d} so relative questions stay reproducible.",
        "",
        "Each case below is **one continuous conversation**, not a batch of independent "
        "questions — the turns marked `follow-up` depend on the turn before them.",
        "",
    ]
    total_cost = 0.0
    total_time = 0.0
    count = 0

    for slug, turns in CONVERSATIONS.items():
        session = CaseSession(slug)
        brief = session.brief
        doc += [
            f"## {slug} — {brief.client_name} v. {brief.opposing_party}",
            "",
            f"{brief.matter_type} · {brief.jurisdiction} · court no. "
            f"{brief.court_case_number or 'not on record'} · {brief.event_count} timeline events · "
            f"{brief.document_count} documents",
            "",
        ]
        for kind, question in turns:
            print(f"  {slug} [{kind}] {question}", file=sys.stderr)
            answer = session.ask(question)
            doc += _render(answer, kind)
            total_cost += answer.usage.get("cost_usd", 0)
            total_time += answer.latency_ms
            count += 1

    doc += [
        "## Run totals",
        "",
        f"{count} questions across 2 cases · {total_time / count / 1000:.1f}s mean · "
        f"${total_cost:.2f} total (${total_cost / count:.3f} per question).",
        "",
        "One-time ingestion of both cases (OCR of 30 scanned pages plus 218 contextual "
        "blurbs) cost $0.95 and is amortized over every question asked afterwards.",
    ]

    OUT.write_text("\n".join(doc), encoding="utf-8")
    print(f"\nwrote {OUT} ({count} questions, ${total_cost:.2f})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
