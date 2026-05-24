from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from prr_pressure_cooker.config import Settings
from prr_pressure_cooker.drafts import build_packet_bundle, write_draft
from prr_pressure_cooker.models import CaseEvent, CaseRecord, EscalationDecision, Pathway


def _case() -> CaseRecord:
    now = datetime(2026, 5, 22, tzinfo=UTC)
    return CaseRecord(
        case_id="demo",
        agency="Demo Agency",
        request_title="Demo request",
        created_at=now,
        updated_at=now,
    )


def _event() -> CaseEvent:
    return CaseEvent(
        event_id="evt_demo",
        case_id="demo",
        received_at=datetime(2026, 5, 22, tzinfo=UTC),
        summary="Agency response",
        content_text="Agency response text",
        evidence_refs=["evi_demo"],
    )


def _decision(pathway: Pathway, draft_type: str) -> EscalationDecision:
    return EscalationDecision(
        decision_id=f"dec_{pathway.value}",
        case_id="demo",
        source_event_id="evt_demo",
        pathway=pathway,
        issue_tags=[pathway.value],
        pressure_score=5,
        current_state="INTAKE",
        recommended_next_state="review",
        draft_type=draft_type,
        evidence_refs=["evi_demo"],
        rationale="Test rationale.",
        created_at=datetime(2026, 5, 22, tzinfo=UTC),
    )


def test_pathway_packet_builders_create_named_outputs_and_indexes(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "prr.db", casefiles_dir=tmp_path / "casefiles")
    expected = {
        Pathway.FEE_OPACITY: ("particularized_estimate_request", "03_fee_audit.md"),
        Pathway.DEFECTIVE_ESTIMATE: ("supervisor_review_request", "03_fee_audit.md"),
        Pathway.SILENCE_DELAY: ("status_nudge", "03_unanswered_questions_matrix.md"),
        Pathway.CLOSURE_THREAT: ("no_withdrawal_preservation_reply", "03_closure_timeline.md"),
        Pathway.EXEMPTION_VAGUENESS: (
            "withholding_exemption_matrix",
            "03_withholding_exemption_matrix.md",
        ),
        Pathway.COUNSEL_OR_MEDIATION: ("attorney_faf_packet", "03_attorney_faf_packet.md"),
        Pathway.PUBLIC_PRESSURE: (
            "commissioner_reporter_one_pager",
            "03_commissioner_reporter_one_pager.md",
        ),
    }

    for pathway, (draft_type, expected_name) in expected.items():
        paths = [
            Path(path)
            for path in build_packet_bundle(
                _case(), _event(), _decision(pathway, draft_type), settings
            )
        ]
        names = {path.name for path in paths}

        assert expected_name in names
        assert {"messages.csv", "threads.csv", "attachments.csv", "contacts.csv"} <= names
        assert all("raw" not in path.parts for path in paths)
        assert any("derived" in path.parts and "packets" in path.parts for path in paths)
        assert any("redacted" in path.parts and path.name == "README.md" for path in paths)


def test_no_withdrawal_draft_uses_case_history_for_stronger_rebuttal(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "prr.db", casefiles_dir=tmp_path / "casefiles")
    case = CaseRecord(
        case_id="allmail-records-corr-2026-300",
        agency="Osceola County Corrections",
        request_title="Osceola County Corrections Records Request CORR-2026-300",
        created_at=datetime(2026, 5, 20, tzinfo=UTC),
        updated_at=datetime(2026, 5, 20, tzinfo=UTC),
    )
    agency_event = CaseEvent(
        event_id="evt_agency",
        case_id=case.case_id,
        event_type="closure_warning_received",
        received_at=datetime(2026, 5, 20, 13, 32, tzinfo=UTC),
        summary="Deposit Payment Needed",
        content_text=(
            "Please be advised that the last clarification communication was completed "
            "on May 12, 2026. As stated, when the invoice was issued, you have 10 "
            "business days to respond. Please note, if a response is not received "
            "within 10 business days from May 12, 2026, your request will be closed."
        ),
    )
    requester_reply = CaseEvent(
        event_id="evt_requester",
        case_id=case.case_id,
        event_type="human_sent_message",
        received_at=datetime(2026, 5, 20, 14, 55, tzinfo=UTC),
        summary="Requester disputed the estimate",
        content_text=(
            "Good afternoon Ms. Nimcharoen,\n"
            "Thank you for sending the estimate. I understand from your email that "
            "Osceola County Corrections is estimating a total of 53 staff hours--3 "
            "hours for a Records Technician, 10 hours for Inmate Services personnel, "
            "and 40 hours for Finance personnel--at a total cost of $1,318.86 with "
            "a $659.43 deposit.\n"
            "The county's estimate gives no document count, task breakdown, or "
            "explanation of why 53 hours of work are required. I still do not know "
            "the approximate number of finance records or pages involved, whether "
            "the records are electronic or paper, whether email correspondence is "
            "included, and whether duplicates were removed before estimating the time.\n"
            "If it is administratively easier, I remain willing to proceed with Phase "
            "1 on its own or to narrow the date range.\n"
            "Please keep the request open and confirm that the statutory deadline is "
            "paused while I await a lawful and reasonable estimate."
        ),
    )
    decision = _decision(Pathway.CLOSURE_THREAT, "no_withdrawal_preservation_reply")

    draft_path = write_draft(
        case,
        agency_event,
        decision,
        settings,
        case_events=[agency_event, requester_reply],
    )
    draft = draft_path.read_text(encoding="utf-8")

    assert "I already responded on May 20, 2026" in draft
    assert "10 business days from May 12, 2026" in draft
    assert "53 staff hours" in draft
    assert "$1,318.86 total" in draft
    assert "a $659.43 deposit" in draft
    assert "40-hour Finance Personnel line" in draft
    assert "approximate number of records, pages, files, and attachments" in draft
    assert "whether email correspondence is included" in draft
    assert "whether duplicates were excluded before estimating time" in draft
    assert "I remain willing to proceed in phases or narrow the request" in draft
    assert "I am not withdrawing or abandoning this request. Please preserve" not in draft


def test_duplicate_inflation_draft_uses_case_context_not_one_line(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "prr.db", casefiles_dir=tmp_path / "casefiles")
    case = CaseRecord(
        case_id="allmail-osceolacountysheriff-records-center-public-record-request-p041212-031826",
        agency="OSCEOLACOUNTYSHERIFF",
        request_title="[Records Center] PUBLIC RECORD REQUEST :: P041212-031826",
        created_at=datetime(2026, 5, 13, tzinfo=UTC),
        updated_at=datetime(2026, 5, 13, tzinfo=UTC),
    )
    agency_event = CaseEvent(
        event_id="evt_agency",
        case_id=case.case_id,
        event_type="agency_message_received",
        received_at=datetime(2026, 5, 16, 13, 41, tzinfo=UTC),
        summary="[Records Center] PUBLIC RECORD REQUEST :: P041212-031826",
        content_text=(
            "The request has been in \"Waiting for Payment\" status for 30 days. "
            "The Osceola County Sheriff's Office considers this request closed. "
            "Description of Records Requested: public records related to ICE detention "
            "services, immigration enforcement coordination, Agreement Records, "
            "287(g) program operations, financial records, operational data, "
            "and communications."
        ),
        evidence_refs=["evi_demo"],
    )
    decision = _decision(Pathway.DUPLICATE_INFLATION, "duplicate_inflation_audit")

    draft_path = write_draft(case, agency_event, decision, settings, case_events=[agency_event])
    draft = draft_path.read_text(encoding="utf-8")

    assert "P041212-031826" in draft
    assert "waiting-for-payment status" in draft
    assert "agreement and contract records" in draft
    assert "unique responsive records, pages, files, attachments, and messages" in draft
    assert "duplicate emails, duplicate attachments" in draft
    assert "Please explain how duplicate records were counted" not in draft


def test_no_withdrawal_draft_names_waiting_for_payment_closed_notice(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "prr.db", casefiles_dir=tmp_path / "casefiles")
    case = CaseRecord(
        case_id="allmail-osceolacountysheriff-records-center-public-record-request-p041212-031826",
        agency="OSCEOLACOUNTYSHERIFF",
        request_title="[Records Center] PUBLIC RECORD REQUEST :: P041212-031826",
        created_at=datetime(2026, 5, 13, tzinfo=UTC),
        updated_at=datetime(2026, 5, 13, tzinfo=UTC),
    )
    agency_event = CaseEvent(
        event_id="evt_agency",
        case_id=case.case_id,
        event_type="closure_warning_received",
        received_at=datetime(2026, 5, 16, 13, 41, tzinfo=UTC),
        summary="[Records Center] PUBLIC RECORD REQUEST :: P041212-031826",
        content_text=(
            "The request has been in \"Waiting for Payment\" status for 30 days. "
            "The Osceola County Sheriff's Office considers this request closed."
        ),
        evidence_refs=["evi_demo"],
    )
    decision = _decision(Pathway.CLOSURE_THREAT, "no_withdrawal_preservation_reply")

    draft_path = write_draft(case, agency_event, decision, settings, case_events=[agency_event])
    draft = draft_path.read_text(encoding="utf-8")

    assert "P041212-031826" in draft
    assert "30-day waiting-for-payment status" in draft
    assert "I am not withdrawing or abandoning P041212-031826" in draft
