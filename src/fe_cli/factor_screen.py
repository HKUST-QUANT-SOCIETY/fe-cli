"""Edit a local definition without executing DSL; the caller owns persistence."""

from collections.abc import Callable

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Checkbox, Collapsible, Input, Label, Static, TextArea

from fe_cli.catalog import FactorDefinition
from fe_cli.inputs import MultilineInput
from fe_cli.theme import BG, BLUE, BORDER, MINT, MUTED, ROSE, TEXT


class FactorEditor(Vertical):
    """Edit inline and notify the caller after a validated definition is saved."""

    class Closed(Message):
        def __init__(self, factor: FactorDefinition | None) -> None:
            super().__init__()
            self.factor = factor

    BINDINGS = [
        Binding("escape", "dismiss_form", "Cancel", show=False, priority=True),
        Binding("ctrl+s", "save_factor", "Save", show=False, priority=True),
        Binding("enter", "save_factor", "Save", show=False, priority=True),
    ]
    DEFAULT_CSS = f"""
    FactorEditor {{ height: 1fr; background: transparent; color: {TEXT}; }}
    FactorEditor #factor-title {{ height: 1; text-style: bold; color: {MINT}; }}
    FactorEditor #factor-fields {{ height: 1fr; }}
    FactorEditor #factor-identity {{ height: 3; }}
    FactorEditor .factor-field {{ width: 1fr; height: 3; }}
    FactorEditor #factor-name-field {{ margin-left: 2; }}
    FactorEditor Label {{ height: 1; background: transparent; color: {BLUE}; }}
    FactorEditor Input, FactorEditor TextArea {{ margin: 0; padding: 0;
        background: {BG}; color: {TEXT}; border: none; border-bottom: solid {BORDER}; }}
    FactorEditor Input {{ height: 2; }}
    FactorEditor Input:focus, FactorEditor TextArea:focus {{
        background-tint: transparent; border: none; border-bottom: solid {MINT}; }}
    FactorEditor TextArea .text-area--cursor-line {{ background: {BG}; color: {TEXT}; }}
    FactorEditor TextArea .text-area--cursor {{ background: {MINT}; color: {BG}; }}
    FactorEditor TextArea .text-area--selection {{ background: {BORDER}; color: {TEXT}; }}
    FactorEditor #factor-id:disabled {{ color: {MUTED}; opacity: 100%; }}
    FactorEditor #factor-expression {{ height: 4; }}
    FactorEditor #factor-description {{ height: 3; }}
    FactorEditor Collapsible {{ padding: 0; margin: 0; border: none;
        background: transparent; }}
    FactorEditor Collapsible > Contents {{ padding: 0; }}
    FactorEditor #factor-enabled {{ height: 3; background: transparent; color: {TEXT};
        border: none; padding: 0; }}
    FactorEditor #factor-error {{ color: {MUTED}; height: 1; }}
    FactorEditor #factor-error.has-error {{ color: {ROSE}; }}
    FactorEditor #factor-shortcuts {{ height: 1; color: {MUTED}; }}
    """

    def __init__(
        self,
        initial: FactorDefinition | None = None,
        *,
        on_save: Callable[[FactorDefinition], None] | None = None,
    ) -> None:
        super().__init__()
        self.initial = initial
        self.on_save = on_save
        self._form_closed = False

    def compose(self) -> ComposeResult:
        initial = self.initial
        yield Static("Edit factor" if initial else "New factor", id="factor-title")
        with VerticalScroll(id="factor-fields"):
            with Horizontal(id="factor-identity"):
                with Vertical(classes="factor-field"):
                    yield Label("Factor ID (fixed)")
                    yield Input(
                        initial.factor_id if initial else "",
                        id="factor-id",
                        max_length=80,
                        disabled=initial is not None,
                    )
                with Vertical(classes="factor-field", id="factor-name-field"):
                    yield Label("Name (defaults to ID)")
                    yield Input(initial.name if initial else "", id="factor-label", max_length=160)
            yield Label("Expression")
            yield MultilineInput(initial.expression if initial else "", id="factor-expression")
            with Collapsible(title="Details", collapsed=True):
                yield Label("Description (optional)")
                yield MultilineInput(
                    initial.description if initial else "", id="factor-description"
                )
                yield Checkbox(
                    "Enabled", value=initial.enabled if initial else True, id="factor-enabled"
                )
        yield Static("Saved without engine validation.", id="factor-error", markup=False)
        yield Static(
            Text.assemble(
                ("Enter", MINT),
                (" / ", MUTED),
                ("Ctrl+S", MINT),
                (" save · ", MUTED),
                ("Ctrl+J", MINT),
                (" newline · ", MUTED),
                ("Esc", BLUE),
                (" cancel", MUTED),
            ),
            id="factor-shortcuts",
        )

    def on_mount(self) -> None:
        self.query_one("#factor-label" if self.initial else "#factor-id", Input).focus()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "save_factor":
            return not getattr(self.app, "_copy_mode", False) and isinstance(
                self.app.focused, (Input, TextArea)
            )
        return super().check_action(action, parameters)

    def action_dismiss_form(self) -> None:
        if not self._form_closed:
            self._form_closed = True
            self.post_message(self.Closed(None))

    def action_save_factor(self) -> None:
        if self._form_closed:
            return
        factor_id = (
            self.initial.factor_id
            if self.initial
            else self.query_one("#factor-id", Input).value.strip()
        )
        name = self.query_one("#factor-label", Input).value.strip()
        if not self.initial and not name:
            name = factor_id
        error_prefix = "Check factor"
        try:
            factor = FactorDefinition(
                factor_id=factor_id,
                name=name,
                expression=self.query_one("#factor-expression", TextArea).text,
                enabled=self.query_one("#factor-enabled", Checkbox).value,
                description=self.query_one("#factor-description", TextArea).text,
            )
            error_prefix = "Could not save factor"
            if self.on_save is not None:
                self.on_save(factor)
        except (ValueError, OSError) as exc:
            error = self.query_one("#factor-error", Static)
            error.add_class("has-error")
            error.update(f"{error_prefix}: {exc}")
            error.tooltip = str(exc)
            return
        self._form_closed = True
        self.post_message(self.Closed(factor))
