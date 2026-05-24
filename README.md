# PRR Pressure Cooker

Runnable scaffold for the PRR Pressure Cooker MVP. The app preserves raw public-records evidence, classifies new events, computes deterministic escalation decisions, and blocks all outbound activity behind human review.

## Quick Start

```bash
uv sync
uv run prr init-case demo --agency "Demo Agency" --request-title "Demo request"
uv run prr import demo tests/fixtures/opaque_fee_estimate.txt
uv run prr route demo --event <event_id>
uv run prr review list
```

The local database defaults to `var/prr.db`. Raw evidence is stored under
`casefiles/<case_id>/raw/`; generated drafts and packets go under
`casefiles/<case_id>/derived/`; human-reviewed redacted derivatives belong under
`casefiles/<case_id>/redacted/`.

Mail and portal imports also maintain canonical casefile indexes under
`casefiles/<case_id>/indexes/`:

- `messages.csv`
- `threads.csv`
- `attachments.csv`
- `contacts.csv`
- `timeline.md`

Raw `.eml` files remain immutable. Attachments extracted from imported email are
stored as child evidence refs under the raw evidence tree and referenced from the
attachment index. `messages.csv` and `timeline.md` include source evidence IDs
and stored paths so packets and review prompts can point back to the raw case
context instead of only naming uploaded files.

JSON/YAML portal events can include the same evidence metadata. Supported fields
include `subject`, `portal_message_id`, `portal_thread_id`, `sender`,
`recipients`, `cc`, `contacts`, and embedded `attachments` with `filename`,
`mime_type`, and `content_b64` or `content_text`. Embedded portal attachments are
also stored as child evidence refs under the immutable raw evidence tree.

## Workflow And Deadline Commands

Start or inspect the durable case workflow record:

```bash
uv run prr workflow start-case demo
uv run prr workflow signal-event demo --event <event_id>
uv run prr workflow status demo
uv run prr workflow resolve-case demo
```

Use `--backend mistral` or set `PRR_WORKFLOW_BACKEND=mistral` when the command
should start, signal, or query the hosted Mistral Workflow execution instead of the
local SQLite-backed shim. `PRR_WORKFLOW_API_BASE_URL` can override the default
Mistral API URL for both the worker and local signaling client. Hosted
`signal-event` and `ingest-push` calls include the event payload and raw evidence
needed for the Koyeb worker to create the case, persist evidence, and route the
event from its `/data` store.

`signal-event` routes the imported event, records a route audit, creates any pending human-review task, and refreshes the workflow status query state. `ingest-push` is the tailnet/Koyeb handoff command for raw evidence that has already been exported from Proton/Himalaya:

```bash
MISTRAL_API_KEY=... DEPLOYMENT_NAME=prr-pressure-cooker-prod \
  uv run prr ingest-push demo /path/to/message.eml --backend mistral
```

Human approval is available through the `prr-review-assistant` conversational
workflow. Publish that workflow in Le Chat and start it without arguments; if
more than one review is pending, it shows a numbered queue by agency and request
title so users can choose a review without knowing a case number. It then
displays the pending review task with the triggering agency text, decision
rationale, packet summaries, and draft preview, then collects an `approve`,
`revise`, `defer`, or `cancel` decision. Notes are optional for every decision.
When a note is provided, a review-note judge records how the note should affect
the final artifact and, when applicable, writes a reviewed draft plus judgment
file before signaling the case lifecycle workflow through `approval_reply`. The
chat workflow only records review state and ready-to-send artifacts; it does not
send email, post to a portal, pay an invoice, contact media, or close a case.

Deadline checks can run separately and emit deadline-elapsed events back through the same routing path:

```bash
uv run prr deadline scan --emit-events
uv run prr deadline scan --emit-events --backend mistral
```

Rebuild or export the casefile indexes after backfills or manual evidence repair:

```bash
uv run prr casefile rebuild-indexes demo
uv run prr casefile export-indexes demo
```

## Pull from Proton/Himalaya

When Himalaya is running on a tailnet host, pull a raw `.eml` through SSH and route it immediately:

```bash
uv run prr pull-himalaya seminole-scout \
  --ssh-target drake@omarchy-mbp.tail7e7910.ts.net \
  --folder "Folders/Public Records Requests" \
  --message-id 1 \
  --create-case \
  --route
```

The command exports a full raw message with `himalaya message export -F`, stores it under `casefiles/<case_id>/incoming/himalaya/`, imports it into the immutable evidence store, and optionally routes it through the escalation rules.

For broader discovery, search `All Mail` and derive cases from request numbers in the subject:

```bash
uv run prr pull-himalaya-batch \
  --ssh-target drake@omarchy-mbp.tail7e7910.ts.net \
  --folder "All Mail" \
  --query "subject records order by date desc" \
  --limit 25 \
  --route
```

The batch command recognizes common PRR identifiers including `PRR-163721`, public-records portal IDs like `#26-17289`, JustFOIA IDs like `CORR-2026-300`, and MyCustHelp-style IDs like `W203033-051226`.

## Worker

Run a Mistral Workflows worker locally:

```bash
MISTRAL_API_KEY=... DEPLOYMENT_NAME=prr-pressure-cooker-dev uv run prr worker
```

The Mistral API key already exists in `.env` for local use. Do not commit or echo it.

## Koyeb

Koyeb deployment notes live in [docs/koyeb.md](docs/koyeb.md).
