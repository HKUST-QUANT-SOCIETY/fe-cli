"""Reusable choices embedded in the command area."""

from dataclasses import dataclass

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import OptionList, Static


@dataclass(frozen=True)
class Choice:
    label: str
    value: object
    preview: str = ""


class InlinePicker(Vertical):
    """Show configuration choices without changing the screen or input focus."""

    BINDINGS = [("escape", "cancel", "Back")]

    class Cancelled(Message):
        """Request cancellation when focus is inside the picker."""

    class Chosen(Message):
        def __init__(self, value: object) -> None:
            super().__init__()
            self.value = value

    def __init__(self, *, id: str = "config-picker") -> None:
        super().__init__(id=id)
        self._choices: tuple[Choice, ...] = ()
        self.display = False

    def compose(self) -> ComposeResult:
        yield Static("", id="config-title", markup=False)
        yield OptionList(id="config-options", wrap=False)
        yield Static("", id="config-preview", markup=False)
        yield Static("↑↓ Choose · Enter Apply · Esc Back", classes="picker-hint")

    def open(self, title: str, choices: list[Choice], selected: int = 0) -> None:
        if not choices:
            raise ValueError("A picker requires at least one choice.")
        if not 0 <= selected < len(choices):
            raise ValueError("Selected choice is out of range.")
        self._choices = tuple(choices)
        options = self.query_one("#config-options", OptionList)
        options.clear_options().add_options(Text(choice.label) for choice in self._choices)
        options.highlighted = selected
        self.query_one("#config-title", Static).update(title)
        self._update_preview()
        self.display = True
        self.call_after_refresh(self._scroll_to_highlight)

    def on_resize(self) -> None:
        self.call_after_refresh(self._scroll_to_highlight)

    def _scroll_to_highlight(self) -> None:
        if self.display and self._choices:
            self.query_one("#config-options", OptionList).scroll_to_highlight()

    def move(self, direction: int) -> None:
        if not self.display or not self._choices or direction == 0:
            return
        options = self.query_one("#config-options", OptionList)
        if direction < 0:
            options.action_cursor_up()
        else:
            options.action_cursor_down()
        self._update_preview()

    def choose(self) -> None:
        if not self.display or not self._choices:
            return
        index = self.query_one("#config-options", OptionList).highlighted
        if index is not None and 0 <= index < len(self._choices):
            self.post_message(self.Chosen(self._choices[index].value))

    def close(self) -> None:
        self.display = False
        self._choices = ()
        self.query_one("#config-options", OptionList).clear_options()
        self.query_one("#config-title", Static).update("")
        self.query_one("#config-preview", Static).update("")

    def action_cancel(self) -> None:
        if self.display:
            self.post_message(self.Cancelled())

    def _update_preview(self) -> None:
        index = self.query_one("#config-options", OptionList).highlighted
        if index is not None and 0 <= index < len(self._choices):
            self.query_one("#config-preview", Static).update(self._choices[index].preview)

    @on(OptionList.OptionHighlighted, "#config-options")
    def highlight_option(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        self._update_preview()

    @on(OptionList.OptionSelected, "#config-options")
    def select_option(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.choose()
