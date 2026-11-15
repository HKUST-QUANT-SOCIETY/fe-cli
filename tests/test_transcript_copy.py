from unittest.mock import Mock

from rich.table import Table
from rich.text import Text
from textual.document._document import Selection
from textual.widgets import Input, Static

from fe_cli.app import FactorTerminal, TranscriptSelection
from fe_cli.backend import DemoBackend


async def test_copy_snapshot_preserves_literals_paths_and_selection_during_output(tmp_path):
    app = FactorTerminal(backend=DemoBackend(tmp_path))
    app.copy_to_clipboard = Mock()
    long_path = "/workspace/" + "factor_results/" * 12 + "result.parquet"
    async with app.run_test(size=(80, 24)) as pilot:
        app.write(long_path)
        app.write("[red]literal[/red] with 中文")
        table = Table("ID", "State")
        table.add_row(Text("demo-1"), Text("Completed"))
        app.append_output(table)
        await pilot.press("f2")
        field = app.query_one("#transcript-selection", TranscriptSelection)
        assert field.has_focus and field.read_only
        assert long_path in field.text
        assert "[red]literal[/red] with 中文" in field.text
        assert "demo-1" in field.text and "Completed" in field.text
        assert "\x1b" not in field.text
        snapshot = field.text
        await pilot.press("ctrl+a")
        assert field.selected_text == snapshot
        app.write("NEW OUTPUT")
        await pilot.pause()
        assert field.text == snapshot and field.selected_text == snapshot
        await pilot.press("ctrl+c")
        app.copy_to_clipboard.assert_called_once_with(snapshot)
        assert app._copy_mode
        assert "Copy request sent" in str(app.query_one("#copy-hint", Static).render())
        await pilot.press("escape")
        assert not app._copy_mode
        assert app.query_one("#command", Input).has_focus
        assert app.query_one("#transcript").display
        await pilot.press("f2")
        assert "NEW OUTPUT" in field.text
        await pilot.press("f2")
        assert not app._copy_mode


async def test_copy_requires_selection_and_read_only_text_stays_unchanged(tmp_path):
    app = FactorTerminal(backend=DemoBackend(tmp_path))
    app.copy_to_clipboard = Mock()
    async with app.run_test() as pilot:
        await pilot.press("f2")
        field = app.query_one("#transcript-selection", TranscriptSelection)
        snapshot = field.text
        await pilot.press("ctrl+c", "x", "backspace", "enter")
        assert field.text == snapshot
        app.copy_to_clipboard.assert_not_called()
        assert "Select text first" in str(app.query_one("#copy-hint", Static).render())
        field.selection = Selection((0, 0), (0, 6))
        await pilot.press("f8")
        app.copy_to_clipboard.assert_called_once_with("Factor")
        await pilot.press("escape")


async def test_mouse_drag_selects_output_and_copies_with_keyboard(tmp_path):
    app = FactorTerminal(backend=DemoBackend(tmp_path))
    app.copy_to_clipboard = Mock()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("f2")
        field = app.query_one("#transcript-selection", TranscriptSelection)
        field.move_cursor((0, 0))
        await pilot.pause()
        await pilot.mouse_down(field, offset=(0, 0))
        await pilot.hover(field, offset=(6, 0))
        await pilot.mouse_up(field, offset=(6, 0))
        assert field.selected_text == "Factor"
        await pilot.press("ctrl+shift+c")
        app.copy_to_clipboard.assert_called_once_with("Factor")


async def test_selecting_output_keeps_inline_run_draft(tmp_path):
    app = FactorTerminal(backend=DemoBackend(tmp_path))
    app.copy_to_clipboard = Mock()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("ctrl+n")
        editor = app._run_editor
        formula = editor.query_one("#formula")
        formula.load_text("draft expression")
        await pilot.press("f2", "ctrl+a", "ctrl+c", "enter")
        assert app._run_editor is editor and not app.backend.jobs()
        await pilot.press("escape")
        assert app._run_editor is editor and formula.text == "draft expression"
        assert not app.backend.jobs()
        assert len(app.screen_stack) == 1
        await pilot.press("escape")
        assert app._run_editor is None
