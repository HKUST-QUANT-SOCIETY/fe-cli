"""Local, non-secret SSH connection settings."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

_SAFE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")
_SAFE_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


def _has_control(value: str) -> bool:
    return any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)


@dataclass(frozen=True)
class ConnectionConfig:
    host: str
    user: str = ""
    ssh_executable: str = "ssh"
    identity_file: str | None = None
    port: int = 22
    connect_timeout: int = 10
    timeout: int = 30

    def __post_init__(self) -> None:
        if (
            not isinstance(self.host, str)
            or len(self.host) > 253
            or not _SAFE_HOST.fullmatch(self.host)
        ):
            raise ValueError("Host must be a hostname, IPv4 address, or safe SSH alias.")
        if not isinstance(self.user, str) or (
            self.user and not _SAFE_NAME.fullmatch(self.user)
        ):
            raise ValueError(
                "SSH user must contain only letters, numbers, underscores, dots or '-'."
            )
        if (
            not isinstance(self.ssh_executable, str)
            or not self.ssh_executable.strip()
            or _has_control(self.ssh_executable)
        ):
            raise ValueError("SSH executable must be a nonempty executable name or path.")
        if self.identity_file is not None and (
            not isinstance(self.identity_file, str)
            or not self.identity_file.strip()
            or _has_control(self.identity_file)
        ):
            raise ValueError("Identity file must be a nonempty path without control characters.")
        for name, maximum in (("port", 65535), ("connect_timeout", 300), ("timeout", 3600)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be an integer between 1 and {maximum}.")


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "fe-cli" / "connection.json"


def load_config(path: Path | None = None) -> ConnectionConfig:
    """Read settings only; never read an identity file or create directories."""
    target = Path(path) if path is not None else default_config_path()
    try:
        with target.open("r", encoding="utf-8") as handle:
            content = handle.read(65537)
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"Connection config not found at {target}. Run 'fe configure' first."
        ) from exc
    except UnicodeError as exc:
        raise ValueError(f"Invalid connection encoding at {target}; expected UTF-8 JSON.") from exc
    if len(content) > 65536:
        raise ValueError(f"Connection config at {target} exceeds the 64 KiB limit.")
    try:
        document = json.loads(content)
    except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
        raise ValueError(
            f"Invalid connection JSON at {target}; repair it before configuring."
        ) from exc
    if not isinstance(document, dict) or set(document) != {"version", "connection"}:
        raise ValueError(f"Invalid connection config at {target}: expected version and connection.")
    if type(document["version"]) is not int or document["version"] != 1:
        raise ValueError(f"Unsupported connection config version at {target}; expected version 1.")
    values = document["connection"]
    if not isinstance(values, dict):
        raise ValueError(f"Invalid connection settings at {target}: expected an object.")
    try:
        return ConnectionConfig(**values)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid connection settings at {target}: {exc}") from exc


def save_config(config: ConnectionConfig, path: Path | None = None) -> Path:
    """Atomically replace valid local settings, protecting malformed existing files."""
    if not isinstance(config, ConnectionConfig):
        raise ValueError("Expected ConnectionConfig settings.")
    config.__post_init__()
    target = Path(path) if path is not None else default_config_path()
    if target.exists() or target.is_symlink():
        load_config(target)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = json.dumps({"version": 1, "connection": asdict(config)}, indent=2) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target
