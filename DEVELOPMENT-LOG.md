# Development log

How this was built, and specifically where the direction changed. Written
because the interesting part of the process was not the code — it was four
course corrections, three of which came from the reviewer pushing back on my
first answer, and one from a measurement contradicting my assumption.

Roles below: **[steer]** is direction from the reviewer, **[me]** is what I
did with it.

---

## 1. Read the data before designing anything

**[steer]** *"first read the files and the readme, then see the code and the
data itself, and explain me clearly and in simple words exactly what the task
is."*

Before proposing anything I profiled both cases: 33 distinct
`activity_type`/`activity_sub_type` combinations, `extra_details` with a
different schema per subtype, ~39k tokens of `content` plus ~50k of
`extra_details` per case, and six PDFs in case_001 with **zero fonts and zero
text operators** — pure scans.

That pass produced the findings the whole design later rested on: the starter
`search_timeline` only reads `content`, which is a generic one-liner for whole
classes of event; `read_document` returns a path, not text; and the
`documents/` folder is a subset of what the timeline says was uploaded.

**Worth keeping:** the ground truth for the eval set was established here, by
hand, against the raw CSV — *before* any pipeline existed. That is why the
gold data is independent of the system under test.

## 2. Asking for the data dictionary

**[steer]** *"I ADDED ONE FILE timeline-reference as more context to
understand the data."*

`case_data/TIMELINE-REFERENCE.md` turned out to be the same reference the
firm's production AI systems use. It documents rules that are invisible in the
data itself and that a reasonable reading gets wrong:

- Calendar entries are internal firm meetings, never court hearings —
  "confusing the two falsely inflates urgency."
- Braze-sourced email and templated SMS are automation, not contact.
- Call transcripts contain transcription errors; never quote names or
  addresses from them.
- A `Data Requests` entry marked *cancelled* is not outstanding.
- `Service Draft` is a form workflow, not document drafting.

**[me]** I cross-checked every claim against both CSVs and found the
reference over-describes our snapshots: nine documented event types
(`Task/Legal_Task`, `LSS Event`, `Legal Cortex/Case Update`, …) appear in
neither case. That is what pushed the adapter registry toward keying on
*shape* with a loud default, and produced the test that feeds a synthetic
`Task/Legal_Task` row through the default path.

**This is the decision that shaped the codebase.** Every rule above became a
mechanism with a test, not a line in a prompt.

## 3. "vector + bm25 might be a good solution?"

**[steer]** *"also direction for the docs, events fetcher, vector + bm25 might
be a good solution?"* and *"because we have big files, a rag and chunking the
file can be a better fit than loading the whole document to the model as a
context to prevent hallucinations."*

My plan at that point leaned on Opus 5's 1M context: put a digest of the whole
case in a cached prefix and pass documents as whole `document` blocks. The
reviewer was right that this does not hold for documents — case_002 contains
an 85-page tax return, and loading it whole is both expensive and
attention-diluting.

**[me]** Rewrote the retrieval plan: page-aware chunking with page
attribution, speaker-turn chunking for the 38,000-character call transcript,
BM25 + dense fused with RRF. The cached skeleton survived but was reframed
honestly — it is an **index** (one headline line per event, no bodies), not
content. The two are complementary: the skeleton means the model never has to
search to discover that something exists; RAG supplies the bodies.

## 4. "onnx for what? we have llm provider such as claude"

**[steer]** *"onnx for what? we have llm provider for better accuracy such as
claude."*

A fair challenge that I had to answer with a fact rather than a preference:
**Claude cannot emit an embedding vector.** Anthropic ships no embeddings
endpoint, its recommended partner is Voyage, and `.env` has neither a Voyage
nor an OpenAI key. For the dense signal specifically a local model is the only
option available.

But the challenge was still correct about the *emphasis*. I had Claude doing
almost nothing while a 45MB ONNX model did the interesting work. Rebalanced:

| job | who | why |
|---|---|---|
| Contextual blurbs per chunk (offline, batched) | **Claude** | the technique with the largest published gain |
| OCR of the six scanned PDFs | **Claude vision** | best OCR available, already in the stack |
| Reranking fused candidates | **Claude** | a reading task, not a lexical one |
| Dense vectors | ONNX | nothing else can produce them |

And rather than assert the balance was right, I made the eval decide it — six
retrieval modes, scored generation-free.

## 5. "think in terms of scale"

**[steer]** *"we might wanna preprocess and index the data before in order to
do a better retrieval instead of facing with files."*

**[me]** Split the system in two. `src/ingest` is an offline pipeline that
emits a versioned, content-hashed index artifact per case; `src/agent` only
ever reads it. Query time never opens a CSV or a PDF. `manifest.json` records
a hash of every source file plus the models and prompt versions used, so
re-ingesting an unchanged case is a no-op and a stale index is detectable
rather than silently wrong.

That is also what makes the expensive work affordable: OCR and contextual
blurbs cost $0.95 once for both cases and are amortized over every question
asked afterwards.

## 6. Eval scope

**[steer]** *"eval should cover all steps — fetcher tools, fetch the right
docs (recall, precision, mrr), llm as a judge with 3 metrics into one score
(correctness, completeness, groundedness, 1–5 each), include logs of
observability for evals, for latency and for token usage per query, also for
tools eval recall to choose the right tools with the right parameters."*

Implemented as specified, in two tiers so the cheap one can run on every
commit. Two additions beyond the brief, both because the brief's metrics are
only trustworthy if the measurement is:

- **Retrieval scored independently of generation.** A failure there is an
  index problem; a failure only end-to-end is a prompt problem.
- **The judge is itself under test.** Four poisoned controls with
  known-correct grades run alongside the real items, and the report prints how
  many the judge caught.

**[steer]** *"nice chatbot interface with great ui to play with the tool. give
the user the ability to choose the right case and ask questions with citations
of the docs as a proof."*

Built as a case picker, a live brief, and an evidence drawer where a citation
opens the **rendered page image** of the document it came from — including
pages that exist only because they were OCR'd at ingest.

---

## What measurement changed my mind

### Contextual blurbs made BM25 worse

I expected Anthropic's Contextual Retrieval result to reproduce. It did not,
on the sparse side:

| mode | recall@10 | MRR |
|---|---|---|
| BM25 raw | 69% | 0.653 |
| BM25 + Claude blurb | 62% | 0.575 |

The cause is visible in the blurbs: every one names the parties, the court and
the matter type, so on the sparse side every *document* chunk starts matching
every query and pushes gold *events* out of the top-k. Fused with dense, where
RRF works on ranks, the same text is the best non-reranked configuration.
Both variants stayed in the ablation table so the finding is a measurement
rather than folklore.

### The first ablation measured the wrong thing

My first run scored all 23 items and produced mush. Several of them — "what is
the most recent inbound message?" — are exact *filter* questions; ranking them
measures nothing. Tagging each dataset item with the mechanism its answer
actually depends on (`ranked` / `filter` / `brief`) and scoring the ablation on
the 10 `ranked` items separated the modes cleanly, and showed reranking is the
component that matters: recall 64%→81%, precision 22%→39%, MRR 0.703→0.858,
total misses 1→0.

---

## Bugs the build caught that would otherwise have shipped

Each of these was found by running the thing, and each looked like success
from the outside.

| Bug | What it would have done |
|---|---|
| Haiku 4.5 rejects `adaptive thinking` and `effort` with a 400, and a broad `except` swallowed it | The first ingest produced an index with **zero** contextual blurbs and reported success. Fixed by gating those parameters on model capability and making the failure loud. |
| `actor_id` was set to whichever id the row carried, not the acting side's | An inbound client call also names the staff member who handled it, so the attorney was merged into the client in the roster. |
| The document matcher stripped all digits as "noise" | Every `FL-1xx` form collapsed to `fl` — FL-150 matched FL-140's file. Form numbers *are* identity. |
| Greedy per-event document matching | Early weak matches stole files that later sources matched better; replaced with global best-first assignment. |
| The LLM validator judged groundedness against a truncated evidence pack | It marked well-cited claims unsupported, forced retries on correct answers, and surfaced them to the attorney as caveats. Split the signals: exact checks gate, fuzzy checks only calibrate confidence. "When is the next court hearing?" went from 56.7s / $0.51 / five bogus caveats to 4.5s / $0.015 / clean. |
| Event evidence excerpts dropped `facts` | For `Data Entries` and `Serving` — the event types whose body is generic and whose payload is entirely in `facts` — the judge could not see the very fact the answer cited. Groundedness rose from 3.04 to 3.42 once fixed. |
| Five concurrent eval workers rate-limited each other | Four items stalled at ~610s (a 600s client timeout plus a retry). p95 was 633s and meaningless. Lowered the client timeout to 180s and the default worker count to 3; p95 is now ~32s. |
| Negation-blind assertions — three times | A lint flagged "this is **not** a court hearing"; an assertion flagged "there's **nothing** the attorney advised on a call"; another matched "Report" inside the filename `Payslip_to_Print_-_Report_Design_09_05_2026.pdf`. All three were the harness calling a correct answer wrong. |

That last row is the one I would take to the next project: when a check fires
on a *correct* answer, the check is usually the thing that is broken, and the
fix is to make it context-aware rather than to relax it.

---

## Where I diverged from my own plan, and why

- **`strict: true` on tool schemas.** Planned, then removed: strict mode
  requires every property in `required`, so optional filters become null
  unions, and this tool surface has twenty of them — the API rejects it
  outright (limit is 16). Argument hygiene moved into the dispatcher, and the
  schemas got easier for the model to read as a side effect.
- **Dynamic tool narrowing in `tool_fetcher`.** Planned, then dropped: prompt
  caching matches a `tools → system → messages` prefix, so a per-question tool
  set would invalidate the ~10k-token cached prefix on every turn. The stage
  now prefetches results instead, and the tool list stays static.
- **LangGraph.** An independent design review argued for dropping it as "a
  `while` loop with extra imports". Kept, narrowly — it owns the validator
  retry cycle — but model calls go through the raw Anthropic SDK, not
  `ChatAnthropic`, and `create_react_agent` was never on the table because it
  hides the agent loop, which is the thing being evaluated.

  I also had to correct my own claim about it. I wrote that the checkpointed
  thread was what made follow-ups work; it was not — conversation memory is
  an explicit `history` list replayed as plain text. Sharing one checkpoint
  thread across turns instead made LangGraph carry per-turn channels forward,
  so turn 2 inherited turn 1's evidence and a `RetrievalHint` came back as a
  dict and crashed the executor. The checkpoint is now scoped to a single
  turn, which is the only thing it was ever needed for.
