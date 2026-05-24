from __future__ import annotations

import asyncio
import os

from prr_pressure_cooker.config import Settings, load_env_file


async def run_worker() -> None:
    load_env_file()
    settings = Settings.from_env()
    os.environ.setdefault("DEPLOYMENT_NAME", settings.deployment_name)
    if settings.workflow_api_base_url:
        os.environ.setdefault("SERVER_URL", settings.workflow_api_base_url)

    if not os.getenv("MISTRAL_API_KEY"):
        raise RuntimeError("MISTRAL_API_KEY is required to start a Mistral Workflows worker")
    from prr_pressure_cooker.workflows import discover_workflows

    workflow_classes = discover_workflows()
    if not workflow_classes:
        raise RuntimeError("mistralai-workflows is not installed or no workflows were discovered")

    import mistralai.workflows as workflows

    await workflows.run_worker(workflow_classes)


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
