from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from prr_pressure_cooker.ids import content_id, utc_now
from prr_pressure_cooker.models import (
    ApprovalInteractionRecord,
    AttachmentIndexRecord,
    CaseCommandRunRecord,
    CaseCommandRunStatus,
    CaseEvent,
    CaseExternalRefRecord,
    CaseRecord,
    CaseStateRecord,
    ContactIndexRecord,
    DeadlineRecord,
    DeadlineStatus,
    EscalationDecision,
    EvidenceRef,
    HumanApprovalTask,
    KanbanCard,
    MessageIndexRecord,
    PacketArtifactRecord,
    RouteAuditRecord,
    ThreadIndexRecord,
    WorkflowExecutionRecord,
    WorkflowExecutionStatus,
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

                CREATE TABLE IF NOT EXISTS case_external_refs (
                    normalized_ref TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    ref_type TEXT NOT NULL,
                    ref_value TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS case_states (
                    case_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    pending_task_id TEXT,
                    latest_event_id TEXT,
                    latest_event_summary TEXT,
                    pressure_score INTEGER NOT NULL,
                    active_deadline_count INTEGER NOT NULL,
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

                CREATE TABLE IF NOT EXISTS message_indexes (
                    message_index_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    evidence_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    message_id TEXT,
                    in_reply_to TEXT,
                    references_json TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    sender_name TEXT,
                    sender_address TEXT,
                    recipients_json TEXT NOT NULL,
                    cc_json TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    snippet TEXT NOT NULL,
                    attachment_evidence_ids_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS thread_indexes (
                    thread_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    message_count INTEGER NOT NULL,
                    first_event_id TEXT NOT NULL,
                    latest_event_id TEXT NOT NULL,
                    first_received_at TEXT NOT NULL,
                    latest_received_at TEXT NOT NULL,
                    participants_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS attachment_indexes (
                    attachment_index_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    parent_evidence_id TEXT NOT NULL,
                    evidence_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    stored_path TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS contact_indexes (
                    contact_index_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    name TEXT,
                    address TEXT NOT NULL,
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

                CREATE TABLE IF NOT EXISTS workflow_executions (
                    execution_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    workflow_name TEXT NOT NULL,
                    backend TEXT NOT NULL DEFAULT 'local',
                    status TEXT NOT NULL,
                    latest_event_id TEXT,
                    root_execution_id TEXT,
                    run_id TEXT,
                    remote_status TEXT,
                    data_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS case_command_runs (
                    command_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    command_type TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    workflow_execution_id TEXT,
                    status TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(command_type, idempotency_key)
                );

                CREATE TABLE IF NOT EXISTS deadlines (
                    deadline_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    source_event_id TEXT,
                    kind TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS route_audits (
                    audit_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    decision_id TEXT,
                    pathway TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    data_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS packet_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    decision_id TEXT NOT NULL,
                    pathway TEXT NOT NULL,
                    artifact_type TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS approval_interactions (
                    interaction_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    decision_id TEXT NOT NULL,
                    choice TEXT NOT NULL,
                    note TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )
            self._ensure_columns(
                conn,
                "workflow_executions",
                {
                    "backend": "TEXT NOT NULL DEFAULT 'local'",
                    "root_execution_id": "TEXT",
                    "run_id": "TEXT",
                    "remote_status": "TEXT",
                    "data_json": "TEXT NOT NULL DEFAULT '{}'",
                },
            )

    def _ensure_columns(
        self, conn: sqlite3.Connection, table: str, columns: dict[str, str]
    ) -> None:
        existing = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def save_message_index(self, record: MessageIndexRecord) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO message_indexes
                (message_index_id, case_id, event_id, evidence_id, thread_id, message_id,
                 in_reply_to, references_json, subject, sender_name, sender_address,
                 recipients_json, cc_json, received_at, snippet, attachment_evidence_ids_json,
                 created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.message_index_id,
                    record.case_id,
                    record.event_id,
                    record.evidence_id,
                    record.thread_id,
                    record.message_id,
                    record.in_reply_to,
                    _json(record.references),
                    record.subject,
                    record.sender_name,
                    record.sender_address,
                    _json(record.recipients),
                    _json(record.cc),
                    record.received_at.isoformat(),
                    record.snippet,
                    _json(record.attachment_evidence_ids),
                    record.created_at.isoformat(),
                ),
            )

    def list_message_indexes(self, case_id: str) -> list[MessageIndexRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM message_indexes
                WHERE case_id = ?
                ORDER BY received_at, event_id, message_index_id
                """,
                (case_id,),
            ).fetchall()
        return [
            MessageIndexRecord(
                message_index_id=row["message_index_id"],
                case_id=row["case_id"],
                event_id=row["event_id"],
                evidence_id=row["evidence_id"],
                thread_id=row["thread_id"],
                message_id=row["message_id"],
                in_reply_to=row["in_reply_to"],
                references=_loads(row["references_json"], []),
                subject=row["subject"],
                sender_name=row["sender_name"],
                sender_address=row["sender_address"],
                recipients=_loads(row["recipients_json"], []),
                cc=_loads(row["cc_json"], []),
                received_at=_dt(row["received_at"]),
                snippet=row["snippet"],
                attachment_evidence_ids=_loads(row["attachment_evidence_ids_json"], []),
                created_at=_dt(row["created_at"]),
            )
            for row in rows
        ]

    def save_thread_index(self, record: ThreadIndexRecord) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO thread_indexes
                (thread_id, case_id, subject, message_count, first_event_id, latest_event_id,
                 first_received_at, latest_received_at, participants_json, created_at,
                 updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.thread_id,
                    record.case_id,
                    record.subject,
                    record.message_count,
                    record.first_event_id,
                    record.latest_event_id,
                    record.first_received_at.isoformat(),
                    record.latest_received_at.isoformat(),
                    _json(record.participants),
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                ),
            )

    def list_thread_indexes(self, case_id: str) -> list[ThreadIndexRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM thread_indexes
                WHERE case_id = ?
                ORDER BY latest_received_at, thread_id
                """,
                (case_id,),
            ).fetchall()
        return [
            ThreadIndexRecord(
                thread_id=row["thread_id"],
                case_id=row["case_id"],
                subject=row["subject"],
                message_count=row["message_count"],
                first_event_id=row["first_event_id"],
                latest_event_id=row["latest_event_id"],
                first_received_at=_dt(row["first_received_at"]),
                latest_received_at=_dt(row["latest_received_at"]),
                participants=_loads(row["participants_json"], []),
                created_at=_dt(row["created_at"]),
                updated_at=_dt(row["updated_at"]),
            )
            for row in rows
        ]

    def save_attachment_index(self, record: AttachmentIndexRecord) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO attachment_indexes
                (attachment_index_id, case_id, event_id, parent_evidence_id, evidence_id,
                 filename, mime_type, size_bytes, sha256, stored_path, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.attachment_index_id,
                    record.case_id,
                    record.event_id,
                    record.parent_evidence_id,
                    record.evidence_id,
                    record.filename,
                    record.mime_type,
                    record.size_bytes,
                    record.sha256,
                    record.stored_path,
                    record.created_at.isoformat(),
                ),
            )

    def list_attachment_indexes(self, case_id: str) -> list[AttachmentIndexRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM attachment_indexes
                WHERE case_id = ?
                ORDER BY created_at, filename, attachment_index_id
                """,
                (case_id,),
            ).fetchall()
        return [
            AttachmentIndexRecord(
                attachment_index_id=row["attachment_index_id"],
                case_id=row["case_id"],
                event_id=row["event_id"],
                parent_evidence_id=row["parent_evidence_id"],
                evidence_id=row["evidence_id"],
                filename=row["filename"],
                mime_type=row["mime_type"],
                size_bytes=row["size_bytes"],
                sha256=row["sha256"],
                stored_path=row["stored_path"],
                created_at=_dt(row["created_at"]),
            )
            for row in rows
        ]

    def save_contact_index(self, record: ContactIndexRecord) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO contact_indexes
                (contact_index_id, case_id, event_id, role, name, address, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.contact_index_id,
                    record.case_id,
                    record.event_id,
                    record.role,
                    record.name,
                    record.address,
                    record.created_at.isoformat(),
                ),
            )

    def list_contact_indexes(self, case_id: str) -> list[ContactIndexRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM contact_indexes
                WHERE case_id = ?
                ORDER BY created_at, role, address, contact_index_id
                """,
                (case_id,),
            ).fetchall()
        return [
            ContactIndexRecord(
                contact_index_id=row["contact_index_id"],
                case_id=row["case_id"],
                event_id=row["event_id"],
                role=row["role"],
                name=row["name"],
                address=row["address"],
                created_at=_dt(row["created_at"]),
            )
            for row in rows
        ]

    def clear_casefile_indexes(self, case_id: str) -> int:
        tables = (
            "message_indexes",
            "thread_indexes",
            "attachment_indexes",
            "contact_indexes",
        )
        deleted = 0
        with self.connect() as conn:
            for table in tables:
                cursor = conn.execute(f"DELETE FROM {table} WHERE case_id = ?", (case_id,))
                deleted += cursor.rowcount
        return deleted

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
            conn.execute(
                """
                INSERT INTO case_states
                (case_id, status, pending_task_id, latest_event_id, latest_event_summary,
                 pressure_score, active_deadline_count, updated_at, data_json)
                VALUES (?, ?, NULL, NULL, NULL, 0, 0, ?, ?)
                ON CONFLICT(case_id) DO UPDATE SET
                  status=excluded.status,
                  updated_at=excluded.updated_at
                """,
                (
                    case.case_id,
                    case.status,
                    case.updated_at.isoformat(),
                    _json({}),
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

    def save_case_external_ref(self, record: CaseExternalRefRecord) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO case_external_refs
                (normalized_ref, case_id, ref_type, ref_value, source, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(normalized_ref) DO UPDATE SET
                  source=excluded.source,
                  updated_at=excluded.updated_at
                """,
                (
                    record.normalized_ref,
                    record.case_id,
                    record.ref_type,
                    record.ref_value,
                    record.source,
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                ),
            )

    def get_case_external_ref(self, normalized_ref: str) -> CaseExternalRefRecord | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM case_external_refs WHERE normalized_ref = ?",
                (normalized_ref,),
            ).fetchone()
        if row is None:
            return None
        return CaseExternalRefRecord(
            normalized_ref=row["normalized_ref"],
            case_id=row["case_id"],
            ref_type=row["ref_type"],
            ref_value=row["ref_value"],
            source=row["source"],
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    def find_case_by_external_refs(self, normalized_refs: Iterable[str]) -> str | None:
        refs = list(dict.fromkeys(normalized_refs))
        if not refs:
            return None
        placeholders = ",".join("?" for _ in refs)
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT case_id FROM case_external_refs
                WHERE normalized_ref IN ({placeholders})
                ORDER BY updated_at DESC, normalized_ref
                LIMIT 1
                """,
                refs,
            ).fetchone()
        return str(row["case_id"]) if row else None

    def list_case_external_refs(self, case_id: str) -> list[CaseExternalRefRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM case_external_refs
                WHERE case_id = ?
                ORDER BY ref_type, ref_value
                """,
                (case_id,),
            ).fetchall()
        return [
            CaseExternalRefRecord(
                normalized_ref=row["normalized_ref"],
                case_id=row["case_id"],
                ref_type=row["ref_type"],
                ref_value=row["ref_value"],
                source=row["source"],
                created_at=_dt(row["created_at"]),
                updated_at=_dt(row["updated_at"]),
            )
            for row in rows
        ]

    def save_case_state(self, record: CaseStateRecord) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO case_states
                (case_id, status, pending_task_id, latest_event_id, latest_event_summary,
                 pressure_score, active_deadline_count, updated_at, data_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(case_id) DO UPDATE SET
                  status=excluded.status,
                  pending_task_id=excluded.pending_task_id,
                  latest_event_id=excluded.latest_event_id,
                  latest_event_summary=excluded.latest_event_summary,
                  pressure_score=excluded.pressure_score,
                  active_deadline_count=excluded.active_deadline_count,
                  updated_at=excluded.updated_at,
                  data_json=excluded.data_json
                """,
                (
                    record.case_id,
                    record.status,
                    record.pending_task_id,
                    record.latest_event_id,
                    record.latest_event_summary,
                    record.pressure_score,
                    record.active_deadline_count,
                    record.updated_at.isoformat(),
                    _json(record.data),
                ),
            )

    def get_case_state(self, case_id: str) -> CaseStateRecord | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM case_states WHERE case_id = ?",
                (case_id,),
            ).fetchone()
        if row is None:
            return None
        return CaseStateRecord(
            case_id=row["case_id"],
            status=row["status"],
            pending_task_id=row["pending_task_id"],
            latest_event_id=row["latest_event_id"],
            latest_event_summary=row["latest_event_summary"],
            pressure_score=row["pressure_score"],
            active_deadline_count=row["active_deadline_count"],
            updated_at=_dt(row["updated_at"]),
            data=_loads(row["data_json"], {}),
        )

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

    def latest_event(self, case_id: str) -> CaseEvent | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT event_id FROM events
                WHERE case_id = ?
                ORDER BY received_at DESC, event_id DESC
                LIMIT 1
                """,
                (case_id,),
            ).fetchone()
        return self.get_event(case_id, row["event_id"]) if row else None

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

    def get_decision(self, decision_id: str) -> EscalationDecision:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM decisions WHERE decision_id = ?", (decision_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"decision not found: {decision_id}")
        return EscalationDecision(
            decision_id=row["decision_id"],
            case_id=row["case_id"],
            source_event_id=row["source_event_id"],
            pathway=row["pathway"],
            issue_tags=_loads(row["issue_tags_json"], []),
            pressure_score=row["pressure_score"],
            current_state=row["current_state"],
            recommended_next_state=row["recommended_next_state"],
            draft_type=row["draft_type"],
            human_approval_required=bool(row["human_approval_required"]),
            due_at=_dt(row["due_at"]) if row["due_at"] else None,
            evidence_refs=_loads(row["evidence_refs_json"], []),
            risk_level=row["risk_level"],
            rationale=row["rationale"],
            created_at=_dt(row["created_at"]),
        )

    def latest_decision(self, case_id: str) -> EscalationDecision | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT decision_id FROM decisions
                WHERE case_id = ?
                ORDER BY created_at DESC, decision_id DESC
                LIMIT 1
                """,
                (case_id,),
            ).fetchone()
        return self.get_decision(row["decision_id"]) if row else None

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

    def save_workflow_execution(self, record: WorkflowExecutionRecord) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO workflow_executions
                (execution_id, case_id, workflow_name, backend, status, latest_event_id,
                 root_execution_id, run_id, remote_status, data_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(execution_id) DO UPDATE SET
                  case_id=excluded.case_id,
                  workflow_name=excluded.workflow_name,
                  backend=excluded.backend,
                  status=excluded.status,
                  latest_event_id=excluded.latest_event_id,
                  root_execution_id=excluded.root_execution_id,
                  run_id=excluded.run_id,
                  remote_status=excluded.remote_status,
                  data_json=excluded.data_json,
                  updated_at=excluded.updated_at
                """,
                (
                    record.execution_id,
                    record.case_id,
                    record.workflow_name,
                    record.backend,
                    record.status,
                    record.latest_event_id,
                    record.root_execution_id,
                    record.run_id,
                    record.remote_status,
                    _json(record.data),
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                ),
            )

    def get_workflow_execution(self, execution_id: str) -> WorkflowExecutionRecord | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
        return self._workflow_execution_from_row(row) if row else None

    def list_workflow_executions(
        self,
        case_id: str | None = None,
        *,
        workflow_name: str | None = None,
        status: WorkflowExecutionStatus | str | None = None,
        backend: str | None = None,
    ) -> list[WorkflowExecutionRecord]:
        clauses = []
        params: list[Any] = []
        if case_id is not None:
            clauses.append("case_id = ?")
            params.append(case_id)
        if workflow_name is not None:
            clauses.append("workflow_name = ?")
            params.append(workflow_name)
        if status is not None:
            clauses.append("status = ?")
            params.append(str(status))
        if backend is not None:
            clauses.append("backend = ?")
            params.append(backend)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM workflow_executions
                {where}
                ORDER BY
                  CASE status WHEN 'active' THEN 0 WHEN 'started' THEN 1 ELSE 2 END,
                  CASE backend WHEN 'mistral' THEN 0 ELSE 1 END,
                  updated_at DESC,
                  execution_id DESC
                """,
                tuple(params),
            ).fetchall()
        return [self._workflow_execution_from_row(row) for row in rows]

    def get_workflow_execution_for_case(
        self,
        case_id: str,
        *,
        workflow_name: str | None = None,
        status: WorkflowExecutionStatus | str | None = None,
        backend: str | None = None,
    ) -> WorkflowExecutionRecord | None:
        records = self.list_workflow_executions(
            case_id,
            workflow_name=workflow_name,
            status=status,
            backend=backend,
        )
        return records[0] if records else None

    def get_active_workflow_execution_for_case(
        self,
        case_id: str,
        *,
        workflow_name: str | None = None,
        backend: str | None = None,
    ) -> WorkflowExecutionRecord | None:
        return self.get_workflow_execution_for_case(
            case_id,
            workflow_name=workflow_name,
            status=WorkflowExecutionStatus.ACTIVE,
            backend=backend,
        )

    def _workflow_execution_from_row(self, row: sqlite3.Row) -> WorkflowExecutionRecord:
        return WorkflowExecutionRecord(
            execution_id=row["execution_id"],
            case_id=row["case_id"],
            workflow_name=row["workflow_name"],
            backend=row["backend"],
            status=row["status"],
            latest_event_id=row["latest_event_id"],
            root_execution_id=row["root_execution_id"],
            run_id=row["run_id"],
            remote_status=row["remote_status"],
            data=_loads(row["data_json"], {}),
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    def begin_case_command_run(
        self,
        *,
        command_type: str,
        case_id: str,
        idempotency_key: str,
        input_data: dict[str, Any],
        workflow_execution_id: str | None = None,
    ) -> tuple[CaseCommandRunRecord, bool]:
        existing = self.get_case_command_run_by_idempotency(command_type, idempotency_key)
        if existing and existing.status == CaseCommandRunStatus.SUCCEEDED:
            return existing, False

        now = utc_now()
        if existing:
            updated = existing.model_copy(
                update={
                    "case_id": case_id,
                    "status": CaseCommandRunStatus.ACTIVE,
                    "input_data": input_data,
                    "workflow_execution_id": workflow_execution_id
                    or existing.workflow_execution_id,
                    "error": None,
                    "updated_at": now,
                }
            )
            self.save_case_command_run(updated)
            return updated, True

        record = CaseCommandRunRecord(
            command_id=content_id("cmd", command_type, idempotency_key),
            case_id=case_id,
            command_type=command_type,
            idempotency_key=idempotency_key,
            workflow_execution_id=workflow_execution_id,
            status=CaseCommandRunStatus.ACTIVE,
            input_data=input_data,
            created_at=now,
            updated_at=now,
        )
        self.save_case_command_run(record)
        return record, True

    def save_case_command_run(self, record: CaseCommandRunRecord) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO case_command_runs
                (command_id, case_id, command_type, idempotency_key, workflow_execution_id,
                 status, input_json, result_json, error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(command_id) DO UPDATE SET
                  case_id=excluded.case_id,
                  command_type=excluded.command_type,
                  idempotency_key=excluded.idempotency_key,
                  workflow_execution_id=excluded.workflow_execution_id,
                  status=excluded.status,
                  input_json=excluded.input_json,
                  result_json=excluded.result_json,
                  error=excluded.error,
                  updated_at=excluded.updated_at
                """,
                (
                    record.command_id,
                    record.case_id,
                    record.command_type,
                    record.idempotency_key,
                    record.workflow_execution_id,
                    record.status,
                    _json(record.input_data),
                    _json(record.result),
                    record.error,
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                ),
            )

    def get_case_command_run(self, command_id: str) -> CaseCommandRunRecord | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM case_command_runs WHERE command_id = ?",
                (command_id,),
            ).fetchone()
        return self._case_command_run_from_row(row) if row else None

    def get_case_command_run_by_idempotency(
        self, command_type: str, idempotency_key: str
    ) -> CaseCommandRunRecord | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM case_command_runs
                WHERE command_type = ? AND idempotency_key = ?
                """,
                (command_type, idempotency_key),
            ).fetchone()
        return self._case_command_run_from_row(row) if row else None

    def list_case_command_runs(
        self,
        case_id: str | None = None,
        *,
        command_type: str | None = None,
        status: CaseCommandRunStatus | str | None = None,
    ) -> list[CaseCommandRunRecord]:
        clauses = []
        params: list[Any] = []
        if case_id is not None:
            clauses.append("case_id = ?")
            params.append(case_id)
        if command_type is not None:
            clauses.append("command_type = ?")
            params.append(command_type)
        if status is not None:
            clauses.append("status = ?")
            params.append(str(status))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM case_command_runs
                {where}
                ORDER BY updated_at DESC, command_id DESC
                """,
                tuple(params),
            ).fetchall()
        return [self._case_command_run_from_row(row) for row in rows]

    def mark_case_command_run_succeeded(
        self, command_id: str, result: dict[str, Any]
    ) -> CaseCommandRunRecord:
        record = self.get_case_command_run(command_id)
        if record is None:
            raise KeyError(f"case command run not found: {command_id}")
        updated = record.model_copy(
            update={
                "status": CaseCommandRunStatus.SUCCEEDED,
                "result": result,
                "error": None,
                "updated_at": utc_now(),
            }
        )
        self.save_case_command_run(updated)
        return updated

    def mark_case_command_run_failed(
        self, command_id: str, error: str
    ) -> CaseCommandRunRecord:
        record = self.get_case_command_run(command_id)
        if record is None:
            raise KeyError(f"case command run not found: {command_id}")
        updated = record.model_copy(
            update={
                "status": CaseCommandRunStatus.FAILED,
                "error": error,
                "updated_at": utc_now(),
            }
        )
        self.save_case_command_run(updated)
        return updated

    def _case_command_run_from_row(self, row: sqlite3.Row) -> CaseCommandRunRecord:
        return CaseCommandRunRecord(
            command_id=row["command_id"],
            case_id=row["case_id"],
            command_type=row["command_type"],
            idempotency_key=row["idempotency_key"],
            workflow_execution_id=row["workflow_execution_id"],
            status=row["status"],
            input_data=_loads(row["input_json"], {}),
            result=_loads(row["result_json"], {}),
            error=row["error"],
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    def save_deadline(self, deadline: DeadlineRecord) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO deadlines
                (deadline_id, case_id, source_event_id, kind, due_at, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(deadline_id) DO UPDATE SET
                  status=excluded.status
                """,
                (
                    deadline.deadline_id,
                    deadline.case_id,
                    deadline.source_event_id,
                    deadline.kind,
                    deadline.due_at.isoformat(),
                    deadline.status,
                    deadline.created_at.isoformat(),
                ),
            )

    def list_deadlines(
        self, case_id: str | None = None, status: str | None = None
    ) -> list[DeadlineRecord]:
        conditions: list[str] = []
        params: list[str] = []
        if case_id:
            conditions.append("case_id = ?")
            params.append(case_id)
        if status:
            conditions.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM deadlines {where} ORDER BY due_at, deadline_id", params
            ).fetchall()
        return [
            DeadlineRecord(
                deadline_id=row["deadline_id"],
                case_id=row["case_id"],
                source_event_id=row["source_event_id"],
                kind=row["kind"],
                due_at=_dt(row["due_at"]),
                status=row["status"],
                created_at=_dt(row["created_at"]),
            )
            for row in rows
        ]

    def due_deadlines(self, now: datetime) -> list[DeadlineRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM deadlines
                WHERE status = ? AND due_at <= ?
                ORDER BY due_at, deadline_id
                """,
                (DeadlineStatus.OPEN.value, now.isoformat()),
            ).fetchall()
        return [
            DeadlineRecord(
                deadline_id=row["deadline_id"],
                case_id=row["case_id"],
                source_event_id=row["source_event_id"],
                kind=row["kind"],
                due_at=_dt(row["due_at"]),
                status=row["status"],
                created_at=_dt(row["created_at"]),
            )
            for row in rows
        ]

    def set_deadline_status(self, deadline_id: str, status: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE deadlines SET status = ? WHERE deadline_id = ?",
                (status, deadline_id),
            )

    def cancel_open_deadlines_for_case(self, case_id: str) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE deadlines
                SET status = ?
                WHERE case_id = ? AND status = ?
                """,
                (DeadlineStatus.CANCELED.value, case_id, DeadlineStatus.OPEN.value),
            )
            return cursor.rowcount

    def save_route_audit(self, audit: RouteAuditRecord) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO route_audits
                (audit_id, case_id, event_id, decision_id, pathway, status, created_at,
                 data_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit.audit_id,
                    audit.case_id,
                    audit.event_id,
                    audit.decision_id,
                    audit.pathway,
                    audit.status,
                    audit.created_at.isoformat(),
                    _json(audit.data),
                ),
            )

    def list_route_audits(self, case_id: str | None = None) -> list[RouteAuditRecord]:
        if case_id:
            query = "SELECT * FROM route_audits WHERE case_id = ? ORDER BY created_at"
            params = (case_id,)
        else:
            query = "SELECT * FROM route_audits ORDER BY created_at"
            params = ()
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            RouteAuditRecord(
                audit_id=row["audit_id"],
                case_id=row["case_id"],
                event_id=row["event_id"],
                decision_id=row["decision_id"],
                pathway=row["pathway"],
                status=row["status"],
                created_at=_dt(row["created_at"]),
                data=_loads(row["data_json"], {}),
            )
            for row in rows
        ]

    def save_packet_artifact(self, artifact: PacketArtifactRecord) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO packet_artifacts
                (artifact_id, case_id, decision_id, pathway, artifact_type, file_path,
                 created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.artifact_id,
                    artifact.case_id,
                    artifact.decision_id,
                    artifact.pathway,
                    artifact.artifact_type,
                    artifact.file_path,
                    artifact.created_at.isoformat(),
                ),
            )

    def list_packet_artifacts(self, case_id: str | None = None) -> list[PacketArtifactRecord]:
        if case_id:
            query = "SELECT * FROM packet_artifacts WHERE case_id = ? ORDER BY created_at"
            params = (case_id,)
        else:
            query = "SELECT * FROM packet_artifacts ORDER BY created_at"
            params = ()
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            PacketArtifactRecord(
                artifact_id=row["artifact_id"],
                case_id=row["case_id"],
                decision_id=row["decision_id"],
                pathway=row["pathway"],
                artifact_type=row["artifact_type"],
                file_path=row["file_path"],
                created_at=_dt(row["created_at"]),
            )
            for row in rows
        ]

    def save_approval_interaction(self, interaction: ApprovalInteractionRecord) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO approval_interactions
                (interaction_id, case_id, task_id, decision_id, choice, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    interaction.interaction_id,
                    interaction.case_id,
                    interaction.task_id,
                    interaction.decision_id,
                    interaction.choice,
                    interaction.note,
                    interaction.created_at.isoformat(),
                ),
            )

    def list_approval_interactions(
        self, case_id: str | None = None
    ) -> list[ApprovalInteractionRecord]:
        if case_id:
            query = "SELECT * FROM approval_interactions WHERE case_id = ? ORDER BY created_at"
            params = (case_id,)
        else:
            query = "SELECT * FROM approval_interactions ORDER BY created_at"
            params = ()
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            ApprovalInteractionRecord(
                interaction_id=row["interaction_id"],
                case_id=row["case_id"],
                task_id=row["task_id"],
                decision_id=row["decision_id"],
                choice=row["choice"],
                note=row["note"],
                created_at=_dt(row["created_at"]),
            )
            for row in rows
        ]
