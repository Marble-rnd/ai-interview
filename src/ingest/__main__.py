"""CLI for the ingestion pipeline: `uv run python -m src.ingest --case case_001`."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .. import config
from ..agent.llm import LLM, Usage
from ..data_loader import list_cases
from .pipeline import ingest_case


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.ingest",
        description="Build the searchable index artifact for one or more cases.",
    )
    parser.add_argument(
        "--case",
        action="append",
        dest="cases",
        help="Case slug (repeatable). Defaults to every case in case_data/.",
    )
    parser.add_argument(
        "--force", action="store_true", help="Rebuild even if sources are unchanged."
    )
    parser.add_argument(
        "--no-enrich",
        action="store_true",
        help="Skip Claude contextual blurbs; use deterministic structural context instead.",
    )
    parser.add_argument(
        "--no-dense", action="store_true", help="Skip embeddings; index sparse-only."
    )
    parser.add_argument("--json", action="store_true", help="Emit manifests as JSON.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    cases = args.cases or list_cases(config.CASE_DATA_DIR)
    if not cases:
        print(f"No cases found under {config.CASE_DATA_DIR}", file=sys.stderr)
        return 1

    usage = Usage()
    manifests = []
    for slug in cases:
        manifest = ingest_case(
            slug,
            llm=LLM(usage),
            force=args.force,
            enrich=not args.no_enrich,
            dense=not args.no_dense,
        )
        manifests.append(manifest)
        if args.json:
            continue
        if manifest.get("skipped"):
            print(f"{slug}: up to date (sources unchanged)")
            continue
        counts = manifest["counts"]
        print(
            f"{slug}: {counts['events']} events, {counts['chunks']} chunks "
            f"({counts['event_chunks']} event / {counts['document_chunks']} document), "
            f"{counts['documents_on_disk']} documents "
            f"({counts['documents_absent']} recorded but not in snapshot), "
            f"{counts['ocr_pages']} OCR pages, {counts['blurbs_generated']} new blurbs "
            f"in {manifest['build_seconds']}s"
        )
        unhandled = manifest["normalize_report"]["unhandled_shapes"]
        if unhandled:
            print(f"  ! unhandled event shapes: {unhandled}")

    if args.json:
        print(json.dumps(manifests, indent=1))
    elif usage.calls:
        spend = usage.to_dict()
        print(
            f"\ningest model spend: {spend['calls']} calls, "
            f"{spend['input_tokens']:,} in / {spend['output_tokens']:,} out, "
            f"cache hit {spend['cache_hit_rate']:.0%}, ${spend['cost_usd']:.2f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
