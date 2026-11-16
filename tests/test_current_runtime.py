"""Current Core selection is local until the explicitly submitted job starts."""

import json
import shlex
from dataclasses import asdict

from fe_cli import cli
from fe_cli.catalog import FactorDefinition
from fe_cli.config import ConnectionConfig
from fe_cli.execution import RunService
from fe_cli.job_transport import build_submit_argv
from fe_cli.resources import ResourceRequest
from fe_cli.runtime_settings import RuntimeSettings, RuntimeStore
from fe_cli.workspace import Workspace


def test_init_and_prepare_current_core_need_no_connection_or_manifest_digest(tmp_path):
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    runtime = RuntimeStore(workspace.root).load()
    assert runtime == RuntimeSettings()
    assert runtime.release_sha256 is None
    factor = workspace.catalog.add(FactorDefinition("mean20", "Mean", "ts_mean(ret,20)"))
    record = RunService(workspace).prepare(
        factor, ResourceRequest(), "2026-10-01", "2026-10-30"
    )
    assert record["request"]["runtime"] == asdict(runtime)
    argv = build_submit_argv(ConnectionConfig("fixture.invalid"), record["request"], runtime)
    command = shlex.split(argv[-1])
    assert command[command.index("--release-sha256") + 1] == "current"
    assert record["request"]["factor"]["expression"] == "ts_mean(ret,20)"
    assert record["request"]["runtime"]["release_sha256"] is None


def test_configure_run_defaults_to_current_core_without_network(tmp_path, monkeypatch, capsys):
    def no_network(*args, **kwargs):
        raise AssertionError("Local configuration must not connect")

    monkeypatch.setattr(cli, "load_config", no_network)
    root = tmp_path / "workspace"
    assert cli.main(["--workspace", str(root), "init"]) == 0
    initialized = json.loads(capsys.readouterr().out)
    assert initialized["runtime_path"] == str(root / "runtime.json")
    assert cli.main(["--workspace", str(root), "configure-run"]) == 0
    assert json.loads(capsys.readouterr().out)["runtime"] == asdict(RuntimeSettings())
    assert RuntimeStore(root).load().release_sha256 is None


def test_prepare_does_not_evaluate_formula_or_rewrite_its_definition(tmp_path):
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    marker = tmp_path / "must-not-exist"
    text = f"__import__('pathlib').Path({str(marker)!r}).touch()"
    factor = workspace.catalog.add(FactorDefinition("text", "Text", text))
    before = workspace.catalog_path.read_bytes()
    record = RunService(workspace).prepare(
        factor, ResourceRequest(), "2026-10-01", "2026-10-30"
    )
    assert record["request"]["factor"]["expression"] == text
    assert workspace.catalog_path.read_bytes() == before
    assert not marker.exists()
