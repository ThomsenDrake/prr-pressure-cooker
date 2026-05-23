# PRR Pressure Cooker MVP Plan

## Summary

Build a runnable Python/uv scaffold in `/Users/drakethomsen-mai/Documents/code-projects/prr-pressure-cooker` that implements the PRD as a live Mistral Workflows-first system with repeatable local import-folder ingestion. The MVP will preserve raw evidence, classify events, compute deterministic escalation decisions, create human-review tasks, and expose Kanban/mail integrations through adapters so Hermes and tailnet-hosted Proton/himalaya can be wired without changing core workflow logic. Mistral workers should be packageable for hosted deployment on Koyeb through the Koyeb CLI.

## Key Changes

- Create a `uv` Python project using `mistralai-workflows[mistralai]`, `pydantic`, `sqlite-utils` or `sqlmodel`, `pytest`, and `ruff`.
- Add `src/prr_pressure_cooker/` with:
  - workflow definitions for `case_lifecycle_workflow`, `prr_escalation_router`, and human approval handling
  - activity modules for import parsing, classification, fee audit, deadline evaluation, draft generation, packet skeleton generation, and Kanban upserts
  - adapter interfaces for `MistralWorkflowAdapter`, `KanbanAdapter`, `EvidenceStore`, and `MailIngestAdapter`
- Add a Koyeb deployment path for Mistral workers:
  - package `prr worker` so it can run as a long-lived Koyeb background worker/service
  - include Koyeb CLI deployment notes and any required service config
  - keep local worker execution available for development and tests
- Start with import-folder ingestion:
  - `casefiles/<case_id>/incoming/` accepts `.eml`, `.pdf`, `.txt`, `.md`, and JSON/YAML portal-event fixtures
  - raw evidence is copied into immutable `raw/` paths with hashes
  - derived outputs go under `indexes/`, `audits/`, `drafts/`, and `packets/`
- Use SQLite as the MVP case database for cases, events, decisions, deadlines, human approval tasks, evidence refs, and audit records.
- Implement deterministic rules from the PRD for the first MVP pathways:
  - silence/delay
  - fee opacity
  - defective estimate
  - custodian dodge
  - closure threat
  - post-notice counsel/mediation readiness
- Keep all outbound actions blocked behind human approval. The MVP may draft emails, notices, packets, and Kanban cards, but it must never send email, post portal replies, pay invoices, close cases, or contact external parties.

## Public Interfaces

- CLI commands:
  - `prr init-case <case_id> --agency ... --request-title ...`
  - `prr import <case_id> <path>`
  - `prr route <case_id> --event <event_id>`
  - `prr review list`
  - `prr review approve|revise|defer|cancel <task_id>`
  - `prr worker` to start the Mistral Workflows worker
  - `prr deploy koyeb` or documented Koyeb CLI commands for deploying/updating the hosted worker
- Core schemas:
  - `CaseRecord`
  - `CaseEvent`
  - `EvidenceRef`
  - `FeeEstimateAudit`
  - `EscalationDecision`
  - `DeadlineRecord`
  - `HumanApprovalTask`
  - `KanbanCard`
- Config:
  - read `MISTRAL_API_KEY` from the existing local `.env`
  - mirror `MISTRAL_API_KEY` into the Koyeb worker environment/secrets for hosted worker deployments
  - support local adapter mode for Hermes and mail, with Proton/himalaya treated as a remote tailnet service rather than a local process requirement
  - do not require live mailbox access for MVP acceptance

## Test Plan

- Unit tests for each deterministic escalation rule, including no-action cases.
- Fixture tests for `.eml`, portal JSON, fee estimate text, closure threat text, and manual notes.
- SQLite persistence tests proving events, evidence refs, decisions, and review tasks survive process restart.
- Workflow smoke test that imports a fixture, starts/runs the router workflow, creates a human approval task, and blocks before any outbound action.
- Worker packaging smoke test that verifies the same `prr worker` entrypoint can run locally and is suitable for Koyeb deployment.
- Guardrail tests proving outbound send/post/payment/closure methods are unavailable or fail closed without explicit human approval.
- CLI smoke tests for case creation, import, routing, and review listing.

## Assumptions

- First deliverable is a runnable scaffold, not documentation-only.
- Mistral Workflows is live from the start, with local tests around adapter boundaries and worker deployment targeting Koyeb.
- The Mistral API key already exists in `.env`; do not echo or commit the secret.
- Import-folder ingestion is the MVP source of truth; Proton Bridge/himalaya integration is behind `MailIngestAdapter` and should connect to the existing service hosted on another machine in the current tailnet.
- Hermes Kanban is deferred behind `KanbanAdapter`; MVP will write local Kanban-card records to SQLite/JSON until the actual Hermes command/API is available.
- No `git init` unless separately requested.
- Current Mistral docs confirm the chosen shape: Workflows run in hybrid mode, workers run locally, side effects belong in activities, `uv add mistralai-workflows[mistralai]` is the documented install path, and structured human confirmations/forms are supported.
- Koyeb docs confirm it supports background workers/services and provides a CLI for terminal-managed deployments.

## Sources

- [Mistral Workflows overview](https://docs.mistral.ai/studio-api/workflows/getting-started/overview)
- [Mistral Workflows installation](https://docs.mistral.ai/studio-api/workflows/getting-started/installation)
- [Mistral workflow/activity split](https://docs.mistral.ai/studio-api/workflows/building-workflows/workflows)
- [Mistral forms and confirmations](https://docs.mistral.ai/studio-api/workflows/interacting-with-workflows/conversational_workflows/forms_and_confirmations)
- [Koyeb documentation](https://www.koyeb.com/docs)
- [Koyeb CLI installation](https://www.koyeb.com/docs/build-and-deploy/cli/installation)
