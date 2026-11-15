from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from textual.widgets import Input, RichLog, Static

from fe_cli.app import FactorTerminal
from fe_cli.backend import DemoBackend
from fe_cli.catalog import FactorDefinition
from fe_cli.config import ConnectionConfig
from fe_cli.date_range import DateRange, DateRangeStore
from fe_cli.range_editor import DateRangeEditor
from fe_cli.runtime_settings import RuntimeSettings, RuntimeStore
from fe_cli.workspace import Workspace


async def command(app, pilot, value):
    field = app.query_one("#command", Input)
    field.value = value
    field.focus()
    await pilot.press("enter")


def transcript(app):
    return "\n".join(line.text for line in app.query_one("#transcript", RichLog).lines)


async def test_range_persists_and_runs_keep_overrides_separate(tmp_path):
    store = DateRangeStore(tmp_path / "range.json")
    backend = DemoBackend(tmp_path / "demo", clock=lambda: 100)
    app = FactorTerminal(backend=backend, date_range_store=store)
    async with app.run_test(size=(80, 24)) as pilot:
        await command(app, pilot, "/range 2020-01-01 2025-12-31")
        assert app.date_range == store.load() == DateRange("2020-01-01", "2025-12-31")
        await command(app, pilot, "/run")
        assert app.query_one("#start-date", Input).value == "2020-01-01"
        assert app.query_one("#end-date", Input).value == "2025-12-31"
        app.query_one("#start-date", Input).value = "2022-01-01"
        app.query_one("#end-date", Input).value = "2022-12-31"
        await pilot.press("enter")
        job = backend.jobs()[0]
        assert (job.start, job.end) == ("2022-01-01", "2022-12-31")
        assert store.load() == DateRange("2020-01-01", "2025-12-31")
        await command(app, pilot, "/range 2023-01-01 2024-12-31")
        assert backend.get(job.id).start == "2022-01-01"
    restored = FactorTerminal(backend=backend, date_range_store=store)
    assert restored.date_range == DateRange("2023-01-01", "2024-12-31")


@pytest.mark.parametrize("size", [(80, 24), (60, 20)])
async def test_range_inline_edit_cancel_and_enter_save(tmp_path, size):
    store = DateRangeStore(tmp_path / "range.json")
    app = FactorTerminal(backend=DemoBackend(tmp_path / "demo"), date_range_store=store)
    async with app.run_test(size=size) as pilot:
        await command(app, pilot, "/range")
        editor = app.query_one(DateRangeEditor)
        assert len(app.screen_stack) == 1 and editor.screen is app.home
        assert app.query_one("#range-start", Input).has_focus
        assert editor.region.bottom <= app.query_one("#command", Input).region.y
        assert not store.path.exists()
        app.query_one("#range-start", Input).value = "2020-02-29"
        await pilot.press("escape")
        assert app._range_editor is None and not store.path.exists()
        assert app.query_one("#command", Input).has_focus
        await command(app, pilot, "/range")
        app.query_one("#range-start", Input).value = "2020-02-29"
        app.query_one("#range-end", Input).value = "2024-02-29"
        await pilot.press("enter")
        assert store.load() == DateRange("2020-02-29", "2024-02-29")
        assert app._range_editor is None


@pytest.mark.parametrize("line", [
    "/range 2020-01-01", "/range nope 2026-01-01", "/range 2025-01-01 2020-01-01",
])
async def test_invalid_range_leaves_saved_default_unchanged(tmp_path, line):
    store = DateRangeStore(tmp_path / "range.json")
    store.save(DateRange())
    original = store.path.read_bytes()
    app = FactorTerminal(backend=DemoBackend(tmp_path / "demo"), date_range_store=store)
    async with app.run_test() as pilot:
        await command(app, pilot, line)
        assert "Error:" in transcript(app)
        assert store.path.read_bytes() == original and app.date_range == DateRange()


async def test_range_editor_keeps_draft_and_blocks_other_edits(tmp_path):
    store = DateRangeStore(tmp_path / "range.json")
    app = FactorTerminal(backend=DemoBackend(tmp_path / "demo"), date_range_store=store)
    async with app.run_test() as pilot:
        await command(app, pilot, "/range")
        editor = app._range_editor
        app.query_one("#range-start", Input).value = "2027-01-01"
        await pilot.press("enter")
        assert app._range_editor is editor and not store.path.exists()
        assert "start date must not be after end date" in str(
            editor.query_one("#range-error", Static).render()
        )
        for value in ("/range", "/run", "/factor new", "/node C", "/resources 2"):
            await command(app, pilot, value)
            assert app._range_editor is editor and not app._run_editor and not app._factor_editor
        await pilot.press("ctrl+n")
        assert app._range_editor is editor
        await command(app, pilot, "/help")
        assert "default date" in transcript(app).lower()
        app.query_one("#range-start", Input).value = "2020-01-01"
        await pilot.press("enter")
        assert app._range_editor is None and store.load().start == "2020-01-01"


async def test_online_range_is_local_and_prefills_real_run(tmp_path):
    store = DateRangeStore(tmp_path / "range.json")
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    workspace.catalog.add(FactorDefinition("example", "Example", 'col("ret")'))
    RuntimeStore(workspace.root).save(RuntimeSettings(
        "/approved/python", data_root="/fixture/data", release_root="/approved/release",
        release_sha256="a" * 64,
    ))
    remote = SimpleNamespace(config=ConnectionConfig("fixture.invalid"), query=AsyncMock())
    app = FactorTerminal(online=remote, workspace=workspace, date_range_store=store)
    async with app.run_test() as pilot:
        await command(app, pilot, "/range 2021-01-01 2022-12-31")
        await command(app, pilot, "/run example")
        assert app.query_one("#start-date", Input).value == "2021-01-01"
        assert app.query_one("#end-date", Input).value == "2022-12-31"
        remote.query.assert_not_called()
        assert app.backend is None
        await pilot.press("escape")
