"""Online terminal run flows use saved factors and explicit confirmation only."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from textual.widgets import Button, Input, RichLog, Select, Static, TextArea

from fe_cli.app import FactorTerminal
from fe_cli.archive_io import ExecutionFailed
from fe_cli.catalog import FactorDefinition
from fe_cli.config import ConnectionConfig
from fe_cli.factor_screen import FactorEditor
from fe_cli.remote import RemoteResult
from fe_cli.resources import ResourceRequest
from fe_cli.runtime_settings import RuntimeSettings, RuntimeStore
from fe_cli.screens import RunEditor
from fe_cli.workspace import Workspace


@pytest.fixture
def online_run(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    factor = FactorDefinition("selected", "Selected factor", "rank(close)", True, "CSV notes")
    workspace.catalog.add(factor)
    runtime = RuntimeSettings(
        remote_python="/approved/runtime/bin/python", data_root="/fixture/data",
        release_root="/approved/release", release_sha256="a" * 64,
    )
    RuntimeStore(workspace.root).save(runtime)
    remote = SimpleNamespace(
        config=ConnectionConfig(host="cluster.example", user="researcher"),
        query=AsyncMock(side_effect=AssertionError("No SSH queries in run tests")),
        diagnose=AsyncMock(side_effect=AssertionError("No SSH authentication in run tests")),
    )
    request_id = "a" * 32
    submitted = {"request_id": request_id, "job_id": "981", "status": "SUBMITTED"}
    receipt = {
        **submitted,
        "status": "COMMITTED",
        "rows": 42,
        "values_path": str(workspace.root / "values" / "selected.parquet"),
        "manifest_path": str(workspace.root / "runs" / request_id / "manifest.json"),
        "timing": {"elapsed_seconds": 12.345},
    }
    service = Mock(
        submit=AsyncMock(return_value=submitted),
        collect=AsyncMock(return_value=receipt),
    )
    service.get.return_value = submitted
    factory = Mock(return_value=service)
    monkeypatch.setattr("fe_cli.run_ui.RunService", factory)
    monkeypatch.setattr(
        "fe_cli.app.DemoBackend", Mock(side_effect=AssertionError("No demo fallback"))
    )
    app = FactorTerminal(online=remote, workspace=workspace)
    return SimpleNamespace(
        app=app, workspace=workspace, factor=factor, runtime=runtime, remote=remote,
        service=service, factory=factory, submitted=submitted, receipt=receipt,
    )


def transcript(app):
    return "\n".join(line.text for line in app.home.query_one("#transcript", RichLog).lines)


async def command(app, pilot, value):
    app.home.query_one("#command", Input).value = value
    await pilot.press("enter")


def run_editor(app):
    editor = app.query_one(RunEditor)
    assert app._run_editor is editor
    assert len(app.screen_stack) == 1
    assert editor.screen is app.home
    assert app.home.query_one("#run-editor-host").display
    return editor


@pytest.mark.parametrize("selected", [False, True])
async def test_online_run_editor_requires_confirmation_and_uses_default_resources(
    online_run, selected
):
    app = online_run.app
    if selected:
        app.library.selected_id = "selected"
    async with app.run_test(size=(100, 45)) as pilot:
        await command(app, pilot, "/run" if selected else "/run selected")
        editor = run_editor(app)
        assert editor.online is True
        assert editor.query_one("#factor-name", Input).value == "selected"
        assert editor.query_one("#formula", TextArea).text == "rank(close)"
        assert "Enter" in str(editor.query_one("#run-shortcuts", Static).render())
        assert "submit" in str(editor.query_one("#run-shortcuts", Static).render()).lower()
        assert not editor.query(Button)
        assert editor.query_one("#run-node", Select).value == "quant-a"
        assert editor.query_one("#run-cpus", Select).value == 6
        summary = str(editor.query_one("#run-resources-summary", Static).render())
        assert "6 CPU" in summary and "18 GB" in summary
        online_run.factory.assert_not_called()
        await pilot.press("escape")
        assert len(app.screen_stack) == 1
        assert app._run_editor is None and not app.query(RunEditor)
        assert app.home.query_one("#command", Input).has_focus
        online_run.service.submit.assert_not_called()
        online_run.remote.query.assert_not_called()


async def test_online_confirm_submits_saved_factor_snapshot_and_edited_window(online_run):
    app = online_run.app
    original = online_run.workspace.catalog.path.read_bytes()
    async with app.run_test(size=(100, 45)) as pilot:
        await command(app, pilot, "/run selected")
        editor = run_editor(app)
        editor.query_one("#formula", TextArea).load_text("rank(volume)")
        editor.query_one("#start-date", Input).value = "2026-09-01"
        editor.query_one("#end-date", Input).value = "2026-09-30"
        editor.query_one("#run-cpus", Select).value = 3
        await pilot.pause()
        online_run.factory.assert_not_called()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        factor, resources, start, end = online_run.service.submit.await_args.args
        assert factor.factor_id == "selected"
        assert factor.name == "Selected factor"
        assert factor.description == "CSV notes"
        assert factor.expression == "rank(volume)"
        assert resources == ResourceRequest("A", 3)
        assert (start, end) == ("2026-09-01", "2026-09-30")
        online_run.service.submit.assert_awaited_once()
        online_run.factory.assert_called_once_with(
            online_run.workspace, online_run.remote.config, settings=online_run.runtime
        )
        assert online_run.workspace.catalog.path.read_bytes() == original
        output = transcript(app)
        assert "Submitted job 981" in output
        assert online_run.submitted["request_id"] in output
        assert "/results 981" in output
        assert not app._query_pending
        assert app.run_actions.last_request_id == online_run.submitted["request_id"]
        online_run.remote.query.assert_not_called()


async def test_inline_run_keeps_draft_during_commands_and_routes_confirmation(online_run):
    app = online_run.app
    online_run.remote.query.side_effect = None
    online_run.remote.query.return_value = RemoteResult("cluster-status", "No jobs", "")
    async with app.run_test(size=(80, 24)) as pilot:
        await command(app, pilot, "/run selected")
        editor = run_editor(app)
        formula = editor.query_one("#formula", TextArea)
        formula.load_text("rank(volume)")
        field = app.home.query_one("#command", Input)
        for value in ("/resources 2", "/factor new", "/run selected"):
            field.focus()
            await command(app, pilot, value)
            assert run_editor(app) is editor
            assert formula.text == "rank(volume)"
            assert app.resources == ResourceRequest()
            assert not app.query(FactorEditor)
            assert not app.home.query_one("#config-picker").display
            online_run.factory.assert_not_called()
        field.focus()
        await command(app, pilot, "/jobs")
        await app.workers.wait_for_complete()
        online_run.remote.query.assert_awaited_once_with("cluster-status")
        assert run_editor(app) is editor
        assert formula.text == "rank(volume)"
        assert not app._query_pending
        field.focus()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        online_run.service.submit.assert_awaited_once()
        assert online_run.service.submit.await_args.args[0].expression == "rank(volume)"
        assert app._run_editor is None and not app.query(RunEditor)
        assert field.has_focus and len(app.screen_stack) == 1


@pytest.mark.parametrize("problem,expected", [
    ("runtime", "configure-run --help"),
    ("disabled", "Enable this factor before running"),
    ("missing", "Unknown factor_id"),
    ("unselected", "Select a saved factor"),
])
async def test_online_run_invalid_setup_never_opens_editor_or_submits(
    online_run, problem, expected
):
    if problem == "runtime":
        RuntimeStore(online_run.workspace.root).path.unlink()
    elif problem == "disabled":
        online_run.workspace.catalog.update("selected", enabled=False)
    value = "/run" if problem == "unselected" else (
        "/run missing" if problem == "missing" else "/run selected"
    )
    app = online_run.app
    async with app.run_test(size=(100, 45)) as pilot:
        await command(app, pilot, value)
        assert len(app.screen_stack) == 1
        assert not app.query(RunEditor)
        assert expected in transcript(app)
        online_run.factory.assert_not_called()
        online_run.remote.query.assert_not_called()
        assert not app._query_pending


async def test_disabled_factor_is_rechecked_on_confirmation(online_run):
    app = online_run.app
    async with app.run_test(size=(100, 45)) as pilot:
        await command(app, pilot, "/run selected")
        editor = run_editor(app)
        online_run.workspace.catalog.update("selected", enabled=False)
        await pilot.press("enter")
        await pilot.pause()
        assert run_editor(app) is editor
        assert "This factor is disabled" in str(editor.query_one("#run-error", Static).render())
        online_run.factory.assert_not_called()
        assert not app._query_pending
        online_run.workspace.catalog.update("selected", enabled=True)
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        online_run.service.submit.assert_awaited_once()
        assert not app.query(RunEditor)


async def test_pending_read_only_query_preserves_run_draft_until_confirmation_retry(online_run):
    app = online_run.app
    started, release = asyncio.Event(), asyncio.Event()

    async def query(command):
        started.set()
        await release.wait()
        return RemoteResult(command, "No jobs", "")

    online_run.remote.query.side_effect = query
    async with app.run_test(size=(80, 24)) as pilot:
        await command(app, pilot, "/run selected")
        editor = run_editor(app)
        editor.query_one("#formula", TextArea).load_text("rank(volume)")
        field = app.home.query_one("#command", Input)
        field.focus()
        await command(app, pilot, "/jobs")
        await asyncio.wait_for(started.wait(), timeout=2)
        try:
            await pilot.press("enter")
            await pilot.pause()
            assert run_editor(app) is editor
            assert editor.query_one("#formula", TextArea).text == "rank(volume)"
            assert "in progress" in str(editor.query_one("#run-error", Static).render())
            assert app._query_pending
            online_run.factory.assert_not_called()
        finally:
            release.set()
        await app.workers.wait_for_complete()
        assert not app._query_pending
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        online_run.service.submit.assert_awaited_once()
        assert online_run.service.submit.await_args.args[0].expression == "rank(volume)"
        assert not app.query(RunEditor)


@pytest.mark.parametrize("already_committed", [False, True])
async def test_online_results_reports_committed_paths_and_preserves_request_identity(
    online_run, already_committed
):
    app = online_run.app
    if already_committed:
        online_run.service.get.return_value = online_run.receipt
    async with app.run_test(size=(100, 45)) as pilot:
        await command(app, pilot, f"/results {online_run.submitted['request_id']}")
        await app.workers.wait_for_complete()
        online_run.service.get.assert_called_once_with(online_run.submitted["request_id"])
        if already_committed:
            online_run.service.collect.assert_not_called()
        else:
            online_run.service.collect.assert_awaited_once_with(
                "981", request_id=online_run.submitted["request_id"]
            )
        output = transcript(app)
        assert "Local result committed" in output
        assert "42 rows" in output
        assert "Run elapsed  12.345 s" in output
        assert "excludes queue and download" in output
        unwrapped = output.replace("\n", "")
        assert online_run.receipt["values_path"] in unwrapped
        assert online_run.receipt["manifest_path"] in unwrapped
        assert "Result saved locally" in str(app.home.query_one("#mode", Static).render())
        assert not app._query_pending
        online_run.remote.query.assert_not_called()


@pytest.mark.parametrize("error", [
    ValueError("Bad manifest"),
    OSError("Download interrupted"),
    ExecutionFailed({
        "error": {"type": "NativeCompileError", "message": "bad expression"},
        "timing": {"elapsed_seconds": 0.125},
    }),
])
async def test_online_collection_failure_is_visible_and_clears_pending(online_run, error):
    app = online_run.app
    online_run.service.collect.side_effect = error
    async with app.run_test(size=(100, 45)) as pilot:
        await command(app, pilot, "/results 981")
        await app.workers.wait_for_complete()
        assert f"Run action failed: {error}" in transcript(app)
        if isinstance(error, ExecutionFailed):
            assert "Run elapsed  0.125 s" in transcript(app)
        assert "Local result committed" not in transcript(app)
        assert "Run action failed" in str(app.home.query_one("#mode", Static).render())
        assert not app._query_pending


async def test_old_result_without_timing_stays_readable(online_run):
    online_run.receipt.pop("timing")
    online_run.service.get.return_value = online_run.receipt
    async with online_run.app.run_test() as pilot:
        await command(online_run.app, pilot, "/results 981")
        await online_run.app.workers.wait_for_complete()
        assert "Run elapsed  unavailable" in transcript(online_run.app)
        assert "Local result committed" in transcript(online_run.app)
        online_run.service.collect.assert_not_called()


async def test_online_explicit_request_recovers_unknown_job(online_run):
    online_run.service.get.return_value = {
        "request_id": "a" * 32, "job_id": None, "status": "SUBMISSION_UNKNOWN"
    }
    app = online_run.app
    async with app.run_test(size=(100, 45)) as pilot:
        await command(app, pilot, "/results 981 " + "a" * 32)
        await app.workers.wait_for_complete()
        online_run.service.collect.assert_awaited_once_with("981", request_id="a" * 32)
        assert "Local result committed" in transcript(app)


async def test_online_saved_failure_shows_reason_without_redownload(online_run):
    online_run.service.get.return_value = {
        "request_id": "a" * 32, "job_id": "981", "status": "EXECUTION_FAILED",
        "error": "Job 981 failed: NativeCompileError: use col",
        "timing": {"elapsed_seconds": 0.125},
    }
    app = online_run.app
    async with app.run_test(size=(100, 45)) as pilot:
        await command(app, pilot, "/results 981")
        await app.workers.wait_for_complete()
        assert "Job 981 failed: NativeCompileError" in transcript(app)
        assert "Run elapsed  0.125 s" in transcript(app)
        assert "Local result committed" not in transcript(app)
        online_run.service.collect.assert_not_called()


async def test_online_unknown_submission_shows_recovery_message(online_run):
    app = online_run.app
    online_run.service.submit.side_effect = ValueError(
        "SUBMISSION_UNKNOWN; check /jobs and /history before submitting again."
    )
    async with app.run_test(size=(100, 45)) as pilot:
        await command(app, pilot, "/run selected")
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        output = transcript(app)
        assert "Run action failed: SUBMISSION_UNKNOWN" in output
        assert "check /jobs and /history" in output
        assert "Submitted job" not in output
        assert app.run_actions.last_request_id is None
        assert not app._query_pending


async def test_public_runtime_opens_run_editor_on_c_without_forcing_a(online_run):
    release = "/public/fe/releases/test-release"
    RuntimeStore(online_run.workspace.root).save(RuntimeSettings(
        remote_python=release + "/.venv/bin/python", node="shared",
        data_root="/fixture/data",
        release_root=release, release_sha256="a" * 64,
    ))
    app = online_run.app
    app.resources = ResourceRequest("C", 6)
    async with app.run_test(size=(100, 45)) as pilot:
        await command(app, pilot, "/run selected")
        editor = app.query_one(RunEditor)
        assert editor.online
        assert editor.query_one("#run-node", Select).value == "quant-c"
        online_run.service.submit.assert_not_called()


async def test_online_pending_submission_does_not_start_duplicate_actions(online_run):
    app = online_run.app
    started, release = asyncio.Event(), asyncio.Event()

    async def submit(*args):
        started.set()
        await release.wait()
        return online_run.submitted

    online_run.service.submit.side_effect = submit
    async with app.run_test(size=(100, 45)) as pilot:
        await command(app, pilot, "/run selected")
        await pilot.press("enter")
        await asyncio.wait_for(started.wait(), timeout=2)
        try:
            await command(app, pilot, "/run selected")
            await command(app, pilot, "/results 981")
            assert "A server action is in progress" in transcript(app)
            assert len(app.screen_stack) == 1
            online_run.service.submit.assert_awaited_once()
            online_run.service.collect.assert_not_called()
            online_run.factory.assert_called_once()
        finally:
            release.set()
        await app.workers.wait_for_complete()
        assert not app._query_pending
