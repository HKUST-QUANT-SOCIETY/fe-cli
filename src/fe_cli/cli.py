"""Command line entry point for local research and explicit online operations."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from fe_cli import __version__
from fe_cli.backend import F01_FORMULA, DemoBackend
from fe_cli.catalog import FactorDefinition
from fe_cli.config import ConnectionConfig, load_config, save_config
from fe_cli.date_range import DateRange, DateRangeStore
from fe_cli.execution import RunService
from fe_cli.history import recent_history
from fe_cli.remote import RemoteClient
from fe_cli.resources import ResourceRequest, ResourceStore, normalize_node
from fe_cli.runtime_settings import RuntimeSettings, RuntimeStore
from fe_cli.updates import UpdateChecker, UpdateError, version_key
from fe_cli.workspace import Workspace

_ONLINE_COMMANDS = {
    "envs": "cluster-envs",
    "jobs": "cluster-status",
    "history": "cluster-history",
}


def _parser() -> argparse.ArgumentParser:
    program = "fe-cli" if Path(sys.argv[0]).name in {"fe-cli", "__main__.py"} else "fe"
    parser = argparse.ArgumentParser(
        prog=program,
        description="Factor research terminal — local demo or explicit online operations.",
    )
    parser.add_argument("--version", action="version", version=f"{program} {__version__}")
    maintenance = parser.add_mutually_exclusive_group()
    maintenance.add_argument("--check-update", action="store_true", help="check for a CLI update")
    maintenance.add_argument("--update", action="store_true", help="install the latest stable CLI")
    parser.add_argument(
        "--no-update-check", action="store_true", help="disable background CLI update checks"
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--demo", action="store_true", help="use the local demo (default)")
    modes.add_argument("--online", action="store_true", help="use explicit remote operations")
    parser.add_argument("--config", type=Path, help="local connection configuration file")
    parser.add_argument("--resource-config", type=Path, help="local resource preference file")
    parser.add_argument(
        "--range-config", type=Path, help="local default date range preference file"
    )
    parser.add_argument("--state-dir", type=Path, help="directory for local demonstration state")
    parser.add_argument(
        "--workspace", type=Path, help="local factor workspace (default: ~/fe-workspace)"
    )
    commands = parser.add_subparsers(dest="command")
    initialize = commands.add_parser("init", help="initialize a local factor workspace")
    initialize.add_argument("directory", nargs="?", type=Path)
    factor = commands.add_parser("factor", help="manage the local CSV factor library")
    factors = factor.add_subparsers(dest="factor_command", required=True)
    factors.add_parser("list", help="list local factor definitions")
    show = factors.add_parser("show", help="show one local factor definition")
    show.add_argument("factor_id")
    add = factors.add_parser("add", help="add a local factor definition")
    add.add_argument("factor_id")
    add.add_argument("--expression", required=True, help="factor DSL text (never executed locally)")
    add.add_argument("--name")
    add.add_argument("--description", default="")
    add.add_argument("--disabled", action="store_true")
    update = factors.add_parser("update", help="update a local factor definition")
    update.add_argument("factor_id")
    update.add_argument("--name")
    update.add_argument("--expression")
    update.add_argument("--description")
    enabled = update.add_mutually_exclusive_group()
    enabled.add_argument("--enabled", dest="enabled", action="store_true")
    enabled.add_argument("--disabled", dest="enabled", action="store_false")
    update.set_defaults(enabled=None)
    delete = factors.add_parser("delete", help="delete a local definition; keep existing outputs")
    delete.add_argument("factor_id")
    configure = commands.add_parser("configure", help="save local SSH connection settings")
    configure.add_argument("--host", required=True)
    configure.add_argument("--user", default="")
    configure.add_argument("--ssh-executable", default="ssh")
    configure.add_argument("--identity-file")
    configure.add_argument("--port", type=int, default=22)
    configure.add_argument("--connect-timeout", type=int, default=10)
    configure.add_argument("--timeout", type=int, default=30)
    runtime = commands.add_parser("configure-run", help="save local server runtime settings")
    runtime.add_argument("--remote-python", required=True, help="existing server Python path")
    runtime.add_argument("--data-root", required=True, help="approved server data directory")
    runtime.add_argument("--node", default="A", metavar="A|C|shared")
    runtime.add_argument("--release-root", required=True, help="approved public release directory")
    runtime.add_argument(
        "--release-sha256", required=True, help="frozen public release manifest SHA-256"
    )
    runtime.add_argument("--env", default="research", dest="environment")
    runtime.add_argument("--time", default="00:30:00", dest="time_limit")
    node = commands.add_parser("node", help="show or save the local node selection")
    node.add_argument("node", nargs="?", metavar="A|C")
    resources = commands.add_parser("resources", help="show or save CPUs paired with 3 GB each")
    resources.add_argument("cpus", nargs="?", type=int, metavar="CPUS")
    date_range = commands.add_parser("range", help="show or save the default date range")
    date_range.add_argument("start", nargs="?", metavar="START")
    date_range.add_argument("end", nargs="?", metavar="END")
    commands.add_parser(
        "setup", help="diagnose connection and SSH authentication (requires --online)"
    )
    commands.add_parser("envs", help="list remote environments (requires --online)")
    history = commands.add_parser(
        "history", help="show latest 10 history records or full server history (requires --online)"
    )
    history.add_argument("scope", nargs="?", choices=["full"], help="show all server history")
    run = commands.add_parser("run", help="submit one job (demo by default; --online for real)")
    run.add_argument("factor_id", nargs="?", help="enabled factor ID from the local library")
    run.add_argument("--factor", help="factor ID (legacy option)")
    run.add_argument("--formula", help="formula override; ad hoc text is supported in demo mode")
    run.add_argument("--fail", action="store_true", help="simulate a failed job")
    prepare = commands.add_parser("prepare", help="prepare one local run bundle without connecting")
    prepare.add_argument("factor_id", help="enabled factor ID from the local library")
    prepare.add_argument("--formula", help="override the formula in this run snapshot only")
    for command in (run, prepare):
        command.add_argument("--start", help="override the saved start date for this run")
        command.add_argument("--end", help="override the saved end date for this run")
        command.add_argument("--node", metavar="A|C", help="override the saved node for this job")
        command.add_argument("--cpus", type=int, help="override CPUs for this job (3 GB per CPU)")
    commands.add_parser("jobs", help="list simulated jobs or query online cluster status")
    cancel = commands.add_parser("cancel", help="cancel a simulated job")
    cancel.add_argument("job_id")
    results = commands.add_parser("results", help="show a demo report or collect an online result")
    results.add_argument("job_id")
    results.add_argument("--request-id", help="identify the saved online run")
    collect = commands.add_parser(
        "collect", help="download and commit one result (requires --online)"
    )
    collect.add_argument("job_id")
    collect.add_argument("--request-id", help="identify the saved online run")
    imported = commands.add_parser(
        "import-results", help="verify and commit a local result archive"
    )
    imported.add_argument("archive", type=Path)
    imported.add_argument("--request-id", help="require this saved run's identity")
    commands.add_parser("runs", help="list local prepared, submitted, and committed runs")
    return parser


def _print_json(key: str, value: Any) -> None:
    print(json.dumps({"demo": True, key: value}, ensure_ascii=False))


def _factor_record(definition: FactorDefinition) -> dict[str, Any]:
    return {**asdict(definition), "definition_hash": definition.definition_hash}


def _initialize(args: argparse.Namespace) -> None:
    if args.directory is not None and args.workspace is not None:
        if args.directory.expanduser().resolve() != args.workspace.expanduser().resolve():
            raise ValueError("init DIRECTORY and --workspace must identify the same directory")
    workspace = Workspace(args.directory if args.directory is not None else args.workspace)
    result = workspace.initialize()
    print(json.dumps({
        "workspace": str(result.workspace),
        "csv": str(result.csv_path),
        "readme": str(result.readme_path),
        "created": result.created,
        "values_directory": str(workspace.root / "values"),
        "runs_directory": str(workspace.root / "runs"),
    }, ensure_ascii=False))
    print(
        f"Edit {result.csv_path} to maintain factor definitions. "
        f"Next: fe --workspace {str(workspace.root)!r} factor list",
        file=sys.stderr,
    )


def _factors(args: argparse.Namespace) -> None:
    catalog = Workspace(args.workspace).catalog
    command = args.factor_command
    if command == "list":
        payload = {"factors": [_factor_record(item) for item in catalog.list()]}
    elif command == "show":
        payload = {"factor": _factor_record(catalog.get(args.factor_id))}
    elif command == "add":
        definition = FactorDefinition(
            factor_id=args.factor_id,
            name=args.factor_id if args.name is None else args.name,
            expression=args.expression,
            enabled=not args.disabled,
            description=args.description,
        )
        payload = {"factor": _factor_record(catalog.add(definition))}
    elif command == "update":
        fields = {name: getattr(args, name) for name in (
            "name", "expression", "enabled", "description"
        ) if getattr(args, name) is not None}
        if not fields:
            raise ValueError("factor update requires --name, --expression, --description, "
                             "--enabled, or --disabled")
        payload = {"factor": _factor_record(catalog.update(args.factor_id, **fields))}
    else:
        payload = {"deleted": _factor_record(catalog.delete(args.factor_id))}
    print(json.dumps({"kind": "local", **payload}, ensure_ascii=False))


def _run_definition(args: argparse.Namespace) -> tuple[str, str]:
    if args.factor_id is not None and args.factor is not None and args.factor_id != args.factor:
        raise ValueError("run FACTOR_ID and --factor must identify the same factor")
    factor_id = args.factor_id if args.factor_id is not None else args.factor
    if args.formula is not None and args.factor_id is None:
        return factor_id or "f01", args.formula
    if factor_id is None:
        return "f01", F01_FORMULA
    definition = Workspace(args.workspace).catalog.get(factor_id)
    if not definition.enabled:
        raise ValueError(f"Factor {factor_id!r} is disabled; enable it before running")
    return definition.factor_id, (
        definition.expression if args.formula is None else args.formula
    )


def _selected_definition(args: argparse.Namespace) -> FactorDefinition:
    legacy_id = getattr(args, "factor", None)
    if args.factor_id is not None and legacy_id is not None and args.factor_id != legacy_id:
        raise ValueError("run FACTOR_ID and --factor must identify the same factor")
    factor_id = args.factor_id if args.factor_id is not None else legacy_id
    if factor_id is None:
        raise ValueError("Online run requires an enabled factor ID from the local library")
    definition = Workspace(args.workspace).catalog.get(factor_id)
    if not definition.enabled:
        raise ValueError(f"Factor {factor_id!r} is disabled; enable it before running")
    return definition if args.formula is None else replace(definition, expression=args.formula)


def _run_resources(args: argparse.Namespace) -> ResourceRequest:
    current = ResourceStore(path=args.resource_config).load()
    return ResourceRequest(
        node=current.node if args.node is None else args.node,
        cpus=current.cpus if args.cpus is None else args.cpus,
    )


def _run_date_range(args: argparse.Namespace) -> DateRange:
    current = DateRangeStore(path=args.range_config).load()
    return DateRange(
        start=current.start if args.start is None else args.start,
        end=current.end if args.end is None else args.end,
    )


def _local_run_command(args: argparse.Namespace) -> None:
    workspace = Workspace(args.workspace)
    if args.command == "configure-run":
        settings = RuntimeSettings(
            remote_python=args.remote_python,
            data_root=args.data_root,
            node="shared" if args.node.strip().lower() == "shared" else normalize_node(args.node),
            environment=args.environment,
            time_limit=args.time_limit,
            release_root=args.release_root,
            release_sha256=args.release_sha256,
        )
        path = RuntimeStore(workspace.root).save(settings)
        payload = {"configured": True, "config": str(path), "runtime": asdict(settings)}
    elif args.command == "prepare":
        factor = _selected_definition(args)
        resources = _run_resources(args)
        window = _run_date_range(args)
        payload = {"run": RunService(workspace).prepare(
            factor, resources, window.start, window.end
        )}
    elif args.command == "import-results":
        payload = {"receipt": RunService(workspace).import_results(
            args.archive, request_id=args.request_id
        )}
    else:
        payload = {"runs": RunService(workspace).list_runs()}
    print(json.dumps({"demo": False, **payload}, ensure_ascii=False))


def _configure(args: argparse.Namespace) -> None:
    config = ConnectionConfig(
        host=args.host,
        user=args.user,
        ssh_executable=args.ssh_executable,
        identity_file=args.identity_file,
        port=args.port,
        connect_timeout=args.connect_timeout,
        timeout=args.timeout,
    )
    path = save_config(config, args.config)
    print(json.dumps({"configured": True, "config": str(path)}, ensure_ascii=False))


def _resources(args: argparse.Namespace) -> None:
    store = ResourceStore(path=args.resource_config)
    current = store.load()
    node = args.node if args.command == "node" else None
    cpus = args.cpus if args.command == "resources" else None
    request = ResourceRequest(
        node=current.node if node is None else node,
        cpus=current.cpus if cpus is None else cpus,
    )
    saved = node is not None or cpus is not None
    if saved:
        store.save(request)
    print(json.dumps({
        "resources": {
            "node": request.node,
            "cpus": request.cpus,
            "memory": request.memory,
            "memory_gb": request.memory_gb,
        },
        "saved": saved,
    }, ensure_ascii=False))


def _online(args: argparse.Namespace) -> int:
    if args.command == "cancel":
        raise ValueError(
            "Online cancellation is disabled; cancel manually using the cluster tools."
        )
    if args.command == "run":
        if args.fail:
            raise ValueError("--fail is only available for simulated demo jobs")
        factor = _selected_definition(args)
        resources = _run_resources(args)
        window = _run_date_range(args)
        service = RunService(Workspace(args.workspace), load_config(args.config))
        record = asyncio.run(service.submit(factor, resources, window.start, window.end))
        print(json.dumps({"demo": False, "run": record}, ensure_ascii=False))
        return 0
    if args.command in {"collect", "results"}:
        service = RunService(Workspace(args.workspace), load_config(args.config))
        receipt = asyncio.run(service.collect(args.job_id, request_id=args.request_id))
        print(json.dumps({"demo": False, "receipt": receipt}, ensure_ascii=False))
        return 0
    client = RemoteClient(load_config(args.config))
    if args.command == "setup":
        result = asyncio.run(client.diagnose())
        print(json.dumps({"demo": False, "diagnostic": asdict(result)}, ensure_ascii=False))
        return 0 if result.ok else 1
    if args.command is None:
        from fe_cli.app import FactorTerminal

        FactorTerminal(
            online=client, resource_store=ResourceStore(path=args.resource_config),
            date_range_store=DateRangeStore(path=args.range_config),
            workspace=Workspace(args.workspace),
            update_checker=_update_checker(args),
        ).run()
        return 0
    result = asyncio.run(client.query(_ONLINE_COMMANDS[args.command]))
    if result.returncode:
        print(
            f"fe: online error: {result.command} failed (exit {result.returncode})"
            + (f": {result.stderr}" if result.stderr else ""),
            file=sys.stderr,
        )
        return 1
    print(json.dumps({
        "demo": False,
        "command": result.command,
        "stdout": recent_history(result.stdout)
        if args.command == "history" and args.scope != "full" else result.stdout,
        "stderr": result.stderr,
    }, ensure_ascii=False))
    return 0


def _update_checker(args: argparse.Namespace) -> UpdateChecker | None:
    if args.no_update_check or os.environ.get("FE_CLI_NO_UPDATE_CHECK"):
        return None
    return UpdateChecker()


def _manage_updates(args: argparse.Namespace) -> int:
    try:
        release = UpdateChecker().check(force=True)
        if release is None:
            raise UpdateError("No stable CLI release is available.")
        print(f"Current CLI version: {__version__}")
        print(f"Latest stable CLI version: {release.version}")
        if version_key(release.version) <= version_key(__version__):
            print("No newer stable CLI version is available.")
        elif args.check_update:
            print("Update available. Run fe-cli --update to install it.")
        else:
            from fe_cli.self_update import run_update

            print(f"Updating fe-cli {__version__} to {release.version}...", flush=True)
            run_update(release)
            print(f"Updated to fe-cli {release.version}. Run fe-cli again to start it.")
        return 0
    except (UpdateError, OSError, ValueError) as exc:
        print(f"fe-cli: update error: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    """Run a CLI command or open the terminal, returning a process exit code."""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.update or args.check_update:
        if args.command is not None:
            parser.error("--update and --check-update cannot be combined with a subcommand")
        return _manage_updates(args)
    try:
        if args.command == "init":
            _initialize(args)
            return 0
        if args.command == "factor":
            _factors(args)
            return 0
        if args.command == "configure":
            _configure(args)
            return 0
        if args.command in {"node", "resources"}:
            _resources(args)
            return 0
        if args.command == "range":
            if (args.start is None) != (args.end is None):
                raise ValueError("range requires both START and END, or neither to show defaults")
            store = DateRangeStore(path=args.range_config)
            saved = args.start is not None
            window = DateRange(args.start, args.end) if saved else store.load()
            if saved:
                store.save(window)
            print(json.dumps({"range": asdict(window), "saved": saved}, ensure_ascii=False))
            return 0
        if args.command in {"configure-run", "prepare", "import-results", "runs"}:
            _local_run_command(args)
            return 0
        if args.online:
            return _online(args)
        if args.command in {"setup", "envs", "history", "collect"}:
            raise ValueError(f"{args.command} requires --online and connection settings")
        if args.command == "results" and args.request_id is not None:
            raise ValueError("results --request-id requires --online")
        if args.command == "run":
            factor_id, formula = _run_definition(args)
            resources = _run_resources(args)
            window = _run_date_range(args)
        backend = DemoBackend(state_dir=args.state_dir)
        if args.command is None:
            # Validate persisted state before Textual takes over the terminal.
            backend.jobs()
            from fe_cli.app import FactorTerminal

            FactorTerminal(
                backend=backend, resource_store=ResourceStore(path=args.resource_config),
                date_range_store=DateRangeStore(path=args.range_config),
                workspace=Workspace(args.workspace),
                update_checker=_update_checker(args),
            ).run()
        elif args.command == "run":
            job = backend.submit(
                factor=factor_id,
                formula=formula,
                start=window.start,
                end=window.end,
                fail=args.fail,
                node=resources.node,
                cpus=resources.cpus,
            )
            _print_json("job", asdict(job))
        elif args.command == "jobs":
            _print_json("jobs", [asdict(job) for job in backend.jobs()])
        elif args.command == "cancel":
            _print_json("job", asdict(backend.cancel(args.job_id)))
        elif args.command == "results":
            _print_json("report", backend.report(args.job_id))
    except (ValueError, OSError) as exc:
        if args.command in {"init", "factor", "prepare", "import-results", "runs"}:
            mode = "local workspace"
        elif args.command in {"node", "resources"}:
            mode = "resource configuration"
        elif args.command == "range":
            mode = "date range configuration"
        else:
            mode = "configuration" if args.command in {"configure", "configure-run"} else (
                "online" if args.online else "demo"
            )
        print(f"fe: {mode} error: {exc}", file=sys.stderr)
        return 1
    return 0
