from __future__ import annotations

import base64
import csv
import hashlib
import json
import mimetypes
import re
import shutil
from dataclasses import dataclass
from email import policy
from email.headerregistry import AddressHeader
from email.parser import BytesParser
from email.utils import getaddresses
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import yaml

from prr_pressure_cooker.config import Settings
from prr_pressure_cooker.ids import content_id, file_sha256, slugify, utc_now
from prr_pressure_cooker.models import (
    AttachmentIndexRecord,
    CaseEvent,
    CaseExternalRefRecord,
    ContactIndexRecord,
    EventSource,
    EvidenceRef,
    MessageIndexRecord,
    ThreadIndexRecord,
)
from prr_pressure_cooker.storage import Store

SUPPORTED_IMPORT_SUFFIXES = {".eml", ".pdf", ".txt", ".md", ".json", ".yaml", ".yml"}


@dataclass(frozen=True)
class ParsedAddress:
    name: str | None
    address: str


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
    index_event_evidence(event, store, settings)
    record_case_external_refs(
        event.case_id,
        store,
        str(event.source),
        event.summary,
        *(
            message.subject
            for message in store.list_message_indexes(event.case_id)
            if message.event_id == event.event_id
        ),
    )
    return [event]


def record_case_external_refs(
    case_id: str,
    store: Store,
    source: str,
    *texts: str,
) -> list[CaseExternalRefRecord]:
    now = utc_now()
    records = [
        CaseExternalRefRecord(
            normalized_ref=ref["normalized_ref"],
            case_id=case_id,
            ref_type=ref["ref_type"],
            ref_value=ref["ref_value"],
            source=source,
            created_at=now,
            updated_at=now,
        )
        for ref in extract_case_external_refs(*texts)
    ]
    for record in records:
        store.save_case_external_ref(record)
    return records


def resolve_case_id_from_external_refs(
    store: Store,
    *texts: str,
) -> str | None:
    refs = extract_case_external_refs(*texts)
    return store.find_case_by_external_refs(ref["normalized_ref"] for ref in refs)


def extract_case_external_refs(*texts: str) -> list[dict[str, str]]:
    combined = "\n".join(text for text in texts if text)
    patterns = [
        (
            "prr",
            re.compile(r"\bPRR[-\s#]*([0-9]+)\b", re.IGNORECASE),
            lambda match: f"PRR-{match.group(1)}",
            lambda match: f"prr:{match.group(1)}",
        ),
        (
            "public_records_request",
            re.compile(r"\bpublic records request\s*#\s*([0-9]+-[0-9]+)\b", re.I),
            lambda match: f"#{match.group(1)}",
            lambda match: f"records:{match.group(1).lower()}",
        ),
        (
            "corr",
            re.compile(r"\b(CORR-[0-9]+-[0-9]+)\b", re.IGNORECASE),
            lambda match: match.group(1).upper(),
            lambda match: f"corr:{match.group(1).lower()}",
        ),
        (
            "agency_records_request",
            re.compile(r"\brecords request\s+([A-Z]+-[0-9]+-[0-9]+)\b", re.I),
            lambda match: match.group(1).upper(),
            lambda match: f"records-request:{match.group(1).lower()}",
        ),
        (
            "mycusthelp",
            re.compile(r"\b(W[0-9]+-[0-9]+)\b", re.IGNORECASE),
            lambda match: match.group(1).upper(),
            lambda match: f"mycusthelp:{match.group(1).lower()}",
        ),
    ]
    refs: list[dict[str, str]] = []
    seen: set[str] = set()
    for ref_type, pattern, ref_value, normalized_ref in patterns:
        for match in pattern.finditer(combined):
            normalized = normalized_ref(match)
            if normalized in seen:
                continue
            seen.add(normalized)
            refs.append(
                {
                    "ref_type": ref_type,
                    "ref_value": ref_value(match),
                    "normalized_ref": normalized,
                }
            )
    return refs


def index_event_evidence(event: CaseEvent, store: Store, settings: Settings) -> dict:
    evidence_refs = store.get_evidence_refs(event.evidence_refs)
    messages_before = len(store.list_message_indexes(event.case_id))
    attachments_before = len(store.list_attachment_indexes(event.case_id))
    for ref in evidence_refs:
        if _is_eml_ref(ref):
            _index_eml_ref(event, ref, store, settings)
        elif _is_structured_ref(ref):
            _index_structured_ref(event, ref, store, settings)
        else:
            _index_generic_event_ref(event, ref, store)
    _refresh_thread_indexes(event.case_id, store)
    index_paths = export_casefile_indexes(event.case_id, store, settings)
    return {
        "case_id": event.case_id,
        "event_id": event.event_id,
        "messages_before": messages_before,
        "messages_after": len(store.list_message_indexes(event.case_id)),
        "attachments_before": attachments_before,
        "attachments_after": len(store.list_attachment_indexes(event.case_id)),
        "index_paths": index_paths,
    }


def rebuild_casefile_indexes(case_id: str, store: Store, settings: Settings) -> dict:
    store.get_case(case_id)
    deleted = store.clear_casefile_indexes(case_id)
    indexed_events = 0
    for event in store.list_events(case_id):
        index_event_evidence(event, store, settings)
        indexed_events += 1
    index_paths = export_casefile_indexes(case_id, store, settings)
    return {
        "case_id": case_id,
        "deleted_index_rows": deleted,
        "indexed_events": indexed_events,
        "messages": len(store.list_message_indexes(case_id)),
        "threads": len(store.list_thread_indexes(case_id)),
        "attachments": len(store.list_attachment_indexes(case_id)),
        "contacts": len(store.list_contact_indexes(case_id)),
        "index_paths": index_paths,
    }


def export_casefile_indexes(case_id: str, store: Store, settings: Settings) -> list[str]:
    case = store.get_case(case_id)
    indexes_dir = settings.casefiles_dir / case_id / "indexes"
    indexes_dir.mkdir(parents=True, exist_ok=True)

    messages_path = indexes_dir / "messages.csv"
    threads_path = indexes_dir / "threads.csv"
    attachments_path = indexes_dir / "attachments.csv"
    contacts_path = indexes_dir / "contacts.csv"
    timeline_path = indexes_dir / "timeline.md"

    messages = store.list_message_indexes(case_id)
    threads = store.list_thread_indexes(case_id)
    attachments = store.list_attachment_indexes(case_id)
    contacts = store.list_contact_indexes(case_id)
    message_ref_paths = {
        ref.evidence_id: ref.stored_path
        for ref in store.get_evidence_refs(message.evidence_id for message in messages)
    }

    with messages_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "event_id",
                "evidence_id",
                "stored_path",
                "received_at",
                "thread_id",
                "message_id",
                "subject",
                "sender",
                "recipients",
                "cc",
                "attachments",
                "snippet",
            ]
        )
        for message in messages:
            writer.writerow(
                [
                    message.event_id,
                    message.evidence_id,
                    message_ref_paths.get(message.evidence_id, ""),
                    message.received_at.isoformat(),
                    message.thread_id,
                    message.message_id or "",
                    message.subject,
                    _format_sender(message),
                    "; ".join(message.recipients),
                    "; ".join(message.cc),
                    "; ".join(message.attachment_evidence_ids),
                    message.snippet,
                ]
            )

    with threads_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "thread_id",
                "subject",
                "message_count",
                "first_received_at",
                "latest_received_at",
                "first_event_id",
                "latest_event_id",
                "participants",
            ]
        )
        for thread in threads:
            writer.writerow(
                [
                    thread.thread_id,
                    thread.subject,
                    thread.message_count,
                    thread.first_received_at.isoformat(),
                    thread.latest_received_at.isoformat(),
                    thread.first_event_id,
                    thread.latest_event_id,
                    "; ".join(thread.participants),
                ]
            )

    with attachments_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "evidence_id",
                "event_id",
                "parent_evidence_id",
                "filename",
                "mime_type",
                "size_bytes",
                "sha256",
                "stored_path",
            ]
        )
        for attachment in attachments:
            writer.writerow(
                [
                    attachment.evidence_id,
                    attachment.event_id,
                    attachment.parent_evidence_id,
                    attachment.filename,
                    attachment.mime_type,
                    attachment.size_bytes,
                    attachment.sha256,
                    attachment.stored_path,
                ]
            )

    with contacts_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["event_id", "role", "name", "address"])
        for contact in contacts:
            writer.writerow(
                [
                    contact.event_id,
                    contact.role,
                    contact.name or "",
                    contact.address,
                ]
            )

    timeline_path.write_text(
        _case_timeline_markdown(
            case.agency,
            case.request_title,
            messages,
            attachments,
            message_ref_paths,
        ),
        encoding="utf-8",
    )

    return [
        str(messages_path.resolve()),
        str(threads_path.resolve()),
        str(attachments_path.resolve()),
        str(contacts_path.resolve()),
        str(timeline_path.resolve()),
    ]


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


def _is_eml_ref(ref: EvidenceRef) -> bool:
    return ref.mime_type == "message/rfc822" or ref.stored_file.suffix.lower() == ".eml"


def _is_structured_ref(ref: EvidenceRef) -> bool:
    return ref.stored_file.suffix.lower() in {".json", ".yaml", ".yml"}


def _index_eml_ref(event: CaseEvent, ref: EvidenceRef, store: Store, settings: Settings) -> None:
    message = BytesParser(policy=policy.default).parsebytes(ref.stored_file.read_bytes())
    subject = str(message.get("subject") or event.summary or ref.stored_file.name)
    message_id = _normalize_message_id(message.get("message-id"))
    in_reply_to = _normalize_message_id(message.get("in-reply-to"))
    references = [
        value
        for value in (
            _normalize_message_id(raw) for raw in str(message.get("references") or "").split()
        )
        if value
    ]
    from_addresses = _addresses(message.get_all("from", []))
    sender = from_addresses[0] if from_addresses else None
    to_addresses = _addresses(message.get_all("to", []))
    cc_addresses = _addresses(message.get_all("cc", []))
    bcc_addresses = _addresses(message.get_all("bcc", []))

    attachment_ids = _extract_attachment_indexes(event, ref, message, store, settings)
    body = message.get_body(preferencelist=("plain", "html"))
    content = _message_body_to_text(body) if body is not None else event.content_text
    snippet = _snippet(strip_quoted_history(content) or event.content_text)
    thread_id = _thread_id(event.case_id, subject, references, in_reply_to)
    created_at = utc_now()
    store.save_message_index(
        MessageIndexRecord(
            message_index_id=content_id(
                "message-index", event.case_id, event.event_id, ref.evidence_id
            ),
            case_id=event.case_id,
            event_id=event.event_id,
            evidence_id=ref.evidence_id,
            thread_id=thread_id,
            message_id=message_id,
            in_reply_to=in_reply_to,
            references=references,
            subject=subject,
            sender_name=sender.name if sender else None,
            sender_address=sender.address if sender else None,
            recipients=[address.address for address in to_addresses],
            cc=[address.address for address in cc_addresses],
            received_at=event.received_at,
            snippet=snippet,
            attachment_evidence_ids=attachment_ids,
            created_at=created_at,
        )
    )
    for role, addresses in (
        ("sender", from_addresses),
        ("recipient", to_addresses),
        ("cc", cc_addresses),
        ("bcc", bcc_addresses),
    ):
        _save_contact_indexes(event, role, addresses, store, created_at)


def _index_structured_ref(
    event: CaseEvent, ref: EvidenceRef, store: Store, settings: Settings
) -> None:
    data = _load_structured_ref(ref.stored_file)
    subject = str(
        data.get("subject")
        or data.get("title")
        or data.get("summary")
        or event.summary
        or ref.stored_file.name
    )
    sender_addresses = _structured_addresses(
        data.get("sender")
        or data.get("from")
        or data.get("agency_contact")
        or data.get("portal_sender")
    )
    recipient_addresses = _structured_addresses(
        data.get("recipients") or data.get("to") or data.get("requester")
    )
    cc_addresses = _structured_addresses(data.get("cc"))
    references = _structured_references(data.get("references"))
    in_reply_to = _normalize_message_id(data.get("in_reply_to") or data.get("reply_to"))
    attachment_ids = _extract_structured_attachment_indexes(event, ref, data, store, settings)
    thread_key = str(data.get("thread_id") or data.get("portal_thread_id") or "")
    thread_id = (
        content_id("thread", event.case_id, thread_key)
        if thread_key
        else _thread_id(event.case_id, subject, references, in_reply_to)
    )
    sender = sender_addresses[0] if sender_addresses else None
    created_at = utc_now()
    store.save_message_index(
        MessageIndexRecord(
            message_index_id=content_id(
                "message-index", event.case_id, event.event_id, ref.evidence_id
            ),
            case_id=event.case_id,
            event_id=event.event_id,
            evidence_id=ref.evidence_id,
            thread_id=thread_id,
            message_id=_normalize_message_id(
                data.get("message_id") or data.get("portal_message_id")
            ),
            in_reply_to=in_reply_to,
            references=references,
            subject=subject,
            sender_name=sender.name if sender else None,
            sender_address=sender.address if sender else None,
            recipients=[address.address for address in recipient_addresses],
            cc=[address.address for address in cc_addresses],
            received_at=event.received_at,
            snippet=_snippet(event.content_text or json.dumps(data, sort_keys=True)),
            attachment_evidence_ids=attachment_ids,
            created_at=created_at,
        )
    )
    for role, addresses in (
        ("sender", sender_addresses),
        ("recipient", recipient_addresses),
        ("cc", cc_addresses),
    ):
        _save_contact_indexes(event, role, addresses, store, created_at)
    _save_structured_contacts(event, data.get("contacts"), store, created_at)


def _index_generic_event_ref(event: CaseEvent, ref: EvidenceRef, store: Store) -> None:
    created_at = utc_now()
    store.save_message_index(
        MessageIndexRecord(
            message_index_id=content_id(
                "message-index", event.case_id, event.event_id, ref.evidence_id
            ),
            case_id=event.case_id,
            event_id=event.event_id,
            evidence_id=ref.evidence_id,
            thread_id=_thread_id(event.case_id, event.summary, [], None),
            subject=event.summary or ref.stored_file.name,
            received_at=event.received_at,
            snippet=_snippet(event.content_text),
            created_at=created_at,
        )
    )


def _extract_attachment_indexes(
    event: CaseEvent,
    ref: EvidenceRef,
    message,
    store: Store,
    settings: Settings,
) -> list[str]:
    attachment_ids: list[str] = []
    attachment_dir = (
        settings.casefiles_dir
        / event.case_id
        / "raw"
        / event.received_at.strftime("%Y%m%d")
        / "attachments"
    )
    attachment_dir.mkdir(parents=True, exist_ok=True)
    for index, part in enumerate(message.iter_attachments(), start=1):
        filename = part.get_filename() or f"attachment-{index}.bin"
        payload = part.get_payload(decode=True)
        data = payload if payload is not None else part.get_content().encode("utf-8")
        sha = hashlib.sha256(data).hexdigest()
        evidence_id = content_id(
            "evi",
            event.case_id,
            event.event_id,
            ref.evidence_id,
            filename,
            sha,
        )
        stored_path = attachment_dir / _stored_attachment_name(evidence_id, filename)
        if not stored_path.exists():
            stored_path.write_bytes(data)
        attachment_ref = EvidenceRef(
            evidence_id=evidence_id,
            case_id=event.case_id,
            event_id=event.event_id,
            original_path=f"{ref.original_path}::{filename}",
            stored_path=str(stored_path.resolve()),
            sha256=sha,
            mime_type=part.get_content_type(),
            size_bytes=len(data),
            created_at=utc_now(),
        )
        store.save_evidence_ref(attachment_ref)
        store.save_attachment_index(
            AttachmentIndexRecord(
                attachment_index_id=content_id("attachment-index", evidence_id),
                case_id=event.case_id,
                event_id=event.event_id,
                parent_evidence_id=ref.evidence_id,
                evidence_id=evidence_id,
                filename=filename,
                mime_type=part.get_content_type(),
                size_bytes=len(data),
                sha256=sha,
                stored_path=str(stored_path.resolve()),
                created_at=attachment_ref.created_at,
            )
        )
        attachment_ids.append(evidence_id)
    return attachment_ids


def _extract_structured_attachment_indexes(
    event: CaseEvent,
    ref: EvidenceRef,
    data: dict[str, Any],
    store: Store,
    settings: Settings,
) -> list[str]:
    attachments = data.get("attachments") or []
    if not isinstance(attachments, list):
        return []

    attachment_ids: list[str] = []
    attachment_dir = (
        settings.casefiles_dir
        / event.case_id
        / "raw"
        / event.received_at.strftime("%Y%m%d")
        / "attachments"
    )
    attachment_dir.mkdir(parents=True, exist_ok=True)
    for index, attachment in enumerate(attachments, start=1):
        if not isinstance(attachment, dict):
            continue
        filename = str(attachment.get("filename") or f"portal-attachment-{index}.bin")
        data_bytes = _structured_attachment_bytes(attachment)
        if data_bytes is None:
            continue
        mime_type = (
            attachment.get("mime_type")
            or attachment.get("content_type")
            or mimetypes.guess_type(filename)[0]
            or "application/octet-stream"
        )
        sha = hashlib.sha256(data_bytes).hexdigest()
        evidence_id = content_id(
            "evi",
            event.case_id,
            event.event_id,
            ref.evidence_id,
            filename,
            sha,
        )
        stored_path = attachment_dir / _stored_attachment_name(evidence_id, filename)
        if not stored_path.exists():
            stored_path.write_bytes(data_bytes)
        attachment_ref = EvidenceRef(
            evidence_id=evidence_id,
            case_id=event.case_id,
            event_id=event.event_id,
            original_path=f"{ref.original_path}::{filename}",
            stored_path=str(stored_path.resolve()),
            sha256=sha,
            mime_type=str(mime_type),
            size_bytes=len(data_bytes),
            created_at=utc_now(),
        )
        store.save_evidence_ref(attachment_ref)
        store.save_attachment_index(
            AttachmentIndexRecord(
                attachment_index_id=content_id("attachment-index", evidence_id),
                case_id=event.case_id,
                event_id=event.event_id,
                parent_evidence_id=ref.evidence_id,
                evidence_id=evidence_id,
                filename=filename,
                mime_type=str(mime_type),
                size_bytes=len(data_bytes),
                sha256=sha,
                stored_path=str(stored_path.resolve()),
                created_at=attachment_ref.created_at,
            )
        )
        attachment_ids.append(evidence_id)
    return attachment_ids


def _structured_attachment_bytes(attachment: dict[str, Any]) -> bytes | None:
    if attachment.get("content_b64"):
        return base64.b64decode(str(attachment["content_b64"]).encode("ascii"))
    if attachment.get("content_text") is not None:
        return str(attachment["content_text"]).encode("utf-8")
    if attachment.get("content") is not None:
        content = attachment["content"]
        if isinstance(content, str):
            return content.encode("utf-8")
    if attachment.get("path"):
        path = Path(str(attachment["path"]))
        if path.exists() and path.is_file():
            return path.read_bytes()
    return None


def _save_contact_indexes(
    event: CaseEvent,
    role: str,
    addresses: list[ParsedAddress],
    store: Store,
    created_at,
) -> None:
    for address in addresses:
        store.save_contact_index(
            ContactIndexRecord(
                contact_index_id=content_id(
                    "contact-index",
                    event.case_id,
                    event.event_id,
                    role,
                    address.address,
                ),
                case_id=event.case_id,
                event_id=event.event_id,
                role=role,
                name=address.name,
                address=address.address,
                created_at=created_at,
            )
        )


def _save_structured_contacts(
    event: CaseEvent,
    raw_contacts: Any,
    store: Store,
    created_at,
) -> None:
    if not raw_contacts:
        return
    contacts = raw_contacts if isinstance(raw_contacts, list) else [raw_contacts]
    for raw_contact in contacts:
        if not isinstance(raw_contact, dict):
            for address in _structured_addresses(raw_contact):
                _save_contact_indexes(event, "contact", [address], store, created_at)
            continue
        role = str(raw_contact.get("role") or "contact")
        _save_contact_indexes(
            event,
            role,
            _structured_addresses(raw_contact),
            store,
            created_at,
        )


def _refresh_thread_indexes(case_id: str, store: Store) -> None:
    by_thread: dict[str, list[MessageIndexRecord]] = {}
    for message in store.list_message_indexes(case_id):
        by_thread.setdefault(message.thread_id, []).append(message)
    now = utc_now()
    for thread_id, messages in by_thread.items():
        ordered = sorted(messages, key=lambda item: (item.received_at, item.event_id))
        participants = sorted(
            {
                value
                for message in ordered
                for value in [
                    message.sender_address,
                    *message.recipients,
                    *message.cc,
                ]
                if value
            }
        )
        store.save_thread_index(
            ThreadIndexRecord(
                thread_id=thread_id,
                case_id=case_id,
                subject=ordered[0].subject,
                message_count=len(ordered),
                first_event_id=ordered[0].event_id,
                latest_event_id=ordered[-1].event_id,
                first_received_at=ordered[0].received_at,
                latest_received_at=ordered[-1].received_at,
                participants=participants,
                created_at=ordered[0].created_at,
                updated_at=now,
            )
        )


def _addresses(raw_values: list[str]) -> list[ParsedAddress]:
    addresses: list[ParsedAddress] = []
    seen: set[str] = set()
    for name, address in getaddresses(raw_values):
        normalized = address.strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        addresses.append(ParsedAddress(name=name.strip() or None, address=normalized))
    return addresses


def _structured_addresses(value: Any) -> list[ParsedAddress]:
    if value is None:
        return []
    if isinstance(value, list):
        parsed: list[ParsedAddress] = []
        seen: set[str] = set()
        for item in value:
            for address in _structured_addresses(item):
                if address.address not in seen:
                    seen.add(address.address)
                    parsed.append(address)
        return parsed
    if isinstance(value, dict):
        address = (
            value.get("address") or value.get("email") or value.get("mail") or value.get("name")
        )
        if not address:
            return []
        return [
            ParsedAddress(
                name=str(value.get("name")).strip()
                if value.get("name") and value.get("name") != address
                else None,
                address=str(address).strip().lower(),
            )
        ]
    return _addresses([str(value)])


def _structured_references(value: Any) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, list) else str(value).split()
    return [
        normalized
        for normalized in (_normalize_message_id(str(item)) for item in values)
        if normalized
    ]


def _load_structured_ref(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    data = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    return data if isinstance(data, dict) else {}


def _normalize_message_id(value: Any) -> str | None:
    if not value:
        return None
    normalized = str(value).strip()
    if normalized.startswith("<") and normalized.endswith(">"):
        return normalized[1:-1].strip() or None
    return normalized or None


def _thread_id(
    case_id: str,
    subject: str,
    references: list[str],
    in_reply_to: str | None,
) -> str:
    subject_key = _normalize_thread_subject(subject)
    root = subject_key or (references[0] if references else in_reply_to) or "untitled"
    return content_id("thread", case_id, root)


def _normalize_thread_subject(subject: str) -> str:
    normalized = subject.strip()
    while True:
        lowered = normalized.lower()
        for prefix in ("re:", "fw:", "fwd:"):
            if lowered.startswith(prefix):
                normalized = normalized[len(prefix) :].strip()
                break
        else:
            break
    return " ".join(normalized.split()).lower()


def _stored_attachment_name(evidence_id: str, filename: str) -> str:
    path = Path(filename)
    suffix = path.suffix.lower() or ".bin"
    stem = slugify(path.stem, fallback="attachment")
    return f"{evidence_id}_{stem}{suffix}"


def _snippet(text: str, max_chars: int = 260) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= max_chars:
        return normalized
    return f"{normalized[:max_chars].rstrip()}..."


def _format_sender(message: MessageIndexRecord) -> str:
    if message.sender_name and message.sender_address:
        return f"{message.sender_name} <{message.sender_address}>"
    return message.sender_address or message.sender_name or ""


def _case_timeline_markdown(
    agency: str,
    request_title: str,
    messages: list[MessageIndexRecord],
    attachments: list[AttachmentIndexRecord],
    message_ref_paths: dict[str, str],
) -> str:
    attachments_by_event: dict[str, list[AttachmentIndexRecord]] = {}
    for attachment in attachments:
        attachments_by_event.setdefault(attachment.event_id, []).append(attachment)
    lines = [
        f"# Timeline: {request_title}",
        "",
        f"- Agency: {agency}",
        f"- Messages indexed: {len(messages)}",
        f"- Attachments indexed: {len(attachments)}",
        "",
    ]
    for message in messages:
        lines.extend(
            [
                f"## {message.received_at.isoformat()} - {message.subject}",
                "",
                f"- Event: `{message.event_id}`",
                f"- Source evidence: `{message.evidence_id}`",
                f"- Source path: `{message_ref_paths.get(message.evidence_id, 'unknown')}`",
                f"- Thread: `{message.thread_id}`",
                f"- From: {_format_sender(message) or 'unknown'}",
                f"- To: {'; '.join(message.recipients) or 'none'}",
                f"- Attachments: {len(attachments_by_event.get(message.event_id, []))}",
                "",
                message.snippet or "No message text captured.",
                "",
            ]
        )
        for attachment in attachments_by_event.get(message.event_id, []):
            lines.append(
                f"  - `{attachment.evidence_id}` {attachment.filename}: {attachment.stored_path}"
            )
        if attachments_by_event.get(message.event_id):
            lines.append("")
    return "\n".join(lines)


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
