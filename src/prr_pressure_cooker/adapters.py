from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from typing import Protocol

from prr_pressure_cooker.ids import content_id, utc_now
from prr_pressure_cooker.models import EscalationDecision, HumanApprovalTask, KanbanCard
from prr_pressure_cooker.storage import Store


class KanbanAdapter(Protocol):
    def upsert_card(self, decision: EscalationDecision, task: HumanApprovalTask) -> KanbanCard:
        ...


class MailIngestAdapter(Protocol):
    def fetch_new_events(self, case_id: str) -> list[str]:
        ...


class LocalKanbanAdapter:
    def __init__(self, store: Store):
        self.store = store

    def upsert_card(self, decision: EscalationDecision, task: HumanApprovalTask) -> KanbanCard:
        now = utc_now()
        card = KanbanCard(
            card_id=content_id("card", task.task_id, decision.pathway),
            case_id=decision.case_id,
            task_id=task.task_id,
            title=f"{decision.case_id} - approve {decision.draft_type.replace('_', ' ')}",
            lane="Human Review",
            priority=_priority(decision.pressure_score),
            body=decision.rationale,
            due_at=decision.due_at,
            evidence_refs=decision.evidence_refs,
            acceptance_criteria=[
                "draft reviewed",
                "evidence refs verified",
                "no unsupported legal claims",
                "human approves before sending",
            ],
            created_at=now,
            updated_at=now,
        )
        self.store.save_card(card)
        return card


class TailnetHimalayaAdapter:
    def __init__(self, base_url: str | None):
        self.base_url = base_url

    def fetch_new_events(self, case_id: str) -> list[str]:
        if not self.base_url:
            raise RuntimeError(
                "PROTON_HIMALAYA_BASE_URL is not configured; "
                "import-folder ingestion remains available"
            )
        raise NotImplementedError(
            f"tailnet Proton/himalaya adapter is configured for {self.base_url}, "
            f"but live fetch is not implemented in the MVP scaffold for {case_id}"
        )


class SSHHimalayaAdapter:
    def __init__(
        self,
        ssh_target: str,
        folder: str = "Folders/Public Records Requests",
        account: str | None = None,
    ):
        self.ssh_target = ssh_target
        self.folder = folder
        self.account = account

    def list_envelopes(self, query: str = "order by date desc", limit: int = 10) -> list[dict]:
        account_args = self._account_args()
        command = (
            f"himalaya -o json envelope list {account_args} "
            f"-f {shlex.quote(self.folder)} -s {int(limit)} {shlex.quote(query)}"
        )
        completed = self._ssh(command, text=True)
        return _parse_json_stdout(completed.stdout)

    def export_message(self, message_id: str, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        account_args = self._account_args()
        command = " ".join(
            [
                "tmp=$(mktemp /tmp/prr-himalaya-XXXXXX.eml);",
                "trap 'rm -f \"$tmp\"' EXIT;",
                "himalaya message export",
                account_args,
                f"-f {shlex.quote(self.folder)}",
                "-F",
                '-d "$tmp"',
                shlex.quote(message_id),
                ">/dev/null;",
                'cat "$tmp"',
            ]
        )
        completed = self._ssh(command, text=False)
        destination.write_bytes(completed.stdout)
        return destination

    def _account_args(self) -> str:
        return f"-a {shlex.quote(self.account)}" if self.account else ""

    def _ssh(self, remote_command: str, text: bool) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                self.ssh_target,
                remote_command,
            ],
            check=True,
            capture_output=True,
            text=text,
        )


class OutboundActionBlocked(RuntimeError):
    pass


def block_outbound(action: str) -> None:
    raise OutboundActionBlocked(
        f"blocked outbound action '{action}': human approval and manual send are required"
    )


def _priority(score: int) -> str:
    if score >= 8:
        return "high"
    if score >= 5:
        return "medium"
    return "low"


def _parse_json_stdout(stdout: str) -> list[dict]:
    start = stdout.find("[")
    if start < 0:
        start = stdout.find("{")
    if start < 0:
        raise ValueError(f"no JSON object found in Himalaya output: {stdout[:200]}")
    parsed = json.loads(stdout[start:])
    if isinstance(parsed, list):
        return parsed
    return [parsed]
