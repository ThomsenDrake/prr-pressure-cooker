# Koyeb-Hosted Mistral Workflows Gap Closure Plan

## Summary

Keep Koyeb as the production worker host while using Mistral Workflows for durable
orchestration. Mistral owns case lifecycle, signals, timers, approvals, and pathway
progression. The Koyeb worker runs Python workflow/activity code, stores SQLite and
casefiles on a Koyeb Volume, and receives pushed ingest events from the tailnet machine
that already hosts Proton/Himalaya.

## Key Changes

### Workflow Runtime

- Tighten the workflow dependency to `mistralai-workflows[mistralai]>=3.0.0,<4`.
- Replace manual workflow registration with upstream-style auto-discovery.
- Convert `CaseLifecycleWorkflow` into an `InteractiveWorkflow`.
- Add case workflow signals for agency events, deadlines, human replies, payment,
  records release, and manual resolution.
- Add a `get_case_status` query exposing current case state, pending task, active
  deadlines, latest event, and pressure score.
- Keep workflow code deterministic; DB, file, Mistral API, Himalaya, packet, and
  Kanban work must live in activities.

### Koyeb Deployment

- Mount a Koyeb Volume at `/data`.
- Run one worker instance for v1 because SQLite and casefiles are volume-backed.
- Use:
  - `PRR_DB_PATH=/data/prr.db`
  - `PRR_CASEFILES_DIR=/data/casefiles`
  - `DEPLOYMENT_NAME=prr-pressure-cooker-prod`
  - `MISTRAL_API_KEY={{ secret.MISTRAL_API_KEY }}`
- Update the Dockerfile, deployment docs, and `prr deploy koyeb` helper so volume
  and environment setup are explicit.

### Local Ingest Push

- Keep Proton/Himalaya pulls on the tailnet machine.
- Add `prr ingest-push` for the tailnet host.
- Flow:
  1. Tailnet machine exports `.eml` through Himalaya.
  2. Raw `.eml` is imported locally and hashed.
  3. Event payload plus raw evidence is pushed to the Koyeb-backed workflow path.
  4. Koyeb persists evidence to `/data/casefiles`, saves the event, and signals the
     running case workflow.
- Do not require Koyeb to join Tailscale for this milestone.

### Case Lifecycle And Pathways

- Wrap existing routing/rerouting behavior in workflow activities:
  - `load_case_activity`
  - `persist_event_activity`
  - `classify_event_activity`
  - `audit_fee_estimate_activity`
  - `compute_decision_activity`
  - `reroute_case_activity`
  - `create_review_task_activity`
  - `build_packet_activity`
- Add deadline activity support for silence after acknowledgment, closure windows,
  Section 119.12 cure windows, and post-notice no-cure escalation.
- Add missing pathway coverage for exemption vagueness, duplicate inflation, public
  pressure packets, and counsel/mediation packets.
- Preserve the no-outbound guardrail: no email, portal post, payment, media outreach,
  legal notice send, or case closure happens automatically.

### Human Approval

- Use `InteractiveWorkflow.wait_for_input()` as the primary approval gate.
- Approval choices are `approve`, `revise`, `defer`, and `cancel`.
- Mirror each approval interaction into local `HumanApprovalTask` state for audit.
- Approval produces ready-to-send artifacts only; it does not send anything.

### Packet And Evidence Outputs

- Replace the current small packet skeleton with pathway-specific bundle builders:
  - fee audit
  - unanswered questions matrix
  - closure timeline
  - withholding/exemption matrix
  - attorney/FAF packet
  - commissioner/reporter one-pager
- Generate case indexes:
  - `indexes/messages.csv`
  - `indexes/threads.csv`
  - `indexes/attachments.csv`
  - `indexes/contacts.csv`
- Keep raw evidence immutable. Derived and redacted outputs go in separate folders.

## Public Interfaces

- Add CLI commands:
  - `prr workflow start-case <case_id>`
  - `prr workflow signal-event <case_id> --event <event_id>`
  - `prr workflow status <case_id>`
  - `prr ingest-push ...`
  - `prr deadline scan --emit-events`
  - `prr deploy koyeb --volume prr-data`
- Keep existing commands:
  - `prr init-case`
  - `prr import`
  - `prr route`
  - `prr reroute-case`
  - `prr reroute-batch`
  - `prr review ...`
- Add durable records for workflow execution IDs, deadlines, route audits, packet
  artifacts, and approval interaction history.

## Test Plan

- Unit tests:
  - workflow discovery finds all workflow classes
  - deadline rules produce expected deadline events
  - missing pathway rules route correctly
  - packet builders create expected files without mutating raw evidence
- Integration tests:
  - import event -> signal case workflow -> creates interactive approval task
  - payment confirmation resolves stale fee task
  - records released resolves pending closure/fee paths
  - deadline elapsed creates silence or post-notice task
- Workflow tests:
  - use Mistral's test worker pattern for non-interactive routing
  - test interactive approval with mocked input
  - verify workflow query returns current case status
- Deployment smoke:
  - `uv run ruff check .`
  - `uv run pytest`
  - `uv run prr deploy koyeb` prints command with volume/env settings
  - Koyeb worker starts with `/data` paths and discovers workflows
  - local ingest-push can deliver one fixture event into the Koyeb-backed workflow path

## Assumptions

- Koyeb remains the production worker host.
- Persistence v1 uses one Koyeb Volume plus SQLite/casefiles, not Postgres.
- Tailnet/Himalaya stays off Koyeb; a local/tailnet process pushes imported events into
  the workflow system.
- Mistral `InteractiveWorkflow` is the primary approval surface.
- Outbound actions remain manual-only.
- Koyeb volume/service/env behavior follows current Koyeb docs for worker services,
  environment variables/secrets, and volumes.
- Mistral workflow implementation follows public Mistral examples/docs:
  - <https://github.com/mistralai/workflows-starter-app>
  - <https://github.com/mistralai/demo-medical-doc-processor-workflow>
  - <https://github.com/mistralai/platform-docs-public>
