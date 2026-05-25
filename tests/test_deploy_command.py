from pathlib import Path

from prr_pressure_cooker.cli import (
    _case_id_from_envelope,
    _resolve_case_id_from_envelope,
    build_parser,
)
from prr_pressure_cooker.config import Settings
from prr_pressure_cooker.ingest import record_case_external_refs
from prr_pressure_cooker.storage import Store


def test_koyeb_deploy_command_shape(capsys):
    parser = build_parser()
    args = parser.parse_args(["deploy", "koyeb"])
    args.func(args)
    output = capsys.readouterr().out

    assert "koyeb deploy . prr-pressure-cooker/worker" in output
    assert "--type worker" in output
    assert "MISTRAL_API_KEY={{ secret.MISTRAL_API_KEY }}" in output
    assert "DEPLOYMENT_NAME=prr-pressure-cooker-prod" in output
    assert "--volumes prr-data:/data" in output
    assert "--scale 1" in output
    assert "--instance-type small" in output
    assert "--archive-ignore-dir .venv" in output
    assert "--archive-ignore-dir casefiles" in output
    assert "--archive-ignore-dir var" in output
    assert "PRR_DB_PATH=/data/prr.db" in output
    assert "PRR_CASEFILES_DIR=/data/casefiles" in output
    assert "PRR_WORKFLOW_MODE=step" in output


def test_koyeb_deploy_command_can_wait(capsys):
    parser = build_parser()
    args = parser.parse_args(["deploy", "koyeb", "--wait", "--wait-timeout", "15m"])
    args.func(args)
    output = capsys.readouterr().out

    assert "--wait --wait-timeout 15m" in output


def test_pull_himalaya_parser_accepts_live_options():
    parser = build_parser()
    args = parser.parse_args(
        [
            "pull-himalaya",
            "seminole-scout",
            "--ssh-target",
            "drake@omarchy-mbp.tail7e7910.ts.net",
            "--folder",
            "Folders/Public Records Requests",
            "--message-id",
            "1",
            "--create-case",
            "--route",
        ]
    )

    assert args.case_id == "seminole-scout"
    assert args.ssh_target == "drake@omarchy-mbp.tail7e7910.ts.net"
    assert args.message_id == "1"
    assert args.route is True


def test_pull_himalaya_batch_parser_defaults_to_all_mail():
    parser = build_parser()
    args = parser.parse_args(
        [
            "pull-himalaya-batch",
            "--ssh-target",
            "drake@omarchy-mbp.tail7e7910.ts.net",
            "--limit",
            "12",
            "--route",
        ]
    )

    assert args.folder == "All Mail"
    assert args.query == "subject PRR order by date desc"
    assert args.limit == 12
    assert args.route is True


def test_case_id_derivation_handles_portal_request_numbers():
    assert (
        _case_id_from_envelope(
            {
                "id": "1",
                "subject": "Orange County Sheriff's Office public records request #26-17289",
            },
            "allmail-records",
        )
        == "allmail-records-26-17289"
    )
    assert (
        _case_id_from_envelope(
            {"id": "2", "subject": "Osceola County Corrections Records Request CORR-2026-300"},
            "allmail-records",
        )
        == "allmail-records-corr-2026-300"
    )
    assert (
        _case_id_from_envelope(
            {"id": "3", "subject": "[Records Center] PUBLIC RECORD REQUEST :: P041212-031826"},
            "allmail-records",
        )
        == "allmail-records-p041212-031826"
    )
    assert (
        _case_id_from_envelope(
            {"id": "4", "subject": "Re: Request for oversight regarding Scout records request"},
            "allmail-records",
        )
        == "allmail-records-unknown-agency-request-for-oversight-regarding-scout-records-request"
    )


def test_case_id_resolution_reuses_durable_external_refs(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "prr.db", casefiles_dir=tmp_path / "casefiles")
    store = Store(settings.db_path)
    store.create_case("canonical-scout", "Lynx", "Scout microtransit")
    record_case_external_refs(
        "canonical-scout",
        store,
        "test",
        "Original agency receipt for PRR-163721",
    )

    case_id = _resolve_case_id_from_envelope(
        {
            "id": "99",
            "subject": "Re: PRR-163721 revised estimate",
            "from": {"name": "Records Unit", "addr": "records@example.gov"},
        },
        "allmail-records",
        store,
    )

    assert case_id == "canonical-scout"
