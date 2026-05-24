# Short-Lived Step Workflows With Durable Case Objects Plan

## Summary

Move PRR case ownership out of long-running per-case workflow executions and into the
durable case store. Mistral Workflows should become bounded step processors: one run for
one incoming event, one run for one review decision, one run for one deadline tick, and
one run for repair or manual resolution. The durable SQLite state in `/data/prr.db`
remains the source of truth for cases, tasks, deadlines, packet artifacts, audits, and
workflow run history.

This keeps the user-facing Le Chat review experience while removing the need for every
case to keep a 180-day `case-lifecycle-workflow` execution alive.

## Problem Statement

The current design stores real case state durably, but still treats a running workflow
execution as the case lifecycle object.

- `CaseLifecycleWorkflow` is defined as a 180-day interactive workflow with in-memory
  event and approval queues.
- `MistralWorkflowRuntime` starts, signals, discovers, and queries
  `case-lifecycle-workflow` executions for case operations.
- `workflow_executions` persists remote Mistral execution IDs, but it currently acts like
  a mostly singleton active lifecycle record per case.
- `PRRReviewAssistantWorkflow` is bounded, but review submission still prefers signaling
  the active lifecycle workflow before falling back to direct durable updates.
- `route_case_event_with_activities` and `PRREscalationRouter` already show the better
  shape: a short-lived workflow can load durable state, apply one command, write durable
  output, and finish.

This creates avoidable operational failure modes. A workflow history can outlive the code
version that created it, a deploy can expose replay-sensitive changes, and a single case
with many agency back-and-forth messages forces one execution to represent a whole
thread of unrelated steps.

## Target Architecture

The database is the durable case object. Workflow executions are command attempts.

### Durable Case Store

Keep these as the source of truth:

- `cases` and `case_states` for current case posture.
- `events` and evidence tables for immutable incoming records.
- `approval_tasks`, `approval_interactions`, and `review_note_judgments` for human
  decisions.
- `deadlines` for pending timers and emitted deadline events.
- `route_audits` and `packet_artifacts` for reproducible output history.
- `workflow_executions` as a ledger of remote and local step runs.

The status API should read durable case state directly. It should not require querying a
currently running workflow.

### Step Workflows

Add or repurpose bounded workflows around explicit commands:

| Workflow | Input | Durable effect | Expected lifetime |
| --- | --- | --- | --- |
| `prr-case-event-step` | `CaseWorkflowSignal` or `{case_id, event_id, event_payload}` | Persist pushed event if present, route event, reconcile case state, save run result | seconds to minutes |
| `prr-approval-step` | `ApprovalRecordInput` | Apply review choice, run the note judge, update task/interactions/final artifact, reconcile case state | seconds |
| `prr-deadline-step` | deadline ID or scan input | Emit due deadline events and process each event through the event step | seconds to minutes |
| `prr-manual-resolution-step` | case ID plus note | Resolve case, cancel pending tasks/deadlines, persist case state | seconds |
| `prr-case-reconcile-step` | case ID plus options | Reroute or repair one case, optionally replacing tasks | minutes |

`prr-review-assistant` remains interactive because the user has to respond in Le Chat.
After collecting the choice, it should call `prr-approval-step` or the same approval
activity directly. It should not depend on signaling a long-running lifecycle execution.

### Execution Identity

Use deterministic execution IDs for idempotent step starts, with forced retry IDs only
for explicit manual retries.

- Event step: `wf_<deployment>_prr-case-event-step_<case_id>_<event_id>`.
- Approval step: `wf_<deployment>_prr-approval-step_<task_id>_<choice_hash>`.
- Deadline step: `wf_<deployment>_prr-deadline-step_<deadline_id>`.
- Reconcile step: `wf_<deployment>_prr-case-reconcile-step_<case_id>_<request_hash>`.

Store the actual remote Mistral execution ID returned by the platform in
`workflow_executions.execution_id`. Store the requested deterministic ID,
idempotency key, command payload, result summary, and error text in `data_json`.

Multiple workflow records per case become normal. The latest case state is not inferred
from the latest workflow record; it comes from `case_states`.

## Data Model Changes

### Workflow Run Statuses

Extend `WorkflowExecutionStatus` beyond lifecycle terms:

- `started`
- `active`
- `succeeded`
- `failed`
- `canceled`
- `superseded`

Keep `resolved` temporarily for old `case-lifecycle-workflow` rows and migration
compatibility. New step runs should end as `succeeded` or `failed`.

### Idempotency Records

Add an explicit command ledger table instead of overloading workflow rows for all
deduplication:

```sql
CREATE TABLE case_command_runs (
    command_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    command_type TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    workflow_execution_id TEXT,
    status TEXT NOT NULL,
    input_json TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(command_type, idempotency_key)
);
```

Recommended command types:

- `case_event`
- `approval`
- `deadline`
- `manual_resolution`
- `case_reconcile`

### Case Processing Lease

Add a lease table before enabling multiple Koyeb worker instances or concurrent
deadline/event processors:

```sql
CREATE TABLE case_processing_leases (
    case_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

For the current one-worker SQLite deployment, this can be implemented with a short
transactional lease helper and tested locally. If the service later moves to Postgres,
the same abstraction can map to row locks or advisory locks.

## Code Changes

### 1. Add Durable Command Helpers

Touchpoints:

- `src/prr_pressure_cooker/models.py`
- `src/prr_pressure_cooker/storage.py`
- `src/prr_pressure_cooker/service.py`

Add models and store methods for `CaseCommandRun` and optional processing leases.
Provide helpers:

- `begin_case_command(command_type, idempotency_key, input)`
- `mark_case_command_succeeded(command_id, result)`
- `mark_case_command_failed(command_id, error)`
- `with_case_processing_lease(case_id, owner_id, ttl)`

The service layer should make duplicate command handling explicit: if a completed
command with the same idempotency key already exists, return its prior result without
creating duplicate tasks, packets, interactions, or deadline events.

### 2. Extract Step Service Functions

Touchpoint:

- `src/prr_pressure_cooker/service.py`

Create durable command functions that can be called by local CLI, Mistral activities,
or repair jobs:

- `process_case_event_step(signal, store, settings)`
- `process_approval_step(input, store, settings)`
- `process_deadline_step(input, store, settings)`
- `process_manual_resolution_step(input, store, settings)`
- `process_case_reconcile_step(input, store, settings)`

`process_case_event_step` should wrap the current lifecycle event path:

1. Persist pushed event payload when present.
2. Route the event through the existing classification/audit/decision/packet/task
   activity chain.
3. Reconcile durable case state.
4. Save command and workflow run outcome.

`process_approval_step` should wrap the current `apply_review_record_if_pending` path,
including the note judge and final artifact. It should reconcile case state before
returning.

### 3. Add Step Workflow Classes

Touchpoint:

- `src/prr_pressure_cooker/workflows/router.py`

Add bounded workflow classes:

- `PRRCaseEventStepWorkflow`
- `PRRApprovalStepWorkflow`
- `PRRDeadlineStepWorkflow`
- `PRRManualResolutionStepWorkflow`
- `PRRCaseReconcileStepWorkflow`

Each workflow should call one activity that owns durable command execution. Avoid
in-memory queues, long waits, workflow queries, and signal handlers in these step
workflows.

`PRREscalationRouter` can either be renamed/reused as `prr-case-event-step` or kept as
an internal lower-level classifier. The public runtime should call the step workflow
that handles persistence, reconciliation, idempotency, and run ledger updates.

### 4. Replace Runtime Semantics

Touchpoint:

- `src/prr_pressure_cooker/workflow_runtime.py`

Change the protocol from lifecycle operations to command starts:

- `process_event(case_id, signal)`
- `process_approval(input)`
- `process_deadlines(...)`
- `resolve_case(case_id, note)`
- `reconcile_case(case_id, options)`
- `status(case_id)`

For the Mistral backend:

- `process_event` executes `prr-case-event-step` and persists the returned remote
  execution metadata.
- `process_approval` executes `prr-approval-step`.
- `status` reads `get_case_status(case_id, store)` and includes recent workflow run
  records from the local ledger. It should not query `get_case_status` on a remote
  workflow execution.
- Remote discovery should become diagnostic only. It should not repair case state by
  finding a long-running workflow.

Keep the old `start_case`, `signal_event`, and `resolve_case` methods as compatibility
wrappers for one release. They should call the new step methods when
`PRR_WORKFLOW_MODE=step`.

### 5. Update CLI And Ingest

Touchpoints:

- `src/prr_pressure_cooker/cli.py`
- any pushed-ingest caller scripts or docs

Add new commands:

- `prr workflow process-event <case_id> --event <event_id>`
- `prr workflow process-approval <task_id> --choice <choice> [--note ...]`
- `prr workflow reconcile-case <case_id> [--replace-tasks]`

Deprecate or hide:

- `prr workflow start-case`
- `prr workflow signal-event`

Change existing entrypoints:

- `prr ingest-push` should persist/import the event and start `process-event`.
- `prr deadline scan --emit-events --backend mistral` should start deadline or event
  steps, not signal a lifecycle workflow.
- `prr workflow status` should read durable state plus recent run ledger records.

### 6. Update Le Chat Review Assistant

Touchpoint:

- `src/prr_pressure_cooker/workflows/router.py`

After `wait_for_input`, replace `signal_approval_reply_activity` with one of:

- preferred: `process_approval_step_activity`, which applies the review decision
  directly and returns the final artifact; or
- acceptable interim: `execute_prr_approval_step_activity`, which starts
  `prr-approval-step` and then reads the updated task/final artifact from durable state.

Remove lifecycle-specific queue filtering once pending tasks no longer require a
signalable active lifecycle execution.

The final Le Chat response should keep returning the final artifact in a copyable
Markdown block and should state that no outbound action was taken.

### 7. Migrate Production Safely

Use a feature flag:

- `PRR_WORKFLOW_MODE=lifecycle` for the current behavior.
- `PRR_WORKFLOW_MODE=step` for the new behavior.

Migration sequence:

1. Deploy schema and compatibility code with `PRR_WORKFLOW_MODE=lifecycle`.
2. Backfill `case_states` for all cases and record a command/run ledger baseline.
3. Deploy step workflow classes and verify worker discovery.
4. Run one known fixture or disposable production case through `prr-case-event-step`.
5. Switch `ingest-push`, deadline scan, and review assistant to step mode.
6. Stop creating new `case-lifecycle-workflow` executions.
7. For existing running lifecycle executions, mark corresponding local
   `workflow_executions` rows as `superseded` once durable case state has been
   reconciled. Cancel or let remote executions expire according to platform support.
8. After a stable period, remove lifecycle signal/discovery code.

Backout is simple while compatibility remains: flip `PRR_WORKFLOW_MODE` back to
`lifecycle`, because the durable tables are backwards compatible and the old workflow
code is still registered.

## Tests

### Unit Tests

- Duplicate event command returns the existing result and does not create duplicate
  approval tasks or packet artifacts.
- Duplicate approval command does not record duplicate interactions or rerun the note
  judge after success.
- Deadline command marks a deadline emitted exactly once.
- Status reads from `case_states` after all workflow runs have completed.
- Workflow execution records can store multiple runs for the same case and preserve the
  actual remote execution IDs.

### Integration Tests

- `ingest-push` with Mistral backend starts `prr-case-event-step`, then durable status
  shows the new latest event and pending task.
- `prr-review-assistant` collects a Le Chat decision and applies it without a running
  `case-lifecycle-workflow`.
- A new agency receipt on an existing case starts a new event step and updates the same
  durable case object.
- A production-style DB wipe followed by mailbox pull creates durable external aliases,
  cases, events, command runs, and step workflow records.

### Hosted Smoke

- Deploy to Koyeb with `PRR_WORKFLOW_MODE=step`.
- Confirm worker discovery includes the new step workflows.
- Start a disposable review assistant execution and verify it no longer hides tasks
  because a lifecycle workflow is missing.
- Pull one fresh mailbox event and verify the event run completes rather than staying
  running.
- Confirm no new `case-lifecycle-workflow` executions are created.

## Acceptance Criteria

- No new per-case workflow execution is expected to run for days or weeks.
- A case can receive many back-and-forth agency messages, with each message producing a
  separate step run tied to the same durable case.
- `prr workflow status` works when no Mistral execution is currently running.
- Le Chat review still shows the queue, accepts a decision, applies optional notes,
  and returns the final artifact.
- Duplicate pushes, duplicate deadline scans, and duplicate review submissions are
  idempotent.
- Production can be switched between lifecycle mode and step mode during the migration
  window.

## Open Questions

- Should `case_command_runs` be introduced now, or should `workflow_executions.data_json`
  carry idempotency until the next schema pass? The separate table is cleaner and safer.
- Should `prr-review-assistant` apply approval directly in its own activity, or start a
  separate `prr-approval-step` run for maximum audit symmetry? Direct application is
  simpler; a separate step run is more consistent operationally.
- How aggressively should existing remote lifecycle executions be canceled once step mode
  is enabled? The durable DB can supersede them, but cancellation policy depends on what
  the platform exposes reliably.
- Do we want a dedicated scheduler for deadlines, or is the current CLI/Koyeb scheduled
  scan enough for this milestone?

## Recommended First Implementation Slice

Build this in one reversible slice:

1. Add `WorkflowExecutionStatus.SUCCEEDED`, `CANCELED`, and `SUPERSEDED`.
2. Add `case_command_runs` and store helpers.
3. Implement `process_case_event_step` and call it from a new
   `prr-case-event-step` workflow.
4. Add `WorkflowRuntime.process_event` and make `signal_event` delegate to it when
   `PRR_WORKFLOW_MODE=step`.
5. Update `ingest-push` and deadline emission under the flag.
6. Add tests proving duplicate event processing is idempotent and status is DB-backed.

Only after that slice is stable should review approval move off lifecycle signaling.
