"""
Terminal client: `uv run python -m src.cli --case case_001`.

Two modes, both backed by the same `CaseSession` the web UI and the eval
harness use, so there is exactly one code path to trust:

  --ask "..."   one question, print the answer and exit
  (default)     an interactive REPL that keeps conversation context
"""

from __future__ import annotations

import argparse
import sys

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from . import config
from .agent.session import Answer, CaseSession
from .data_loader import list_cases

console = Console()

_VERDICT_STYLE = {
    "answered": "green",
    "partial": "yellow",
    "unknown": "cyan",
    "refused": "red",
}


def _print_brief(session: CaseSession) -> None:
    brief = session.brief
    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim")
    table.add_column()
    table.add_row("Client", brief.client_name or "unknown")
    table.add_row("Opposing party", brief.opposing_party or "unknown")
    table.add_row("Matter", f"{brief.matter_type or '?'} · {brief.jurisdiction or '?'}")
    table.add_row("Court case no.", brief.court_case_number or "not on record")
    table.add_row("Status", brief.case_status or "unknown")
    if brief.status_detail:
        table.add_row("", f"[dim]{brief.status_detail}[/dim]")
    contact = brief.contact
    if contact.days_since_client_spoke is not None:
        table.add_row(
            "Contact",
            f"client last wrote {contact.days_since_client_spoke}d ago · "
            f"we last wrote {contact.days_since_we_reached_out}d ago",
        )
    for deadline in brief.deadlines:
        due = f"{deadline.due:%Y-%m-%d}" if deadline.due else "unknown"
        table.add_row(deadline.name, f"{due} [{deadline.status}]")
    table.add_row("Volume", f"{brief.event_count} events · {brief.document_count} documents")
    console.print(
        Panel(
            table, title=f"[bold]{brief.case_slug}[/bold]", subtitle=f"as of {session.now:%Y-%m-%d}"
        )
    )


def _print_answer(answer: Answer, show_trace: bool) -> None:
    console.print()
    console.print(Markdown(answer.answer or "_(no answer)_"))

    cited = answer.cited
    if cited:
        console.print()
        console.print("[dim]Sources[/dim]")
        for citation in cited:
            when = (citation["when"] or "")[:10]
            flag = (
                " [yellow](transcript — do not quote verbatim)[/yellow]"
                if citation["low_fidelity"]
                else ""
            )
            console.print(f"  [dim]•[/dim] [{citation['ref']}] {when} {citation['label']}{flag}")

    if answer.gaps:
        console.print()
        console.print("[yellow]Caveats[/yellow]")
        for gap in answer.gaps:
            console.print(f"  [dim]•[/dim] {gap}")

    style = _VERDICT_STYLE.get(answer.verdict, "white")
    usage = answer.usage
    console.print()
    console.print(
        f"[{style}]{answer.verdict}[/{style}] · confidence {answer.confidence} · "
        f"{answer.latency_ms / 1000:.1f}s · {len(answer.tool_calls)} tool call(s) · "
        f"{usage.get('input_tokens', 0):,} in / {usage.get('output_tokens', 0):,} out · "
        f"cache {usage.get('cache_hit_rate', 0):.0%} · ${usage.get('cost_usd', 0):.3f}",
        style="dim",
    )

    if show_trace:
        table = Table(title="Tool calls", show_header=True, header_style="dim", box=None)
        table.add_column("tool")
        table.add_column("arguments", overflow="fold")
        table.add_column("ms", justify="right")
        for call in answer.tool_calls:
            table.add_row(call["tool"], str(call["arguments"]), f"{call['latency_ms']:.0f}")
        console.print(table)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.cli")
    parser.add_argument("--case", help="Case slug, e.g. case_001")
    parser.add_argument("--ask", help="Ask one question and exit")
    parser.add_argument("--trace", action="store_true", help="Show tool calls after each answer")
    parser.add_argument("--no-rerank", action="store_true", help="Disable the Claude reranker")
    args = parser.parse_args(argv)

    cases = list_cases(config.CASE_DATA_DIR)
    if not cases:
        console.print(f"[red]No cases found under {config.CASE_DATA_DIR}[/red]")
        return 1

    case_slug = args.case
    if case_slug is None:
        if len(cases) == 1:
            case_slug = cases[0]
        else:
            console.print("Available cases: " + ", ".join(cases))
            case_slug = console.input("Case: ").strip()
    if case_slug not in cases:
        console.print(f"[red]Unknown case '{case_slug}'. Available: {', '.join(cases)}[/red]")
        return 1

    try:
        session = CaseSession(case_slug, rerank=not args.no_rerank)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    if args.ask:
        _print_answer(session.ask(args.ask), args.trace)
        return 0

    _print_brief(session)
    console.print("[dim]Ask about this case. /brief, /reset, /trace, /quit[/dim]\n")
    while True:
        try:
            question = console.input("[bold cyan]?[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return 0
        if not question:
            continue
        if question in ("/quit", "/exit", "/q"):
            return 0
        if question == "/brief":
            _print_brief(session)
            continue
        if question == "/reset":
            session.reset()
            console.print("[dim]conversation cleared[/dim]")
            continue
        if question == "/trace":
            args.trace = not args.trace
            console.print(f"[dim]trace {'on' if args.trace else 'off'}[/dim]")
            continue
        try:
            with console.status("[dim]researching…[/dim]"):
                answer = session.ask(question)
        except Exception as exc:  # keep the REPL alive
            console.print(f"[red]{type(exc).__name__}: {exc}[/red]")
            continue
        _print_answer(answer, args.trace)
        console.print()


if __name__ == "__main__":
    sys.exit(main())
