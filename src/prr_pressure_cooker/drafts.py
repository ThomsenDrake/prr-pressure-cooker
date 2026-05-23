from __future__ import annotations

from pathlib import Path

from prr_pressure_cooker.config import Settings
from prr_pressure_cooker.ids import slugify, utc_now
from prr_pressure_cooker.models import CaseEvent, CaseRecord, EscalationDecision


def write_draft(
    case: CaseRecord, event: CaseEvent, decision: EscalationDecision, settings: Settings
) -> Path:
    drafts_dir = settings.casefiles_dir / case.case_id / "drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    draft_path = drafts_dir / f"{decision.created_at.strftime('%Y%m%d')}_{decision.draft_type}.md"
    draft_path.write_text(_draft_body(case, event, decision), encoding="utf-8")
    return draft_path


def build_packet_skeleton(
    case: CaseRecord, event: CaseEvent, decision: EscalationDecision, settings: Settings
) -> list[str]:
    packet_dir = settings.casefiles_dir / case.case_id / "packets" / slugify(decision.pathway)
    packet_dir.mkdir(parents=True, exist_ok=True)
    summary_path = packet_dir / "00_case_summary.md"
    timeline_path = packet_dir / "01_timeline.md"
    evidence_path = packet_dir / "02_evidence_refs.md"

    summary_path.write_text(
        "\n".join(
            [
                f"# {case.request_title}",
                "",
                f"- Case: `{case.case_id}`",
                f"- Agency: {case.agency}",
                f"- Pathway: `{decision.pathway}`",
                f"- Recommended state: `{decision.recommended_next_state}`",
                f"- Risk: `{decision.risk_level}`",
                "",
                decision.rationale,
                "",
            ]
        ),
        encoding="utf-8",
    )
    timeline_path.write_text(
        "\n".join(
            [
                "# Timeline",
                "",
                f"- {event.received_at.isoformat()} - {event.summary}",
                f"- {utc_now().isoformat()} - Drafted `{decision.draft_type}` for human review.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    evidence_path.write_text(
        "# Evidence References\n\n"
        + "\n".join(f"- `{evidence_id}`" for evidence_id in decision.evidence_refs)
        + "\n",
        encoding="utf-8",
    )
    return [str(path.resolve()) for path in [summary_path, timeline_path, evidence_path]]


def _draft_body(case: CaseRecord, event: CaseEvent, decision: EscalationDecision) -> str:
    return "\n".join(
        [
            f"# Draft: {decision.draft_type.replace('_', ' ').title()}",
            "",
            "> Human approval is required before anything leaves this system.",
            "",
            f"- Case: `{case.case_id}`",
            f"- Agency: {case.agency}",
            f"- Source event: `{event.event_id}`",
            f"- Pathway: `{decision.pathway}`",
            f"- Pressure score: `{decision.pressure_score}`",
            f"- Due: `{decision.due_at.isoformat() if decision.due_at else 'not set'}`",
            "",
            "## Rationale",
            "",
            decision.rationale,
            "",
            "## Evidence",
            "",
            *[f"- `{ref}`" for ref in decision.evidence_refs],
            "",
            "## Proposed Message",
            "",
            _message_template(decision),
            "",
        ]
    )


def _message_template(decision: EscalationDecision) -> str:
    templates = {
        "status_nudge": (
            "Please provide a current status update for this public records request, "
            "including whether any records have been located, whether any exemptions "
            "are being asserted, "
            "and the expected next step."
        ),
        "particularized_estimate_request": (
            "Please provide a particularized explanation of the estimate, including the "
            "task phases, personnel classifications, hourly rates, and how each line "
            "item relates to responsive records."
        ),
        "supervisor_review_request": (
            "Please route this estimate for supervisory review. I cannot evaluate or pay "
            "an estimate "
            "that does not reconcile the stated hours, rates, alternatives, and total."
        ),
        "forced_agency_position_letter": (
            "Please clarify the agency's position on records held by this agency: whether "
            "a reasonable search was conducted, whether no agency-held responsive "
            "records exist, whether records are "
            "exempt, or whether records can be produced."
        ),
        "no_withdrawal_preservation_reply": (
            "I am not withdrawing or abandoning this request. Please preserve the request "
            "while the "
            "pending clarification or estimate issue is resolved."
        ),
        "attorney_faf_packet": (
            "Attached is a timeline and evidence packet for review. The request appears "
            "unresolved after "
            "formal notice and the cure window."
        ),
    }
    return templates.get(decision.draft_type, "Draft content pending human review.")
