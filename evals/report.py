"""Markdown rendering for eval runs.

The reports are committed so a reviewer without an API key still sees the
numbers.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .metrics import mean, percentile


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join("" if c is None else str(c) for c in row) + " |")
    return "\n".join(lines)


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def _num(value: float | None, spec: str = ".2f") -> str:
    return "—" if value is None else format(value, spec)


def render_retrieval_report(report: dict[str, Any]) -> str:
    k = report["k"]
    summary = report["summary"]
    order = ["bm25", "bm25_contextual", "dense", "hybrid_raw", "hybrid", "hybrid_rerank"]
    labels = {
        "bm25": "BM25 (raw chunk text)",
        "bm25_contextual": "BM25 + Claude context blurb",
        "dense": "Dense (bge-small, local ONNX)",
        "hybrid_raw": "Hybrid RRF (raw BM25 + dense)",
        "hybrid": "Hybrid RRF (contextual BM25 + dense) — default",
        "hybrid_rerank": "Hybrid RRF + Claude rerank",
    }
    rows = []
    for mode in order:
        stats = summary.get(mode)
        if not stats:
            continue
        rows.append(
            [
                labels[mode],
                stats["items"],
                _pct(stats[f"recall@{k}"]),
                _pct(stats[f"precision@{k}"]),
                _num(stats["mrr"], ".3f"),
                stats["perfect_recall"],
                stats["total_miss"],
                f"{stats['p50_latency_ms']:.0f}ms",
            ]
        )

    out = [
        "# Retrieval evaluation",
        "",
        f"Generation-free: the retriever is scored directly against hand-labelled gold "
        f"evidence at k={k}, with no model writing an answer. A failure here is an index "
        "problem; a failure only in the end-to-end run is a prompt problem.",
        "",
        f"Scored on the **{sum(len(v) for v in report['per_item'].values()) // max(len(report['summary']), 1)} "
        "items whose answer is genuinely reachable by similarity search**. Items answered "
        "by an exact filter (a date window, a direction) or by the precomputed brief are "
        f"excluded — {len(report.get('excluded', []))} of them — because ranking is not "
        "the mechanism under test there.",
        "",
        _table(
            [
                "mode",
                "items",
                f"recall@{k}",
                f"precision@{k}",
                "MRR",
                "perfect",
                "total miss",
                "p50",
            ],
            rows,
        ),
        "",
        "**Reading it.** `bm25` → `bm25_contextual` isolates Claude's ingest-time "
        "context blurbs; `hybrid_raw` → `hybrid` isolates them again once fused with "
        "dense. The counter-intuitive result is that the blurbs *hurt* sparse retrieval "
        "on their own — each one repeats the party names, the court and the matter "
        "type, so every document chunk starts matching every query — but help once "
        "fused, where RRF works on ranks and the dense side rewards the added context. "
        "`dense` is the only component that needs a local model, so if hybrid did not "
        "beat the sparse-only rows the dependency would be dropped. Reranking mostly "
        "moves MRR, which is what it is for.",
    ]

    misses = [
        (mode, row)
        for mode in order
        for row in report["per_item"].get(mode, [])
        if row["recall"] < 1.0
    ]
    if misses:
        out += ["", "## Incomplete retrievals", ""]
        out.append(
            _table(
                ["mode", "item", "recall", "MRR", "missed"],
                [
                    [
                        mode,
                        row["id"],
                        _num(row["recall"]),
                        _num(row["mrr"]),
                        ", ".join(row["missed"]),
                    ]
                    for mode, row in misses
                ],
            )
        )
    return "\n".join(out) + "\n"


def render_report(payload: dict[str, Any]) -> str:
    results = payload["results"]
    judged = [r for r in results if r.get("judge")]
    ok = [r for r in results if not r.get("error")]

    passed = sum(1 for r in results if r.get("passed"))
    verdict_ok = sum(1 for r in ok if r.get("verdict_ok"))

    out = [
        "# Agent evaluation",
        "",
        f"- Model: `{payload['model']}` · reranking {'on' if payload['rerank'] else 'off'} "
        f"· clock pinned to {payload['now'][:10]}",
        f"- Items: **{len(results)}** · assertions+verdict passed: **{passed}/{len(results)}**",
        "",
        "## Headline",
        "",
    ]

    latencies = [r["latency_ms"] for r in ok]
    costs = [r["usage"].get("cost_usd", 0) for r in ok]
    rows = [
        [
            "Deterministic pass rate",
            f"{passed}/{len(results)} ({_pct(passed / max(len(results), 1))})",
        ],
        ["Verdict class correct", f"{verdict_ok}/{len(ok)}"],
        ["Citation precision (mean)", _pct(mean(r["citations"]["precision"] for r in ok))],
        ["Citation recall (mean)", _pct(mean(r["citations"]["recall"] for r in ok))],
        ["Agent retrieval recall (mean)", _pct(mean(r["agent_retrieval_recall"] for r in ok))],
        ["Tool recall (mean)", _pct(mean(r["tools"]["recall"] for r in ok))],
        [
            "Tool arg correctness (mean)",
            _pct(
                mean(
                    r["tools"]["arg_correctness"]
                    for r in ok
                    if r["tools"]["arg_correctness"] is not None
                )
            ),
        ],
        ["Tool calls / question", _num(mean(r["tools"]["total_calls"] for r in ok), ".1f")],
        ["Duplicate tool calls", sum(r["tools"]["duplicate_calls"] for r in ok)],
        [
            "Latency p50 / p95",
            f"{percentile(latencies, 50) / 1000:.1f}s / {percentile(latencies, 95) / 1000:.1f}s",
        ],
        ["Cost / question (mean)", f"${_num(mean(costs), '.3f')}"],
        ["Total run cost", f"${sum(costs):.2f}"],
        ["Cache hit rate (mean)", _pct(mean(r["usage"].get("cache_hit_rate", 0) for r in ok))],
        ["Retries triggered", sum(1 for r in ok if r.get("attempts", 1) > 1)],
        ["Planner invoked", sum(1 for r in ok if r.get("planned"))],
    ]
    unjudged = [r for r in ok if r.get("judge_error")]
    # A judging gap is reported, never silently averaged away.
    if judged:
        rows += [
            ["Judge coverage", f"{len(judged)}/{len(ok)}"],
            ["Judge — correctness (1-5)", _num(mean(r["judge"]["correctness"] for r in judged))],
            ["Judge — completeness (1-5)", _num(mean(r["judge"]["completeness"] for r in judged))],
            ["Judge — groundedness (1-5)", _num(mean(r["judge"]["groundedness"] for r in judged))],
            [
                "**Judge — composite**",
                f"**{_num(mean(r['judge']['composite'] for r in judged))} / 5**",
            ],
        ]
    out.append(_table(["metric", "value"], rows))

    # Abstention is its own number: the negative and safety items are where a
    # confident-sounding wrong answer does the most damage.
    abstention = [r for r in ok if r.get("category") in ("negative", "safety")]
    if abstention:
        hit = sum(1 for r in abstention if r.get("passed"))
        out += [
            "",
            "## Abstention and safety",
            "",
            f"{hit}/{len(abstention)} correct on items where the right answer is a refusal "
            "or a reported absence.",
            "",
            _table(
                ["item", "expected", "verdict", "result"],
                [
                    [
                        r["id"],
                        r.get("expected_verdict"),
                        r["verdict"],
                        "pass" if r["passed"] else "FAIL",
                    ]
                    for r in abstention
                ],
            ),
        ]

    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in ok:
        by_category[result.get("category") or "other"].append(result)
    out += [
        "",
        "## By category",
        "",
        _table(
            ["category", "n", "passed", "citation recall", "judge composite", "p50 latency"],
            [
                [
                    category,
                    len(rows_),
                    sum(1 for r in rows_ if r["passed"]),
                    _pct(mean(r["citations"]["recall"] for r in rows_)),
                    (
                        _num(mean(r["judge"]["composite"] for r in rows_ if r.get("judge")))
                        if any(r.get("judge") for r in rows_)
                        else "—"
                    ),
                    f"{percentile([r['latency_ms'] for r in rows_], 50) / 1000:.1f}s",
                ]
                for category, rows_ in sorted(by_category.items())
            ],
        ),
    ]

    if unjudged:
        out += [
            "",
            "## Judging gaps",
            "",
            f"{len(unjudged)} item(s) could not be graded and are excluded from the judge "
            "means rather than counted as zero:",
            "",
            _table(
                ["item", "reason"],
                [[r["id"], r["judge_error"]] for r in unjudged],
            ),
        ]

    if payload.get("judge"):
        controls = payload["judge"]["controls"]
        out += [
            "",
            "## Judge calibration",
            "",
            "Deliberately broken answers with known-correct grades, run through the same "
            "judge. A judge that passes these is worth believing; one that does not is "
            f"visible here rather than assumed. **{controls['passed']}/{controls['total']} "
            f"({_pct(controls['accuracy'])})**",
            "",
            _table(
                ["control", "expected", "correctness", "groundedness", "result"],
                [
                    [
                        c["id"],
                        ", ".join(f"{k}={v}" for k, v in c["expected"].items()),
                        c["scored"]["correctness"],
                        c["scored"]["groundedness"],
                        "pass" if c["passed"] else "FAIL",
                    ]
                    for c in controls["results"]
                ],
            ),
        ]

    out += ["", "## Per item", ""]
    out.append(
        _table(
            ["id", "case", "category", "pass", "verdict", "cite P/R", "tools", "judge", "s", "$"],
            [
                [
                    r["id"],
                    r["case"].replace("case_", ""),
                    r.get("category"),
                    "✓" if r.get("passed") else "✗",
                    f"{r.get('verdict')}"
                    + ("" if r.get("verdict_ok") else f" (want {r.get('expected_verdict')})"),
                    f"{_num(r['citations']['precision'])}/{_num(r['citations']['recall'])}",
                    f"{r['tools']['total_calls']}",
                    f"{r['judge']['composite']:.1f}" if r.get("judge") else "—",
                    f"{r['latency_ms'] / 1000:.0f}",
                    f"{r['usage'].get('cost_usd', 0):.3f}",
                ]
                for r in results
                if not r.get("error")
            ],
        )
    )

    failures = [r for r in results if not r.get("passed")]
    if failures:
        out += ["", "## Failures", ""]
        for result in failures:
            out.append(f"### {result['id']}")
            if result.get("error"):
                out += ["", f"Errored: `{result['error']}`", ""]
                continue
            assertions = result["assertions"]
            if assertions["missing"]:
                out.append(f"- missing required pattern(s): `{assertions['missing']}`")
            if assertions["forbidden_present"]:
                out.append(f"- matched forbidden pattern(s): `{assertions['forbidden_present']}`")
            if not result.get("verdict_ok"):
                out.append(
                    f"- verdict `{result['verdict']}`, expected `{result['expected_verdict']}`"
                )
            if result.get("judge", {}).get("quotes"):
                out.append(f"- judge flagged: {result['judge']['quotes']}")
            out += ["", "```", result["answer"][:900], "```", ""]

    return "\n".join(out) + "\n"
