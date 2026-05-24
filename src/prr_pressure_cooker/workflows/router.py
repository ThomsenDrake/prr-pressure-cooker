from __future__ import annotations

import re
from datetime import datetime, timedelta
from html import unescape
from zoneinfo import ZoneInfo

try:
    import mistralai.workflows as workflows
    import mistralai.workflows.plugins.mistralai as workflows_mistralai
    import temporalio.workflow
    from mistralai.workflows.conversational import FormInput, SingleChoice, TextField
    from pydantic import Field

    with temporalio.workflow.unsafe.imports_passed_through():
        from prr_pressure_cooker.config import Settings
        from prr_pressure_cooker.models import (
            ApprovalRecordInput,
            CaseWorkflowInput,
            CaseWorkflowSignal,
            CaseWorkflowStatus,
            RerouteCaseInput,
            ReviewAssistantInput,
            ReviewQueuePrompt,
            ReviewStatus,
            ReviewTaskPrompt,
            RouteEvaluationPayload,
            RouteEventInput,
            RouteEventResult,
        )
        from prr_pressure_cooker.service import (
            CASE_WORKFLOW_NAME,
            apply_review_record_if_pending,
            audit_fee_payload,
            build_packet_payload,
            classify_route_payload,
            compute_decision_payload,
            create_review_task_payload,
            get_case_status,
            is_route_payload_taskworthy,
            load_case_event,
            mark_case_workflow_resolved,
            persist_event_payload,
            persist_pushed_event_payload,
            reconcile_case_state,
            reroute_case,
            resolve_case_workflow,
            review_final_artifact,
            review_task_prompt,
            review_task_queue,
            route_event,
            route_result_for_payload,
            save_route_audit,
            start_case_workflow,
        )
        from prr_pressure_cooker.storage import Store
except ImportError:  # pragma: no cover - exercised only without optional runtime dependency
    workflows = None


if workflows is not None:

    class PRRReviewApprovalForm(FormInput):
        choice: str = SingleChoice(
            options=[
                (ReviewStatus.APPROVED.value, "Approve ready-to-send draft"),
                (ReviewStatus.REVISED.value, "Revise before manual send"),
                (ReviewStatus.DEFERRED.value, "Defer for more review"),
                (ReviewStatus.CANCELED.value, "Cancel this task"),
            ],
            description="Choose what to do with this PRR review task.",
        )
        note: str = Field(
            default="",
            description=(
                "Optional review note. Leave blank when no changes or context are needed."
            ),
        )

    class PRRReviewQueueSelectionForm(FormInput):
        selection: str = TextField(
            description="Enter the number of the pending review item to open."
        )

    @workflows.activity()
    async def load_case_activity(input_data: RouteEventInput) -> dict:
        input_data = RouteEventInput.model_validate(input_data)
        settings = Settings.from_env()
        store = Store(settings.db_path)
        payload = load_case_event(input_data.case_id, input_data.event_id, store)
        return payload.model_dump(mode="json")

    @workflows.activity()
    async def classify_event_activity(input_data: RouteEvaluationPayload) -> dict:
        payload = RouteEvaluationPayload.model_validate(input_data)
        payload = classify_route_payload(payload)
        return payload.model_dump(mode="json")

    @workflows.activity()
    async def audit_fee_estimate_activity(input_data: RouteEvaluationPayload) -> dict:
        payload = RouteEvaluationPayload.model_validate(input_data)
        payload = audit_fee_payload(payload)
        return payload.model_dump(mode="json")

    @workflows.activity()
    async def compute_decision_activity(input_data: RouteEvaluationPayload) -> dict:
        payload = RouteEvaluationPayload.model_validate(input_data)
        payload = compute_decision_payload(payload)
        return payload.model_dump(mode="json")

    @workflows.activity()
    async def persist_event_activity(input_data: RouteEvaluationPayload) -> dict:
        payload = RouteEvaluationPayload.model_validate(input_data)
        settings = Settings.from_env()
        store = Store(settings.db_path)
        payload = persist_event_payload(payload, store)
        return payload.model_dump(mode="json")

    @workflows.activity()
    async def build_packet_activity(input_data: RouteEvaluationPayload) -> dict:
        payload = RouteEvaluationPayload.model_validate(input_data)
        settings = Settings.from_env()
        store = Store(settings.db_path)
        payload = build_packet_payload(payload, store, settings)
        return payload.model_dump(mode="json")

    @workflows.activity()
    async def create_review_task_activity(input_data: RouteEvaluationPayload) -> dict:
        payload = RouteEvaluationPayload.model_validate(input_data)
        settings = Settings.from_env()
        store = Store(settings.db_path)
        payload = create_review_task_payload(payload, store)
        return payload.model_dump(mode="json")

    @workflows.activity()
    async def reroute_case_activity(input_data: RerouteCaseInput) -> dict:
        input_data = RerouteCaseInput.model_validate(input_data)
        settings = Settings.from_env()
        store = Store(settings.db_path)
        return reroute_case(
            input_data.case_id,
            store,
            settings,
            replace_tasks=input_data.replace_tasks,
        )

    @workflows.activity()
    async def save_route_audit_activity(input_data: RouteEventResult) -> dict:
        result = RouteEventResult.model_validate(input_data)
        settings = Settings.from_env()
        store = Store(settings.db_path)
        save_route_audit(result, store)
        return result.model_dump(mode="json")

    @workflows.activity()
    async def route_event_activity(input_data: RouteEventInput) -> dict:
        input_data = RouteEventInput.model_validate(input_data)
        settings = Settings.from_env()
        store = Store(settings.db_path)
        result = route_event(input_data.case_id, input_data.event_id, store, settings)
        return result.model_dump(mode="json")

    @workflows.activity()
    async def start_case_workflow_activity(input_data: CaseWorkflowInput) -> dict:
        input_data = CaseWorkflowInput.model_validate(input_data)
        settings = Settings.from_env()
        store = Store(settings.db_path)
        if input_data.case is not None:
            store.upsert_case(input_data.case)
        record = start_case_workflow(
            input_data.case_id,
            store,
            settings,
            execution_id=input_data.execution_id,
            backend=input_data.backend or "local",
            root_execution_id=input_data.root_execution_id,
            run_id=input_data.run_id,
            remote_status=input_data.remote_status,
            data={
                key: value
                for key, value in {
                    "source": "case_lifecycle_entrypoint",
                    "input_execution_id": input_data.execution_id,
                }.items()
                if value is not None
            },
        )
        return record.model_dump(mode="json")

    @workflows.activity()
    async def resolve_case_workflow_activity(input_data: CaseWorkflowInput) -> dict:
        input_data = CaseWorkflowInput.model_validate(input_data)
        settings = Settings.from_env()
        store = Store(settings.db_path)
        record = resolve_case_workflow(
            input_data.case_id,
            store,
            settings,
            note="Workflow manual resolution signal received.",
            execution_id=input_data.execution_id,
            backend=input_data.backend,
            root_execution_id=input_data.root_execution_id,
            run_id=input_data.run_id,
            remote_status=input_data.remote_status,
        )
        return record.model_dump(mode="json")

    @workflows.activity()
    async def persist_pushed_event_activity(input_data: CaseWorkflowSignal) -> dict:
        signal = CaseWorkflowSignal.model_validate(input_data)
        if signal.event_payload is None:
            return {"persisted": False, "event_id": signal.event_id}
        settings = Settings.from_env()
        store = Store(settings.db_path)
        event = persist_pushed_event_payload(signal.event_payload, store, settings)
        return {"persisted": True, "event_id": event.event_id}

    @workflows.activity()
    async def get_case_status_activity(input_data: CaseWorkflowInput) -> dict:
        input_data = CaseWorkflowInput.model_validate(input_data)
        settings = Settings.from_env()
        store = Store(settings.db_path)
        status = get_case_status(input_data.case_id, store)
        return status.model_dump(mode="json")

    @workflows.activity()
    async def record_approval_interaction_activity(input_data: ApprovalRecordInput) -> dict:
        input_data = ApprovalRecordInput.model_validate(input_data)
        settings = Settings.from_env()
        store = Store(settings.db_path)
        task, interaction, judgment, applied, reason = apply_review_record_if_pending(
            input_data, store
        )
        return {
            "applied": applied,
            "reason": reason,
            "task": task.model_dump(mode="json") if task else None,
            "interaction": interaction.model_dump(mode="json") if interaction else None,
            "judgment": judgment.model_dump(mode="json") if judgment else None,
        }

    @workflows.activity()
    async def get_review_task_prompt_activity(input_data: ReviewAssistantInput) -> dict:
        input_data = ReviewAssistantInput.model_validate(input_data or {})
        settings = Settings.from_env()
        store = Store(settings.db_path)
        prompt = review_task_prompt(input_data, store, settings)
        return prompt.model_dump(mode="json")

    @workflows.activity()
    async def get_review_task_queue_activity() -> dict:
        settings = Settings.from_env()
        store = Store(settings.db_path)
        queue = review_task_queue(store)
        queue = _filter_signalable_review_queue(queue, store, settings)
        return queue.model_dump(mode="json")

    @workflows.activity()
    async def reconcile_case_activity(input_data: CaseWorkflowInput) -> dict:
        input_data = CaseWorkflowInput.model_validate(input_data)
        settings = Settings.from_env()
        store = Store(settings.db_path)
        return reconcile_case_state(
            input_data.case_id,
            store,
            settings,
            execution_id=input_data.execution_id,
            backend=input_data.backend,
            root_execution_id=input_data.root_execution_id,
            run_id=input_data.run_id,
            remote_status=input_data.remote_status,
        )

    @workflows.activity()
    async def mark_case_workflow_resolved_activity(input_data: CaseWorkflowInput) -> dict:
        input_data = CaseWorkflowInput.model_validate(input_data)
        settings = Settings.from_env()
        store = Store(settings.db_path)
        record = mark_case_workflow_resolved(
            input_data.case_id,
            store,
            execution_id=input_data.execution_id,
            backend=input_data.backend,
            root_execution_id=input_data.root_execution_id,
            run_id=input_data.run_id,
            remote_status=input_data.remote_status,
            data={"resolved_by": "lifecycle_status_sync"},
        )
        return record.model_dump(mode="json") if record else {"resolved": False}

    @workflows.activity()
    async def signal_approval_reply_activity(input_data: ApprovalRecordInput) -> dict:
        input_data = ApprovalRecordInput.model_validate(input_data)
        settings = Settings.from_env()
        store = Store(settings.db_path)
        task = store.get_task(input_data.task_id)
        workflow_record = store.get_active_workflow_execution_for_case(
            task.case_id,
            workflow_name=CASE_WORKFLOW_NAME,
            backend="mistral",
        )
        if workflow_record is None:
            local_record = store.get_active_workflow_execution_for_case(
                task.case_id,
                workflow_name=CASE_WORKFLOW_NAME,
            )
            return _apply_review_locally(
                input_data,
                store,
                local_record.execution_id if local_record else None,
                "missing_remote_lifecycle_local_review",
                (
                    "No signalable remote case lifecycle execution was recorded; "
                    "review was recorded directly in the case store."
                ),
            )

        import os

        from mistralai.client import Mistral

        api_key = os.getenv("MISTRAL_API_KEY")
        if not api_key:
            raise RuntimeError("MISTRAL_API_KEY is required to signal approval replies")
        client = Mistral(api_key=api_key, server_url=settings.workflow_api_base_url)
        if _is_legacy_interactive_lifecycle(
            client, workflow_record.execution_id, settings.workflow_api_base_url
        ):
            return _apply_review_locally(
                input_data,
                store,
                workflow_record.execution_id,
                "legacy_local_review",
                (
                    "Legacy lifecycle execution was not signaled; review was "
                    "recorded directly in the case store."
                ),
            )
        try:
            response = client.workflows.executions.signal_workflow_execution(
                execution_id=workflow_record.execution_id,
                name="approval_reply",
                input=input_data.model_dump(mode="json"),
                server_url=settings.workflow_api_base_url,
            )
        except Exception as exc:
            if not _is_stale_lifecycle_signal_error(exc):
                raise
            return _apply_review_locally(
                input_data,
                store,
                workflow_record.execution_id,
                "stale_lifecycle_local_review",
                (
                    "The case lifecycle was no longer running, so the review was "
                    "recorded directly in the case store."
                ),
                signal_error=str(exc),
            )
        return {
            "case_id": task.case_id,
            "execution_id": workflow_record.execution_id,
            "signal": "approval_reply",
            "response": response.model_dump(mode="json")
            if hasattr(response, "model_dump")
            else response,
            "processed": False,
            "mode": "workflow_signal_accepted",
            "task": task.model_dump(mode="json"),
            "judgment": None,
        }

    def _apply_review_locally(
        input_data: ApprovalRecordInput,
        store: Store,
        execution_id: str | None,
        mode: str,
        message: str,
        signal_error: str | None = None,
    ) -> dict:
        task, interaction, judgment, applied, reason = apply_review_record_if_pending(
            input_data, store
        )
        result = {
            "case_id": task.case_id if task else None,
            "execution_id": execution_id,
            "signal": mode,
            "response": {
                "message": message,
                "mode": mode,
            },
            "processed": applied,
            "mode": mode,
            "reason": reason,
            "task": task.model_dump(mode="json") if task else None,
            "interaction": interaction.model_dump(mode="json") if interaction else None,
            "judgment": judgment.model_dump(mode="json") if judgment else None,
            "final_artifact": review_final_artifact(task, judgment),
        }
        if signal_error:
            result["signal_error"] = signal_error
        return result

    def _is_stale_lifecycle_signal_error(exc: Exception) -> bool:
        text = str(exc).lower()
        stale_terms = (
            "workflow not running",
            "wf_1102",
            "status 409",
            "status 404",
            "execution not found",
            '"status":"canceled"',
            '"status":"completed"',
            '"status":"timed_out"',
        )
        return any(term in text for term in stale_terms)

    def _filter_signalable_review_queue(
        queue: ReviewQueuePrompt, store: Store, settings: Settings
    ) -> ReviewQueuePrompt:
        import os

        from mistralai.client import Mistral

        api_key = os.getenv("MISTRAL_API_KEY")
        if not api_key or not queue.items:
            return queue

        client = Mistral(api_key=api_key, server_url=settings.workflow_api_base_url)
        actionable_items = []
        hidden_count = 0
        for item in queue.items:
            workflow_record = store.get_active_workflow_execution_for_case(
                item.case_id,
                workflow_name=CASE_WORKFLOW_NAME,
                backend="mistral",
            )
            if workflow_record is None:
                workflow_record = store.get_active_workflow_execution_for_case(
                    item.case_id,
                    workflow_name=CASE_WORKFLOW_NAME,
                )
            if workflow_record and _is_legacy_interactive_lifecycle(
                client, workflow_record.execution_id, settings.workflow_api_base_url
            ):
                hidden_count += 1
                continue
            actionable_items.append(item)

        if hidden_count == 0:
            return queue

        count = len(actionable_items)
        message = f"{count} actionable PRR review task{'s' if count != 1 else ''}."
        message += (
            f" Hidden {hidden_count} legacy task{'s' if hidden_count != 1 else ''} "
            "that must be rerouted under the current lifecycle before review."
        )
        return ReviewQueuePrompt(
            message=message,
            items=actionable_items,
            total_count=count,
        )

    def _is_legacy_interactive_lifecycle(
        client,
        execution_id: str,
        server_url: str | None,
    ) -> bool:
        try:
            history = client.workflows.executions.get_workflow_execution_history(
                execution_id=execution_id,
                decode_payloads=True,
                server_url=server_url,
            )
        except Exception:
            return False
        return _history_has_legacy_interactive_input(history)

    def _history_has_legacy_interactive_input(history: dict) -> bool:
        legacy_activity_types = {
            "__emit_waiting_for_input_started",
            "__emit_waiting_for_input_completed",
            "__emit_waiting_for_input_failed",
        }
        for event in history.get("events", []):
            attrs = event.get("markerRecordedEventAttributes")
            if attrs is None or attrs.get("markerName") != "core_local_activity":
                continue
            payloads = attrs.get("details", {}).get("data", {}).get("payloads", [])
            for payload in payloads:
                if payload.get("activity_type") in legacy_activity_types:
                    return True
        return False

    def _with_current_workflow_execution(input_data: CaseWorkflowInput) -> CaseWorkflowInput:
        if input_data.execution_id:
            return input_data
        try:
            info = temporalio.workflow.info()
        except Exception:
            return input_data
        execution_id = getattr(info, "workflow_id", None)
        if not execution_id:
            return input_data
        return input_data.model_copy(
            update={
                "execution_id": execution_id,
                "root_execution_id": execution_id,
                "run_id": getattr(info, "run_id", None),
                "backend": "mistral",
                "remote_status": "RUNNING",
            }
        )

    async def route_case_event_with_activities(input_data: RouteEventInput) -> dict:
        input_data = RouteEventInput.model_validate(input_data)
        payload = RouteEvaluationPayload.model_validate(await load_case_activity(input_data))
        payload = RouteEvaluationPayload.model_validate(await classify_event_activity(payload))
        payload = RouteEvaluationPayload.model_validate(await audit_fee_estimate_activity(payload))
        payload = RouteEvaluationPayload.model_validate(await compute_decision_activity(payload))
        payload = RouteEvaluationPayload.model_validate(await persist_event_activity(payload))
        if is_route_payload_taskworthy(payload):
            payload = RouteEvaluationPayload.model_validate(await build_packet_activity(payload))
            payload = RouteEvaluationPayload.model_validate(
                await create_review_task_activity(payload)
            )

        result = route_result_for_payload(payload)
        await save_route_audit_activity(result)
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
            input_data = RouteEventInput.model_validate(input_data)
            return await route_case_event_with_activities(input_data)

    @workflows.workflow.define(
        name="case-lifecycle-workflow",
        workflow_display_name="PRR Case Lifecycle",
        workflow_description=(
            "Tracks one PRR case, accepts external case-event signals, and pauses at "
            "human approval gates before producing ready-to-send artifacts."
        ),
        execution_timeout=timedelta(days=180),
    )
    class CaseLifecycleWorkflow(workflows.InteractiveWorkflow):
        def __init__(self) -> None:
            super().__init__()
            self.case_id: str | None = None
            self._event_queue: list[CaseWorkflowSignal] = []
            self._approval_queue: list[ApprovalRecordInput] = []
            self._resolved = False
            self._status: CaseWorkflowStatus | None = None
            self._input_data: CaseWorkflowInput | None = None

        @workflows.workflow.entrypoint
        async def run(self, input_data: CaseWorkflowInput) -> dict:
            input_data = CaseWorkflowInput.model_validate(input_data)
            input_data = _with_current_workflow_execution(input_data)
            self.case_id = input_data.case_id
            self._input_data = input_data
            await start_case_workflow_activity(input_data)
            await self._refresh_status(input_data)
            if input_data.initial_event_id:
                self._event_queue.append(
                    CaseWorkflowSignal(event_id=input_data.initial_event_id)
                )

            while not self._resolved:
                await temporalio.workflow.wait_condition(
                    lambda: self._resolved
                    or bool(self._event_queue)
                    or bool(self._approval_queue)
                )
                while self._event_queue:
                    signal = self._event_queue.pop(0)
                    if signal.event_payload is not None:
                        await persist_pushed_event_activity(signal)
                    await route_case_event_with_activities(
                        RouteEventInput(case_id=input_data.case_id, event_id=signal.event_id)
                    )
                    await reconcile_case_activity(input_data)
                    await self._refresh_status(input_data)
                while self._approval_queue and not self._resolved:
                    approval = self._approval_queue.pop(0)
                    await record_approval_interaction_activity(approval)
                    await self._refresh_status(input_data)

            return (
                self._status.model_dump(mode="json")
                if self._status
                else {"case_id": input_data.case_id, "status": "resolved"}
            )

        @workflows.workflow.signal(name="agency_event")
        async def agency_event(self, signal: CaseWorkflowSignal) -> None:
            self._queue_signal(signal, "agency_event")

        @workflows.workflow.signal(name="deadline_elapsed")
        async def deadline_elapsed(self, signal: CaseWorkflowSignal) -> None:
            self._queue_signal(signal, "deadline")

        @workflows.workflow.signal(name="human_reply")
        async def human_reply(self, signal: CaseWorkflowSignal) -> None:
            self._queue_signal(signal, "human_reply")

        @workflows.workflow.signal(name="payment")
        async def payment(self, signal: CaseWorkflowSignal) -> None:
            self._queue_signal(signal, "payment")

        @workflows.workflow.signal(name="records_release")
        async def records_release(self, signal: CaseWorkflowSignal) -> None:
            self._queue_signal(signal, "records_release")

        @workflows.workflow.signal(name="manual_resolution")
        async def manual_resolution(self) -> None:
            if self.case_id is not None:
                input_data = self._input_data or _with_current_workflow_execution(
                    CaseWorkflowInput(case_id=self.case_id)
                )
                await resolve_case_workflow_activity(input_data)
                self._status = CaseWorkflowStatus.model_validate(
                    await get_case_status_activity(input_data)
                )
            self._resolved = True

        @workflows.workflow.signal(name="approval_reply")
        async def approval_reply(self, approval: ApprovalRecordInput) -> None:
            self._approval_queue.append(ApprovalRecordInput.model_validate(approval))

        @workflows.workflow.query(name="get_case_status")
        def get_case_status(self) -> dict:
            return self._status.model_dump(mode="json") if self._status else {}

        def _queue_signal(self, signal: CaseWorkflowSignal, signal_type: str) -> None:
            parsed = CaseWorkflowSignal.model_validate(signal)
            self._event_queue.append(
                CaseWorkflowSignal(
                    event_id=parsed.event_id,
                    signal_type=signal_type,
                    event_payload=parsed.event_payload,
                )
            )

        async def _refresh_status(self, input_data: CaseWorkflowInput) -> None:
            self._status = CaseWorkflowStatus.model_validate(
                await get_case_status_activity(input_data)
            )
            if self._status.case_status == "RESOLVED":
                self._resolved = True

    @workflows.workflow.define(
        name="prr-review-assistant",
        workflow_display_name="PRR Review Assistant",
        workflow_description=(
            "Presents the next pending PRR approval task in Le Chat and submits the "
            "human decision back to the durable case lifecycle workflow."
        ),
        execution_timeout=timedelta(hours=1),
    )
    class PRRReviewAssistantWorkflow(workflows.InteractiveWorkflow):
        @workflows.workflow.entrypoint
        async def run(self) -> workflows_mistralai.ChatAssistantWorkflowOutput:
            queue = ReviewQueuePrompt.model_validate(await get_review_task_queue_activity())
            if not queue.items:
                await workflows_mistralai.send_assistant_message(queue.message)
                return workflows_mistralai.ChatAssistantWorkflowOutput(
                    content=[workflows_mistralai.TextOutput(text=queue.message)],
                    structuredContent={"status": "no_pending_review_task"},
                )
            if len(queue.items) > 1:
                await workflows_mistralai.send_assistant_message(
                    _review_queue_message(queue)
                )
                selected_task_id = await self._select_queue_task(queue)
                if selected_task_id is None:
                    message = (
                        "I could not match that selection. Start the assistant again "
                        "and choose one of the listed review numbers."
                    )
                    return workflows_mistralai.ChatAssistantWorkflowOutput(
                        content=[workflows_mistralai.TextOutput(text=message)],
                        structuredContent={"status": "invalid_queue_selection"},
                        isError=True,
                    )
                input_data = ReviewAssistantInput(task_id=selected_task_id)
            else:
                input_data = ReviewAssistantInput(task_id=queue.items[0].task_id)

            prompt = ReviewTaskPrompt.model_validate(
                await get_review_task_prompt_activity(input_data)
            )
            await workflows_mistralai.send_assistant_message(_review_task_message(prompt))
            if prompt.task is None:
                return workflows_mistralai.ChatAssistantWorkflowOutput(
                    content=[workflows_mistralai.TextOutput(text=prompt.message)],
                    structuredContent={"status": "no_pending_review_task"},
                )

            review = await self.wait_for_input(
                PRRReviewApprovalForm,
                label=_review_decision_label(prompt),
            )
            approval = ApprovalRecordInput(
                task_id=prompt.task.task_id,
                choice=ReviewStatus(review.choice),
                note=review.note.strip() or None,
            )
            signal = await signal_approval_reply_activity(approval)
            item_label = _review_decision_label(prompt).removeprefix("Decision for ")
            judgment = signal.get("judgment") if isinstance(signal, dict) else None
            message_parts = [f"Submitted `{approval.choice.value}` for {item_label}."]
            if judgment:
                message_parts.append(
                    f"Review-note judge: {judgment.get('summary', 'completed')}."
                )
            if signal.get("mode") == "legacy_local_review":
                message_parts.append(
                    "This was an older lifecycle run, so the review was recorded "
                    "directly in the case store instead of signaling the stale workflow."
                )
            elif signal.get("mode") == "workflow_signal_accepted":
                message_parts.append(
                    "The lifecycle accepted the signal and will process the review; "
                    "the task may remain visible briefly."
                )
            message_parts.append(
                "No outbound email, portal post, payment, media outreach, or case "
                "closure was performed."
            )
            message = " ".join(message_parts)
            final_artifact = _review_completion_artifact(prompt, approval, signal)
            if final_artifact:
                message = f"{message}\n\n{_final_artifact_block(final_artifact)}"
            return workflows_mistralai.ChatAssistantWorkflowOutput(
                content=[workflows_mistralai.TextOutput(text=message)],
                structuredContent={
                    "status": "submitted",
                    "task_id": approval.task_id,
                    "choice": approval.choice.value,
                    "signal": signal,
                    "final_artifact": final_artifact,
                },
            )

        async def _select_queue_task(self, queue: ReviewQueuePrompt) -> str | None:
            for attempt in range(2):
                selection = await self.wait_for_input(
                    PRRReviewQueueSelectionForm,
                    label="Choose a pending PRR review",
                )
                task_id = _task_id_from_queue_selection(queue, selection.selection)
                if task_id is not None:
                    return task_id
                if attempt == 0:
                    await workflows_mistralai.send_assistant_message(
                        f"Please enter a number from 1 to {len(queue.items)}."
                    )
            return None

    def _review_queue_message(queue: ReviewQueuePrompt) -> str:
        rows = []
        for index, item in enumerate(queue.items, start=1):
            title = _queue_item_title(item.agency, item.request_title)
            decision = _queue_action_label(item.proposed_action, item.pathway)
            pathway = _queue_pathway_label(item.pathway)
            pressure = _queue_pressure_label(item.pressure_score)
            due = _queue_due_label(item.due_at)
            reason = _queue_clean_text(item.action_reason or "No rationale captured.", 220)
            excerpt = _queue_clean_text(
                item.action_excerpt or item.latest_event_summary or "No event excerpt.",
                320,
                fallback=item.latest_event_summary,
            )
            rows.append(
                f"{index}. {title}\n"
                f"   Records requested: {_queue_record_request_label(item.request_summary)}\n"
                f"   Needed decision: {decision}\n"
                f"   Issue: {pathway}; pressure: {pressure}; deadline: {due}\n"
                f"   Why it is flagged: {reason}\n"
                f"   Latest message: {excerpt}"
            )
        return (
            f"{queue.message}\n\n"
            "Choose a number to open the review. You do not need a case number.\n\n"
            + "\n\n".join(rows)
        )

    def _task_id_from_queue_selection(
        queue: ReviewQueuePrompt, selection: str
    ) -> str | None:
        text = selection.strip()
        if not text:
            return None
        number_text = text.split(".", 1)[0].strip()
        if not number_text.isdigit():
            return None
        index = int(number_text)
        if 1 <= index <= len(queue.items):
            return queue.items[index - 1].task_id
        return None

    def _queue_record_request_label(request_summary: str | None) -> str:
        if not request_summary:
            return "not captured in the indexed case history"
        return _queue_clean_text(request_summary, 300)

    def _queue_item_title(agency: str, request_title: str) -> str:
        title = _strip_subject_noise(request_title)
        if (
            agency
            and not _queue_agency_is_generic(agency)
            and agency.lower() not in title.lower()
        ):
            title = f"{agency} - {title}"
        return _queue_clean_text(title or agency or "Untitled PRR review", 120)

    def _queue_agency_is_generic(agency: str) -> bool:
        return " ".join(agency.lower().split()) in {"xyz inbox", "publicrecords"}

    def _strip_subject_noise(title: str) -> str:
        normalized = " ".join(title.split())
        while True:
            trimmed = re.sub(r"^(?:re|fw|fwd)\s*:\s*", "", normalized, flags=re.I)
            if trimmed == normalized:
                break
            normalized = trimmed.strip()
        normalized = re.sub(
            r"^\[external message added\]\s*",
            "",
            normalized,
            flags=re.I,
        )
        normalized = re.sub(
            r"^orange county public records online payment confirmation\s*-\s*",
            "",
            normalized,
            flags=re.I,
        )
        return normalized.strip()

    def _queue_action_label(proposed_action: str, pathway: str) -> str:
        labels = {
            "no_withdrawal_preservation_reply": (
                "Keep the request open and preserve the objection to a closure or "
                "payment clock."
            ),
            "supervisor_review_request": (
                "Ask a supervisor to fix the estimate before any payment deadline runs."
            ),
            "particularized_estimate_request": (
                "Ask for a particularized estimate with enough detail to evaluate fees."
            ),
            "forced_agency_position_letter": (
                "Force the agency to state whether it has responsive records."
            ),
            "status_nudge": "Send a status nudge and keep the case active.",
            "withholding_exemption_matrix": (
                "Ask for a record-by-record withholding and exemption explanation."
            ),
            "duplicate_inflation_audit": (
                "Challenge duplicate or inflated record counts before accepting the estimate."
            ),
            "attorney_faf_packet": (
                "Prepare an attorney or mediation packet for an unresolved notice window."
            ),
            "commissioner_reporter_one_pager": (
                "Prepare a public-pressure one-pager for commissioners or reporters."
            ),
        }
        if proposed_action in labels:
            return labels[proposed_action]
        fallback = proposed_action or pathway or "review the next PRR action"
        return fallback.replace("_", " ").capitalize()

    def _queue_pathway_label(pathway: str) -> str:
        labels = {
            "closure_threat": "closure or payment deadline",
            "defective_estimate": "defective fee estimate",
            "fee_opacity": "unclear fee estimate",
            "custodian_dodge": "agency responsibility dodge",
            "silence_delay": "agency delay",
            "exemption_vagueness": "unclear exemption or withholding",
            "duplicate_inflation": "duplicate or inflated count",
            "public_pressure": "public-interest pressure packet",
            "counsel_or_mediation": "attorney or mediation review",
            "no_action": "no escalation needed",
        }
        return labels.get(str(pathway), str(pathway).replace("_", " "))

    def _queue_pressure_label(score: int) -> str:
        if score >= 8:
            level = "high"
        elif score >= 6:
            level = "elevated"
        elif score >= 4:
            level = "moderate"
        else:
            level = "low"
        return f"{level} ({score}/10)"

    def _queue_due_label(due_at: datetime | None) -> str:
        if due_at is None:
            return "no active deadline"
        try:
            due = due_at.astimezone(ZoneInfo("America/New_York"))
            suffix = " ET"
        except Exception:
            due = due_at
            suffix = " UTC"
        time_text = due.strftime("%I:%M %p").lstrip("0")
        return f"{due:%b} {due.day}, {due.year} at {time_text}{suffix}"

    def _queue_clean_text(text: str, max_chars: int, fallback: str | None = None) -> str:
        cleaned = unescape(text)
        if "<" in cleaned and ">" in cleaned:
            cleaned = re.sub(r"(?is)<(script|style).*?</\1>", " ", cleaned)
            cleaned = re.sub(r"(?s)<[^>]+>", " ", cleaned)
        cleaned = re.sub(
            r"(?is)attach a non-image file and/or reply above this line.*?request\.\s*",
            " ",
            cleaned,
        )
        cleaned = re.sub(r"(?i)^\s*--\s*", "", cleaned)
        cleaned = " ".join(cleaned.split())
        cleaned = re.sub(r"(?i)^\[external message added\]\s*", "", cleaned)
        cleaned = re.sub(r"(?i)^--\s*", "", cleaned)
        cleaned = re.sub(r"(?:\s+--)+\s+", " ", cleaned).strip(" -")
        if _queue_text_looks_like_markup(cleaned) and fallback and fallback != text:
            return _queue_clean_text(fallback, max_chars)
        if not cleaned:
            return "No text captured."
        if len(cleaned) <= max_chars:
            return cleaned
        return f"{cleaned[:max_chars].rstrip()}..."

    def _queue_text_looks_like_markup(text: str) -> bool:
        lowered = text[:160].lower()
        return lowered.startswith("<html") or "xmlns:" in lowered

    def _review_decision_label(prompt: ReviewTaskPrompt) -> str:
        case = prompt.case
        if case is None:
            return "Decision for pending PRR review"
        label = f"Decision for {case.agency} - {case.request_title}"
        return label[:120]

    def _review_task_message(prompt: ReviewTaskPrompt) -> str:
        if prompt.task is None:
            return prompt.message
        case = prompt.case
        task = prompt.task
        decision = prompt.decision
        case_context = prompt.case_context or "No case context available."
        history = _context_section(
            case_context,
            "Relevant case history:",
            "Indexed attachments:",
        )
        agency_excerpt = _context_section(
            case_context,
            "Agency text excerpt:",
        )
        attachments = _context_section(
            case_context,
            "Indexed attachments:",
            "Indexed contacts:",
        )
        draft_message = _draft_proposed_message(prompt.draft_preview)
        evidence_refs = _review_evidence_refs(prompt)
        deadline_summary = _review_deadline_summary(prompt)
        state_line = (
            f"{decision.current_state} -> {decision.recommended_next_state}"
            if decision
            else "unknown"
        )
        return (
            "PRR decision brief\n\n"
            f"Agency: {case.agency if case else 'Unknown'}\n"
            f"Request: {case.request_title if case else 'Unknown'}\n"
            f"Recommended action: {task.proposed_action} ({task.pathway})\n"
            f"State: {state_line}\n"
            f"Pressure score: {decision.pressure_score if decision else 'unknown'}\n"
            f"Deadline: {deadline_summary}\n\n"
            "Why this needs review:\n"
            f"- {decision.rationale if decision else 'unknown'}\n\n"
            "What happened:\n"
            f"{_bullet_block(agency_excerpt)}\n\n"
            "Relevant case history:\n"
            f"{_bullet_block(history)}\n\n"
            "Evidence to check:\n"
            f"{evidence_refs}\n"
            f"{_bullet_block(attachments)}\n\n"
            "Draft proposed message:\n"
            "```markdown\n"
            f"{draft_message}\n"
            "```\n\n"
            "Choose approve, revise, defer, or cancel. Notes are optional; when "
            "provided, the review-note judge records how to incorporate them into "
            "the final artifact. This only records a human review decision and "
            "prepares manual follow-up; it does not send anything."
        )

    def _context_section(text: str, start: str, end: str | None = None) -> str:
        start_index = text.find(start)
        if start_index < 0:
            return ""
        start_index += len(start)
        if end is None:
            return text[start_index:].strip()
        end_index = text.find(end, start_index)
        if end_index < 0:
            return text[start_index:].strip()
        return text[start_index:end_index].strip()

    def _bullet_block(text: str) -> str:
        lines = [line.rstrip() for line in text.splitlines() if line.strip()]
        return "\n".join(lines) if lines else "- Not captured."

    def _review_deadline_summary(prompt: ReviewTaskPrompt) -> str:
        if not prompt.active_deadlines:
            return "No active deadline."
        ordered = sorted(prompt.active_deadlines, key=lambda deadline: deadline.due_at)
        first = ordered[0]
        extra = len(ordered) - 1
        suffix = f" (+{extra} related deadline{'s' if extra != 1 else ''})" if extra else ""
        return f"{first.kind} due {first.due_at.isoformat()}{suffix}"

    def _review_evidence_refs(prompt: ReviewTaskPrompt) -> str:
        refs = prompt.decision.evidence_refs if prompt.decision else []
        if not refs:
            return "- Source evidence: not captured."
        return "- Source evidence: " + ", ".join(f"`{ref}`" for ref in refs)

    def _review_completion_artifact(
        prompt: ReviewTaskPrompt,
        approval: ApprovalRecordInput,
        signal: dict,
    ) -> dict | None:
        if approval.choice not in {ReviewStatus.APPROVED, ReviewStatus.REVISED}:
            return None
        signal_artifact = signal.get("final_artifact") if isinstance(signal, dict) else None
        if isinstance(signal_artifact, dict) and signal_artifact.get("content"):
            return signal_artifact
        draft_message = _draft_proposed_message(prompt.draft_preview)
        if not draft_message or draft_message == "No draft preview available.":
            return None
        draft_file = prompt.task.draft_file if prompt.task else "final-prr-response.md"
        filename = draft_file.rsplit("/", 1)[-1]
        if filename.endswith(".md"):
            filename = filename[:-3] + "_final.md"
        return {
            "format": "markdown",
            "filename": filename or "final-prr-response.md",
            "source_file": draft_file,
            "content": draft_message,
        }

    def _final_artifact_block(artifact: dict) -> str:
        filename = artifact.get("filename") or "final-prr-response.md"
        content = str(artifact.get("content") or "").strip()
        escaped = content.replace("```", "`\u200b``")
        return (
            f"Final artifact `{filename}` (copyable Markdown):\n\n"
            "```markdown\n"
            f"{escaped}\n"
            "```"
        )

    def _draft_proposed_message(draft_preview: str | None) -> str:
        if not draft_preview:
            return "No draft preview available."
        marker = "## Proposed Message"
        index = draft_preview.find(marker)
        if index < 0:
            return _trim_text(draft_preview, 1400)
        message = draft_preview[index + len(marker) :].strip()
        return _trim_text(message, 1400)

    def _trim_text(text: str, max_chars: int) -> str:
        normalized = text.strip()
        if len(normalized) <= max_chars:
            return normalized
        return f"{normalized[:max_chars].rstrip()}\n\n[truncated]"

    WORKFLOW_CLASSES = [
        PRREscalationRouter,
        CaseLifecycleWorkflow,
        PRRReviewAssistantWorkflow,
    ]
else:
    WORKFLOW_CLASSES = []
