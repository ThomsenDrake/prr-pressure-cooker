from __future__ import annotations

from pathlib import Path

from prr_pressure_cooker.config import Settings
from prr_pressure_cooker.ingest import import_path
from prr_pressure_cooker.service import route_event
from prr_pressure_cooker.storage import Store


def test_import_route_and_review_task(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "prr.db", casefiles_dir=tmp_path / "casefiles")
    store = Store(settings.db_path)
    store.create_case("demo", "Demo Agency", "Demo request")

    fixture = Path("tests/fixtures/defective_estimate.txt")
    events = import_path("demo", fixture, store, settings)
    result = route_event("demo", events[0].event_id, store, settings)

    assert result.status == "waiting_for_human_review"
    assert result.pathway == "defective_estimate"

    tasks = store.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].status == "pending"
    assert Path(tasks[0].draft_file).exists()
    assert tasks[0].evidence_packet


def test_sqlite_persistence_survives_new_store(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "prr.db", casefiles_dir=tmp_path / "casefiles")
    store = Store(settings.db_path)
    store.create_case("demo", "Demo Agency", "Demo request")
    events = import_path("demo", Path("tests/fixtures/defective_estimate.txt"), store, settings)
    route_event("demo", events[0].event_id, store, settings)

    reopened = Store(settings.db_path)

    assert reopened.get_case("demo").agency == "Demo Agency"
    assert reopened.get_event("demo", events[0].event_id).summary == "Revised cost estimate"
    assert len(reopened.list_tasks()) == 1


def test_no_action_does_not_create_review_task(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "prr.db", casefiles_dir=tmp_path / "casefiles")
    store = Store(settings.db_path)
    store.create_case("demo", "Demo Agency", "Demo request")

    events = import_path("demo", Path("tests/fixtures/no_action.txt"), store, settings)
    result = route_event("demo", events[0].event_id, store, settings)

    assert result.status == "updated_no_action_required"
    assert store.list_tasks() == []
