from __future__ import annotations

import json
import mimetypes
import shutil
from email import policy
from email.headerregistry import AddressHeader
from email.parser import BytesParser
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import yaml

from prr_pressure_cooker.config import Settings
from prr_pressure_cooker.ids import content_id, file_sha256, slugify, utc_now
from prr_pressure_cooker.models import CaseEvent, EventSource, EvidenceRef
from prr_pressure_cooker.storage import Store

SUPPORTED_IMPORT_SUFFIXES = {".eml", ".pdf", ".txt", ".md", ".json", ".yaml", ".yml"}


def import_path(
    case_id: str, source_path: Path, store: Store, settings: Settings
) -> list[CaseEvent]:
    if source_path.is_dir():
        events: list[CaseEvent] = []
        for child in sorted(p for p in source_path.rglob("*") if p.is_file()):
            if child.suffix.lower() in SUPPORTED_IMPORT_SUFFIXES:
                events.extend(import_path(case_id, child, store, settings))
        return events

    if not source_path.exists():
        raise FileNotFoundError(source_path)
    if source_path.suffix.lower() not in SUPPORTED_IMPORT_SUFFIXES:
        raise ValueError(f"unsupported import file type: {source_path.suffix}")

    store.get_case(case_id)
    event, evidence_ref = _build_event(case_id, source_path, settings)
    store.save_evidence_ref(evidence_ref)
    store.save_event(event)
    return [event]


def _build_event(case_id: str, path: Path, settings: Settings) -> tuple[CaseEvent, EvidenceRef]:
    sha = file_sha256(path)
    event_id = content_id("evt", case_id, sha, str(path.resolve()))
    evidence_id = content_id("evi", case_id, sha, path.name)
    now = utc_now()

    raw_dir = settings.casefiles_dir / case_id / "raw" / now.strftime("%Y%m%d")
    raw_dir.mkdir(parents=True, exist_ok=True)
    stored_path = raw_dir / f"{evidence_id}_{slugify(path.stem)}{path.suffix.lower()}"
    if not stored_path.exists():
        shutil.copy2(path, stored_path)

    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    evidence_ref = EvidenceRef(
        evidence_id=evidence_id,
        case_id=case_id,
        event_id=event_id,
        original_path=str(path.resolve()),
        stored_path=str(stored_path.resolve()),
        sha256=sha,
        mime_type=mime_type,
        size_bytes=path.stat().st_size,
        created_at=now,
    )

    parsed = _parse_import_file(path)
    event = CaseEvent(
        event_id=event_id,
        case_id=case_id,
        source=parsed.get("source", EventSource.IMPORT),
        event_type=parsed.get("event_type", "agency_message_received"),
        received_at=parsed.get("received_at", now),
        summary=parsed.get("summary", path.name),
        content_text=parsed.get("content_text", ""),
        evidence_refs=[evidence_id],
    )
    return event, evidence_ref


def _parse_import_file(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix == ".eml":
        return _parse_eml(path)
    if suffix in {".json", ".yaml", ".yml"}:
        return _parse_structured_event(path)
    if suffix in {".txt", ".md"}:
        text = path.read_text(encoding="utf-8", errors="replace")
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), path.name)
        return {"summary": first_line[:180], "content_text": text}
    if suffix == ".pdf":
        return {
            "summary": f"PDF imported: {path.name}",
            "content_text": f"[PDF imported without text extraction: {path.name}]",
            "event_type": "document_received",
        }
    raise ValueError(f"unsupported import file type: {suffix}")


def _parse_structured_event(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"structured event must be an object: {path}")

    received_at = data.get("received_at")
    return {
        "source": data.get("source", EventSource.PORTAL),
        "event_type": data.get("event_type", "portal_status_changed"),
        "received_at": utc_now() if not received_at else _parse_datetime(received_at),
        "summary": data.get("summary", path.name),
        "content_text": data.get("content_text") or data.get("body") or text,
    }


def _parse_eml(path: Path) -> dict[str, Any]:
    message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    subject = message.get("subject", path.name)
    date_header = message.get("date")
    from_header = message.get("from")
    body = message.get_body(preferencelist=("plain", "html"))
    content = _message_body_to_text(body) if body is not None else ""
    return {
        "source": EventSource.HIMALAYA,
        "event_type": "human_sent_message"
        if _is_requester_sender(from_header)
        else "agency_message_received",
        "received_at": _parse_datetime(date_header) if date_header else utc_now(),
        "summary": subject,
        "content_text": strip_quoted_history(content),
    }


def _parse_datetime(value: str):
    from datetime import datetime
    from email.utils import parsedate_to_datetime

    try:
        parsed = parsedate_to_datetime(value)
        return parsed if parsed.tzinfo else parsed.astimezone()
    except (TypeError, ValueError):
        return datetime.fromisoformat(value)


def _is_requester_sender(from_header) -> bool:
    if isinstance(from_header, AddressHeader):
        addresses = [address.addr_spec.lower() for address in from_header.addresses]
    else:
        addresses = [str(from_header or "").lower()]
    requester_emails = {"drake.t98@proton.me", "drake@draket.xyz"}
    return any(address in requester_emails for address in addresses)


def _message_body_to_text(body) -> str:
    content = body.get_content()
    if body.get_content_type() == "text/html":
        return html_to_text(content)
    return content


def strip_quoted_history(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    cut_markers = [
        "\nFrom:",
        "\nSent:",
        "\nOn ",
        "\n-----Original Message-----",
        "\nNOTICE: This email was sent",
    ]
    cut_at = len(normalized)
    for marker in cut_markers:
        index = normalized.find(marker)
        if index >= 0:
            cut_at = min(cut_at, index)
    return "\n".join(line.rstrip() for line in normalized[:cut_at].splitlines()).strip()


def html_to_text(html: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(html)
    return parser.get_text()


class _HTMLTextExtractor(HTMLParser):
    BLOCK_TAGS = {
        "br",
        "div",
        "p",
        "tr",
        "table",
        "li",
        "ul",
        "ol",
        "body",
        "head",
    }

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"style", "script"}:
            self._skip_depth += 1
            return
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"style", "script"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = unescape(data).strip()
        if text:
            self.parts.append(text)
            self.parts.append(" ")

    def get_text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self.parts).splitlines()]
        return "\n".join(line for line in lines if line)
