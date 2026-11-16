from pathlib import Path

import pytest
from textual.widgets import (
    Button,
    Collapsible,
    Input,
    OptionList,
    RichLog,
    Select,
    Static,
    TextArea,
)

from fe_cli.app import CommandInput, FactorTerminal
from fe_cli.backend import DemoBackend
from fe_cli.screens import RunEditor


def run_editor(app):
    editor = app.query_one(RunEditor)
    assert app._run_editor is editor
    assert len(app.screen_stack) == 1
    assert editor.screen is app.home
    assert app.home.query_one("#run-editor-host").display
    return editor


@pytest.mark.parametrize("size", [(120, 40), (80, 24), (60, 20)])
async def test_terminal_layout_and_menu(tmp_path: Path, size: tuple[int, int]) -> None:
    app = FactorTerminal(backend=DemoBackend(tmp_path))
    async with app.run_test(size=size) as pilot:
        field = app.query_one("#command", Input)
        assert field.has_focus
        assert "LOCAL DEMO" in str(app.query_one("#mode", Static).render())
        await pilot.press("/")
        menu = app.query_one("#commands", OptionList)
        assert menu.display and menu.option_count == 14
        assert "/status" in app.commands
        assert "/setup" not in app.commands
        await pilot.press("down", "tab")
        assert field.value == "/jobs "
        await pilot.press("enter")
        assert field.value == ""
        assert not menu.display
        assert field.region.bottom <= size[1]
        assert app.query_one("#transcript", RichLog).region.height >= 3


@pytest.mark.parametrize("value", ["/status", "/setup"])
async def test_demo_status_reports_local_defaults_without_creating_jobs(tmp_path, value):
    backend = DemoBackend(tmp_path)
    app = FactorTerminal(backend=backend)
    async with app.run_test() as pilot:
        app.query_one("#command", Input).value = value
        await pilot.press("enter")
        output = "\n".join(line.text for line in app.query_one("#transcript", RichLog).lines)
        assert "Mode: local demo. SSH access is not checked in this mode." in output
        assert "Next task:" in output
        assert not backend.jobs()


async def test_inline_command_arguments_are_hints_only(tmp_path: Path) -> None:
    app = FactorTerminal(backend=DemoBackend(tmp_path))
    async with app.run_test(size=(80, 24)) as pilot:
        field = app.query_one("#command", CommandInput)
        field.value = "/fa"
        await pilot.pause()
        assert field.argument_hint.startswith("ctor [list | new")
        await pilot.press("tab")
        assert field.value == "/factor "
        assert field.argument_hint.startswith("[list | new")
        assert not app.query_one("#commands", OptionList).display
        assert field._value.plain == field.value + field.argument_hint
        await pilot.press("right")
        assert field.value == "/factor "
        field.value = "/factor edit "
        await pilot.pause()
        assert field.argument_hint == "ID"
        await pilot.press("home")
        assert field._value.plain == field.value
        await pilot.press("end")
        assert field._value.plain.endswith(" ID")
        field.value = "/factor edit sample"
        await pilot.pause()
        assert field.argument_hint == ""
        field.value = "/node "
        await pilot.pause()
        assert field.argument_hint == "[A | C]"
        await pilot.press("enter")
        assert app.query_one("#config-picker").display
        await pilot.press("escape")
        assert field.value == "" and field.argument_hint == ""


async def test_command_hint_tracks_menu_selection(tmp_path: Path) -> None:
    app = FactorTerminal(backend=DemoBackend(tmp_path))
    async with app.run_test() as pilot:
        field = app.query_one("#command", CommandInput)
        await pilot.press("/")
        assert field.argument_hint == "run"
        await pilot.press("down")
        assert field.argument_hint == "jobs"
        await pilot.press("escape")
        assert field.argument_hint == ""


async def test_run_confirmation_persistence_and_report(tmp_path: Path) -> None:
    now = [100.0]
    backend = DemoBackend(tmp_path, clock=lambda: now[0])
    app = FactorTerminal(backend=backend)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.press("/", "r", "u", "n", "enter")
        run_editor(app)
        assert not backend.jobs()
        await pilot.press("enter")
        await pilot.pause()
        job = backend.jobs()[0]
        assert job.status == "PENDING"
        assert not app.query("#jobs")
        now[0] += 6
        field = app.query_one("#command", Input)
        field.value = f"/results {job.id}"
        await pilot.press("enter")
        log = app.query_one("#transcript", RichLog)
        rendered = "\n".join(line.text for line in log.lines)
        assert "Synthetic data" in rendered
        assert "Simulated elapsed  5.000 s" in rendered
        assert "Historical PIT" in rendered
        field.value = f"/results {job.id} --raw"
        await pilot.press("enter")
        assert "historical_pit" in "\n".join(line.text for line in log.lines)
        assert DemoBackend(tmp_path, clock=lambda: now[0]).get(job.id).status == "COMPLETED"


async def test_form_validation_and_back(tmp_path: Path) -> None:
    backend = DemoBackend(tmp_path)
    app = FactorTerminal(backend=backend)
    async with app.run_test(size=(80, 24)) as pilot:
        app.action_new_run()
        await pilot.pause()
        editor = run_editor(app)
        editor.query_one("#start-date", Input).value = "2027-01-01"
        await pilot.press("enter")
        assert run_editor(app) is editor
        assert "Start date" in str(editor.query_one("#run-error", Static).render())
        assert not backend.jobs()
        editor.query_one("#start-date", Input).value = "20261001"
        await pilot.press("enter")
        assert "YYYY-MM-DD" in str(editor.query_one("#run-error", Static).render())
        assert not backend.jobs()
        await pilot.press("escape")
        assert app._run_editor is None and not app.query(RunEditor)
        assert app.query_one("#command", Input).has_focus


async def test_cancel_and_error_recovery(tmp_path: Path) -> None:
    backend = DemoBackend(tmp_path, clock=lambda: 100)
    job = backend.submit()
    app = FactorTerminal(backend=backend)
    async with app.run_test() as pilot:
        field = app.query_one("#command", Input)
        for command in ("/cancel", "/unknown", "'", "/results unknown", f"/cancel {job.id}"):
            field.value = command
            await pilot.press("enter")
        assert backend.get(job.id).status == "CANCELLED"
        assert not app.query("#jobs")
        assert field.has_focus


async def test_jobs_are_on_demand_and_keep_literal_markup(tmp_path: Path) -> None:
    now = [100.0]
    backend = DemoBackend(tmp_path, clock=lambda: now[0])
    job = backend.submit(factor="[red]literal[/red]")
    app = FactorTerminal(backend=backend)
    async with app.run_test() as pilot:
        field = app.query_one("#command", Input)
        log = app.query_one("#transcript", RichLog)
        assert job.id not in "\n".join(line.text for line in log.lines)
        assert not app.query("#jobs")
        field.value = "/jobs"
        await pilot.press("enter")
        rendered = "\n".join(line.text for line in log.lines)
        assert job.id in rendered
        assert "[red]literal[/red]" in rendered
        assert "Queued" in rendered
        assert field.has_focus
        now[0] += 6
        field.value = "/jobs"
        await pilot.press("enter")
        assert "Completed" in "\n".join(line.text for line in log.lines)
        assert backend.get(job.id).status == "COMPLETED"


async def test_form_focus_while_jobs_progress(tmp_path: Path) -> None:
    now = [100.0]
    backend = DemoBackend(tmp_path, clock=lambda: now[0])
    job = backend.submit()
    app = FactorTerminal(backend=backend)
    async with app.run_test(size=(80, 24)) as pilot:
        app.action_new_run()
        await pilot.pause()
        now[0] += 6
        await pilot.press("ctrl+n", "ctrl+l")
        assert len(app.screen_stack) == 1
        assert len(app.query(RunEditor)) == 1
        run_editor(app)
        assert backend.get(job.id).status == "COMPLETED"
        assert not app.home.query("#jobs")
        await pilot.press("escape")
        assert app.query_one("#command", Input).has_focus


async def test_run_editor_stays_above_command_and_cancels_at_80x24(tmp_path: Path) -> None:
    backend = DemoBackend(tmp_path)
    app = FactorTerminal(backend=backend)
    async with app.run_test(size=(80, 24)) as pilot:
        home = app.screen
        app.action_new_run()
        await pilot.pause()
        editor = run_editor(app)
        field = editor.query_one("#factor-name", Input)
        command_field = app.home.query_one("#command", Input)
        assert field.has_focus
        assert app.screen is home
        assert editor.region.height > 0 and editor.region.y >= 0
        assert editor.region.bottom <= command_field.region.y
        assert command_field.region.bottom <= app.screen.region.bottom
        shortcuts = editor.query_one("#run-shortcuts", Static)
        assert shortcuts.region.height == 1
        assert editor.region.contains_region(shortcuts.region)
        assert "Enter" in str(shortcuts.render())
        assert "Ctrl+J newline" in str(shortcuts.render())
        assert "Esc cancel" in str(shortcuts.render())
        assert not editor.query(Button)
        app.action_new_run()
        await pilot.pause()
        assert run_editor(app) is editor and len(app.query(RunEditor)) == 1
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is home and not app.query(RunEditor)
        assert not app.home.query_one("#run-editor-host").display
        assert command_field.has_focus and not backend.jobs()


async def test_run_formula_ctrl_j_inserts_newline_then_enter_submits(tmp_path: Path) -> None:
    backend = DemoBackend(tmp_path)
    app = FactorTerminal(backend=backend)
    async with app.run_test(size=(80, 24)) as pilot:
        app.action_new_run()
        await pilot.pause()
        editor = run_editor(app)
        formula = editor.query_one("#formula", TextArea)
        formula.load_text("rank(close)")
        formula.move_cursor((0, len(formula.text)))
        formula.focus()
        await pilot.press("ctrl+j")
        assert formula.text == "rank(close)\n"
        assert run_editor(app) is editor and not backend.jobs()
        await pilot.press("enter")
        await pilot.pause()
        assert len(backend.jobs()) == 1
        assert backend.jobs()[0].formula == "rank(close)"
        assert not app.query(RunEditor)
        assert app.home.query_one("#command", Input).has_focus


async def test_run_details_and_resource_selection_enter_do_not_submit(tmp_path: Path) -> None:
    backend = DemoBackend(tmp_path)
    app = FactorTerminal(backend=backend)
    async with app.run_test(size=(80, 24)) as pilot:
        app.action_new_run()
        await pilot.pause()
        editor = run_editor(app)
        details = editor.query_one(Collapsible)
        assert details.collapsed
        details.query_one("CollapsibleTitle").focus()
        await pilot.press("enter")
        assert not details.collapsed
        assert run_editor(app) is editor and not backend.jobs()
        cpu = editor.query_one("#run-cpus", Select)
        cpu.focus()
        await pilot.press("enter")
        assert cpu.expanded
        assert run_editor(app) is editor and not backend.jobs()
        await pilot.press("up", "enter")
        assert not cpu.expanded and cpu.value == 5
        assert run_editor(app) is editor and not backend.jobs()
        await pilot.press("escape")
        assert not app.query(RunEditor)
        assert app.home.query_one("#command", Input).has_focus
