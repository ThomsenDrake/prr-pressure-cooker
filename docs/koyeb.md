# Koyeb Worker Deployment

Mistral Workflows workers can run as long-lived Koyeb worker services. The worker entrypoint is:

```bash
uv run prr worker
```

The worker needs these runtime variables:

- `MISTRAL_API_KEY`, configured from a Koyeb Secret.
- `DEPLOYMENT_NAME`, for example `prr-pressure-cooker-prod`.
- `PRR_DB_PATH=/data/prr.db`.
- `PRR_CASEFILES_DIR=/data/casefiles`.
- `PRR_WORKFLOW_API_BASE_URL`, optional, only if a non-default Mistral API URL is needed.
- `HIMALAYA_SSH_TARGET`, optional, pointing at the tailnet-hosted Proton/himalaya SSH target such as `drake@omarchy-mbp.tail7e7910.ts.net`.
- `HIMALAYA_FOLDER`, optional, defaulting to `Folders/Public Records Requests`.
- `HIMALAYA_ACCOUNT`, optional, if the remote Himalaya config has more than one account.
- `PROTON_HIMALAYA_BASE_URL`, optional legacy placeholder for an HTTP service wrapper.

Print the deploy command:

```bash
uv run prr deploy koyeb
```

Execute the deploy command after the Koyeb CLI is authenticated and the `MISTRAL_API_KEY` secret exists:

```bash
uv run prr deploy koyeb --volume prr-data --wait --execute
```

The generated command deploys the current project directory as a Docker-built Koyeb `worker` service, pins `--scale 1`, uses the standard `small` instance type by default, mounts the volume at `/data`, and sets:

- `MISTRAL_API_KEY={{ secret.MISTRAL_API_KEY }}`
- `DEPLOYMENT_NAME=prr-pressure-cooker-prod`
- `PRR_DB_PATH=/data/prr.db`
- `PRR_CASEFILES_DIR=/data/casefiles`

SQLite and casefiles are both volume-backed in v1, so do not raise the worker scale above one until storage moves off the single Koyeb Volume.
The deploy helper excludes local `.venv`, `casefiles`, and `var` directories from the archive so local evidence and SQLite state are not uploaded with the image source.

The same worker registers the durable `case-lifecycle-workflow` and the
Le Chat-facing `prr-review-assistant` conversational workflow. Publish
`prr-review-assistant` in Le Chat for human review: users start it without a case
number, choose from a numbered queue labeled by agency and request title, then
review the triggering agency text, decision rationale, packet summaries, and
draft preview. It waits for a structured approval choice, with notes optional
for every choice. When a note is present, a review-note judge records how to
incorporate it and writes a reviewed draft plus judgment file when the note
changes the final artifact. The workflow then signals `approval_reply` back to
the case workflow. It only records the review decision; all outbound email,
portal posting, payment, media, legal notice sending, and case closure remain
manual.

Useful checks:

```bash
koyeb service get prr-pressure-cooker/worker
koyeb service logs prr-pressure-cooker/worker
```

The local `.env` remains the source for development only. Do not upload or commit `.env`.

Tailnet Proton/Himalaya stays outside Koyeb for this milestone. Use `prr ingest-push <case_id> <path> --backend mistral` from the tailnet host after exporting a raw `.eml`; the command imports and hashes immutable evidence locally, includes the case record and raw evidence in the workflow signal payload, and lets the Koyeb worker create the case and persist the evidence to `/data/casefiles` before routing it. `prr workflow signal-event <case_id> --event <event_id> --backend mistral` and `prr deadline scan --emit-events --backend mistral` also include event payloads so the hosted worker is not expected to see the tailnet host's local SQLite or casefiles directory. The tailnet host needs `MISTRAL_API_KEY`, `DEPLOYMENT_NAME=prr-pressure-cooker-prod`, and either `--backend mistral` or `PRR_WORKFLOW_BACKEND=mistral`; it does not need Koyeb to join Tailscale.
