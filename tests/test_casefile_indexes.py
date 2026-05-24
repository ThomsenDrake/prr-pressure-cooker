from __future__ import annotations

import base64
import json
from email.message import EmailMessage
from pathlib import Path

from prr_pressure_cooker.cli import main
from prr_pressure_cooker.config import Settings
from prr_pressure_cooker.ingest import import_path, rebuild_casefile_indexes
from prr_pressure_cooker.models import ReviewAssistantInput
from prr_pressure_cooker.service import (
    build_pushed_event_payload,
    persist_pushed_event_payload,
    review_task_prompt,
    route_event,
)
from prr_pressure_cooker.storage import Store


def _settings(tmp_path: Path) -> Settings:
    return Settings(db_path=tmp_path / "prr.db", casefiles_dir=tmp_path / "casefiles")


def _write_estimate_email(tmp_path: Path) -> Path:
    message = EmailMessage()
    message["From"] = "Records Unit <records@example.gov>"
    message["To"] = "Drake <drake.t98@proton.me>"
    message["Cc"] = "Supervisor <supervisor@example.gov>"
    message["Subject"] = "Re: PRR-163721 revised estimate"
    message["Date"] = "Mon, 11 May 2026 17:03:00 +0000"
    message["Message-ID"] = "<agency-estimate-1@example.gov>"
    message.set_content(
        "Fee estimate: deposit $350.00\n"
        "Labor and records review.\n"
        "Please pay before we continue processing."
    )
    message.add_attachment(
        b"estimate pdf bytes",
        maintype="application",
        subtype="pdf",
        filename="estimate.pdf",
    )
    path = tmp_path / "estimate.eml"
    path.write_bytes(message.as_bytes())
    return path


def _write_followup_email(directory: Path) -> Path:
    message = EmailMessage()
    message["From"] = "Drake <drake.t98@proton.me>"
    message["To"] = "Records Unit <records@example.gov>"
    message["Subject"] = "Re: PRR-163721 revised estimate"
    message["Date"] = "Mon, 11 May 2026 18:03:00 +0000"
    message["Message-ID"] = "<requester-followup-1@example.test>"
    message["In-Reply-To"] = "<agency-estimate-1@example.gov>"
    message["References"] = "<agency-estimate-1@example.gov>"
    message.set_content("I need the estimate itemized before I can evaluate payment.")
    path = directory / "followup.eml"
    path.write_bytes(message.as_bytes())
    return path


def _write_cross_reference_email(directory: Path) -> Path:
    message = EmailMessage()
    message["From"] = "Records Unit <records@example.gov>"
    message["To"] = "Drake <drake.t98@proton.me>"
    message["Subject"] = "Re: PRR-111111 estimate"
    message["Date"] = "Mon, 11 May 2026 19:03:00 +0000"
    message["Message-ID"] = "<agency-cross-ref-1@example.gov>"
    message.set_content(
        "This response concerns PRR-111111. For context, your older PRR-222222 "
        "was handled by a different unit."
    )
    path = directory / "cross_ref.eml"
    path.write_bytes(message.as_bytes())
    return path


def _write_portal_event(tmp_path: Path) -> Path:
    payload = {
        "source": "portal",
        "event_type": "agency_message_received",
        "received_at": "2026-05-12T14:30:00+00:00",
        "summary": "Portal estimate posted",
        "subject": "PRR-163721 portal fee estimate",
        "portal_message_id": "portal-msg-42",
        "portal_thread_id": "portal-thread-prr-163721",
        "sender": {"name": "Public Records Portal", "address": "records@example.gov"},
        "recipients": [{"name": "Drake", "address": "drake.t98@proton.me"}],
        "contacts": [
            {
                "role": "custodian",
                "name": "Records Custodian",
                "address": "custodian@example.gov",
            }
        ],
        "content_text": (
            "Fee estimate: deposit $425.00\n"
            "Generic labor and records review. Payment is required before processing."
        ),
        "attachments": [
            {
                "filename": "portal-estimate.pdf",
                "mime_type": "application/pdf",
                "content_b64": base64.b64encode(b"portal estimate bytes").decode("ascii"),
            }
        ],
    }
    path = tmp_path / "portal_event.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_eml_import_populates_casefile_indexes_and_extracts_attachments(
    tmp_path: Path,
):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.create_case("demo", "Demo Agency", "Demo request")

    events = import_path("demo", _write_estimate_email(tmp_path), store, settings)

    assert len(events) == 1
    messages = store.list_message_indexes("demo")
    attachments = store.list_attachment_indexes("demo")
    contacts = store.list_contact_indexes("demo")
    threads = store.list_thread_indexes("demo")
    assert len(messages) == 1
    assert messages[0].message_id == "agency-estimate-1@example.gov"
    assert messages[0].sender_address == "records@example.gov"
    assert messages[0].recipients == ["drake.t98@proton.me"]
    assert messages[0].cc == ["supervisor@example.gov"]
    assert len(messages[0].attachment_evidence_ids) == 1
    assert len(attachments) == 1
    assert attachments[0].filename == "estimate.pdf"
    assert Path(attachments[0].stored_path).exists()
    assert {(contact.role, contact.address) for contact in contacts} == {
        ("sender", "records@example.gov"),
        ("recipient", "drake.t98@proton.me"),
        ("cc", "supervisor@example.gov"),
    }
    assert len(threads) == 1
    assert threads[0].message_count == 1

    messages_csv = settings.casefiles_dir / "demo" / "indexes" / "messages.csv"
    attachments_csv = settings.casefiles_dir / "demo" / "indexes" / "attachments.csv"
    timeline = settings.casefiles_dir / "demo" / "indexes" / "timeline.md"
    messages_text = messages_csv.read_text(encoding="utf-8")
    timeline_text = timeline.read_text(encoding="utf-8")
    assert "agency-estimate-1@example.gov" in messages_text
    assert messages[0].evidence_id in messages_text
    assert "estimate.pdf" in attachments_csv.read_text(encoding="utf-8")
    assert "Attachments: 1" in timeline_text
    assert f"Source evidence: `{messages[0].evidence_id}`" in timeline_text
    assert attachments[0].stored_path in timeline_text


def test_import_records_durable_case_external_refs(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.create_case("demo", "Demo Agency", "Demo request")

    import_path("demo", _write_estimate_email(tmp_path), store, settings)

    ref = store.get_case_external_ref("prr:163721")
    assert ref is not None
    assert ref.case_id == "demo"
    assert ref.ref_value == "PRR-163721"


def test_import_does_not_alias_body_only_cross_references(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.create_case("demo", "Demo Agency", "Demo request")

    import_path("demo", _write_cross_reference_email(tmp_path), store, settings)

    assert store.get_case_external_ref("prr:111111") is not None
    assert store.get_case_external_ref("prr:222222") is None


def test_directory_import_groups_related_messages_into_one_thread(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.create_case("demo", "Demo Agency", "Demo request")
    thread_dir = tmp_path / "thread"
    thread_dir.mkdir()
    _write_estimate_email(thread_dir)
    _write_followup_email(thread_dir)

    events = import_path("demo", thread_dir, store, settings)

    messages = store.list_message_indexes("demo")
    threads = store.list_thread_indexes("demo")
    assert len(events) == 2
    assert len(messages) == 2
    assert len(threads) == 1
    assert threads[0].message_count == 2
    assert {
        "drake.t98@proton.me",
        "records@example.gov",
        "supervisor@example.gov",
    } <= set(threads[0].participants)


def test_casefile_index_rebuild_is_idempotent(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.create_case("demo", "Demo Agency", "Demo request")
    import_path("demo", _write_estimate_email(tmp_path), store, settings)

    first = rebuild_casefile_indexes("demo", store, settings)
    second = rebuild_casefile_indexes("demo", store, settings)

    assert first["messages"] == second["messages"] == 1
    assert first["threads"] == second["threads"] == 1
    assert first["attachments"] == second["attachments"] == 1
    assert first["contacts"] == second["contacts"] == 3


def test_portal_casefile_index_rebuild_is_idempotent(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.create_case("demo", "Demo Agency", "Demo request")
    import_path("demo", _write_portal_event(tmp_path), store, settings)

    first = rebuild_casefile_indexes("demo", store, settings)
    second = rebuild_casefile_indexes("demo", store, settings)

    assert first["messages"] == second["messages"] == 1
    assert first["threads"] == second["threads"] == 1
    assert first["attachments"] == second["attachments"] == 1
    assert first["contacts"] == second["contacts"] == 3


def test_portal_import_populates_casefile_indexes_and_embedded_attachments(
    tmp_path: Path,
):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.create_case("demo", "Demo Agency", "Demo request")

    event = import_path("demo", _write_portal_event(tmp_path), store, settings)[0]

    messages = store.list_message_indexes("demo")
    attachments = store.list_attachment_indexes("demo")
    contacts = store.list_contact_indexes("demo")
    threads = store.list_thread_indexes("demo")
    assert event.source == "portal"
    assert len(messages) == 1
    assert messages[0].message_id == "portal-msg-42"
    assert messages[0].subject == "PRR-163721 portal fee estimate"
    assert messages[0].sender_address == "records@example.gov"
    assert messages[0].recipients == ["drake.t98@proton.me"]
    assert len(attachments) == 1
    assert attachments[0].filename == "portal-estimate.pdf"
    assert Path(attachments[0].stored_path).exists()
    assert {
        ("sender", "records@example.gov"),
        ("recipient", "drake.t98@proton.me"),
        ("custodian", "custodian@example.gov"),
    } <= {(contact.role, contact.address) for contact in contacts}
    assert len(threads) == 1
    assert threads[0].message_count == 1
    assert "portal-estimate.pdf" in (
        settings.casefiles_dir / "demo" / "indexes" / "attachments.csv"
    ).read_text(encoding="utf-8")


def test_pushed_portal_event_rebuilds_indexes_from_raw_json(tmp_path: Path):
    local_settings = _settings(tmp_path / "local")
    local_store = Store(local_settings.db_path)
    local_store.create_case("demo", "Demo Agency", "Demo request")
    event = import_path("demo", _write_portal_event(tmp_path), local_store, local_settings)[0]
    payload = build_pushed_event_payload("demo", event.event_id, local_store)

    remote_settings = _settings(tmp_path / "remote")
    remote_store = Store(remote_settings.db_path)
    persist_pushed_event_payload(payload, remote_store, remote_settings)

    assert len(remote_store.list_message_indexes("demo")) == 1
    assert len(remote_store.list_attachment_indexes("demo")) == 1
    assert remote_store.list_attachment_indexes("demo")[0].filename == "portal-estimate.pdf"
    external_ref = remote_store.get_case_external_ref("prr:163721")
    assert external_ref is not None
    assert external_ref.case_id == "demo"
    assert any(
        contact.address == "custodian@example.gov"
        for contact in remote_store.list_contact_indexes("demo")
    )


def test_portal_indexes_feed_packet_and_review_context(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.create_case("demo", "Demo Agency", "Demo request")
    event = import_path("demo", _write_portal_event(tmp_path), store, settings)[0]

    route_event("demo", event.event_id, store, settings)
    task = store.list_tasks(status="pending", case_id="demo")[0]
    packet_attachments = [
        Path(path)
        for path in task.evidence_packet
        if "derived" in Path(path).parts
        and "indexes" in Path(path).parts
        and Path(path).name == "attachments.csv"
    ]
    prompt = review_task_prompt(ReviewAssistantInput(case_id="demo"), store, settings)

    assert len(packet_attachments) == 1
    assert "portal-estimate.pdf" in packet_attachments[0].read_text(encoding="utf-8")
    assert prompt.case_context is not None
    assert "PRR-163721 portal fee estimate" in prompt.case_context
    assert "portal-estimate.pdf" in prompt.case_context
    assert "custodian@example.gov" in prompt.case_context
    assert "source `evi_" in prompt.case_context


def test_review_prompt_includes_indexed_timeline_and_attachments(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.create_case("demo", "Demo Agency", "Demo request")
    event = import_path("demo", _write_estimate_email(tmp_path), store, settings)[0]

    route_event("demo", event.event_id, store, settings)
    prompt = review_task_prompt(ReviewAssistantInput(case_id="demo"), store, settings)

    assert prompt.case_context is not None
    assert "Relevant case history:" in prompt.case_context
    assert "PRR-163721 revised estimate" in prompt.case_context
    assert "estimate.pdf" in prompt.case_context
    assert "source `evi_" in prompt.case_context
    assert prompt.packet_context is not None
    assert "Timeline: Demo request" in prompt.packet_context


def test_packet_indexes_copy_canonical_casefile_rows(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.create_case("demo", "Demo Agency", "Demo request")
    event = import_path("demo", _write_estimate_email(tmp_path), store, settings)[0]

    route_event("demo", event.event_id, store, settings)
    task = store.list_tasks(status="pending", case_id="demo")[0]

    packet_messages = [
        Path(path)
        for path in task.evidence_packet
        if "derived" in Path(path).parts
        and "indexes" in Path(path).parts
        and Path(path).name == "messages.csv"
    ]
    assert len(packet_messages) == 1
    assert "agency-estimate-1@example.gov" in packet_messages[0].read_text(encoding="utf-8")


def test_pushed_event_payload_rebuilds_indexes_from_raw_evidence(tmp_path: Path):
    local_settings = _settings(tmp_path / "local")
    local_store = Store(local_settings.db_path)
    local_store.create_case("demo", "Demo Agency", "Demo request")
    event = import_path("demo", _write_estimate_email(tmp_path), local_store, local_settings)[0]
    payload = build_pushed_event_payload("demo", event.event_id, local_store)

    remote_settings = _settings(tmp_path / "remote")
    remote_store = Store(remote_settings.db_path)
    persisted = persist_pushed_event_payload(payload, remote_store, remote_settings)

    assert persisted.event_id == event.event_id
    assert remote_store.get_case("demo").agency == "Demo Agency"
    assert len(remote_store.list_message_indexes("demo")) == 1
    assert len(remote_store.list_attachment_indexes("demo")) == 1
    assert remote_store.list_attachment_indexes("demo")[0].filename == "estimate.pdf"


def test_casefile_rebuild_indexes_cli_outputs_canonical_paths(tmp_path: Path, monkeypatch, capsys):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.create_case("demo", "Demo Agency", "Demo request")
    import_path("demo", _write_estimate_email(tmp_path), store, settings)
    monkeypatch.setenv("PRR_DB_PATH", str(settings.db_path))
    monkeypatch.setenv("PRR_CASEFILES_DIR", str(settings.casefiles_dir))

    main(["casefile", "rebuild-indexes", "demo"])

    output = json.loads(capsys.readouterr().out)
    assert output["case_id"] == "demo"
    assert output["messages"] == 1
    assert output["attachments"] == 1
    assert any(path.endswith("indexes/messages.csv") for path in output["index_paths"])
