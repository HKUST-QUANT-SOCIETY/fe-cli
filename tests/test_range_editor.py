from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, Input, Static

from fe_cli.date_range import DateRange
from fe_cli.range_editor import DateRangeEditor


class EditorApp(App[None]):
    CSS = """
    #range-editor-host { height: 7; }
    #command { height: 3; }
    """

    def __init__(self, editor: DateRangeEditor) -> None:
        super().__init__()
        self.editor = editor
        self.results: list[DateRange | None] = []

    def compose(self) -> ComposeResult:
        yield Static("Workspace")
        with Vertical(id="range-editor-host"):
            yield self.editor
        yield Input(id="command")

    def on_date_range_editor_closed(self, event: DateRangeEditor.Closed) -> None:
        assert event.editor is self.editor
        self.results.append(event.value)
        self.query_one("#range-editor-host").display = False
        self.query_one("#command", Input).focus()


async def test_open_and_cancel_preserve_initial_without_writes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    saved: list[DateRange] = []
    initial = DateRange("2026-09-01", "2026-09-30")
    editor = DateRangeEditor(initial, on_save=saved.append)
    app = EditorApp(editor)
    async with app.run_test() as pilot:
        assert editor.query_one("#range-start", Input).value == initial.start
        assert editor.query_one("#range-end", Input).value == initial.end
        assert editor.query_one("#range-start", Input).has_focus
        assert len(app.screen_stack) == 1
        assert saved == []
        editor.query_one("#range-start", Input).value = "2026-09-15"
        await pilot.press("escape")
        assert app.results == [None]
        assert app.query_one("#command", Input).has_focus
        assert saved == []
        assert initial == DateRange("2026-09-01", "2026-09-30")
        assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("field", ["#range-start", "#range-end"])
async def test_enter_saves_from_either_input_after_callback(field: str) -> None:
    saved: list[DateRange] = []

    def save(value: DateRange) -> None:
        assert app.results == []
        saved.append(value)

    editor = DateRangeEditor(DateRange(), on_save=save)
    app = EditorApp(editor)
    async with app.run_test() as pilot:
        editor.query_one("#range-start", Input).value = "2026-10-05"
        editor.query_one("#range-end", Input).value = "2026-10-25"
        editor.query_one(field, Input).focus()
        await pilot.press("enter")
        assert saved == [DateRange("2026-10-05", "2026-10-25")]
        assert app.results == saved


@pytest.mark.parametrize(
    "start, end",
    [("", "2026-10-31"), ("2026-02-30", "2026-10-31"), ("2026-10-31", "2026-10-01")],
)
async def test_invalid_date_range_preserves_draft(start: str, end: str) -> None:
    saved: list[DateRange] = []
    editor = DateRangeEditor(DateRange(), on_save=saved.append)
    app = EditorApp(editor)
    async with app.run_test() as pilot:
        editor.query_one("#range-start", Input).value = start
        editor.query_one("#range-end", Input).value = end
        await pilot.press("enter")
        assert saved == app.results == []
        assert editor.query_one("#range-start", Input).value == start
        assert editor.query_one("#range-end", Input).value == end
        error = editor.query_one("#range-error", Static)
        assert "Check date range:" in str(error.render())
        assert error.tooltip


@pytest.mark.parametrize("failure", [ValueError("invalid saved range"), OSError("read-only file")])
async def test_save_failure_keeps_draft_and_allows_retry(failure: Exception) -> None:
    attempts: list[DateRange] = []

    def save(value: DateRange) -> None:
        assert app.results == []
        attempts.append(value)
        if len(attempts) == 1:
            raise failure

    editor = DateRangeEditor(DateRange(), on_save=save)
    app = EditorApp(editor)
    async with app.run_test() as pilot:
        editor.query_one("#range-start", Input).value = "2026-10-05"
        await pilot.press("enter")
        assert app.results == []
        assert editor.query_one("#range-start", Input).value == "2026-10-05"
        error = editor.query_one("#range-error", Static)
        assert f"Could not save date range: {failure}" in str(error.render())
        assert error.tooltip == str(failure)
        await pilot.press("enter")
        assert len(attempts) == 2
        assert app.results == [attempts[-1]]


async def test_closed_editor_ignores_duplicate_save_and_cancel() -> None:
    saved: list[DateRange] = []
    editor = DateRangeEditor(DateRange(), on_save=saved.append)
    app = EditorApp(editor)
    async with app.run_test() as pilot:
        editor.action_save_range()
        editor.action_save_range()
        editor.action_dismiss_form()
        await pilot.pause()
        assert len(saved) == 1
        assert app.results == saved


async def test_enter_requires_input_focus_and_respects_copy_mode() -> None:
    saved: list[DateRange] = []
    editor = DateRangeEditor(DateRange(), on_save=saved.append)
    app = EditorApp(editor)
    async with app.run_test() as pilot:
        button = Button("Other action")
        await editor.mount(button)
        button.focus()
        await pilot.pause()
        assert editor.check_action("save_range", ()) is False
        await pilot.press("enter")
        assert saved == app.results == []
        editor.query_one("#range-start", Input).focus()
        app._copy_mode = True
        await pilot.press("enter")
        assert saved == app.results == []
        app._copy_mode = False
        await pilot.press("enter")
        assert len(saved) == 1
        assert app.results == saved


@pytest.mark.parametrize("size", [(80, 24), (60, 18), (40, 14)])
async def test_compact_editor_keeps_fields_error_and_shortcuts_visible(
    size: tuple[int, int],
) -> None:
    editor = DateRangeEditor(DateRange())
    app = EditorApp(editor)
    async with app.run_test(size=size) as pilot:
        editor.query_one("#range-start", Input).value = "invalid"
        await pilot.press("enter")
        assert editor.region.height <= 7
        for field in ("#range-start", "#range-end"):
            widget = editor.query_one(field, Input)
            assert widget.max_length == 10
            assert widget.content_region.height == 1
            assert editor.region.contains_region(widget.region)
        for field in ("#range-error", "#range-shortcuts"):
            widget = editor.query_one(field, Static)
            assert widget.region.height == 1
            assert editor.region.contains_region(widget.region)
            assert widget.region.bottom <= size[1]
        assert "Enter save · Esc cancel" in str(editor.query_one("#range-shortcuts").render())
        assert editor.region.bottom <= app.query_one("#command").region.y
