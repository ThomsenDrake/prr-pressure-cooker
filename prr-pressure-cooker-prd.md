Yes. Treat escalation pathways as first-class Mistral Workflows, not just draft-generation steps.

The clean architecture is:

Hermes Kanban = visible pressure board
Mistral Workflows = durable escalation state machine
Proton Bridge + himalaya = evidence/event ingestion
Human approval = mandatory gate before anything leaves your system

Mistral Workflows is a good fit because it is designed for long-running, multi-step workflows involving LLM calls, tool use, external APIs, and human input; it can pause for human approvals or external events and resume later. It is currently in public preview, so I would keep your integration adapter-based rather than locking yourself into brittle assumptions.  ￼

Escalation architecture

flowchart TB
  subgraph Inputs["Evidence + Status Inputs"]
    Mail["Proton Bridge + himalaya\n.eml exports, attachments"]
    Portal["Agency portal events\nstatus changes, messages, invoices"]
    Manual["Manual notes\ncalls, screenshots, PDFs"]
    Public["Public contradiction sources\nagency pages, news, statutes"]
  end
  subgraph Mistral["Mistral Workflows: Escalation Orchestration"]
    CaseWF["case_lifecycle_workflow\none long-running execution per PRR"]
    Router["escalation_router_workflow"]
    Timer["deadline_timer_workflow\nbusiness days, closure windows, notice clocks"]
    Score["pressure_score_workflow"]
    HumanGate["human_approval_workflow\napprove / revise / defer / cancel"]
  end
  subgraph Pathways["Escalation Pathway Child Workflows"]
    Silence["silence_delay_pathway"]
    FeeOpacity["fee_opacity_pathway"]
    Defective["defective_estimate_pathway"]
    Dodge["custodian_dodge_pathway"]
    Closure["closure_threat_pathway"]
    Exemption["exemption_vagueness_pathway"]
    Duplicate["duplicate_inflation_pathway"]
    PublicPressure["public_pressure_pathway"]
    Counsel["counsel_or_mediation_pathway"]
  end
  subgraph Activities["Activities: Side-Effect / Tool Layer"]
    Parse["parse_eml / parse_portal_message"]
    Classify["classify_agency_response"]
    Audit["fee_math_and_scope_audit"]
    Draft["draft_next_action"]
    Packet["build_escalation_packet"]
    Hermes["upsert_hermes_kanban_card"]
    Notify["notify_human_reviewer"]
  end
  subgraph Stores["State + Evidence"]
    Raw["raw_evidence_store\n.eml, PDFs, attachments, hashes"]
    DB["case_db\ncases, events, deadlines, scores"]
    Vector["semantic index\nthreads, filings, prior patterns"]
    Packets["packet_store\nFAF / attorney / reporter / legislator"]
  end
  subgraph Board["Hermes Kanban"]
    Intake["Intake"]
    Waiting["Waiting for Agency"]
    Nonanswer["Agency Nonanswer"]
    Supervisor["Supervisor Escalation"]
    Notice["119.12 Notice Ready/Sent"]
    External["External Packet Ready"]
    Review["Human Review"]
    Closed["Resolved / Closed"]
  end
  Mail --> Parse
  Portal --> Parse
  Manual --> Parse
  Public --> Classify
  Parse --> Raw
  Parse --> CaseWF
  CaseWF --> Router
  Router --> Classify
  Classify --> Score
  Score --> Timer
  Router --> Silence
  Router --> FeeOpacity
  Router --> Defective
  Router --> Dodge
  Router --> Closure
  Router --> Exemption
  Router --> Duplicate
  Router --> PublicPressure
  Router --> Counsel
  Silence --> Draft
  FeeOpacity --> Audit
  Defective --> Audit
  Dodge --> Draft
  Closure --> Draft
  Exemption --> Draft
  Duplicate --> Draft
  PublicPressure --> Packet
  Counsel --> Packet
  Audit --> Draft
  Draft --> HumanGate
  Packet --> HumanGate
  HumanGate --> Hermes
  HumanGate --> DB
  Hermes --> Board
  DB --> Vector
  Raw --> Vector
  Packet --> Packets

The important design rule: workflows decide the escalation path; activities do the external work. Mistral’s docs distinguish workflows as deterministic orchestration logic and activities as the units that touch the outside world, such as LLM calls, HTTP requests, database writes, file reads, and tool invocations.  ￼

How Hermes and Mistral should divide responsibility

Do not let Hermes and Mistral both “own” the same decision. That will create split-brain escalation logic.

I’d split it like this:

Layer	Owns	Does not own
Mistral Workflows	Case state, escalation pathway, timers, approvals, scoring, next-action logic	Visual task layout
Hermes Kanban	Visible task board, assignee lanes, reviewer queue, human-facing status	Legal/escalation decision logic
Case DB	Durable facts, evidence refs, deadlines, scores, event history	UI workflow
Raw evidence store	Immutable .eml, PDFs, attachments, hashes	Summaries as source of truth

Hermes Kanban is still valuable because it is a durable task board shared across Hermes profiles, with tasks and handoffs stored in SQLite; its worker-lane docs also frame Kanban as owning task lifecycle and audit trail for worker tasks.  ￼  ￼ For your setup, that means Hermes should own task cards, while Mistral owns the PRR case workflow.

Escalation pathway model

Each PRR case should start one long-running workflow execution:

case_lifecycle_workflow(case_id)

That workflow should listen for signals:

signals:
  - agency_message_received
  - portal_status_changed
  - invoice_received
  - fee_estimate_received
  - records_produced
  - closure_warning_received
  - human_sent_message
  - human_sent_11912_notice
  - human_paid_invoice
  - commissioner_reply_received
  - faf_reply_received
  - attorney_reply_received
  - reporter_reply_received
  - deadline_elapsed
  - human_marked_resolved

Mistral Workflows fits that because executions have event histories, workflows can wait for signals or external events, and the platform can reconstruct state after worker restarts.  ￼

The escalation pathways

1. Silence / delay pathway

Trigger:

issue_tags:
  - silence_delay
conditions:
  - no_ack_after_days >= configured_threshold
  - no_substantive_update_after_days >= configured_threshold

Workflow:

stateDiagram-v2
  [*] --> Waiting
  Waiting --> StatusNudgeDrafted: deadline elapsed
  StatusNudgeDrafted --> HumanApproval
  HumanApproval --> SentStatusNudge: approved
  SentStatusNudge --> WaitingForAgency
  WaitingForAgency --> SupervisorEscalationReady: no response
  SupervisorEscalationReady --> HumanApproval
  HumanApproval --> Closed: agency responds or human closes

Outputs:

status_nudge.md
supervisor_status_escalation.md
kanban card: "Agency stale — approve status nudge"

2. Fee opacity pathway

This is the Seminole / Osceola pattern: the estimate may not be arithmetically wrong, but it is too opaque to evaluate. Your Osceola example is exactly this: the invoice math checked out, but the estimate was still opaque because the three generic labor lines did not identify phases, tasks, or personnel classifications.  ￼

Trigger:

issue_tags:
  - fee_opacity
  - deposit_condition
  - task_basis_unanswered

Workflow:

stateDiagram-v2
  [*] --> EstimateReceived
  EstimateReceived --> AuditEstimate
  AuditEstimate --> ParticularizedEstimateDraft
  ParticularizedEstimateDraft --> HumanApproval
  HumanApproval --> SentClarification
  SentClarification --> WaitForAgency
  WaitForAgency --> SupervisorEscalation: repeated nonanswer
  SupervisorEscalation --> NoticeReady: payment conditioned on unexplained charge
  NoticeReady --> PacketReady: human approves notice or external packet

Outputs:

fee_audit.md
unanswered_questions_matrix.md
particularized_estimate_request.md
supervisor_escalation.md
11912_notice_draft.md

3. Defective estimate pathway

This is the Pinellas pattern. The system should automatically detect arithmetic defects, stacked alternatives, and payment clocks tied to unusable estimates. Your PCSO notes already identify the escalation ladder: supervisor review first, then a formal custodian notice if the agency still sends “mush.”  ￼

Trigger:

issue_tags:
  - defective_estimate
conditions:
  - hours_times_rate_mismatch == true
  - alternatives_appear_cumulative == true
  - total_does_not_reconcile == true
  - repeat_defect_count >= 1

Workflow:

flowchart TD
  A["Invoice received"] --> B["fee_math_and_scope_audit"]
  B --> C{"Defect type"}
  C --> D["Arithmetic mismatch"]
  C --> E["Stacked alternatives"]
  C --> F["Unestimated remainder"]
  C --> G["Payment clock tied to defective quote"]
  D --> H["Draft corrected-estimate demand"]
  E --> H
  F --> H
  G --> H
  H --> I["Human approval"]
  I --> J["Send / portal-post manually"]
  J --> K["Wait for agency response"]
  K --> L{"Cured?"}
  L -->|yes| M["Update case, lower score"]
  L -->|no| N["Supervisor/custodian escalation"]
  N --> O["119.12 notice ready"]

Outputs:

estimate_defect_report.md
corrected_estimate_request.md
supervisor_review_packet.md
payment_clock_objection.md

4. Custodian dodge pathway

This is the VSO pattern: the agency points you elsewhere instead of answering whether it has its own records. Your existing VSO response strategy was to force the agency to say whether it conducted a reasonable search, whether no VSO-held records exist, whether records are exempt, or whether records can be produced.  ￼

Trigger:

issue_tags:
  - custodian_dodge
  - answer_wrong_request
conditions:
  - agency_refers_to_other_agency == true
  - request_sought_agency_held_records == true

Workflow:

stateDiagram-v2
  [*] --> DodgeDetected
  DodgeDetected --> ForcedPositionDraft
  ForcedPositionDraft --> HumanApproval
  HumanApproval --> SentForcedPosition
  SentForcedPosition --> WaitFiveBusinessDays
  WaitFiveBusinessDays --> CustodianNoticeReady: no clear position
  CustodianNoticeReady --> ExternalPacketReady

Outputs:

forced_agency_position_letter.md
custodian_notice.md
contradiction_file.md

This pathway should also check public statements and prior records from related agencies. But it should frame them carefully: public statements do not prove a specific record exists; they justify asking the agency to clarify whether it is asserting that no agency-held records exist after a reasonable search.

5. Closure threat pathway

This needs its own fast lane. Closure threats are time-sensitive and should override slower pathways.

Trigger:

issue_tags:
  - closure_threat
conditions:
  - agency_says_request_will_close == true
  - clarification_or_estimate_dispute_pending == true

Workflow:

flowchart LR
  A["Closure threat detected"] --> B["Immediate preservation reply draft"]
  B --> C["Human approval"]
  C --> D["Portal/email manual send"]
  D --> E["Deadline timer"]
  E --> F["Escalate if closure occurs"]

Output:

no_withdrawal_preservation_reply.md
closure_deadline_event
kanban card: "URGENT: closure threat — approve preservation reply"

This is especially useful for agencies like Osceola Corrections, where the problem was not arithmetic but a deposit/closure clock attached to a generic estimate.  ￼

6. Public-pressure pathway

This should not be a panic button. It should activate only when the paper trail is clean.

Trigger:

conditions:
  - formal_notice_sent == true OR repeated_nonanswer_count >= 2
  - public_interest_score >= 1
  - evidence_bundle_ready == true
  - human_approval_required == true

Workflow:

stateDiagram-v2
  [*] --> PacketPrecheck
  PacketPrecheck --> BuildOnePager
  BuildOnePager --> BuildRecipientVariants
  BuildRecipientVariants --> HumanApproval
  HumanApproval --> CommissionerPacketReady
  HumanApproval --> LegislatorPacketReady
  HumanApproval --> FAFPacketReady
  HumanApproval --> ReporterPacketReady
  CommissionerPacketReady --> WaitForReplies
  LegislatorPacketReady --> WaitForReplies
  FAFPacketReady --> WaitForReplies
  ReporterPacketReady --> WaitForReplies
  WaitForReplies --> UpdateCase

Your Seminole sequence shows why this pathway matters: after formal notice and repeated nonanswers, commissioner outreach became a measured oversight step rather than a “mass complaint grenade,” and the recommended framing was to ask whether the commissioner was concerned that access was being conditioned on unexplained special-service charges.  ￼

Outputs:

commissioner_packet.md
legislator_packet.md
faf_packet.md
attorney_intake_packet.md
reporter_pitch.md
one_page_timeline.md

7. Counsel / mediation pathway

This pathway should start before you are ready to sue. It is a preparedness workflow.

Trigger:

conditions:
  - 11912_notice_sent == true
  - cure_window_elapsed == true
  - agency_nonanswer_after_notice == true

Workflow:

flowchart TD
  A["Notice sent"] --> B["Start cure timer"]
  B --> C{"Agency cured?"}
  C -->|yes| D["Update case, downgrade pressure"]
  C -->|partial| E["Build partial-cure memo"]
  C -->|no| F["Build counsel/FAF packet"]
  F --> G["Human approval"]
  G --> H["Attorney outreach draft"]
  G --> I["FAF/Brechner draft"]
  G --> J["AG mediation packet"]
  H --> K["Track replies"]
  I --> K
  J --> K

Outputs:

11912_notice_clock.md
post_notice_status.md
attorney_intake_summary.md
faf_referral_email.md
ag_mediation_packet.md

This matches your existing escalation posture: your handoff summary documented formal notice, state/commissioner outreach, and the fact that Herr and Lockhart’s offices asked for the thread, invoices, and timeline; Bobby Block later shared the matter with FAF legal staff.  ￼  ￼

Core workflow schema

Use a common escalation decision object so every pathway writes the same type of record.

EscalationDecision:
  decision_id: "dec_2026_04_21_pcso_defective_estimate"
  case_id: "pcso_ice_287g_2026"
  source_event_id: "evt_2026_04_21_agency_quote"
  pathway: "defective_estimate"
  issue_tags:
    - defective_estimate
    - stacked_alternatives
    - payment_clock
    - no_usable_breakdown
  pressure_score: 12
  current_state: "FEE_ESTIMATE_RECEIVED"
  recommended_next_state: "SUPERVISOR_ESCALATION_READY"
  draft_type: "supervisor_review_request"
  human_approval_required: true
  due_at: "2026-04-26T17:00:00-04:00"
  evidence_refs:
    - "raw/2026-04-21_pcso_quote.eml"
    - "attachments/pcso_quote_2026-04-21.pdf"
    - "audits/fee_math_audit_2026-04-21.md"
  risk_level: "medium"
  rationale: >
    Agency issued a revised estimate that appears to combine alternatives
    and does not reconcile hours, rates, and totals.

Rules engine

The workflow should not rely on vibes. Give it deterministic escalation rules.

rules:
  - id: silence_after_ack
    when:
      all:
        - case.status in ["WAITING_FOR_AGENCY", "WAITING_FOR_STATUS"]
        - business_days_since_last_substantive_response >= 10
    action:
      pathway: silence_delay
      draft: status_nudge
      pressure_delta: 2
  - id: opaque_fee_with_deposit
    when:
      all:
        - event.type == "fee_estimate_received"
        - estimate.deposit_required == true
        - estimate.task_breakdown_missing == true
    action:
      pathway: fee_opacity
      draft: particularized_estimate_request
      pressure_delta: 5
  - id: repeated_defective_estimate
    when:
      all:
        - estimate.math_defect == true
        - case.defective_estimate_count >= 2
    action:
      pathway: defective_estimate
      draft: supervisor_review_request
      pressure_delta: 8
  - id: closure_while_clarification_pending
    when:
      all:
        - event.contains_closure_warning == true
        - case.unanswered_questions_count > 0
    action:
      pathway: closure_threat
      draft: no_withdrawal_preservation_reply
      priority: urgent
      pressure_delta: 6
  - id: custodian_dodge
    when:
      all:
        - event.refers_requester_to_other_agency == true
        - request.scope == "agency_held_records"
    action:
      pathway: custodian_dodge
      draft: forced_agency_position_letter
      pressure_delta: 5
  - id: post_notice_no_cure
    when:
      all:
        - case.notice_11912_sent == true
        - business_days_since_notice >= 5
        - case.cure_status in ["none", "partial_nonresponsive"]
    action:
      pathway: counsel_or_mediation
      draft: attorney_faf_packet
      pressure_delta: 10

Mistral workflow pseudocode

import mistralai.workflows as workflows
from pydantic import BaseModel
from typing import list, Optional
class CaseEventInput(BaseModel):
    case_id: str
    event_id: str
    source: str  # himalaya, portal, manual, calendar_tick
    received_at: str
@workflows.workflow.define(name="prr_escalation_router")
class PRREscalationRouter:
    @workflows.workflow.entrypoint
    async def run(self, params: CaseEventInput) -> dict:
        # Activities touch external systems. Keep workflow logic deterministic.
        event = await parse_event_activity(params.event_id)
        case = await load_case_activity(params.case_id)
        classification = await classify_response_activity(event, case)
        audit = None
        if classification.contains_fee_estimate:
            audit = await fee_math_and_scope_audit_activity(event, case)
        decision = await compute_escalation_decision_activity(
            case=case,
            event=event,
            classification=classification,
            audit=audit,
        )
        await update_case_state_activity(case.case_id, decision)
        await upsert_hermes_card_activity(case.case_id, decision)
        if decision.human_approval_required:
            draft = await draft_next_action_activity(case, event, decision)
            await create_human_review_task_activity(case.case_id, decision, draft)
            return {
                "status": "waiting_for_human_review",
                "case_id": case.case_id,
                "pathway": decision.pathway,
                "draft_type": decision.draft_type,
            }
        return {
            "status": "updated_no_action_required",
            "case_id": case.case_id,
            "pathway": decision.pathway,
        }

The worker model matters here: Mistral’s docs say workers run your workflow/activity code, while the Workflows API orchestrates executions and event history; workers connect outbound, so this can sit around your local Proton Bridge / himalaya setup without exposing your mailbox through an inbound cloud service.  ￼

Human approval workflow

Every sensitive escalation should become a structured approval form:

HumanApprovalTask:
  case_id: "seminole_scout_2026"
  pathway: "public_pressure"
  proposed_action: "send_commissioner_packet"
  draft_file: "drafts/commissioner_herr_2026-03-25.md"
  evidence_packet:
    - "01_timeline.pdf"
    - "02_email_thread.pdf"
    - "03_invoices.pdf"
    - "04_11912_notice.pdf"
  choices:
    - approve
    - approve_with_edits
    - defer
    - cancel
  required_human_note: true

Mistral’s conversational workflow docs explicitly support user interaction during execution, including structured forms/confirmations, progress tracking, canvas, and tool UI, which is exactly the approval surface you want for legal-adjacent escalation.  ￼

Kanban cards created by escalation workflows

Use Hermes Kanban as the cockpit. The workflow should create or update cards like:

title: "PCSO P172615 — approve supervisor escalation re defective quote"
lane: "Human Review"
assignee: "Drake"
priority: "high"
case_id: "pcso_ice_287g_2026"
pathway: "defective_estimate"
due_at: "2026-04-26T17:00:00-04:00"
body: |
  PCSO appears to have issued a second defective estimate.
  Proposed action: send supervisory review request to Jennifer Crockett,
  cc Christopher Sahagian and Shannon Lockheart.
acceptance_criteria:
  - draft reviewed
  - evidence refs verified
  - no unsupported legal claims
  - user approves before sending
evidence_refs:
  - audits/pcso_fee_audit_2026-04-21.md
  - raw/pcso_april_21_quote.eml
  - drafts/pcso_supervisor_review.md

Hermes’ Kanban docs say dashboard, CLI, and worker tools all route through the same board database, which makes it a solid visible coordination layer for review and task handoffs.  ￼

Escalation pathway table

Pathway	Trigger	First action	Escalation action	Packet
Silence / delay	No status after threshold	Status nudge	Supervisor/custodian follow-up	Timeline + request copy
Fee opacity	Generic fee lines, no task basis	Particularized estimate request	§119.12 notice draft	Fee audit + unanswered questions
Defective estimate	Bad math, stacked alternatives	Corrected estimate demand	Supervisor/custodian review	Estimate audit
Custodian dodge	“Ask another agency”	Forced agency-position letter	Custodian notice	Contradiction file
Closure threat	Pay/respond or request closes	No-withdrawal preservation reply	Closure challenge	Closure timeline
Exemption vagueness	Withholding without statute	Exemption-basis demand	Custodian notice	Withholding matrix
Duplicate inflation	Huge email count, no dedup answer	Unique-message clarification	Narrowed estimate request	Search-count audit
Public pressure	Repeated nonanswer + clean packet	Commissioner/legislator draft	Reporter/FAF packet	One-page public summary
Counsel / mediation	Notice window elapsed, no cure	Attorney/FAF intake	AG mediation packet	Lawyer-ready bundle

Evidence packet generation should be part of escalation

Every pathway should maintain a packet skeleton automatically:

casefile/
  raw/
    *.eml
    attachments/
  indexes/
    messages.csv
    threads.csv
    attachments.csv
    contacts.csv
  audits/
    fee_math_audit.md
    unanswered_questions_matrix.md
    deadline_log.md
  drafts/
    next_action.md
    supervisor_escalation.md
    11912_notice.md
  packets/
    attorney/
      00_case_summary.md
      01_timeline.md
      02_key_emails/
      03_invoices/
      04_notice/
    faf/
    reporter/
    commissioner/

This is consistent with the casefile approach you already outlined: export raw .eml files, preserve headers, save attachments, build message/thread/attachment indexes, create a timeline, and produce lawyer-ready evidence bundles.  ￼

Guardrails

Hard-code these rules:

never_auto_send:
  - emails
  - portal replies
  - 119.12 notices
  - attorney emails
  - FAF/Brechner emails
  - reporter pitches
  - commissioner/legislator outreach
  - mediation submissions
always_require_human_approval:
  - legal notice language
  - accusations of illegality
  - public/media escalation
  - final case closure
  - payment decisions
  - abandonment/withdrawal language
evidence_rules:
  - raw evidence is immutable
  - summaries must link to source files
  - ambiguous messages go to review queue
  - no redaction of originals
  - redacted copies live in separate folder

The MVP build order

Build the escalation workflows in this order:

1. Escalation router
    Classifies new agency events and applies issue tags.
2. Deadline timer
    Tracks business-day windows, closure threats, and formal notice clocks.
3. Fee audit pathway
    Handles Seminole, PCSO, and Osceola-style estimates.
4. Custodian dodge pathway
    Handles Volusia-style “ask another agency” replies.
5. Human approval workflow
    Drafts messages and blocks until you approve, revise, defer, or cancel.
6. Hermes Kanban adapter
    Upserts cards for every pending human decision.
7. Packet builder
    Creates attorney / FAF / reporter / commissioner bundles.

The most useful mental model is:

Mistral Workflows should not merely draft escalation messages. It should remember where each PRR is in the pressure ladder, wait for the next agency event or deadline, select the next pathway, prepare the packet, and block at human approval.

That gives you scalable pressure without creating an autonomous legal missile launcher.
