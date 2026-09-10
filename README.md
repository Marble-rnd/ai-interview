# Case Research Agent — AI Engineer Exercise

## Context

You're joining the AI team at a legal tech company that provides family law and immigration legal services at scale. Attorneys on the platform handle hundreds of active cases simultaneously and need help researching case details quickly.

Today, when an attorney needs to answer a question about a case — "When is the next court date?", "Has the client responded to the financial disclosure request?", "What documents are still missing?" — they have to manually search through timeline logs and documents. This is slow and error-prone.

## Your Task

**Build a conversational agent that attorneys can ask questions about a case.**

In the `case_data/` directory you'll find two real case snapshots. Every person in them has been given a fictional identity (names, contact details, addresses, account and case numbers); dates, amounts, courts and the sequence of events are real.

- **case_001**: A Florida divorce with a young child — ~214 timeline events, 21 documents. The petition has been filed and served; the response window is running and the team is preparing for default, disclosures or mediation.
- **case_002**: A California agreed divorce with a teenage daughter — ~190 timeline events, 10 documents. Early stage: the client signed on and paid, the FL-100 family of court forms has been auto-drafted, and the client has started uploading financial disclosures; the kickoff call with the attorney is still being scheduled.

Each case directory contains:
- `case-timeline.csv` — Chronological log of all case activity (emails, calls, SMS, documents, status changes, tasks, etc.)
- `documents/` — Raw case documents (PDFs, DOCXs, images)

## What We Provide

- `src/data_loader.py` — Utilities to load timeline data and list documents
- `src/tools.py` — Starter tool implementations and OpenAI-format tool definitions
- API keys for OpenAI and Anthropic (in `.env`)

**The provided code is strictly optional scaffolding.** You are free to use it, modify it, replace it entirely, or ignore it. Use any tools, libraries, or approach you'd like. You have full access to the case data and can perform any operations on it — including using LLMs — however you see fit.

## Requirements

1. The agent should be able to answer questions about a case using the available tools
2. It should handle at least these question types:
   - Factual lookup: "When was the petition filed?"
   - Status/progress: "What's the current status of the case?"
   - Communication history: "When was the last time the client was contacted?"
   - Document-related: "What documents have been uploaded?"
   - Synthesis: "Summarize the key events in the last two weeks"
3. Demonstrate it working on both cases with at least 5 different questions

## What We're Looking For

- **Tool and agent design** — How you structure the tools, system prompt, and agent loop
- **Retrieval quality** — Does the agent find the right information to answer the question?
- **Engineering quality** — Clean code that we'd be comfortable shipping
- **Product sense** — Does the agent behave the way an attorney would want it to?

## Timeline Column Reference

| Column | Description |
|---|---|
| `id` | Unique event identifier (UUID) |
| `activity_created_at` | When the event was created (chronological sort key) |
| `start_date` | When the activity occurred (can be future for scheduled events) |
| `activity_type` | Category: Communications, Documents, Task, Service, Case Update, etc. |
| `activity_sub_type` | Specific type: Email, SMS, Call, Document_Upload, Legal_Task, etc. |
| `direction` | Inbound, Outbound, or Internal |
| `content` | The event content (email body, call transcript, note text, etc.) |
| `extra_details` | JSON metadata (varies by event type — may contain S3 keys, phone numbers, email addresses, etc.) |
| `int_user_marble_id` | Internal staff member ID (attorney, paralegal, admin) |
| `ext_user_marble_id` | External user ID (client) |

## Getting Started

```bash
# Install dependencies
uv sync

# Copy and fill in API keys
cp .env.example .env

# Explore the data
uv run python -c "from src.data_loader import *; print(list_cases()); print(len(load_timeline('case_data/case_001')))"
```

## Confidentiality

The case data comes from real matters whose people were replaced with fictional identities. Any resemblance of a name, address or contact detail to a real person is coincidental; do not try to re-identify anyone.
