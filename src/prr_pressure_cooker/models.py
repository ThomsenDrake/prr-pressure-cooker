from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CaseStatus(StrEnum):
    INTAKE = "INTAKE"
    WAITING_FOR_AGENCY = "WAITING_FOR_AGENCY"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    READY_TO_SEND = "READY_TO_SEND"
    RESOLVED = "RESOLVED"


class Pathway(StrEnum):
    SILENCE_DELAY = "silence_delay"
    FEE_OPACITY = "fee_opacity"
    DEFECTIVE_ESTIMATE = "defective_estimate"
    CUSTODIAN_DODGE = "custodian_dodge"
    CLOSURE_THREAT = "closure_threat"
    EXEMPTION_VAGUENESS = "exemption_vagueness"
    DUPLICATE_INFLATION = "duplicate_inflation"
    PUBLIC_PRESSURE = "public_pressure"
    COUNSEL_OR_MEDIATION = "counsel_or_mediation"
    NO_ACTION = "no_action"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REVISED = "revised"
    DEFERRED = "deferred"
    CANCELED = "canceled"


class ReviewNoteDisposition(StrEnum):
    NO_NOTE = "no_note"
    RECORD_ONLY = "record_only"
    APPLY_TONE_EDIT = "apply_tone_edit"
    APPLY_SPECIFIC_EDIT = "apply_specific_edit"
    NEEDS_MANUAL_REVISION = "needs_manual_revision"


class WorkflowExecutionStatus(StrEnum):
    STARTED = "started"
    ACTIVE = "active"
    SUCCEEDED = "succeeded"
    RESOLVED = "resolved"
    FAILED = "failed"
    CANCELED = "canceled"
    SUPERSEDED = "superseded"


class CaseCommandRunStatus(StrEnum):
    STARTED = "started"
    ACTIVE = "active"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class DeadlineStatus(StrEnum):
    OPEN = "open"
    EMITTED = "emitted"
    CANCELED = "canceled"


class EventSource(StrEnum):
    IMPORT = "import"
    HIMALAYA = "himalaya"
    PORTAL = "portal"
    MANUAL = "manual"
    CALENDAR_TICK = "calendar_tick"


class CaseRecord(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    case_id: str
    agency: str
    request_title: str
    status: CaseStatus = CaseStatus.INTAKE
    created_at: datetime
    updated_at: datetime
    data: dict[str, Any] = Field(default_factory=dict)


class CaseExternalRefRecord(BaseModel):
    normalized_ref: str
    case_id: str
    ref_type: str
    ref_value: str
    source: str
    created_at: datetime
    updated_at: datetime


class CaseStateRecord(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    case_id: str
    status: CaseStatus
    pending_task_id: str | None = None
    latest_event_id: str | None = None
    latest_event_summary: str | None = None
    pressure_score: int = 0
    active_deadline_count: int = 0
    updated_at: datetime
    data: dict[str, Any] = Field(default_factory=dict)


class EvidenceRef(BaseModel):
    evidence_id: str
    case_id: str
    event_id: str | None = None
    original_path: str
    stored_path: str
    sha256: str
    mime_type: str
    size_bytes: int
    created_at: datetime

    @property
    def stored_file(self) -> Path:
        return Path(self.stored_path)


class MessageIndexRecord(BaseModel):
    message_index_id: str
    case_id: str
    event_id: str
    evidence_id: str
    thread_id: str
    message_id: str | None = None
    in_reply_to: str | None = None
    references: list[str] = Field(default_factory=list)
    subject: str
    sender_name: str | None = None
    sender_address: str | None = None
    recipients: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    received_at: datetime
    snippet: str = ""
    attachment_evidence_ids: list[str] = Field(default_factory=list)
    created_at: datetime


class ThreadIndexRecord(BaseModel):
    thread_id: str
    case_id: str
    subject: str
    message_count: int
    first_event_id: str
    latest_event_id: str
    first_received_at: datetime
    latest_received_at: datetime
    participants: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class AttachmentIndexRecord(BaseModel):
    attachment_index_id: str
    case_id: str
    event_id: str
    parent_evidence_id: str
    evidence_id: str
    filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    stored_path: str
    created_at: datetime


class ContactIndexRecord(BaseModel):
    contact_index_id: str
    case_id: str
    event_id: str
    role: str
    name: str | None = None
    address: str
    created_at: datetime


class FeeLine(BaseModel):
    description: str
    hours: float | None = None
    rate: float | None = None
    amount: float | None = None


class FeeEstimateAudit(BaseModel):
    contains_fee_estimate: bool = False
    deposit_required: bool = False
    task_breakdown_missing: bool = False
    math_defect: bool = False
    total_does_not_reconcile: bool = False
    alternatives_appear_cumulative: bool = False
    payment_clock_detected: bool = False
    detected_total: float | None = None
    computed_total: float | None = None
    lines: list[FeeLine] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class EventClassification(BaseModel):
    event_type: str
    issue_tags: list[str] = Field(default_factory=list)
    contains_fee_estimate: bool = False
    contains_closure_warning: bool = False
    refers_requester_to_other_agency: bool = False
    public_interest_signal: bool = False
    summary: str = ""


class CaseEvent(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    event_id: str
    case_id: str
    source: EventSource = EventSource.IMPORT
    event_type: str = "manual_note"
    received_at: datetime
    summary: str = ""
    content_text: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    issue_tags: list[str] = Field(default_factory=list)
    classification: EventClassification | None = None


class EscalationDecision(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    decision_id: str
    case_id: str
    source_event_id: str
    pathway: Pathway
    issue_tags: list[str]
    pressure_score: int
    current_state: str
    recommended_next_state: str
    draft_type: str
    human_approval_required: bool = True
    due_at: datetime | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.MEDIUM
    rationale: str
    created_at: datetime


class DeadlineRecord(BaseModel):
    deadline_id: str
    case_id: str
    source_event_id: str | None = None
    kind: str
    due_at: datetime
    status: DeadlineStatus = DeadlineStatus.OPEN
    created_at: datetime


class WorkflowExecutionRecord(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    execution_id: str
    case_id: str
    workflow_name: str
    backend: str = "local"
    status: WorkflowExecutionStatus = WorkflowExecutionStatus.STARTED
    latest_event_id: str | None = None
    root_execution_id: str | None = None
    run_id: str | None = None
    remote_status: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class CaseCommandRunRecord(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    command_id: str
    case_id: str
    command_type: str
    idempotency_key: str
    workflow_execution_id: str | None = None
    status: CaseCommandRunStatus = CaseCommandRunStatus.STARTED
    input_data: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class RouteAuditRecord(BaseModel):
    audit_id: str
    case_id: str
    event_id: str
    decision_id: str | None
    pathway: str
    status: str
    created_at: datetime
    data: dict[str, Any] = Field(default_factory=dict)


class PacketArtifactRecord(BaseModel):
    artifact_id: str
    case_id: str
    decision_id: str
    pathway: Pathway
    artifact_type: str
    file_path: str
    created_at: datetime


class ApprovalInteractionRecord(BaseModel):
    interaction_id: str
    case_id: str
    task_id: str
    decision_id: str
    choice: ReviewStatus
    note: str | None = None
    created_at: datetime


class ReviewNoteJudgment(BaseModel):
    judgment_id: str
    case_id: str
    task_id: str
    decision_id: str
    choice: ReviewStatus
    disposition: ReviewNoteDisposition
    summary: str
    applied_changes: list[str] = Field(default_factory=list)
    final_draft_file: str | None = None
    judgment_file: str | None = None
    created_at: datetime


class HumanApprovalTask(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    task_id: str
    case_id: str
    decision_id: str
    pathway: Pathway
    proposed_action: str
    status: ReviewStatus = ReviewStatus.PENDING
    draft_file: str
    evidence_packet: list[str] = Field(default_factory=list)
    choices: list[str] = Field(default_factory=lambda: ["approve", "revise", "defer", "cancel"])
    required_human_note: bool = True
    human_note: str | None = None
    created_at: datetime
    updated_at: datetime


class KanbanCard(BaseModel):
    card_id: str
    case_id: str
    task_id: str
    title: str
    lane: str
    priority: str
    body: str
    due_at: datetime | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class RouteEventInput(BaseModel):
    case_id: str
    event_id: str


class RerouteCaseInput(BaseModel):
    case_id: str
    replace_tasks: bool = False


class RouteEventResult(BaseModel):
    case_id: str
    event_id: str
    decision_id: str | None
    task_id: str | None
    pathway: str
    status: str


class RouteEvaluationPayload(BaseModel):
    case: CaseRecord
    event: CaseEvent
    classification: EventClassification | None = None
    audit: FeeEstimateAudit | None = None
    decision: EscalationDecision | None = None
    draft_file: str | None = None
    packet_paths: list[str] = Field(default_factory=list)
    task: HumanApprovalTask | None = None
    result: RouteEventResult | None = None


class CaseWorkflowInput(BaseModel):
    case_id: str
    initial_event_id: str | None = None
    case: CaseRecord | None = None
    execution_id: str | None = None
    root_execution_id: str | None = None
    run_id: str | None = None
    backend: str | None = None
    remote_status: str | None = None


class PushedEvidence(BaseModel):
    evidence_id: str
    original_path: str
    stored_name: str
    sha256: str
    mime_type: str
    size_bytes: int
    content_b64: str


class PushedEventPayload(BaseModel):
    event: CaseEvent
    case: CaseRecord | None = None
    evidence: list[PushedEvidence] = Field(default_factory=list)


class CaseWorkflowSignal(BaseModel):
    event_id: str
    signal_type: str = "agency_event"
    event_payload: PushedEventPayload | None = None


class CaseEventStepInput(BaseModel):
    case_id: str
    signal: CaseWorkflowSignal
    execution_id: str | None = None
    root_execution_id: str | None = None
    run_id: str | None = None
    backend: str | None = None
    remote_status: str | None = None


class CaseWorkflowStatus(BaseModel):
    case_id: str
    case_status: str
    pending_task_id: str | None = None
    active_deadlines: list[DeadlineRecord] = Field(default_factory=list)
    latest_event_id: str | None = None
    latest_event_summary: str | None = None
    pressure_score: int = 0


class ApprovalInput(BaseModel):
    choice: ReviewStatus
    note: str | None = None


class ApprovalRecordInput(BaseModel):
    task_id: str
    choice: ReviewStatus
    note: str | None = None


class ReviewAssistantInput(BaseModel):
    case_id: str | None = None
    task_id: str | None = None


class ReviewQueueItem(BaseModel):
    task_id: str
    case_id: str
    agency: str
    request_title: str
    request_summary: str | None = None
    pathway: Pathway
    proposed_action: str
    pressure_score: int = 0
    latest_event_summary: str | None = None
    action_reason: str | None = None
    action_excerpt: str | None = None
    due_at: datetime | None = None
    created_at: datetime


class ReviewQueuePrompt(BaseModel):
    message: str
    items: list[ReviewQueueItem] = Field(default_factory=list)
    total_count: int = 0


class ReviewTaskPrompt(BaseModel):
    message: str
    case: CaseRecord | None = None
    task: HumanApprovalTask | None = None
    decision: EscalationDecision | None = None
    event: CaseEvent | None = None
    active_deadlines: list[DeadlineRecord] = Field(default_factory=list)
    case_context: str | None = None
    packet_context: str | None = None
    draft_preview: str | None = None
    evidence_packet: list[str] = Field(default_factory=list)
