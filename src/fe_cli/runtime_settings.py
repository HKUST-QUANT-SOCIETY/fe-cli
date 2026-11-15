"""Local configuration of the approved server runtime and data location."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True)
class RuntimeSettings:
    remote_python: str
    data_root: str = ""
    node: str = "quant-a"
    environment: str = "research"
    time_limit: str = "00:30:00"
    release_root: str | None = None
    release_sha256: str | None = None

    def __post_init__(self) -> None:
        for field in ("remote_python", "data_root"):
            value = getattr(self, field)
            if field == "data_root" and value == "":
                continue
            if (
                not isinstance(value, str)
                or not value.startswith("/")
                or any(ord(c) < 32 for c in value)
            ):
                raise ValueError(f"{field} must be an absolute server path")
        if (self.release_root is None) != (self.release_sha256 is None):
            raise ValueError("release_root and release_sha256 must be configured together")
        if self.release_root is not None:
            if (
                not isinstance(self.release_root, str)
                or not self.release_root.startswith("/")
                or ".." in PurePosixPath(self.release_root).parts
                or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in self.release_root)
            ):
                raise ValueError("release_root must be a safe absolute server path")
            if not isinstance(self.release_sha256, str) or not re.fullmatch(
                r"[a-f0-9]{64}", self.release_sha256
            ):
                raise ValueError("release_sha256 must be a lowercase SHA-256 digest")
        nodes = {"quant-a", "quant-c"}
        if self.release_root is not None:
            nodes.add("shared")
        if self.node not in nodes:
            raise ValueError(
                "Runtime node must be quant-a or quant-c (public releases allow shared)"
            )
        if not isinstance(self.environment, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]+", self.environment
        ):
            raise ValueError("Invalid registered environment name")
        if not isinstance(self.time_limit, str) or not re.fullmatch(
            r"\d{2}:\d{2}:\d{2}", self.time_limit
        ):
            raise ValueError("Time limit must use HH:MM:SS")
        hours, minutes, seconds = map(int, self.time_limit.split(":"))
        total = hours * 3600 + minutes * 60 + seconds
        if minutes > 59 or seconds > 59 or not 1 <= total <= 86400:
            raise ValueError("Time limit must be between one second and 24 hours")


class RuntimeStore:
    def __init__(self, workspace: Path) -> None:
        self.path = Path(workspace) / "runtime.json"

    def load(self) -> RuntimeSettings:
        try:
            payload = self.path.read_bytes()
        except FileNotFoundError as exc:
            raise ValueError(
                "Run runtime is not configured. Use fe configure-run --help."
            ) from exc
        if len(payload) > 65536:
            raise ValueError("Runtime settings exceed the size limit")
        try:
            doc = json.loads(payload)
            if (
                set(doc) != {"version", "runtime"}
                or type(doc["version"]) is not int
                or doc["version"] != 1
            ):
                raise ValueError("Unsupported runtime settings format")
            return RuntimeSettings(**doc["runtime"])
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            raise ValueError(f"Invalid runtime settings at {self.path}: {exc}") from exc

    def save(self, settings: RuntimeSettings) -> Path:
        if self.path.exists():
            self.load()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".runtime-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "runtime": asdict(settings)}, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return self.path
