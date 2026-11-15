#!/usr/bin/env python3
"""Build immutable CLI installation assets from a fixed Git commit; never publish."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import zipfile
from datetime import UTC, datetime
from email.parser import BytesParser
from pathlib import Path

SCHEMA = "fe-cli-install-release-v1"
REPOSITORY = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}\Z")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:rc[0-9]+)?\Z")


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, check=True, capture_output=True)


def _check_package(path: Path, version: str) -> None:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(names) != 1:
                raise ValueError("Wheel requires exactly one package metadata file")
            raw = archive.read(names[0])
    else:
        with tarfile.open(path, "r:gz") as archive:
            members = [m for m in archive.getmembers()
                       if len(Path(m.name).parts) == 2 and m.name.endswith("/PKG-INFO")]
            if len(members) != 1 or not members[0].isfile():
                raise ValueError("Source distribution requires package metadata")
            raw = archive.extractfile(members[0]).read()
    metadata = BytesParser().parsebytes(raw)
    if metadata["Name"] != "fe-cli" or metadata["Version"] != version:
        raise ValueError("Built package name/version differs from fixed source metadata")


def build_release(
    output: Path,
    repository: str,
    *,
    ref: str = "HEAD",
    offline: bool = False,
    repo_root: Path | None = None,
) -> dict:
    if not REPOSITORY.fullmatch(repository) or repository.split("/")[1] in {".", ".."}:
        raise ValueError("repository must be a GitHub OWNER/REPO name")
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"Release output already exists: {output}")
    root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[1]
    commit = run(["git", "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
                 root).stdout.decode().strip()
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("Expected a full Git commit SHA")
    with tempfile.TemporaryDirectory(prefix="fe-cli-release-") as temporary:
        temporary = Path(temporary)
        source, assets = temporary / "source", temporary / "assets"
        source.mkdir()
        assets.mkdir()
        snapshot = temporary / "source.tar"
        with snapshot.open("wb") as handle:
            subprocess.run(["git", "archive", "--format=tar", commit], cwd=root,
                           stdout=handle, stderr=subprocess.PIPE, check=True)
        with tarfile.open(snapshot) as archive:
            archive.extractall(source, filter="data")
        with (source / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle)["project"]
        version = project.get("version", "")
        if (project.get("name") != "fe-cli" or not isinstance(version, str)
                or not VERSION.fullmatch(version)):
            raise ValueError("Fixed source must declare fe-cli and a managed X.Y.Z[rcN] version")
        template = (source / "scripts/install.sh").read_text(encoding="utf-8")
        flags = ["--offline"] if offline else []
        run(["uv", "build", "--wheel", "--sdist", "--out-dir", str(assets), *flags], source)
        run(["uv", "export", "--locked", "--no-dev", "--no-emit-project", "--no-annotate",
             "--no-header", "--output-file", str(assets / "requirements.txt"), *flags], source)
        wheels, sdists = list(assets.glob("*.whl")), list(assets.glob("*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            raise ValueError("Build must produce exactly one wheel and one source distribution")
        wheel, sdist = wheels[0], sdists[0]
        for package in (wheel, sdist):
            _check_package(package, version)
        requirements = assets / "requirements.txt"
        if not requirements.is_file() or not requirements.read_text(encoding="utf-8").strip():
            raise ValueError("Locked dependency export is empty or missing")
        artifacts = {path.name: digest(path) for path in (wheel, sdist, requirements)}
        manifest = {
            "schema": SCHEMA,
            "product": "fe-cli",
            "version": version,
            "source_commit": commit,
            "built_at": datetime.now(UTC).isoformat(),
            "download_repository": repository,
            "artifacts": artifacts,
            "compatibility": {
                "installer": {"os": "Linux", "architecture": "x86_64", "glibc": ">=2.28",
                              "python": "3.12"},
                "package": {"python": ">=3.12"},
            },
            "protocols": {"request_result": "fe-cli-result-v1",
                          "public_release": "fe-cli-public-release-v1"},
            "core": {"installed_by_cli": False, "versioning": "independent",
                     "compatibility_status": "unverified", "real_task_validation": "not_claimed"},
        }
        manifest_path = assets / "release.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                                 encoding="utf-8")
        substitutions = {
            "@FE_CLI_VERSION@": version,
            "@FE_CLI_REPOSITORY@": repository,
            "@WHEEL_SHA256@": artifacts[wheel.name],
            "@REQUIREMENTS_SHA256@": artifacts[requirements.name],
            "@MANIFEST_SHA256@": digest(manifest_path),
        }
        for placeholder, value in substitutions.items():
            if placeholder not in template:
                raise ValueError(f"Installer template lacks {placeholder}")
            template = template.replace(placeholder, value)
        installer = assets / "install.sh"
        installer.write_text(template, encoding="utf-8")
        installer.chmod(0o755)
        files = [wheel, sdist, requirements, manifest_path, installer]
        checksums = "".join(f"{digest(path)}  {path.name}\n"
                            for path in sorted(files))
        checksum_path = assets / "SHA256SUMS"
        checksum_path.write_text(checksums, encoding="utf-8")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.mkdir()
        for path in (*files, checksum_path):
            shutil.copy2(path, output / path.name)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new local asset directory")
    parser.add_argument("--repository", required=True, help="GitHub OWNER/REPO download repository")
    parser.add_argument("--ref", default="HEAD", help="fixed source reference (default: HEAD)")
    parser.add_argument("--offline", action="store_true", help="use existing uv cache only")
    args = parser.parse_args(argv)
    try:
        manifest = build_release(args.output, args.repository, ref=args.ref, offline=args.offline)
    except (ValueError, OSError, subprocess.CalledProcessError, tarfile.TarError,
            zipfile.BadZipFile) as exc:
        parser.exit(1, f"Release build failed: {exc}\n")
    print(json.dumps({"version": manifest["version"], "source_commit": manifest["source_commit"],
                      "output": str(args.output.absolute())}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
