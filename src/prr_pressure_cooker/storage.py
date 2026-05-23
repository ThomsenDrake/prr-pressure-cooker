from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from prr_pressure_cooker.ids import utc_now
from prr_pressure_cooker.models import (
    CaseEvent,
    CaseRecord,
    EscalationDecision,
    EvidenceRef,
    HumanApprovalTask,
    KanbanCard,
)


def _json(value: Any) -> str:
    if isinstance(value, BaseModel):
        return value.model_dump_json()
    return json.dumps(value, default=str)


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS cases (
                    case_id TEXT PRIMARY KEY,
                    agency TEXT NOT NULL,
                    request_title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    data_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS evidence_refs (
                    evidence_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    event_id TEXT,
                    original_path TEXT NOT NULL,
                    stored_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    content_text TEXT NOT NULL,
                    evidence_refs_json TEXT NOT NULL,
                    issue_tags_json TEXT NOT NULL,
                    classification_json TEXT
                );

                CREATE TABLE IF NOT EXISTS decisions (
                    decision_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    pathway TEXT NOT NULL,
                    issue_tags_json TEXT NOT NULL,
                    pressure_score INTEGER NOT NULL,
                    current_state TEXT NOT NULL,
                    recommended_next_state TEXT NOT NULL,
                    draft_type TEXT NOT NULL,
                    human_approval_required INTEGER NOT NULL,
                    due_at TEXT,
                    evidence_refs_json TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS approval_tasks (
                    task_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    decision_id TEXT NOT NULL,
                    pathway TEXT NOT NULL,
                    proposed_action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    draft_file TEXT NOT NULL,
                    evidence_packet_json TEXT NOT NULL,
                    choices_json TEXT NOT NULL,
                    required_human_note INTEGER NOT NULL,
                    human_note TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS kanban_cards (
                    card_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    lane TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    body TEXT NOT NULL,
                    due_at TEXT,
                    evidence_refs_json TEXT NOT NULL,
                    acceptance_criteria_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def upsert_case(self, case: CaseRecord) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO cases
                (case_id, agency, request_title, status, created_at, updated_at, data_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(case_id) DO UPDATE SET
                  agency=excluded.agency,
                  request_title=excluded.request_title,
                  status=excluded.status,
                  updated_at=excluded.updated_at,
                  data_json=excluded.data_json
                """,
                (
                    case.case_id,
                    case.agency,
                    case.request_title,
                    case.status,
                    case.created_at.isoformat(),
                    case.updated_at.isoformat(),
                    _json(case.data),
                ),
            )

    def create_case(self, case_id: str, agency: str, request_title: str) -> CaseRecord:
        now = utc_now()
        case = CaseRecord(
            case_id=case_id,
            agency=agency,
            request_title=request_title,
            created_at=now,
            updated_at=now,
        )
        self.upsert_case(case)
        return case

    def get_case(self, case_id: str) -> CaseRecord:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
        if row is None:
            raise KeyError(f"case not found: {case_id}")
        return CaseRecord(
            case_id=row["case_id"],
            agency=row["agency"],
            request_title=row["request_title"],
            status=row["status"],
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
            data=_loads(row["data_json"], {}),
        )

    def list_cases(self, prefix: str | None = None) -> list[CaseRecord]:
        with self.connect() as conn:
            if prefix is None:
                rows = conn.execute("SELECT case_id FROM cases ORDER BY case_id").fetchall()
            else:
                rows = conn.execute(
                    "SELECT case_id FROM cases WHERE case_id LIKE ? ORDER BY case_id",
                    (f"{prefix}%",),
                ).fetchall()
        return [self.get_case(row["case_id"]) for row in rows]

    def save_evidence_ref(self, ref: EvidenceRef) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO evidence_refs
                (evidence_id, case_id, event_id, original_path, stored_path, sha256,
                 mime_type, size_bytes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ref.evidence_id,
                    ref.case_id,
                    ref.event_id,
                    ref.original_path,
                    ref.stored_path,
                    ref.sha256,
                    ref.mime_type,
                    ref.size_bytes,
                    ref.created_at.isoformat(),
                ),
            )

    def get_evidence_refs(self, evidence_ids: Iterable[str]) -> list[EvidenceRef]:
        ids = list(evidence_ids)
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM evidence_refs WHERE evidence_id IN ({placeholders})", ids
            ).fetchall()
        return [
            EvidenceRef(
                evidence_id=row["evidence_id"],
                case_id=row["case_id"],
                event_id=row["event_id"],
                original_path=row["original_path"],
                stored_path=row["stored_path"],
                sha256=row["sha256"],
                mime_type=row["mime_type"],
                size_bytes=row["size_bytes"],
                created_at=_dt(row["created_at"]),
            )
            for row in rows
        ]

    def save_event(self, event: CaseEvent) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO events
                (event_id, case_id, source, event_type, received_at, summary, content_text,
                 evidence_refs_json, issue_tags_json, classification_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.case_id,
                    event.source,
                    event.event_type,
                    event.received_at.isoformat(),
                    event.summary,
                    event.content_text,
                    _json(event.evidence_refs),
                    _json(event.issue_tags),
                    event.classification.model_dump_json()
                    if event.classification is not None
                    else None,
                ),
            )

    def get_event(self, case_id: str, event_id: str) -> CaseEvent:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM events WHERE case_id = ? AND event_id = ?", (case_id, event_id)
            ).fetchone()
        if row is None:
            raise KeyError(f"event not found: {case_id}/{event_id}")
        return CaseEvent(
            event_id=row["event_id"],
            case_id=row["case_id"],
            source=row["source"],
            event_type=row["event_type"],
            received_at=_dt(row["received_at"]),
            summary=row["summary"],
            content_text=row["content_text"],
            evidence_refs=_loads(row["evidence_refs_json"], []),
            issue_tags=_loads(row["issue_tags_json"], []),
            classification=_loads(row["classification_json"], None),
        )

    def list_events(self, case_id: str) -> list[CaseEvent]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT event_id FROM events WHERE case_id = ? ORDER BY received_at", (case_id,)
            ).fetchall()
        return [self.get_event(case_id, row["event_id"]) for row in rows]

    def save_decision(self, decision: EscalationDecision) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO decisions
                (decision_id, case_id, source_event_id, pathway, issue_tags_json, pressure_score,
                 current_state, recommended_next_state, draft_type, human_approval_required,
                 due_at, evidence_refs_json, risk_level, rationale, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.decision_id,
                    decision.case_id,
                    decision.source_event_id,
                    decision.pathway,
                    _json(decision.issue_tags),
                    decision.pressure_score,
                    decision.current_state,
                    decision.recommended_next_state,
                    decision.draft_type,
                    int(decision.human_approval_required),
                    decision.due_at.isoformat() if decision.due_at else None,
                    _json(decision.evidence_refs),
                    decision.risk_level,
                    decision.rationale,
                    decision.created_at.isoformat(),
                ),
            )

    def save_task(self, task: HumanApprovalTask) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO approval_tasks
                (task_id, case_id, decision_id, pathway, proposed_action, status, draft_file,
                 evidence_packet_json, choices_json, required_human_note, human_note,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    task.case_id,
                    task.decision_id,
                    task.pathway,
                    task.proposed_action,
                    task.status,
                    task.draft_file,
                    _json(task.evidence_packet),
                    _json(task.choices),
                    int(task.required_human_note),
                    task.human_note,
                    task.created_at.isoformat(),
                    task.updated_at.isoformat(),
                ),
            )

    def get_task(self, task_id: str) -> HumanApprovalTask:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM approval_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"review task not found: {task_id}")
        return HumanApprovalTask(
            task_id=row["task_id"],
            case_id=row["case_id"],
            decision_id=row["decision_id"],
            pathway=row["pathway"],
            proposed_action=row["proposed_action"],
            status=row["status"],
            draft_file=row["draft_file"],
            evidence_packet=_loads(row["evidence_packet_json"], []),
            choices=_loads(row["choices_json"], []),
            required_human_note=bool(row["required_human_note"]),
            human_note=row["human_note"],
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    def list_tasks(
        self, status: str | None = None, case_id: str | None = None
    ) -> list[HumanApprovalTask]:
        conditions: list[str] = []
        params: list[str] = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if case_id:
            conditions.append("case_id = ?")
            params.append(case_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT task_id FROM approval_tasks {where} ORDER BY created_at", params
            ).fetchall()
        return [self.get_task(row["task_id"]) for row in rows]

    def cancel_pending_tasks_for_case(self, case_id: str, note: str) -> int:
        now = utc_now().isoformat()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE approval_tasks
                SET status = ?, human_note = ?, updated_at = ?
                WHERE case_id = ? AND status = ?
                """,
                ("canceled", note, now, case_id, "pending"),
            )
            return cursor.rowcount

    def save_card(self, card: KanbanCard) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO kanban_cards
                (card_id, case_id, task_id, title, lane, priority, body, due_at,
                 evidence_refs_json, acceptance_criteria_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    card.card_id,
                    card.case_id,
                    card.task_id,
                    card.title,
                    card.lane,
                    card.priority,
                    card.body,
                    card.due_at.isoformat() if card.due_at else None,
                    _json(card.evidence_refs),
                    _json(card.acceptance_criteria),
                    card.created_at.isoformat(),
                    card.updated_at.isoformat(),
                ),
            )

    def delete_cards_for_case(self, case_id: str) -> int:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM kanban_cards WHERE case_id = ?", (case_id,))
            return cursor.rowcount
