"""Review one task inline before its caller creates or submits a job."""

from datetime import date

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Collapsible, Input, Label, Select, Static, TextArea

from fe_cli.backend import F01_FORMULA
from fe_cli.date_range import DateRange
from fe_cli.inputs import MultilineInput
from fe_cli.resources import NODE_CPU_LIMITS, ResourceRequest
from fe_cli.theme import BG, BLUE, BORDER, MINT, MUTED, ROSE, TEXT


class ResourceFields(Vertical):
    """Keep node limits and paired CPU / memory choices together."""

    DEFAULT_CSS = """
    ResourceFields { height: auto; }
    ResourceFields Label { margin-top: 0; background: transparent; color: #a9c9ed; }
    ResourceFields SelectCurrent { background: #101113; color: #e4e6eb;
        border: tall #343840; }
    ResourceFields Select:focus > SelectCurrent {
        background: #17191d; background-tint: transparent; border: tall #b4dfc5; }
    ResourceFields SelectOverlay { background: #17191d; color: #e4e6eb;
        border: tall #343840; }
    ResourceFields SelectOverlay:focus { background-tint: transparent; }
    ResourceFields SelectOverlay > .option-list--option-highlighted,
    ResourceFields SelectOverlay:focus > .option-list--option-highlighted {
        background: #b4dfc5; color: #101113; }
    ResourceFields SelectOverlay > .option-list--option-hover {
        background: #343840; color: #e4e6eb; }
    ResourceFields .resource-summary { height: auto; min-height: 2; color: #9299a5; }
    """

    def __init__(self, resources: ResourceRequest, prefix: str) -> None:
        super().__init__()
        self.resources = resources
        self.prefix = prefix

    @staticmethod
    def cpu_options(node: str) -> list[tuple[str, int]]:
        return [
            (f"{cpus} CPU / {cpus * 3} GB", cpus)
            for cpus in range(1, NODE_CPU_LIMITS[node] + 1)
        ]

    def compose(self) -> ComposeResult:
        yield Label("Node")
        yield Select(
            [("Node A · quant-a", "quant-a"), ("Node C · quant-c", "quant-c")],
            value=self.resources.node,
            allow_blank=False,
            id=f"{self.prefix}-node",
        )
        yield Label("Resources (1 CPU / 3 GB per level)")
        yield Select(
            self.cpu_options(self.resources.node),
            value=self.resources.cpus,
            allow_blank=False,
            id=f"{self.prefix}-cpus",
        )
        yield Static(
            self.summary(), id=f"{self.prefix}-resources-summary", classes="resource-summary"
        )

    def summary(self) -> str:
        return (
            f"{self.resources.node} · {self.resources.cpus} CPU · "
            f"{self.resources.memory_gb} GB\nMemory follows the selected CPU level."
        )

    @on(Select.Changed)
    def update_resources(self, event: Select.Changed) -> None:
        event.stop()
        if event.value is Select.BLANK or event.value != event.select.value:
            return
        adjusted = ""
        if event.select.id == f"{self.prefix}-node":
            node = str(event.value)
            if node == self.resources.node:
                return
            previous_cpus = self.resources.cpus
            cpus = min(previous_cpus, NODE_CPU_LIMITS[node])
            self.resources = ResourceRequest(node=node, cpus=cpus)
            cpu_select = self.query_one(f"#{self.prefix}-cpus", Select)
            cpu_select.set_options(self.cpu_options(node))
            cpu_select.value = cpus
            if cpus != previous_cpus:
                adjusted = f"\nAdjusted from {previous_cpus} to {cpus} CPU for this node."
        elif event.select.id == f"{self.prefix}-cpus":
            cpus = int(event.value)
            if cpus == self.resources.cpus:
                return
            self.resources = ResourceRequest(node=self.resources.node, cpus=cpus)
        summary = self.query_one(f"#{self.prefix}-resources-summary", Static)
        summary.update(self.summary() + adjusted)


class RunEditor(Vertical):
    """Collect task parameters inline; the caller owns all execution."""

    class Closed(Message):
        def __init__(self, editor: "RunEditor", values: dict[str, str | int] | None) -> None:
            super().__init__()
            self.editor = editor
            self.values = values

    BINDINGS = [
        Binding("escape", "dismiss_form", "Cancel", show=False, priority=True),
        Binding("enter", "confirm", "Confirm", show=False, priority=True),
    ]
    DEFAULT_CSS = f"""
    RunEditor {{ height: 1fr; background: transparent; color: {TEXT}; }}
    RunEditor #run-title {{ height: 1; text-style: bold; color: {MINT}; }}
    RunEditor #run-fields {{ height: 1fr; }}
    RunEditor #run-identity {{ height: 3; }}
    RunEditor .run-field {{ width: 1fr; height: 3; }}
    RunEditor .run-date {{ margin-left: 2; }}
    RunEditor Label {{ height: 1; margin: 0; background: transparent; color: {BLUE}; }}
    RunEditor Input, RunEditor TextArea {{ margin: 0; padding: 0;
        background: {BG}; color: {TEXT}; border: none; border-bottom: solid {BORDER}; }}
    RunEditor Input {{ height: 2; }}
    RunEditor Input:focus, RunEditor TextArea:focus {{
        background-tint: transparent; border: none; border-bottom: solid {MINT}; }}
    RunEditor #formula {{ height: 3; }}
    RunEditor TextArea .text-area--cursor-line {{ background: {BG}; color: {TEXT}; }}
    RunEditor TextArea .text-area--cursor {{ background: {MINT}; color: {BG}; }}
    RunEditor TextArea .text-area--selection {{ background: {BORDER}; color: {TEXT}; }}
    RunEditor Collapsible {{ padding: 0; margin: 0; border: none;
        background: transparent; }}
    RunEditor Collapsible > Contents {{ padding: 0; }}
    RunEditor .muted {{ height: auto; color: {MUTED}; }}
    RunEditor #run-error {{ height: 1; color: {MUTED}; }}
    RunEditor #run-error.has-error {{ color: {ROSE}; }}
    RunEditor #run-shortcuts {{ height: 1; color: {MUTED}; }}
    """

    def __init__(
        self,
        resources: ResourceRequest | None = None,
        *,
        factor: str = "f01",
        formula: str = F01_FORMULA,
        online: bool = False,
        source_label: str | None = None,
        date_range: DateRange | None = None,
    ) -> None:
        super().__init__()
        self.initial_resources = resources or ResourceRequest()
        self.initial_factor = factor
        self.initial_formula = formula
        self.online = online
        self.source_label = source_label
        self.date_range = date_range or DateRange()
        self._form_closed = False

    def compose(self) -> ComposeResult:
        yield Static("Run factor" if self.online else "New demo job", id="run-title")
        with VerticalScroll(id="run-fields"):
            with Horizontal(id="run-identity"):
                with Vertical(classes="run-field"):
                    yield Label("Factor ID" if self.online else "Factor name")
                    yield Input(self.initial_factor, id="factor-name", max_length=80)
                with Vertical(classes="run-field run-date"):
                    yield Label("Start date")
                    yield Input(self.date_range.start, id="start-date", max_length=10)
                with Vertical(classes="run-field run-date"):
                    yield Label("End date")
                    yield Input(self.date_range.end, id="end-date", max_length=10)
            yield Label("Expression (one-run override)" if self.online else "Formula")
            yield MultilineInput(self.initial_formula, id="formula", soft_wrap=True)
            with Collapsible(title="Details", collapsed=True):
                yield Static(
                    "Submit one cluster job; collect its result to save values locally."
                    if self.online
                    else "Local demo · No cluster submission or Factor Engine execution",
                    classes="muted",
                    markup=False,
                )
                if self.source_label:
                    yield Static(self.source_label, classes="muted", markup=False)
                yield Static(
                    "CSV stays unchanged."
                    if self.online
                    else "Editable example; FE semantics are not validated.",
                    classes="muted",
                )
                yield ResourceFields(self.initial_resources, "run")
                yield Static(
                    "Historical PIT remains unverified."
                    if self.online
                    else "Synthetic report; historical PIT is unverified.",
                    classes="muted",
                )
        yield Static(
            "Collect results separately to save values." if self.online else "Local demo only.",
            id="run-error",
            markup=False,
        )
        yield Static(
            "Enter submit job · Ctrl+J newline · Esc cancel"
            if self.online
            else "Enter create demo · Ctrl+J newline · Esc cancel",
            id="run-shortcuts",
        )

    def on_mount(self) -> None:
        self.query_one("#factor-name", Input).focus()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "confirm":
            return not getattr(self.app, "_copy_mode", False) and isinstance(
                self.app.focused, (Input, TextArea)
            )
        return True

    def action_dismiss_form(self) -> None:
        self._close(None)

    def _close(self, values: dict[str, str | int] | None) -> None:
        if not self._form_closed:
            self._form_closed = True
            self.post_message(self.Closed(self, values))

    def set_error(self, message: str) -> None:
        self._form_closed = False
        error = self.query_one("#run-error", Static)
        error.add_class("has-error")
        error.update(message)
        error.tooltip = message

    def action_confirm(self) -> None:
        if self._form_closed:
            return
        values = {
            "factor": self.query_one("#factor-name", Input).value.strip(),
            "formula": self.query_one("#formula", TextArea).text.strip(),
            "start": self.query_one("#start-date", Input).value.strip(),
            "end": self.query_one("#end-date", Input).value.strip(),
        }
        try:
            if not values["factor"] or not values["formula"]:
                raise ValueError("Enter a factor name and formula.")
            for field in ("start", "end"):
                if date.fromisoformat(values[field]).isoformat() != values[field]:
                    raise ValueError("Use YYYY-MM-DD dates.")
            if date.fromisoformat(values["start"]) > date.fromisoformat(values["end"]):
                raise ValueError("Start date must not be after end date.")
        except ValueError as exc:
            self.set_error(f"Check parameters: {exc}")
            return
        resources = self.query_one(ResourceFields).resources
        self._close({**values, "node": resources.node, "cpus": resources.cpus})
