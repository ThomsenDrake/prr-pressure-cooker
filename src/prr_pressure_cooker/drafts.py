from __future__ import annotations

import re
import shutil
from pathlib import Path

from prr_pressure_cooker.config import Settings
from prr_pressure_cooker.ids import slugify, utc_now
from prr_pressure_cooker.models import CaseEvent, CaseRecord, EscalationDecision, Pathway


def write_draft(
    case: CaseRecord,
    event: CaseEvent,
    decision: EscalationDecision,
    settings: Settings,
    case_events: list[CaseEvent] | None = None,
    case_history_text: str | None = None,
) -> Path:
    drafts_dir = settings.casefiles_dir / case.case_id / "drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    draft_path = drafts_dir / f"{decision.created_at.strftime('%Y%m%d')}_{decision.draft_type}.md"
    draft_path.write_text(
        _draft_body(case, event, decision, case_events or [event], case_history_text),
        encoding="utf-8",
    )
    return draft_path


def build_packet_bundle(
    case: CaseRecord,
    event: CaseEvent,
    decision: EscalationDecision,
    settings: Settings,
    case_index_paths: list[str] | None = None,
) -> list[str]:
    packet_dir = (
        settings.casefiles_dir / case.case_id / "derived" / "packets" / slugify(decision.pathway)
    )
    redacted_dir = (
        settings.casefiles_dir / case.case_id / "redacted" / "packets" / slugify(decision.pathway)
    )
    indexes_dir = packet_dir / "indexes"
    packet_dir.mkdir(parents=True, exist_ok=True)
    redacted_dir.mkdir(parents=True, exist_ok=True)
    indexes_dir.mkdir(parents=True, exist_ok=True)
    summary_path = packet_dir / "00_case_summary.md"
    timeline_path = packet_dir / "01_timeline.md"
    evidence_path = packet_dir / "02_evidence_refs.md"
    redacted_readme = redacted_dir / "README.md"

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
    redacted_readme.write_text(
        "\n".join(
            [
                "# Redacted Outputs",
                "",
                "Put human-reviewed redacted derivatives here. Raw evidence remains under "
                "the case raw evidence tree and should not be edited.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    pathway_paths = []
    for filename, body in _pathway_specific_packets(case, event, decision):
        path = packet_dir / filename
        path.write_text(body, encoding="utf-8")
        pathway_paths.append(path)

    messages_index = indexes_dir / "messages.csv"
    threads_index = indexes_dir / "threads.csv"
    attachments_index = indexes_dir / "attachments.csv"
    contacts_index = indexes_dir / "contacts.csv"
    _write_packet_index(
        messages_index,
        case_index_paths,
        "messages.csv",
        "event_id,received_at,summary,pathway\n"
        f"{event.event_id},{event.received_at.isoformat()},"
        f"{_csv_cell(event.summary)},{decision.pathway}\n",
    )
    _write_packet_index(
        threads_index,
        case_index_paths,
        "threads.csv",
        "case_id,request_title,agency,latest_event_id\n"
        f"{case.case_id},{_csv_cell(case.request_title)},"
        f"{_csv_cell(case.agency)},{event.event_id}\n",
    )
    _write_packet_index(
        attachments_index,
        case_index_paths,
        "attachments.csv",
        "evidence_id,source_event_id\n"
        + "".join(f"{evidence_id},{event.event_id}\n" for evidence_id in decision.evidence_refs),
    )
    _write_packet_index(
        contacts_index,
        case_index_paths,
        "contacts.csv",
        "role,name_or_address\n"
        f"agency,{_csv_cell(case.agency)}\n"
        "requester,drake.t98@proton.me\n",
    )
    paths = [
        summary_path,
        timeline_path,
        evidence_path,
        *pathway_paths,
        messages_index,
        threads_index,
        attachments_index,
        contacts_index,
        redacted_readme,
    ]
    return [str(path.resolve()) for path in paths]


def build_packet_skeleton(
    case: CaseRecord, event: CaseEvent, decision: EscalationDecision, settings: Settings
) -> list[str]:
    return build_packet_bundle(case, event, decision, settings)


def _write_packet_index(
    destination: Path,
    case_index_paths: list[str] | None,
    filename: str,
    fallback: str,
) -> None:
    source = _find_case_index(case_index_paths, filename)
    if source is not None:
        shutil.copy2(source, destination)
        return
    destination.write_text(fallback, encoding="utf-8")


def _find_case_index(case_index_paths: list[str] | None, filename: str) -> Path | None:
    if not case_index_paths:
        return None
    for raw_path in case_index_paths:
        path = Path(raw_path)
        if path.name == filename and path.exists():
            return path
    return None


def _draft_body(
    case: CaseRecord,
    event: CaseEvent,
    decision: EscalationDecision,
    case_events: list[CaseEvent],
    case_history_text: str | None = None,
) -> str:
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
            _message_template(case, event, decision, case_events, case_history_text),
            "",
        ]
    )


def _message_template(
    case: CaseRecord,
    event: CaseEvent,
    decision: EscalationDecision,
    case_events: list[CaseEvent],
    case_history_text: str | None = None,
) -> str:
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
        "no_withdrawal_preservation_reply": _no_withdrawal_preservation_reply,
        "attorney_faf_packet": (
            "Attached is a timeline and evidence packet for review. The request appears "
            "unresolved after "
            "formal notice and the cure window."
        ),
        "withholding_exemption_matrix": (
            "Please identify each withheld record or redaction, the specific exemption "
            "asserted, and how the exemption applies to the withheld material."
        ),
        "duplicate_inflation_audit": _duplicate_inflation_reply,
        "commissioner_reporter_one_pager": (
            "This one-pager summarizes the request timeline, unresolved public-interest "
            "issues, and the precise agency action needed to resolve the records request."
        ),
    }
    template = templates.get(decision.draft_type)
    if callable(template):
        return template(case, event, decision, case_events, case_history_text)
    if template is not None:
        return template
    return "Draft content pending human review."


def _no_withdrawal_preservation_reply(
    case: CaseRecord,
    event: CaseEvent,
    decision: EscalationDecision,
    case_events: list[CaseEvent],
    case_history_text: str | None = None,
) -> str:
    history = sorted(case_events or [event], key=lambda item: (item.received_at, item.event_id))
    direct_history = "\n\n".join(_direct_message_body(item.content_text) for item in history)
    full_history = "\n\n".join(item.content_text for item in history)
    latest_requester_reply = _latest_requester_reply(history)
    prior_reply_text = (
        _direct_message_body(latest_requester_reply.content_text)
        if latest_requester_reply is not None
        else ""
    )
    evidence_text = "\n\n".join(
        part
        for part in (
            direct_history,
            full_history,
            case_history_text or "",
            event.content_text,
            case.request_title,
        )
        if part
    )
    request_id = _request_identifier(case)
    clock = _closure_clock(event.content_text)
    cost_summary = _cost_summary(evidence_text)
    unresolved_issue = _unresolved_estimate_issue(evidence_text)
    detail_requests = _estimate_detail_requests(evidence_text)
    phased_path = _phased_path(prior_reply_text)
    previous_reply_line = _previous_reply_line(latest_requester_reply, event, request_id)

    lines = [
        "Good afternoon,",
        "",
        f"I am following up on {request_id}.",
    ]
    if previous_reply_line:
        lines.append(previous_reply_line)
    if clock:
        if "status" in clock or "notice" in clock:
            lines.append(
                f"I object to treating {request_id} as closed based on the {clock} "
                "while the estimate issue remains unresolved."
            )
        else:
            lines.append(
                f"I object to closing the request based on the {clock} response clock while "
                "the estimate issue remains unresolved."
            )
    else:
        lines.append(
            "I object to closing the request while the estimate issue remains unresolved."
        )
    lines.extend(
        [
            "",
            (
                f"I am not withdrawing or abandoning {request_id}. Florida's "
                "public-records law permits a special service charge only when the "
                "request requires extensive clerical, supervisory, or information "
                "technology resources, and the charge must be reasonable and tied to "
                "the actual work required."
            ),
        ]
    )
    if cost_summary:
        lines.append(cost_summary)
    if unresolved_issue:
        lines.append(unresolved_issue)
    if detail_requests:
        lines.append(detail_requests)

    lines.extend(
        [
            "",
            (
                "I do not authorize the deposit or any charge based on the current "
                "estimate. I need a particularized, task-specific estimate before I can "
                "decide whether to proceed, narrow, or modify the request."
            ),
        ]
    )
    if phased_path:
        lines.append(phased_path)

    lines.extend(
        [
            "",
            (
                f"Please keep {request_id} open, confirm that it will not be closed "
                "while this estimate dispute is pending, and provide a revised estimate "
                "or the factual basis for the current estimate."
            ),
            "",
            "Thank you,",
            "Drake Thomsen",
        ]
    )
    return "\n".join(lines)


def _latest_requester_reply(case_events: list[CaseEvent]) -> CaseEvent | None:
    requester_events = [
        event for event in case_events if event.event_type == "human_sent_message"
    ]
    return requester_events[-1] if requester_events else None


def _duplicate_inflation_reply(
    case: CaseRecord,
    event: CaseEvent,
    decision: EscalationDecision,
    case_events: list[CaseEvent],
    case_history_text: str | None = None,
) -> str:
    history = sorted(case_events or [event], key=lambda item: (item.received_at, item.event_id))
    evidence_text = "\n\n".join(
        part
        for part in (
            "\n\n".join(_direct_message_body(item.content_text) for item in history),
            "\n\n".join(item.content_text for item in history),
            case_history_text or "",
            event.content_text,
            case.request_title,
        )
        if part
    )
    request_id = _request_identifier(case)
    request_scope = _records_requested_summary(evidence_text)
    closure_line = _closure_position_line(evidence_text, request_id)

    lines = [
        "Good afternoon,",
        "",
        f"I am following up on {request_id}.",
    ]
    if request_scope:
        lines.append(f"My request seeks {request_scope}.")
    if closure_line:
        lines.append(closure_line)

    lines.extend(
        [
            "",
            (
                "Before I can evaluate any payment demand, volume estimate, or closure "
                "position, please provide the factual count basis for the responsive "
                "records. The current response repeats the request text, but it does not "
                "show how the agency counted unique responsive records or handled "
                "duplicates."
            ),
            "",
            "Please provide:",
            "",
            (
                "1. the number of unique responsive records, pages, files, attachments, "
                "and messages located for each requested category;"
            ),
            (
                "2. whether duplicate emails, duplicate attachments, duplicate contract "
                "copies, repeated payment records, or repeated jail/ICE activity records "
                "were excluded before estimating volume or staff time;"
            ),
            (
                "3. the search locations and date ranges used for each category, "
                "including agreement records, 287(g) program records, financial records, "
                "and operational ICE detention or hold data;"
            ),
            (
                "4. how any stated volume maps to the staff time or fee being requested; "
                "and"
            ),
            (
                "5. a revised estimate, or confirmation that no payment is currently due, "
                "once duplicate and nonresponsive counts are removed."
            ),
            "",
            (
                f"Please keep {request_id} open while this count and fee basis is "
                "resolved. I remain willing to narrow or phase the request after the "
                "agency identifies the actual unique responsive record volume."
            ),
            "",
            "Thank you,",
            "Drake Thomsen",
        ]
    )
    return "\n".join(lines)


def _records_requested_summary(text: str) -> str | None:
    lowered = text.lower()
    categories: list[str] = []
    category_labels = [
        ("agreement", "agreement and contract records involving ICE, DHS, DOJ, USMS, or 287(g)"),
        ("287(g)", "287(g) program records"),
        ("financial", "financial, invoice, payment, reimbursement, and budget records"),
        (
            "operational",
            "operational ICE detainee, immigration hold, transfer, and detention activity data",
        ),
        ("communications", "related communications and coordination records"),
    ]
    for needle, label in category_labels:
        if needle in lowered and label not in categories:
            categories.append(label)
    if categories:
        if len(categories) == 1:
            return categories[0]
        return ", ".join(categories[:-1]) + f", and {categories[-1]}"

    match = re.search(
        r"description of records requested:\s*(.+?)(?:\n\s*\n|$)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        match = re.search(
            r"records requested\s*(.+?)(?:\n\s*\n|$)",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
    if not match:
        return None
    summary = " ".join(match.group(1).split())
    if len(summary) > 260:
        summary = f"{summary[:260].rstrip()}..."
    return summary


def _closure_position_line(text: str, request_id: str) -> str | None:
    lowered = text.lower()
    if (
        "considers this request closed" in lowered
        or "has been in \"waiting for payment\"" in lowered
    ):
        return (
            f"I object to treating {request_id} as closed based only on waiting-for-payment "
            "status while the record count and fee basis remain unsupported."
        )
    if _closure_clock(text):
        return (
            f"I object to closing {request_id} while the count, duplicate-removal, and "
            "fee basis remain unresolved."
        )
    return None


def _direct_message_body(text: str) -> str:
    split_patterns = [
        r"\nOn .+ wrote:\s*",
        r"\nFrom:\s",
        r"\n-{2,}\s*Original Message\s*-{2,}",
    ]
    direct = text.strip()
    for pattern in split_patterns:
        match = re.search(pattern, direct, flags=re.IGNORECASE | re.DOTALL)
        if match:
            direct = direct[: match.start()].strip()
    return direct


def _request_identifier(case: CaseRecord) -> str:
    text = f"{case.request_title} {case.case_id}"
    for pattern in (
        r"\bCORR-\d{4}-\d+\b",
        r"\bP\d{6}-\d{6}\b",
        r"\bPRR-?\s*\d+\b",
        r"#\d{2}-\d+",
    ):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return " ".join(match.group(0).replace("\u2011", "-").split())
    return f"request `{case.case_id}`"


def _closure_clock(text: str) -> str | None:
    lowered = text.lower()
    if "waiting for payment" in lowered and "30 days" in lowered:
        return "30-day waiting-for-payment status"
    if "considers this request closed" in lowered:
        return "agency closed notice"
    match = re.search(
        r"(\d+\s+business\s+days?\s+from\s+[A-Z][a-z]+\s+\d{1,2},\s+\d{4})",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        return " ".join(match.group(1).split())
    if "will be closed" in text.lower():
        return "agency closure"
    return None


def _cost_summary(text: str) -> str | None:
    total_hours = re.search(r"total of\s+(\d+(?:\.\d+)?)\s+staff hours?", text, re.I)
    records_hours = re.search(r"(\d+(?:\.\d+)?)\s+hours?\s+for a Records Technician", text, re.I)
    inmate_hours = re.search(r"(\d+(?:\.\d+)?)\s+hours?\s+for Inmate Services", text, re.I)
    finance_hours = re.search(r"(\d+(?:\.\d+)?)\s+hours?\s+for Finance", text, re.I)
    total_cost = re.search(r"total cost of\s+\$([0-9,.]+)", text, re.I)
    deposit = re.search(r"\$([0-9,.]+)\s+deposit", text, re.I)
    if not any((total_hours, records_hours, inmate_hours, finance_hours, total_cost, deposit)):
        return None

    parts: list[str] = []
    if total_hours:
        parts.append(f"{total_hours.group(1)} staff hours")
    breakdown = []
    if records_hours:
        breakdown.append(f"{records_hours.group(1)} Records Technician hours")
    if inmate_hours:
        breakdown.append(f"{inmate_hours.group(1)} Inmate Services hours")
    if finance_hours:
        breakdown.append(f"{finance_hours.group(1)} Finance hours")
    if breakdown:
        parts.append(f"including {_join_list(breakdown)}")
    if total_cost:
        parts.append(f"${total_cost.group(1)} total")
    if deposit:
        parts.append(f"a ${deposit.group(1)} deposit")
    return f"The current estimate appears to require {_join_list(parts)}."


def _unresolved_estimate_issue(text: str) -> str | None:
    lowered = text.lower()
    if "40-hour finance" in lowered or "40 hours" in lowered and "finance" in lowered:
        return (
            "The main unresolved issue is the 40-hour Finance Personnel line, which "
            "appears to drive most of the estimate and still lacks enough factual "
            "support to evaluate whether the charge is reasonable."
        )
    if "estimate" in lowered:
        return (
            "The estimate still lacks enough factual support to evaluate whether the "
            "charge is reasonable."
        )
    return None


def _estimate_detail_requests(text: str) -> str | None:
    lowered = text.lower()
    requested: list[str] = []
    if "document count" in lowered or "number of finance records" in lowered:
        requested.append("the approximate number of records, pages, files, and attachments")
    if "paper" in lowered or "electronic" in lowered:
        requested.append("whether the records are paper, electronic, or mixed")
    if "email correspondence" in lowered:
        requested.append("whether email correspondence is included")
    if "duplicate" in lowered:
        requested.append("whether duplicates were excluded before estimating time")
    if "task breakdown" in lowered:
        requested.append("the task breakdown supporting each line item")
    if not requested:
        return None
    return f"Please provide {_join_list(requested)}."


def _phased_path(prior_reply_text: str) -> str | None:
    lowered = prior_reply_text.lower()
    if "phase 1" not in lowered and "narrow" not in lowered:
        return None
    return (
        "I remain willing to proceed in phases or narrow the request if that is "
        "administratively easier, but I need a clear estimate for the narrowed portion "
        "before authorizing work."
    )


def _previous_reply_line(
    latest_requester_reply: CaseEvent | None, trigger_event: CaseEvent, request_id: str
) -> str | None:
    if latest_requester_reply is None:
        return None
    if latest_requester_reply.received_at < trigger_event.received_at:
        return None
    date_text = (
        f"{latest_requester_reply.received_at.strftime('%B')} "
        f"{latest_requester_reply.received_at.day}, "
        f"{latest_requester_reply.received_at.year}"
    )
    return (
        f"I already responded on {date_text} and preserved {request_id}; that response "
        "should not be treated as abandonment or withdrawal."
    )


def _join_list(items: list[str]) -> str:
    clean_items = [item for item in items if item]
    if len(clean_items) <= 1:
        return clean_items[0] if clean_items else ""
    if len(clean_items) == 2:
        return f"{clean_items[0]} and {clean_items[1]}"
    return f"{', '.join(clean_items[:-1])}, and {clean_items[-1]}"


def _pathway_specific_packets(
    case: CaseRecord, event: CaseEvent, decision: EscalationDecision
) -> list[tuple[str, str]]:
    pathway = str(decision.pathway)
    builders = {
        Pathway.FEE_OPACITY.value: ("03_fee_audit.md", _fee_audit_packet),
        Pathway.DEFECTIVE_ESTIMATE.value: ("03_fee_audit.md", _fee_audit_packet),
        Pathway.SILENCE_DELAY.value: (
            "03_unanswered_questions_matrix.md",
            _unanswered_questions_packet,
        ),
        Pathway.CUSTODIAN_DODGE.value: (
            "03_unanswered_questions_matrix.md",
            _unanswered_questions_packet,
        ),
        Pathway.CLOSURE_THREAT.value: ("03_closure_timeline.md", _closure_timeline_packet),
        Pathway.EXEMPTION_VAGUENESS.value: (
            "03_withholding_exemption_matrix.md",
            _withholding_exemption_packet,
        ),
        Pathway.DUPLICATE_INFLATION.value: (
            "03_duplicate_inflation_audit.md",
            _duplicate_inflation_packet,
        ),
        Pathway.COUNSEL_OR_MEDIATION.value: (
            "03_attorney_faf_packet.md",
            _attorney_faf_packet,
        ),
        Pathway.PUBLIC_PRESSURE.value: (
            "03_commissioner_reporter_one_pager.md",
            _commissioner_reporter_packet,
        ),
    }
    filename, builder = builders.get(
        pathway, (f"03_{slugify(decision.draft_type)}.md", _generic_pathway_packet)
    )
    return [(filename, builder(case, event, decision))]


def _packet_header(
    heading: str, case: CaseRecord, event: CaseEvent, decision: EscalationDecision
) -> list[str]:
    return [
        f"# {heading}",
        "",
        f"- Case: `{case.case_id}`",
        f"- Agency: {case.agency}",
        f"- Source event: `{event.event_id}`",
        f"- Pathway: `{decision.pathway}`",
        f"- Draft type: `{decision.draft_type}`",
        f"- Pressure score: `{decision.pressure_score}`",
        "",
        "## Decision Rationale",
        "",
        decision.rationale,
        "",
    ]


def _ready_to_send_boundary() -> list[str]:
    return [
        "## Ready-To-Send Boundary",
        "",
        "This packet is a draft artifact only. It does not send email, post to a "
        "portal, pay an invoice, contact media, send legal notice, or close a case.",
        "",
    ]


def _generic_pathway_packet(
    case: CaseRecord, event: CaseEvent, decision: EscalationDecision
) -> str:
    return "\n".join(
        _packet_header("Pathway Packet", case, event, decision)
        + _ready_to_send_boundary()
    )


def _fee_audit_packet(case: CaseRecord, event: CaseEvent, decision: EscalationDecision) -> str:
    return "\n".join(
        _packet_header("Fee Audit", case, event, decision)
        + [
            "## Fee Questions",
            "",
            "- Does the estimate identify each task phase?",
            "- Does each line item identify personnel classification and hourly rate?",
            "- Do stated hours, rates, alternatives, and total reconcile?",
            "- Is any deposit clock tied to an unresolved estimate defect?",
            "",
        ]
        + _ready_to_send_boundary()
    )


def _unanswered_questions_packet(
    case: CaseRecord, event: CaseEvent, decision: EscalationDecision
) -> str:
    return "\n".join(
        _packet_header("Unanswered Questions Matrix", case, event, decision)
        + [
            "## Matrix",
            "",
            "| Question | Current Record | Needed Agency Position |",
            "| --- | --- | --- |",
            "| Search status | Pending from latest event | Confirm search scope and result |",
            "| Responsive records | Not established | Identify production/withholding status |",
            "| Next step | Unclear | State the concrete next action and timing |",
            "",
        ]
        + _ready_to_send_boundary()
    )


def _closure_timeline_packet(
    case: CaseRecord, event: CaseEvent, decision: EscalationDecision
) -> str:
    return "\n".join(
        _packet_header("Closure Timeline", case, event, decision)
        + [
            "## Timeline Questions",
            "",
            f"- Closure-triggering event: `{event.event_id}` at {event.received_at.isoformat()}",
            "- Requester position: not withdrawn or abandoned.",
            "- Needed agency action: keep the request open while the disputed issue is resolved.",
            "",
        ]
        + _ready_to_send_boundary()
    )


def _withholding_exemption_packet(
    case: CaseRecord, event: CaseEvent, decision: EscalationDecision
) -> str:
    return "\n".join(
        _packet_header("Withholding/Exemption Matrix", case, event, decision)
        + [
            "## Matrix",
            "",
            "| Record Or Redaction | Claimed Exemption | Specific Basis | Status |",
            "| --- | --- | --- | --- |",
            "| TBD | Not specified | Ask agency to identify statute and application | Open |",
            "",
        ]
        + _ready_to_send_boundary()
    )


def _duplicate_inflation_packet(
    case: CaseRecord, event: CaseEvent, decision: EscalationDecision
) -> str:
    return "\n".join(
        _packet_header("Duplicate Inflation Audit", case, event, decision)
        + [
            "## Count Audit",
            "",
            "- Requested count basis: unique responsive records after deduplication.",
            "- Agency count basis: unresolved from current event.",
            "- Needed support: explain duplicate handling and how volume affects any fee.",
            "",
        ]
        + _ready_to_send_boundary()
    )


def _attorney_faf_packet(
    case: CaseRecord, event: CaseEvent, decision: EscalationDecision
) -> str:
    return "\n".join(
        _packet_header("Attorney And FAF Packet", case, event, decision)
        + [
            "## Review Points",
            "",
            "- Formal notice/cure status.",
            "- Chronology of agency response and non-response.",
            "- Evidence references supporting unresolved records, fee, or closure issues.",
            "- Draft-only posture pending human legal review.",
            "",
        ]
        + _ready_to_send_boundary()
    )


def _commissioner_reporter_packet(
    case: CaseRecord, event: CaseEvent, decision: EscalationDecision
) -> str:
    return "\n".join(
        _packet_header("Commissioner And Reporter One-Pager", case, event, decision)
        + [
            "## One-Pager",
            "",
            "- Public-interest hook: unresolved from current event.",
            "- Agency action requested: provide records, position, or lawful withholding basis.",
            "- Evidence posture: cite packet indexes before any external outreach.",
            "",
            "## Ready-To-Send Boundary",
            "",
            "This one-pager is not media outreach. It remains a draft until a human "
            "explicitly approves any external use.",
            "",
        ]
    )


def _csv_cell(value: str) -> str:
    escaped = value.replace('"', '""')
    return f'"{escaped}"'
