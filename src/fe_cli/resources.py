"""Local resource preferences for synthetic jobs and manual submission guidance."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

NODE_CPU_LIMITS = {"quant-a": 6, "quant-c": 8}


def normalize_node(value: str) -> str:
    """Accept the short A/C selectors and return a canonical node name."""
    if isinstance(value, str):
        candidate = value.strip().lower()
        if candidate in {"a", "c"}:
            candidate = f"quant-{candidate}"
        if candidate in NODE_CPU_LIMITS:
            return candidate
    raise ValueError("Node must be A or C (quant-a or quant-c)")


@dataclass(frozen=True)
class ResourceRequest:
    """Each requested CPU is paired with 3 GB; allocation may round on the server."""

    node: str = "quant-a"
    cpus: int = 6

    def __post_init__(self) -> None:
        node = normalize_node(self.node)
        limit = NODE_CPU_LIMITS[node]
        if type(self.cpus) is not int or not 1 <= self.cpus <= limit:
            raise ValueError(f"CPU count for {node} must be an integer from 1 to {limit}")
        object.__setattr__(self, "node", node)

    @property
    def memory_gb(self) -> int:
        return self.cpus * 3

    @property
    def memory(self) -> str:
        return f"{self.memory_gb}G"


class ResourceStore:
    """Read and atomically save local preferences, preserving invalid files."""

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            root = os.environ.get("XDG_CONFIG_HOME")
            path = (Path(root) if root else Path.home() / ".config") / "fe-cli" / "resources.json"
        self.path = Path(path)

    def load(self) -> ResourceRequest:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ResourceRequest()
        except UnicodeError as exc:
            raise self._invalid("expected UTF-8 text") from exc
        try:
            state = json.loads(text)
            if not isinstance(state, dict) or set(state) != {"version", "resources"}:
                raise ValueError("expected only version and resources fields")
            if type(state["version"]) is not int or state["version"] != 1:
                raise ValueError("unsupported resource preference version")
            resources = state["resources"]
            if not isinstance(resources, dict) or set(resources) != {"node", "cpus"}:
                raise ValueError("resources must contain only node and cpus")
            request = ResourceRequest(**resources)
            if resources["node"] != request.node:
                raise ValueError("saved node must use quant-a or quant-c")
            return request
        except (ValueError, TypeError, OverflowError, RecursionError) as exc:
            raise self._invalid(str(exc)) from exc

    def save(self, request: ResourceRequest) -> Path:
        if not isinstance(request, ResourceRequest):
            raise ValueError("Resource preferences must be a ResourceRequest")
        request = ResourceRequest(request.node, request.cpus)
        self.load()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".resources-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(
                    {"version": 1, "resources": {"node": request.node, "cpus": request.cpus}},
                    stream,
                    indent=2,
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return self.path

    def _invalid(self, reason: str) -> ValueError:
        return ValueError(
            f"Invalid resource preferences at {self.path}: {reason}. "
            "Back up or move this file before saving preferences; it was not overwritten."
        )
