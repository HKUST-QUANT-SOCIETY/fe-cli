"""Offline release construction checks using a real fixed Git archive and fake uv."""

import hashlib
import importlib.util
import io
import json
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "cli_release_builder", Path(__file__).resolve().parents[1] / "scripts/build_cli_release.py"
)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "scripts").mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "fe-cli"\nversion = "0.1.0"\n')
    (root / "uv.lock").write_text("fixed lock")
    (root / ".gitignore").write_text("ignored.txt\n")
    (root / "scripts/install.sh").write_text(
        "#!/bin/sh\n# @FE_CLI_VERSION@ @FE_CLI_REPOSITORY@\n"
        "# @WHEEL_SHA256@ @REQUIREMENTS_SHA256@ @MANIFEST_SHA256@\n"
    )
    for argv in (["init", "--quiet"], ["add", "."],
                 ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                  "commit", "--quiet", "-m", "fixed source"]):
        subprocess.run(["git", *argv], cwd=root, check=True, capture_output=True)
    return root


@pytest.fixture
def fake_uv(monkeypatch):
    original_run = builder.run
    observations = []

    def execute(argv, cwd):
        if argv[0] != "uv":
            return original_run(argv, cwd)
        observations.append((argv, cwd))
        assert "0.1.0" in (cwd / "pyproject.toml").read_text()
        assert not (cwd / "dirty.txt").exists()
        assert not (cwd / "ignored.txt").exists()
        assert (cwd / "uv.lock").read_text() == "fixed lock"
        if argv[1] == "build":
            output = Path(argv[argv.index("--out-dir") + 1])
            with zipfile.ZipFile(output / "fe_cli-0.1.0-py3-none-any.whl", "w") as package:
                package.writestr("fe_cli-0.1.0.dist-info/METADATA",
                                 "Name: fe-cli\nVersion: 0.1.0\n")
            with tarfile.open(output / "fe_cli-0.1.0.tar.gz", "w:gz") as package:
                payload = b"Name: fe-cli\nVersion: 0.1.0\n"
                member = tarfile.TarInfo("fe_cli-0.1.0/PKG-INFO")
                member.size = len(payload)
                package.addfile(member, io.BytesIO(payload))
        else:
            Path(argv[argv.index("--output-file") + 1]).write_text(
                "textual==0.89.1 \\\n    --hash=sha256:" + "a" * 64 + "\n"
            )
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(builder, "run", execute)
    return observations


def test_release_uses_fixed_archive_and_complete_asset_hashes(repository, fake_uv, tmp_path):
    (repository / "pyproject.toml").write_text("dirty invalid project metadata")
    (repository / "scripts/install.sh").write_text("dirty template")
    (repository / "dirty.txt").write_text("untracked")
    (repository / "ignored.txt").write_text("ignored")
    output = tmp_path / "release assets"
    manifest = builder.build_release(output, "Example/fe-cli", offline=True, repo_root=repository)
    assert manifest == json.loads((output / "release.json").read_text())
    assert manifest["schema"] == "fe-cli-install-release-v1"
    assert manifest["version"] == "0.1.0"
    assert manifest["download_repository"] == "Example/fe-cli"
    assert manifest["source_commit"] == builder.run(
        ["git", "rev-parse", "HEAD"], repository
    ).stdout.decode().strip()
    assert manifest["core"]["installed_by_cli"] is False
    assert manifest["core"]["compatibility_status"] == "unverified"
    compatibility = manifest["compatibility"]
    assert compatibility["installer"] == {
        "os": "Linux", "architecture": "x86_64", "glibc": ">=2.28", "python": "3.12",
    }
    assert compatibility["installer_targets"] == [
        compatibility["installer"],
        {"os": "macOS", "minimum_version": "14", "architecture": "arm64", "python": "3.12",
         "validation_status": "pending_native_validation"},
        {"os": "macOS", "minimum_version": "14", "architecture": "x86_64", "python": "3.12",
         "validation_status": "pending_native_validation"},
    ]
    assert "install.sh" not in manifest["artifacts"]
    assert "release.json" not in manifest["artifacts"]
    assert len(manifest["artifacts"]) == 3
    assert "--hash=sha256:" in (output / "requirements.txt").read_text()
    installer = (output / "install.sh").read_text()
    assert "@" not in installer
    assert "0.1.0 Example/fe-cli" in installer
    assert builder.digest(output / "release.json") in installer
    checksums = dict(line.split("  ", 1)[::-1]
                     for line in (output / "SHA256SUMS").read_text().splitlines())
    assert set(checksums) == {path.name for path in output.iterdir()} - {"SHA256SUMS"}
    for name, expected in checksums.items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == expected
    assert all("--offline" in argv for argv, _ in fake_uv)
    export = fake_uv[1][0]
    assert {"--locked", "--no-dev", "--no-emit-project", "--no-annotate", "--no-header"} <= set(
        export
    )
    assert len({cwd for _, cwd in fake_uv}) == 1
    assert (repository / "uv.lock").read_text() == "fixed lock"


@pytest.mark.parametrize("name", ["", "owner", "owner/repo/extra", "../repo", "a/..",
                                 "-owner/repo", "a/repo;echo hi", "https://github.com/a/b"])
def test_invalid_download_repository_rejected(tmp_path, name):
    with pytest.raises(ValueError, match="OWNER/REPO"):
        builder.build_release(tmp_path / "output", name, repo_root=tmp_path)
    assert not (tmp_path / "output").exists()


def test_existing_output_rejected_without_changes(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    with pytest.raises(FileExistsError):
        builder.build_release(output, "owner/repo", repo_root=tmp_path)
    assert list(output.iterdir()) == []


def test_built_version_mismatch_rejected(repository, fake_uv, monkeypatch, tmp_path):
    execute = builder.run

    def wrong_version(argv, cwd):
        result = execute(argv, cwd)
        if argv[:2] == ["uv", "build"]:
            output = Path(argv[argv.index("--out-dir") + 1])
            wheel = next(output.glob("*.whl"))
            with zipfile.ZipFile(wheel, "w") as package:
                package.writestr("fe_cli-0.1.0.dist-info/METADATA",
                                 "Name: fe-cli\nVersion: 9.9.9\n")
        return result

    monkeypatch.setattr(builder, "run", wrong_version)
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="differs from fixed source"):
        builder.build_release(output, "owner/repo", repo_root=repository)
    assert not output.exists()


def test_missing_fixed_template_fails_before_build(repository, fake_uv, tmp_path):
    subprocess.run(["git", "rm", "scripts/install.sh"], cwd=repository,
                   check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=f@example.invalid",
                    "commit", "--quiet", "-m", "remove template"],
                   cwd=repository, check=True, capture_output=True)
    (repository / "scripts").mkdir(exist_ok=True)
    (repository / "scripts/install.sh").write_text("uncommitted replacement")
    with pytest.raises(FileNotFoundError):
        builder.build_release(tmp_path / "output", "owner/repo", repo_root=repository)
    assert fake_uv == []
