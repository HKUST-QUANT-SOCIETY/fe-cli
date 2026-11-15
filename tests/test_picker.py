import pytest
from textual import on
from textual.app import App, ComposeResult
from textual.widgets import Input, OptionList, Static

from fe_cli.picker import Choice, InlinePicker


class PickerApp(App[None]):
    CSS = """
    InlinePicker { height: auto; }
    #config-title, #config-preview, .picker-hint { height: 1; }
    #config-options { height: auto; max-height: 4; border: none; padding: 0; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.chosen: list[object] = []
        self.bubbled_selections = 0
        self.cancelled = 0

    def compose(self) -> ComposeResult:
        yield InlinePicker()
        yield Input(id="command")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(InlinePicker.Chosen)
    def collect_choice(self, event: InlinePicker.Chosen) -> None:
        self.chosen.append(event.value)

    @on(OptionList.OptionSelected)
    def collect_option_selection(self) -> None:
        self.bubbled_selections += 1

    @on(InlinePicker.Cancelled)
    def cancel_picker(self) -> None:
        self.cancelled += 1
        self.query_one(InlinePicker).close()
        self.query_one(Input).focus()


async def test_open_preserves_input_focus_and_navigation_updates_preview() -> None:
    app = PickerApp()
    async with app.run_test() as pilot:
        picker = app.query_one(InlinePicker)
        assert not picker.display
        picker.open("Environment", [Choice("One", 1, "First"), Choice("Two", 2, "Second")])
        await pilot.pause()
        assert picker.display
        assert app.query_one(Input).has_focus
        assert str(app.query_one("#config-title", Static).renderable) == "Environment"
        picker.move(1)
        assert str(app.query_one("#config-preview", Static).renderable) == "Second"
        picker.move(-1)
        assert str(app.query_one("#config-preview", Static).renderable) == "First"
        assert app.query_one(Input).has_focus
        picker.choose()
        await pilot.pause()
        assert app.chosen == [1]


async def test_cancel_clears_and_reopen_selects_latest_values() -> None:
    app = PickerApp()
    async with app.run_test() as pilot:
        picker = app.query_one(InlinePicker)
        picker.open("Old", [Choice("Old", "old", "Old preview")])
        picker.close()
        picker.move(1)
        picker.choose()
        await pilot.pause()
        assert not picker.display
        assert app.query_one(OptionList).option_count == 0
        assert str(app.query_one("#config-preview", Static).renderable) == ""
        assert not app.chosen
        value = object()
        picker.open("New", [Choice("First", None), Choice("Latest", value, "Latest")], 1)
        await pilot.pause()
        picker.choose()
        await pilot.pause()
        assert app.chosen == [value]
        assert str(app.query_one("#config-preview", Static).renderable) == "Latest"


@pytest.mark.parametrize(
    "choices, selected", [([], 0), ([Choice("One", 1)], -1), ([Choice("One", 1)], 1)]
)
async def test_invalid_choices_leave_picker_closed(choices: list[Choice], selected: int) -> None:
    app = PickerApp()
    async with app.run_test():
        picker = app.query_one(InlinePicker)
        with pytest.raises(ValueError):
            picker.open("Invalid", choices, selected)
        assert not picker.display


async def test_mouse_and_list_keyboard_emit_one_chosen_message_each() -> None:
    app = PickerApp()
    async with app.run_test() as pilot:
        picker = app.query_one(InlinePicker)
        picker.open("Choice", [Choice("One", 1), Choice("Two", 2)])
        await pilot.pause()
        await pilot.click("#config-options", offset=(1, 1))
        await pilot.pause()
        assert app.chosen == [2]
        assert app.bubbled_selections == 0
        await pilot.press("up", "enter")
        assert app.chosen == [2, 1]
        assert app.bubbled_selections == 0


async def test_long_list_scrolls_to_latest_highlight_in_small_viewport() -> None:
    app = PickerApp()
    async with app.run_test(size=(40, 12)) as pilot:
        picker = app.query_one(InlinePicker)
        choices = [Choice(f"Choice {index}", index, f"Preview {index}") for index in range(30)]
        picker.open("Choice", choices, 29)
        await pilot.pause()
        options = app.query_one(OptionList)
        assert options.highlighted == 29
        assert options.scroll_y > 0
        assert app.query_one(Input).has_focus
        picker.move(-1)
        picker.choose()
        await pilot.pause()
        assert app.chosen == [28]
        assert str(app.query_one("#config-preview", Static).renderable) == "Preview 28"


async def test_escape_from_focused_options_requests_cancel() -> None:
    app = PickerApp()
    async with app.run_test() as pilot:
        picker = app.query_one(InlinePicker)
        picker.open("Choice", [Choice("One", 1)])
        app.query_one(OptionList).focus()
        await pilot.pause()
        assert app.query_one(OptionList).has_focus
        await pilot.press("escape")
        assert app.cancelled == 1
        assert not app.chosen
        assert not picker.display
        assert app.query_one(Input).has_focus


async def test_resize_keeps_highlighted_option_visible_without_taking_focus() -> None:
    app = PickerApp()
    async with app.run_test(size=(110, 30)) as pilot:
        picker = app.query_one(InlinePicker)
        options = app.query_one(OptionList)
        options.styles.max_height = "20vh"
        picker.open("Choice", [Choice(f"Choice {index}", index) for index in range(8)], 5)
        await pilot.pause()
        assert options.scrollable_content_region.height == 6
        await pilot.resize_terminal(60, 18)
        await pilot.pause()
        assert options.scrollable_content_region.height < 6
        assert options.highlighted == 5
        visible_rows = [
            options.render_line(row).text for row in range(options.scrollable_content_region.height)
        ]
        assert any("Choice 5" in row for row in visible_rows)
        assert app.query_one(Input).has_focus
