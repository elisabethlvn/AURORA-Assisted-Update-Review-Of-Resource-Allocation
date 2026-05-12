# AURORA
**Assisted Update Review Of Resource Allocation**

AURORA is a Microsoft-stack project planning assistant that turns unstructured project communications into structured, reviewable plan updates. It helps project managers keep official trackers, schedules, and Gantt views aligned with what is actually discussed in emails, meeting notes, and informal updates.

## Problem

In complex delivery environments, the official project plan often falls out of sync with reality. Updates about new tasks, shifted dates, blockers, owner changes, and dependencies are usually buried in meeting notes or email threads. Unless a project manager manually updates the tracker, the plan becomes stale.

AURORA addresses this by extracting planning signals from unstructured text and converting them into draft updates. The system is intentionally human-in-the-loop: AI never directly overwrites the official plan.

## Solution

AURORA ingests project emails and meeting notes, extracts structured planning updates, stores them as draft updates, and routes them to a SharePoint review queue. Project managers can approve, reject, or correct each draft before it updates the official `tasks_master` table. Every approval and rejection is recorded in `change_log` for auditability.

## Core Capabilities

- Extracts updates from `emails.csv` and `meeting_notes.jsonl`
- Detects new tasks, owner changes, due-date shifts, status updates, blockers, and ambiguity
- Matches extracted updates against existing `tasks_master` tasks
- Generates draft updates with confidence, source evidence, and clarification questions
- Routes drafts to a SharePoint human review queue
- Updates `tasks_master` only after PM approval
- Records approved and rejected decisions in `change_log`
- Sends Teams notifications when review items are ready
- Supports Power BI reporting for master plan, draft queue, and audit trail
- Supports Teams bot questions backed by PostgreSQL and Azure AI Search

## Architecture

![Aurora Solution Architecture](./"Solution Architecture.png")

The architecture starts with meeting-note or email file upload in SharePoint. Power Automate invokes Azure Functions to extract plan updates using Azure AI Foundry and validation rules, stores draft updates in Azure PostgreSQL, routes them to a SharePoint human review queue, and updates the official project plan only after PM approval. Power BI and Teams provide reporting and conversational access.

---
## Microsoft Stack

### Active Services

- **Microsoft Foundry / Azure OpenAI**  
  Used for AI-assisted extraction and reasoning over project updates.

- **Azure Functions**  
  Hosts the backend APIs:
  - `IngestUploadedFile`
  - `IngestProjectData`
  - `ReviewDraft`
  - `GetPendingDrafts`
  - `SearchEvidence`
  - `AuroraTeamsBot`
  - `BuildInfo`

- **Azure Database for PostgreSQL**  
  Structured system of record for:
  - `tasks_master`
  - `draft_plan_updates`
  - `change_log`
  - `people`
  - `dependencies`
  - `plan_snapshots`

- **Power Automate**  
  Orchestrates file ingestion, SharePoint review item creation, Teams notifications, approval/rejection flows, and Power BI refresh.

- **SharePoint Lists**  
  Human-in-the-loop review and correction interface.

- **Microsoft Teams**  
  Notification channel and conversational interface for AURORA.

- **Power BI**  
  Reporting layer for master plan, draft review queue, Gantt-style views, and audit history.

- **Azure AI Search**  
  Indexes raw source evidence from uploaded emails and meeting notes, enabling searchable audit support.

- **GitHub**  
  Source control and submission repository.

### Optional / Future Services

- **Azure Blob Storage**  
  Can be used as a raw document archive/data lake.

- **Azure Cosmos DB**  
  Not required in the current MVP because auditability is handled in PostgreSQL `change_log`.

## Data Flow

1. A project file is uploaded to SharePoint.
2. Power Automate reads the file and calls `IngestUploadedFile`.
3. The Azure Function parses email or meeting-note records.
4. AURORA extracts plan updates using deterministic rules and Azure AI Foundry fallback.
5. Raw source records are indexed into Azure AI Search.
6. Extracted updates are validated against source evidence.
7. Matching logic checks whether each update relates to an existing task.
8. Drafts are inserted into `draft_plan_updates`.
9. Power Automate creates rows in the SharePoint `Aurora Draft Review` list.
10. Teams notifies the PM that review items are ready.
11. The PM approves, rejects, or corrects drafts in SharePoint.
12. Power Automate calls `ReviewDraft`.
13. Approved drafts update `tasks_master`.
14. Approved and rejected decisions are recorded in `change_log`.
15. Power BI visualizes the latest project plan and audit trail.

## Human-In-The-Loop Review

AURORA does not directly update the official plan. Every extracted update becomes a draft first.

Drafts include:

- draft ID
- project ID
- source ID
- update type
- matched task ID
- proposed change summary
- confidence
- source evidence
- clarification question
- review status

The PM can:

- approve a clean draft
- reject an irrelevant draft
- correct task ID, owner, due date, or status
- retry failed submissions after fixing correction fields

Failed submissions are visible in SharePoint through `flow_status` and `flow_error`, so PMs do not need to inspect Power Automate run logs.

## PostgreSQL Tables

### `tasks_master`

Official project plan table. Updated only after human approval.

### `draft_plan_updates`

AI-generated draft updates awaiting review or already reviewed.

### `change_log`

Audit trail for approved and rejected decisions.

Approved updates record field-level changes:

```text
change_type
field_changed
old_value
new_value
reviewed_by
source_evidence
comments
```

Rejected updates record:

```text
change_type = REJECTED
field_changed = No official plan change
old_value = NULL
new_value = NULL
reviewed_by
comments
```

### `plan_snapshots`

Used for baseline/current comparison and future delta detection.

### `dependencies`

Stores task dependency relationships for sequencing and Gantt-style views.

### `people`

Stores team member metadata for owner matching and future assignment recommendations.

## SharePoint Review Queue

The SharePoint list `Aurora Draft Review` acts as the PM review desk.

Recommended columns:

```text
draft ID
project_id
source_id
update_type
task_title
proposed_change
review_status
confidence
source_evidence
clarification_question
matched_task_id
corrected_task_id
corrected_owner_name
corrected_due_date
corrected_status
decision
review_notes
flow_status
flow_error
```

`decision` choices:

```text
Pending
Approve
Reject
```

`corrected_status` choices:

```text
No change
Not started
In progress
Blocked
Done
```

`flow_status` choices:

```text
Waiting for review
Processing
Completed
Failed
```

## Teams Bot Capabilities

AURORA can answer operational project questions using PostgreSQL and Azure AI Search.

Example questions:

```text
Aurora, why was D00043 created?
Aurora, show pending approvals for PRJ001.
Aurora, show blocked tasks.
Aurora, show change log for T00065.
Aurora, what changed this week?
Aurora, search evidence about coordination model.
```

Known draft IDs and task IDs are answered deterministically from PostgreSQL. Broader source-evidence questions use Azure AI Search.

## Power BI Dashboard

The Power BI report contains three primary views.

### 1. Master Plan

- current tasks from `tasks_master`
- task status distribution
- blocked tasks
- upcoming deadlines
- Gantt-style timeline

### 2. AI Draft Review

- pending drafts
- drafts needing clarification
- approved/rejected review history
- confidence levels
- source evidence

### 3. Change Log

- approved task changes
- rejected AI draft decisions
- reviewer
- old/new values
- source evidence
- comments

## Azure Function Endpoints

### `IngestUploadedFile`

Receives uploaded CSV or JSONL file content from Power Automate.

### `IngestProjectData`

Processes one project communication record and extracts draft updates.

### `ReviewDraft`

Approves or rejects a draft. Approved drafts update `tasks_master`; all decisions are written to `change_log`.

### `GetPendingDrafts`

Returns pending or clarification-needed drafts.

### `SearchEvidence`

Searches indexed source evidence in Azure AI Search.

### `AuroraTeamsBot`

Answers Teams questions using PostgreSQL, Azure AI Search, and Foundry.

### `BuildInfo`

Returns deployed app version and configuration status.

## Environment Variables

Required:

```text
DB_HOST
DB_USER
DB_PASS
DB_NAME
FOUNDRY_ENDPOINT
FOUNDRY_AGENT_NAME
FOUNDRY_AGENT_VERSION
```

For Azure AI Search:

```text
AZURE_SEARCH_ENDPOINT
AZURE_SEARCH_API_KEY
AZURE_SEARCH_INDEX_NAME
AZURE_SEARCH_API_VERSION
```

## Why This Is More Than Summarization

AURORA does not simply summarize meetings. It converts conversations into governed planning updates.

It supports:

- structured extraction
- task matching
- ambiguity detection
- confidence scoring
- source evidence
- human approval
- correction workflow
- official plan update
- rejection tracking
- audit trail
- dashboard reporting
- Teams-based project queries

## MVP Scope

The MVP focuses on a curated project dataset containing meeting notes, email-style updates, task trackers, dependencies, people, and plan snapshots. It prioritizes reliability, transparency, and review-before-update governance over full automation.

## Demo Flow

1. Upload an email or meeting-note file to SharePoint.
2. Power Automate calls `IngestUploadedFile`.
3. AURORA extracts draft updates.
4. SharePoint review items are created.
5. Teams notifies the PM.
6. PM approves one clean draft.
7. PM corrects one ambiguous draft.
8. PM rejects one irrelevant draft.
9. `tasks_master` updates only for approved changes.
10. `change_log` records approved and rejected decisions.
11. Power BI shows the updated plan and audit trail.
12. Teams bot explains why a draft was created.

## Future Enhancements

- Dependency-aware schedule impact analysis
- Priority scoring based on urgency, blockers, and due dates
- More advanced natural-language project analytics in Aurora Teams Bot

