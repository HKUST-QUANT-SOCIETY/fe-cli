"""Deterministic checks for the local demo lifecycle and state boundary."""

import fcntl
import json
from dataclasses import FrozenInstanceError

import pytest

from fe_cli.backend import F01_FORMULA, DemoBackend


@pytest.fixture
def demo(tmp_path):
    now = [1000.0]
    return DemoBackend(tmp_path, lambda: now[0]), now


def test_lifecycle_persistence_and_monotonic_ids(demo, tmp_path):
    backend, now = demo
    first = backend.submit()
    assert first.id == "demo-0001"
    assert first.formula == F01_FORMULA
    assert first.status == "PENDING"
    now[0] += 2
    assert backend.get(first.id).status == "RUNNING"
    assert backend.get(first.id).progress == 50
    reloaded = DemoBackend(tmp_path, lambda: now[0])
    second = reloaded.submit(factor="f02")
    assert second.id == "demo-0002"
    assert [job.id for job in backend.jobs()] == [second.id, first.id]
    now[0] += 3
    assert backend.get(first.id).status == "COMPLETED"
    assert backend.get(first.id).progress == 100
    assert reloaded.get(second.id).status == "RUNNING"
    # Persisted state does not regress when a wall clock moves backwards.
    now[0] = 0
    assert backend.get(first.id).status == "COMPLETED"
    assert backend.get(second.id).status == "RUNNING"


@pytest.mark.parametrize("elapsed", [0, 3])
def test_cancellation_is_terminal(demo, elapsed):
    backend, now = demo
    job = backend.submit()
    now[0] += elapsed
    assert backend.cancel(job.id).status == "CANCELLED"
    now[0] += 100
    assert backend.get(job.id).status == "CANCELLED"
    with pytest.raises(ValueError, match="Cannot cancel CANCELLED"):
        backend.cancel(job.id)
    with pytest.raises(ValueError, match="COMPLETED"):
        backend.report(job.id)


def test_failure_survives_reload_and_report_is_gated(demo, tmp_path):
    backend, now = demo
    job = backend.submit(fail=True)
    with pytest.raises(ValueError, match="COMPLETED"):
        backend.report(job.id)
    now[0] += 5
    failed = DemoBackend(tmp_path, lambda: now[0]).get(job.id)
    assert failed.status == "FAILED"
    assert "Simulated" in failed.error
    with pytest.raises(ValueError, match="Cannot cancel FAILED"):
        backend.cancel(job.id)
    with pytest.raises(ValueError, match="COMPLETED"):
        backend.report(job.id)


def test_report_is_explicitly_synthetic_and_deterministic(demo, tmp_path):
    backend, now = demo
    job = backend.submit(start="2026-10-01", end="2026-10-02")
    now[0] += 5
    report = backend.report(job.id)
    assert report["demo"] is True
    assert report["status"] == "PASS"
    assert report["output_rows"] == report["valid_rows"] == 200
    assert report["nonfinite_valid_rows"] == 0
    assert report["historical_pit"] == "UNKNOWN"
    assert report["timing"] == {"elapsed_seconds": 5.0}
    assert "Synthetic" in report["note"]
    assert "no factor engine or market data" in report["note"]
    assert "simulated submission-to-completion duration" in report["note"]
    now[0] += 86400
    assert report == DemoBackend(tmp_path, lambda: now[0]).report(job.id)
    with pytest.raises(ValueError, match="Cannot cancel COMPLETED"):
        backend.cancel(job.id)


@pytest.mark.parametrize(
    "values",
    [
        {"factor": " "},
        {"factor": "x" * 129},
        {"formula": ""},
        {"formula": "x" * 4097},
        {"start": "20261001"},
        {"end": "2026-02-30"},
        {"start": "2026-11-01", "end": "2026-10-31"},
        {"fail": "yes"},
        {"node": "quant-b"},
        {"node": "A", "cpus": 7},
        {"node": "C", "cpus": 9},
        {"cpus": True},
        {"cpus": 0},
        {"cpus": 2.0},
    ],
)
def test_invalid_inputs_do_not_write_state(demo, values):
    backend, _ = demo
    with pytest.raises(ValueError):
        backend.submit(**values)
    assert not backend.state_path.exists()


@pytest.mark.parametrize("timestamp", [float("nan"), float("inf"), True, "today"])
def test_invalid_clock_is_rejected(tmp_path, timestamp):
    backend = DemoBackend(tmp_path, lambda: timestamp)
    with pytest.raises(ValueError, match="finite timestamp"):
        backend.submit()
    assert not backend.state_path.exists()


@pytest.mark.parametrize("content", ["{broken", "[]", '{"version": 99}', '{"version": true}'])
def test_corrupt_state_is_not_overwritten(tmp_path, content):
    backend = DemoBackend(tmp_path)
    backend.state_path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid demo state.*not overwritten"):
        backend.submit()
    assert backend.state_path.read_text(encoding="utf-8") == content


def test_deeply_nested_state_is_rejected_and_preserved(tmp_path):
    backend = DemoBackend(tmp_path)
    content = "[" * 10_000 + "]" * 10_000
    backend.state_path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid demo state.*not overwritten"):
        backend.jobs()
    assert backend.state_path.read_text(encoding="utf-8") == content


@pytest.mark.parametrize("field,value", [
    ("created_at", float("nan")), ("status", "BOGUS"),
    ("cpus", True), ("cpus", 7), ("cpus", 0), ("cpus", 2.0),
    ("node", "quant-b"), ("node", "A"), ("memory", "19G"), ("memory", 18),
])
def test_corrupt_job_is_not_overwritten(demo, field, value):
    backend, _ = demo
    backend.submit()
    state = json.loads(backend.state_path.read_text(encoding="utf-8"))
    state["jobs"][0][field] = value
    content = json.dumps(state)
    backend.state_path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid demo state"):
        backend.jobs()
    assert backend.state_path.read_text(encoding="utf-8") == content


def test_lock_conflict_is_reported_without_writing(demo):
    backend, _ = demo
    backend.submit()
    original = backend.state_path.read_bytes()
    with (backend.state_dir / ".demo-state.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(OSError, match="busy"):
            backend.submit()
    assert backend.state_path.read_bytes() == original


def test_atomic_write_failure_preserves_old_state(demo, monkeypatch):
    backend, _ = demo
    backend.submit()
    original = backend.state_path.read_bytes()

    def reject_replace(*args):
        raise OSError("simulated filesystem failure")

    monkeypatch.setattr("fe_cli.backend.os.replace", reject_replace)
    with pytest.raises(OSError, match="filesystem failure"):
        backend.submit()
    assert backend.state_path.read_bytes() == original
    assert not list(backend.state_dir.glob(".demo-state-*"))


def test_invalid_text_state_has_actionable_error(demo):
    backend, _ = demo
    backend.state_path.write_bytes(b"\xff")
    with pytest.raises(ValueError, match="expected UTF-8.*not overwritten"):
        backend.jobs()
    assert backend.state_path.read_bytes() == b"\xff"


def test_unknown_job_and_immutable_snapshots(demo):
    backend, _ = demo
    for method in (backend.get, backend.cancel, backend.report):
        with pytest.raises(ValueError, match="Unknown demo job"):
            method("demo-9999")
    with pytest.raises(FrozenInstanceError):
        backend.submit().status = "COMPLETED"


def test_default_state_directory_uses_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    backend = DemoBackend()
    assert backend.state_dir == tmp_path / "fe-cli"


def test_default_state_directory_falls_back_to_home(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert DemoBackend().state_dir == tmp_path / ".local" / "state" / "fe-cli"


def test_requested_resources_are_independent_persisted_job_snapshots(demo, tmp_path):
    backend, now = demo
    first = backend.submit()
    second = backend.submit(node="C", cpus=7)
    third = backend.submit(node="a", cpus=1)
    assert (first.node, first.cpus, first.memory) == ("quant-a", 6, "18G")
    assert (second.node, second.cpus, second.memory) == ("quant-c", 7, "21G")
    assert (third.node, third.cpus, third.memory) == ("quant-a", 1, "3G")
    now[0] += 5
    reloaded = DemoBackend(tmp_path, lambda: now[0])
    for job in (first, second, third):
        snapshot = reloaded.get(job.id)
        assert (snapshot.node, snapshot.cpus, snapshot.memory) == (
            job.node, job.cpus, job.memory
        )
        report = reloaded.report(job.id)
        assert (report["node"], report["cpus"], report["memory"]) == (
            job.node, job.cpus, job.memory
        )


def test_invalid_resource_request_cannot_advance_existing_state(demo):
    backend, now = demo
    backend.submit()
    original = backend.state_path.read_bytes()
    now[0] += 10
    with pytest.raises(ValueError, match="CPU count"):
        backend.submit(node="C", cpus=9)
    assert backend.state_path.read_bytes() == original
