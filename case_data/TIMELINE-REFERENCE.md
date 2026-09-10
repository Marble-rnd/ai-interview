# Timeline Data Reference

> **Interviewer note (delete before sharing):** Hand this file to the candidate if they ask what the timeline data means — event types, columns, the `extra_details` JSON, who the actors are. Asking for this is a good sign (see EVALUATION.md). Everything below is candidate-safe: it describes the data model, not the cases.

---

This is the internal reference for `case-timeline.csv` — the same documentation our production AI systems use to interpret the timeline. It covers column semantics, actor identity, and per-event-type guidance on where the real information lives.

## Background: how a case works here

Marble provides limited-scope legal representation: clients buy specific **services** (e.g., "Petition for Divorce", "Adjustment of Status") rather than open-ended hourly representation. A typical journey: a lead comes in through the marketing funnel → sales intake call → **LSS** (a paid attorney consultation where strategy is outlined) → client signs the agreement and pays → **CKC** (kick-off call with the assigned case attorney) → the legal team delivers the service (drafting, client data collection, filing, serving). The timeline is the merged, chronological log of everything that happened across all these systems (Salesforce, the client portal, call platforms, document storage, internal tooling).

Glossary of terms you'll see in the data:

| Term | Meaning |
|---|---|
| **LSS** | Legal Strategy Session — the paid attorney consultation before signing. "LSS Intake Notes" are the notes taken there (case type, parties, goals). |
| **CKC** | Client Kick-off Call — first call between the client and their assigned attorney after signing. |
| **CC** | Co-Counsel — independent contractor attorneys in Marble's network who handle cases. |
| **CSM / Paralegal** | Case success manager / paralegal — the staff member driving day-to-day case delivery. |
| **OP** | Opposing party. |
| **Autopilot / Express** | Automated service-delivery model with minimal attorney involvement (vs. **Copilot**, attorney-led). |
| **Marbles** | A client-onboarding milestone checklist (gamification) — *not* people or money. |
| **Serving** | Service of process — delivering filed documents to the opposing party via a process server. |

## Columns

| Column | Semantics |
|---|---|
| `id` | Unique event identifier (UUID). |
| `activity_created_at` | When the event was created in the source system — **the correct chronological sort key**. |
| `start_date` | When the activity *occurs*. For scheduled-event types (Calendar Event, LSS Event) this is the scheduled meeting time and **can be in the future** relative to `activity_created_at` (a booking creates one row at booking time; completion/cancellation creates more rows later). For everything else it equals the event time. |
| `end_date` | For calls/meetings/chats: end of the event (duration = end − start). For point-in-time events: same as `start_date`. |
| `insert_timestamp` / `update_timestamp` | Data-warehouse bookkeeping (when the row was ingested/merged). Ignore for case narrative. |
| `case_id` | Marble's internal case identifier — NOT the court case number (those appear in content/documents). |
| `int_user_marble_id` | Internal staff actor (attorney, paralegal, admin). Opaque ID; same ID = same person across events. **Null/empty = automated or system-generated** (marketing emails, status changes). |
| `ext_user_marble_id` | External actor — usually the client, and usually the same ID across the whole case. |
| `activity_type` / `activity_sub_type` | Event category / specific type. See the per-type guide below. |
| `direction` | `Inbound` = client → Marble (questions, concerns, uploads). `Outbound` = Marble → client (guidance, updates). Empty = system events, documents, non-directional. |
| `content` | Primary text. **Quality varies a lot by type** — for some types it's the full email body or call transcript; for others it's a generic one-liner and the real data is in `extra_details` (flagged per type below). |
| `extra_details` | JSON metadata, schema varies by event type. It's JSON inside a CSV cell: quoted, with newlines escaped as `\n`. |
| `source` | Always `timeline` in these snapshots — ignore. |

## Event type guide

Only interpretation that is non-obvious from the raw data. Organized by `activity_type` / `activity_sub_type`.

### Communications

- **Email** — Content is the message (some sources prefix `Subject: ...\nBody: ...`). Can be to/from clients, opposing counsel, courts — not just Marble staff. **Automated marketing** emails have `extra_details.source = "braze"` plus a `flow_name`; don't read those as personal attorney communication. `sender_role` / `recipient_role` identify who's talking.
- **SMS** — Only Marble ↔ client. Automated/templated messages carry a `template_name` (source SMS-Magic/braze); personal texts come from dialpad.
- **Call** — Only Marble ↔ client. **Many calls have no transcript** — an empty/short `content` does not mean nothing happened: if `start_date != end_date` a conversation occurred (duration = end − start), and direction tells you who initiated. When a transcript exists, the overall story is reliable but **specific details (names, addresses, numbers) often contain transcription errors** — don't extract verbatim spellings from transcripts. `extra_details` may include `outcome`, `sentiment_score`, `duration_ms`.
- **Voice AI** — AI-agent phone calls (mostly intake/scheduling). Content may be empty or a short summary/transcript; `extra_details` carries call metadata (`duration_ms`, `call_status`, agent info).
- **Chat** — Human/assistant chat sessions. `extra_details.conversation_entries` has the structured transcript.
- **Ticket** — Salesforce support tickets. Content is well structured (Ticket Number, Type, Status, Outcome, Subject, Description). Open/New/Pending = unresolved work; a Closed ticket confirms resolution only if no similar ticket remains open. `extra_details.type` distinguishes support tickets from `"Filing/Serving Expense"` (financial) — for those, `extra_details.outcome` records payment resolution ("Paid", "Client didn't pay", ...).

### Intake & pre-signing

- **Intake Event / Intake Call** — Pre-case lead tracking. `extra_details.funnel_parameters` is a goldmine for the initial story: the client's own words (`moreInfo`), contact details, state/county, practice, urgency self-report.
- **LSS Event** (Created / Completed / Cancelled) — The paid strategy consultation. Content carries `LSS Status` plus **Intake Notes** (client type, parties, goals); `extra_details.intake.notes` / `.summary` ({clientGoals, caseOverview, financialInfo}) and, on Completed, `extra_details.summaries`. This is usually the best single source for the initial case story.
- **Case / Intake notes update** — Updates to the LSS intake notes (parties, case type, goals in `content` / `extra_details.intake_notes`).

### Case & service lifecycle

- **Case / Status_Change** — Case-level transitions. `content` = "Action: Case accepted" is the case-start signal. `extra_details` has `previous_status`, `status`, `sub_status`, `case_start_date`.
- **Service / Status_Change** — Service-level transitions (created, activated, ...). `extra_details.is_express_service` (boolean) identifies Autopilot/Express services. `service_type_name` names what the client bought.
- **Service / Milestone Update & Progress Update** — Delivery progress markers. Carry milestone name plus before/after completion percentages (`service_percent_completed`, `milestone_percent_completed`) — useful for "where does this case stand".
- **Service / Service Draft** — A *form* workflow, **not document drafting**. Status ladder: Waiting Client → Client Completed → Attorney Completed → Completed. Near "Client Completed", expect Data Entries rows with the client-filled data.
- **Case Update** — Staff-written case notes. **Case Review** sub-type is a comprehensive analysis (high quality, great for status/next-steps questions); **General** is supplementary (short notes like "Signed Marble agreement"). `extra_details.created_by_role` says who wrote it.
- **Task / Legal_Task** — Attorney-created tasks assigned to paralegals. State is in the content prefix `Legal Task: [State]`. New/Open = pending work; "Updated" with status still New/Open means the task was modified, not completed. `extra_details` has `status`, `subject`, `due_date`, `priority`, `completion_recap`. `int_user_marble_id` is the attorney who created the task.

### Client data & documents

- **Data Entries** (Client Data / Client Docs) — **Content is always generic** ("Client data updated"); the real data is `extra_details.data_points`, a dict of questionnaire fields (personal details, opposing party info, children, finances). Caveats: keys are internal field IDs (e.g., `petitionerMarriageQuestion`) — translate to natural language, don't quote keys; and the same fields are written by many flows (client intake questionnaire, later client edits, staff edits, AI extraction), so a row tells you *what was recorded*, not *which form it came from*.
- **Data Requests** (LSS / Drafts / Intake form / ...) — Tracks whether a requested form was completed: content "Completed" = finished, "Pending"/"Created" = still waiting. Sub-type "Drafts" = an Express-services form (NOT document drafting). Useful for "what is the client still missing" questions — pair with Data Entries (which record the actual submissions).
- **Documents** (Document_Upload / Document_Rename / Document_Delete) — File metadata plus an **AI-generated summary** of the document (`content` includes `Summary: ...`; `extra_details` has `file_name`, `summary`, `classification`, `document_category`, `who_uploaded`, `s3_key`). The summary describes what the document is, but for verbatim contents (what a petition actually pleads, what a checklist actually requires) you need the file itself. Note: the `documents/` folder in each case snapshot contains a **subset** of uploaded files with cleaned-up filenames — match them to timeline rows by document type/description, not exact filename.

### Scheduling

- **Calendar Event** (CKC / Court Prep / ...) — **Marble-internal scheduled calls, never court hearings.** CKC = the kick-off call with the assigned attorney. `start_date` is the scheduled (often future) time; `extra_details.status` tracks the booking lifecycle (SCHEDULED / COMPLETED / CANCELED). Don't describe a Calendar Event as a hearing — court-imposed dates come from court documents, and confusing the two falsely inflates urgency.

### Feedback & engagement signals

- **Feedback** (D14 / D30 / CKC / Case Start / Customer Support) — Client satisfaction surveys; the sub-type is the survey category (D14/D30 = check-ins 14/30 days after case acceptance). Rating in content and `extra_details.rating`; `extra_details.questions` holds the answers including the client's free-text comment; `is_skip` = client dismissed without answering. Customer Support feedback is tied to a ticket (`ticket_number`, `comment`, `issue_resolved`).
- **Marbles / Status_Change** — Client onboarding checklist progress ("Type: CKC_SCHEDULE_KICKOFF_CALL\nChange: Done"). Signals client engagement with the portal; not a legal event.
- **Legal Cortex / Case Update** — AI-generated case status updates sent to the client via the portal. The `Body:` in content is a readable case summary at that point in time.
- **Legal Cortex / User_Interaction** — A staff member acting on an AI-generated recommendation (e.g., marking an action In Progress/Completed). `extra_details.content_json` has the form fields (e.g., `what_was_done`); `original_artifact_content` is the AI output they responded to.

### Filing & serving

- **Serving / Serving job** — Process-server jobs for delivering filed documents to the opposing party. `extra_details` tracks `status`, `attempt_status`, `recipient_name`/`recipient_type`, attempt descriptions. Multiple rows per job as status changes.

## Reading tips

- Sort/filter chronologically by `activity_created_at`, not `start_date` (scheduled events put `start_date` in the future) and not `insert_timestamp` (warehouse time).
- Generic `content`, rich `extra_details`: Data Entries, Serving, non-completed Data Requests. Rich `content`: Email, SMS, Call (when transcribed), Ticket, LSS Event, Case Update, Legal Cortex.
- For "who did X": resolve the actor from `int_user_marble_id`/`ext_user_marble_id` consistency across events plus role fields in `extra_details` (`sender_role`, `created_by_role`, `who_uploaded`) — the IDs themselves are opaque.
- A silent client is a signal: gaps between Inbound events (or unanswered Outbound chains) are how you detect responsiveness issues. Missed calls: some call records have `answered_at = null` or zero duration.
