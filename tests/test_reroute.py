from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from prr_pressure_cooker.config import Settings
from prr_pressure_cooker.models import CaseEvent
from prr_pressure_cooker.service import reroute_case, route_event
from prr_pressure_cooker.storage import Store


def _settings(tmp_path: Path) -> Settings:
    return Settings(db_path=tmp_path / "prr.db", casefiles_dir=tmp_path / "casefiles")


def _event(
    case_id: str,
    event_id: str,
    offset: int,
    text: str,
    event_type: str = "agency_message_received",
):
    return CaseEvent(
        event_id=event_id,
        case_id=case_id,
        event_type=event_type,
        received_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC) + timedelta(minutes=offset),
        summary=text.splitlines()[0][:80],
        content_text=text,
    )


def test_payment_confirmation_cancels_stale_fee_task(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.create_case("allmail-prr-163721", "Orange County", "PRR-163721")
    estimate = _event(
        "allmail-prr-163721",
        "evt_estimate",
        0,
        "Fee estimate: deposit $350.00\nLabor and records review.",
    )
    store.save_event(estimate)
    route_event("allmail-prr-163721", "evt_estimate", store, settings)
    assert len(store.list_tasks(status="pending", case_id="allmail-prr-163721")) == 1

    store.save_event(
        _event(
            "allmail-prr-163721",
            "evt_paid",
            5,
            "I have submitted payment. Please proceed with processing.",
            event_type="human_sent_message",
        )
    )
    store.save_event(
        _event(
            "allmail-prr-163721",
            "evt_confirmation",
            10,
            "Orange County Public Records Online Payment Confirmation - PRR-163721",
        )
    )

    result = reroute_case("allmail-prr-163721", store, settings, replace_tasks=True)

    assert result["pending_before"] == 1
    assert result["pending_after"] == 0
    assert result["canceled_tasks"] == 1
    assert result["active_decisions"] == []


def test_later_closure_warning_leaves_only_urgent_closure_task(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.create_case("allmail-records-26-11231", "Orlando", "#26-11231")
    store.save_event(
        _event(
            "allmail-records-26-11231",
            "evt_estimate",
            0,
            "Fee estimate: deposit $200.00\nLabor and records review.",
        )
    )
    store.save_event(
        _event(
            "allmail-records-26-11231",
            "evt_clock",
            5,
            "Estimated processing time: 2 hours 35 minutes at $27.09 per hour "
            "for an estimated cost of $69.98. If payment is not received within "
            "two business days, your request will be closed.",
        )
    )

    result = reroute_case("allmail-records-26-11231", store, settings, replace_tasks=True)
    pending = store.list_tasks(status="pending", case_id="allmail-records-26-11231")

    assert result["pending_after"] == 1
    assert [decision["pathway"] for decision in result["active_decisions"]] == [
        "closure_threat"
    ]
    assert len(pending) == 1
    assert pending[0].pathway == "closure_threat"


def test_records_release_then_fulfilled_closure_has_no_pending_task(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.create_case("allmail-records-26-10768", "Orlando", "#26-10768")
    store.save_event(
        _event(
            "allmail-records-26-10768",
            "evt_release",
            0,
            "Documents have been released to requester for public records request #26-10768.",
        )
    )
    store.save_event(
        _event(
            "allmail-records-26-10768",
            "evt_closed",
            5,
            "Public records request #26-10768 has been closed as fulfilled. "
            "Provided all records responsive to your request.",
        )
    )

    result = reroute_case("allmail-records-26-10768", store, settings, replace_tasks=True)

    assert result["pending_after"] == 0
    assert result["active_decisions"] == []
    assert store.list_tasks(status="pending", case_id="allmail-records-26-10768") == []
