from __future__ import annotations

import os
from typing import Any, Protocol

from mistralai.client import Mistral

from prr_pressure_cooker.config import Settings
from prr_pressure_cooker.ids import content_id, utc_now
from prr_pressure_cooker.models import (
    CaseWorkflowInput,
    CaseWorkflowSignal,
    CaseWorkflowStatus,
    WorkflowExecutionRecord,
    WorkflowExecutionStatus,
)
from prr_pressure_cooker.service import CASE_WORKFLOW_NAME, get_case_status
from prr_pressure_cooker.storage import Store


class WorkflowRuntime(Protocol):
    def start_case(self, case_id: str, *, force_new: bool = False) -> dict[str, Any]: ...

    def signal_event(self, case_id: str, signal: CaseWorkflowSignal) -> dict[str, Any]: ...

    def resolve_case(self, case_id: str) -> dict[str, Any]: ...

    def status(self, case_id: str) -> dict[str, Any]: ...


class LocalWorkflowRuntime:
    def __init__(self, store: Store, settings: Settings):
        self.store = store
        self.settings = settings

    def start_case(self, case_id: str, *, force_new: bool = False) -> dict[str, Any]:
        from prr_pressure_cooker.service import start_case_workflow

        record = start_case_workflow(case_id, self.store, self.settings)
        return {"backend": "local", "workflow": record.model_dump(mode="json")}

    def signal_event(self, case_id: str, signal: CaseWorkflowSignal) -> dict[str, Any]:
        from prr_pressure_cooker.service import signal_case_event

        result = signal_case_event(case_id, signal.event_id, self.store, self.settings)
        return {"backend": "local", **result}

    def resolve_case(self, case_id: str) -> dict[str, Any]:
        from prr_pressure_cooker.service import resolve_case_workflow

        record = resolve_case_workflow(case_id, self.store, self.settings)
        status = get_case_status(case_id, self.store)
        return {
            "backend": "local",
            "workflow": record.model_dump(mode="json"),
            "status": status.model_dump(mode="json"),
        }

    def status(self, case_id: str) -> dict[str, Any]:
        status = get_case_status(case_id, self.store)
        return {"backend": "local", "status": status.model_dump(mode="json")}


class MistralWorkflowRuntime:
    def __init__(
        self,
        store: Store,
        settings: Settings,
        client: Mistral | None = None,
    ):
        self.store = store
        self.settings = settings
        api_key = os.getenv("MISTRAL_API_KEY")
        if client is None and not api_key:
            raise RuntimeError("MISTRAL_API_KEY is required for PRR_WORKFLOW_BACKEND=mistral")
        self.client = client or Mistral(
            api_key=api_key,
            server_url=self.settings.workflow_api_base_url,
        )

    def start_case(self, case_id: str, *, force_new: bool = False) -> dict[str, Any]:
        case = self.store.get_case(case_id)
        id_parts = [self.settings.deployment_name, CASE_WORKFLOW_NAME, case_id]
        if force_new:
            id_parts.append(utc_now().isoformat())
        requested_execution_id = content_id("wf", *id_parts)
        response = self.client.workflows.execute_workflow(
            workflow_identifier=CASE_WORKFLOW_NAME,
            execution_id=requested_execution_id,
            input=CaseWorkflowInput(case_id=case_id, case=case).model_dump(mode="json"),
            deployment_name=self.settings.deployment_name,
            server_url=self.settings.workflow_api_base_url,
        )
        response_data = _model_dump(response)
        execution_id = _remote_execution_id(response_data, requested_execution_id)
        now = utc_now()
        record = WorkflowExecutionRecord(
            execution_id=execution_id,
            case_id=case_id,
            workflow_name=CASE_WORKFLOW_NAME,
            backend="mistral",
            status=WorkflowExecutionStatus.ACTIVE,
            latest_event_id=None,
            root_execution_id=_optional_str(response_data.get("root_execution_id")),
            run_id=_optional_str(response_data.get("run_id")),
            remote_status=_optional_str(response_data.get("status")),
            data={"requested_execution_id": requested_execution_id},
            created_at=now,
            updated_at=now,
        )
        self.store.save_workflow_execution(record)
        return {
            "backend": "mistral",
            "workflow": record.model_dump(mode="json"),
            "remote": response_data,
        }

    def signal_event(self, case_id: str, signal: CaseWorkflowSignal) -> dict[str, Any]:
        record = self._active_case_workflow(case_id)
        if record is None:
            record = self._find_remote_case_workflow(case_id)
        if record is None:
            self.start_case(case_id, force_new=True)
            record = self._active_case_workflow(case_id)
        if record is None:
            raise RuntimeError(f"case workflow did not start for {case_id}")

        signal_name = _signal_name(signal.signal_type)
        restarted_stale_workflow = False
        try:
            response = self.client.workflows.executions.signal_workflow_execution(
                execution_id=record.execution_id,
                name=signal_name,
                input=signal.model_dump(mode="json"),
                server_url=self.settings.workflow_api_base_url,
            )
        except Exception as exc:
            if not _is_stale_workflow_signal_error(exc):
                raise
            failed = record.model_copy(
                update={
                    "status": WorkflowExecutionStatus.FAILED,
                    "updated_at": utc_now(),
                }
            )
            self.store.save_workflow_execution(failed)
            self.start_case(case_id, force_new=True)
            record = self._active_case_workflow(case_id)
            if record is None:
                raise RuntimeError(f"case workflow did not restart for {case_id}") from exc
            response = self.client.workflows.executions.signal_workflow_execution(
                execution_id=record.execution_id,
                name=signal_name,
                input=signal.model_dump(mode="json"),
                server_url=self.settings.workflow_api_base_url,
            )
            restarted_stale_workflow = True
        now = utc_now()
        updated = record.model_copy(
            update={
                "status": WorkflowExecutionStatus.ACTIVE,
                "latest_event_id": signal.event_id,
                "updated_at": now,
            }
        )
        self.store.save_workflow_execution(updated)
        return {
            "backend": "mistral",
            "workflow": updated.model_dump(mode="json"),
            "signal": {"name": signal_name, "response": _model_dump(response)},
            "restarted_stale_workflow": restarted_stale_workflow,
        }

    def resolve_case(self, case_id: str) -> dict[str, Any]:
        record = self._active_case_workflow(case_id)
        if record is None:
            record = self._find_remote_case_workflow(case_id)
        if record is None:
            self.start_case(case_id)
            record = self._active_case_workflow(case_id)
        if record is None:
            raise RuntimeError(f"case workflow did not start for {case_id}")

        response = self.client.workflows.executions.signal_workflow_execution(
            execution_id=record.execution_id,
            name="manual_resolution",
            server_url=self.settings.workflow_api_base_url,
        )
        now = utc_now()
        updated = record.model_copy(
            update={
                "status": WorkflowExecutionStatus.RESOLVED,
                "updated_at": now,
            }
        )
        self.store.save_workflow_execution(updated)
        return {
            "backend": "mistral",
            "workflow": updated.model_dump(mode="json"),
            "signal": {"name": "manual_resolution", "response": _model_dump(response)},
        }

    def status(self, case_id: str) -> dict[str, Any]:
        record = self._active_case_workflow(case_id)
        if record is None:
            record = self._find_remote_case_workflow(case_id)
        if record is None:
            local_record = self.store.get_active_workflow_execution_for_case(
                case_id,
                workflow_name=CASE_WORKFLOW_NAME,
            )
            try:
                status = get_case_status(case_id, self.store)
            except KeyError:
                local_record = self._placeholder_case_workflow(case_id)
            else:
                workflow = local_record.model_dump(mode="json") if local_record else None
                return {
                    "backend": "mistral",
                    "workflow": workflow,
                    "status": status.model_dump(mode="json"),
                    "remote": None,
                }
            record = local_record
        response = self.client.workflows.executions.query_workflow_execution(
            execution_id=record.execution_id,
            name="get_case_status",
            server_url=self.settings.workflow_api_base_url,
        )
        response_data = _model_dump(response)
        result = response_data.get("result") or {}
        status = (
            CaseWorkflowStatus.model_validate(result)
            if result
            else CaseWorkflowStatus(case_id=case_id, case_status="STARTING")
        )
        return {
            "backend": "mistral",
            "workflow": record.model_dump(mode="json"),
            "status": status.model_dump(mode="json"),
            "remote": response_data,
        }

    def _active_case_workflow(self, case_id: str) -> WorkflowExecutionRecord | None:
        return self.store.get_active_workflow_execution_for_case(
            case_id,
            workflow_name=CASE_WORKFLOW_NAME,
            backend="mistral",
        )

    def _find_remote_case_workflow(self, case_id: str) -> WorkflowExecutionRecord | None:
        try:
            runs = self.client.workflows.runs.list_runs(
                workflow_identifier=CASE_WORKFLOW_NAME,
                status="RUNNING",
                page_size=100,
                server_url=self.settings.workflow_api_base_url,
            )
        except Exception:
            return None
        result = getattr(runs, "result", runs)
        executions = getattr(result, "executions", []) or []
        for execution in executions:
            execution_id = getattr(execution, "execution_id", None)
            if not execution_id:
                continue
            try:
                response = self.client.workflows.executions.query_workflow_execution(
                    execution_id=execution_id,
                    name="get_case_status",
                    server_url=self.settings.workflow_api_base_url,
                )
            except Exception:
                continue
            response_data = _model_dump(response)
            status_data = response_data.get("result") or {}
            if status_data.get("case_id") != case_id:
                continue
            now = utc_now()
            record = WorkflowExecutionRecord(
                execution_id=execution_id,
                case_id=case_id,
                workflow_name=CASE_WORKFLOW_NAME,
                backend="mistral",
                status=WorkflowExecutionStatus.ACTIVE,
                latest_event_id=status_data.get("latest_event_id"),
                root_execution_id=_remote_attr(execution, "root_execution_id"),
                run_id=_remote_attr(execution, "run_id"),
                remote_status=_remote_attr(execution, "status"),
                data={"discovered_by": "running_workflow_query"},
                created_at=getattr(execution, "start_time", None) or now,
                updated_at=now,
            )
            self.store.save_workflow_execution(record)
            return record
        return None

    def _placeholder_case_workflow(self, case_id: str) -> WorkflowExecutionRecord:
        now = utc_now()
        return WorkflowExecutionRecord(
            execution_id=content_id(
                "wf", self.settings.deployment_name, CASE_WORKFLOW_NAME, case_id
            ),
            case_id=case_id,
            workflow_name=CASE_WORKFLOW_NAME,
            status=WorkflowExecutionStatus.ACTIVE,
            latest_event_id=None,
            created_at=now,
            updated_at=now,
        )


def workflow_runtime(
    store: Store, settings: Settings, backend: str | None = None
) -> WorkflowRuntime:
    selected = (backend or settings.workflow_backend).lower()
    if selected == "local":
        return LocalWorkflowRuntime(store, settings)
    if selected == "mistral":
        return MistralWorkflowRuntime(store, settings)
    raise ValueError(f"unsupported workflow backend: {selected}")


def _signal_name(signal_type: str) -> str:
    aliases = {
        "deadline": "deadline_elapsed",
        "agency": "agency_event",
    }
    return aliases.get(signal_type, signal_type)


def _model_dump(value) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return value
    return {"value": value}


def _remote_execution_id(response_data: dict[str, Any], fallback: str) -> str:
    for key in ("execution_id", "workflow_execution_id", "id"):
        value = response_data.get(key)
        if value:
            return str(value)
    execution = response_data.get("execution")
    if isinstance(execution, dict):
        for key in ("execution_id", "workflow_execution_id", "id"):
            value = execution.get(key)
            if value:
                return str(value)
    return fallback


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _remote_attr(value: Any, name: str) -> str | None:
    if isinstance(value, dict):
        return _optional_str(value.get(name))
    return _optional_str(getattr(value, name, None))


def _is_stale_workflow_signal_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "workflow not running" in message
        or "not running status" in message
        or "status canceled" in message
        or "status cancelled" in message
        or "execution not found" in message
        or "status 404" in message
    )
