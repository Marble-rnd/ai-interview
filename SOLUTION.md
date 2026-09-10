# Case Research Agent

A conversational agent attorneys can ask questions about a family-law case. It
answers from the case record only, cites the timeline event or document page
behind every claim, and says so plainly when the record does not contain the
answer.

```bash
cp .env.example .env            # ANTHROPIC_API_KEY=...
uv sync
uv run python -m src.ingest     # build the index for both cases (~4 min, ~$1)

uv run uvicorn src.api.server:app --port 8000   # web UI at localhost:8000
uv run python -m src.cli --case case_001        # or the terminal REPL
```

Already-generated output, so none of this has to be run to review it:
[`demo/transcript.md`](demo/transcript.md) ·
[`evals/results/agent.md`](evals/results/agent.md) ·
[`evals/results/retrieval.md`](evals/results/retrieval.md)

---

## The shape of the problem

The interesting difficulty is not the agent loop, it is the data. A case
timeline is the merged log of six source systems, and it lies to you in
specific, documented ways:

- `content` is a useless one-liner for whole classes of event — every
  `Data Entries` row says "Client data updated" and the payload is in
  `extra_details.data_points` under internal field ids like
  `opposingPartyBestTimesToFind`.
- Calendar entries are **internal firm meetings**, never court hearings.
- Half the outbound traffic is Braze marketing and templated SMS, so "when did
  we last contact the client" has a wrong answer that looks right.
- Call transcripts contain transcription errors, so names and addresses in them
  must not be quoted.
- A request marked *cancelled* is not outstanding — the firm withdrew it.
- The `documents/` folder holds a **subset** of what was uploaded, under
  different filenames.
- Six PDFs in case_001 are image-only scans with no text layer. The docket
  number `2486DR7193` and the twenty-day response window exist **only** there.

The provided `src/tools.py` substring-matches `content` and nothing else, so it
cannot reach any of that.

**The bet:** put those rules in code, once, as a semantic layer — then a
straightforward agent on top is enough. Every rule above is a mechanism in this
repo, not a line in a prompt, and each has a test.

---

## Architecture

Two phases, which is the load-bearing decision.

```
                    OFFLINE  (src/ingest)                    ONLINE  (src/agent)

  case-timeline.csv ─┐                                    ┌─ timeline skeleton (cached prefix)
                     ├─ normalize (7 shape adapters)      │
  documents/ ────────┤  ├─ PII redaction                  ├─ 8 tools bound to one case
                     │  ├─ actor roster                   │
                     │  └─ automation / fidelity flags    ├─ LangGraph: 9 stages, ≤3 LLM calls
                     ├─ extract text · Claude vision OCR  │
                     ├─ chunk (event / transcript / page) └─ deterministic lint + citations
                     ├─ Claude contextual blurbs (batched)
                     ├─ embed (local ONNX) + BM25
                     └─► index/{case}/{version}/  ◄────────── read-only at query time
                           events · chunks · vectors
                           brief · roster · catalog · manifest
```

Query time never opens a CSV or a PDF. Ingestion is O(case) and parallel across
cases; the expensive model work (OCR, contextual blurbs) is paid once and
amortized over every question afterwards. `manifest.json` records a content hash
of every source file, so re-ingesting an unchanged case is a no-op and a stale
index is detectable rather than silently wrong.

```
$ uv run python -m src.ingest
case_001: 214 events, 296 chunks (220 event / 76 document), 21 documents
          (21 recorded but not in snapshot), 30 OCR pages, 76 new blurbs in 112s
case_002: 190 events, 353 chunks (199 event / 154 document), 10 documents
          (4 recorded but not in snapshot), 0 OCR pages, 142 new blurbs in 148s
ingest model spend: 218 calls, 368,184 in / 19,308 out, cache hit 79%, $0.95
```

---

## The semantic layer

`NormalizedEvent` replaces 33 raw row shapes with one. Adapters are keyed by
**shape**, not by subtype — message-like, document-like, request-like,
milestone-like, data-entry-like, calendar-like, serving-like, note-like — with
`(type, subtype)` → `type` → a loud default fallthrough.

That is what makes the generalization claim testable rather than aspirational.
The data dictionary documents nine event types that appear in **neither**
bundled case (`Task/Legal_Task`, `LSS Event`, `Legal Cortex/Case Update`, …).
Two tests cover it: one asserts every `(type, subtype)` present in both CSVs
resolves to a real adapter (currently 0 fall through), and one pushes a
synthetic `Task/Legal_Task` row through the default path and asserts it comes
out readable and searchable.

**Rules enforced as mechanism, each with a test:**

| Rule | Mechanism |
|---|---|
| Secrets never reach the model, the logs, or the index | Redacted in the `NormalizedEvent` constructor, at ingest — not at output. `clientSSN` is encrypted at source; the ciphertext is replaced with a marker so the agent can still say one is on file. |
| Transcripts are not quotable | `low_fidelity` flag on the event; the tool layer attaches an explicit warning to every transcript excerpt it returns. |
| A calendar entry is not a hearing | The adapter writes it into the headline itself, and an output lint fails any answer that *asserts* a hearing (negation-aware, so "this is not a court hearing" passes). |
| Automated ≠ contact | `is_automated` from Braze `source`/`flow_name`, SMS `template_name`, or a non-human sender. case_001 has 63 messages, of which only 20 are human. |
| An untranscribed call still happened | `has_silent_body` when the body is empty but `start_date != end_date`. |
| No raw UUIDs in prose | Ids are clustered into a per-case roster at ingest; a lint catches any bare 24-hex id that escapes. |
| Internal field ids rendered in English | `opposingPartyBestTimesToFind` → "Opposing party best times to find", with overrides for the ones a generic splitter gets wrong. |

### The deterministic case brief

Status questions are arithmetic, not reading comprehension, so they are computed
at ingest: parties, jurisdiction, docket number, services, contact recency, open
requests, and **deadline math**. For case_001 that turns "what's the status?"
into:

> Opposing party served 2026-09-04 (2nd attempt); return of service 2026-09-07;
> 20-day window under Fla. Fam. L. R. P. 12.140 closes 2026-09-24; no response on
> record.

Two things this caught that a naive reading gets wrong:

- The case status row literally says **"Case completed"**. It refers to the
  purchased service; five follow-on services (Clerk Default, Mandatory
  Disclosures, Mediation, Motion for Final Judgment after Default, Answer to
  Petition) open in the same minute. The brief carries a `status_detail` that
  spells that out, so the agent cannot tell an attorney the matter is over.
- Three sources disagree on jurisdiction — the intake funnel says Hillsborough,
  the intake call says Sarasota, the filed petition says **Manatee**. A filed
  document outranks intake and transcript, so the brief reads Manatee.

---

## Retrieval

Two complementary layers.

**1. The whole timeline sits in the cached prefix as an index.** One line per
event, ~8k tokens, headlines only — no bodies, no document text:

```
E166 | 2026-09-04 16:35 | Serving/Serving job | the system | Service of process — attempt 2: SUCCEEDED [auto]
```

The model can see that an event exists before deciding to fetch it, which
removes the failure mode where a retriever silently misses something the model
never knew to look for. It also makes several questions free — "when is the next
court hearing?" is answered in **4.5s for $0.015 with zero tool calls**, at 99%
cache hit, because the answer is visible in the skeleton.

**2. Hybrid RAG for anything with a body.** Documents are chunked page-aware
with page attribution, transcripts on speaker turns (one is 38,000 characters),
short events kept atomic. Fusion is RRF (k=60) because BM25 scores are unbounded
and cosine is not, then a Claude listwise rerank.

### What the ablation actually showed

`uv run python -m evals.run --retrieval-only`, scored generation-free against
hand-labelled gold evidence at k=10, on the 10 dataset items where similarity
search is genuinely the right mechanism (filter- and brief-shaped questions are
excluded — ranking a "most recent inbound message" question measures the wrong
thing):

| mode | recall@10 | precision@10 | MRR | total misses |
|---|---|---|---|---|
| BM25 (raw chunk text) | 69% | 18% | 0.653 | 1 |
| BM25 + Claude context blurb | 62% | 16% | 0.575 | 1 |
| Dense (bge-small, local ONNX) | 61% | 22% | 0.625 | 2 |
| Hybrid RRF (raw BM25 + dense) | 64% | 17% | 0.639 | 1 |
| **Hybrid RRF (contextual BM25 + dense)** — default | 64% | 22% | 0.703 | 1 |
| **+ Claude listwise rerank** | **81%** | **38%** | **0.858** | **0** |

Two findings worth stating plainly:

- **Claude reranking is the component that matters** — +17pts recall, +16pts
  precision, +0.16 MRR, and it eliminates total misses entirely. It costs ~2.5s.
- **Contextual blurbs made BM25 *worse* on its own** (69% → 62%), which is the
  opposite of the published result. The reason is visible in the blurbs: every
  one names the parties, the court and the matter type, so on the sparse side
  every document chunk starts matching every query and pushes gold *events* out
  of the top-k. Fused with dense, where RRF works on ranks, the same text is the
  best non-reranked configuration measured. Both variants are kept as ablation
  rows so this stays a measurement rather than folklore.

### Why a local ONNX model, given we have Claude

Claude cannot emit an embedding vector, and there is no Voyage or OpenAI key
here — so for the dense signal specifically, a local model is the only option.
It is deliberately the *least* important component: Claude does the OCR, the
contextual blurbs, and the reranking, which is where the accuracy actually
moved. If the model cannot be fetched, retrieval degrades to contextual BM25 +
Claude rerank with a warning rather than failing.

There is no vector database. A few thousand rows of 384 floats is one numpy
dot product; introducing Qdrant at this size would add operational surface
without changing a single result. The `Retriever` interface is the part designed
to survive that swap.

---

## The agent

LangGraph owns the graph — the validator retry cycle, and the checkpointed
thread that makes follow-ups work. Model calls go through one thin wrapper over
the raw Anthropic SDK, **not** `ChatAnthropic` and **not** `create_react_agent`:
we need adaptive thinking, per-call `effort`, explicit `cache_control`
breakpoints and clean `usage` accounting, and `create_react_agent` would hide
the agent loop, which is the thing being evaluated.

Nine stages, of which at most three call a model:

| # | stage | LLM | what it does |
|---|---|---|---|
| 1 | `guardrail_in` | — | scope check; **case binding is structural** — tools are constructed against one artifact and take no case argument, so cross-case leakage is impossible rather than classifier-dependent |
| 2 | `context_builder` | — | the precomputed brief |
| 3 | `planner` | low, conditional | skipped by heuristic for simple lookups — tool use *is* planning, and an extra Opus turn on a 10k prefix to derive a hint the executor would find anyway is pure latency |
| 4 | `tool_fetcher` | — | prefetches what the plan implies. Deliberately does **not** narrow the tool list: caching matches a `tools → system → messages` prefix, so a per-question tool set would invalidate the cached prefix every turn |
| 5 | `executor` | high | plain `while` over `stop_reason == "tool_use"`, parallel calls, all results returned in one user message |
| 6 | `validator` | low, conditional | deterministic lint first; the model pass runs only while it can still change the outcome |
| 7 | `evidence` | — | citation pack, including citations the model made from the cached prefix without a tool call |
| 8 | `verdict` | — | answered / partial / unknown + confidence |
| 9 | `output` | — | redaction as defence in depth |

**Eight tools**, static and bound to one case: `get_case_brief`, `get_events`
(exact filter), `search_case` (hybrid RAG), `get_event_detail`, `list_documents`,
`read_document`, `get_contact_history`, `get_outstanding_items`.

Filters and ranking are separate paths on purpose. "Everything in the last two
weeks" is a `where` clause; routing it through a ranker turns a complete answer
into a plausible one, so the tool descriptions push the model to `get_events`
whenever a predicate would do.

`read_document` never returns a whole file — one document here is 85 pages. It
takes a query and returns page-attributed passages, or an explicit page range.

### Two bugs worth calling out

Both were found by running the thing, and both would have shipped silently.

**The validator was punishing correct answers.** The LLM validator judged
groundedness against a truncated evidence pack, so it marked well-cited claims
unsupported, forced a retry, and surfaced them to the attorney as caveats. The
fix separates the two signals by kind: **exact checks gate, fuzzy checks
calibrate.** Deterministic lint (does this citation resolve? does this date
appear in retrieved evidence?) drives the retry and is safe to show a user; the
model's opinion only moves the confidence label. On "when is the next court
hearing?" that took the answer from 56.7s / $0.51 / 2 attempts with five bogus
caveats, to 4.5s / $0.015 / clean.

**Haiku 4.5 rejects `adaptive thinking` and `effort` with a 400**, and a broad
`except` in the enrichment path swallowed it — the first ingest produced an
index with *zero* contextual blurbs that looked completely successful. Fixed by
gating those parameters on model capability and making the failure loud. The
lesson is in `llm.py` as a comment, because it will happen again with the next
auxiliary model.

---

## Evaluation

Two tiers, kept separate so the cheap one can run on every commit and a flaky
judge call cannot red the suite.

```bash
uv run python -m evals.run --retrieval-only   # ablation, no agent, no judge
uv run python -m evals.run --tier1            # agent + deterministic metrics
uv run python -m evals.run --all              # + LLM judge and its controls
```

**24 items** across both cases covering the five required question types plus
the traps this data sets. Every gold value was verified by hand against the raw
CSV before the pipeline existed. The clock is pinned so "the last two weeks" is
reproducible.

**Tier 1 — deterministic.** Retrieval recall/precision/MRR measured
*independently of generation*; the mode ablation; tool-selection recall,
precision and **argument correctness** (did `get_events` actually set
`date_from` for a date-window question?); citation precision and recall against
gold evidence; abstention correctness as its own number; per-question latency,
tokens, cache hit rate and cost from the JSONL traces every run writes.

**Tier 2 — the judge, with the judge itself under test.** Correctness,
completeness and groundedness, 1–5 each, weighted into one composite. Four
things stop it rubber-stamping:

1. **It sees less than you would expect.** Groundedness sees `(question, answer,
   evidence)` and never the gold answer; correctness sees `(question, answer,
   gold)` and never the evidence. Separate calls, so neither haloes the other.
2. **Every deduction must quote** a verbatim span. A judge that has to point at
   the words cannot hand-wave.
3. **Poisoned controls** run alongside the real items — a flatly wrong answer, a
   correct-but-unsupported answer, and a fluent answer citing a real event that
   does not support its claim. The report prints how many the judge caught.
4. Judge failures are recorded as gaps, never as passes.

Results: [`evals/results/agent.md`](evals/results/agent.md) and
[`evals/results/retrieval.md`](evals/results/retrieval.md), both committed.

---

## Interface

A web chat at `localhost:8000` — case picker, live brief with the computed
deadline and silence counters, and a conversation that keeps context.

What makes it useful rather than decorative is the evidence drawer. Citations
render inline as chips; clicking one opens the timeline record (full body,
structured fields, transcript warning where it applies) or the document — the
**rendered page image** plus its indexed text, including pages that only exist
because they were OCR'd at ingest. A citation you can click through to the page
is proof; one you have to take on trust is decoration.

Progress streams as SSE. What streams is the *work* — "searching the timeline",
"reading standing_family_law_order.pdf" — not tokens, because for a multi-step
agent that says more about what is happening.

Same `CaseSession` behind the UI, the CLI and the eval harness, so there is one
code path to trust.

---

## Known limitations

- **Deadline arithmetic covers two jurisdictions.** Florida and California
  response windows are in a table; anything else produces no computed deadline
  rather than a guess. It is labelled "computed — verify against local rules"
  wherever it appears, because an attorney should not rely on it unchecked.
- **The gold sets use `E###` refs**, which are positional. They are stable for a
  given CSV (and the manifest hashes it), but inserting a row would shift them.
  A production version would key on the row UUID.
- **The retrieval ablation is 10 items.** Enough to separate reranking from the
  rest decisively; not enough to tune RRF's `k` or a convex-combination alpha,
  which is why neither is tuned.
- **Latency is 15–25s for questions needing real research.** The floor is the
  Opus tool loop. Prefetching, the cached prefix, the conditional planner and
  skipping the final-attempt validator got the easy wins; further reduction
  means a cheaper executor model, which trades the accuracy this is graded on.
- **One in-process session map** behind the API. Fine for one attorney, wrong
  for a fleet.
