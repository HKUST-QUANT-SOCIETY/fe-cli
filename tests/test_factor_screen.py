from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Button, Checkbox, Collapsible, Input, Static, TextArea
from textual.widgets.text_area import Selection

from fe_cli.catalog import FactorDefinition
from fe_cli.factor_screen import FactorEditor


class EditorApp(App[None]):
    CSS = """
    #factor-editor-host { height: 12; }
    Screen.short #factor-editor-host { height: 8; }
    #command { height: 3; }
    """

    def __init__(self, form: FactorEditor) -> None:
        super().__init__()
        self.form = form
        self.results: list[FactorDefinition | None] = []

    def compose(self) -> ComposeResult:
        yield Static("Local factor catalog")
        with Vertical(id="factor-editor-host"):
            yield self.form
        yield Input(id="command")

    def on_resize(self) -> None:
        self.screen.set_class(self.size.height < 24, "short")

    def on_factor_editor_closed(self, event: FactorEditor.Closed) -> None:
        self.results.append(event.factor)
        self.query_one("#factor-editor-host").display = False
        self.query_one("#command", Input).focus()


async def test_create_preserves_multiline_dsl_without_execution_or_catalog_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    expression = (
        '  rank("close, adjusted")\n'
        "__import__('pathlib').Path('executed').write_text('must stay text')\n"
    )
    form = FactorEditor()
    app = EditorApp(form)
    async with app.run_test(size=(80, 24)) as pilot:
        assert form.query_one("#factor-id", Input).has_focus
        assert len(app.screen_stack) == 1
        assert form.query_one(Collapsible).collapsed
        form.query_one("#factor-id", Input).value = "local_alpha"
        form.query_one("#factor-expression", TextArea).load_text(expression)
        form.query_one("#factor-description", TextArea).load_text('A "quoted", note\n第二行')
        form.query_one("#factor-enabled", Checkbox).value = False
        await pilot.press("ctrl+s")
        assert app.results == [
            FactorDefinition(
                factor_id="local_alpha",
                name="local_alpha",
                expression=expression,
                enabled=False,
                description='A "quoted", note\n第二行',
            )
        ]
        assert list(tmp_path.iterdir()) == []


async def test_edit_prefills_fields_and_keeps_original_id() -> None:
    initial = FactorDefinition("alpha", "Original", "rank(close)", False, "Old description")
    form = FactorEditor(initial)
    app = EditorApp(form)
    async with app.run_test() as pilot:
        assert form.query_one("#factor-label", Input).has_focus
        assert form.query_one("#factor-id", Input).disabled
        assert form.query_one("#factor-expression", TextArea).text == initial.expression
        assert form.query_one("#factor-description", TextArea).text == initial.description
        assert not form.query_one("#factor-enabled", Checkbox).value
        form.query_one("#factor-id", Input).value = "changed_programmatically"
        form.query_one("#factor-label", Input).value = "Revised"
        form.query_one("#factor-expression", TextArea).load_text("rank(volume)\n+ 1")
        form.query_one("#factor-enabled", Checkbox).value = True
        await pilot.press("enter")
        assert app.results == [
            FactorDefinition("alpha", "Revised", "rank(volume)\n+ 1", True, "Old description")
        ]
        assert initial.name == "Original"


@pytest.mark.parametrize(
    "factor_id, expression",
    [("", "close"), ("非ASCII", "close"), ("alpha", " \n "), ("alpha", "x" * 16385)],
)
async def test_invalid_definition_stays_open_with_error(factor_id: str, expression: str) -> None:
    form = FactorEditor()
    app = EditorApp(form)
    async with app.run_test() as pilot:
        form.query_one("#factor-id", Input).value = factor_id
        form.query_one("#factor-expression", TextArea).load_text(expression)
        await pilot.press("ctrl+s")
        assert app.results == []
        assert "Check factor:" in str(form.query_one("#factor-error", Static).render())


async def test_edit_name_cannot_be_blank() -> None:
    form = FactorEditor(FactorDefinition("alpha", "Original", "close"))
    app = EditorApp(form)
    async with app.run_test() as pilot:
        form.query_one("#factor-label", Input).value = " "
        await pilot.press("ctrl+s")
        assert app.results == []
        assert "Check factor:" in str(form.query_one("#factor-error", Static).render())


@pytest.mark.parametrize("field", ["#factor-expression", "#factor-description"])
@pytest.mark.parametrize("shortcut", ["ctrl+j", "shift+enter"])
async def test_multiline_shortcut_adds_newline_without_submitting(
    field: str, shortcut: str
) -> None:
    form = FactorEditor()
    app = EditorApp(form)
    async with app.run_test() as pilot:
        form.query_one("#factor-id", Input).value = "alpha"
        form.query_one(Collapsible).collapsed = False
        await pilot.pause()
        area = form.query_one(field, TextArea)
        area.load_text("close")
        area.focus()
        await pilot.pause()
        await pilot.press("end", shortcut)
        assert area.text == "close\n"
        assert app.results == []


@pytest.mark.parametrize("reverse", [False, True])
async def test_multiline_shortcut_replaces_selection_and_supports_undo(reverse: bool) -> None:
    form = FactorEditor()
    app = EditorApp(form)
    async with app.run_test() as pilot:
        area = form.query_one("#factor-expression", TextArea)
        area.load_text("close + volume")
        area.focus()
        start, end = (0, 5), (0, 8)
        area.selection = Selection(end, start) if reverse else Selection(start, end)
        await pilot.press("ctrl+j")
        assert area.text == "close\nvolume"
        assert area.cursor_location == (1, 0)
        assert area.selection.is_empty
        assert app.results == []
        await pilot.press("ctrl+z")
        assert area.text == "close + volume"


async def test_multiline_shortcut_respects_read_only() -> None:
    form = FactorEditor()
    app = EditorApp(form)
    async with app.run_test() as pilot:
        area = form.query_one("#factor-expression", TextArea)
        area.load_text("close")
        area.read_only = True
        area.focus()
        await pilot.press("ctrl+j")
        assert area.text == "close"
        assert app.results == []


@pytest.mark.parametrize(
    "field, shortcut",
    [
        ("#factor-expression", "ctrl+s"),
        ("#factor-expression", "enter"),
        ("#factor-description", "enter"),
    ],
)
async def test_save_shortcuts_work_from_text_areas(field: str, shortcut: str) -> None:
    saved: list[FactorDefinition] = []
    form = FactorEditor(on_save=saved.append)
    app = EditorApp(form)
    async with app.run_test() as pilot:
        form.query_one("#factor-id", Input).value = "alpha"
        form.query_one("#factor-expression", TextArea).load_text("close")
        form.query_one(Collapsible).collapsed = False
        await pilot.pause()
        form.query_one(field, TextArea).focus()
        await pilot.pause()
        assert form.query_one(field, TextArea).has_focus
        await pilot.press(shortcut)
        assert len(saved) == 1
        assert app.results == saved
        assert app.query_one("#command", Input).has_focus


@pytest.mark.parametrize("failure", [ValueError("duplicate ID"), OSError("read-only catalog")])
async def test_persistence_error_keeps_draft_for_retry(failure: Exception) -> None:
    attempts: list[FactorDefinition] = []

    def save(factor: FactorDefinition) -> None:
        assert app.results == []
        attempts.append(factor)
        if len(attempts) == 1:
            raise failure

    form = FactorEditor(on_save=save)
    app = EditorApp(form)
    async with app.run_test() as pilot:
        form.query_one("#factor-id", Input).value = " alpha "
        form.query_one("#factor-label", Input).value = " Draft name "
        form.query_one("#factor-expression", TextArea).load_text(" close\n+ 1 ")
        form.query_one("#factor-description", TextArea).load_text("hidden details\n")
        form.query_one("#factor-enabled", Checkbox).value = False
        await pilot.press("enter")
        assert app.results == []
        assert form.query_one("#factor-id", Input).value == " alpha "
        assert form.query_one("#factor-label", Input).value == " Draft name "
        assert form.query_one("#factor-expression", TextArea).text == " close\n+ 1 "
        assert form.query_one("#factor-description", TextArea).text == "hidden details\n"
        assert not form.query_one("#factor-enabled", Checkbox).value
        assert form.query_one(Collapsible).collapsed
        error = form.query_one("#factor-error", Static)
        assert f"Could not save factor: {failure}" in str(error.render())
        assert error.tooltip == str(failure)
        await pilot.press("enter")
        assert len(attempts) == 2
        assert app.results == [attempts[-1]]


async def test_closed_editor_ignores_duplicate_save_and_cancel() -> None:
    saved: list[FactorDefinition] = []
    form = FactorEditor(FactorDefinition("alpha", "Original", "close"), on_save=saved.append)
    app = EditorApp(form)
    async with app.run_test() as pilot:
        form.action_save_factor()
        form.action_save_factor()
        form.action_dismiss_form()
        await pilot.pause()
        assert len(saved) == 1
        assert app.results == saved


async def test_enter_keeps_details_and_checkbox_interactive_without_saving() -> None:
    saved: list[FactorDefinition] = []
    form = FactorEditor(FactorDefinition("alpha", "Original", "close"), on_save=saved.append)
    app = EditorApp(form)
    async with app.run_test() as pilot:
        details = form.query_one(Collapsible)
        title = details.query_one("CollapsibleTitle")
        title.focus()
        await pilot.press("enter")
        assert not details.collapsed
        checkbox = form.query_one("#factor-enabled", Checkbox)
        checkbox.focus()
        await pilot.press("enter")
        assert not checkbox.value
        assert saved == []
        assert app.results == []


@pytest.mark.parametrize("shortcut", ["enter", "ctrl+s"])
async def test_save_shortcuts_do_not_save_in_copy_mode(shortcut: str) -> None:
    saved: list[FactorDefinition] = []
    form = FactorEditor(FactorDefinition("alpha", "Original", "close"), on_save=saved.append)
    app = EditorApp(form)
    async with app.run_test() as pilot:
        app._copy_mode = True
        await pilot.press(shortcut)
        assert saved == []
        assert app.results == []
        app._copy_mode = False
        await pilot.press(shortcut)
        assert app.results == saved
        assert len(saved) == 1


async def test_escape_discards_edits() -> None:
    form = FactorEditor(FactorDefinition("alpha", "Original", "close"))
    app = EditorApp(form)
    async with app.run_test(size=(80, 24)) as pilot:
        form.query_one("#factor-expression", TextArea).load_text("unsaved changes")
        form.query_one("#factor-expression", TextArea).focus()
        await pilot.press("escape")
        assert app.results == [None]


@pytest.mark.parametrize("size", [(80, 24), (60, 18)])
async def test_shortcuts_remain_visible_when_fields_scroll(size: tuple[int, int]) -> None:
    form = FactorEditor()
    app = EditorApp(form)
    async with app.run_test(size=size) as pilot:
        factor_id = form.query_one("#factor-id", Input)
        factor_id.value = "visible_alpha"
        await pilot.pause()
        assert factor_id.has_focus
        assert factor_id.content_region.height == 1
        expression = form.query_one("#factor-expression", TextArea)
        expression.focus()
        await pilot.pause()
        assert expression.content_region.height == 3
        fields = form.query_one("#factor-fields", VerticalScroll)
        if size[1] >= 24:
            details = form.query_one(Collapsible)
            assert details.region.bottom <= fields.region.bottom
        fields.scroll_end(animate=False)
        await pilot.pause()
        shortcuts = form.query_one("#factor-shortcuts", Static)
        assert shortcuts.region.height == 1
        assert form.region.contains_region(shortcuts.region)
        assert 0 <= shortcuts.region.x < shortcuts.region.right <= size[0]
        assert 0 <= shortcuts.region.y < shortcuts.region.bottom <= size[1]
        assert "Enter / Ctrl+S save · Ctrl+J newline · Esc cancel" in str(shortcuts.render())
        assert not form.query(Button)
        command = app.query_one("#command", Input)
        assert form.region.bottom <= command.region.y
        assert form.region.height == (12 if size[1] >= 24 else 8)
        await pilot.press("ctrl+s")
        assert app.results == []
        await pilot.press("escape")
        assert app.results == [None]
        assert command.has_focus
