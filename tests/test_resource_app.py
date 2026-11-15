from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from textual.widgets import Input, OptionList, RichLog, Select, Static

from fe_cli.app import FactorTerminal
from fe_cli.backend import DemoBackend
from fe_cli.config import ConnectionConfig
from fe_cli.picker import InlinePicker
from fe_cli.resources import ResourceRequest, ResourceStore
from fe_cli.screens import RunEditor


async def command(app, pilot, value):
    app.home.query_one("#command", Input).value = value
    await pilot.press("enter")


def transcript(app):
    return "\n".join(line.text for line in app.home.query_one("#transcript", RichLog).lines)


async def test_online_preferences_persist_without_query_or_demo(tmp_path: Path):
    store = ResourceStore(tmp_path / "resources.json")
    online = SimpleNamespace(config=ConnectionConfig("cluster.example"), query=AsyncMock())
    app = FactorTerminal(online=online, resource_store=store)
    async with app.run_test() as pilot:
        await command(app, pilot, "/node c")
        await command(app, pilot, "/resources 8")
        assert app.resources == ResourceRequest("C", 8)
        assert store.load() == ResourceRequest("C", 8)
        assert "24 GB" in str(app.query_one("#resource-status", Static).render())
        await command(app, pilot, "/node A")
        assert app.resources == ResourceRequest("C", 8)
        assert "from 1 to 6" in transcript(app)
        assert store.load() == ResourceRequest("C", 8)
        online.query.assert_not_called()
        assert app.backend is None
    restored = FactorTerminal(online=online, resource_store=store)
    assert restored.resources == ResourceRequest("C", 8)


async def test_per_task_override_preserves_defaults_and_existing_jobs(tmp_path: Path):
    backend = DemoBackend(tmp_path / "state", clock=lambda: 100)
    store = ResourceStore(tmp_path / "resources.json")
    app = FactorTerminal(backend=backend, resource_store=store)
    async with app.run_test(size=(100, 40)) as pilot:
        await command(app, pilot, "/node C")
        await command(app, pilot, "/resources 4")
        await command(app, pilot, "/run")
        editor = app.query_one(RunEditor)
        assert len(app.screen_stack) == 1 and editor.screen is app.home
        assert editor.query_one("#run-node", Select).value == "quant-c"
        assert editor.query_one("#run-cpus", Select).value == 4
        editor.query_one("#run-cpus", Select).value = 8
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        job = backend.jobs()[0]
        assert (job.node, job.cpus, job.memory) == ("quant-c", 8, "24G")
        assert store.load() == ResourceRequest("C", 4)
        await command(app, pilot, "/resources 2")
        await command(app, pilot, "/node A")
        await command(app, pilot, "/run")
        editor = app.query_one(RunEditor)
        assert editor.query_one("#run-node", Select).value == "quant-a"
        assert editor.query_one("#run-cpus", Select).value == 2
        await pilot.press("escape")
        assert backend.get(job.id) == job
        await command(app, pilot, "/jobs")
        assert "quant-c" in transcript(app) and "24G" in transcript(app)


async def test_invalid_and_cancelled_resource_changes_leave_preferences_unchanged(tmp_path: Path):
    store = ResourceStore(tmp_path / "resources.json")
    app = FactorTerminal(backend=DemoBackend(tmp_path / "state"), resource_store=store)
    async with app.run_test(size=(80, 24)) as pilot:
        for value in ("/node B", "/resources 0", "/resources 7", "/resources two", "/node A C"):
            await command(app, pilot, value)
            assert app.resources == ResourceRequest()
            assert not store.path.exists()
        for value in ("/node", "/resources"):
            await command(app, pilot, value)
            await pilot.press("down", "escape")
            assert app.resources == ResourceRequest()
            assert not store.path.exists()
            assert app.query_one("#command", Input).has_focus


async def test_failed_resource_save_does_not_change_current_selection(tmp_path: Path, monkeypatch):
    store = ResourceStore(tmp_path / "resources.json")
    app = FactorTerminal(backend=DemoBackend(tmp_path / "state"), resource_store=store)

    def fail_save(request):
        raise OSError("read-only preference directory")

    monkeypatch.setattr(store, "save", fail_save)
    async with app.run_test() as pilot:
        await command(app, pilot, "/node C")
        assert app.resources == ResourceRequest()
        assert "Could not save resource selection" in transcript(app)


@pytest.mark.parametrize("size", [(110, 32), (80, 24), (60, 18)])
async def test_config_picker_stays_above_input_on_same_screen(tmp_path, size):
    store = ResourceStore(tmp_path / "resources.json")
    remote = SimpleNamespace(config=ConnectionConfig("cluster.example"), query=AsyncMock())
    app = FactorTerminal(online=remote, resource_store=store)
    async with app.run_test(size=size) as pilot:
        field = app.query_one("#command", Input)
        picker = app.query_one("#config-picker", InlinePicker)
        await command(app, pilot, "/node")
        assert len(app.screen_stack) == 1
        assert picker.display and field.has_focus
        assert picker.region.bottom == field.region.y
        assert app.query_one("#transcript", RichLog).region.height >= 3
        await pilot.press("down", "enter")
        assert app.resources == ResourceRequest("C", 6)
        assert store.load() == app.resources
        assert not picker.display and field.has_focus
        await command(app, pilot, "/resources")
        assert picker.display and len(app.screen_stack) == 1
        assert picker.region.bottom == field.region.y
        await pilot.press("up", "enter")
        assert app.resources == ResourceRequest("C", 5)
        await command(app, pilot, "/node")
        await pilot.press("up")
        picker.query_one("#config-options", OptionList).focus()
        await pilot.press("escape")
        assert app.resources == ResourceRequest("C", 5)
        assert not picker.display and field.has_focus
        assert app.query_one("#resource-status").region.bottom <= size[1]
        remote.query.assert_not_called()


async def test_typing_new_command_closes_picker_without_applying(tmp_path):
    app = FactorTerminal(backend=DemoBackend(tmp_path))
    async with app.run_test() as pilot:
        await command(app, pilot, "/node")
        await pilot.press("down", "/")
        assert not app.query_one("#config-picker", InlinePicker).display
        assert app.query_one("#commands").display
        assert app.resources == ResourceRequest()
        assert app.query_one("#command", Input).value == "/"
