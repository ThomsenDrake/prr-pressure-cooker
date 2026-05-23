from __future__ import annotations

import asyncio
import os

from prr_pressure_cooker.config import Settings, load_env_file
from prr_pressure_cooker.workflows import WORKFLOW_CLASSES


async def run_worker() -> None:
    load_env_file()
    settings = Settings.from_env()
    os.environ.setdefault("DEPLOYMENT_NAME", settings.deployment_name)

    if not os.getenv("MISTRAL_API_KEY"):
        raise RuntimeError("MISTRAL_API_KEY is required to start a Mistral Workflows worker")
    if not WORKFLOW_CLASSES:
        raise RuntimeError("mistralai-workflows is not installed or no workflows were discovered")

    import mistralai.workflows as workflows

    await workflows.run_worker(WORKFLOW_CLASSES)


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
