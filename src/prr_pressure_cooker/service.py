from __future__ import annotations

from prr_pressure_cooker.adapters import LocalKanbanAdapter
from prr_pressure_cooker.config import Settings
from prr_pressure_cooker.drafts import build_packet_skeleton, write_draft
from prr_pressure_cooker.ids import content_id, utc_now
from prr_pressure_cooker.models import (
    CaseEvent,
    CaseRecord,
    EscalationDecision,
    EventClassification,
    FeeEstimateAudit,
    HumanApprovalTask,
    Pathway,
    ReviewStatus,
    RouteEventResult,
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


def route_event(case_id: str, event_id: str, store: Store, settings: Settings) -> RouteEventResult:
    case = store.get_case(case_id)
    event = store.get_event(case_id, event_id)
    evaluated = evaluate_event(case, event)
    event = evaluated["event"]
    decision = evaluated["decision"]
    store.save_event(event)
    store.save_decision(decision)

    if decision.pathway == Pathway.NO_ACTION or not decision.human_approval_required:
        return RouteEventResult(
            case_id=case_id,
            event_id=event_id,
            decision_id=decision.decision_id,
            task_id=None,
            pathway=decision.pathway,
            status="updated_no_action_required",
        )

    task = persist_human_review_task(case, event, decision, store, settings)

    return RouteEventResult(
        case_id=case_id,
        event_id=event_id,
        decision_id=decision.decision_id,
        task_id=task.task_id,
        pathway=decision.pathway,
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
        "event": event,
        "classification": classification,
        "audit": audit,
        "decision": decision,
    }


def reroute_case(
    case_id: str, store: Store, settings: Settings, replace_tasks: bool = False
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
        canceled_tasks = store.cancel_pending_tasks_for_case(
            case_id, "Canceled by chronological reroute repair."
        )
        deleted_cards = store.delete_cards_for_case(case_id)
        for item in evaluated:
            store.save_event(item["event"])
            store.save_decision(item["decision"])
        for item in active_items:
            task = persist_human_review_task(
                case,
                item["event"],
                item["decision"],
                store,
                settings,
            )
            created_tasks.append(task)

    pending_after = len(store.list_tasks(status=ReviewStatus.PENDING.value, case_id=case_id))
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


def is_taskworthy(decision: EscalationDecision) -> bool:
    return bool(decision.human_approval_required and decision.pathway != Pathway.NO_ACTION)


def persist_human_review_task(
    case: CaseRecord,
    event: CaseEvent,
    decision: EscalationDecision,
    store: Store,
    settings: Settings,
) -> HumanApprovalTask:
    draft_path = write_draft(case, event, decision, settings)
    packet_paths = build_packet_skeleton(case, event, decision, settings)
    task = create_human_review_task(decision, event, str(draft_path.resolve()), packet_paths)
    store.save_task(task)
    LocalKanbanAdapter(store).upsert_card(decision, task)
    return task


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
