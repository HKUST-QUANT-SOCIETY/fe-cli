"""Public release configuration and request-only preparation stay entirely local."""

import json
import shlex
import tarfile
from dataclasses import asdict, replace

import pytest

from fe_cli import cli
from fe_cli.catalog import FactorDefinition
from fe_cli.config import ConnectionConfig
from fe_cli.execution import RunService
from fe_cli.job_transport import build_submit_argv
from fe_cli.resources import ResourceRequest
from fe_cli.runtime_settings import RuntimeSettings, RuntimeStore
from fe_cli.workspace import Workspace

RELEASE_ROOT = "/public/fe/releases/test-release"
RELEASE_SHA256 = "a1" * 32
PUBLIC_PYTHON = "/public/fe/envs/test-runtime/bin/python"


@pytest.fixture
def workspace(tmp_path):
    result = Workspace(tmp_path / "workspace")
    result.initialize()
    result.catalog.add(FactorDefinition("mean3", "Mean return", "ts_mean(col('ret'), 3)"))
    return result


def public_settings(**changes):
    return RuntimeSettings(
        **(
            {
                "remote_python": PUBLIC_PYTHON,
                "data_root": "/fixture/data",
                "release_root": RELEASE_ROOT,
                "release_sha256": RELEASE_SHA256,
                "node": "shared",
            }
            | changes
        )
    )


@pytest.mark.parametrize("node", ["quant-a", "quant-c"])
def test_public_prepare_uploads_only_frozen_request_for_each_node(workspace, node):
    runtime = public_settings()
    config = ConnectionConfig("local-fixture.invalid")
    service = RunService(workspace, config, settings=runtime)
    record = service.prepare(
        workspace.catalog.get("mean3"), ResourceRequest(node, 2), "2026-01-03", "2026-01-05"
    )
    with tarfile.open(record["bundle_path"]) as archive:
        assert archive.getnames() == ["request.json"]
        assert json.load(archive.extractfile("request.json")) == record["request"]
    assert record["request"]["runtime"] == asdict(runtime)
    assert record["request"]["source"]["data_root"] == runtime.data_root
    assert record["request"]["resources"]["node"] == node
    arguments = build_submit_argv(config, record["request"], runtime)
    command = shlex.split(arguments[-1])
    assert command[command.index("--node") + 1] == node
    assert command[command.index("--") + 1 :] == [
        PUBLIC_PYTHON,
        "-I",
        f"{RELEASE_ROOT}/runner.py",
        "--release-sha256",
        RELEASE_SHA256,
        "request.json",
    ]
    assert "runner.py" in record["manual_command"]
    assert "worker.py" not in record["manual_command"]

    RuntimeStore(workspace.root).save(public_settings(release_sha256="b2" * 32))
    assert service.get(record["request_id"])["request"]["runtime"] == asdict(runtime)


@pytest.mark.parametrize(
    "field,value",
    [
        ("release_root", "/public/fe/releases/other"),
        ("release_sha256", "b2" * 32),
    ],
)
def test_submission_rejects_release_different_from_frozen_request(workspace, field, value):
    runtime = public_settings()
    record = RunService(workspace, settings=runtime).prepare(
        workspace.catalog.get("mean3"), ResourceRequest(), "2026-01-03", "2026-01-05"
    )
    with pytest.raises(ValueError, match="frozen request"):
        build_submit_argv(
            ConnectionConfig("fixture.invalid"),
            record["request"],
            replace(runtime, **{field: value}),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"release_root": None},
        {"release_sha256": None},
        {"release_root": "relative/release"},
        {"release_root": "/public/fe/../release"},
        {"release_root": "/public/fe/release\n"},
        {"release_root": "/public/fe/release\x7f"},
        {"release_sha256": "a" * 63},
        {"release_sha256": "g" * 64},
        {"release_sha256": "A" * 64},
        {"node": "quant-b"},
    ],
)
def test_public_settings_reject_invalid_release_pair_paths_and_digest(changes):
    with pytest.raises(ValueError):
        public_settings(**changes)


def test_release_root_can_use_another_approved_absolute_directory():
    assert public_settings(release_root="/approved release's/root").release_root.startswith("/")


def test_legacy_settings_load_but_new_preparation_and_submission_are_rejected(workspace):
    store = RuntimeStore(workspace.root)
    legacy = asdict(RuntimeSettings("/legacy/env/bin/python", data_root="/fixture/data"))
    legacy.pop("release_root")
    legacy.pop("release_sha256")
    store.path.write_text(json.dumps({"version": 1, "runtime": legacy}))
    loaded = store.load()
    assert loaded.release_root is None and loaded.release_sha256 is None
    saved = store.path.read_bytes()
    with pytest.raises(ValueError, match="legacy code-upload mode"):
        RunService(workspace).prepare(
            workspace.catalog.get("mean3"), ResourceRequest(), "2026-01-03", "2026-01-05"
        )
    assert store.path.read_bytes() == saved
    assert not list(workspace.runs_path.iterdir())
    record = RunService(workspace, settings=public_settings()).prepare(
        workspace.catalog.get("mean3"), ResourceRequest(), "2026-01-03", "2026-01-05"
    )
    with pytest.raises(ValueError, match="legacy code-upload mode"):
        build_submit_argv(ConnectionConfig("fixture.invalid"), record["request"], loaded)
    with pytest.raises(ValueError, match="Runtime node"):
        RuntimeSettings("/legacy/python", data_root="/fixture/data", node="shared")


def test_configure_public_release_is_local_and_loads_roundtrip(workspace, monkeypatch, capsys):
    def refuse_connection(*args, **kwargs):
        raise AssertionError("configure-run must not connect")

    monkeypatch.setattr(cli, "load_config", refuse_connection)
    assert (
        cli.main(
            [
                "--online",
                "--workspace",
                str(workspace.root),
                "configure-run",
                "--remote-python",
                PUBLIC_PYTHON,
                "--data-root",
                "/fixture/data",
                "--node",
                "shared",
                "--release-root",
                RELEASE_ROOT,
                "--release-sha256",
                RELEASE_SHA256,
            ]
        )
        == 0
    )
    configured = json.loads(capsys.readouterr().out)
    assert configured["runtime"] == asdict(public_settings())
    assert RuntimeStore(workspace.root).load() == public_settings()
