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
    status: str = "open"
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


class RouteEventResult(BaseModel):
    case_id: str
    event_id: str
    decision_id: str | None
    task_id: str | None
    pathway: str
    status: str
