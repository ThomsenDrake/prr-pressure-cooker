# Koyeb Worker Deployment

Mistral Workflows workers can run as long-lived Koyeb worker services. The worker entrypoint is:

```bash
uv run prr worker
```

The worker needs these runtime variables:

- `MISTRAL_API_KEY`, configured from a Koyeb Secret.
- `DEPLOYMENT_NAME`, for example `prr-pressure-cooker-prod`.
- `PRR_DB_PATH`, defaulting to `/app/var/prr.db` in the container.
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
uv run prr deploy koyeb --execute
```

The generated command deploys the current project directory as a Docker-built Koyeb `WORKER` service and sets `MISTRAL_API_KEY={{ secret.MISTRAL_API_KEY }}` plus `DEPLOYMENT_NAME`.

Useful checks:

```bash
koyeb service get prr-pressure-cooker/worker
koyeb service logs prr-pressure-cooker/worker
```

The local `.env` remains the source for development only. Do not upload or commit `.env`.
