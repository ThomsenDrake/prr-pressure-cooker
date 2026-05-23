from __future__ import annotations

from datetime import timedelta

from prr_pressure_cooker.config import Settings
from prr_pressure_cooker.models import RouteEventInput
from prr_pressure_cooker.service import route_event
from prr_pressure_cooker.storage import Store

try:
    import mistralai.workflows as workflows
except ImportError:  # pragma: no cover - exercised only without optional runtime dependency
    workflows = None


if workflows is not None:

    @workflows.activity()
    async def route_event_activity(input_data: RouteEventInput) -> dict:
        settings = Settings.from_env()
        store = Store(settings.db_path)
        result = route_event(input_data.case_id, input_data.event_id, store, settings)
        return result.model_dump(mode="json")

    @workflows.workflow.define(
        name="prr-escalation-router",
        workflow_display_name="PRR Escalation Router",
        workflow_description="Classifies a PRR event and prepares the next human-review action.",
        execution_timeout=timedelta(days=7),
    )
    class PRREscalationRouter:
        @workflows.workflow.entrypoint
        async def run(self, input_data: RouteEventInput) -> dict:
            return await route_event_activity(input_data)

    @workflows.workflow.define(
        name="case-lifecycle-workflow",
        workflow_display_name="PRR Case Lifecycle",
        workflow_description="Routes a single PRR case event through the pressure ladder.",
        execution_timeout=timedelta(days=30),
    )
    class CaseLifecycleWorkflow:
        @workflows.workflow.entrypoint
        async def run(self, input_data: RouteEventInput) -> dict:
            return await route_event_activity(input_data)

    WORKFLOW_CLASSES = [PRREscalationRouter, CaseLifecycleWorkflow]
else:
    WORKFLOW_CLASSES = []
