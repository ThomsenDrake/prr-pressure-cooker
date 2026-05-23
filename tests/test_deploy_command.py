from prr_pressure_cooker.cli import _case_id_from_envelope, build_parser


def test_koyeb_deploy_command_shape(capsys):
    parser = build_parser()
    args = parser.parse_args(["deploy", "koyeb"])
    args.func(args)
    output = capsys.readouterr().out

    assert "koyeb deploy . prr-pressure-cooker/worker" in output
    assert "--type WORKER" in output
    assert "MISTRAL_API_KEY={{ secret.MISTRAL_API_KEY }}" in output
    assert "DEPLOYMENT_NAME=prr-pressure-cooker-prod" in output


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
