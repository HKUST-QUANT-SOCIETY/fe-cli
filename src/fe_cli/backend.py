"""A local, synthetic job lifecycle for trying the interface without an engine."""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from fe_cli.resources import ResourceRequest

# An illustrative formula, not a claim about the production f01 definition.
F01_FORMULA = 'ts_mean(col("ret"), 20)'
_STATUSES = {"PENDING", "RUNNING", "COMPLETED", "CANCELLED", "FAILED"}
_TERMINAL = {"COMPLETED", "CANCELLED", "FAILED"}


@dataclass(frozen=True)
class Job:
    id: str
    factor: str
    formula: str
    start: str
    end: str
    created_at: float
    status: str
    cpus: int = 6
    memory: str = "18G"
    node: str = "quant-a"
    error: str | None = None

    @property
    def progress(self) -> int:
        """Coarse synthetic progress; this does not measure engine execution."""
        return {"RUNNING": 50, "COMPLETED": 100}.get(self.status, 0)


class DemoBackend:
    """Persist demo jobs locally, progressing after two and five seconds.

    State calls take a nonblocking advisory lock and reload before writing.
    Atomic replacement protects existing state if serialization/write fails.
    """

    def __init__(
        self, state_dir: Path | None = None, clock: Callable[[], float] = time.time
    ) -> None:
        if state_dir is None:
            state_root = os.environ.get("XDG_STATE_HOME")
            state_dir = (
                Path(state_root) if state_root else Path.home() / ".local" / "state"
            ) / "fe-cli"
        self.state_dir = Path(state_dir)
        self.state_path = self.state_dir / "demo-state.json"
        self._clock = clock

    def submit(
        self,
        factor: str = "f01",
        formula: str = F01_FORMULA,
        start: str = "2026-10-01",
        end: str = "2026-10-31",
        *,
        fail: bool = False,
        node: str = "quant-a",
        cpus: int = 6,
    ) -> Job:
        self._validate_inputs(factor, formula, start, end)
        resources = ResourceRequest(node=node, cpus=cpus)
        if not isinstance(fail, bool):
            raise ValueError("fail must be a boolean")
        now = self._now()
        with self._locked():
            state = self._current(now)
            job = Job(
                id=f"demo-{state['next_id']:04d}",
                factor=factor.strip(),
                formula=formula.strip(),
                start=start,
                end=end,
                created_at=now,
                status="PENDING",
                node=resources.node,
                cpus=resources.cpus,
                memory=resources.memory,
            )
            state["jobs"].append(asdict(job))
            state["next_id"] += 1
            if fail:
                state["fail_ids"].append(job.id)
            self._write(state)
            return job

    def jobs(self) -> list[Job]:
        with self._locked():
            state = self._current(self._now())
            return [Job(**record) for record in reversed(state["jobs"])]

    def get(self, job_id: str) -> Job:
        for job in self.jobs():
            if job.id == job_id:
                return job
        raise ValueError(f"Unknown demo job: {job_id}")

    def cancel(self, job_id: str) -> Job:
        with self._locked():
            state = self._current(self._now())
            for record in state["jobs"]:
                if record["id"] == job_id:
                    if record["status"] in _TERMINAL:
                        raise ValueError(f"Cannot cancel {record['status']} job {job_id}")
                    record["status"] = "CANCELLED"
                    self._write(state)
                    return Job(**record)
        raise ValueError(f"Unknown demo job: {job_id}")

    def report(self, job_id: str) -> dict:
        job = self.get(job_id)
        if job.status != "COMPLETED":
            raise ValueError("A demo report is available only for COMPLETED jobs")
        # Deliberately synthetic values, stable across processes and reloads.
        days = (date.fromisoformat(job.end) - date.fromisoformat(job.start)).days + 1
        rows = days * 100
        return {
            "demo": True,
            "status": "PASS",
            "job_id": job.id,
            "factor": job.factor,
            "formula": job.formula,
            "start": job.start,
            "end": job.end,
            "node": job.node,
            "cpus": job.cpus,
            "memory": job.memory,
            "output_rows": rows,
            "valid_rows": rows,
            "nonfinite_valid_rows": 0,
            "historical_pit": "UNKNOWN",
            "timing": {"elapsed_seconds": 5.0},
            "note": (
                "Synthetic demo report: no factor engine or market data was executed. "
                "Elapsed time is the simulated submission-to-completion duration."
            ),
        }

    def _now(self) -> float:
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Demo clock must return a finite timestamp")
        if not math.isfinite(value):
            raise ValueError("Demo clock must return a finite timestamp")
        return float(value)

    @staticmethod
    def _validate_inputs(factor: str, formula: str, start: str, end: str) -> None:
        for label, value, limit in (("factor", factor, 128), ("formula", formula, 4096)):
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise ValueError(f"{label} must contain 1–{limit} characters")
        for label, value in (("start", start), ("end", end)):
            try:
                if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                    raise ValueError
            except (ValueError, TypeError):
                raise ValueError(f"{label} must be an ISO date (YYYY-MM-DD)") from None
        if start > end:
            raise ValueError("start date must not be after end date")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with (self.state_dir / ".demo-state.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise OSError("Demo state is busy in another client; retry shortly") from None
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _read(self) -> dict:
        try:
            text = self.state_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"version": 1, "next_id": 1, "jobs": [], "fail_ids": []}
        except UnicodeError as exc:
            raise ValueError(
                f"Invalid demo state at {self.state_path}: expected UTF-8 text. "
                "Back up or move this file before starting fresh; it was not overwritten."
            ) from exc
        try:
            state = json.loads(text)
            self._validate_state(state)
            return state
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
            raise ValueError(
                f"Invalid demo state at {self.state_path}: {exc}. "
                "Back up or move this file before starting fresh; it was not overwritten."
            ) from exc

    def _validate_state(self, state: dict) -> None:
        if (
            not isinstance(state, dict)
            or type(state.get("version")) is not int
            or state["version"] != 1
        ):
            raise ValueError("unsupported or missing state version")
        if isinstance(state["next_id"], bool) or not isinstance(state["next_id"], int):
            raise ValueError("next_id must be an integer")
        if not isinstance(state["jobs"], list) or not isinstance(state["fail_ids"], list):
            raise ValueError("jobs and fail_ids must be lists")
        identifiers = set()
        largest = 0
        for record in state["jobs"]:
            if not isinstance(record, dict):
                raise ValueError("job must be an object")
            job = Job(**record)
            if not isinstance(job.id, str) or not re.fullmatch(r"demo-[0-9]{4,}", job.id):
                raise ValueError("invalid job identifier")
            if job.id in identifiers:
                raise ValueError("duplicate job identifier")
            identifiers.add(job.id)
            largest = max(largest, int(job.id[5:]))
            self._validate_inputs(job.factor, job.formula, job.start, job.end)
            if isinstance(job.created_at, bool) or not isinstance(job.created_at, (int, float)):
                raise ValueError("job timestamp must be finite")
            if not math.isfinite(job.created_at):
                raise ValueError("job timestamp must be finite")
            if not isinstance(job.status, str) or job.status not in _STATUSES:
                raise ValueError("invalid job status")
            resources = ResourceRequest(node=job.node, cpus=job.cpus)
            if job.node != resources.node or job.memory != resources.memory:
                raise ValueError("invalid resource fields")
            if job.error is not None and not isinstance(job.error, str):
                raise ValueError("invalid error field")
        if state["next_id"] <= largest:
            raise ValueError("next_id must exceed existing job identifiers")
        if not all(isinstance(v, str) and v in identifiers for v in state["fail_ids"]):
            raise ValueError("invalid failure identifiers")

    def _current(self, now: float) -> dict:
        state = self._read()
        changed = False
        for record in state["jobs"]:
            if record["status"] in _TERMINAL:
                continue
            elapsed = max(0, now - record["created_at"])
            status = record["status"]
            if elapsed >= 5:
                status = "FAILED" if record["id"] in state["fail_ids"] else "COMPLETED"
            elif elapsed >= 2:
                status = "RUNNING"
            if status != record["status"]:
                record["status"] = status
                if status == "FAILED":
                    record["error"] = "Simulated demo failure; no engine was executed."
                changed = True
        if changed:
            self._write(state)
        return state

    def _write(self, state: dict) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=".demo-state-", dir=self.state_dir)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(state, stream, indent=2, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
        finally:
            Path(temporary).unlink(missing_ok=True)
