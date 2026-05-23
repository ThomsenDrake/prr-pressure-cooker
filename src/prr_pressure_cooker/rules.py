from __future__ import annotations

import re
from datetime import timedelta

from prr_pressure_cooker.ids import content_id, utc_now
from prr_pressure_cooker.models import (
    CaseEvent,
    CaseRecord,
    EscalationDecision,
    EventClassification,
    FeeEstimateAudit,
    FeeLine,
    Pathway,
    RiskLevel,
)

MONEY_RE = re.compile(
    r"\$?\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})|[0-9]+(?:\.[0-9]{2})?)"
)
HOURS_MINUTES_RATE_RE = re.compile(
    r"(?P<hours>[0-9]+(?:\.[0-9]+)?)\s*(?:hours?|hrs?)"
    r"(?:\s+(?P<minutes>[0-9]+(?:\.[0-9]+)?)\s*(?:minutes?|mins?))?"
    r".{0,80}?\bat\s+\$?(?P<rate>[0-9]+(?:\.[0-9]{2})?)"
    r"\s*(?:per\s*(?:hour|hr)|/(?:hour|hr))?",
    re.IGNORECASE,
)
MINUTES_RATE_RE = re.compile(
    r"(?P<minutes>[0-9]+(?:\.[0-9]+)?)\s*(?:minutes?|mins?)"
    r".{0,80}?\bat\s+\$?(?P<rate>[0-9]+(?:\.[0-9]{2})?)"
    r"\s*(?:per\s*(?:hour|hr)|/(?:hour|hr))?",
    re.IGNORECASE,
)
PROCESSING_RATE_RE = re.compile(
    r"(?P<hours>[0-9]+(?:\.[0-9]+)?)\s*(?:hours?|hrs?)\s*[-–—]?\s*"
    r"\$?(?P<rate>[0-9]+(?:\.[0-9]{2})?)\s*(?:per\s*(?:hour|hr)|/(?:hour|hr))",
    re.IGNORECASE | re.DOTALL,
)
RESULTS_IN_HOURS_RATE_RE = re.compile(
    r"results?\s+in\s+(?P<hours>[0-9]+(?:\.[0-9]+)?)\s*(?:hours?|hrs?).{0,160}?"
    r"\(\$?(?P<rate>[0-9]+(?:\.[0-9]{2})?)\)",
    re.IGNORECASE,
)


def classify_event(event: CaseEvent, case: CaseRecord) -> EventClassification:
    text = event.content_text.lower()
    if event.event_type == "human_sent_message":
        return EventClassification(
            event_type=event.event_type,
            issue_tags=[],
            summary=event.summary,
        )

    if _is_submitted_acknowledgment(event.summary, text):
        return EventClassification(
            event_type="submitted_acknowledgment",
            issue_tags=[],
            summary=event.summary,
        )

    if _is_payment_confirmation(event.summary, text):
        return EventClassification(
            event_type="payment_confirmation_received",
            issue_tags=[],
            summary=event.summary,
        )

    if _is_fulfilled_closure(event.summary, text):
        return EventClassification(
            event_type="fulfilled_closure",
            issue_tags=[],
            summary=event.summary,
        )

    if _is_records_released(event.summary, text):
        return EventClassification(
            event_type="records_released",
            issue_tags=[],
            summary=event.summary,
        )

    tags: list[str] = []

    contains_fee = _contains_actual_fee_estimate(text)
    contains_closure = _contains_unresolved_closure_threat(text)
    dodge = _has_any(
        text,
        [
            "contact another agency",
            "direct your request",
            "request should be directed",
            "we do not maintain",
            "not the custodian",
        ],
    ) and _has_any(text, ["records", "request"])

    if contains_closure:
        tags.append("closure_threat")
    if contains_fee:
        tags.append("fee_estimate")
    if dodge:
        tags.append("custodian_dodge")
    if "exempt" in text and "statute" not in text and "119." not in text:
        tags.append("exemption_vagueness")
    if _has_any(text, ["duplicate", "dedup", "unique message", "unique email"]):
        tags.append("duplicate_inflation")
    if event.event_type == "deadline_elapsed":
        tags.append("silence_delay")
    if case.data.get("notice_11912_sent") and event.event_type == "deadline_elapsed":
        tags.append("post_notice_no_cure")

    event_type = _base_event_type(event.event_type)
    if contains_fee:
        event_type = "fee_estimate_received"
    if contains_closure:
        event_type = "closure_warning_received"

    return EventClassification(
        event_type=event_type,
        issue_tags=dedupe(tags),
        contains_fee_estimate=contains_fee,
        contains_closure_warning=contains_closure,
        refers_requester_to_other_agency=dodge,
        public_interest_signal=_has_any(text, ["commissioner", "public interest", "press"]),
        summary=event.summary,
    )


def audit_fee_estimate(event: CaseEvent) -> FeeEstimateAudit:
    text = event.content_text
    lowered = text.lower()
    audit = FeeEstimateAudit(
        contains_fee_estimate=_contains_actual_fee_estimate(lowered),
        deposit_required=_has_any(lowered, ["deposit", "payment is required"]),
        payment_clock_detected=_contains_unresolved_closure_threat(lowered),
        alternatives_appear_cumulative=_has_any(
            lowered, ["alternative", "option a", "option b", "cumulative", "stacked"]
        ),
    )

    if not audit.contains_fee_estimate:
        return audit

    audit.task_breakdown_missing = _task_breakdown_missing(lowered)

    lines = _extract_fee_lines(text)
    audit.lines = lines
    computed = sum(
        (line.hours or 0) * (line.rate or 0)
        for line in lines
        if line.hours and line.rate
    )
    audit.computed_total = round(computed, 2) if computed else None
    audit.detected_total = _detect_total(text)

    for line in lines:
        if line.hours is not None and line.rate is not None and line.amount is not None:
            expected = round(line.hours * line.rate, 2)
            if abs(expected - line.amount) > 0.02:
                audit.math_defect = True
                audit.notes.append(
                    f"{line.description} does not reconcile: "
                    f"{line.hours} * {line.rate} != {line.amount}"
                )

    if audit.detected_total is not None and audit.computed_total is not None and lines:
        if abs(audit.detected_total - audit.computed_total) > 0.02:
            audit.total_does_not_reconcile = True
            audit.math_defect = True
            audit.notes.append(
                f"detected total {audit.detected_total} does not match "
                f"computed total {audit.computed_total}"
            )

    if audit.task_breakdown_missing:
        audit.notes.append("estimate lacks particularized phase/task/personnel basis")
    if audit.alternatives_appear_cumulative:
        audit.notes.append("estimate appears to include alternatives or stacked options")
    return audit


def compute_decision(
    case: CaseRecord,
    event: CaseEvent,
    classification: EventClassification,
    audit: FeeEstimateAudit | None = None,
) -> EscalationDecision:
    tags = set(classification.issue_tags)
    now = utc_now()

    if classification.event_type in NO_ACTION_EVENT_TYPES:
        return _decision(
            case,
            event,
            Pathway.NO_ACTION,
            tags,
            0,
            case.status,
            "none",
            "No escalation rule matched this event.",
            RiskLevel.LOW,
            None,
            human_approval_required=False,
        )

    if classification.contains_closure_warning:
        return _decision(
            case,
            event,
            Pathway.CLOSURE_THREAT,
            tags | {"closure_threat"},
            6,
            "URGENT_CLOSURE_REVIEW",
            "no_withdrawal_preservation_reply",
            "Agency appears to attach a closure clock to a pending clarification, "
            "fee, or response issue.",
            RiskLevel.HIGH,
            now + timedelta(days=1),
        )

    if audit and (audit.math_defect or audit.alternatives_appear_cumulative):
        defect_tags = {"defective_estimate"}
        if audit.total_does_not_reconcile:
            defect_tags.add("total_does_not_reconcile")
        if audit.alternatives_appear_cumulative:
            defect_tags.add("stacked_alternatives")
        return _decision(
            case,
            event,
            Pathway.DEFECTIVE_ESTIMATE,
            tags | defect_tags,
            8,
            "SUPERVISOR_ESCALATION_READY",
            "supervisor_review_request",
            "Estimate appears defective because math, totals, or alternatives cannot "
            "be reconciled.",
            RiskLevel.MEDIUM,
            now + timedelta(days=5),
        )

    if audit and audit.contains_fee_estimate and audit.task_breakdown_missing:
        return _decision(
            case,
            event,
            Pathway.FEE_OPACITY,
            tags | {"fee_opacity", "task_basis_unanswered"},
            5,
            "PARTICULARIZED_ESTIMATE_REQUEST_READY",
            "particularized_estimate_request",
            "Estimate includes charges that cannot be evaluated from the provided "
            "task/personnel basis.",
            RiskLevel.MEDIUM,
            now + timedelta(days=5),
        )

    if classification.refers_requester_to_other_agency:
        return _decision(
            case,
            event,
            Pathway.CUSTODIAN_DODGE,
            tags | {"custodian_dodge", "answer_wrong_request"},
            5,
            "FORCED_POSITION_LETTER_READY",
            "forced_agency_position_letter",
            "Agency appears to redirect the request instead of clearly stating its "
            "position on agency-held records.",
            RiskLevel.MEDIUM,
            now + timedelta(days=5),
        )

    if "post_notice_no_cure" in tags:
        return _decision(
            case,
            event,
            Pathway.COUNSEL_OR_MEDIATION,
            tags,
            10,
            "COUNSEL_PACKET_READY",
            "attorney_faf_packet",
            "Formal notice window appears elapsed without a complete cure.",
            RiskLevel.HIGH,
            now + timedelta(days=2),
        )

    if "silence_delay" in tags:
        return _decision(
            case,
            event,
            Pathway.SILENCE_DELAY,
            tags,
            2,
            "STATUS_NUDGE_READY",
            "status_nudge",
            "No substantive agency status was recorded by the configured deadline.",
            RiskLevel.LOW,
            now + timedelta(days=3),
        )

    return _decision(
        case,
        event,
        Pathway.NO_ACTION,
        tags,
        0,
        case.status,
        "none",
        "No escalation rule matched this event.",
        RiskLevel.LOW,
        None,
        human_approval_required=False,
    )


def dedupe(values: list[str] | set[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _decision(
    case: CaseRecord,
    event: CaseEvent,
    pathway: Pathway,
    tags: set[str],
    score: int,
    next_state: str,
    draft_type: str,
    rationale: str,
    risk_level: RiskLevel,
    due_at,
    human_approval_required: bool = True,
) -> EscalationDecision:
    return EscalationDecision(
        decision_id=content_id("dec", case.case_id, event.event_id, pathway.value, rationale),
        case_id=case.case_id,
        source_event_id=event.event_id,
        pathway=pathway,
        issue_tags=dedupe(tags),
        pressure_score=score,
        current_state=case.status,
        recommended_next_state=next_state,
        draft_type=draft_type,
        human_approval_required=human_approval_required,
        due_at=due_at,
        evidence_refs=event.evidence_refs,
        risk_level=risk_level,
        rationale=rationale,
        created_at=utc_now(),
    )


def _extract_fee_lines(text: str) -> list[FeeLine]:
    lines: list[FeeLine] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or not _line_can_contain_fee_math(line):
            continue
        match = _duration_rate_match(line)
        if match is None:
            continue
        remainder = line[match.end() :]
        amounts = _line_amounts(remainder)
        hours = float(match.groupdict().get("hours") or 0)
        minutes = float(match.groupdict().get("minutes") or 0)
        hours += minutes / 60
        rate = float(match.group("rate"))
        amount = amounts[-1] if amounts else None
        lines.append(
            FeeLine(description=line[:160], hours=round(hours, 4), rate=rate, amount=amount)
        )
    return lines


def _detect_total(text: str) -> float | None:
    for raw_line in text.splitlines():
        line = raw_line.strip().lower()
        if (
            ("total" in line or "deposit" in line or "estimated cost" in line)
            and "emails" not in line
            and "messages" not in line
            and "records responsive" not in line
        ):
            matches = MONEY_RE.findall(raw_line)
            if matches:
                return _money_to_float(matches[-1])
    return None


def _money_to_float(value: str | tuple[str, ...]) -> float:
    if isinstance(value, tuple):
        value = value[0]
    return float(value.replace(",", ""))


def _task_breakdown_missing(text: str) -> bool:
    generic_terms = ["labor", "review", "research", "redaction", "records preparation"]
    basis_terms = [
        "personnel",
        "classification",
        "phase",
        "task",
        "hourly rate",
        "staff title",
        "staff time",
        "per hour",
        "emails per hour",
        "minutes at",
        "hours at",
        "processing time",
    ]
    return _has_any(text, generic_terms) and not _has_any(text, basis_terms)


def _has_any(text: str, needles: list[str]) -> bool:
    return any(needle in text for needle in needles)


def _is_payment_confirmation(summary: str, text: str) -> bool:
    combined = f"{summary}\n{text}".lower()
    return "payment confirmation" in combined or "online payment confirmation" in combined


def _is_submitted_acknowledgment(summary: str, text: str) -> bool:
    combined = f"{summary}\n{text}".lower()
    return (
        "has been submitted" in combined
        or "submitted successfully" in combined
        or "confirm receipt of your public record request" in combined
    )


def _is_records_released(summary: str, text: str) -> bool:
    combined = f"{summary}\n{text}".lower()
    return _has_any(
        combined,
        [
            "document released to requester",
            "documents have been released",
            "we released the public records responsive",
            "we released all of the responsive public records",
            "provided all records responsive",
        ],
    )


def _is_fulfilled_closure(summary: str, text: str) -> bool:
    combined = f"{summary}\n{text}".lower()
    if "has been closed" not in combined and "record request" not in combined:
        return False
    return _has_any(
        combined,
        [
            "fulfilled",
            "provided all records responsive",
            "released all of the responsive public records",
            "we released all",
            "records have been released",
        ],
    )


def _contains_unresolved_closure_threat(text: str) -> bool:
    if _has_any(text, ["has been closed", "record request #"]) and _has_any(
        text, ["fulfilled", "provided all records responsive", "documents have been released"]
    ):
        return False
    return _has_any(
        text,
        [
            "will close",
            "will be closed",
            "request will close",
            "request will be closed",
            "close your request",
            "closed if payment",
            "administratively close",
            "we will consider this request closed",
            "your request will be closed",
        ],
    )


def _contains_actual_fee_estimate(text: str) -> bool:
    if _has_any(
        text,
        [
            "attached invoice is a cost estimate",
            "your estimated total is",
            "estimated total is",
            "estimated cost of",
            "special service charge",
            "cost estimate for",
            "deposit payment needed",
        ],
    ):
        return True
    if "deposit" in text and _has_any(text, ["required before", "payment needed", "invoice"]):
        return True
    if "fee estimate" in text:
        return True
    if "cost estimate" in text:
        future_or_conditional = _has_any(
            text,
            [
                "will provide you with a cost estimate, if any",
                "provide a cost estimate, if any",
                "cost estimate, if any",
                "if any fees will be charged",
            ],
        )
        return not future_or_conditional
    return bool(_extract_fee_lines(text))


def _duration_rate_match(line: str):
    return (
        HOURS_MINUTES_RATE_RE.search(line)
        or MINUTES_RATE_RE.search(line)
        or PROCESSING_RATE_RE.search(line)
        or RESULTS_IN_HOURS_RATE_RE.search(line)
    )


def _line_can_contain_fee_math(line: str) -> bool:
    lowered = line.lower()
    if _has_any(lowered, ["emails per hour", "messages per hour", "48-hour", "72-hour"]):
        return False
    if "$" not in line:
        return False
    return _has_any(
        lowered,
        [
            "staff time",
            "labor",
            "processing time",
            "estimated cost",
            "per hour",
            "/hr",
            "project coordinator",
            "records (technology search",
        ],
    )


def _line_amounts(remainder: str) -> list[float]:
    lowered = remainder.lower()
    if not _has_any(lowered, ["estimated cost", "cost of", "=", "total", "amount"]):
        return []
    return [_money_to_float(value) for value in MONEY_RE.findall(remainder)]


def _base_event_type(event_type: str) -> str:
    if event_type in {
        "fee_estimate_received",
        "closure_warning_received",
        "payment_confirmation_received",
        "submitted_acknowledgment",
        "records_released",
        "fulfilled_closure",
    }:
        return "agency_message_received"
    return event_type


def is_fee_resolved_event(event: CaseEvent, classification: EventClassification) -> bool:
    text = event.content_text.lower()
    return classification.event_type == "payment_confirmation_received" or (
        classification.event_type == "human_sent_message"
        and _has_any(
            text,
            [
                "submitted payment",
                "payment has been made",
                "i have submitted payment",
                "i am comfortable paying",
                "please proceed with processing",
                "please proceed",
                "agree to pay",
                "authorize work",
            ],
        )
    )


def is_case_resolved_event(classification: EventClassification) -> bool:
    return classification.event_type in {"records_released", "fulfilled_closure"}


NO_ACTION_EVENT_TYPES = {
    "human_sent_message",
    "payment_confirmation_received",
    "submitted_acknowledgment",
    "records_released",
    "fulfilled_closure",
}
