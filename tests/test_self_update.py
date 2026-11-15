"""Self-update only delegates verified scripts for an active installer-owned environment."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from fe_cli import self_update
from fe_cli.updates import ReleaseInfo, UpdateError

PAYLOAD = b"#!/bin/sh\nexit 0\n"
RELEASE = ReleaseInfo("0.1.1", hashlib.sha256(PAYLOAD).hexdigest())


@pytest.fixture
def owned(tmp_path, monkeypatch):
    root = tmp_path / "install root with spaces"
    manifest = json.dumps({
        "product": "fe-cli", "schema": "fe-cli-install-release-v1", "version": "0.1.0",
    }).encode()
    digest = hashlib.sha256(manifest).hexdigest()
    environment = root / "versions" / f"0.1.0-{digest}"
    (environment / "bin").mkdir(parents=True)
    (environment / "bin" / "fe-cli").write_text("current launcher")
    (environment / "fe-cli-release.json").write_bytes(manifest)
    (environment / ".fe-cli-ready").write_text(digest + "\n")
    bin_dir = tmp_path / "custom bin with spaces"
    bin_dir.mkdir()
    launcher = bin_dir / "fe-cli"
    launcher.symlink_to(environment / "bin" / "fe-cli")
    (environment / ".fe-cli-bin-dir").write_text(str(bin_dir) + "\n")
    monkeypatch.setattr(self_update.sys, "prefix", str(environment))
    monkeypatch.setattr(self_update, "__version__", "0.1.0")
    return root, bin_dir, environment


def forbid_execution(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Installer execution was not authorized")
    monkeypatch.setattr(self_update.subprocess, "run", forbidden)


@pytest.mark.parametrize("location", ["source/.venv", "uv/tools/fe-cli", "pip-env"])
def test_unmanaged_installation_is_rejected_before_network(tmp_path, monkeypatch, location):
    monkeypatch.setattr(self_update.sys, "prefix", str(tmp_path / location))
    forbid_execution(monkeypatch)
    monkeypatch.setattr(self_update, "fetch_bytes", lambda *a, **kw: pytest.fail("Network used"))

    with pytest.raises(UpdateError, match="original installation method"):
        self_update.run_update(RELEASE)


@pytest.mark.parametrize("damage", [
    "missing-record", "multiline-record", "relative-record", "parent-record", "linked-record",
    "bad-ready", "bad-manifest", "wrong-version", "wrong-schema", "unrelated-symlink",
    "plain-launcher", "inactive-environment",
])
def test_invalid_ownership_is_rejected(owned, monkeypatch, damage):
    root, bin_dir, environment = owned
    marker = environment / ".fe-cli-bin-dir"
    launcher = bin_dir / "fe-cli"
    if damage == "missing-record":
        marker.unlink()
    elif damage == "multiline-record":
        marker.write_text(str(bin_dir) + "\nextra\n")
    elif damage == "relative-record":
        marker.write_text("relative/bin\n")
    elif damage == "parent-record":
        marker.write_text(str(bin_dir / ".." / bin_dir.name) + "\n")
    elif damage == "linked-record":
        other = root / "record"
        marker.rename(other)
        marker.symlink_to(other)
    elif damage == "bad-ready":
        (environment / ".fe-cli-ready").write_text("0" * 64 + "\n")
    elif damage in {"bad-manifest", "wrong-version", "wrong-schema"}:
        manifest_path = environment / "fe-cli-release.json"
        manifest = json.loads(manifest_path.read_bytes())
        field = {"bad-manifest": "product", "wrong-version": "version",
                 "wrong-schema": "schema"}[damage]
        manifest[field] = "unrelated"
        manifest_path.write_text(json.dumps(manifest))
    else:
        launcher.unlink()
        if damage == "plain-launcher":
            launcher.write_text("unrelated command")
        else:
            destination = root / ("unrelated" if damage == "unrelated-symlink" else "versions/old")
            destination.mkdir(parents=True)
            other = destination / "fe-cli"
            other.write_text("other launcher")
            launcher.symlink_to(other)
    forbid_execution(monkeypatch)
    monkeypatch.setattr(self_update, "fetch_bytes", lambda *a, **kw: pytest.fail("Network used"))

    with pytest.raises(UpdateError, match="official curl installer"):
        self_update.run_update(RELEASE)


def test_bad_installer_digest_never_starts_shell(owned, monkeypatch):
    monkeypatch.setattr(self_update, "fetch_bytes", lambda *a, **kw: b"tampered script")
    forbid_execution(monkeypatch)

    with pytest.raises(UpdateError, match="SHA256"):
        self_update.run_update(RELEASE)


def test_update_uses_fixed_installer_and_overrides_inherited_install_paths(owned, monkeypatch):
    root, bin_dir, environment = owned
    monkeypatch.setenv("FE_CLI_RELEASE_DIR", "/untrusted-assets")
    monkeypatch.setenv("FE_CLI_INSTALL_ROOT", "/other-root")
    monkeypatch.setenv("FE_CLI_BIN_DIR", "/other-bin")
    monkeypatch.setenv("FE_CLI_EXPECTED_VERSION", "unrelated")
    monkeypatch.setenv("FE_TEST_INHERITED", "preserved")
    calls = []
    new_launcher = root / "versions" / "0.1.1-new-build" / "bin" / "fe-cli"

    def fetch(url, *, limit, timeout):
        assert url == RELEASE.installer_url
        assert limit == 256 * 1024
        assert timeout == 15.0
        return PAYLOAD

    def run(argv, **kwargs):
        assert kwargs["shell"] is False
        if argv[1] == "--version":
            assert argv == [str(bin_dir / "fe-cli"), "--version"]
            assert kwargs == {"shell": False, "capture_output": True, "text": True, "timeout": 5}
            return subprocess.CompletedProcess(argv, 0, "fe-cli 0.1.1\n", "")
        assert argv[0] == "sh"
        script = Path(argv[1])
        assert script.parent == Path(kwargs["cwd"])
        assert script.read_bytes() == PAYLOAD
        assert not kwargs["check"]
        env = kwargs["env"]
        assert "FE_CLI_RELEASE_DIR" not in env
        assert env["FE_CLI_INSTALL_ROOT"] == str(root)
        assert env["FE_CLI_BIN_DIR"] == str(bin_dir)
        assert env["FE_CLI_EXPECTED_VERSION"] == RELEASE.version
        assert env["FE_TEST_INHERITED"] == "preserved"
        calls.append(script)
        new_launcher.parent.mkdir(parents=True)
        new_launcher.write_text("updated launcher")
        (bin_dir / "fe-cli").unlink()
        (bin_dir / "fe-cli").symlink_to(new_launcher)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(self_update, "fetch_bytes", fetch)
    monkeypatch.setattr(self_update.subprocess, "run", run)
    self_update.run_update(RELEASE)

    assert len(calls) == 1
    assert not calls[0].exists()
    assert os.readlink(bin_dir / "fe-cli") == str(new_launcher)
    assert (environment / "bin" / "fe-cli").read_text() == "current launcher"


@pytest.mark.parametrize("failure", ["old-version", "failed-probe", "timeout", "missing-command"])
def test_installer_success_requires_active_version_match(owned, monkeypatch, failure):
    _, bin_dir, environment = owned
    monkeypatch.setattr(self_update, "fetch_bytes", lambda *a, **kw: PAYLOAD)

    def run(argv, **kwargs):
        if argv[0] == "sh":
            return subprocess.CompletedProcess(argv, 0)
        assert argv == [str(bin_dir / "fe-cli"), "--version"]
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 5)
        if failure == "missing-command":
            raise OSError("Command is unavailable")
        return subprocess.CompletedProcess(
            argv, int(failure == "failed-probe"), "fe-cli 0.1.0\n", "",
        )

    monkeypatch.setattr(self_update.subprocess, "run", run)
    with pytest.raises(UpdateError, match="Active CLI version mismatch"):
        self_update.run_update(RELEASE)
    assert (environment / "bin" / "fe-cli").read_text() == "current launcher"


@pytest.mark.parametrize("target", ["relative", "escape", "unrelated", "plain", "missing"])
def test_installer_success_cannot_probe_unowned_launcher(owned, monkeypatch, target):
    root, bin_dir, environment = owned
    monkeypatch.setattr(self_update, "fetch_bytes", lambda *a, **kw: PAYLOAD)

    def run(argv, **kwargs):
        assert argv[0] == "sh", "Unowned launcher must not be executed"
        launcher = bin_dir / "fe-cli"
        launcher.unlink()
        if target == "plain":
            launcher.write_text("unrelated command")
        else:
            destination = {
                "relative": "../other/bin/fe-cli",
                "escape": str(root / "versions" / ".." / environment.name / "bin" / "fe-cli"),
                "unrelated": str(root / "other" / "bin" / "fe-cli"),
                "missing": str(root / "versions" / "missing" / "bin" / "fe-cli"),
            }[target]
            launcher.symlink_to(destination)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(self_update.subprocess, "run", run)
    with pytest.raises(UpdateError, match="Active CLI version mismatch"):
        self_update.run_update(RELEASE)
    assert (environment / "bin" / "fe-cli").read_text() == "current launcher"


@pytest.mark.parametrize("failure", ["download", "nonzero", "missing-shell"])
def test_update_failure_retains_current_entry(owned, monkeypatch, failure):
    _, bin_dir, environment = owned
    original = os.readlink(bin_dir / "fe-cli")

    def fetch(*args, **kwargs):
        if failure == "download":
            raise UpdateError("Download failed")
        return PAYLOAD

    def run(argv, **kwargs):
        if failure == "missing-shell":
            raise OSError("No sh available")
        assert failure == "nonzero"
        return subprocess.CompletedProcess(argv, 31)

    monkeypatch.setattr(self_update, "fetch_bytes", fetch)
    monkeypatch.setattr(self_update.subprocess, "run", run)
    with pytest.raises(UpdateError, match="Download failed|exit 31|Could not run"):
        self_update.run_update(RELEASE)
    assert os.readlink(bin_dir / "fe-cli") == original
    assert (environment / "bin" / "fe-cli").read_text() == "current launcher"
