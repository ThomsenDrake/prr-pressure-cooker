from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
from pathlib import Path

from prr_pressure_cooker.adapters import SSHHimalayaAdapter
from prr_pressure_cooker.config import Settings
from prr_pressure_cooker.ids import kebab_slug, utc_now
from prr_pressure_cooker.ingest import (
    export_casefile_indexes,
    import_path,
    rebuild_casefile_indexes,
    record_case_external_refs,
    resolve_case_id_from_external_refs,
)
from prr_pressure_cooker.models import CaseWorkflowSignal, PushedEventPayload, ReviewStatus
from prr_pressure_cooker.service import (
    apply_review_choice,
    build_pushed_event_payload,
    deadline_elapsed_event,
    reroute_batch,
    reroute_case,
    route_event,
    scan_deadlines,
)
from prr_pressure_cooker.storage import Store
from prr_pressure_cooker.workflow_runtime import workflow_runtime


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prr")
    subparsers = parser.add_subparsers(required=True)

    init_case = subparsers.add_parser("init-case", help="create or update a PRR case")
    init_case.add_argument("case_id")
    init_case.add_argument("--agency", required=True)
    init_case.add_argument("--request-title", required=True)
    init_case.set_defaults(func=cmd_init_case)

    import_cmd = subparsers.add_parser("import", help="import evidence into a case")
    import_cmd.add_argument("case_id")
    import_cmd.add_argument("path", type=Path)
    import_cmd.set_defaults(func=cmd_import)

    ingest_push = subparsers.add_parser(
        "ingest-push", help="import raw evidence and signal the case workflow"
    )
    ingest_push.add_argument("case_id")
    ingest_push.add_argument("path", type=Path)
    ingest_push.add_argument("--create-case", action="store_true")
    ingest_push.add_argument("--agency")
    ingest_push.add_argument("--request-title")
    ingest_push.add_argument("--backend", choices=["local", "mistral"])
    ingest_push.add_argument("--no-signal", action="store_true")
    ingest_push.set_defaults(func=cmd_ingest_push)

    pull = subparsers.add_parser("pull-himalaya", help="pull a PRR message from remote Himalaya")
    pull.add_argument("case_id")
    pull.add_argument("--ssh-target")
    pull.add_argument("--folder")
    pull.add_argument("--account")
    pull.add_argument("--message-id")
    pull.add_argument("--query", default="order by date desc")
    pull.add_argument("--limit", type=int, default=10)
    pull.add_argument("--route", action="store_true")
    pull.add_argument("--create-case", action="store_true")
    pull.add_argument("--agency")
    pull.add_argument("--request-title")
    pull.set_defaults(func=cmd_pull_himalaya)

    batch = subparsers.add_parser(
        "pull-himalaya-batch", help="pull multiple PRR messages from remote Himalaya"
    )
    batch.add_argument("--ssh-target")
    batch.add_argument("--folder", default="All Mail")
    batch.add_argument("--account")
    batch.add_argument("--query", default="subject PRR order by date desc")
    batch.add_argument("--limit", type=int, default=25)
    batch.add_argument("--route", action="store_true")
    batch.add_argument("--case-prefix", default="prr")
    batch.set_defaults(func=cmd_pull_himalaya_batch)

    route_cmd = subparsers.add_parser("route", help="route an imported event through the ladder")
    route_cmd.add_argument("case_id")
    route_cmd.add_argument("--event", required=True, dest="event_id")
    route_cmd.set_defaults(func=cmd_route)

    casefile = subparsers.add_parser("casefile", help="work with casefile evidence indexes")
    casefile_sub = casefile.add_subparsers(required=True)
    rebuild_indexes = casefile_sub.add_parser("rebuild-indexes")
    rebuild_indexes.add_argument("case_id")
    rebuild_indexes.set_defaults(func=cmd_casefile_rebuild_indexes)
    export_indexes = casefile_sub.add_parser("export-indexes")
    export_indexes.add_argument("case_id")
    export_indexes.set_defaults(func=cmd_casefile_export_indexes)

    workflow = subparsers.add_parser("workflow", help="work with case workflows")
    workflow_sub = workflow.add_subparsers(required=True)
    workflow_start = workflow_sub.add_parser("start-case")
    workflow_start.add_argument("case_id")
    workflow_start.add_argument("--backend", choices=["local", "mistral"])
    workflow_start.set_defaults(func=cmd_workflow_start_case)
    workflow_signal = workflow_sub.add_parser("signal-event")
    workflow_signal.add_argument("case_id")
    workflow_signal.add_argument("--event", required=True, dest="event_id")
    workflow_signal.add_argument(
        "--signal",
        default="agency_event",
        choices=[
            "agency_event",
            "deadline_elapsed",
            "human_reply",
            "payment",
            "records_release",
        ],
    )
    workflow_signal.add_argument("--push-payload", action="store_true")
    workflow_signal.add_argument("--backend", choices=["local", "mistral"])
    workflow_signal.set_defaults(func=cmd_workflow_signal_event)
    workflow_status = workflow_sub.add_parser("status")
    workflow_status.add_argument("case_id")
    workflow_status.add_argument("--backend", choices=["local", "mistral"])
    workflow_status.set_defaults(func=cmd_workflow_status)
    workflow_resolve = workflow_sub.add_parser("resolve-case")
    workflow_resolve.add_argument("case_id")
    workflow_resolve.add_argument("--backend", choices=["local", "mistral"])
    workflow_resolve.set_defaults(func=cmd_workflow_resolve_case)

    reroute_case_cmd = subparsers.add_parser(
        "reroute-case", help="chronologically reroute one case"
    )
    reroute_case_cmd.add_argument("case_id")
    reroute_case_cmd.add_argument("--replace-tasks", action="store_true")
    reroute_case_cmd.set_defaults(func=cmd_reroute_case)

    reroute_batch_cmd = subparsers.add_parser(
        "reroute-batch", help="chronologically reroute cases matching a prefix"
    )
    reroute_batch_cmd.add_argument("--case-prefix", default="allmail-")
    reroute_batch_cmd.add_argument("--replace-tasks", action="store_true")
    reroute_batch_cmd.set_defaults(func=cmd_reroute_batch)

    review = subparsers.add_parser("review", help="work with human review tasks")
    review_sub = review.add_subparsers(required=True)
    review_list = review_sub.add_parser("list")
    review_list.add_argument("--status", default=ReviewStatus.PENDING.value)
    review_list.set_defaults(func=cmd_review_list)
    for action in ["approve", "revise", "defer", "cancel"]:
        action_cmd = review_sub.add_parser(action)
        action_cmd.add_argument("task_id")
        action_cmd.add_argument("--note", default=None)
        action_cmd.set_defaults(func=cmd_review_action, action=action)

    worker = subparsers.add_parser("worker", help="start the Mistral Workflows worker")
    worker.set_defaults(func=cmd_worker)

    deadline = subparsers.add_parser("deadline", help="work with case deadlines")
    deadline_sub = deadline.add_subparsers(required=True)
    deadline_scan = deadline_sub.add_parser("scan")
    deadline_scan.add_argument("--emit-events", action="store_true")
    deadline_scan.add_argument("--backend", choices=["local", "mistral"])
    deadline_scan.set_defaults(func=cmd_deadline_scan)

    deploy = subparsers.add_parser("deploy", help="deployment helpers")
    deploy_sub = deploy.add_subparsers(required=True)
    koyeb = deploy_sub.add_parser("koyeb", help="print or execute Koyeb worker deploy command")
    koyeb.add_argument("--app", default="prr-pressure-cooker")
    koyeb.add_argument("--service", default="worker")
    koyeb.add_argument("--deployment-name", default="prr-pressure-cooker-prod")
    koyeb.add_argument("--volume", default="prr-data")
    koyeb.add_argument("--instance-type", default="small")
    koyeb.add_argument("--wait", action="store_true")
    koyeb.add_argument("--wait-timeout", default="10m")
    koyeb.add_argument("--execute", action="store_true")
    koyeb.set_defaults(func=cmd_deploy_koyeb)

    return parser


def _store_settings() -> tuple[Store, Settings]:
    settings = Settings.from_env()
    return Store(settings.db_path), settings


def cmd_init_case(args) -> None:
    store, _settings = _store_settings()
    case = store.create_case(args.case_id, args.agency, args.request_title)
    print(case.model_dump_json(indent=2))


def cmd_import(args) -> None:
    store, settings = _store_settings()
    events = import_path(args.case_id, args.path, store, settings)
    print(json.dumps([event.model_dump(mode="json") for event in events], indent=2))


def cmd_ingest_push(args) -> None:
    store, settings = _store_settings()
    try:
        store.get_case(args.case_id)
    except KeyError:
        if not args.create_case:
            raise SystemExit(
                f"case {args.case_id!r} does not exist; pass --create-case with "
                "--agency and --request-title"
            ) from None
        store.create_case(
            args.case_id,
            args.agency or "Unknown agency",
            args.request_title or f"Pushed ingest for {args.case_id}",
        )

    events = import_path(args.case_id, args.path, store, settings)
    signaled = []
    if not args.no_signal:
        runtime = workflow_runtime(store, settings, args.backend)
        selected_backend = args.backend or settings.workflow_backend
        signaled = []
        for event in events:
            event_payload = (
                build_pushed_event_payload(args.case_id, event.event_id, store)
                if selected_backend == "mistral"
                else None
            )
            signaled.append(
                runtime.signal_event(
                    args.case_id,
                    CaseWorkflowSignal(
                        event_id=event.event_id,
                        signal_type="agency_event",
                        event_payload=event_payload,
                    ),
                )
            )
    print(
        json.dumps(
            {
                "case_id": args.case_id,
                "imported": [event.model_dump(mode="json") for event in events],
                "signaled": signaled,
            },
            indent=2,
        )
    )


def cmd_pull_himalaya(args) -> None:
    store, settings = _store_settings()
    ssh_target = args.ssh_target or settings.himalaya_ssh_target
    if not ssh_target:
        raise SystemExit("set --ssh-target or HIMALAYA_SSH_TARGET")

    folder = args.folder or settings.himalaya_folder
    adapter = SSHHimalayaAdapter(
        ssh_target=ssh_target,
        folder=folder,
        account=args.account or settings.himalaya_account,
    )

    envelope = _select_envelope(adapter, args.message_id, args.query, args.limit)
    case_exists = True
    try:
        store.get_case(args.case_id)
    except KeyError:
        case_exists = False

    if not case_exists:
        if not args.create_case:
            raise SystemExit(
                f"case {args.case_id!r} does not exist; pass --create-case with --agency "
                "and --request-title"
            )
        store.create_case(
            args.case_id,
            args.agency or _agency_from_envelope(envelope),
            args.request_title or envelope.get("subject") or f"Himalaya message {envelope['id']}",
        )
    record_case_external_refs(
        args.case_id,
        store,
        "himalaya-envelope",
        envelope.get("subject") or "",
        _agency_from_envelope(envelope),
    )

    incoming_dir = settings.casefiles_dir / args.case_id / "incoming" / "himalaya"
    message_id = str(envelope["id"])
    destination = incoming_dir / f"himalaya_{message_id}.eml"
    adapter.export_message(message_id, destination)
    events = import_path(args.case_id, destination, store, settings)

    payload = {
        "envelope": envelope,
        "imported": [event.model_dump(mode="json") for event in events],
        "routed": [],
    }
    if args.route:
        payload["routed"] = [
            route_event(args.case_id, event.event_id, store, settings).model_dump(mode="json")
            for event in events
        ]
    print(json.dumps(payload, indent=2))


def cmd_pull_himalaya_batch(args) -> None:
    store, settings = _store_settings()
    ssh_target = args.ssh_target or settings.himalaya_ssh_target
    if not ssh_target:
        raise SystemExit("set --ssh-target or HIMALAYA_SSH_TARGET")

    adapter = SSHHimalayaAdapter(
        ssh_target=ssh_target,
        folder=args.folder or settings.himalaya_folder,
        account=args.account or settings.himalaya_account,
    )
    envelopes = adapter.list_envelopes(query=args.query, limit=args.limit)
    payload = []
    for envelope in envelopes:
        case_id = _resolve_case_id_from_envelope(envelope, args.case_prefix, store)
        try:
            store.get_case(case_id)
        except KeyError:
            store.create_case(
                case_id,
                _agency_from_envelope(envelope),
                envelope.get("subject") or f"Himalaya message {envelope['id']}",
            )
        record_case_external_refs(
            case_id,
            store,
            "himalaya-envelope",
            envelope.get("subject") or "",
            _agency_from_envelope(envelope),
        )

        folder_slug = kebab_slug(args.folder)
        message_id = str(envelope["id"])
        destination = (
            settings.casefiles_dir
            / case_id
            / "incoming"
            / "himalaya"
            / folder_slug
            / f"himalaya_{message_id}.eml"
        )
        adapter.export_message(message_id, destination)
        events = import_path(case_id, destination, store, settings)
        routed = []
        if args.route:
            routed = [
                route_event(case_id, event.event_id, store, settings).model_dump(mode="json")
                for event in events
            ]
        payload.append(
            {
                "case_id": case_id,
                "folder": args.folder,
                "envelope": envelope,
                "imported": [event.model_dump(mode="json") for event in events],
                "routed": routed,
            }
        )
    print(json.dumps(payload, indent=2))


def cmd_route(args) -> None:
    store, settings = _store_settings()
    result = route_event(args.case_id, args.event_id, store, settings)
    print(result.model_dump_json(indent=2))


def cmd_casefile_rebuild_indexes(args) -> None:
    store, settings = _store_settings()
    result = rebuild_casefile_indexes(args.case_id, store, settings)
    print(json.dumps(result, indent=2))


def cmd_casefile_export_indexes(args) -> None:
    store, settings = _store_settings()
    paths = export_casefile_indexes(args.case_id, store, settings)
    print(json.dumps({"case_id": args.case_id, "index_paths": paths}, indent=2))


def cmd_workflow_start_case(args) -> None:
    store, settings = _store_settings()
    result = workflow_runtime(store, settings, args.backend).start_case(args.case_id)
    print(json.dumps(result, indent=2))


def cmd_workflow_signal_event(args) -> None:
    store, settings = _store_settings()
    selected_backend = args.backend or settings.workflow_backend
    event_payload = (
        build_pushed_event_payload(args.case_id, args.event_id, store)
        if args.push_payload or selected_backend == "mistral"
        else None
    )
    result = workflow_runtime(store, settings, args.backend).signal_event(
        args.case_id,
        CaseWorkflowSignal(
            event_id=args.event_id,
            signal_type=args.signal,
            event_payload=event_payload,
        ),
    )
    print(json.dumps(result, indent=2))


def cmd_workflow_status(args) -> None:
    store, settings = _store_settings()
    result = workflow_runtime(store, settings, args.backend).status(args.case_id)
    print(json.dumps(result, indent=2))


def cmd_workflow_resolve_case(args) -> None:
    store, settings = _store_settings()
    result = workflow_runtime(store, settings, args.backend).resolve_case(args.case_id)
    print(json.dumps(result, indent=2))


def cmd_reroute_case(args) -> None:
    store, settings = _store_settings()
    result = reroute_case(args.case_id, store, settings, replace_tasks=args.replace_tasks)
    print(json.dumps(result, indent=2))


def cmd_reroute_batch(args) -> None:
    store, settings = _store_settings()
    result = reroute_batch(args.case_prefix, store, settings, replace_tasks=args.replace_tasks)
    print(json.dumps(result, indent=2))


def cmd_review_list(args) -> None:
    store, _settings = _store_settings()
    tasks = store.list_tasks(status=args.status if args.status != "all" else None)
    print(json.dumps([task.model_dump(mode="json") for task in tasks], indent=2))


def cmd_review_action(args) -> None:
    store, _settings = _store_settings()
    task = store.get_task(args.task_id)
    status_map = {
        "approve": ReviewStatus.APPROVED,
        "revise": ReviewStatus.REVISED,
        "defer": ReviewStatus.DEFERRED,
        "cancel": ReviewStatus.CANCELED,
    }
    try:
        task, _interaction, _judgment = apply_review_choice(
            task, status_map[args.action], args.note, store
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    print(task.model_dump_json(indent=2))


def cmd_worker(_args) -> None:
    import asyncio

    from prr_pressure_cooker.worker import run_worker

    asyncio.run(run_worker())


def cmd_deadline_scan(args) -> None:
    store, settings = _store_settings()
    selected_backend = args.backend or settings.workflow_backend
    if args.emit_events and selected_backend == "mistral":
        result = _scan_deadlines_into_runtime(store, settings, args.backend)
    else:
        result = scan_deadlines(store, settings, emit_events=args.emit_events)
    print(json.dumps(result, indent=2))


def _scan_deadlines_into_runtime(store: Store, settings: Settings, backend: str | None) -> dict:
    now = utc_now()
    runtime = workflow_runtime(store, settings, backend)
    items = []
    for deadline in store.due_deadlines(now):
        event = deadline_elapsed_event(deadline, now)
        store.save_event(event)
        signal = CaseWorkflowSignal(
            event_id=event.event_id,
            signal_type="deadline_elapsed",
            event_payload=PushedEventPayload(
                event=event,
                case=store.get_case(deadline.case_id),
            ),
        )
        signaled = runtime.signal_event(deadline.case_id, signal)
        store.set_deadline_status(deadline.deadline_id, "emitted")
        items.append(
            {
                "deadline": deadline.model_dump(mode="json"),
                "event": event.model_dump(mode="json"),
                "signal": signaled,
            }
        )
    return {
        "due": len(items),
        "emit_events": True,
        "backend": selected_backend_label(backend, settings),
        "items": items,
    }


def selected_backend_label(backend: str | None, settings: Settings) -> str:
    return backend or settings.workflow_backend


def cmd_deploy_koyeb(args) -> None:
    command = [
        "koyeb",
        "deploy",
        ".",
        f"{args.app}/{args.service}",
        "--type",
        "worker",
        "--archive-builder",
        "docker",
        "--archive-ignore-dir",
        ".git",
        "--archive-ignore-dir",
        ".venv",
        "--archive-ignore-dir",
        "casefiles",
        "--archive-ignore-dir",
        "var",
        "--volumes",
        f"{args.volume}:/data",
        "--scale",
        "1",
        "--instance-type",
        args.instance_type,
        "--env",
        "MISTRAL_API_KEY={{ secret.MISTRAL_API_KEY }}",
        "--env",
        f"DEPLOYMENT_NAME={args.deployment_name}",
        "--env",
        "PRR_DB_PATH=/data/prr.db",
        "--env",
        "PRR_CASEFILES_DIR=/data/casefiles",
    ]
    if args.wait:
        command.extend(["--wait", "--wait-timeout", args.wait_timeout])
    print(" ".join(shlex.quote(part) for part in command))
    if args.execute:
        subprocess.run(command, check=True)


def _select_envelope(
    adapter: SSHHimalayaAdapter, message_id: str | None, query: str, limit: int
) -> dict:
    envelopes = adapter.list_envelopes(query=query, limit=limit)
    if not envelopes:
        raise SystemExit("no Himalaya envelopes matched the query")
    if message_id is None:
        return envelopes[0]
    for envelope in envelopes:
        if str(envelope.get("id")) == str(message_id):
            return envelope
    return {"id": message_id, "subject": f"Himalaya message {message_id}"}


def _agency_from_envelope(envelope: dict) -> str:
    sender = envelope.get("from") or {}
    if isinstance(sender, dict):
        return sender.get("name") or sender.get("addr") or "Unknown agency"
    return str(sender)


def _resolve_case_id_from_envelope(envelope: dict, prefix: str, store: Store) -> str:
    existing_case_id = resolve_case_id_from_external_refs(
        store,
        envelope.get("subject") or "",
        _agency_from_envelope(envelope),
    )
    if existing_case_id:
        return existing_case_id
    return _case_id_from_envelope(envelope, prefix)


def _case_id_from_envelope(envelope: dict, prefix: str) -> str:
    subject = envelope.get("subject") or ""
    patterns = [
        (r"\bPRR[-\s]*([0-9]+)\b", lambda match: match.group(1)),
        (r"\bpublic records request #([0-9]+-[0-9]+)\b", lambda match: match.group(1)),
        (r"\brecords request ([A-Z]+-[0-9]+-[0-9]+)\b", lambda match: match.group(1)),
        (r"\b(CORR-[0-9]+-[0-9]+)\b", lambda match: match.group(1)),
        (r"\b(W[0-9]+-[0-9]+)\b", lambda match: match.group(1)),
    ]
    for pattern, build_id in patterns:
        match = re.search(pattern, subject, flags=re.IGNORECASE)
        if match:
            return f"{prefix}-{kebab_slug(build_id(match))}"
    sender = _agency_from_envelope(envelope)
    return kebab_slug(f"{prefix}-{sender}-{subject}", fallback=f"{prefix}-message-{envelope['id']}")
