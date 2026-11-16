"""Behavior checks for local demo, run preparation, and explicit online commands."""

import csv
import json
import subprocess
import sys
from dataclasses import asdict
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from fe_cli import __version__, cli
from fe_cli.backend import F01_FORMULA, DemoBackend
from fe_cli.date_range import DateRange, DateRangeStore
from fe_cli.resources import ResourceRequest, ResourceStore
from fe_cli.runtime_settings import RuntimeSettings, RuntimeStore
from fe_cli.workspace import Workspace


@pytest.fixture(autouse=True)
def isolated_resource_preferences(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))


def test_run_and_jobs_share_persisted_demo_state(tmp_path, capsys):
    args = ["--state-dir", str(tmp_path)]
    assert cli.main([*args, "run"]) == 0
    submitted = json.loads(capsys.readouterr().out)
    assert submitted["demo"] is True
    assert submitted["job"]["factor"] == "f01"
    assert submitted["job"]["formula"] == F01_FORMULA
    assert submitted["job"]["start"] == "2026-10-01"
    assert submitted["job"]["end"] == "2026-10-31"
    assert submitted["job"]["cpus"] == 6
    assert submitted["job"]["memory"] == "18G"
    assert submitted["job"]["node"] == "quant-a"

    assert cli.main([*args, "jobs"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["demo"] is True
    assert [job["id"] for job in listed["jobs"]] == [submitted["job"]["id"]]


def test_run_passes_explicit_parameters_and_preserves_unicode(tmp_path, capsys):
    formula = "rank(收益率)"
    assert cli.main(
        [
            "--demo", "--state-dir", str(tmp_path), "run", "--factor", "research",
            "--formula", formula, "--start", "2026-09-01", "--end", "2026-09-30",
            "--fail",
        ]
    ) == 0
    output = capsys.readouterr().out
    assert formula in output
    payload = json.loads(output)
    assert payload["demo"] is True
    assert payload["job"]["factor"] == "research"
    assert payload["job"]["formula"] == formula
    assert payload["job"]["start"] == "2026-09-01"
    assert payload["job"]["end"] == "2026-09-30"


def test_cancel_persists_job(tmp_path, capsys):
    backend = DemoBackend(state_dir=tmp_path)
    job = backend.submit()
    assert cli.main(["--state-dir", str(tmp_path), "cancel", job.id]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["demo"] is True
    assert payload["job"]["status"] == "CANCELLED"
    assert DemoBackend(state_dir=tmp_path).get(job.id).status == "CANCELLED"


def test_results_are_json_demo_envelope(tmp_path, capsys):
    backend = Mock()
    backend.report.return_value = {"job_id": "demo-1", "note": "模拟报告"}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cli, "DemoBackend", Mock(return_value=backend))
        assert cli.main(["--state-dir", str(tmp_path), "results", "demo-1"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"demo": True, "report": backend.report.return_value}
    backend.report.assert_called_once_with("demo-1")


@pytest.mark.parametrize("command", [["cancel", "missing"], ["results", "missing"]])
def test_unknown_job_returns_clear_error(tmp_path, capsys, command):
    assert cli.main(["--state-dir", str(tmp_path), *command]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert "demo error:" in output.err
    assert "missing" in output.err
    assert "Traceback" not in output.err


@pytest.mark.parametrize("command", [[], ["jobs"]])
def test_invalid_persisted_state_is_handled_before_terminal(tmp_path, capsys, command):
    backend = DemoBackend(state_dir=tmp_path)
    backend.submit()
    state_files = list(tmp_path.glob("*.json"))
    assert state_files
    for state_file in state_files:
        state_file.write_text("{broken", encoding="utf-8")
    assert cli.main(["--state-dir", str(tmp_path), *command]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert "demo error:" in output.err
    assert "Traceback" not in output.err


def test_state_write_error_is_handled(tmp_path, capsys, monkeypatch):
    backend = Mock()
    backend.submit.side_effect = OSError("state directory is read-only")
    monkeypatch.setattr(cli, "DemoBackend", Mock(return_value=backend))
    assert cli.main(["--state-dir", str(tmp_path), "run"]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert "state directory is read-only" in output.err
    assert "Traceback" not in output.err


@pytest.mark.parametrize("extra_args", [[], ["--demo"]])
def test_no_subcommand_launches_terminal(tmp_path, monkeypatch, extra_args):
    app = ModuleType("fe_cli.app")
    terminal = Mock()
    app.FactorTerminal = terminal
    monkeypatch.setitem(sys.modules, "fe_cli.app", app)
    assert cli.main(["--state-dir", str(tmp_path), *extra_args]) == 0
    backend = terminal.call_args.kwargs["backend"]
    assert isinstance(backend, DemoBackend)
    store = terminal.call_args.kwargs["resource_store"]
    assert isinstance(store, ResourceStore)
    assert store.load() == ResourceRequest()
    assert isinstance(terminal.call_args.kwargs["workspace"], Workspace)
    terminal.return_value.run.assert_called_once_with()


@pytest.mark.parametrize("program", ["fe", "fe-cli"])
def test_version_does_not_construct_backend(monkeypatch, capsys, program):
    monkeypatch.setattr(sys, "argv", [program])
    backend = Mock(side_effect=AssertionError("version should not open state"))
    monkeypatch.setattr(cli, "DemoBackend", backend)
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"{program} {__version__}"
    backend.assert_not_called()


def test_init_creates_workspace_and_preserves_existing_edits(tmp_path, capsys):
    root = tmp_path / "research"
    assert cli.main(["init", str(root)]) == 0
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload == {
        "workspace": str(root),
        "csv": str(root / "factors.csv"),
        "readme": str(root / "README.md"),
        "created": True,
        "values_directory": str(root / "values"),
        "runs_directory": str(root / "runs"),
        "runtime_path": str(root / "runtime.json"),
    }
    assert "Edit" in output.err
    assert "factor list" in output.err
    assert (root / "values").is_dir()
    assert (root / "runs").is_dir()
    csv_path = root / "factors.csv"
    with csv_path.open(newline="", encoding="utf-8") as stream:
        assert csv.DictReader(stream).fieldnames == [
            "factor_id", "name", "expression", "enabled", "description"
        ]
    original = csv_path.read_bytes()
    readme = root / "README.md"
    readme.write_text("My research notes\n", encoding="utf-8")
    assert cli.main(["--workspace", str(root), "init", str(root)]) == 0
    assert json.loads(capsys.readouterr().out)["created"] is False
    assert csv_path.read_bytes() == original
    assert readme.read_text(encoding="utf-8") == "My research notes\n"


def test_init_rejects_conflicting_workspace_before_creating_files(tmp_path, capsys):
    first, second = tmp_path / "first", tmp_path / "second"
    assert cli.main(["--workspace", str(first), "init", str(second)]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert "same directory" in output.err
    assert not first.exists()
    assert not second.exists()


def test_init_global_workspace_and_default_home(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert cli.main(["init"]) == 0
    assert json.loads(capsys.readouterr().out)["workspace"] == str(tmp_path / "fe-workspace")
    other = tmp_path / "other"
    assert cli.main(["--workspace", str(other), "init"]) == 0
    assert json.loads(capsys.readouterr().out)["workspace"] == str(other)


@pytest.fixture
def initialized_workspace(tmp_path, capsys):
    root = tmp_path / "workspace"
    assert cli.main(["init", str(root)]) == 0
    capsys.readouterr()
    return root


def test_factor_crud_is_local_json_and_does_not_purge_outputs(
    initialized_workspace, capsys
):
    root = initialized_workspace
    args = ["--workspace", str(root), "factor"]
    assert cli.main([*args, "list"]) == 0
    original = json.loads(capsys.readouterr().out)
    assert original["kind"] == "local"
    assert "demo" not in original
    assert cli.main([
        *args, "add", "research", "--expression", "rank(收益率)",
        "--description", "Local study", "--disabled",
    ]) == 0
    added = json.loads(capsys.readouterr().out)["factor"]
    assert added["factor_id"] == added["name"] == "research"
    assert added["expression"] == "rank(收益率)"
    assert added["enabled"] is False
    assert added["description"] == "Local study"
    assert added["definition_hash"]
    assert cli.main([
        *args, "update", "research", "--name", "Research", "--enabled",
        "--expression", "rank(close)", "--description", "",
    ]) == 0
    updated = json.loads(capsys.readouterr().out)["factor"]
    assert updated["name"] == "Research"
    assert updated["expression"] == "rank(close)"
    assert updated["enabled"] is True
    assert updated["description"] == ""
    assert updated["definition_hash"] != added["definition_hash"]
    assert cli.main([*args, "show", "research"]) == 0
    assert json.loads(capsys.readouterr().out)["factor"] == updated
    assert cli.main([*args, "list"]) == 0
    assert updated in json.loads(capsys.readouterr().out)["factors"]
    output = root / "values" / "research.csv"
    output.write_text("keep this existing output\n", encoding="utf-8")
    assert cli.main([*args, "delete", "research"]) == 0
    assert json.loads(capsys.readouterr().out) == {"kind": "local", "deleted": updated}
    assert output.read_text(encoding="utf-8") == "keep this existing output\n"
    assert cli.main([*args, "show", "research"]) == 1
    assert "research" in capsys.readouterr().err


def test_factor_update_requires_an_actual_option(initialized_workspace, capsys):
    root = initialized_workspace
    before = (root / "factors.csv").read_bytes()
    assert cli.main(["--workspace", str(root), "factor", "update", "f01"]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert "requires --name" in output.err
    assert (root / "factors.csv").read_bytes() == before


def test_local_library_never_loads_online_configuration_or_demo(
    tmp_path, capsys, online_client, monkeypatch
):
    root = tmp_path / "workspace"
    monkeypatch.setattr(cli, "ResourceStore", Mock(side_effect=AssertionError("resources loaded")))
    args = ["--online", "--workspace", str(root), "--config", str(tmp_path / "missing.json")]
    for command in [
        ["init"], ["factor", "list"],
        ["factor", "add", "local", "--expression", "rank(close)"],
        ["factor", "show", "local"], ["factor", "update", "local", "--disabled"],
        ["factor", "delete", "local"],
    ]:
        assert cli.main([*args, *command]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert "demo" not in payload
    online_client.load.assert_not_called()
    online_client.factory.assert_not_called()


def test_missing_catalog_is_actionable_without_creating_workspace(tmp_path, capsys):
    root = tmp_path / "missing"
    assert cli.main(["--workspace", str(root), "factor", "list"]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert "fe init" in output.err
    assert not root.exists()


def test_run_reads_external_csv_edits_and_legacy_factor_option(
    initialized_workspace, tmp_path, capsys
):
    root = initialized_workspace
    args = ["--workspace", str(root), "--state-dir", str(tmp_path / "state")]
    csv_path = root / "factors.csv"
    csv_path.write_text(
        "factor_id,name,expression,enabled,description\n"
        "external,External,rank(close),true,Edited outside the CLI\n",
        encoding="utf-8",
    )
    assert cli.main([*args, "run", "external"]) == 0
    job = json.loads(capsys.readouterr().out)["job"]
    assert job["factor"] == "external"
    assert job["formula"] == "rank(close)"
    csv_path.write_text(csv_path.read_text(encoding="utf-8").replace(
        "rank(close)", "rank(volume)"
    ), encoding="utf-8")
    assert cli.main([*args, "run", "--factor", "external"]) == 0
    assert json.loads(capsys.readouterr().out)["job"]["formula"] == "rank(volume)"


@pytest.mark.parametrize("command", [
    ["run", "disabled"], ["run", "missing"], ["run", "one", "--factor", "two"],
])
def test_invalid_library_run_rejects_before_state_or_resources(
    initialized_workspace, capsys, monkeypatch, command
):
    root = initialized_workspace
    assert cli.main([
        "--workspace", str(root), "factor", "add", "disabled", "--disabled",
        "--expression", "rank(close)",
    ]) == 0
    capsys.readouterr()
    monkeypatch.setattr(cli, "ResourceStore", Mock(side_effect=AssertionError("resources loaded")))
    monkeypatch.setattr(cli, "DemoBackend", Mock(side_effect=AssertionError("demo opened")))
    assert cli.main(["--workspace", str(root), *command]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert "error:" in output.err


@pytest.mark.parametrize("factor_id", ["missing", "disabled"])
def test_positional_formula_override_rejects_unknown_or_disabled_before_state(
    initialized_workspace, capsys, monkeypatch, factor_id
):
    root = initialized_workspace
    assert cli.main([
        "--workspace", str(root), "factor", "add", "disabled", "--disabled",
        "--expression", "rank(close)",
    ]) == 0
    capsys.readouterr()
    before = (root / "factors.csv").read_bytes()
    monkeypatch.setattr(cli, "ResourceStore", Mock(side_effect=AssertionError("resources loaded")))
    monkeypatch.setattr(cli, "DemoBackend", Mock(side_effect=AssertionError("demo opened")))
    assert cli.main([
        "--workspace", str(root), "run", factor_id, "--formula", "rank(volume)",
    ]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert factor_id in output.err
    assert (root / "factors.csv").read_bytes() == before


def test_positional_formula_override_preserves_catalog(initialized_workspace, tmp_path, capsys):
    root = initialized_workspace
    assert cli.main([
        "--workspace", str(root), "factor", "add", "override", "--expression", "rank(close)",
    ]) == 0
    capsys.readouterr()
    before = (root / "factors.csv").read_bytes()
    assert cli.main([
        "--workspace", str(root), "--state-dir", str(tmp_path / "state"),
        "run", "override", "--formula", "rank(volume)",
    ]) == 0
    job = json.loads(capsys.readouterr().out)["job"]
    assert job["factor"] == "override"
    assert job["formula"] == "rank(volume)"
    assert (root / "factors.csv").read_bytes() == before


def test_formula_override_needs_no_library_and_never_evaluates_text(tmp_path, capsys):
    marker = tmp_path / "executed"
    formula = f"__import__('pathlib').Path({str(marker)!r}).touch()"
    assert cli.main([
        "--workspace", str(tmp_path / "missing-library"),
        "--state-dir", str(tmp_path / "state"),
        "run", "--factor", "ad_hoc", "--formula", formula,
    ]) == 0
    job = json.loads(capsys.readouterr().out)["job"]
    assert job["factor"] == "ad_hoc"
    assert job["formula"] == formula
    assert not marker.exists()
    assert not (tmp_path / "missing-library").exists()


def test_unknown_option_has_argparse_exit_code(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--not-a-real-option"])
    assert exc.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_module_entry_point(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "fe_cli", "--state-dir", str(tmp_path), "jobs"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"demo": True, "jobs": []}


def test_run_payload_matches_backend_dataclass(tmp_path, capsys, monkeypatch):
    backend = DemoBackend(state_dir=tmp_path)
    job = backend.submit()
    fake = Mock()
    fake.submit.return_value = job
    monkeypatch.setattr(cli, "DemoBackend", Mock(return_value=fake))
    assert cli.main(["run"]) == 0
    assert json.loads(capsys.readouterr().out) == {"demo": True, "job": asdict(job)}


@pytest.fixture
def online_client(monkeypatch):
    config = object()
    load = Mock(return_value=config)
    client = Mock()
    client.query = AsyncMock()
    client.diagnose = AsyncMock()
    factory = Mock(return_value=client)
    monkeypatch.setattr(cli, "load_config", load)
    monkeypatch.setattr(cli, "RemoteClient", factory)
    monkeypatch.setattr(cli, "DemoBackend", Mock(side_effect=AssertionError("demo opened")))
    return SimpleNamespace(config=config, load=load, client=client, factory=factory)


@pytest.mark.parametrize("command,remote_command", [
    (["envs"], "cluster-envs"),
    (["jobs"], "cluster-status"),
    (["history"], "cluster-history"),
    (["history", "full"], "cluster-history"),
])
def test_online_queries_are_read_only_json(
    tmp_path, capsys, online_client, command, remote_command
):
    state = tmp_path / "demo-state"
    config = tmp_path / "connection.json"
    online_client.client.query.return_value = SimpleNamespace(
        command=remote_command, stdout="remote output 模拟\n", stderr="warning\n", returncode=0
    )
    assert cli.main([
        "--online", "--config", str(config), "--state-dir", str(state), *command
    ]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "demo": False, "command": remote_command,
        "stdout": "remote output 模拟\n", "stderr": "warning\n",
    }
    online_client.load.assert_called_once_with(config)
    online_client.factory.assert_called_once_with(online_client.config)
    online_client.client.query.assert_awaited_once_with(remote_command)
    online_client.client.diagnose.assert_not_called()
    assert not state.exists()


@pytest.mark.parametrize("authentication,exit_code", [
    ("authenticated", 0),
    ("failed", 1),
    ("unknown", 1),
])
@pytest.mark.parametrize("command", ["status", "setup"])
def test_online_status_returns_connection_diagnostic_without_query(
    tmp_path, capsys, online_client, authentication, exit_code, command
):
    from fe_cli.diagnostics import DiagnosticResult

    state = tmp_path / "demo-state"
    config = tmp_path / "connection.json"
    result = DiagnosticResult(
        host="cluster.example", port=2222, user="researcher", connection="connected",
        authentication=authentication,
        key_fingerprint="SHA256:public-fingerprint" if exit_code == 0 else None,
        key_type="ED25519" if exit_code == 0 else None,
        detail="SSH authentication checked.",
    )
    online_client.client.diagnose.return_value = result

    assert cli.main([
        "--online", "--config", str(config), "--state-dir", str(state), command
    ]) == exit_code

    output = capsys.readouterr()
    assert json.loads(output.out) == {"demo": False, "diagnostic": asdict(result)}
    assert not output.err
    assert "groups" not in output.out
    assert "PRIVATE KEY" not in output.out
    online_client.load.assert_called_once_with(config)
    online_client.factory.assert_called_once_with(online_client.config)
    online_client.client.diagnose.assert_awaited_once_with()
    online_client.client.query.assert_not_called()
    assert not state.exists()


@pytest.mark.parametrize("error", [ValueError("invalid connection"), OSError("SSH unavailable")])
def test_online_setup_errors_are_handled(capsys, online_client, error):
    online_client.client.diagnose.side_effect = error
    assert cli.main(["--online", "setup"]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert f"online error: {error}" in output.err
    assert "Traceback" not in output.err
    online_client.client.query.assert_not_called()


@pytest.mark.parametrize("command", [["cancel", "123"]])
def test_online_disabled_commands_reject_before_loading_config(
    tmp_path, capsys, online_client, command
):
    state = tmp_path / "demo-state"
    online_client.load.side_effect = AssertionError("configuration must not be loaded")
    assert cli.main([
        "--online", "--config", str(tmp_path / "missing.json"),
        "--state-dir", str(state), *command,
    ]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert "Online cancellation is disabled" in output.err
    assert "manually" in output.err
    online_client.load.assert_not_called()
    online_client.factory.assert_not_called()
    assert not state.exists()


@pytest.mark.parametrize("missing", ["--data-root", "--release-root", "--release-sha256"])
def test_configure_run_uses_standard_defaults_for_omitted_fields(
    tmp_path, capsys, online_client, missing
):
    workspace = tmp_path / "workspace"
    options = {
        "--remote-python": "/approved/python",
        "--data-root": "/fixture/data",
        "--release-root": "/approved/release",
        "--release-sha256": "a" * 64,
    }
    arguments = [
        argument for key, value in options.items() if key != missing
        for argument in (key, value)
    ]

    assert cli.main([
        "--online", "--workspace", str(workspace), "configure-run", *arguments
    ]) == 0
    runtime = json.loads(capsys.readouterr().out)["runtime"]
    defaults = RuntimeSettings()
    field = missing.removeprefix("--").replace("-", "_")
    assert runtime[field] == getattr(defaults, field)
    online_client.load.assert_not_called()
    online_client.factory.assert_not_called()


def test_old_runtime_loads_without_guessing_data_or_release_paths(tmp_path):
    store = RuntimeStore(tmp_path)
    original = json.dumps({
        "version": 1, "runtime": {"remote_python": "/legacy/python"},
    }).encode()
    store.path.write_bytes(original)

    runtime = store.load()

    assert runtime.remote_python == "/legacy/python"
    assert runtime.data_root == ""
    assert runtime.release_root is None and runtime.release_sha256 is None
    assert store.path.read_bytes() == original


def test_configure_run_preserves_corrupt_existing_configuration(
    tmp_path, capsys, online_client
):
    store = RuntimeStore(tmp_path)
    store.path.write_bytes(b"{broken configuration")

    assert cli.main([
        "--online", "--workspace", str(tmp_path), "configure-run",
        "--remote-python", "/approved/python", "--data-root", "/fixture/data",
        "--release-root", "/approved/release", "--release-sha256", "a" * 64,
    ]) == 1

    assert "Invalid runtime settings" in capsys.readouterr().err
    assert store.path.read_bytes() == b"{broken configuration"
    online_client.load.assert_not_called()
    online_client.factory.assert_not_called()


def test_prepare_reads_current_csv_and_freezes_local_runtime_without_ssh(
    initialized_workspace, tmp_path, capsys, online_client
):
    root = initialized_workspace
    args = ["--online", "--workspace", str(root), "--config", str(tmp_path / "absent.json")]
    assert cli.main([
        *args, "configure-run", "--remote-python", "/approved/venv/bin/python",
        "--data-root", "/fixture/data", "--release-root", "/approved/release",
        "--release-sha256", "a" * 64,
        "--node", "C", "--env", "research", "--time", "00:05:00",
    ]) == 0
    settings = json.loads(capsys.readouterr().out)
    assert settings["demo"] is False
    assert settings["runtime"]["node"] == "quant-c"
    csv_path = root / "factors.csv"
    csv_path.write_text(
        "factor_id,name,expression,enabled,description\n"
        "external,External,rank(col('close')),true,Current local CSV\n", encoding="utf-8"
    )
    assert cli.main([
        *args, "prepare", "external", "--node", "C", "--cpus", "8",
        "--start", "2026-09-01", "--end", "2026-09-30",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    run = payload["run"]
    assert payload["demo"] is False
    assert run["status"] == "PREPARED"
    assert run["job_id"] is None
    assert run["request"]["factor"]["expression"] == "rank(col('close'))"
    assert run["request"]["factor"]["name"] == "External"
    assert run["request"]["factor"]["description"] == "Current local CSV"
    assert run["request"]["resources"] == {"node": "quant-c", "cpus": 8, "memory": "24G"}
    assert run["runtime"]["remote_python"] == "/approved/venv/bin/python"
    assert run["request"]["window"] == {"start": "2026-09-01", "end": "2026-09-30"}
    assert (root / "runs" / run["request_id"] / "payload.tar.gz").is_file()
    assert cli.main([*args, "runs"]) == 0
    assert json.loads(capsys.readouterr().out)["runs"] == [run]
    online_client.load.assert_not_called()
    online_client.factory.assert_not_called()


def test_prepare_uses_initialized_current_runtime_without_connecting(
    initialized_workspace, capsys, online_client
):
    root = initialized_workspace
    Workspace(root).catalog.add(cli.FactorDefinition("f01", "F01", "close", True, ""))
    assert cli.main(["--workspace", str(root), "prepare", "f01"]) == 0
    record = json.loads(capsys.readouterr().out)["run"]
    assert record["status"] == "PREPARED"
    assert record["runtime"]["release_sha256"] is None
    assert record["request"]["factor"]["expression"] == "close"
    online_client.load.assert_not_called()


def test_online_run_uses_enabled_catalog_snapshot_and_explicit_resources(
    initialized_workspace, capsys, online_client, monkeypatch
):
    root = initialized_workspace
    root.joinpath("factors.csv").write_text(
        "factor_id,name,expression,enabled,description\n"
        "selected,Selected,rank(volume),true,Edited CSV\n", encoding="utf-8"
    )
    service = Mock()
    service.submit = AsyncMock(return_value={
        "status": "SUBMITTED", "job_id": "981", "request_id": "a" * 32,
    })
    factory = Mock(return_value=service)
    monkeypatch.setattr(cli, "RunService", factory)
    assert cli.main([
        "--online", "--workspace", str(root), "run", "selected",
        "--node", "C", "--cpus", "7", "--start", "2026-08-01", "--end", "2026-08-31",
    ]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "demo": False, "run": service.submit.return_value,
    }
    factor, resources, start, end = service.submit.await_args.args
    assert asdict(factor) == {
        "factor_id": "selected", "name": "Selected", "expression": "rank(volume)",
        "enabled": True, "description": "Edited CSV",
    }
    assert resources == ResourceRequest("C", 7)
    assert (start, end) == ("2026-08-01", "2026-08-31")
    assert factory.call_args.args[0].root == root
    assert factory.call_args.args[1] is online_client.config
    online_client.factory.assert_not_called()


def test_online_formula_override_requires_library_and_preserves_csv(
    initialized_workspace, capsys, online_client, monkeypatch
):
    root = initialized_workspace
    Workspace(root).catalog.add(cli.FactorDefinition("f01", "F01", "close", True, ""))
    original = (root / "factors.csv").read_bytes()
    service = Mock(submit=AsyncMock(return_value={"status": "SUBMITTED"}))
    monkeypatch.setattr(cli, "RunService", Mock(return_value=service))
    assert cli.main([
        "--online", "--workspace", str(root), "run", "--factor", "f01",
        "--formula", "rank(volume)",
    ]) == 0
    capsys.readouterr()
    factor = service.submit.await_args.args[0]
    assert factor.factor_id == "f01"
    assert factor.expression == "rank(volume)"
    assert (root / "factors.csv").read_bytes() == original


@pytest.mark.parametrize("command", [
    ["run"], ["run", "--formula", "rank(close)"],
    ["run", "missing", "--formula", "rank(close)"],
    ["run", "disabled"], ["run", "disabled", "--formula", "rank(close)"],
    ["run", "f01", "--fail"], ["run", "f01", "--factor", "other"],
])
def test_invalid_online_run_never_loads_connection_or_allocates(
    initialized_workspace, capsys, online_client, monkeypatch, command
):
    root = initialized_workspace
    Workspace(root).catalog.add(cli.FactorDefinition("disabled", "Disabled", "close", False, ""))
    monkeypatch.setattr(cli, "ResourceStore", Mock(side_effect=AssertionError("resources loaded")))
    service = Mock(side_effect=AssertionError("service created"))
    monkeypatch.setattr(cli, "RunService", service)
    assert cli.main(["--online", "--workspace", str(root), *command]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert "online error:" in output.err
    online_client.load.assert_not_called()
    service.assert_not_called()


@pytest.mark.parametrize("command", ["results", "collect"])
def test_online_collection_binds_job_and_request_without_remote_query(
    tmp_path, capsys, online_client, monkeypatch, command
):
    receipt = {"status": "COMMITTED", "job_id": "981", "request_id": "a" * 32}
    service = Mock(collect=AsyncMock(return_value=receipt))
    factory = Mock(return_value=service)
    monkeypatch.setattr(cli, "RunService", factory)
    root = tmp_path / "workspace"
    assert cli.main([
        "--online", "--workspace", str(root), command, "981", "--request-id", "a" * 32,
    ]) == 0
    assert json.loads(capsys.readouterr().out) == {"demo": False, "receipt": receipt}
    service.collect.assert_awaited_once_with("981", request_id="a" * 32)
    assert factory.call_args.args[0].root == root
    online_client.factory.assert_not_called()


def test_import_results_is_local_with_missing_connection(
    tmp_path, capsys, online_client, monkeypatch
):
    receipt = {"status": "COMMITTED", "request_id": "a" * 32}
    service = Mock()
    service.import_results.return_value = receipt
    factory = Mock(return_value=service)
    monkeypatch.setattr(cli, "RunService", factory)
    archive = tmp_path / "download.tar.gz"
    assert cli.main([
        "--online", "--config", str(tmp_path / "missing.json"),
        "--workspace", str(tmp_path / "workspace"), "import-results", str(archive),
        "--request-id", "a" * 32,
    ]) == 0
    assert json.loads(capsys.readouterr().out) == {"demo": False, "receipt": receipt}
    service.import_results.assert_called_once_with(archive, request_id="a" * 32)
    online_client.load.assert_not_called()
    online_client.factory.assert_not_called()


def test_collect_requires_explicit_online_without_local_demo_state(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "DemoBackend", Mock(side_effect=AssertionError("demo opened")))
    assert cli.main(["--state-dir", str(tmp_path), "collect", "981"]) == 1
    assert "collect requires --online" in capsys.readouterr().err


def test_online_ambiguous_submission_error_retains_recovery_details(
    initialized_workspace, capsys, online_client, monkeypatch
):
    Workspace(initialized_workspace).catalog.add(
        cli.FactorDefinition("f01", "F01", "close", True, "")
    )
    service = Mock(submit=AsyncMock(side_effect=ValueError(
        "Request abc is SUBMISSION_UNKNOWN; check /jobs and /history before submitting again."
    )))
    monkeypatch.setattr(cli, "RunService", Mock(return_value=service))
    assert cli.main([
        "--online", "--workspace", str(initialized_workspace), "run", "f01",
    ]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert "SUBMISSION_UNKNOWN" in output.err
    assert "check /jobs and /history" in output.err
    assert "Traceback" not in output.err


def test_online_no_subcommand_launches_terminal_without_query(tmp_path, monkeypatch, online_client):
    app = ModuleType("fe_cli.app")
    terminal = Mock()
    app.FactorTerminal = terminal
    monkeypatch.setitem(sys.modules, "fe_cli.app", app)
    state = tmp_path / "demo-state"
    workspace = tmp_path / "workspace"
    assert cli.main([
        "--online", "--state-dir", str(state), "--workspace", str(workspace)
    ]) == 0
    terminal.assert_called_once()
    assert terminal.call_args.kwargs["online"] is online_client.client
    store = terminal.call_args.kwargs["resource_store"]
    assert isinstance(store, ResourceStore)
    assert store.load() == ResourceRequest()
    assert terminal.call_args.kwargs["workspace"].root == workspace
    terminal.return_value.run.assert_called_once_with()
    online_client.client.query.assert_not_called()
    online_client.client.diagnose.assert_not_called()
    assert not state.exists()
    assert not workspace.exists()


def test_online_nonzero_exit_is_reported(capsys, online_client):
    online_client.client.query.return_value = SimpleNamespace(
        command="cluster-status", stdout="partial", stderr="permission denied", returncode=1
    )
    assert cli.main(["--online", "jobs"]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert "cluster-status failed (exit 1)" in output.err
    assert "permission denied" in output.err
    assert "Traceback" not in output.err


@pytest.mark.parametrize("error", [ValueError("invalid connection"), OSError("SSH unavailable")])
def test_online_query_errors_are_handled(capsys, online_client, error):
    online_client.client.query.side_effect = error
    assert cli.main(["--online", "jobs"]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert f"online error: {error}" in output.err
    assert "Traceback" not in output.err


def test_online_missing_config_is_handled_without_demo(tmp_path, capsys, monkeypatch):
    backend = Mock(side_effect=AssertionError("demo opened"))
    remote = Mock(side_effect=AssertionError("remote opened"))
    monkeypatch.setattr(cli, "DemoBackend", backend)
    monkeypatch.setattr(cli, "RemoteClient", remote)
    assert cli.main(["--online", "--config", str(tmp_path / "missing.json"), "jobs"]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert "online error:" in output.err
    assert "Traceback" not in output.err
    backend.assert_not_called()
    remote.assert_not_called()


@pytest.mark.parametrize("command", [
    ["status"], ["setup"], ["envs"], ["history"], ["history", "full"],
])
def test_online_only_commands_reject_demo_before_creating_state(tmp_path, capsys, command):
    state = tmp_path / "demo-state"
    assert cli.main(["--state-dir", str(state), *command]) == 1
    assert "requires --online" in capsys.readouterr().err
    assert not state.exists()


@pytest.mark.parametrize("command,expected", [(["history"], 10), (["history", "full"], 13)])
def test_history_recent_and_full_json(capsys, online_client, command, expected):
    output = "JobID JobName State\n----- ------- -----\n" + "".join(
        f"{number:04d} job{number} COMPLETED\n" for number in range(1, 14)
    )
    online_client.client.query.return_value = SimpleNamespace(
        command="cluster-history", stdout=output, stderr="", returncode=0
    )
    assert cli.main(["--online", *command]) == 0
    payload = json.loads(capsys.readouterr().out)
    lines = payload["stdout"].splitlines()
    records = [line for line in lines if line.split() and line.split()[0].isdigit()]
    assert len(records) == expected
    assert ("0001 job1 " in payload["stdout"]) == (expected == 13)
    if expected == 13:
        assert payload["stdout"] == output
    else:
        assert [int(line.split()[0]) for line in records] == list(range(13, 3, -1))
    online_client.client.query.assert_awaited_once_with("cluster-history")


@pytest.mark.parametrize("command", [
    ["history", "all"], ["history", "FULL"], ["history", "full", "extra"], ["history-full"],
])
def test_invalid_history_arguments_reject_before_loading_config(capsys, online_client, command):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--online", *command])
    assert exc.value.code == 2
    assert capsys.readouterr().err
    online_client.load.assert_not_called()
    online_client.factory.assert_not_called()
    online_client.client.query.assert_not_called()


def test_history_full_preserves_unknown_server_format(capsys, online_client):
    output = "\n".join(f"unknown {index}" for index in range(11))
    online_client.client.query.return_value = SimpleNamespace(
        command="cluster-history", stdout=output, stderr="", returncode=0
    )
    assert cli.main(["--online", "history"]) == 1
    error = capsys.readouterr()
    assert not error.out
    assert "use history full" in error.err

    assert cli.main(["--online", "history", "full"]) == 0
    assert json.loads(capsys.readouterr().out)["stdout"] == output


def test_demo_and_online_are_mutually_exclusive(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--demo", "--online", "jobs"])
    assert exc.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


@pytest.mark.parametrize("mode", [[], ["--demo"], ["--online"]])
def test_configure_saves_only_local_settings(tmp_path, capsys, monkeypatch, mode):
    from fe_cli.config import load_config

    backend = Mock(side_effect=AssertionError("demo opened"))
    remote = Mock(side_effect=AssertionError("remote opened"))
    monkeypatch.setattr(cli, "DemoBackend", backend)
    monkeypatch.setattr(cli, "RemoteClient", remote)
    config_path = tmp_path / "nested" / "connection.json"
    identity = tmp_path / "key"
    identity.write_text("PRIVATE KEY CONTENT MUST NOT BE PRINTED", encoding="utf-8")
    assert cli.main([
        *mode, "--config", str(config_path), "configure", "--host", "quant.example",
        "--user", "researcher", "--identity-file", str(identity), "--port", "2222",
        "--ssh-executable", "custom-ssh", "--connect-timeout", "15", "--timeout", "40",
    ]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {"configured": True, "config": str(config_path)}
    assert "PRIVATE KEY" not in output.out + output.err
    saved = load_config(config_path)
    assert saved.host == "quant.example"
    assert saved.user == "researcher"
    assert saved.port == 2222
    assert saved.ssh_executable == "custom-ssh"
    assert str(saved.identity_file) == str(identity)
    assert saved.connect_timeout == 15
    assert saved.timeout == 40
    backend.assert_not_called()
    remote.assert_not_called()


def test_configure_invalid_settings_is_handled(tmp_path, capsys):
    path = tmp_path / "connection.json"
    assert cli.main([
        "--config", str(path), "configure", "--host", "quant.example", "--port", "0"
    ]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert "configuration error:" in output.err
    assert not path.exists()


@pytest.mark.parametrize("command", ["node", "resources"])
def test_resource_inspection_is_local_and_does_not_save(tmp_path, capsys, online_client, command):
    path = tmp_path / "preferences.json"
    assert cli.main(["--online", "--resource-config", str(path), command]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "resources": {"node": "quant-a", "cpus": 6, "memory": "18G", "memory_gb": 18},
        "saved": False,
    }
    assert not path.exists()
    online_client.load.assert_not_called()
    online_client.factory.assert_not_called()


@pytest.mark.parametrize("mode", [[], ["--online"]])
def test_node_and_resources_persist_paired_steps(tmp_path, capsys, online_client, mode):
    path = tmp_path / "preferences.json"
    args = [*mode, "--resource-config", str(path)]
    for command, expected in [
        (["node", "C"], ("quant-c", 6, "18G")),
        (["resources", "7"], ("quant-c", 7, "21G")),
        (["resources", "8"], ("quant-c", 8, "24G")),
        (["resources", "6"], ("quant-c", 6, "18G")),
        (["node", "A"], ("quant-a", 6, "18G")),
        (["resources", "1"], ("quant-a", 1, "3G")),
    ]:
        assert cli.main([*args, *command]) == 0
        payload = json.loads(capsys.readouterr().out)
        node, cpus, memory = expected
        assert payload == {
            "resources": {"node": node, "cpus": cpus, "memory": memory, "memory_gb": cpus * 3},
            "saved": True,
        }
        assert ResourceStore(path).load() == ResourceRequest(node=node, cpus=cpus)
    online_client.load.assert_not_called()
    online_client.factory.assert_not_called()


def test_node_switch_rejects_excess_cpus_without_changing_preferences(tmp_path, capsys):
    path = tmp_path / "preferences.json"
    store = ResourceStore(path)
    store.save(ResourceRequest("C", 8))
    before = path.read_bytes()
    assert cli.main(["--resource-config", str(path), "node", "A"]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert "quant-a" in output.err
    assert "1 to 6" in output.err
    assert path.read_bytes() == before


@pytest.mark.parametrize("command", [
    ["node", "B"], ["resources", "0"], ["resources", "-1"], ["resources", "7"],
])
def test_invalid_resource_selection_does_not_allocate_state(
    tmp_path, capsys, monkeypatch, command
):
    backend = Mock(side_effect=AssertionError("demo opened"))
    monkeypatch.setattr(cli, "DemoBackend", backend)
    path = tmp_path / "preferences.json"
    assert cli.main(["--resource-config", str(path), *command]) == 1
    assert "resource configuration error:" in capsys.readouterr().err
    backend.assert_not_called()
    assert not path.exists()


@pytest.mark.parametrize("command", [["resources", "1.5"], ["run", "--cpus", "true"]])
def test_resource_cpu_arguments_require_integer_steps(capsys, command):
    with pytest.raises(SystemExit) as exc:
        cli.main(command)
    assert exc.value.code == 2
    assert "invalid int value" in capsys.readouterr().err


def test_jobs_snapshot_saved_selection_and_independent_overrides(tmp_path, capsys):
    path = tmp_path / "preferences.json"
    store = ResourceStore(path)
    store.save(ResourceRequest("C", 7))
    args = ["--resource-config", str(path), "--state-dir", str(tmp_path / "state")]
    expected = [("quant-c", 7, "21G"), ("quant-a", 2, "6G"), ("quant-c", 1, "3G")]
    commands = [["run"], ["run", "--node", "A", "--cpus", "2"], ["run", "--cpus", "1"]]
    for command, resources in zip(commands, expected, strict=True):
        assert cli.main([*args, *command]) == 0
        job = json.loads(capsys.readouterr().out)["job"]
        assert (job["node"], job["cpus"], job["memory"]) == resources
    assert store.load() == ResourceRequest("C", 7)
    store.save(ResourceRequest("A", 6))
    assert cli.main([*args, "jobs"]) == 0
    jobs = json.loads(capsys.readouterr().out)["jobs"]
    assert sorted((job["node"], job["cpus"], job["memory"]) for job in jobs) == sorted(expected)


def test_run_node_override_preserves_default_cpu_request(tmp_path, capsys):
    assert cli.main([
        "--state-dir", str(tmp_path / "state"), "run", "--node", "C"
    ]) == 0
    job = json.loads(capsys.readouterr().out)["job"]
    assert (job["node"], job["cpus"], job["memory"]) == ("quant-c", 6, "18G")
    assert not ResourceStore().path.exists()


@pytest.mark.parametrize("command", [["node"], ["resources", "2"], ["run"]])
def test_invalid_saved_preferences_fail_without_overwrite_or_backend(
    tmp_path, capsys, monkeypatch, command
):
    backend = Mock(side_effect=AssertionError("demo opened"))
    monkeypatch.setattr(cli, "DemoBackend", backend)
    path = tmp_path / "preferences.json"
    path.write_text(json.dumps({
        "version": 1, "resources": {"node": "quant-a", "cpus": True},
    }), encoding="utf-8")
    before = path.read_bytes()
    assert cli.main(["--resource-config", str(path), *command]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert "Invalid resource preferences" in output.err
    assert "Traceback" not in output.err
    assert path.read_bytes() == before
    backend.assert_not_called()


@pytest.mark.parametrize("overrides", [
    ["--node", "C", "--cpus", "9"], ["--node", "A", "--cpus", "7"],
    ["--cpus", "0"], ["--cpus", "-1"], ["--node", "B"],
])
def test_invalid_run_resources_fail_before_backend(tmp_path, capsys, monkeypatch, overrides):
    backend = Mock(side_effect=AssertionError("demo opened"))
    monkeypatch.setattr(cli, "DemoBackend", backend)
    state = tmp_path / "state"
    assert cli.main(["--state-dir", str(state), "run", *overrides]) == 1
    assert "demo error:" in capsys.readouterr().err
    backend.assert_not_called()
    assert not state.exists()


def test_online_run_rejects_before_loading_resource_preferences(tmp_path, capsys, online_client):
    path = tmp_path / "broken.json"
    path.write_text("broken", encoding="utf-8")
    assert cli.main([
        "--online", "--resource-config", str(path), "run", "--node", "C", "--cpus", "8"
    ]) == 1
    assert "requires an enabled factor ID" in capsys.readouterr().err
    online_client.load.assert_not_called()
    online_client.factory.assert_not_called()


@pytest.mark.parametrize("online", [False, True])
def test_terminal_receives_explicit_resource_store(
    tmp_path, monkeypatch, online_client, online
):
    app = ModuleType("fe_cli.app")
    app.FactorTerminal = Mock()
    monkeypatch.setitem(sys.modules, "fe_cli.app", app)
    # Online fixtures reject demo access; allow it only for this demo launch.
    if not online:
        monkeypatch.setattr(cli, "DemoBackend", DemoBackend)
    path = tmp_path / "preferences.json"
    ResourceStore(path).save(ResourceRequest("C", 8))
    args = ["--online"] if online else ["--state-dir", str(tmp_path / "state")]
    assert cli.main([*args, "--resource-config", str(path)]) == 0
    store = app.FactorTerminal.call_args.kwargs["resource_store"]
    assert store.path == path
    assert store.load() == ResourceRequest("C", 8)
    online_client.client.query.assert_not_called()


@pytest.mark.parametrize("mode", [[], ["--online"]])
def test_range_show_and_save_are_local_and_reload_preferences(
    tmp_path, capsys, online_client, mode
):
    path = tmp_path / "preferences" / "range.json"
    args = [*mode, "--range-config", str(path)]
    assert cli.main([*args, "range"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "range": {"start": "2026-10-01", "end": "2026-10-31"}, "saved": False,
    }
    assert not path.parent.exists()
    assert cli.main([*args, "range", "2026-09-01", "2026-09-30"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "range": {"start": "2026-09-01", "end": "2026-09-30"}, "saved": True,
    }
    assert DateRangeStore(path).load() == DateRange("2026-09-01", "2026-09-30")
    before = path.read_bytes()
    assert cli.main([*args, "range"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "range": {"start": "2026-09-01", "end": "2026-09-30"}, "saved": False,
    }
    assert path.read_bytes() == before
    online_client.load.assert_not_called()
    online_client.factory.assert_not_called()


@pytest.mark.parametrize("bounds", [
    ["2026-10-01"], ["2026-10-1", "2026-10-31"], ["2026-02-29", "2026-10-31"],
    ["2026-11-01", "2026-10-31"],
])
def test_invalid_range_command_preserves_preferences(tmp_path, capsys, online_client, bounds):
    path = tmp_path / "range.json"
    DateRangeStore(path).save(DateRange())
    before = path.read_bytes()
    assert cli.main(["--online", "--range-config", str(path), "range", *bounds]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert "date range configuration error:" in output.err
    assert path.read_bytes() == before
    online_client.load.assert_not_called()


@pytest.mark.parametrize("bounds", [[], ["2026-09-01", "2026-09-30"]])
def test_corrupt_range_command_is_actionable_and_preserves_file(tmp_path, capsys, bounds):
    path = tmp_path / "range.json"
    path.write_text("broken", encoding="utf-8")
    assert cli.main(["--range-config", str(path), "range", *bounds]) == 1
    assert "not overwritten" in capsys.readouterr().err
    assert path.read_text() == "broken"


@pytest.mark.parametrize("overrides,expected", [
    ([], ("2026-09-01", "2026-09-30")),
    (["--start", "2026-09-05"], ("2026-09-05", "2026-09-30")),
    (["--end", "2026-09-20"], ("2026-09-01", "2026-09-20")),
    (["--start", "2026-08-01", "--end", "2026-08-31"], ("2026-08-01", "2026-08-31")),
])
def test_demo_run_resolves_saved_bounds_independently_without_saving(
    tmp_path, capsys, overrides, expected
):
    path = tmp_path / "range.json"
    DateRangeStore(path).save(DateRange("2026-09-01", "2026-09-30"))
    before = path.read_bytes()
    assert cli.main([
        "--range-config", str(path), "--state-dir", str(tmp_path / "state"),
        "run", *overrides,
    ]) == 0
    job = json.loads(capsys.readouterr().out)["job"]
    assert (job["start"], job["end"]) == expected
    assert path.read_bytes() == before


@pytest.mark.parametrize("overrides,expected", [
    ([], ("2026-09-01", "2026-09-30")),
    (["--start", "2026-09-05"], ("2026-09-05", "2026-09-30")),
    (["--end", "2026-09-20"], ("2026-09-01", "2026-09-20")),
    (["--start", "2026-08-01", "--end", "2026-08-31"], ("2026-08-01", "2026-08-31")),
])
def test_prepare_and_online_run_share_resolved_bounds_without_saving(
    initialized_workspace, tmp_path, capsys, online_client, monkeypatch, overrides, expected
):
    root = initialized_workspace
    Workspace(root).catalog.add(cli.FactorDefinition("selected", "Selected", "close", True, ""))
    path = tmp_path / "range.json"
    DateRangeStore(path).save(DateRange("2026-09-01", "2026-09-30"))
    before = path.read_bytes()
    service = Mock(
        prepare=Mock(return_value={"status": "PREPARED"}),
        submit=AsyncMock(return_value={"status": "SUBMITTED"}),
    )
    monkeypatch.setattr(cli, "RunService", Mock(return_value=service))
    args = ["--online", "--workspace", str(root), "--range-config", str(path)]
    assert cli.main([*args, "prepare", "selected", *overrides]) == 0
    capsys.readouterr()
    online_client.load.assert_not_called()
    assert cli.main([*args, "run", "selected", *overrides]) == 0
    capsys.readouterr()
    assert service.prepare.call_args.args[2:] == expected
    assert service.submit.await_args.args[2:] == expected
    assert path.read_bytes() == before
    online_client.factory.assert_not_called()


@pytest.mark.parametrize("command", ["demo", "prepare", "online"])
@pytest.mark.parametrize("overrides", [
    ["--start", "2026-11-01"], ["--end", "2026-09-30"], ["--start", "2026-02-29"],
])
def test_invalid_resolved_bounds_fail_before_run_actions(
    initialized_workspace, tmp_path, capsys, online_client, monkeypatch, command, overrides
):
    root = initialized_workspace
    Workspace(root).catalog.add(cli.FactorDefinition("selected", "Selected", "close", True, ""))
    service = Mock(side_effect=AssertionError("service constructed"))
    backend = Mock(side_effect=AssertionError("backend constructed"))
    monkeypatch.setattr(cli, "RunService", service)
    monkeypatch.setattr(cli, "DemoBackend", backend)
    path = tmp_path / "range.json"
    args = ["--workspace", str(root), "--range-config", str(path)]
    if command == "online":
        args.append("--online")
    verb = "prepare" if command == "prepare" else "run"
    assert cli.main([*args, verb, "selected", *overrides]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert "date" in output.err
    assert not path.exists()
    service.assert_not_called()
    backend.assert_not_called()
    online_client.load.assert_not_called()


@pytest.mark.parametrize("online", [False, True])
def test_terminal_receives_explicit_date_range_store(
    tmp_path, monkeypatch, online_client, online
):
    app = ModuleType("fe_cli.app")
    app.FactorTerminal = Mock()
    monkeypatch.setitem(sys.modules, "fe_cli.app", app)
    if not online:
        monkeypatch.setattr(cli, "DemoBackend", DemoBackend)
    path = tmp_path / "range.json"
    DateRangeStore(path).save(DateRange("2026-09-01", "2026-09-30"))
    args = ["--online"] if online else ["--state-dir", str(tmp_path / "state")]
    assert cli.main([*args, "--range-config", str(path)]) == 0
    store = app.FactorTerminal.call_args.kwargs["date_range_store"]
    assert store.path == path
    assert store.load() == DateRange("2026-09-01", "2026-09-30")
    online_client.client.query.assert_not_called()


@pytest.mark.parametrize("online", [False, True])
@pytest.mark.parametrize("command", ["jobs", "history"])
def test_queries_do_not_read_date_preferences(
    tmp_path, capsys, online_client, monkeypatch, online, command
):
    monkeypatch.setattr(cli, "DateRangeStore", Mock(side_effect=AssertionError("range loaded")))
    if not online:
        monkeypatch.setattr(cli, "DemoBackend", DemoBackend)
    online_client.client.query.return_value = SimpleNamespace(
        command="cluster-status" if command == "jobs" else "cluster-history",
        stdout="empty", stderr="", returncode=0,
    )
    args = ["--online"] if online else ["--state-dir", str(tmp_path / "state")]
    assert cli.main([*args, command]) == (1 if not online and command == "history" else 0)
    capsys.readouterr()
