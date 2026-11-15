"""Inline editing for the default date range; the caller owns persistence."""

from collections.abc import Callable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Input, Label, Static

from fe_cli.date_range import DateRange
from fe_cli.theme import BG, BLUE, BORDER, MINT, MUTED, ROSE, TEXT


class DateRangeEditor(Vertical):
    """Keep the draft open until validation and synchronous saving succeed."""

    class Closed(Message):
        def __init__(self, editor: "DateRangeEditor", value: DateRange | None) -> None:
            super().__init__()
            self.editor = editor
            self.value = value

    BINDINGS = [
        Binding("escape", "dismiss_form", "Cancel", show=False, priority=True),
        Binding("enter", "save_range", "Save", show=False, priority=True),
    ]
    DEFAULT_CSS = f"""
    DateRangeEditor {{ height: 6; max-height: 7; background: transparent; color: {TEXT}; }}
    DateRangeEditor #range-title {{ height: 1; text-style: bold; color: {MINT}; }}
    DateRangeEditor #range-fields {{ height: 3; }}
    DateRangeEditor .range-field {{ width: 1fr; height: 3; }}
    DateRangeEditor #range-end-field {{ margin-left: 2; }}
    DateRangeEditor Label {{ height: 1; margin: 0; background: transparent; color: {BLUE}; }}
    DateRangeEditor Input {{ height: 2; margin: 0; padding: 0;
        background: {BG}; color: {TEXT}; border: none; border-bottom: solid {BORDER}; }}
    DateRangeEditor Input:focus {{ background-tint: transparent;
        border: none; border-bottom: solid {MINT}; }}
    DateRangeEditor #range-error {{ height: 1; color: {MUTED}; }}
    DateRangeEditor #range-error.has-error {{ color: {ROSE}; }}
    DateRangeEditor #range-shortcuts {{ height: 1; color: {MUTED}; }}
    """

    def __init__(
        self,
        initial: DateRange,
        *,
        on_save: Callable[[DateRange], None] | None = None,
    ) -> None:
        super().__init__()
        self.initial = initial
        self.on_save = on_save
        self._form_closed = False

    def compose(self) -> ComposeResult:
        yield Static("Default date range", id="range-title")
        with Horizontal(id="range-fields"):
            with Vertical(classes="range-field"):
                yield Label("Start date")
                yield Input(self.initial.start, id="range-start", max_length=10)
            with Vertical(classes="range-field", id="range-end-field"):
                yield Label("End date")
                yield Input(self.initial.end, id="range-end", max_length=10)
        yield Static("", id="range-error", markup=False)
        yield Static("Enter save · Esc cancel", id="range-shortcuts")

    def on_mount(self) -> None:
        self.query_one("#range-start", Input).focus()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "save_range":
            return not getattr(self.app, "_copy_mode", False) and isinstance(
                self.app.focused, Input
            )
        return super().check_action(action, parameters)

    def _close(self, value: DateRange | None) -> None:
        if not self._form_closed:
            self._form_closed = True
            self.post_message(self.Closed(self, value))

    def action_dismiss_form(self) -> None:
        self._close(None)

    def action_save_range(self) -> None:
        if self._form_closed:
            return
        error_prefix = "Check date range"
        try:
            value = DateRange(
                start=self.query_one("#range-start", Input).value.strip(),
                end=self.query_one("#range-end", Input).value.strip(),
            )
            error_prefix = "Could not save date range"
            if self.on_save is not None:
                self.on_save(value)
        except (ValueError, OSError) as exc:
            error = self.query_one("#range-error", Static)
            error.add_class("has-error")
            error.update(f"{error_prefix}: {exc}")
            error.tooltip = str(exc)
            return
        self._close(value)
