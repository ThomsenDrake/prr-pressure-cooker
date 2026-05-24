from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from prr_pressure_cooker import cli
from prr_pressure_cooker.config import Settings
from prr_pressure_cooker.ids import content_id, utc_now
from prr_pressure_cooker.ingest import import_path
from prr_pressure_cooker.models import (
    CaseEvent,
    CaseWorkflowSignal,
    WorkflowExecutionRecord,
    WorkflowExecutionStatus,
)
from prr_pressure_cooker.service import (
    persist_pushed_event_payload,
    route_event,
    start_case_workflow,
)
from prr_pressure_cooker.storage import Store
from prr_pressure_cooker.workflow_runtime import MistralWorkflowRuntime


class FakeResponse(BaseModel):
    execution_id: str | None = None
    status: str | None = None
    result: dict[str, Any] | None = None
    message: str | None = None


class FakeExecutions:
    def __init__(self) -> None:
        self.signals: list[dict[str, Any]] = []
        self.queries: list[dict[str, Any]] = []

    def signal_workflow_execution(self, **kwargs) -> FakeResponse:
        self.signals.append(kwargs)
        return FakeResponse(message="Signal accepted")

    def query_workflow_execution(self, **kwargs) -> FakeResponse:
        self.queries.append(kwargs)
        return FakeResponse(
            result={
                "case_id": "demo",
                "case_status": "INTAKE",
                "pending_task_id": None,
                "active_deadlines": [],
                "latest_event_id": "evt_demo",
                "latest_event_summary": "latest",
                "pressure_score": 0,
            }
        )


class FakeWorkflows:
    def __init__(self) -> None:
        self.executions = FakeExecutions()
        self.executed: list[dict[str, Any]] = []

    def execute_workflow(self, **kwargs) -> FakeResponse:
        self.executed.append(kwargs)
        return FakeResponse(execution_id=kwargs["execution_id"], status="RUNNING")


class FakeMistral:
    def __init__(self) -> None:
        self.workflows = FakeWorkflows()


class FakeRunExecution(BaseModel):
    workflow_name: str = "case-lifecycle-workflow"
    execution_id: str
    root_execution_id: str
    status: str = "RUNNING"
    start_time: datetime
    end_time: datetime | None = None
    run_id: str | None = None


class FakeRunList(BaseModel):
    executions: list[FakeRunExecution]


class FakeRuns:
    def __init__(self, executions: list[FakeRunExecution]) -> None:
        self.executions = executions
        self.list_calls: list[dict[str, Any]] = []

    def list_runs(self, **kwargs) -> FakeRunList:
        self.list_calls.append(kwargs)
        return FakeRunList(executions=self.executions)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "prr.db",
        casefiles_dir=tmp_path / "casefiles",
        deployment_name="prr-pressure-cooker-prod",
        workflow_backend="mistral",
        workflow_api_base_url="https://workflow.example.test",
    )


def test_store_keeps_multiple_workflows_and_prefers_active_mistral_lifecycle(
    tmp_path: Path,
):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.create_case("demo", "Demo Agency", "Demo request")
    now = utc_now()
    local = WorkflowExecutionRecord(
        execution_id="wf_local",
        case_id="demo",
        workflow_name="case-lifecycle-workflow",
        backend="local",
        status=WorkflowExecutionStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    remote = WorkflowExecutionRecord(
        execution_id="wf_remote",
        case_id="demo",
        workflow_name="case-lifecycle-workflow",
        backend="mistral",
        status=WorkflowExecutionStatus.ACTIVE,
        run_id="run_remote",
        remote_status="RUNNING",
        created_at=now,
        updated_at=now,
    )
    helper = WorkflowExecutionRecord(
        execution_id="wf_review",
        case_id="demo",
        workflow_name="prr-review-assistant",
        backend="mistral",
        status=WorkflowExecutionStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    store.save_workflow_execution(local)
    store.save_workflow_execution(remote)
    store.save_workflow_execution(helper)

    selected = store.get_active_workflow_execution_for_case(
        "demo",
        workflow_name="case-lifecycle-workflow",
        backend="mistral",
    )

    assert selected is not None
    assert selected.execution_id == "wf_remote"
    assert selected.run_id == "run_remote"
    assert {
        record.execution_id for record in store.list_workflow_executions("demo")
    } == {"wf_local", "wf_remote", "wf_review"}


def test_start_case_workflow_can_record_remote_lifecycle_execution(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.create_case("demo", "Demo Agency", "Demo request")

    record = start_case_workflow(
        "demo",
        store,
        settings,
        execution_id="wf_remote_actual",
        backend="mistral",
        root_execution_id="wf_remote_actual",
        run_id="run_actual",
        remote_status="RUNNING",
        data={"source": "case_lifecycle_entrypoint"},
    )
    selected = store.get_active_workflow_execution_for_case(
        "demo",
        workflow_name="case-lifecycle-workflow",
        backend="mistral",
    )

    assert record.execution_id == "wf_remote_actual"
    assert selected is not None
    assert selected.execution_id == "wf_remote_actual"
    assert selected.run_id == "run_actual"
    assert selected.remote_status == "RUNNING"
    assert selected.data["source"] == "case_lifecycle_entrypoint"


def test_mistral_runtime_starts_signals_and_queries_remote_workflow(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.create_case("demo", "Demo Agency", "Demo request")
    fake_client = FakeMistral()
    runtime = MistralWorkflowRuntime(store, settings, client=fake_client)

    started = runtime.start_case("demo")
    signaled = runtime.signal_event(
        "demo",
        CaseWorkflowSignal(event_id="evt_demo", signal_type="agency_event"),
    )
    resolved = runtime.resolve_case("demo")
    status = runtime.status("demo")
    fresh_settings = Settings(
        db_path=tmp_path / "fresh.db",
        casefiles_dir=tmp_path / "fresh-casefiles",
        deployment_name="prr-pressure-cooker-prod",
        workflow_backend="mistral",
        workflow_api_base_url="https://workflow.example.test",
    )
    fresh_status = MistralWorkflowRuntime(
        Store(fresh_settings.db_path), fresh_settings, client=fake_client
    ).status("demo")

    executed = fake_client.workflows.executed[0]
    assert started["backend"] == "mistral"
    assert executed["workflow_identifier"] == "case-lifecycle-workflow"
    assert executed["deployment_name"] == "prr-pressure-cooker-prod"
    assert executed["server_url"] == "https://workflow.example.test"
    assert executed["input"]["case_id"] == "demo"
    assert executed["input"]["initial_event_id"] is None
    assert executed["input"]["case"]["case_id"] == "demo"
    assert fake_client.workflows.executions.signals[0]["name"] == "agency_event"
    assert (
        fake_client.workflows.executions.signals[0]["server_url"] == "https://workflow.example.test"
    )
    assert (
        fake_client.workflows.executions.queries[0]["server_url"] == "https://workflow.example.test"
    )
    assert signaled["workflow"]["latest_event_id"] == "evt_demo"
    assert fake_client.workflows.executions.signals[1]["name"] == "manual_resolution"
    assert "input" not in fake_client.workflows.executions.signals[1]
    assert resolved["workflow"]["status"] == "resolved"
    assert status["status"]["case_id"] == "demo"
    assert fresh_status["workflow"]["execution_id"] == started["workflow"]["execution_id"]
    assert fresh_status["status"]["case_id"] == "demo"


def test_mistral_runtime_signal_without_local_record_starts_fresh_execution(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.create_case("demo", "Demo Agency", "Demo request")
    fake_client = FakeMistral()
    runtime = MistralWorkflowRuntime(store, settings, client=fake_client)

    signaled = runtime.signal_event(
        "demo",
        CaseWorkflowSignal(event_id="evt_demo", signal_type="agency_event"),
    )

    deterministic_id = content_id(
        "wf",
        settings.deployment_name,
        "case-lifecycle-workflow",
        "demo",
    )
    assert fake_client.workflows.executed[0]["execution_id"] != deterministic_id
    assert signaled["workflow"]["execution_id"] == fake_client.workflows.executed[0][
        "execution_id"
    ]


def test_mistral_runtime_discovers_running_remote_case_before_starting(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.create_case("demo", "Demo Agency", "Demo request")
    fake_client = FakeMistral()
    fake_client.workflows.runs = FakeRuns(
        [
            FakeRunExecution(
                execution_id="wf_other",
                root_execution_id="wf_other",
                run_id="run_other",
                start_time=datetime(2026, 5, 1, tzinfo=UTC),
            ),
            FakeRunExecution(
                execution_id="wf_remote_demo",
                root_execution_id="wf_remote_demo",
                run_id="run_demo",
                start_time=datetime(2026, 5, 2, tzinfo=UTC),
            ),
        ]
    )

    def query_by_execution(**kwargs):
        fake_client.workflows.executions.queries.append(kwargs)
        case_id = "demo" if kwargs["execution_id"] == "wf_remote_demo" else "other"
        return FakeResponse(
            result={
                "case_id": case_id,
                "case_status": "WAITING_FOR_AGENCY",
                "pending_task_id": None,
                "active_deadlines": [],
                "latest_event_id": "evt_remote",
                "latest_event_summary": "remote status",
                "pressure_score": 0,
            }
        )

    fake_client.workflows.executions.query_workflow_execution = query_by_execution
    runtime = MistralWorkflowRuntime(store, settings, client=fake_client)

    signaled = runtime.signal_event(
        "demo",
        CaseWorkflowSignal(event_id="evt_demo", signal_type="agency_event"),
    )

    stored = store.get_active_workflow_execution_for_case(
        "demo",
        workflow_name="case-lifecycle-workflow",
        backend="mistral",
    )
    assert fake_client.workflows.executed == []
    assert stored is not None
    assert stored.execution_id == "wf_remote_demo"
    assert stored.run_id == "run_demo"
    assert stored.data["discovered_by"] == "running_workflow_query"
    assert fake_client.workflows.executions.signals[0]["execution_id"] == "wf_remote_demo"
    assert signaled["workflow"]["latest_event_id"] == "evt_demo"


def test_mistral_runtime_uses_remote_execution_id_from_response(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.create_case("demo", "Demo Agency", "Demo request")

    class RemoteIdWorkflows(FakeWorkflows):
        def execute_workflow(self, **kwargs) -> dict[str, Any]:
            self.executed.append(kwargs)
            return {
                "id": "wf_remote_actual",
                "root_execution_id": "wf_remote_actual",
                "run_id": "run_actual",
                "status": "RUNNING",
            }

    class RemoteIdMistral:
        def __init__(self) -> None:
            self.workflows = RemoteIdWorkflows()

    fake_client = RemoteIdMistral()
    runtime = MistralWorkflowRuntime(store, settings, client=fake_client)

    started = runtime.start_case("demo")
    signaled = runtime.signal_event(
        "demo",
        CaseWorkflowSignal(event_id="evt_demo", signal_type="agency_event"),
    )

    requested_id = fake_client.workflows.executed[0]["execution_id"]
    stored = store.get_active_workflow_execution_for_case(
        "demo",
        workflow_name="case-lifecycle-workflow",
        backend="mistral",
    )
    assert started["workflow"]["execution_id"] == "wf_remote_actual"
    assert requested_id != "wf_remote_actual"
    assert stored is not None
    assert stored.execution_id == "wf_remote_actual"
    assert stored.run_id == "run_actual"
    assert stored.data["requested_execution_id"] == requested_id
    assert fake_client.workflows.executions.signals[0]["execution_id"] == "wf_remote_actual"
    assert signaled["workflow"]["execution_id"] == "wf_remote_actual"


def test_mistral_runtime_status_tolerates_initial_empty_query(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.create_case("demo", "Demo Agency", "Demo request")
    fake_client = FakeMistral()
    runtime = MistralWorkflowRuntime(store, settings, client=fake_client)
    runtime.start_case("demo")

    def empty_query(**kwargs):
        fake_client.workflows.executions.queries.append(kwargs)
        return FakeResponse(result={})

    fake_client.workflows.executions.query_workflow_execution = empty_query

    status = runtime.status("demo")

    assert status["status"]["case_id"] == "demo"
    assert status["status"]["case_status"] == "STARTING"


def test_mistral_runtime_restarts_stale_execution_before_signal(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.create_case("demo", "Demo Agency", "Demo request")
    fake_client = FakeMistral()
    runtime = MistralWorkflowRuntime(store, settings, client=fake_client)
    started = runtime.start_case("demo")
    original_signal = fake_client.workflows.executions.signal_workflow_execution
    signal_calls = 0

    def stale_then_accept(**kwargs):
        nonlocal signal_calls
        signal_calls += 1
        fake_client.workflows.executions.signals.append(kwargs)
        if signal_calls == 1:
            raise RuntimeError("Status 409: Workflow not running status CANCELED")
        return FakeResponse(message="Signal accepted after restart")

    fake_client.workflows.executions.signal_workflow_execution = stale_then_accept

    signaled = runtime.signal_event(
        "demo",
        CaseWorkflowSignal(event_id="evt_demo", signal_type="agency_event"),
    )

    workflow = store.get_workflow_execution_for_case("demo")
    assert workflow is not None
    assert len(fake_client.workflows.executed) == 2
    assert signal_calls == 2
    assert fake_client.workflows.executed[1]["execution_id"] != started["workflow"]["execution_id"]
    assert fake_client.workflows.executions.signals[1]["execution_id"] == workflow.execution_id
    assert workflow.latest_event_id == "evt_demo"
    assert signaled["restarted_stale_workflow"] is True
    fake_client.workflows.executions.signal_workflow_execution = original_signal


def test_ingest_push_mistral_backend_sends_event_payload(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("PRR_DB_PATH", str(tmp_path / "prr.db"))
    monkeypatch.setenv("PRR_CASEFILES_DIR", str(tmp_path / "casefiles"))
    settings = Settings.from_env()
    store = Store(settings.db_path)
    store.create_case("demo", "Demo Agency", "Demo request")
    captured_signals = []

    class CapturingRuntime:
        def signal_event(self, case_id, signal):
            captured_signals.append(signal)
            return {
                "backend": "mistral",
                "case_id": case_id,
                "signal": signal.model_dump(mode="json"),
            }

    monkeypatch.setattr(
        cli, "workflow_runtime", lambda _store, _settings, _backend: CapturingRuntime()
    )

    parser = cli.build_parser()
    args = parser.parse_args(
        ["ingest-push", "demo", "tests/fixtures/defective_estimate.txt", "--backend", "mistral"]
    )
    args.func(args)
    output = json.loads(capsys.readouterr().out)

    signal = captured_signals[0]
    assert output["signaled"][0]["backend"] == "mistral"
    assert signal.event_payload is not None
    assert signal.event_payload.case is not None
    assert signal.event_payload.case.case_id == "demo"
    assert signal.event_payload.event.case_id == "demo"
    assert signal.event_payload.evidence[0].content_b64


def test_workflow_signal_mistral_backend_auto_pushes_event_payload(
    tmp_path: Path, monkeypatch, capsys
):
    monkeypatch.setenv("PRR_DB_PATH", str(tmp_path / "prr.db"))
    monkeypatch.setenv("PRR_CASEFILES_DIR", str(tmp_path / "casefiles"))
    settings = Settings.from_env()
    store = Store(settings.db_path)
    store.create_case("demo", "Demo Agency", "Demo request")
    event = import_path("demo", Path("tests/fixtures/defective_estimate.txt"), store, settings)[0]
    captured_signals = []

    class CapturingRuntime:
        def signal_event(self, case_id, signal):
            captured_signals.append(signal)
            return {
                "backend": "mistral",
                "case_id": case_id,
                "signal": signal.model_dump(mode="json"),
            }

    monkeypatch.setattr(
        cli, "workflow_runtime", lambda _store, _settings, _backend: CapturingRuntime()
    )

    parser = cli.build_parser()
    args = parser.parse_args(
        ["workflow", "signal-event", "demo", "--event", event.event_id, "--backend", "mistral"]
    )
    args.func(args)
    output = json.loads(capsys.readouterr().out)

    signal = captured_signals[0]
    assert output["backend"] == "mistral"
    assert signal.event_payload is not None
    assert signal.event_payload.case is not None
    assert signal.event_payload.case.case_id == "demo"
    assert signal.event_payload.event.event_id == event.event_id
    assert signal.event_payload.evidence[0].content_b64


def test_deadline_scan_mistral_backend_signals_deadline_payload(
    tmp_path: Path, monkeypatch, capsys
):
    monkeypatch.setenv("PRR_DB_PATH", str(tmp_path / "prr.db"))
    monkeypatch.setenv("PRR_CASEFILES_DIR", str(tmp_path / "casefiles"))
    settings = Settings.from_env()
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
    route_event("demo", event.event_id, store, settings)
    captured_signals = []

    class CapturingRuntime:
        def signal_event(self, case_id, signal):
            captured_signals.append(signal)
            return {
                "backend": "mistral",
                "case_id": case_id,
                "signal": signal.model_dump(mode="json"),
            }

    monkeypatch.setattr(
        cli, "workflow_runtime", lambda _store, _settings, _backend: CapturingRuntime()
    )

    parser = cli.build_parser()
    args = parser.parse_args(["deadline", "scan", "--emit-events", "--backend", "mistral"])
    args.func(args)
    output = json.loads(capsys.readouterr().out)

    signal = captured_signals[0]
    assert output["backend"] == "mistral"
    assert output["due"] == 1
    assert signal.signal_type == "deadline_elapsed"
    assert signal.event_payload is not None
    assert signal.event_payload.case is not None
    assert signal.event_payload.case.case_id == "demo"
    assert signal.event_payload.event.event_type == "deadline_elapsed"
    assert Store(settings.db_path).list_deadlines(case_id="demo", status="emitted")


def test_persist_pushed_event_payload_writes_raw_copy_and_event(tmp_path: Path):
    source_settings = _settings(tmp_path / "source")
    source_store = Store(source_settings.db_path)
    source_store.create_case("demo", "Demo Agency", "Demo request")
    event = import_path(
        "demo", Path("tests/fixtures/defective_estimate.txt"), source_store, source_settings
    )[0]
    from prr_pressure_cooker.service import build_pushed_event_payload

    payload = build_pushed_event_payload("demo", event.event_id, source_store)

    target_settings = _settings(tmp_path / "target")
    target_store = Store(target_settings.db_path)
    target_store.create_case("demo", "Demo Agency", "Demo request")
    persisted = persist_pushed_event_payload(payload, target_store, target_settings)
    refs = target_store.get_evidence_refs(persisted.evidence_refs)

    assert target_store.get_case("demo").agency == "Demo Agency"
    assert target_store.get_event("demo", event.event_id).summary == event.summary
    assert len(refs) == 1
    assert refs[0].stored_file.exists()
    assert refs[0].stored_file.read_text(encoding="utf-8").startswith("Revised cost estimate")
