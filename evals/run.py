"""
The evaluation harness.

Three entry points, deliberately separated so the cheap one can run on every
commit:

  --retrieval-only   score the retriever with no agent and no judge, and
                     print the ablation that decides whether each retrieval
                     component earns its place
  --tier1            run the agent, score deterministically (assertions,
                     tool selection, citations, abstention, cost, latency)
  --all              tier 1 plus the LLM judge and its calibration controls

Usage:
    uv run python -m evals.run --retrieval-only
    uv run python -m evals.run --tier1
    uv run python -m evals.run --all
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from src import config
from src.agent.llm import LLM, Usage
from src.agent.rerank import rerank
from src.agent.session import CaseSession
from src.case.retrieval import ALL_MODES, Retriever
from src.ingest.pipeline import load_artifact

from .judge import judge_answer, run_controls
from .metrics import (
    check_assertions,
    mean,
    percentile,
    score_citations,
    score_ranking,
    score_tools,
)
from .report import render_report, render_retrieval_report

HERE = Path(__file__).resolve().parent
DATASET = HERE / "dataset.yaml"
RESULTS = HERE / "results"

# Retrieval is scored at 10: deep enough that a fusion miss is visible,
# shallow enough that precision still means something.
RETRIEVAL_K = 10


def load_dataset(path: Path = DATASET) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data["items"]


def _gold_ids(item: dict[str, Any]) -> list[str]:
    return list(item.get("gold_events") or []) + list(item.get("gold_documents") or [])


def _chunk_id(chunk: Any) -> str | None:
    """The identifier a chunk is scored under: its event ref or its filename."""
    return chunk.event_ref or chunk.document_name


# --------------------------------------------------------------------------
# Retrieval-only evaluation
# --------------------------------------------------------------------------


def evaluate_retrieval(
    items: list[dict[str, Any]], modes: tuple[str, ...] = ALL_MODES, use_llm: bool = True
) -> dict[str, Any]:
    """Score the retriever directly, with no agent and no generation.

    Decoupling retrieval from generation is the point: it says whether a
    failure is the index's fault or the prompt's, and it regression-tests the
    index with no model in the loop for four of the five modes.
    """
    artifacts = {}
    scored: dict[str, list[dict[str, Any]]] = {mode: [] for mode in modes}
    usage = Usage()
    llm = LLM(usage)

    for item in items:
        # Only items whose answer is genuinely reachable by similarity search
        # belong in a ranker ablation. Scoring "what is the most recent
        # inbound message" against BM25 measures the wrong mechanism and adds
        # noise that hides the real differences between modes.
        if item.get("retrieval_mechanism") != "ranked":
            continue
        if item.get("skip_retrieval") or not _gold_ids(item):
            continue
        slug = item["case"]
        if slug not in artifacts:
            artifact = load_artifact(slug)
            artifacts[slug] = Retriever(
                artifact["chunks"], artifact["embeddings"], artifact["events"]
            )
        retriever = artifacts[slug]
        query = item.get("retrieval_query") or item["question"]
        gold = _gold_ids(item)

        for mode in modes:
            if mode == "dense" and not retriever.dense_available:
                continue
            if mode == "hybrid_rerank" and not use_llm:
                continue
            started = time.perf_counter()
            hits = retriever.search(query, k=RETRIEVAL_K * 3, mode=mode)
            if mode == "hybrid_rerank":
                hits = rerank(query, hits, llm, top_k=RETRIEVAL_K)
            latency = (time.perf_counter() - started) * 1000

            # One entry per source: a five-chunk document should occupy one
            # slot, not crowd the ranking.
            ordered: list[str] = []
            for hit in hits:
                identifier = _chunk_id(hit.chunk)
                if identifier and identifier not in ordered:
                    ordered.append(identifier)

            score = score_ranking(ordered, gold, RETRIEVAL_K)
            scored[mode].append(
                {
                    "id": item["id"],
                    "case": slug,
                    "category": item.get("category"),
                    "query": query,
                    "recall": score.recall,
                    "precision": score.precision,
                    "mrr": score.mrr,
                    "first_hit_rank": score.first_hit_rank,
                    "missed": score.missed,
                    "latency_ms": round(latency, 1),
                }
            )

    summary = {
        mode: {
            "items": len(rows),
            f"recall@{RETRIEVAL_K}": mean(r["recall"] for r in rows),
            f"precision@{RETRIEVAL_K}": mean(r["precision"] for r in rows),
            "mrr": mean(r["mrr"] for r in rows),
            "perfect_recall": sum(1 for r in rows if r["recall"] >= 1.0),
            "total_miss": sum(1 for r in rows if r["recall"] == 0.0),
            "p50_latency_ms": percentile([r["latency_ms"] for r in rows], 50),
        }
        for mode, rows in scored.items()
        if rows
    }
    excluded = [
        {"id": i["id"], "mechanism": i.get("retrieval_mechanism")}
        for i in items
        if i.get("retrieval_mechanism") not in ("ranked", None)
    ]
    return {
        "summary": summary,
        "per_item": scored,
        "excluded": excluded,
        "usage": usage.to_dict(),
        "k": RETRIEVAL_K,
    }


# --------------------------------------------------------------------------
# End-to-end evaluation
# --------------------------------------------------------------------------


def _run_item(item: dict[str, Any], rerank_enabled: bool) -> dict[str, Any]:
    """Run one dataset item through a fresh single-turn session."""
    session = CaseSession(
        item["case"],
        rerank=rerank_enabled,
        trace_dir=RESULTS / "traces",
    )
    started = time.perf_counter()
    try:
        answer = session.ask(item["question"])
    except Exception as exc:
        return {
            "id": item["id"],
            "case": item["case"],
            "category": item.get("category"),
            "question": item["question"],
            "error": f"{type(exc).__name__}: {exc}",
            "passed": False,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    assertions = check_assertions(
        answer.answer, item.get("must_include") or [], item.get("must_not_include") or []
    )
    tools = score_tools(
        answer.tool_calls, item.get("gold_tools") or [], item.get("gold_tool_args") or {}
    )
    citations = score_citations(
        answer.citations, item.get("gold_events") or [], item.get("gold_documents") or []
    )
    verdict_ok = answer.verdict == item.get("expected_verdict", answer.verdict)

    # Retrieval recall measured on what the agent actually surfaced, which is
    # distinct from the generation-free retriever score above.
    surfaced = [c.get("document_name") or c.get("ref") for c in answer.citations]
    surfaced = [s for s in surfaced if s]
    retrieval = score_ranking(surfaced, _gold_ids(item), max(RETRIEVAL_K, len(surfaced) or 1))

    return {
        "id": item["id"],
        "case": item["case"],
        "category": item.get("category"),
        "question": item["question"],
        "answer": answer.answer,
        "gold_answer": item.get("gold_answer", ""),
        "verdict": answer.verdict,
        "expected_verdict": item.get("expected_verdict"),
        "verdict_ok": verdict_ok,
        "confidence": answer.confidence,
        "assertions": assertions,
        "tools": {
            "recall": tools.recall,
            "precision": tools.precision,
            "arg_correctness": tools.arg_correctness,
            "used": tools.used,
            "expected": tools.expected,
            "missing_args": tools.missing_args,
            "duplicate_calls": tools.duplicate_calls,
            "total_calls": tools.total_calls,
        },
        "citations": {
            "precision": citations.precision,
            "recall": citations.recall,
            "cited": citations.cited,
            "gold": citations.gold,
            "spurious": citations.spurious,
        },
        "agent_retrieval_recall": retrieval.recall,
        "retrieval_missed": retrieval.missed,
        "gaps": answer.gaps,
        "lint": answer.lint,
        "planned": answer.planned,
        "attempts": answer.attempts,
        "latency_ms": answer.latency_ms,
        "usage": answer.usage,
        "citation_payload": answer.citations,
        "passed": assertions["passed"] and verdict_ok,
    }


def evaluate_agent(
    items: list[dict[str, Any]], *, rerank_enabled: bool = True, workers: int = 4
) -> list[dict[str, Any]]:
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda i: _run_item(i, rerank_enabled), items))


def judge_results(results: list[dict[str, Any]], workers: int = 4) -> dict[str, Any]:
    """Grade every non-errored result, then grade the judge."""
    usage = Usage()
    llm = LLM(usage)

    def grade(result: dict[str, Any]) -> None:
        if result.get("error") or not result.get("gold_answer"):
            return
        try:
            judgement = judge_answer(
                llm,
                question=result["question"],
                answer=result["answer"],
                gold_answer=result["gold_answer"],
                citations=result.get("citation_payload") or [],
            )
        except Exception as exc:
            # A judging failure is a gap in the report, not a failed run —
            # the deterministic tier already stands on its own.
            result["judge_error"] = f"{type(exc).__name__}: {exc}"
            return
        if not judgement.ok:
            result["judge_error"] = judgement.reason
            result.pop("judge", None)
            return
        result["judge"] = judgement.to_dict()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(grade, results))

    controls = run_controls(llm)
    return {"controls": controls, "usage": usage.to_dict()}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.run")
    parser.add_argument("--retrieval-only", action="store_true", help="Retriever ablation only.")
    parser.add_argument("--tier1", action="store_true", help="Agent + deterministic metrics.")
    parser.add_argument("--all", action="store_true", help="Tier 1 plus the LLM judge.")
    parser.add_argument(
        "--judge-only",
        action="store_true",
        help="Re-grade the saved run in evals/results/agent.json without re-running "
        "the agent. Use when judging failed but the agent phase is sound.",
    )
    parser.add_argument("--case", help="Restrict to one case slug.")
    parser.add_argument("--id", action="append", dest="ids", help="Run specific item ids.")
    parser.add_argument("--no-rerank", action="store_true", help="Disable the Claude reranker.")
    parser.add_argument(
        "--workers",
        type=int,
        default=3,
        help="Parallel items. Above ~3 the suite rate-limits itself and "
        "latency numbers stop reflecting the agent.",
    )
    parser.add_argument("--out", type=Path, default=RESULTS)
    args = parser.parse_args(argv)

    if not (args.retrieval_only or args.tier1 or args.all or args.judge_only):
        parser.error("choose one of --retrieval-only, --tier1, --all, --judge-only")

    items = load_dataset()
    if args.case:
        items = [i for i in items if i["case"] == args.case]
    if args.ids:
        items = [i for i in items if i["id"] in set(args.ids)]
    if not items:
        print("No dataset items selected.", file=sys.stderr)
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    if args.retrieval_only:
        report = evaluate_retrieval(items, use_llm=not args.no_rerank)
        (args.out / "retrieval.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
        text = render_retrieval_report(report)
        (args.out / "retrieval.md").write_text(text, encoding="utf-8")
        print(text)
        return 0

    if args.judge_only:
        saved = json.loads((args.out / "agent.json").read_text(encoding="utf-8"))
        selected = {i["id"] for i in items}
        results = [r for r in saved["results"] if r["id"] in selected]
        for result in results:
            result.pop("judge", None)
            result.pop("judge_error", None)
        judge_block = judge_results(results, workers=args.workers)
    else:
        results = evaluate_agent(items, rerank_enabled=not args.no_rerank, workers=args.workers)
        judge_block = judge_results(results, workers=args.workers) if args.all else None

    payload = {
        "run_at": stamp,
        "now": config.now().isoformat(),
        "model": config.MODEL,
        "rerank": not args.no_rerank,
        "results": results,
        "judge": judge_block,
    }
    (args.out / "agent.json").write_text(
        json.dumps(payload, indent=1, default=str), encoding="utf-8"
    )
    text = render_report(payload)
    (args.out / "agent.md").write_text(text, encoding="utf-8")
    print(text)

    failures = [r for r in results if not r.get("passed")]
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
