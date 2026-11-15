"""Validated date windows and local default preferences for future runs."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class DateRange:
    """An inclusive calendar window with strict YYYY-MM-DD bounds."""

    start: str = "2026-10-01"
    end: str = "2026-10-31"

    def __post_init__(self) -> None:
        for label, value in (("start", self.start), ("end", self.end)):
            if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                raise ValueError(f"{label} date must use YYYY-MM-DD")
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError(f"{label} date must be a real calendar date (YYYY-MM-DD)") from exc
        if self.start > self.end:
            raise ValueError("start date must not be after end date")


class DateRangeStore:
    """Read and atomically save date preferences, preserving invalid files."""

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            root = os.environ.get("XDG_CONFIG_HOME")
            path = (Path(root) if root else Path.home() / ".config") / "fe-cli" / "range.json"
        self.path = Path(path)

    def load(self) -> DateRange:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return DateRange()
        except UnicodeError as exc:
            raise self._invalid("expected UTF-8 text") from exc
        try:
            state = json.loads(text)
            if not isinstance(state, dict) or set(state) != {"version", "range"}:
                raise ValueError("expected only version and range fields")
            if type(state["version"]) is not int or state["version"] != 1:
                raise ValueError("unsupported date range preference version")
            window = state["range"]
            if not isinstance(window, dict) or set(window) != {"start", "end"}:
                raise ValueError("range must contain only start and end")
            return DateRange(**window)
        except (ValueError, TypeError, OverflowError, RecursionError) as exc:
            raise self._invalid(str(exc)) from exc

    def save(self, window: DateRange) -> Path:
        if not isinstance(window, DateRange):
            raise ValueError("Date range preferences must be a DateRange")
        window = DateRange(window.start, window.end)
        self.load()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".range-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "range": asdict(window)}, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return self.path

    def _invalid(self, reason: str) -> ValueError:
        return ValueError(
            f"Invalid date range preferences at {self.path}: {reason}. "
            "Back up or move this file before saving preferences; it was not overwritten."
        )
