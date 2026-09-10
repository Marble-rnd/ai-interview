"""
LLM judging, with the judge itself under test.

Three scores, 1–5 each, combined into one number:

  correctness   does it say what the gold answer says?
  completeness  does it cover what was asked?
  groundedness  is every claim supported by the evidence that was retrieved?

Four things stop this from being a rubber stamp.

1. **The judge sees less than you would expect.** Groundedness is judged
   against the evidence pack alone — never the gold answer, never the agent's
   own reasoning. A judge shown the agent's justification adopts it.
   Correctness is judged against the gold answer alone, without the evidence,
   so it cannot rationalise a wrong answer from context.
2. **Every deduction must quote.** A score below 4 requires a verbatim span
   from the answer. A judge that has to point at the words cannot hand-wave.
3. **Poisoned controls.** Deliberately broken answers with known-correct
   scores run alongside the real ones, and the report prints how many the
   judge caught. An uncalibrated judge is visible rather than assumed.
4. **Separate calls.** Correctness and groundedness do not share a context,
   so a strong showing on one cannot halo the other.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from src import config
from src.agent.llm import LLM

# Weighting: an answer that is right but unsupported is worse than one that
# is slightly incomplete, because an attorney cannot verify it.
WEIGHTS = {"correctness": 0.4, "groundedness": 0.4, "completeness": 0.2}

_CORRECTNESS_SYSTEM = """\
You grade one answer from a legal case-research assistant against a reference \
answer written by a human who checked the source records.

correctness (1-5)
  5  every factual claim agrees with the reference
  4  agrees, with a trivial omission or imprecision
  3  mostly right but one material fact is wrong, vague, or misattributed
  2  a central claim contradicts the reference
  1  wrong, or answers a different question

completeness (1-5)
  5  covers everything the question asked
  4  covers the main ask, omits a secondary part
  3  answers roughly half
  2  touches the topic without answering
  1  does not address the question

Judge only against the reference. Extra true detail is not a penalty; extra \
detail that contradicts the reference is. If the reference says something does \
not exist and the answer agrees, that is a 5.

For any score below 4 you must quote the exact span of the answer responsible, \
verbatim. If you cannot quote it, the score is 4 or 5."""

_GROUNDEDNESS_SYSTEM = """\
You check whether an answer is supported by the evidence excerpts supplied. You \
do not know whether the answer is correct and must not guess — judge support only.

groundedness (1-5)
  5  every specific claim (dates, amounts, names, numbers, events) traces to the evidence
  4  fully supported except for uncontroversial framing
  3  one specific claim has no support in the evidence
  2  several specific claims are unsupported
  1  the central claim is unsupported or contradicts the evidence

Each excerpt is headed by its source id, timestamp and title. That header is \
part of the evidence: an answer that states the date, time or type of a cited \
record is supported by it.

An explicit statement that the record does not contain something is grounded \
when the evidence is consistent with that absence. General legal or procedural \
framing is not a claim about this case and does not need support.

For any score below 4 you must quote the exact unsupported span, verbatim. If \
you cannot quote it, the score is 4 or 5."""


class CorrectnessVerdict(BaseModel):
    correctness: int = Field(ge=1, le=5)
    completeness: int = Field(ge=1, le=5)
    offending_quotes: list[str] = Field(
        default_factory=list,
        description="Verbatim spans from the answer justifying any score below 4.",
    )
    reason: str = ""


class GroundednessVerdict(BaseModel):
    groundedness: int = Field(ge=1, le=5)
    unsupported_quotes: list[str] = Field(
        default_factory=list,
        description="Verbatim spans from the answer with no support in the evidence.",
    )
    reason: str = ""


@dataclass
class Judgement:
    correctness: int = 0
    completeness: int = 0
    groundedness: int = 0
    quotes: list[str] | None = None
    reason: str = ""
    ok: bool = True

    @property
    def composite(self) -> float:
        """One 1–5 number, weighted."""
        return round(
            self.correctness * WEIGHTS["correctness"]
            + self.groundedness * WEIGHTS["groundedness"]
            + self.completeness * WEIGHTS["completeness"],
            3,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "correctness": self.correctness,
            "completeness": self.completeness,
            "groundedness": self.groundedness,
            "composite": self.composite,
            "quotes": self.quotes or [],
            "reason": self.reason,
            "ok": self.ok,
        }


def _render_evidence(citations: Sequence[dict[str, Any]], limit: int = 30) -> str:
    if not citations:
        return "(no evidence was retrieved)"
    lines = []
    for citation in citations[:limit]:
        where = citation.get("document_name") or citation.get("ref")
        page = f" p.{citation['page_start']}" if citation.get("page_start") else ""
        # Full timestamp, not just the date: an answer that says "19:04"
        # is grounded in the record's metadata, and truncating it here
        # made the judge mark correct answers unsupported.
        when = (citation.get("when") or "").replace("T", " ")[:19]
        lines.append(
            f"[{where}{page}] {when} {citation.get('label', '')}\n{citation.get('excerpt', '')}"
        )
    return "\n\n".join(lines)


def judge_answer(
    llm: LLM,
    *,
    question: str,
    answer: str,
    gold_answer: str,
    citations: Sequence[dict[str, Any]],
) -> Judgement:
    """Two independent calls, combined."""
    correctness = llm.structured(
        CorrectnessVerdict,
        system=_CORRECTNESS_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": (
                    f"<question>{question}</question>\n\n"
                    f"<reference_answer>\n{gold_answer}\n</reference_answer>\n\n"
                    f"<answer_under_test>\n{answer}\n</answer_under_test>"
                ),
            }
        ],
        effort=config.EFFORT_JUDGE,
        max_tokens=4000,
    )
    grounded = llm.structured(
        GroundednessVerdict,
        system=_GROUNDEDNESS_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": (
                    f"<question>{question}</question>\n\n"
                    f"<answer_under_test>\n{answer}\n</answer_under_test>\n\n"
                    f"<evidence>\n{_render_evidence(citations)}\n</evidence>"
                ),
            }
        ],
        effort=config.EFFORT_JUDGE,
        max_tokens=4000,
    )

    if correctness is None and grounded is None:
        return Judgement(ok=False, reason="judge unavailable")

    quotes = list(correctness.offending_quotes if correctness else [])
    quotes += list(grounded.unsupported_quotes if grounded else [])
    reason = " | ".join(
        part
        for part in [correctness.reason if correctness else "", grounded.reason if grounded else ""]
        if part
    )
    return Judgement(
        correctness=correctness.correctness if correctness else 0,
        completeness=correctness.completeness if correctness else 0,
        groundedness=grounded.groundedness if grounded else 0,
        quotes=quotes,
        reason=reason,
        ok=True,
    )


# --------------------------------------------------------------------------
# Calibration controls
# --------------------------------------------------------------------------

# Each control is an answer whose correct grade is known in advance. They run
# in the same harness as the real items; the report prints how many the judge
# graded as expected. This is the difference between "we used an LLM judge"
# and "we validated our LLM judge".
CONTROLS: list[dict[str, Any]] = [
    {
        "id": "ctrl_flatly_wrong",
        "question": "When was the petition filed?",
        "gold_answer": "It was e-filed on 2026-08-31 through the Florida Courts E-Filing Portal.",
        "answer": "The petition was filed on 2025-02-14 in Orange County, Texas.",
        "citations": [
            {
                "ref": "E124",
                "when": "2026-08-31",
                "label": "E-filing notice",
                "excerpt": "Filing #074038651 E-Filed 08/31/2026, Manatee County, Florida.",
            }
        ],
        "expect": {"correctness": "low", "groundedness": "low"},
    },
    {
        "id": "ctrl_correct_but_unsupported",
        "question": "When was the petition filed?",
        "gold_answer": "It was e-filed on 2026-08-31 through the Florida Courts E-Filing Portal.",
        "answer": "The petition was e-filed on 2026-08-31 through the Florida Courts E-Filing Portal.",
        "citations": [
            {
                "ref": "E210",
                "when": "2026-09-08",
                "label": "Email — filing fee",
                "excerpt": "Filing amount: $423.60. Please note this cost is not included in your agreement.",
            }
        ],
        # Right against the reference, unsupported by the evidence shown.
        "expect": {"correctness": "high", "groundedness": "low"},
    },
    {
        "id": "ctrl_fluent_irrelevant_citation",
        "question": "Where was the opposing party served?",
        "gold_answer": "At 1087 Blue Heron Way, Sarasota, FL 34234.",
        "answer": "She was served at her workplace on Tamiami Trail in Sarasota [E150].",
        "citations": [
            {
                "ref": "E150",
                "when": "2026-09-01",
                "label": "Serving job created",
                "excerpt": "Recipient: Nadia Tessa Sorlie. Service addresses: 1087 Blue Heron Way, Sarasota, FL 34234 (HOME, primary).",
            }
        ],
        "expect": {"correctness": "low", "groundedness": "low"},
    },
    {
        "id": "ctrl_good_answer",
        "question": "When did we last hear from the client?",
        "gold_answer": "2026-09-08 at 19:04, an inbound email about the filing fee.",
        "answer": "2026-09-08 at 19:04 UTC — an inbound email asking what to do about the filing fee before payday [E213].",
        "citations": [
            {
                "ref": "E213",
                "when": "2026-09-08T19:04:49",
                "label": "Email — Re: Initial Documents",
                "excerpt": "Hello Mrs. Dana I just received the thing for paying to filed with the court. I get paid next Thursday what should I do?",
            }
        ],
        "expect": {"correctness": "high", "groundedness": "high"},
    },
]


def run_controls(llm: LLM) -> dict[str, Any]:
    """Grade the controls and report how often the judge behaved as expected."""
    results = []
    for control in CONTROLS:
        judgement = judge_answer(
            llm,
            question=control["question"],
            answer=control["answer"],
            gold_answer=control["gold_answer"],
            citations=control["citations"],
        )
        checks = {}
        for dimension, expectation in control["expect"].items():
            score = getattr(judgement, dimension)
            checks[dimension] = (score >= 4) if expectation == "high" else (score <= 3)
        results.append(
            {
                "id": control["id"],
                "expected": control["expect"],
                "scored": judgement.to_dict(),
                "checks": checks,
                "passed": all(checks.values()),
            }
        )
    passed = sum(1 for r in results if r["passed"])
    return {
        "results": results,
        "passed": passed,
        "total": len(results),
        "accuracy": round(passed / len(results), 3) if results else 0.0,
    }
