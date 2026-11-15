"""Explicit updates for environments owned by the official release installer."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from . import __version__
from .updates import ReleaseInfo, UpdateError, fetch_bytes

_INSTALL_METHOD = (
    "Self-update requires the official curl installer and its active fe-cli launcher. "
    "For source, uv tool, or pip installations, update with the original installation method. "
    "Older installer environments without ownership records require an explicit installer run."
)


def _record(path: Path, limit: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Missing or linked ownership record")
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Oversized ownership record")
    return data


def _installation() -> tuple[Path, Path]:
    """Identify ownership from the venv prefix, never the venv's Python symlink."""
    environment = Path(sys.prefix)
    try:
        if not environment.is_absolute() or environment.resolve() != environment:
            raise ValueError("Noncanonical environment")
        if environment.parent.name != "versions":
            raise ValueError("Unmanaged environment layout")
        manifest_bytes = _record(environment / "fe-cli-release.json", 256 * 1024)
        digest = hashlib.sha256(manifest_bytes).hexdigest()
        ready = _record(environment / ".fe-cli-ready", 65).decode("ascii")
        manifest = json.loads(manifest_bytes)
        if (
            not isinstance(manifest, dict)
            or manifest.get("product") != "fe-cli"
            or manifest.get("schema") != "fe-cli-install-release-v1"
            or manifest.get("version") != __version__
            or ready != digest + "\n"
            or environment.name != f"{__version__}-{digest}"
        ):
            raise ValueError("Installation identity differs")
        recorded_bin = _record(environment / ".fe-cli-bin-dir", 4096).decode("utf-8")
        if not recorded_bin.endswith("\n") or "\n" in recorded_bin[:-1]:
            raise ValueError("Invalid launcher directory record")
        bin_dir = Path(recorded_bin[:-1])
        if (
            "\x00" in recorded_bin
            or not bin_dir.is_absolute()
            or bin_dir.resolve() != bin_dir
        ):
            raise ValueError("Invalid launcher directory")
        launcher = bin_dir / "fe-cli"
        if (
            not launcher.is_symlink()
            or os.readlink(launcher) != str(environment / "bin" / "fe-cli")
            or not launcher.is_file()
        ):
            raise ValueError("Launcher no longer selects the current environment")
        return environment.parent.parent, bin_dir
    except (OSError, ValueError, UnicodeError) as error:
        raise UpdateError(_INSTALL_METHOD) from error


def run_update(release: ReleaseInfo) -> None:
    """Verify a fixed official installer, then let it atomically select a new environment."""
    root, bin_dir = _installation()
    payload = fetch_bytes(release.installer_url, limit=256 * 1024, timeout=15.0)
    if hashlib.sha256(payload).hexdigest() != release.installer_sha256:
        raise UpdateError("Installer SHA256 differs from the GitHub release digest; stopped.")
    environment = os.environ.copy()
    environment.pop("FE_CLI_RELEASE_DIR", None)
    environment.update(
        FE_CLI_INSTALL_ROOT=str(root),
        FE_CLI_BIN_DIR=str(bin_dir),
        FE_CLI_EXPECTED_VERSION=release.version,
    )
    try:
        with tempfile.TemporaryDirectory(prefix="fe-cli-update-") as directory:
            script = Path(directory) / "install.sh"
            script.write_bytes(payload)
            result = subprocess.run(
                ["sh", str(script)], cwd=directory, env=environment, check=False, shell=False,
            )
    except OSError as error:
        raise UpdateError(f"Could not run the release installer: {error}") from error
    if result.returncode:
        raise UpdateError(
            f"Release installer failed (exit {result.returncode}); environments are retained."
        )
    try:
        launcher = bin_dir / "fe-cli"
        link_target = os.readlink(launcher)
        destination = Path(link_target)
        if (
            not destination.is_absolute()
            or str(destination) != link_target
            or destination.name != "fe-cli"
            or destination.parent.name != "bin"
            or destination.parent.parent.parent != root / "versions"
            or destination.resolve() != destination
            or not destination.is_file()
        ):
            raise UpdateError("Active CLI version mismatch: launcher ownership changed.")
        probe = subprocess.run(
            [str(launcher), "--version"], shell=False, capture_output=True, text=True, timeout=5,
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        raise UpdateError(f"Active CLI version mismatch: check failed ({error}).") from error
    if probe.returncode or probe.stdout.strip() != f"fe-cli {release.version}":
        raise UpdateError("Active CLI version mismatch: requested release is not active.")
