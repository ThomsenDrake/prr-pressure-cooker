from __future__ import annotations

import base64
import re
from datetime import datetime, timedelta
from email import policy
from email.parser import BytesParser
from pathlib import Path

from prr_pressure_cooker.adapters import LocalKanbanAdapter
from prr_pressure_cooker.config import Settings
from prr_pressure_cooker.drafts import build_packet_bundle, write_draft
from prr_pressure_cooker.ids import content_id, utc_now
from prr_pressure_cooker.ingest import (
    export_casefile_indexes,
    html_to_text,
    index_event_evidence,
    record_case_external_refs,
)
from prr_pressure_cooker.models import (
    ApprovalInteractionRecord,
    ApprovalRecordInput,
    CaseEvent,
    CaseRecord,
    CaseStateRecord,
    CaseStatus,
    CaseWorkflowStatus,
    DeadlineRecord,
    DeadlineStatus,
    EscalationDecision,
    EventClassification,
    EventSource,
    EvidenceRef,
    FeeEstimateAudit,
    HumanApprovalTask,
    PacketArtifactRecord,
    Pathway,
    PushedEventPayload,
    PushedEvidence,
    ReviewAssistantInput,
    ReviewNoteDisposition,
    ReviewNoteJudgment,
    ReviewQueueItem,
    ReviewQueuePrompt,
    ReviewStatus,
    ReviewTaskPrompt,
    RouteAuditRecord,
    RouteEvaluationPayload,
    RouteEventResult,
    WorkflowExecutionRecord,
    WorkflowExecutionStatus,
)
from prr_pressure_cooker.rules import (
    audit_fee_estimate,
    classify_event,
    compute_decision,
    is_case_resolved_event,
    is_fee_resolved_event,
)
from prr_pressure_cooker.storage import Store

RESOLVABLE_PATHWAYS = {
    Pathway.CLOSURE_THREAT,
    Pathway.DEFECTIVE_ESTIMATE,
    Pathway.FEE_OPACITY,
}
CASE_WORKFLOW_NAME = "case-lifecycle-workflow"


def route_event(case_id: str, event_id: str, store: Store, settings: Settings) -> RouteEventResult:
    payload = load_case_event(case_id, event_id, store)
    payload = classify_route_payload(payload)
    payload = audit_fee_payload(payload)
    payload = compute_decision_payload(payload)
    payload = persist_event_payload(payload, store)
    if is_route_payload_taskworthy(payload):
        payload = build_packet_payload(payload, store, settings)
        payload = create_review_task_payload(payload, store)
    result = route_result_for_payload(payload)
    save_route_audit(result, store)
    persist_case_state(case_id, store)
    return result


def load_case_event(case_id: str, event_id: str, store: Store) -> RouteEvaluationPayload:
    return RouteEvaluationPayload(
        case=store.get_case(case_id),
        event=store.get_event(case_id, event_id),
    )


def classify_route_payload(payload: RouteEvaluationPayload) -> RouteEvaluationPayload:
    classification = classify_event(payload.event, payload.case)
    event = payload.event.model_copy(deep=True)
    event.event_type = classification.event_type
    event.issue_tags = classification.issue_tags
    event.classification = classification
    payload.event = event
    payload.classification = classification
    return payload


def audit_fee_payload(payload: RouteEvaluationPayload) -> RouteEvaluationPayload:
    if payload.classification is None:
        raise ValueError("classification is required before fee audit")
    payload.audit = (
        audit_fee_estimate(payload.event) if payload.classification.contains_fee_estimate else None
    )
    return payload


def compute_decision_payload(payload: RouteEvaluationPayload) -> RouteEvaluationPayload:
    if payload.classification is None:
        raise ValueError("classification is required before decision computation")
    payload.decision = compute_decision(
        payload.case, payload.event, payload.classification, payload.audit
    )
    return payload


def persist_event_payload(payload: RouteEvaluationPayload, store: Store) -> RouteEvaluationPayload:
    if payload.classification is None or payload.decision is None:
        raise ValueError("classification and decision are required before persistence")
    store.save_event(payload.event)
    store.save_decision(payload.decision)
    save_deadlines_for_event(
        payload.case, payload.event, payload.classification, payload.decision, store
    )
    return payload


def is_route_payload_taskworthy(payload: RouteEvaluationPayload) -> bool:
    if payload.decision is None:
        raise ValueError("decision is required before taskworthy check")
    return is_taskworthy(payload.decision)


def build_packet_payload(
    payload: RouteEvaluationPayload, store: Store, settings: Settings
) -> RouteEvaluationPayload:
    if payload.decision is None:
        raise ValueError("decision is required before packet build")
    draft_path = write_draft(
        payload.case,
        payload.event,
        payload.decision,
        settings,
        case_events=store.list_events(payload.case.case_id),
        case_history_text=_draft_case_history_text(payload.case, store),
    )
    case_index_paths = export_casefile_indexes(payload.case.case_id, store, settings)
    packet_paths = build_packet_bundle(
        payload.case,
        payload.event,
        payload.decision,
        settings,
        case_index_paths=case_index_paths,
    )
    packet_paths.extend(case_index_paths)
    save_packet_artifacts(payload.case, payload.decision, packet_paths, store)
    payload.draft_file = str(draft_path.resolve())
    payload.packet_paths = packet_paths
    return payload


def create_review_task_payload(
    payload: RouteEvaluationPayload, store: Store
) -> RouteEvaluationPayload:
    if payload.decision is None or payload.draft_file is None:
        raise ValueError("decision and draft file are required before review task creation")
    task = create_human_review_task(
        payload.decision,
        payload.event,
        payload.draft_file,
        payload.packet_paths,
    )
    store.save_task(task)
    LocalKanbanAdapter(store).upsert_card(payload.decision, task)
    case = store.get_case(task.case_id)
    if case.status != CaseStatus.HUMAN_REVIEW:
        case.status = CaseStatus.HUMAN_REVIEW
        case.updated_at = utc_now()
        store.upsert_case(case)
    payload.task = task
    return payload


def route_result_for_payload(payload: RouteEvaluationPayload) -> RouteEventResult:
    if payload.decision is None:
        raise ValueError("decision is required before route result creation")
    if payload.task is None:
        return RouteEventResult(
            case_id=payload.case.case_id,
            event_id=payload.event.event_id,
            decision_id=payload.decision.decision_id,
            task_id=None,
            pathway=payload.decision.pathway,
            status="updated_no_action_required",
        )
    return RouteEventResult(
        case_id=payload.case.case_id,
        event_id=payload.event.event_id,
        decision_id=payload.decision.decision_id,
        task_id=payload.task.task_id,
        pathway=payload.decision.pathway,
        status="waiting_for_human_review",
    )


def evaluate_event(
    case: CaseRecord, event: CaseEvent
) -> dict[
    str,
    CaseEvent | EventClassification | FeeEstimateAudit | EscalationDecision | None,
]:
    classification = classify_event(event, case)
    event.event_type = classification.event_type
    event.issue_tags = classification.issue_tags
    event.classification = classification
    audit = audit_fee_estimate(event) if classification.contains_fee_estimate else None
    decision = compute_decision(case, event, classification, audit)
    return {
        "case": case,
        "event": event,
        "classification": classification,
        "audit": audit,
        "decision": decision,
    }


def reroute_case(
    case_id: str,
    store: Store,
    settings: Settings,
    replace_tasks: bool = False,
    cancel_note: str = "Canceled by chronological reroute repair.",
) -> dict:
    case = store.get_case(case_id)
    pending_before = len(store.list_tasks(status=ReviewStatus.PENDING.value, case_id=case_id))
    events = store.list_events(case_id)
    evaluated = [evaluate_event(case, event) for event in events]
    active_items = active_routing_items(evaluated)

    canceled_tasks = 0
    deleted_cards = 0
    created_tasks: list[HumanApprovalTask] = []
    if replace_tasks:
        canceled_tasks = store.cancel_pending_tasks_for_case(case_id, cancel_note)
        deleted_cards = store.delete_cards_for_case(case_id)
        for item in evaluated:
            store.save_event(item["event"])
            store.save_decision(item["decision"])
        _cancel_inactive_review_deadlines(evaluated, active_items, store)
        if _case_has_resolution_event(evaluated):
            case.status = CaseStatus.RESOLVED
            case.updated_at = utc_now()
            store.upsert_case(case)
            store.cancel_open_deadlines_for_case(case_id)
        elif active_items:
            for item in active_items:
                task = persist_human_review_task(
                    case,
                    item["event"],
                    item["decision"],
                    store,
                    settings,
                )
                created_tasks.append(task)
        elif events and case.status != CaseStatus.RESOLVED:
            case.status = CaseStatus.WAITING_FOR_AGENCY
            case.updated_at = utc_now()
            store.upsert_case(case)

    pending_after = len(store.list_tasks(status=ReviewStatus.PENDING.value, case_id=case_id))
    persist_case_state(case_id, store)
    return {
        "case_id": case_id,
        "replace_tasks": replace_tasks,
        "events": len(events),
        "pending_before": pending_before,
        "pending_after": pending_after,
        "canceled_tasks": canceled_tasks,
        "deleted_cards": deleted_cards,
        "created_tasks": len(created_tasks),
        "active_decisions": [
            {
                "event_id": item["event"].event_id,
                "received_at": item["event"].received_at.isoformat(),
                "pathway": item["decision"].pathway,
                "draft_type": item["decision"].draft_type,
            }
            for item in active_items
        ],
        "suppressed_decisions": [
            {
                "event_id": item["event"].event_id,
                "received_at": item["event"].received_at.isoformat(),
                "pathway": item["decision"].pathway,
                "reason": suppression_reason(index, evaluated),
            }
            for index, item in enumerate(evaluated)
            if is_taskworthy(item["decision"]) and suppression_reason(index, evaluated)
        ],
    }


def reconcile_case_state(
    case_id: str,
    store: Store,
    settings: Settings,
    *,
    execution_id: str | None = None,
    backend: str | None = None,
    root_execution_id: str | None = None,
    run_id: str | None = None,
    remote_status: str | None = None,
) -> dict:
    result = reroute_case(
        case_id,
        store,
        settings,
        replace_tasks=True,
        cancel_note="Canceled by lifecycle reconciliation.",
    )
    status = get_case_status(case_id, store)
    workflow_record = None
    if status.case_status == CaseStatus.RESOLVED:
        workflow_record = mark_case_workflow_resolved(
            case_id,
            store,
            execution_id=execution_id,
            backend=backend,
            root_execution_id=root_execution_id,
            run_id=run_id,
            remote_status=remote_status,
            data={"resolved_by": "lifecycle_reconciliation"},
        )
    return {
        **result,
        "status": status.model_dump(mode="json"),
        "workflow": workflow_record.model_dump(mode="json") if workflow_record else None,
    }


def reroute_batch(
    case_prefix: str, store: Store, settings: Settings, replace_tasks: bool = False
) -> dict:
    case_results = [
        reroute_case(case.case_id, store, settings, replace_tasks=replace_tasks)
        for case in store.list_cases(prefix=case_prefix)
    ]
    return {
        "case_prefix": case_prefix,
        "replace_tasks": replace_tasks,
        "cases": len(case_results),
        "pending_before": sum(result["pending_before"] for result in case_results),
        "pending_after": sum(result["pending_after"] for result in case_results),
        "canceled_tasks": sum(result["canceled_tasks"] for result in case_results),
        "created_tasks": sum(result["created_tasks"] for result in case_results),
        "case_results": case_results,
    }


def active_routing_items(evaluated: list[dict]) -> list[dict]:
    unsuppressed = [
        item
        for index, item in enumerate(evaluated)
        if is_taskworthy(item["decision"]) and not suppression_reason(index, evaluated)
    ]
    closure_items = [
        item for item in unsuppressed if item["decision"].pathway == Pathway.CLOSURE_THREAT
    ]
    if closure_items:
        return [closure_items[-1]]

    latest_by_pathway: dict[str, dict] = {}
    for item in unsuppressed:
        latest_by_pathway[str(item["decision"].pathway)] = item
    return sorted(
        latest_by_pathway.values(),
        key=lambda item: (item["event"].received_at, item["event"].event_id),
    )


def suppression_reason(index: int, evaluated: list[dict]) -> str | None:
    item = evaluated[index]
    decision = item["decision"]
    later_items = evaluated[index + 1 :]
    if any(is_case_resolved_event(later["classification"]) for later in later_items):
        return "later_case_resolution"
    if decision.pathway in RESOLVABLE_PATHWAYS and any(
        is_fee_resolved_event(later["event"], later["classification"]) for later in later_items
    ):
        return "later_payment_or_authorization"
    return None


def _case_has_resolution_event(evaluated: list[dict]) -> bool:
    return any(is_case_resolved_event(item["classification"]) for item in evaluated)


def _cancel_inactive_review_deadlines(
    evaluated: list[dict], active_items: list[dict], store: Store
) -> None:
    active_event_ids = {item["event"].event_id for item in active_items}
    inactive_kinds_by_event_id: dict[str, set[str]] = {}
    for item in evaluated:
        event = item["event"]
        decision = item["decision"]
        if not is_taskworthy(decision) or event.event_id in active_event_ids:
            continue
        inactive_kinds_by_event_id[event.event_id] = {
            deadline.kind
            for deadline in build_deadlines_for_event(
                item["case"],
                event,
                item["classification"],
                decision,
            )
            if deadline.kind.endswith("_review_due") or deadline.kind == "closure_window"
        }

    for deadline in store.list_deadlines(
        case_id=evaluated[0]["case"].case_id if evaluated else None,
        status=DeadlineStatus.OPEN.value,
    ):
        if deadline.source_event_id is None:
            continue
        inactive_kinds = inactive_kinds_by_event_id.get(deadline.source_event_id, set())
        if deadline.kind in inactive_kinds:
            store.set_deadline_status(deadline.deadline_id, DeadlineStatus.CANCELED.value)


def is_taskworthy(decision: EscalationDecision) -> bool:
    return bool(decision.human_approval_required and decision.pathway != Pathway.NO_ACTION)


def persist_human_review_task(
    case: CaseRecord,
    event: CaseEvent,
    decision: EscalationDecision,
    store: Store,
    settings: Settings,
) -> HumanApprovalTask:
    payload = RouteEvaluationPayload(case=case, event=event, decision=decision)
    payload = build_packet_payload(payload, store, settings)
    payload = create_review_task_payload(payload, store)
    if payload.task is None:
        raise ValueError("review task creation did not produce a task")
    return payload.task


def create_human_review_task(
    decision, event: CaseEvent, draft_file: str, evidence_packet: list[str]
) -> HumanApprovalTask:
    now = utc_now()
    return HumanApprovalTask(
        task_id=content_id("task", decision.decision_id, event.event_id, draft_file),
        case_id=decision.case_id,
        decision_id=decision.decision_id,
        pathway=decision.pathway,
        proposed_action=decision.draft_type,
        status=ReviewStatus.PENDING,
        draft_file=draft_file,
        evidence_packet=evidence_packet,
        required_human_note=True,
        created_at=now,
        updated_at=now,
    )


def start_case_workflow(
    case_id: str,
    store: Store,
    settings: Settings,
    *,
    execution_id: str | None = None,
    backend: str = "local",
    root_execution_id: str | None = None,
    run_id: str | None = None,
    remote_status: str | None = None,
    data: dict | None = None,
) -> WorkflowExecutionRecord:
    store.get_case(case_id)
    now = utc_now()
    existing = store.get_active_workflow_execution_for_case(
        case_id,
        workflow_name=CASE_WORKFLOW_NAME,
        backend=backend,
    )
    if existing:
        record = existing.model_copy(
            update={
                "execution_id": execution_id or existing.execution_id,
                "status": WorkflowExecutionStatus.ACTIVE,
                "root_execution_id": root_execution_id or existing.root_execution_id,
                "run_id": run_id or existing.run_id,
                "remote_status": remote_status or existing.remote_status,
                "data": {**existing.data, **(data or {})},
                "updated_at": now,
            }
        )
    else:
        record = WorkflowExecutionRecord(
            execution_id=execution_id
            or content_id("wf", settings.deployment_name, CASE_WORKFLOW_NAME, case_id),
            case_id=case_id,
            workflow_name=CASE_WORKFLOW_NAME,
            backend=backend,
            status=WorkflowExecutionStatus.ACTIVE,
            latest_event_id=None,
            root_execution_id=root_execution_id,
            run_id=run_id,
            remote_status=remote_status,
            data=data or {},
            created_at=now,
            updated_at=now,
        )
    store.save_workflow_execution(record)
    return record


def mark_case_workflow_resolved(
    case_id: str,
    store: Store,
    *,
    execution_id: str | None = None,
    backend: str | None = None,
    root_execution_id: str | None = None,
    run_id: str | None = None,
    remote_status: str | None = None,
    data: dict | None = None,
) -> WorkflowExecutionRecord | None:
    records = store.list_workflow_executions(
        case_id,
        workflow_name=CASE_WORKFLOW_NAME,
        backend=backend,
    )
    record = None
    if execution_id:
        record = next((item for item in records if item.execution_id == execution_id), None)
    if record is None:
        record = next(
            (item for item in records if item.status == WorkflowExecutionStatus.ACTIVE),
            records[0] if records else None,
        )
    if record is None:
        if execution_id is None:
            return None
        now = utc_now()
        record = WorkflowExecutionRecord(
            execution_id=execution_id,
            case_id=case_id,
            workflow_name=CASE_WORKFLOW_NAME,
            backend=backend or "local",
            status=WorkflowExecutionStatus.RESOLVED,
            root_execution_id=root_execution_id,
            run_id=run_id,
            remote_status=remote_status,
            data=data or {},
            created_at=now,
            updated_at=now,
        )
        store.save_workflow_execution(record)
        return record

    updated = record.model_copy(
        update={
            "status": WorkflowExecutionStatus.RESOLVED,
            "root_execution_id": root_execution_id or record.root_execution_id,
            "run_id": run_id or record.run_id,
            "remote_status": remote_status or record.remote_status,
            "data": {**record.data, **(data or {})},
            "updated_at": utc_now(),
        }
    )
    store.save_workflow_execution(updated)
    return updated


def signal_case_event(case_id: str, event_id: str, store: Store, settings: Settings) -> dict:
    workflow_record = start_case_workflow(case_id, store, settings)
    result = route_event(case_id, event_id, store, settings)
    now = utc_now()
    updated_record = workflow_record.model_copy(
        update={
            "status": WorkflowExecutionStatus.ACTIVE,
            "latest_event_id": event_id,
            "updated_at": now,
        }
    )
    store.save_workflow_execution(updated_record)
    return {
        "workflow": updated_record.model_dump(mode="json"),
        "result": result.model_dump(mode="json"),
    }


def resolve_case_workflow(
    case_id: str,
    store: Store,
    settings: Settings,
    note: str | None = None,
    *,
    execution_id: str | None = None,
    backend: str | None = None,
    root_execution_id: str | None = None,
    run_id: str | None = None,
    remote_status: str | None = None,
) -> WorkflowExecutionRecord:
    workflow_record = start_case_workflow(
        case_id,
        store,
        settings,
        execution_id=execution_id,
        backend=backend or "local",
        root_execution_id=root_execution_id,
        run_id=run_id,
        remote_status=remote_status,
    )
    now = utc_now()
    case = store.get_case(case_id)
    case.status = CaseStatus.RESOLVED
    case.updated_at = now
    if note:
        case.data["resolution_note"] = note
    store.upsert_case(case)
    store.cancel_pending_tasks_for_case(case_id, note or "Case manually resolved.")
    store.cancel_open_deadlines_for_case(case_id)
    updated_record = workflow_record.model_copy(
        update={
            "status": WorkflowExecutionStatus.RESOLVED,
            "updated_at": now,
        }
    )
    store.save_workflow_execution(updated_record)
    persist_case_state(case_id, store)
    return updated_record


def build_pushed_event_payload(case_id: str, event_id: str, store: Store) -> PushedEventPayload:
    case = store.get_case(case_id)
    event = store.get_event(case_id, event_id)
    evidence_refs = store.get_evidence_refs(event.evidence_refs)
    evidence = []
    for ref in evidence_refs:
        stored_file = ref.stored_file
        evidence.append(
            PushedEvidence(
                evidence_id=ref.evidence_id,
                original_path=ref.original_path,
                stored_name=stored_file.name,
                sha256=ref.sha256,
                mime_type=ref.mime_type,
                size_bytes=ref.size_bytes,
                content_b64=base64.b64encode(stored_file.read_bytes()).decode("ascii"),
            )
        )
    return PushedEventPayload(event=event, case=case, evidence=evidence)


def persist_pushed_event_payload(
    payload: PushedEventPayload, store: Store, settings: Settings
) -> CaseEvent:
    now = utc_now()
    event = payload.event
    if payload.case is not None:
        store.upsert_case(payload.case)
    pushed_dir = settings.casefiles_dir / event.case_id / "raw" / "pushed" / now.strftime("%Y%m%d")
    pushed_dir.mkdir(parents=True, exist_ok=True)

    for pushed in payload.evidence:
        stored_path = pushed_dir / pushed.stored_name
        if not stored_path.exists():
            stored_path.write_bytes(base64.b64decode(pushed.content_b64.encode("ascii")))
        ref = EvidenceRef(
            evidence_id=pushed.evidence_id,
            case_id=event.case_id,
            event_id=event.event_id,
            original_path=pushed.original_path,
            stored_path=str(stored_path.resolve()),
            sha256=pushed.sha256,
            mime_type=pushed.mime_type,
            size_bytes=pushed.size_bytes,
            created_at=now,
        )
        store.save_evidence_ref(ref)

    store.save_event(event)
    index_event_evidence(event, store, settings)
    record_case_external_refs(
        event.case_id,
        store,
        "pushed-event",
        event.summary,
        *(
            message.subject
            for message in store.list_message_indexes(event.case_id)
            if message.event_id == event.event_id
        ),
    )
    return event


def get_case_status(case_id: str, store: Store) -> CaseWorkflowStatus:
    return persist_case_state(case_id, store)


def persist_case_state(case_id: str, store: Store) -> CaseWorkflowStatus:
    case = store.get_case(case_id)
    pending_tasks = store.list_tasks(status=ReviewStatus.PENDING.value, case_id=case_id)
    latest_event = store.latest_event(case_id)
    latest_decision = store.latest_decision(case_id)
    active_deadlines = store.list_deadlines(case_id=case_id, status=DeadlineStatus.OPEN.value)
    status = CaseWorkflowStatus(
        case_id=case_id,
        case_status=str(case.status),
        pending_task_id=pending_tasks[0].task_id if pending_tasks else None,
        active_deadlines=active_deadlines,
        latest_event_id=latest_event.event_id if latest_event else None,
        latest_event_summary=latest_event.summary if latest_event else None,
        pressure_score=latest_decision.pressure_score if latest_decision else 0,
    )
    store.save_case_state(
        CaseStateRecord(
            case_id=case_id,
            status=case.status,
            pending_task_id=status.pending_task_id,
            latest_event_id=status.latest_event_id,
            latest_event_summary=status.latest_event_summary,
            pressure_score=status.pressure_score,
            active_deadline_count=len(active_deadlines),
            updated_at=utc_now(),
            data={
                "active_deadline_ids": [deadline.deadline_id for deadline in active_deadlines],
            },
        )
    )
    return status


def scan_deadlines(store: Store, settings: Settings, emit_events: bool = False) -> dict:
    now = utc_now()
    due = store.due_deadlines(now)
    emitted = []
    for deadline in due:
        payload = {"deadline": deadline.model_dump(mode="json"), "event": None, "route": None}
        if emit_events:
            event = deadline_elapsed_event(deadline, now)
            store.save_event(event)
            route_result = route_event(deadline.case_id, event.event_id, store, settings)
            store.set_deadline_status(deadline.deadline_id, DeadlineStatus.EMITTED.value)
            payload["event"] = event.model_dump(mode="json")
            payload["route"] = route_result.model_dump(mode="json")
        emitted.append(payload)
    return {"due": len(due), "emit_events": emit_events, "items": emitted}


def record_approval_interaction(
    task: HumanApprovalTask, choice: ReviewStatus, note: str | None, store: Store
) -> ApprovalInteractionRecord:
    now = utc_now()
    interaction = ApprovalInteractionRecord(
        interaction_id=content_id(
            "approval", task.task_id, choice.value, note or "", now.isoformat()
        ),
        case_id=task.case_id,
        task_id=task.task_id,
        decision_id=task.decision_id,
        choice=choice,
        note=note,
        created_at=now,
    )
    store.save_approval_interaction(interaction)
    return interaction


def apply_review_choice(
    task: HumanApprovalTask, choice: ReviewStatus, note: str | None, store: Store
) -> tuple[HumanApprovalTask, ApprovalInteractionRecord, ReviewNoteJudgment | None]:
    note = _normalize_review_note(note)
    task.status = choice
    task.human_note = note
    task.updated_at = utc_now()
    store.save_task(task)
    interaction = record_approval_interaction(task, choice, note, store)
    judgment = judge_review_note(task, choice, note, store) if note else None
    if judgment and judgment.final_draft_file:
        task.draft_file = judgment.final_draft_file
        task.updated_at = utc_now()
        store.save_task(task)
    if choice in {ReviewStatus.APPROVED, ReviewStatus.REVISED}:
        case = store.get_case(task.case_id)
        case.status = CaseStatus.READY_TO_SEND
        case.updated_at = utc_now()
        store.upsert_case(case)
    persist_case_state(task.case_id, store)
    return task, interaction, judgment


def apply_review_record(
    input_data: ApprovalRecordInput, store: Store
) -> tuple[HumanApprovalTask, ApprovalInteractionRecord, ReviewNoteJudgment | None]:
    task = store.get_task(input_data.task_id)
    return apply_review_choice(task, input_data.choice, input_data.note, store)


def apply_review_record_if_pending(
    input_data: ApprovalRecordInput, store: Store
) -> tuple[
    HumanApprovalTask | None,
    ApprovalInteractionRecord | None,
    ReviewNoteJudgment | None,
    bool,
    str | None,
]:
    try:
        task = store.get_task(input_data.task_id)
    except KeyError:
        return None, None, None, False, "task_not_found"
    if task.status != ReviewStatus.PENDING:
        return task, None, None, False, "task_not_pending"
    task, interaction, judgment = apply_review_choice(
        task, input_data.choice, input_data.note, store
    )
    return task, interaction, judgment, True, None


def review_final_artifact(
    task: HumanApprovalTask | None,
    judgment: ReviewNoteJudgment | None,
) -> dict | None:
    if task is None or task.status not in {ReviewStatus.APPROVED, ReviewStatus.REVISED}:
        return None
    source_file = (
        judgment.final_draft_file if judgment and judgment.final_draft_file else task.draft_file
    )
    content = _read_text_preview(Path(source_file), max_chars=20000)
    proposed_message = _extract_proposed_message(content)
    if not proposed_message:
        return None
    return {
        "format": "markdown",
        "filename": f"{Path(source_file).stem}_final.md",
        "source_file": source_file,
        "content": proposed_message,
    }


def _normalize_review_note(note: str | None) -> str | None:
    if note is None:
        return None
    normalized = note.strip()
    return normalized or None


def judge_review_note(
    task: HumanApprovalTask,
    choice: ReviewStatus,
    note: str,
    store: Store,
) -> ReviewNoteJudgment:
    decision = store.get_decision(task.decision_id)
    disposition = _review_note_disposition(choice, note)
    draft_path = Path(task.draft_file)
    original_draft = _read_text_preview(draft_path, max_chars=20000)
    summary, final_draft = _incorporate_review_note(
        original_draft,
        choice,
        note,
        disposition,
    )
    now = utc_now()
    base_dir = draft_path.parent
    slug = content_id("review", task.task_id, choice.value, note, now.isoformat())
    final_draft_path: Path | None = None
    applied_changes: list[str] = []
    if final_draft is not None:
        final_draft_path = base_dir / f"{draft_path.stem}_{slug}_reviewed.md"
        final_draft_path.write_text(final_draft, encoding="utf-8")
        applied_changes.append(summary)
        store.save_packet_artifact(
            PacketArtifactRecord(
                artifact_id=content_id("artifact", task.task_id, "reviewed_draft", slug),
                case_id=task.case_id,
                decision_id=task.decision_id,
                pathway=task.pathway,
                artifact_type="reviewed_draft",
                file_path=str(final_draft_path.resolve()),
                created_at=now,
            )
        )

    judgment = ReviewNoteJudgment(
        judgment_id=content_id("judgment", task.task_id, choice.value, note, now.isoformat()),
        case_id=task.case_id,
        task_id=task.task_id,
        decision_id=task.decision_id,
        choice=choice,
        disposition=disposition,
        summary=summary,
        applied_changes=applied_changes,
        final_draft_file=str(final_draft_path.resolve()) if final_draft_path else None,
        created_at=now,
    )
    judgment_path = base_dir / f"{draft_path.stem}_{slug}_review_judgment.md"
    judgment = judgment.model_copy(update={"judgment_file": str(judgment_path.resolve())})
    judgment_path.write_text(
        _review_judgment_markdown(judgment, note, decision.rationale),
        encoding="utf-8",
    )
    store.save_packet_artifact(
        PacketArtifactRecord(
            artifact_id=content_id("artifact", task.task_id, "review_note_judgment", slug),
            case_id=task.case_id,
            decision_id=task.decision_id,
            pathway=task.pathway,
            artifact_type="review_note_judgment",
            file_path=str(judgment_path.resolve()),
            created_at=now,
        )
    )
    return judgment


def _review_note_disposition(choice: ReviewStatus, note: str) -> ReviewNoteDisposition:
    text = note.lower()
    if choice in {ReviewStatus.DEFERRED, ReviewStatus.CANCELED}:
        return ReviewNoteDisposition.RECORD_ONLY
    tone_terms = (
        "firm",
        "firmer",
        "strong",
        "stronger",
        "assertive",
        "direct",
        "more forceful",
    )
    if any(term in text for term in tone_terms):
        return ReviewNoteDisposition.APPLY_TONE_EDIT
    edit_terms = (
        "add ",
        "include ",
        "remove ",
        "delete ",
        "change ",
        "rewrite ",
        "revise ",
        "mention ",
        "say ",
    )
    if choice == ReviewStatus.REVISED or any(term in text for term in edit_terms):
        return ReviewNoteDisposition.APPLY_SPECIFIC_EDIT
    return ReviewNoteDisposition.RECORD_ONLY


def _incorporate_review_note(
    original_draft: str,
    choice: ReviewStatus,
    note: str,
    disposition: ReviewNoteDisposition,
) -> tuple[str, str | None]:
    if choice in {ReviewStatus.DEFERRED, ReviewStatus.CANCELED}:
        return (
            "Recorded the note for the reviewer; no ready-to-send draft was produced.",
            None,
        )

    if disposition == ReviewNoteDisposition.APPLY_TONE_EDIT:
        message = _extract_proposed_message(original_draft)
        revised_message = _make_message_firmer(message)
        return (
            "Applied the note as a tone edit and produced a firmer reviewed draft.",
            _replace_proposed_message(
                original_draft,
                revised_message,
                note,
                "Tone edit",
            ),
        )

    if disposition == ReviewNoteDisposition.APPLY_SPECIFIC_EDIT:
        message = _extract_proposed_message(original_draft)
        revised_message = "\n\n".join(
            [
                message,
                f"Reviewer instruction to incorporate before manual send: {note}",
            ]
        )
        return (
            "Carried the reviewer instruction into the reviewed draft for manual incorporation.",
            _replace_proposed_message(
                original_draft,
                revised_message,
                note,
                "Specific edit instruction",
            ),
        )

    return (
        "Recorded the note with the approved draft; no text change was required.",
        _append_review_judge_section(
            original_draft,
            note,
            "Record-only approval note",
            "No draft wording change was required.",
        ),
    )


def _extract_proposed_message(draft: str) -> str:
    marker = "## Proposed Message"
    if marker not in draft:
        return draft.strip()
    return draft.split(marker, 1)[1].strip()


def _replace_proposed_message(draft: str, message: str, note: str, basis: str) -> str:
    marker = "## Proposed Message"
    if marker not in draft:
        body = draft.rstrip()
    else:
        body = f"{draft.split(marker, 1)[0].rstrip()}\n\n{marker}\n\n{message.strip()}"
    return _append_review_judge_section(
        body,
        note,
        basis,
        "The proposed message section was updated for the reviewed draft.",
    )


def _append_review_judge_section(draft: str, note: str, basis: str, summary: str) -> str:
    return "\n".join(
        [
            draft.rstrip(),
            "",
            "## Review Note Judge",
            "",
            f"- Basis: {basis}",
            f"- Summary: {summary}",
            f"- Reviewer note: {note}",
            "",
        ]
    )


def _make_message_firmer(message: str) -> str:
    message = message.strip()
    firm_sentence = (
        "Please provide a corrected, itemized estimate before applying any payment "
        "deadline, and confirm that the request will remain open while this estimate "
        "defect is resolved."
    )
    if "corrected, itemized estimate" in message:
        return message
    return f"{message}\n\n{firm_sentence}"


def _review_judgment_markdown(
    judgment: ReviewNoteJudgment, note: str, decision_rationale: str
) -> str:
    lines = [
        "# Review Note Judgment",
        "",
        f"- Choice: `{judgment.choice}`",
        f"- Disposition: `{judgment.disposition}`",
        f"- Summary: {judgment.summary}",
        f"- Final draft: `{judgment.final_draft_file or 'not produced'}`",
        f"- Created: `{judgment.created_at.isoformat()}`",
        "",
        "## Reviewer Note",
        "",
        note,
        "",
        "## Decision Rationale",
        "",
        decision_rationale,
        "",
    ]
    if judgment.applied_changes:
        lines.extend(["## Applied Changes", ""])
        lines.extend(f"- {change}" for change in judgment.applied_changes)
        lines.append("")
    return "\n".join(lines)


def review_task_prompt(
    input_data: ReviewAssistantInput, store: Store, settings: Settings
) -> ReviewTaskPrompt:
    task = _select_review_task(input_data, store)
    if task is None:
        scope = f" for case `{input_data.case_id}`" if input_data.case_id else ""
        return ReviewTaskPrompt(message=f"No pending PRR review tasks found{scope}.")

    case = store.get_case(task.case_id)
    decision = store.get_decision(task.decision_id)
    event = store.get_event(task.case_id, decision.source_event_id)
    active_deadlines = store.list_deadlines(case_id=task.case_id, status=DeadlineStatus.OPEN.value)
    draft_path = write_draft(
        case,
        event,
        decision,
        settings,
        case_events=store.list_events(case.case_id),
        case_history_text=_draft_case_history_text(case, store),
    )
    return ReviewTaskPrompt(
        message="Pending PRR review task is ready.",
        case=case,
        task=task,
        decision=decision,
        event=event,
        active_deadlines=active_deadlines,
        case_context=_build_review_case_context(
            case,
            event,
            decision,
            store,
            requester_emails=settings.requester_emails,
        ),
        packet_context=_build_review_packet_context(task.evidence_packet),
        draft_preview=_read_text_preview(draft_path),
        evidence_packet=task.evidence_packet,
    )


def review_task_queue(
    store: Store,
    limit: int = 10,
) -> ReviewQueuePrompt:
    tasks = store.list_tasks(status=ReviewStatus.PENDING.value)
    if not tasks:
        return ReviewQueuePrompt(message="No pending PRR review tasks found.")

    items: list[ReviewQueueItem] = []
    for task in tasks:
        case = store.get_case(task.case_id)
        if _is_non_user_review_case(case):
            continue
        decision = store.get_decision(task.decision_id)
        event = store.get_event(task.case_id, decision.source_event_id)
        active_deadlines = store.list_deadlines(
            case_id=task.case_id, status=DeadlineStatus.OPEN.value
        )
        next_deadline = min(
            active_deadlines,
            key=lambda deadline: deadline.due_at,
            default=None,
        )
        items.append(
            ReviewQueueItem(
                task_id=task.task_id,
                case_id=task.case_id,
                agency=case.agency,
                request_title=case.request_title,
                request_summary=_review_request_summary(case, store),
                pathway=task.pathway,
                proposed_action=task.proposed_action,
                pressure_score=decision.pressure_score,
                latest_event_summary=event.summary,
                action_reason=decision.rationale,
                action_excerpt=_review_excerpt(event.content_text, 220),
                due_at=next_deadline.due_at if next_deadline else None,
                created_at=task.created_at,
            )
        )

    if not items:
        return ReviewQueuePrompt(message="No pending PRR review tasks found.")

    items.sort(
        key=lambda item: (
            item.due_at is None,
            item.due_at or item.created_at,
            -item.pressure_score,
            item.created_at,
        )
    )
    visible_items = items[:limit]
    overflow = len(items) - len(visible_items)
    message = f"{len(items)} pending PRR review task{'s' if len(items) != 1 else ''}."
    if overflow > 0:
        message += f" Showing the next {len(visible_items)}."
    return ReviewQueuePrompt(message=message, items=visible_items, total_count=len(items))


def _draft_case_history_text(
    case: CaseRecord,
    store: Store,
    max_chars: int = 40000,
) -> str:
    chunks = [event.content_text for event in store.list_events(case.case_id) if event.content_text]
    messages = store.list_message_indexes(case.case_id)
    refs = {
        ref.evidence_id: ref
        for ref in store.get_evidence_refs(message.evidence_id for message in messages)
    }
    for message in messages:
        ref = refs.get(message.evidence_id)
        if ref is None:
            continue
        raw_text = _read_eml_text(ref.stored_file)
        if raw_text:
            chunks.append(raw_text)
    return "\n\n".join(chunks)[:max_chars]


def _select_review_task(input_data: ReviewAssistantInput, store: Store) -> HumanApprovalTask | None:
    if input_data.task_id:
        task = store.get_task(input_data.task_id)
        if input_data.case_id and task.case_id != input_data.case_id:
            raise ValueError(
                f"task {input_data.task_id} belongs to case {task.case_id}, "
                f"not {input_data.case_id}"
            )
        return task if task.status == ReviewStatus.PENDING else None

    tasks = store.list_tasks(
        status=ReviewStatus.PENDING.value,
        case_id=input_data.case_id,
    )
    return tasks[0] if tasks else None


def _read_text_preview(path: Path, max_chars: int = 4000) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"Could not read draft `{path}`: {exc}"
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars].rstrip()}\n\n[truncated]"


def _is_non_user_review_case(case: CaseRecord) -> bool:
    case_id = case.case_id.lower()
    agency = case.agency.lower()
    title = case.request_title.lower()
    return (
        case_id.startswith(("smoke", "test-", "live-"))
        or "codex smoke" in agency
        or "smoke" in title
        or "live casefile index smoke" in title
    )


def _build_review_case_context(
    case: CaseRecord,
    event: CaseEvent,
    decision: EscalationDecision,
    store: Store,
    requester_emails: tuple[str, ...] = (),
    max_event_chars: int = 900,
    max_message_chars: int = 900,
) -> str:
    event_text = _review_excerpt(event.content_text, max_event_chars)
    issue_tags = ", ".join(decision.issue_tags) or "none"
    evidence_refs = ", ".join(decision.evidence_refs) or "none"
    messages = sorted(
        store.list_message_indexes(case.case_id),
        key=lambda message: (message.received_at, message.event_id),
    )
    attachments = store.list_attachment_indexes(case.case_id)
    contacts = store.list_contact_indexes(case.case_id)
    recent_messages = messages[-5:]
    timeline_lines: list[str] = []
    for message in recent_messages:
        relation = _review_message_relation(message, event, requester_emails)
        excerpt = (
            "See What happened above."
            if message.event_id == event.event_id
            else _review_excerpt(message.snippet, max_message_chars)
        )
        timeline_lines.extend(
            [
                (
                    f"- {message.received_at.isoformat()} | {relation} | "
                    f"{_review_sender(message.sender_name, message.sender_address)}"
                ),
                f"  Subject: {_review_excerpt(message.subject, 140)}",
                f"  Excerpt: {excerpt}",
                f"  source `{message.evidence_id}`",
            ]
        )
    if not timeline_lines:
        timeline_lines = ["- No indexed messages yet."]
    attachment_lines = [
        (
            f"- {attachment.filename} (`{attachment.evidence_id}`, "
            f"{attachment.mime_type}, {attachment.size_bytes} bytes)"
        )
        for attachment in attachments[-8:]
    ] or ["- No indexed attachments."]
    contact_lines = [
        f"- {contact.role}: {contact.name or contact.address} <{contact.address}>"
        if contact.name
        else f"- {contact.role}: {contact.address}"
        for contact in contacts[-10:]
    ] or ["- No indexed contacts."]
    return "\n".join(
        [
            "Decision brief:",
            f"- Why review is needed: {decision.rationale}",
            f"- Current state: {decision.current_state}",
            f"- Recommended next state: {decision.recommended_next_state}",
            f"- Pathway: {decision.pathway}; pressure score: {decision.pressure_score}",
            f"- Issue tags: {issue_tags}",
            f"- Source evidence refs: {evidence_refs}",
            "",
            "Relevant case history:",
            *timeline_lines,
            "",
            "Indexed attachments:",
            *attachment_lines,
            "",
            "Indexed contacts:",
            *contact_lines,
            "",
            "Triggering agency event:",
            f"- Received: {event.received_at.isoformat()}",
            f"- Type: {event.event_type}",
            f"- Summary: {event.summary}",
            "",
            "Agency text excerpt:",
            event_text,
        ]
    )


def _review_message_relation(
    message,
    event: CaseEvent,
    requester_emails: tuple[str, ...],
) -> str:
    if message.event_id == event.event_id:
        return "Triggering agency message"
    if message.received_at > event.received_at:
        if _is_requester_message(message, requester_emails):
            return "Later requester reply"
        return "Later case message"
    if _is_requester_message(message, requester_emails):
        return "Earlier requester message"
    return "Earlier agency message"


def _is_requester_message(message, requester_emails: tuple[str, ...]) -> bool:
    sender = (message.sender_address or "").lower()
    return bool(sender and sender in {email.lower() for email in requester_emails})


def _review_request_summary(case: CaseRecord, store: Store, max_chars: int = 360) -> str:
    explicit = _explicit_request_summary(case)
    if explicit:
        return _review_excerpt(_natural_language_request_summary(explicit, case), max_chars)

    candidates: list[tuple[int, str]] = []
    messages = store.list_message_indexes(case.case_id)
    refs = {
        ref.evidence_id: ref
        for ref in store.get_evidence_refs(message.evidence_id for message in messages)
    }
    for message in messages:
        ref = refs.get(message.evidence_id)
        if ref is not None:
            full_text = _read_eml_text(ref.stored_file)
            if full_text:
                candidates.extend(_request_summary_candidates(full_text))
        candidates.extend(_request_summary_candidates(message.snippet))
        candidates.extend(_request_summary_candidates(message.subject))

    candidates.extend(_request_summary_candidates(case.request_title))
    if not candidates:
        return _review_excerpt(
            _natural_language_request_summary(case.request_title, case), max_chars
        )

    candidates.sort(key=lambda item: (-item[0], len(item[1])))
    return _review_excerpt(_natural_language_request_summary(candidates[0][1], case), max_chars)


def _explicit_request_summary(case: CaseRecord) -> str | None:
    for key in (
        "requested_records",
        "records_requested",
        "request_summary",
        "request_description",
        "request_text",
        "scope",
    ):
        value = case.data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _read_eml_text(path: Path) -> str:
    try:
        message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    except OSError:
        return ""
    body = message.get_body(preferencelist=("plain", "html"))
    if body is None:
        return ""
    try:
        content = body.get_content()
    except Exception:
        return ""
    if body.get_content_type() == "text/html":
        return html_to_text(content)
    return str(content)


def _request_summary_candidates(text: str) -> list[tuple[int, str]]:
    if not text:
        return []
    patterns = [
        (
            100,
            "MY ORIGINAL REQUEST SOUGHT:",
            (
                "DEMAND FOR IMMEDIATE ACTION:",
                "IF THIS MATTER IS NOT RESOLVED",
            ),
        ),
        (
            90,
            "Per your public record request",
            (
                "ISS processing time",
                "Orange County estimates",
                "Payment can be made",
            ),
        ),
        (
            85,
            "Based on your response, I understand the estimate as follows:",
            (
                "Please correct me",
                "At this point",
            ),
        ),
        (
            80,
            "Your estimated total is:",
            (
                "Please respond",
                "If the actual cost",
            ),
        ),
        (
            70,
            "I am still seeking only",
            (
                "Please also confirm",
                "Thank you",
            ),
        ),
        (
            60,
            "Subject: Public Records Request",
            (
                "LEGAL REQUIREMENTS",
                "MY ORIGINAL REQUEST SOUGHT",
            ),
        ),
    ]
    candidates: list[tuple[int, str]] = []
    lowered = text.lower()
    for score, marker, end_markers in patterns:
        index = lowered.find(marker.lower())
        if index < 0:
            continue
        section = text[index:]
        end_index = len(section)
        lowered_section = section.lower()
        for end_marker in end_markers:
            marker_index = lowered_section.find(end_marker.lower(), len(marker))
            if marker_index >= 0:
                end_index = min(end_index, marker_index)
        summary = _clean_request_summary(section[:end_index])
        if summary:
            candidates.append((score, summary))
    return candidates


def _clean_request_summary(text: str) -> str:
    cleaned = text
    for prefix in (
        "MY ORIGINAL REQUEST SOUGHT:",
        "Based on your response, I understand the estimate as follows:",
        "Subject:",
    ):
        if cleaned.lower().startswith(prefix.lower()):
            cleaned = cleaned[len(prefix) :]
            break
    cleaned = cleaned.replace("\u00a0", " ")
    cleaned = " ".join(line.strip(" -\t") for line in cleaned.splitlines() if line.strip())
    cleaned = re.sub(r"(?i)your estimated total is:\s*\$?[0-9.,]+", "", cleaned)
    cleaned = re.sub(
        r"(?i)\b[0-9]+\s+hours?(?:\s+[0-9]+\s+minutes?)?\s+at\s+\$?[0-9.]+"
        r"\s+per\s+hour\s+for\s+",
        "",
        cleaned,
    )
    cleaned = re.sub(
        r"(?i)\b[0-9]+\s+minutes?\s+at\s+\$?[0-9.]+\s+per\s+hour\s+for\s+",
        "",
        cleaned,
    )
    cleaned = re.sub(r"(?i)\s+for an estimated cost of \$?[0-9.]+", "", cleaned)
    cleaned = cleaned.replace(" staff time Records", "; Records")
    cleaned = cleaned.replace(" staff time", "")
    return " ".join(cleaned.split()).strip(" :-")


def _natural_language_request_summary(text: str, case: CaseRecord | None = None) -> str:
    cleaned = _clean_request_summary(text)
    if not cleaned:
        return ""

    for summarizer in (
        _naturalize_scout_microtransit_request,
        _naturalize_orange_county_search_request,
        _naturalize_orlando_portal_request,
        _naturalize_corrections_request,
    ):
        summary = summarizer(cleaned, case)
        if summary:
            return summary

    lowered = cleaned.lower()
    if lowered.startswith("i am still seeking only "):
        cleaned = cleaned[len("I am still seeking only ") :]
        if cleaned and cleaned[0].islower():
            cleaned = cleaned[0].upper() + cleaned[1:]

    return cleaned


def _naturalize_scout_microtransit_request(text: str, case: CaseRecord | None = None) -> str | None:
    if "scout microtransit" not in f"{text} {case.request_title if case else ''}".lower():
        return None

    lowered = text.lower()
    items: list[str] = []
    if "rfp" in lowered or "vendor proposals" in lowered:
        items.append("the RFP and vendor proposals")
    if "befree" in lowered or "contract" in lowered:
        items.append("the BeFree contract and amendments")
    if "bcc" in lowered or "meeting" in lowered:
        items.append("BCC meeting materials")
    if "financial" in lowered or "invoices" in lowered or "budget" in lowered:
        items.append("invoices, payments, and budget records")
    if "performance" in lowered or "ridership" in lowered:
        items.append("performance data")
    if "planning" in lowered or "feasibility" in lowered:
        items.append("planning and feasibility materials")
    if "communications" in lowered:
        items.append("staff/vendor communications")

    if not items:
        return "Scout microtransit program records."
    return f"Scout microtransit program records, including {_join_natural_list(items)}."


def _naturalize_orange_county_search_request(
    text: str, case: CaseRecord | None = None
) -> str | None:
    lowered = text.lower()
    if not (
        "iss ran" in lowered
        or "timeframe:" in lowered
        and "keywords:" in lowered
        or "county wide" in lowered
        and "keyword" in lowered
    ):
        return None

    scope = "emails"
    if "county wide" in lowered or "county-wide" in lowered:
        scope = "county-wide emails"
    excludes_ocso = "no ocso" in lowered

    date_range = _extract_record_date_range(text)
    topics = _orange_county_keyword_topics(text)
    subject = scope[0].upper() + scope[1:]
    if date_range:
        subject = f"{subject} from {date_range[0]} to {date_range[1]}"
    if excludes_ocso:
        subject = f"{subject}, excluding OCSO"
    if topics:
        connector = ", about" if excludes_ocso else " about"
        return f"{subject}{connector} {_join_natural_list(topics)}."
    return f"{subject} responsive to the Orange County records search."


def _naturalize_orlando_portal_request(text: str, case: CaseRecord | None = None) -> str | None:
    parts = [part.strip(" .") for part in text.split(";") if part.strip(" .")]
    if not any("technology search" in part.lower() for part in parts):
        return None

    base_records: list[str] = []
    technology_searches: list[str] = []
    for part in parts:
        search_match = re.search(r"technology search\s*-\s*([^)]+)", part, re.IGNORECASE)
        if search_match:
            technology_searches.append(_friendly_search_medium(search_match.group(1)))
        elif part.lower() == "code enforcement":
            base_records.append("Code Enforcement records")
        else:
            base_records.append(part)

    if base_records and technology_searches:
        return (
            f"{_join_natural_list(base_records)} plus technology searches of "
            f"{_join_natural_list(technology_searches)}."
        )
    if technology_searches:
        return f"Technology searches of {_join_natural_list(technology_searches)}."
    return None


def _naturalize_corrections_request(text: str, case: CaseRecord | None = None) -> str | None:
    context = f"{text} {case.request_title if case else ''}".lower()
    if not (
        "corr-2026-300" in context
        or "osceola county corrections" in context
        or "records technician:" in context
        or "inmate services personnel:" in context
        or "finance personnel:" in context
    ):
        return None

    requested_records: list[str] = []
    if "financial/reimbursement" in context or "finance personnel" in context:
        requested_records.append("financial and reimbursement records")
    if "inmate services" in context:
        requested_records.append("inmate services records")
    if "phase 2" in context:
        requested_records.append("related Phase 2 reimbursement or correspondence records")
    if "phase 1 #3" in context and not requested_records:
        requested_records.append("Phase 1 item #3 records")
    if "phase 1 #4" in context and "inmate services records" not in requested_records:
        requested_records.append("Phase 1 item #4 records")

    if requested_records:
        return (
            "Osceola County Corrections records, including "
            f"{_join_natural_list(requested_records)}."
        )
    return "Osceola County Corrections records responsive to CORR-2026-300."


def _extract_record_date_range(text: str) -> tuple[str, str] | None:
    date_pattern = r"(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})"
    match = re.search(
        rf"(?:from|timeframe:)\s*{date_pattern}\s*(?:to|-|\u2013|\u2014)\s*{date_pattern}",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    return (_format_record_date(match.group(1)), _format_record_date(match.group(2)))


def _format_record_date(value: str) -> str:
    for date_format in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(value, date_format)
        except ValueError:
            continue
        return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"
    return value


def _orange_county_keyword_topics(text: str) -> list[str]:
    lowered = text.lower()
    topics: list[str] = []
    if "igsa contract dispute" in lowered:
        _append_unique(topics, "IGSA contract disputes involving ICE and the mayor")
    if "igsa reimbursement rates" in lowered:
        _append_unique(topics, "IGSA reimbursement rates")
    if "8660 transport" in lowered:
        _append_unique(topics, "ICE records mentioning 8660 Transport Drive")
    if "48-hour ice hold" in lowered or "48 hour ice hold" in lowered or "72-hour" in lowered:
        _append_unique(topics, "48- and 72-hour ICE hold terms")
    if "igsa to boa transition" in lowered:
        _append_unique(topics, "the IGSA-to-BOA transition")
    if "ice release" in lowered:
        _append_unique(topics, "ICE release")
    if "transfer authority" in lowered:
        _append_unique(topics, "transfer authority")
    if topics:
        return topics

    keyword_match = re.search(r"keywords?:\s*(.+)", text, re.IGNORECASE)
    if not keyword_match:
        return []
    keyword_text = re.split(
        r"(?i)\b(?:which resulted|please note|orange county estimates|payment can be made)\b",
        keyword_match.group(1),
        maxsplit=1,
    )[0]
    keyword_text = re.sub(r"=\s*\d[\d,]*", "", keyword_text)
    keyword_text = re.sub(r"[()\"“”]", " ", keyword_text)
    keyword_text = re.sub(r"\b(?:AND|OR)\b", ",", keyword_text, flags=re.IGNORECASE)
    return [
        part
        for part in _dedupe_preserving_order(
            " ".join(part.split()) for part in re.split(r"[,;\n]+", keyword_text)
        )
        if part
    ][:5]


def _friendly_search_medium(value: str) -> str:
    cleaned = " ".join(value.split()).strip(" .").lower()
    if cleaned == "teams messages":
        return "Teams messages"
    if cleaned == "text messages":
        return "text messages"
    return cleaned


def _join_natural_list(items: list[str]) -> str:
    clean_items = [item for item in _dedupe_preserving_order(items) if item]
    if not clean_items:
        return ""
    if len(clean_items) == 1:
        return clean_items[0]
    if len(clean_items) == 2:
        return f"{clean_items[0]} and {clean_items[1]}"
    return f"{', '.join(clean_items[:-1])}, and {clean_items[-1]}"


def _dedupe_preserving_order(items) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _append_unique(items: list[str], item: str) -> None:
    if item.lower() not in {existing.lower() for existing in items}:
        items.append(item)


def _review_excerpt(text: str, max_chars: int = 500) -> str:
    normalized_text = text.strip()
    lowered = normalized_text[:1000].lower()
    if (
        normalized_text.startswith("<")
        or "<html" in lowered
        or "<body" in lowered
        or "</" in lowered
    ):
        normalized_text = html_to_text(normalized_text)
    normalized = " ".join(normalized_text.split())
    if not normalized:
        return "No text captured."
    if len(normalized) <= max_chars:
        return normalized
    return f"{normalized[:max_chars].rstrip()}..."


def _review_sender(name: str | None, address: str | None) -> str:
    if name and address:
        return f"{name} <{address}>"
    return address or name or "unknown"


def _build_review_packet_context(
    evidence_packet: list[str],
    max_file_chars: int = 1200,
    max_total_chars: int = 5200,
) -> str:
    sections: list[str] = []
    total_chars = 0
    for raw_path in evidence_packet:
        path = Path(raw_path)
        if path.suffix.lower() != ".md":
            continue
        title = path.stem.replace("_", " ").replace("-", " ").title()
        preview = _strip_internal_packet_lines(_read_text_preview(path, max_file_chars)).strip()
        section = f"## {title}\n\n{preview}"
        if total_chars + len(section) > max_total_chars:
            remaining = max_total_chars - total_chars
            if remaining > 200:
                sections.append(f"{section[:remaining].rstrip()}\n\n[truncated]")
            break
        sections.append(section)
        total_chars += len(section)
    return "\n\n".join(sections) or "No readable packet summary files were available."


def _strip_internal_packet_lines(text: str) -> str:
    internal_prefixes = (
        "- Case:",
        "- Source event:",
    )
    return "\n".join(
        line for line in text.splitlines() if not line.strip().startswith(internal_prefixes)
    )


def save_deadlines_for_event(
    case: CaseRecord,
    event: CaseEvent,
    classification: EventClassification,
    decision: EscalationDecision,
    store: Store,
) -> list[DeadlineRecord]:
    deadlines = build_deadlines_for_event(case, event, classification, decision)
    for deadline in deadlines:
        store.save_deadline(deadline)
    return deadlines


def build_deadlines_for_event(
    case: CaseRecord,
    event: CaseEvent,
    classification: EventClassification,
    decision: EscalationDecision,
) -> list[DeadlineRecord]:
    deadlines: list[DeadlineRecord] = []
    if classification.event_type == "submitted_acknowledgment":
        deadlines.append(
            create_deadline(
                case.case_id,
                event.event_id,
                "silence_after_acknowledgment",
                event.received_at + timedelta(days=7),
            )
        )
    if classification.contains_closure_warning:
        deadlines.append(
            create_deadline(
                case.case_id,
                event.event_id,
                "closure_window",
                decision.due_at or event.received_at + timedelta(days=2),
            )
        )
    if event.event_type == "human_sent_message" and _mentions_formal_notice(event.content_text):
        deadlines.append(
            create_deadline(
                case.case_id,
                event.event_id,
                "section_11912_cure_window",
                event.received_at + timedelta(days=5),
            )
        )
    if decision.pathway != Pathway.NO_ACTION and decision.due_at is not None:
        deadlines.append(
            create_deadline(
                case.case_id,
                event.event_id,
                f"{decision.pathway}_review_due",
                decision.due_at,
            )
        )
    return deadlines


def create_deadline(
    case_id: str, source_event_id: str, kind: str, due_at: datetime
) -> DeadlineRecord:
    return DeadlineRecord(
        deadline_id=content_id("deadline", case_id, source_event_id, kind, due_at.isoformat()),
        case_id=case_id,
        source_event_id=source_event_id,
        kind=kind,
        due_at=due_at,
        status=DeadlineStatus.OPEN,
        created_at=utc_now(),
    )


def deadline_elapsed_event(deadline: DeadlineRecord, now: datetime) -> CaseEvent:
    return CaseEvent(
        event_id=content_id("evt", deadline.case_id, deadline.deadline_id, "deadline_elapsed"),
        case_id=deadline.case_id,
        source=EventSource.CALENDAR_TICK,
        event_type="deadline_elapsed",
        received_at=now,
        summary=f"Deadline elapsed: {deadline.kind}",
        content_text=(
            f"Deadline `{deadline.kind}` elapsed at {deadline.due_at.isoformat()} "
            f"for case `{deadline.case_id}`."
        ),
        issue_tags=[deadline.kind],
    )


def save_route_audit(result: RouteEventResult, store: Store) -> None:
    now = utc_now()
    audit = RouteAuditRecord(
        audit_id=content_id(
            "route-audit", result.case_id, result.event_id, result.status, now.isoformat()
        ),
        case_id=result.case_id,
        event_id=result.event_id,
        decision_id=result.decision_id,
        pathway=result.pathway,
        status=result.status,
        created_at=now,
        data=result.model_dump(mode="json"),
    )
    store.save_route_audit(audit)


def save_packet_artifacts(
    case: CaseRecord,
    decision: EscalationDecision,
    packet_paths: list[str],
    store: Store,
) -> None:
    now = utc_now()
    for packet_path in packet_paths:
        path = Path(packet_path)
        artifact = PacketArtifactRecord(
            artifact_id=content_id("packet", decision.decision_id, str(path)),
            case_id=case.case_id,
            decision_id=decision.decision_id,
            pathway=decision.pathway,
            artifact_type=_artifact_type(path),
            file_path=str(path),
            created_at=now,
        )
        store.save_packet_artifact(artifact)


def _artifact_type(path: Path) -> str:
    if path.parent.name == "indexes":
        return f"index:{path.stem}"
    return path.stem


def _mentions_formal_notice(text: str) -> bool:
    lowered = text.lower()
    return "119.12" in lowered or "formal notice" in lowered or "notice to cure" in lowered
