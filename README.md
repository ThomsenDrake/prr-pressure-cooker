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

The local database defaults to `var/prr.db`. Raw evidence and derived drafts are stored under `casefiles/<case_id>/`.

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
