from datetime import UTC, datetime

from prr_pressure_cooker.models import CaseEvent, CaseRecord
from prr_pressure_cooker.rules import audit_fee_estimate, classify_event, compute_decision


def case():
    now = datetime(2026, 5, 22, tzinfo=UTC)
    return CaseRecord(
        case_id="demo",
        agency="Demo Agency",
        request_title="Demo request",
        created_at=now,
        updated_at=now,
    )


def event(text: str, event_type: str = "agency_message_received"):
    return CaseEvent(
        event_id="evt_demo",
        case_id="demo",
        event_type=event_type,
        received_at=datetime(2026, 5, 22, tzinfo=UTC),
        summary="summary",
        content_text=text,
    )


def test_closure_threat_wins_over_fee_opacity():
    evt = event("A deposit is required. If payment is not received, the request will be closed.")
    classification = classify_event(evt, case())
    audit = audit_fee_estimate(evt)

    decision = compute_decision(case(), evt, classification, audit)

    assert decision.pathway == "closure_threat"
    assert decision.pressure_score == 6
    assert decision.human_approval_required is True


def test_defective_estimate_detects_math_mismatch():
    evt = event("Research labor: 10 hours at $35/hr = $400.00\nTotal: $400.00")
    classification = classify_event(evt, case())
    audit = audit_fee_estimate(evt)

    decision = compute_decision(case(), evt, classification, audit)

    assert audit.math_defect is True
    assert decision.pathway == "defective_estimate"
    assert "defective_estimate" in decision.issue_tags


def test_fee_opacity_requires_particularized_estimate():
    evt = event("Fee estimate: deposit $350.00\nLabor and records review.")
    classification = classify_event(evt, case())
    audit = audit_fee_estimate(evt)

    decision = compute_decision(case(), evt, classification, audit)

    assert audit.task_breakdown_missing is True
    assert decision.pathway == "fee_opacity"


def test_custodian_dodge_routes_forced_position_letter():
    evt = event("We do not maintain these records. Please contact another agency.")
    classification = classify_event(evt, case())
    decision = compute_decision(case(), evt, classification, None)

    assert decision.pathway == "custodian_dodge"
    assert decision.draft_type == "forced_agency_position_letter"


def test_no_action_for_plain_status_update():
    evt = event("We have received your request and are continuing to process it.")
    classification = classify_event(evt, case())
    decision = compute_decision(case(), evt, classification, None)

    assert decision.pathway == "no_action"
    assert decision.human_approval_required is False


def test_future_conditional_cost_estimate_is_not_fee_opacity():
    evt = event(
        "This correspondence acknowledges receipt of your public records request. "
        "We are reviewing our records to determine if there are any responsive records. "
        "Once this has been determined, I will provide you with a cost estimate, if any."
    )
    classification = classify_event(evt, case())
    audit = audit_fee_estimate(evt)
    decision = compute_decision(case(), evt, classification, audit)

    assert classification.contains_fee_estimate is False
    assert decision.pathway == "no_action"


def test_human_sent_message_does_not_escalate_even_if_quoting_fee_language():
    evt = event(
        "I paid this estimate.\n\nFrom: Agency\nFee estimate: deposit $350.00",
        event_type="human_sent_message",
    )
    classification = classify_event(evt, case())
    audit = audit_fee_estimate(evt)
    decision = compute_decision(case(), evt, classification, audit)

    assert classification.issue_tags == []
    assert decision.pathway == "no_action"


def test_payment_confirmation_does_not_escalate():
    evt = event(
        "Orange County Public Records Online Payment Confirmation - PRR-163721\n"
        "Payment confirmation number 12345."
    )
    evt.summary = "Orange County Public Records Online Payment Confirmation - PRR-163721"
    classification = classify_event(evt, case())
    audit = audit_fee_estimate(evt)
    decision = compute_decision(case(), evt, classification, audit)

    assert classification.event_type == "payment_confirmation_received"
    assert decision.pathway == "no_action"


def test_orange_county_record_counts_do_not_create_math_defect():
    evt = event(
        "PRR-162016 cost estimate for responsive emails.\n"
        "Your search returned approximately 1,970 emails. Staff can review "
        "approximately 200 emails per hour.\n"
        "Records staff time: 9.85 hours at $27.09 per hour; estimated cost $266.84.\n"
        "Payment is required before processing."
    )
    classification = classify_event(evt, case())
    audit = audit_fee_estimate(evt)
    decision = compute_decision(case(), evt, classification, audit)

    assert audit.math_defect is False
    assert audit.detected_total == 266.84
    assert decision.pathway == "no_action"


def test_orange_county_specific_estimate_is_not_fee_opacity():
    evt = event(
        "PRR-163721 cost estimate for public records.\n"
        "Records processing time: 2.5 hours at $27.09 per hour, estimated cost $67.73."
    )
    classification = classify_event(evt, case())
    audit = audit_fee_estimate(evt)
    decision = compute_decision(case(), evt, classification, audit)

    assert classification.contains_fee_estimate is True
    assert audit.task_breakdown_missing is False
    assert decision.pathway == "no_action"


def test_orlando_fulfilled_closure_is_no_action():
    evt = event(
        "Public records request #26-10768 has been closed as fulfilled. "
        "Documents have been released to requester. If you have questions, "
        "contact the Records Office."
    )
    classification = classify_event(evt, case())
    decision = compute_decision(case(), evt, classification, None)

    assert classification.event_type == "fulfilled_closure"
    assert decision.pathway == "no_action"


def test_orlando_hours_minutes_estimate_with_closure_clock_routes_closure_threat():
    evt = event(
        "For request #26-11231, the estimated processing time is 2 hours 35 minutes "
        "at $27.09 per hour for an estimated cost of $69.98. If payment is not "
        "received within two business days, your request will be closed."
    )
    classification = classify_event(evt, case())
    audit = audit_fee_estimate(evt)
    decision = compute_decision(case(), evt, classification, audit)

    assert audit.math_defect is False
    assert audit.computed_total == 69.98
    assert decision.pathway == "closure_threat"


def test_submission_acknowledgment_with_possible_future_deposit_is_no_action():
    evt = event(
        "Orange County Sheriff's Office public records request #26-17289 has been "
        "submitted successfully. If the request is voluminous, a deposit may be "
        "required before records are released."
    )
    classification = classify_event(evt, case())
    audit = audit_fee_estimate(evt)
    decision = compute_decision(case(), evt, classification, audit)

    assert classification.event_type == "submitted_acknowledgment"
    assert classification.contains_fee_estimate is False
    assert decision.pathway == "no_action"


def test_osceola_closure_warning_remains_closure_threat():
    evt = event(
        "Records Request CORR-2026-300 requires clarification. If we do not receive "
        "your response, we will consider this request closed."
    )
    classification = classify_event(evt, case())
    decision = compute_decision(case(), evt, classification, None)

    assert decision.pathway == "closure_threat"


def test_fee_line_parses_hours_plus_minutes():
    evt = event(
        "Estimated processing time: 2 hours 35 minutes at $27.09 per hour "
        "for an estimated cost of $69.98."
    )
    audit = audit_fee_estimate(evt)

    assert len(audit.lines) == 1
    assert audit.lines[0].hours == 2.5833
    assert audit.lines[0].rate == 27.09
    assert audit.lines[0].amount == 69.98
    assert audit.computed_total == 69.98


def test_multiple_fee_lines_sum_to_detected_total():
    evt = event(
        "Technology search: 2 hours at $27.09 per hour = $54.18\n"
        "Legal review: 2 hours at $33.55 per hour = $67.10\n"
        "Estimated total: $121.28"
    )
    audit = audit_fee_estimate(evt)

    assert audit.computed_total == 121.28
    assert audit.detected_total == 121.28
    assert audit.math_defect is False


def test_fee_parser_ignores_non_money_identifiers_and_counts():
    evt = event(
        "Request #26-17289 was received on 05/22/2026. "
        "Call 407-555-1212 with questions. 1,970 emails were located and "
        "staff reviews 200 emails per hour. A response is due within 48-hour notice."
    )
    audit = audit_fee_estimate(evt)

    assert audit.contains_fee_estimate is False
    assert audit.lines == []
    assert audit.detected_total is None
