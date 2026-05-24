from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

import mistralai.workflows as workflows

from prr_pressure_cooker.cli import build_parser
from prr_pressure_cooker.config import Settings
from prr_pressure_cooker.ingest import import_path
from prr_pressure_cooker.models import (
    ApprovalRecordInput,
    CaseEvent,
    CaseWorkflowInput,
    CaseWorkflowSignal,
    ReviewAssistantInput,
    ReviewQueueItem,
    ReviewQueuePrompt,
    ReviewStatus,
    ReviewTaskPrompt,
    RouteEventInput,
)
from prr_pressure_cooker.service import (
    _clean_request_summary,
    _natural_language_request_summary,
    apply_review_choice,
    create_deadline,
    get_case_status,
    resolve_case_workflow,
    review_task_prompt,
    review_task_queue,
    route_event,
    scan_deadlines,
    signal_case_event,
)
from prr_pressure_cooker.storage import Store
from prr_pressure_cooker.workflows import discover_workflows
from prr_pressure_cooker.workflows import router as workflow_router
from prr_pressure_cooker.workflows.router import PRREscalationRouter, PRRReviewAssistantWorkflow


def _settings(tmp_path: Path) -> Settings:
    return Settings(db_path=tmp_path / "prr.db", casefiles_dir=tmp_path / "casefiles")


def _write_review_email(
    directory: Path,
    filename: str,
    *,
    sender: str,
    recipient: str,
    subject: str,
    date: str,
    body: str,
) -> Path:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message["Date"] = date
    message["Message-ID"] = f"<{filename}@example.test>"
    message.set_content(body)
    path = directory / filename
    path.write_bytes(message.as_bytes())
    return path


def test_workflow_signal_records_execution_status_and_artifacts(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.create_case("demo", "Demo Agency", "Demo request")
    event = import_path("demo", Path("tests/fixtures/defective_estimate.txt"), store, settings)[0]

    payload = signal_case_event("demo", event.event_id, store, settings)

    workflow = store.get_workflow_execution_for_case("demo")
    status = get_case_status("demo", store)
    state = store.get_case_state("demo")
    artifacts = store.list_packet_artifacts("demo")

    assert payload["result"]["status"] == "waiting_for_human_review"
    assert workflow is not None
    assert workflow.workflow_name == "case-lifecycle-workflow"
    assert workflow.latest_event_id == event.event_id
    assert status.case_status == "HUMAN_REVIEW"
    assert status.pending_task_id is not None
    assert status.pressure_score == 8
    assert state is not None
    assert state.status == "HUMAN_REVIEW"
    assert state.pending_task_id == status.pending_task_id
    assert state.latest_event_id == event.event_id
    assert state.pressure_score == 8
    assert {artifact.artifact_type for artifact in artifacts} >= {
        "00_case_summary",
        "01_timeline",
        "02_evidence_refs",
        "03_fee_audit",
        "README",
        "index:messages",
        "index:threads",
        "index:attachments",
        "index:contacts",
    }
    assert len(store.list_route_audits("demo")) == 1


def test_review_choice_updates_task_and_records_interaction(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.create_case("demo", "Demo Agency", "Demo request")
    event = import_path("demo", Path("tests/fixtures/defective_estimate.txt"), store, settings)[0]
    signal_case_event("demo", event.event_id, store, settings)
    task = store.list_tasks(status="pending", case_id="demo")[0]

    updated_task, interaction, judgment = apply_review_choice(
        task, ReviewStatus.DEFERRED, "Need to verify the invoice.", store
    )

    assert updated_task.status == "deferred"
    assert interaction.choice == "deferred"
    assert judgment is not None
    assert judgment.disposition == "record_only"
    assert judgment.final_draft_file is None
    assert judgment.judgment_file is not None
    assert Path(judgment.judgment_file).exists()
    assert store.get_task(task.task_id).status == "deferred"
    assert len(store.list_approval_interactions("demo")) == 1


def test_review_choice_notes_are_optional_for_approval(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.create_case("demo", "Demo Agency", "Demo request")
    event = import_path("demo", Path("tests/fixtures/defective_estimate.txt"), store, settings)[0]
    signal_case_event("demo", event.event_id, store, settings)
    task = store.list_tasks(status="pending", case_id="demo")[0]

    updated_task, interaction, judgment = apply_review_choice(
        task, ReviewStatus.APPROVED, None, store
    )

    assert updated_task.status == "approved"
    assert updated_task.human_note is None
    assert interaction.note is None
    assert judgment is None
    state = store.get_case_state("demo")
    assert state is not None
    assert state.status == "READY_TO_SEND"
    assert state.pending_task_id is None


def test_review_note_judge_applies_firm_note_to_reviewed_draft(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.create_case("demo", "Demo Agency", "Demo request")
    event = import_path("demo", Path("tests/fixtures/defective_estimate.txt"), store, settings)[0]
    signal_case_event("demo", event.event_id, store, settings)
    task = store.list_tasks(status="pending", case_id="demo")[0]

    _updated_task, _interaction, judgment = apply_review_choice(
        task,
        ReviewStatus.APPROVED,
        "Make language more firm but good to send otherwise",
        store,
    )

    assert judgment is not None
    assert judgment.disposition == "apply_tone_edit"
    assert judgment.final_draft_file is not None
    assert store.get_task(task.task_id).draft_file == judgment.final_draft_file
    final_draft = Path(judgment.final_draft_file).read_text(encoding="utf-8")
    assert "corrected, itemized estimate" in final_draft
    assert "Review Note Judge" in final_draft
    artifacts = store.list_packet_artifacts("demo")
    assert any(artifact.artifact_type == "reviewed_draft" for artifact in artifacts)
    assert any(artifact.artifact_type == "review_note_judgment" for artifact in artifacts)


def test_review_task_prompt_includes_case_task_and_draft_preview(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.create_case("demo", "Demo Agency", "Demo request")
    event = import_path("demo", Path("tests/fixtures/defective_estimate.txt"), store, settings)[0]
    signal_case_event("demo", event.event_id, store, settings)
    task = store.list_tasks(status="pending", case_id="demo")[0]

    prompt = review_task_prompt(
        ReviewAssistantInput(case_id="demo"),
        store,
        settings,
    )

    assert prompt.task is not None
    assert prompt.task.task_id == task.task_id
    assert prompt.case is not None
    assert prompt.case.case_id == "demo"
    assert prompt.decision is not None
    assert prompt.decision.pathway == "defective_estimate"
    assert prompt.event is not None
    assert prompt.event.event_id == event.event_id
    assert prompt.case_context is not None
    assert "Agency text excerpt" in prompt.case_context
    assert "Research labor" in prompt.case_context
    assert prompt.packet_context is not None
    assert "Fee Audit" in prompt.packet_context
    assert prompt.draft_preview is not None
    assert "does not reconcile" in prompt.draft_preview


def test_review_task_queue_uses_human_labels_not_required_case_ids(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.create_case("demo-a", "Demo Agency A", "Invoice review")
    event_a = import_path("demo-a", Path("tests/fixtures/defective_estimate.txt"), store, settings)[
        0
    ]
    signal_case_event("demo-a", event_a.event_id, store, settings)
    store.create_case("demo-b", "Demo Agency B", "Fee estimate review")
    event_b = import_path("demo-b", Path("tests/fixtures/defective_estimate.txt"), store, settings)[
        0
    ]
    signal_case_event("demo-b", event_b.event_id, store, settings)

    queue = review_task_queue(store)

    assert queue.total_count == 2
    assert [item.agency for item in queue.items] == ["Demo Agency A", "Demo Agency B"]
    assert [item.request_title for item in queue.items] == [
        "Invoice review",
        "Fee estimate review",
    ]
    assert all(item.task_id.startswith("task_") for item in queue.items)


def test_review_queue_filter_hides_legacy_interactive_lifecycle_tasks(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.create_case("legacy", "Legacy Agency", "Legacy request")
    legacy_event = import_path(
        "legacy", Path("tests/fixtures/defective_estimate.txt"), store, settings
    )[0]
    signal_case_event("legacy", legacy_event.event_id, store, settings)
    store.create_case("current", "Current Agency", "Current request")
    current_event = import_path(
        "current", Path("tests/fixtures/defective_estimate.txt"), store, settings
    )[0]
    signal_case_event("current", current_event.event_id, store, settings)
    legacy_record = store.get_workflow_execution_for_case("legacy")
    assert legacy_record is not None
    queue = review_task_queue(store)

    def fake_legacy_check(_client, execution_id, _server_url):
        return execution_id == legacy_record.execution_id

    monkeypatch.setattr(workflow_router, "_is_legacy_interactive_lifecycle", fake_legacy_check)

    filtered = workflow_router._filter_signalable_review_queue(queue, store, settings)

    assert filtered.total_count == 1
    assert [item.agency for item in filtered.items] == ["Current Agency"]
    assert "Hidden 1 legacy task" in filtered.message


def test_history_has_legacy_interactive_input_marker():
    history = {
        "events": [
            {
                "markerRecordedEventAttributes": {
                    "markerName": "core_local_activity",
                    "details": {
                        "data": {
                            "payloads": [{"activity_type": "__emit_waiting_for_input_started"}]
                        }
                    },
                }
            }
        ]
    }

    assert workflow_router._history_has_legacy_interactive_input(history)


def test_start_case_workflow_activity_records_remote_execution_id(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "prr.db"
    casefiles_dir = tmp_path / "casefiles"
    monkeypatch.setenv("PRR_DB_PATH", str(db_path))
    monkeypatch.setenv("PRR_CASEFILES_DIR", str(casefiles_dir))
    settings = Settings.from_env()
    Store(settings.db_path).create_case("demo", "Demo Agency", "Demo request")

    result = asyncio.run(
        workflow_router.start_case_workflow_activity(
            CaseWorkflowInput(
                case_id="demo",
                execution_id="wf_remote_actual",
                root_execution_id="wf_remote_actual",
                run_id="run_actual",
                backend="mistral",
                remote_status="RUNNING",
            )
        )
    )

    stored = Store(settings.db_path).get_active_workflow_execution_for_case(
        "demo",
        workflow_name="case-lifecycle-workflow",
        backend="mistral",
    )
    assert result["execution_id"] == "wf_remote_actual"
    assert result["backend"] == "mistral"
    assert stored is not None
    assert stored.execution_id == "wf_remote_actual"
    assert stored.run_id == "run_actual"


def test_signal_approval_reply_records_legacy_lifecycle_locally(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "prr.db"
    casefiles_dir = tmp_path / "casefiles"
    monkeypatch.setenv("PRR_DB_PATH", str(db_path))
    monkeypatch.setenv("PRR_CASEFILES_DIR", str(casefiles_dir))
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    settings = Settings.from_env()
    store = Store(settings.db_path)
    store.create_case("legacy", "Legacy Agency", "Legacy request")
    event = import_path("legacy", Path("tests/fixtures/defective_estimate.txt"), store, settings)[0]
    signal_case_event("legacy", event.event_id, store, settings)
    task = store.list_tasks(status=ReviewStatus.PENDING.value, case_id="legacy")[0]
    workflow_record = store.get_workflow_execution_for_case("legacy")
    assert workflow_record is not None
    workflow_record = workflow_record.model_copy(update={"backend": "mistral"})
    store.save_workflow_execution(workflow_record)

    class FakeExecutions:
        def signal_workflow_execution(self, **_kwargs):
            raise AssertionError("legacy lifecycle should not be signaled")

    class FakeWorkflows:
        executions = FakeExecutions()

    class FakeMistral:
        workflows = FakeWorkflows()

        def __init__(self, **_kwargs):
            pass

    def fake_legacy_check(_client, execution_id, _server_url):
        return execution_id == workflow_record.execution_id

    monkeypatch.setattr("mistralai.client.Mistral", FakeMistral)
    monkeypatch.setattr(workflow_router, "_is_legacy_interactive_lifecycle", fake_legacy_check)

    result = asyncio.run(
        workflow_router.signal_approval_reply_activity(
            ApprovalRecordInput(task_id=task.task_id, choice=ReviewStatus.APPROVED)
        )
    )

    updated = Store(settings.db_path).get_task(task.task_id)
    assert result["mode"] == "legacy_local_review"
    assert result["processed"] is True
    assert result["judgment"] is None
    assert result["execution_id"] == workflow_record.execution_id
    assert updated.status == ReviewStatus.APPROVED


def test_signal_approval_reply_does_not_locally_apply_current_signal(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "prr.db"
    casefiles_dir = tmp_path / "casefiles"
    monkeypatch.setenv("PRR_DB_PATH", str(db_path))
    monkeypatch.setenv("PRR_CASEFILES_DIR", str(casefiles_dir))
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    settings = Settings.from_env()
    store = Store(settings.db_path)
    store.create_case("current", "Current Agency", "Current request")
    event = import_path("current", Path("tests/fixtures/defective_estimate.txt"), store, settings)[
        0
    ]
    signal_case_event("current", event.event_id, store, settings)
    task = store.list_tasks(status=ReviewStatus.PENDING.value, case_id="current")[0]
    workflow_record = store.get_workflow_execution_for_case("current")
    assert workflow_record is not None
    store.save_workflow_execution(workflow_record.model_copy(update={"backend": "mistral"}))

    class FakeSignalResponse:
        def model_dump(self, mode="json"):
            assert mode == "json"
            return {"message": "Signal accepted"}

    class FakeExecutions:
        def signal_workflow_execution(self, **_kwargs):
            return FakeSignalResponse()

    class FakeWorkflows:
        executions = FakeExecutions()

    class FakeMistral:
        workflows = FakeWorkflows()

        def __init__(self, **_kwargs):
            pass

    monkeypatch.setattr("mistralai.client.Mistral", FakeMistral)
    monkeypatch.setattr(workflow_router, "_is_legacy_interactive_lifecycle", lambda *_args: False)

    result = asyncio.run(
        workflow_router.signal_approval_reply_activity(
            ApprovalRecordInput(task_id=task.task_id, choice=ReviewStatus.APPROVED)
        )
    )

    updated = Store(settings.db_path).get_task(task.task_id)
    assert result["mode"] == "workflow_signal_accepted"
    assert result["processed"] is False
    assert result["response"] == {"message": "Signal accepted"}
    assert updated.status == ReviewStatus.PENDING
    assert Store(settings.db_path).list_approval_interactions("current") == []


def test_signal_approval_reply_records_stale_lifecycle_locally(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "prr.db"
    casefiles_dir = tmp_path / "casefiles"
    monkeypatch.setenv("PRR_DB_PATH", str(db_path))
    monkeypatch.setenv("PRR_CASEFILES_DIR", str(casefiles_dir))
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    settings = Settings.from_env()
    store = Store(settings.db_path)
    store.create_case("stale", "Stale Agency", "Stale request")
    event = import_path("stale", Path("tests/fixtures/defective_estimate.txt"), store, settings)[0]
    signal_case_event("stale", event.event_id, store, settings)
    task = store.list_tasks(status=ReviewStatus.PENDING.value, case_id="stale")[0]
    workflow_record = store.get_workflow_execution_for_case("stale")
    assert workflow_record is not None
    store.save_workflow_execution(workflow_record.model_copy(update={"backend": "mistral"}))

    class FakeExecutions:
        def signal_workflow_execution(self, **_kwargs):
            raise RuntimeError(
                'API error occurred: Status 409. Body: {"detail":"Workflow not running",'
                '"code":"WF_1102","status":"CANCELED"}'
            )

    class FakeWorkflows:
        executions = FakeExecutions()

    class FakeMistral:
        workflows = FakeWorkflows()

        def __init__(self, **_kwargs):
            pass

    monkeypatch.setattr("mistralai.client.Mistral", FakeMistral)
    monkeypatch.setattr(workflow_router, "_is_legacy_interactive_lifecycle", lambda *_args: False)

    result = asyncio.run(
        workflow_router.signal_approval_reply_activity(
            ApprovalRecordInput(task_id=task.task_id, choice=ReviewStatus.APPROVED)
        )
    )

    updated = Store(settings.db_path).get_task(task.task_id)
    assert result["mode"] == "stale_lifecycle_local_review"
    assert result["processed"] is True
    assert result["reason"] is None
    assert result["final_artifact"]["content"]
    assert "Workflow not running" in result["signal_error"]
    assert updated.status == ReviewStatus.APPROVED


def test_deadline_scan_emits_and_routes_elapsed_events(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.create_case("demo", "Demo Agency", "Demo request")
    event = CaseEvent(
        event_id="evt_ack",
        case_id="demo",
        event_type="agency_message_received",
        received_at=datetime(2026, 5, 1, tzinfo=UTC),
        summary="Request submitted successfully",
        content_text="Your public records request has been submitted successfully.",
    )
    store.save_event(event)

    route_event("demo", "evt_ack", store, settings)
    result = scan_deadlines(store, settings, emit_events=True)

    assert result["due"] == 1
    assert result["items"][0]["route"]["pathway"] == "silence_delay"
    assert store.list_deadlines(case_id="demo", status="emitted")
    assert len(store.list_tasks(status="pending", case_id="demo")) == 1


def test_workflow_discovery_finds_router_and_case_lifecycle():
    names = {workflow_class.__name__ for workflow_class in discover_workflows()}

    assert {"PRREscalationRouter", "CaseLifecycleWorkflow", "PRRReviewAssistantWorkflow"} <= names


def test_required_workflow_activities_are_registered():
    required = {
        "load_case_activity",
        "persist_event_activity",
        "classify_event_activity",
        "audit_fee_estimate_activity",
        "compute_decision_activity",
        "reroute_case_activity",
        "resolve_case_workflow_activity",
        "create_review_task_activity",
        "build_packet_activity",
        "get_review_task_prompt_activity",
        "get_review_task_queue_activity",
        "reconcile_case_activity",
        "mark_case_workflow_resolved_activity",
        "signal_approval_reply_activity",
    }

    for name in required:
        activity = getattr(workflow_router, name)
        definition = getattr(activity, "__temporal_activity_definition", None)
        assert definition is not None
        assert definition.name == name


def test_review_approval_form_note_is_optional():
    schema = workflow_router.PRRReviewApprovalForm.model_json_schema()

    assert "note" not in schema.get("required", [])
    form = workflow_router.PRRReviewApprovalForm(choice=ReviewStatus.APPROVED.value)
    assert form.note == ""


def test_router_workflow_runs_non_interactive_activity_pipeline(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PRR_DB_PATH", str(tmp_path / "prr.db"))
    monkeypatch.setenv("PRR_CASEFILES_DIR", str(tmp_path / "casefiles"))
    settings = Settings.from_env()
    store = Store(settings.db_path)
    store.create_case("demo", "Demo Agency", "Demo request")
    event = import_path("demo", Path("tests/fixtures/defective_estimate.txt"), store, settings)[0]

    result = asyncio.run(
        workflows.execute_workflow(
            PRREscalationRouter,
            RouteEventInput(case_id="demo", event_id=event.event_id),
        )
    )

    assert result["status"] == "waiting_for_human_review"
    assert result["pathway"] == "defective_estimate"
    assert len(Store(settings.db_path).list_tasks(status="pending", case_id="demo")) == 1


def test_case_lifecycle_workflow_accepts_approval_reply_signal(monkeypatch):
    workflow = workflow_router.CaseLifecycleWorkflow()
    recorded: list[ApprovalRecordInput] = []

    async def fake_start_case_workflow(input_data):
        return {"case_id": CaseWorkflowInput.model_validate(input_data).case_id}

    async def fake_get_case_status(input_data):
        input_data = CaseWorkflowInput.model_validate(input_data)
        return {
            "case_id": input_data.case_id,
            "case_status": "HUMAN_REVIEW",
            "pending_task_id": "task_review",
            "active_deadlines": [],
            "latest_event_id": "evt_review",
            "latest_event_summary": "Estimate needs review",
            "pressure_score": 8,
        }

    async def fake_route_case_event(input_data):
        input_data = RouteEventInput.model_validate(input_data)
        return {
            "case_id": input_data.case_id,
            "event_id": input_data.event_id,
            "decision_id": "dec_review",
            "task_id": "task_review",
            "pathway": "defective_estimate",
            "status": "waiting_for_human_review",
        }

    async def fake_record_approval(input_data):
        recorded.append(ApprovalRecordInput.model_validate(input_data))
        workflow._resolved = True
        return {
            "applied": True,
            "task": {"task_id": "task_review"},
            "interaction": {"choice": "deferred"},
        }

    async def fake_reconcile_case(input_data):
        input_data = CaseWorkflowInput.model_validate(input_data)
        return {
            "case_id": input_data.case_id,
            "replace_tasks": True,
            "status": {"case_id": input_data.case_id, "case_status": "HUMAN_REVIEW"},
        }

    async def fake_wait_condition(predicate):
        assert predicate()

    monkeypatch.setattr(workflow_router, "start_case_workflow_activity", fake_start_case_workflow)
    monkeypatch.setattr(workflow_router, "get_case_status_activity", fake_get_case_status)
    monkeypatch.setattr(workflow_router, "route_case_event_with_activities", fake_route_case_event)
    monkeypatch.setattr(
        workflow_router, "record_approval_interaction_activity", fake_record_approval
    )
    monkeypatch.setattr(workflow_router, "reconcile_case_activity", fake_reconcile_case)
    monkeypatch.setattr(workflow_router.temporalio.workflow, "wait_condition", fake_wait_condition)

    asyncio.run(
        workflow.approval_reply(
            ApprovalRecordInput(
                task_id="task_review",
                choice=ReviewStatus.DEFERRED,
                note="Need invoice backup.",
            )
        )
    )
    result = asyncio.run(
        workflow.run(CaseWorkflowInput(case_id="demo", initial_event_id="evt_review"))
    )

    assert recorded == [
        ApprovalRecordInput(
            task_id="task_review",
            choice=ReviewStatus.DEFERRED,
            note="Need invoice backup.",
        )
    ]
    assert result["result"]["case_id"] == "demo"
    assert result["result"]["case_status"] == "HUMAN_REVIEW"


def test_case_lifecycle_processes_later_event_while_review_is_pending(monkeypatch):
    workflow = workflow_router.CaseLifecycleWorkflow()
    routed_events: list[str] = []
    reconciled: list[str] = []
    recorded: list[ApprovalRecordInput] = []

    async def fake_start_case_workflow(input_data):
        return {"case_id": CaseWorkflowInput.model_validate(input_data).case_id}

    async def fake_get_case_status(input_data):
        input_data = CaseWorkflowInput.model_validate(input_data)
        if routed_events[-1:] == ["evt_paid"]:
            return {
                "case_id": input_data.case_id,
                "case_status": "WAITING_FOR_AGENCY",
                "pending_task_id": None,
                "active_deadlines": [],
                "latest_event_id": "evt_paid",
                "latest_event_summary": "Payment submitted",
                "pressure_score": 0,
            }
        return {
            "case_id": input_data.case_id,
            "case_status": "HUMAN_REVIEW",
            "pending_task_id": "task_fee",
            "active_deadlines": [],
            "latest_event_id": "evt_fee",
            "latest_event_summary": "Estimate needs review",
            "pressure_score": 5,
        }

    async def fake_route_case_event(input_data):
        input_data = RouteEventInput.model_validate(input_data)
        routed_events.append(input_data.event_id)
        return {
            "case_id": input_data.case_id,
            "event_id": input_data.event_id,
            "decision_id": f"dec_{input_data.event_id}",
            "task_id": "task_fee" if input_data.event_id == "evt_fee" else None,
            "pathway": "fee_opacity" if input_data.event_id == "evt_fee" else "no_action",
            "status": "waiting_for_human_review"
            if input_data.event_id == "evt_fee"
            else "updated_no_action_required",
        }

    async def fake_reconcile_case(input_data):
        input_data = CaseWorkflowInput.model_validate(input_data)
        reconciled.append(routed_events[-1])
        if routed_events[-1] == "evt_paid":
            workflow._resolved = True
        return {
            "case_id": input_data.case_id,
            "replace_tasks": True,
            "status": {"case_id": input_data.case_id},
        }

    async def fake_record_approval(input_data):
        recorded.append(ApprovalRecordInput.model_validate(input_data))
        return {"applied": True}

    async def fake_wait_condition(predicate):
        if not predicate() and routed_events == ["evt_fee"]:
            await workflow.payment(CaseWorkflowSignal(event_id="evt_paid", signal_type="payment"))
        assert predicate()

    monkeypatch.setattr(workflow_router, "start_case_workflow_activity", fake_start_case_workflow)
    monkeypatch.setattr(workflow_router, "get_case_status_activity", fake_get_case_status)
    monkeypatch.setattr(workflow_router, "route_case_event_with_activities", fake_route_case_event)
    monkeypatch.setattr(workflow_router, "reconcile_case_activity", fake_reconcile_case)
    monkeypatch.setattr(
        workflow_router, "record_approval_interaction_activity", fake_record_approval
    )
    monkeypatch.setattr(workflow_router.temporalio.workflow, "wait_condition", fake_wait_condition)

    result = asyncio.run(
        workflow.run(CaseWorkflowInput(case_id="demo", initial_event_id="evt_fee"))
    )

    assert routed_events == ["evt_fee", "evt_paid"]
    assert reconciled == ["evt_fee", "evt_paid"]
    assert recorded == []
    assert result["result"]["pending_task_id"] is None


def test_case_lifecycle_exits_when_reconciled_status_is_resolved(monkeypatch):
    workflow = workflow_router.CaseLifecycleWorkflow()
    routed_events: list[str] = []
    marked_resolved: list[str] = []
    reconciled = False

    async def fake_start_case_workflow(input_data):
        return {"case_id": CaseWorkflowInput.model_validate(input_data).case_id}

    async def fake_get_case_status(input_data):
        input_data = CaseWorkflowInput.model_validate(input_data)
        if reconciled:
            return {
                "case_id": input_data.case_id,
                "case_status": "RESOLVED",
                "pending_task_id": None,
                "active_deadlines": [],
                "latest_event_id": "evt_release",
                "latest_event_summary": "Documents have been released.",
                "pressure_score": 0,
            }
        return {
            "case_id": input_data.case_id,
            "case_status": "WAITING_FOR_AGENCY",
            "pending_task_id": None,
            "active_deadlines": [],
            "latest_event_id": "evt_ack",
            "latest_event_summary": "Request acknowledged.",
            "pressure_score": 0,
        }

    async def fake_route_case_event(input_data):
        input_data = RouteEventInput.model_validate(input_data)
        routed_events.append(input_data.event_id)
        return {
            "case_id": input_data.case_id,
            "event_id": input_data.event_id,
            "decision_id": f"dec_{input_data.event_id}",
            "task_id": None,
            "pathway": "no_action",
            "status": "updated_no_action_required",
        }

    async def fake_reconcile_case(input_data):
        nonlocal reconciled
        reconciled = True
        return {
            "case_id": CaseWorkflowInput.model_validate(input_data).case_id,
            "replace_tasks": True,
            "status": {"case_status": "RESOLVED"},
        }

    async def fake_mark_resolved(input_data):
        marked_resolved.append(CaseWorkflowInput.model_validate(input_data).case_id)
        return {"execution_id": "wf_demo", "status": "resolved"}

    async def fake_wait_condition(predicate):
        assert predicate()

    monkeypatch.setattr(workflow_router, "start_case_workflow_activity", fake_start_case_workflow)
    monkeypatch.setattr(workflow_router, "get_case_status_activity", fake_get_case_status)
    monkeypatch.setattr(workflow_router, "route_case_event_with_activities", fake_route_case_event)
    monkeypatch.setattr(workflow_router, "reconcile_case_activity", fake_reconcile_case)
    monkeypatch.setattr(
        workflow_router,
        "mark_case_workflow_resolved_activity",
        fake_mark_resolved,
    )
    monkeypatch.setattr(workflow_router.temporalio.workflow, "wait_condition", fake_wait_condition)

    result = asyncio.run(
        workflow.run(CaseWorkflowInput(case_id="demo", initial_event_id="evt_release"))
    )

    assert routed_events == ["evt_release"]
    assert marked_resolved == []
    assert workflow._resolved is True
    assert result["result"]["case_id"] == "demo"
    assert result["result"]["case_status"] == "RESOLVED"


def test_case_lifecycle_ready_to_send_is_not_terminal(monkeypatch):
    workflow = workflow_router.CaseLifecycleWorkflow()
    marked_resolved: list[str] = []

    async def fake_get_case_status(input_data):
        input_data = CaseWorkflowInput.model_validate(input_data)
        return {
            "case_id": input_data.case_id,
            "case_status": "READY_TO_SEND",
            "pending_task_id": None,
            "active_deadlines": [],
            "latest_event_id": "evt_reviewed",
            "latest_event_summary": "Draft approved.",
            "pressure_score": 0,
        }

    async def fake_mark_resolved(input_data):
        marked_resolved.append(CaseWorkflowInput.model_validate(input_data).case_id)
        return {"execution_id": "wf_demo", "status": "resolved"}

    monkeypatch.setattr(workflow_router, "get_case_status_activity", fake_get_case_status)
    monkeypatch.setattr(
        workflow_router,
        "mark_case_workflow_resolved_activity",
        fake_mark_resolved,
    )

    asyncio.run(workflow._refresh_status(CaseWorkflowInput(case_id="demo")))

    assert workflow._status is not None
    assert workflow._status.case_status == "READY_TO_SEND"
    assert workflow._resolved is False
    assert marked_resolved == []


def test_prr_review_assistant_collects_chat_approval_and_signals_lifecycle(
    tmp_path: Path, monkeypatch
):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.create_case("demo", "Demo Agency", "Demo request")
    event = import_path("demo", Path("tests/fixtures/defective_estimate.txt"), store, settings)[0]
    signal_case_event("demo", event.event_id, store, settings)
    prompt = review_task_prompt(ReviewAssistantInput(case_id="demo"), store, settings)
    queue = review_task_queue(store)
    assert prompt.task is not None
    assert len(queue.items) == 1

    sent_messages: list[str] = []
    signaled: list[ApprovalRecordInput] = []
    workflow = PRRReviewAssistantWorkflow()

    async def fake_get_review_task_queue():
        return queue.model_dump(mode="json")

    async def fake_get_review_task_prompt(input_data):
        assert ReviewAssistantInput.model_validate(input_data).task_id == prompt.task.task_id
        return prompt.model_dump(mode="json")

    async def fake_send_assistant_message(message, **kwargs):
        sent_messages.append(message)

    async def fake_wait_for_input(schema, *, label=None, timeout=None):
        assert schema is workflow_router.PRRReviewApprovalForm
        assert label == "Decision for Demo Agency - Demo request"
        assert timeout is None
        return workflow_router.PRRReviewApprovalForm(
            choice=ReviewStatus.APPROVED.value,
            note="Reviewed and ready for manual send.",
        )

    async def fake_signal_approval(input_data):
        signaled.append(ApprovalRecordInput.model_validate(input_data))
        return {
            "signal": "approval_reply",
            "accepted": True,
            "judgment": {
                "summary": "Applied the note as a tone edit and produced a firmer reviewed draft."
            },
        }

    monkeypatch.setattr(
        workflow_router, "get_review_task_queue_activity", fake_get_review_task_queue
    )
    monkeypatch.setattr(
        workflow_router, "get_review_task_prompt_activity", fake_get_review_task_prompt
    )
    monkeypatch.setattr(
        workflow_router.workflows_mistralai,
        "send_assistant_message",
        fake_send_assistant_message,
    )
    monkeypatch.setattr(workflow, "wait_for_input", fake_wait_for_input)
    monkeypatch.setattr(workflow_router, "signal_approval_reply_activity", fake_signal_approval)

    result = asyncio.run(workflow.run())

    assert sent_messages
    assert "PRR decision brief" in sent_messages[0]
    visible_summary = sent_messages[0].split("Draft proposed message:", 1)[0]
    assert "Case:" not in visible_summary
    assert "Task:" not in visible_summary
    assert "What happened:" in sent_messages[0]
    assert "Relevant case history:" in sent_messages[0]
    assert "Research labor" in sent_messages[0]
    assert "Evidence packet context:" not in sent_messages[0]
    assert "Packet files:" not in sent_messages[0]
    assert "does not send anything" in sent_messages[0]
    assert signaled == [
        ApprovalRecordInput(
            task_id=prompt.task.task_id,
            choice=ReviewStatus.APPROVED,
            note="Reviewed and ready for manual send.",
        )
    ]
    output = result.get("result", result)
    assert "Review-note judge:" in output["content"][0]["text"]
    assert output["structuredContent"]["status"] == "submitted"
    assert output["structuredContent"]["choice"] == "approved"


def test_review_task_message_is_decision_brief_with_later_reply_context(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.create_case(
        "corr",
        "Osceola County Corrections",
        "CORR-2026-300 Deposit Payment Needed",
    )
    agency_event = import_path(
        "corr",
        _write_review_email(
            tmp_path,
            "closure.eml",
            sender=(
                "Ree Nimcharoen (Osceola County Board of County Commissioners) "
                "<OsceolaCounty@request.justfoia.com>"
            ),
            recipient="Drake <drake@draket.xyz>",
            subject="CORR-2026-300: Deposit Payment Needed",
            date="Wed, 20 May 2026 13:32:07 +0000",
            body=(
                "Please be advised that the last clarification communication was "
                "completed on May 12, 2026. If a response is not received within "
                "10 business days from May 12, 2026, your request will be closed."
            ),
        ),
        store,
        settings,
    )[0]
    route_event("corr", agency_event.event_id, store, settings)
    import_path(
        "corr",
        _write_review_email(
            tmp_path,
            "reply.eml",
            sender="Drake <drake@draket.xyz>",
            recipient="OsceolaCounty@request.justfoia.com",
            subject="Re: CORR-2026-300: Deposit Payment Needed",
            date="Wed, 20 May 2026 14:55:11 +0000",
            body=(
                "I am not withdrawing Request CORR-2026-300, and I will not pay "
                "any deposit until the county provides a particularized estimate. "
                "Please keep the request open while I await a lawful estimate."
            ),
        ),
        store,
        settings,
    )

    prompt = review_task_prompt(ReviewAssistantInput(case_id="corr"), store, settings)
    message = workflow_router._review_task_message(prompt)

    assert "PRR decision brief" in message
    assert "What happened:" in message
    assert "Relevant case history:" in message
    assert "Later requester reply" in message
    assert "I am not withdrawing Request CORR-2026-300" in message
    assert "Evidence packet context:" not in message
    assert "Packet files:" not in message
    assert "messages.csv" not in message
    assert message.count("Agency appears to attach a closure clock") == 1
    assert len(message) < 4200


def test_prr_review_assistant_can_start_from_queue_without_case_id(monkeypatch):
    now = datetime(2026, 5, 23, tzinfo=UTC)
    queue = ReviewQueuePrompt(
        message="2 pending PRR review tasks.",
        total_count=2,
        items=[
            ReviewQueueItem(
                task_id="task_a",
                case_id="case-a",
                agency="Demo Agency A",
                request_title="Invoice review",
                pathway="defective_estimate",
                proposed_action="defective_estimate_reply",
                pressure_score=8,
                latest_event_summary="Estimate needs review",
                due_at=now,
                created_at=now,
            ),
            ReviewQueueItem(
                task_id="task_b",
                case_id="case-b",
                agency="Demo Agency B",
                request_title="Closure warning",
                pathway="closure_threat",
                proposed_action="closure_threat_reply",
                pressure_score=9,
                latest_event_summary="Agency may close the request",
                due_at=now,
                created_at=now,
            ),
        ],
    )
    prompt = ReviewTaskPrompt(
        message="Pending PRR review task is ready.",
        case={
            "case_id": "case-b",
            "agency": "Demo Agency B",
            "request_title": "Closure warning",
            "status": "HUMAN_REVIEW",
            "created_at": now,
            "updated_at": now,
            "data": {},
        },
        task={
            "task_id": "task_b",
            "case_id": "case-b",
            "decision_id": "dec_b",
            "pathway": "closure_threat",
            "proposed_action": "closure_threat_reply",
            "status": "pending",
            "draft_file": "/tmp/draft.md",
            "evidence_packet": [],
            "choices": ["approve", "revise", "defer", "cancel"],
            "required_human_note": True,
            "human_note": None,
            "created_at": now,
            "updated_at": now,
        },
        draft_preview="Draft content",
    )
    sent_messages: list[str] = []
    prompt_inputs: list[ReviewAssistantInput] = []
    signaled: list[ApprovalRecordInput] = []
    workflow = PRRReviewAssistantWorkflow()

    async def fake_get_review_task_queue():
        return queue.model_dump(mode="json")

    async def fake_get_review_task_prompt(input_data):
        parsed = ReviewAssistantInput.model_validate(input_data)
        prompt_inputs.append(parsed)
        return prompt.model_dump(mode="json")

    async def fake_send_assistant_message(message, **kwargs):
        sent_messages.append(message)

    async def fake_wait_for_input(schema, *, label=None, timeout=None):
        if schema is workflow_router.PRRReviewQueueSelectionForm:
            assert label == "Choose a pending PRR review"
            return workflow_router.PRRReviewQueueSelectionForm(selection="2")
        assert schema is workflow_router.PRRReviewApprovalForm
        assert label == "Decision for Demo Agency B - Closure warning"
        return workflow_router.PRRReviewApprovalForm(
            choice=ReviewStatus.APPROVED.value,
            note="",
        )

    async def fake_signal_approval(input_data):
        signaled.append(ApprovalRecordInput.model_validate(input_data))
        return {"signal": "approval_reply", "accepted": True}

    monkeypatch.setattr(
        workflow_router, "get_review_task_queue_activity", fake_get_review_task_queue
    )
    monkeypatch.setattr(
        workflow_router, "get_review_task_prompt_activity", fake_get_review_task_prompt
    )
    monkeypatch.setattr(
        workflow_router.workflows_mistralai,
        "send_assistant_message",
        fake_send_assistant_message,
    )
    monkeypatch.setattr(workflow, "wait_for_input", fake_wait_for_input)
    monkeypatch.setattr(workflow_router, "signal_approval_reply_activity", fake_signal_approval)

    result = asyncio.run(workflow.run())

    assert "Choose a number to open the review" in sent_messages[0]
    assert "Needed decision:" in sent_messages[0]
    assert "Why it is flagged:" in sent_messages[0]
    assert "Latest message:" in sent_messages[0]
    assert "Demo Agency A - Invoice review" in sent_messages[0]
    assert "Demo Agency B - Closure warning" in sent_messages[0]
    assert prompt_inputs == [ReviewAssistantInput(task_id="task_b")]
    assert signaled == [
        ApprovalRecordInput(
            task_id="task_b",
            choice=ReviewStatus.APPROVED,
            note=None,
        )
    ]
    output = result.get("result", result)
    output_text = json.dumps(output, default=str)
    assert "Final artifact" in output_text
    assert "```markdown" in output_text
    assert "Draft content" in output_text
    assert output["structuredContent"]["status"] == "submitted"
    assert output["structuredContent"]["task_id"] == "task_b"
    assert output["structuredContent"]["final_artifact"]["content"] == "Draft content"


def test_review_queue_message_uses_human_legible_case_cards():
    due_at = datetime(2026, 5, 25, 12, 25, tzinfo=UTC)
    queue = ReviewQueuePrompt(
        message="2 pending PRR review tasks.",
        total_count=2,
        items=[
            ReviewQueueItem(
                task_id="task_a",
                case_id="case-a",
                agency="XYZ Inbox",
                request_title=(
                    "Re: FWD: Re: Re: Osceola County Corrections - "
                    "Corrections Records Request CORR-2026-300: Deposit Payment Needed"
                ),
                request_summary=(
                    "Phase 1 financial/reimbursement records and Phase 2 "
                    "correspondence for CORR-2026-300."
                ),
                pathway="closure_threat",
                proposed_action="no_withdrawal_preservation_reply",
                pressure_score=6,
                latest_event_summary="Deposit Payment Needed",
                action_reason=(
                    "Agency appears to attach a closure clock to a pending "
                    "clarification, fee, or response issue."
                ),
                action_excerpt=(
                    "Good morning, Please be advised that the last clarification "
                    "communication was completed on May 12, 2026."
                ),
                due_at=due_at,
                created_at=due_at,
            ),
            ReviewQueueItem(
                task_id="task_b",
                case_id="case-b",
                agency="Orlando Public Records",
                request_title=(
                    "FWD: [External Message Added] Orlando public records request #26-11231"
                ),
                request_summary=(
                    "Code Enforcement records plus email, Teams, and text-message searches."
                ),
                pathway="public_pressure",
                proposed_action="commissioner_reporter_one_pager",
                pressure_score=4,
                latest_event_summary="External Message Added",
                action_reason=(
                    "The event contains public-interest, press, or commissioner "
                    "context that may support a public pressure packet."
                ),
                action_excerpt=(
                    "<html><head><style>.x{}</style></head><body>"
                    "-- Attach a non-image file and/or reply ABOVE THIS LINE with "
                    "a message, and it will be sent to staff on this request. -- "
                    "Orlando Public Records A message was added by staff.</body></html>"
                ),
                due_at=due_at,
                created_at=due_at,
            ),
        ],
    )

    message = workflow_router._review_queue_message(queue)

    assert "Choose a number to open the review" in message
    assert "Records requested: Phase 1 financial/reimbursement records" in message
    assert "Records requested: Code Enforcement records plus email" in message
    assert "Needed decision: Keep the request open" in message
    assert "Issue: closure or payment deadline; pressure: elevated (6/10)" in message
    assert "deadline: May 25, 2026 at 8:25 AM ET" in message
    assert "Prepare a public-pressure one-pager" in message
    assert "Orlando public records request #26-11231" in message
    assert "Latest message: Orlando Public Records A message was added by staff." in message
    assert "XYZ Inbox" not in message
    assert "no_withdrawal_preservation_reply" not in message
    assert "closure_threat" not in message
    assert "2026-05-25T12:25" not in message
    assert "<html" not in message
    assert "Attach a non-image" not in message
    assert "Re: FWD:" not in message
    assert "Your estimated total is" not in _clean_request_summary(
        "Your estimated total is: $121.28 2 hours 35 minutes at $27.09 per hour "
        "for Code Enforcement staff time for an estimated cost of $69.98"
    )


def test_review_queue_derives_requested_records_from_raw_case_history(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.create_case(
        "scout",
        "Seminole County",
        "RE: Formal Escalation - Scout Microtransit Program",
    )
    event = import_path(
        "scout",
        _write_review_email(
            tmp_path,
            "closure.eml",
            sender="PublicRecords <publicrecords@example.test>",
            recipient="Drake <drake@example.test>",
            subject="RE: Formal Escalation - Scout Microtransit Program",
            date="Mon, 09 Mar 2026 15:49:14 +0000",
            body=(
                "Please respond within two days or we will consider this request "
                "closed.\n\n"
                "On Mon, Mar 9, 2026 at 11:04 AM Drake wrote:\n"
                "MY ORIGINAL REQUEST SOUGHT:\n"
                "A comprehensive set of public records related to the Scout "
                "microtransit program, including the RFP, vendor proposals, "
                "contract, financial records, performance data, and staff/vendor "
                "communications.\n"
                "DEMAND FOR IMMEDIATE ACTION:\n"
                "Please acknowledge receipt and produce the requested records."
            ),
        ),
        store,
        settings,
    )[0]
    route_event("scout", event.event_id, store, settings)

    queue = review_task_queue(store)
    assert len(queue.items) == 1
    assert queue.items[0].request_summary is not None
    assert "Scout microtransit program" in queue.items[0].request_summary
    assert "vendor proposals" in queue.items[0].request_summary
    assert "A comprehensive set of public records" not in queue.items[0].request_summary

    message = workflow_router._review_queue_message(queue)
    assert "Records requested: Scout microtransit program records" in message
    assert "DEMAND FOR IMMEDIATE ACTION" not in message


def test_request_summary_naturalizes_agency_source_text():
    orange_summary = _natural_language_request_summary(
        "Per your public record request PRR- 163721, ISS ran the following search: "
        "Timeframe: 04/21/2026 - 05/13/2026 Emails: County wide, no OCSO "
        'Keywords: (("48-hour ICE hold" OR "48 hour ICE hold") OR '
        '("72-hour ICE hold" OR "72 hour ICE hold") OR '
        '("IGSA to BOA transition" OR "48 vs 72 hour hold" OR '
        '"ICE release" OR "Transfer authority")) = 115'
    )
    assert orange_summary == (
        "County-wide emails from April 21, 2026 to May 13, 2026, excluding "
        "OCSO, about 48- and 72-hour ICE hold terms, the IGSA-to-BOA "
        "transition, ICE release, and transfer authority."
    )
    assert "Per your public record request" not in orange_summary
    assert "ISS ran" not in orange_summary
    assert "Keywords:" not in orange_summary

    orlando_summary = _natural_language_request_summary(
        "Code Enforcement; Records (Technology Search - Emails); "
        "Records (Technology Search - Teams messages); "
        "Records (Technology Search - Text messages)"
    )
    assert orlando_summary == (
        "Code Enforcement records plus technology searches of emails, "
        "Teams messages, and text messages."
    )

    corrections_summary = _natural_language_request_summary(
        "Records Technician: estimated 2 to 3 hours for Phase 1 #3 and 1 to "
        "2 hours for Phase 2. Inmate Services Personnel: estimated 10 hours "
        "for Phase 1 #4; Finance Personnel: estimated 40 hours primarily "
        "for Phase 1 financial/reimbursement records, with Phase 2 treated "
        "as overlapping because reimbursement appears in both portions."
    )
    assert corrections_summary == (
        "Osceola County Corrections records, including financial and "
        "reimbursement records, inmate services records, and related Phase 2 "
        "reimbursement or correspondence records."
    )
    assert "estimated" not in corrections_summary
    assert "Records Technician" not in corrections_summary


def test_review_prompt_refreshes_draft_with_full_case_history(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.create_case(
        "allmail-records-corr-2026-300",
        "Osceola County Corrections",
        "Osceola County Corrections Records Request CORR-2026-300",
    )
    agency_event = CaseEvent(
        event_id="evt_agency",
        case_id="allmail-records-corr-2026-300",
        event_type="agency_message_received",
        received_at=datetime(2026, 5, 20, 13, 32, tzinfo=UTC),
        summary="Deposit Payment Needed",
        content_text=(
            "Please note, if a response is not received within 10 business days "
            "from May 12, 2026, your request will be closed."
        ),
    )
    store.save_event(agency_event)
    route_event("allmail-records-corr-2026-300", "evt_agency", store, settings)
    task = store.list_tasks(
        status=ReviewStatus.PENDING.value,
        case_id="allmail-records-corr-2026-300",
    )[0]
    original_draft = Path(task.draft_file).read_text(encoding="utf-8")
    assert "53 staff hours" not in original_draft

    store.save_event(
        CaseEvent(
            event_id="evt_requester",
            case_id="allmail-records-corr-2026-300",
            event_type="human_sent_message",
            received_at=datetime(2026, 5, 20, 14, 55, tzinfo=UTC),
            summary="Requester disputed estimate",
            content_text=(
                "Good afternoon Ms. Nimcharoen,\n"
                "The estimate is a total of 53 staff hours--3 hours for a Records "
                "Technician, 10 hours for Inmate Services personnel, and 40 hours "
                "for Finance personnel--at a total cost of $1,318.86 with a $659.43 "
                "deposit. The estimate gives no document count, task breakdown, "
                "or explanation of why 53 hours are required, and it does not say "
                "whether duplicates were removed before estimating the time. I "
                "remain willing to proceed with Phase 1 on its own or narrow the "
                "date range. Please keep the request open."
            ),
        )
    )

    prompt = review_task_prompt(ReviewAssistantInput(task_id=task.task_id), store, settings)

    assert prompt.draft_preview is not None
    assert "I already responded on May 20, 2026" in prompt.draft_preview
    assert "53 staff hours" in prompt.draft_preview
    assert "40-hour Finance Personnel line" in prompt.draft_preview
    assert "whether duplicates were excluded before estimating time" in prompt.draft_preview
    assert "I remain willing to proceed in phases or narrow the request" in prompt.draft_preview


def test_ingest_push_cli_imports_fixture_and_signals_workflow(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("PRR_DB_PATH", str(tmp_path / "prr.db"))
    monkeypatch.setenv("PRR_CASEFILES_DIR", str(tmp_path / "casefiles"))
    settings = Settings.from_env()
    store = Store(settings.db_path)
    store.create_case("demo", "Demo Agency", "Demo request")

    parser = build_parser()
    args = parser.parse_args(["ingest-push", "demo", "tests/fixtures/defective_estimate.txt"])
    args.func(args)
    payload = json.loads(capsys.readouterr().out)

    assert payload["case_id"] == "demo"
    assert payload["imported"]
    assert payload["signaled"][0]["result"]["status"] == "waiting_for_human_review"
    assert Store(settings.db_path).get_workflow_execution_for_case("demo") is not None
    assert len(Store(settings.db_path).list_tasks(status="pending", case_id="demo")) == 1


def test_manual_resolution_marks_case_resolved_and_closes_local_state(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.create_case("demo", "Demo Agency", "Demo request")
    event = import_path("demo", Path("tests/fixtures/defective_estimate.txt"), store, settings)[0]
    signal_case_event("demo", event.event_id, store, settings)
    store.save_deadline(
        create_deadline("demo", event.event_id, "manual_followup", event.received_at)
    )

    record = resolve_case_workflow("demo", store, settings, note="Closed after release.")
    status = get_case_status("demo", store)

    assert record.status == "resolved"
    assert status.case_status == "RESOLVED"
    assert status.pending_task_id is None
    assert status.active_deadlines == []
    assert store.list_tasks(status="canceled", case_id="demo")[0].human_note == (
        "Closed after release."
    )


def test_workflow_resolve_case_cli_uses_runtime(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("PRR_DB_PATH", str(tmp_path / "prr.db"))
    monkeypatch.setenv("PRR_CASEFILES_DIR", str(tmp_path / "casefiles"))
    settings = Settings.from_env()
    Store(settings.db_path).create_case("demo", "Demo Agency", "Demo request")

    parser = build_parser()
    args = parser.parse_args(["workflow", "resolve-case", "demo"])
    args.func(args)
    payload = json.loads(capsys.readouterr().out)

    assert payload["workflow"]["status"] == "resolved"
    assert payload["status"]["case_status"] == "RESOLVED"
