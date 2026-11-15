"""Command-first terminal for demo jobs and explicit online single-factor runs."""

from __future__ import annotations

import shlex
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from rich.console import Console, RenderableType
from rich.json import JSON
from rich.table import Table
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.screen import Screen
from textual.strip import Strip
from textual.theme import Theme
from textual.widgets import Footer, Input, OptionList, RichLog, Static, TextArea
from textual.widgets.option_list import Option

from fe_cli.backend import DemoBackend
from fe_cli.date_range import DateRange, DateRangeStore
from fe_cli.factor_screen import FactorEditor
from fe_cli.history import recent_history
from fe_cli.library_ui import LibraryController
from fe_cli.picker import Choice, InlinePicker
from fe_cli.range_editor import DateRangeEditor
from fe_cli.resources import NODE_CPU_LIMITS, ResourceRequest, ResourceStore
from fe_cli.run_ui import OnlineRuns
from fe_cli.screens import RunEditor
from fe_cli.theme import BG, BLUE, MINT, MUTED, PEACH, ROSE, SURFACE, TEXT

if TYPE_CHECKING:
    from fe_cli.catalog import FactorDefinition
    from fe_cli.remote import RemoteClient
    from fe_cli.workspace import Workspace

COMMANDS = {
    "/run": "Create a demo job",
    "/jobs": "Show current job status",
    "/node": "Choose node: /node [A|C]",
    "/resources": "Choose CPU and memory: /resources [CPUs]",
    "/range": "Set default dates: /range [START END]",
    "/results": "View report: /results [job-id]",
    "/runs": "View local single-factor runs and saved results",
    "/cancel": "Cancel a job: /cancel job-id",
    "/factor": "Browse or manage your local factor library",
    "/init": "Initialize a local factor workspace",
    "/data": "View example fields and data notes",
    "/setup": "View mode and resource defaults",
    "/help": "Command reference",
    "/quit": "Exit terminal",
}
ONLINE_COMMANDS = {
    "/run": "Submit one saved factor",
    "/results": "Collect or show one real result",
    "/collect": "Download and validate a completed job result",
    "/runs": "View local single-factor runs",
    "/setup": "Diagnose SSH connection and authentication",
    "/envs": "List registered server environments",
    "/jobs": "Query current server jobs",
    "/history": "Query server job history: latest 10, or /history full",
    "/node": "Choose the node for the next task",
    "/resources": "Choose CPU and memory for the next task",
    "/range": "Set the default date range for future tasks",
    "/factor": "Browse or manage your local factor library",
    "/init": "Initialize a local factor workspace",
    "/help": "Command reference",
    "/quit": "Exit terminal",
}
REMOTE_QUERIES = {
    "/envs": "cluster-envs",
    "/jobs": "cluster-status",
    "/history": "cluster-history",
}
COMMAND_ARGUMENTS = {
    "/collect": "JOB_ID [REQUEST_ID]",
    "/factor": "[list | new | show ID | edit ID | delete ID | use ID]",
    "/init": "[directory]",
    "/node": "[A | C]",
    "/resources": "[CPUs]",
    "/range": "[YYYY-MM-DD YYYY-MM-DD]",
    "/results": "[job-id] [--raw]",
    "/cancel": "job-id",
    "/history": "[full]",
}
STATUS_NAMES = {
    "PENDING": "Queued",
    "RUNNING": "Running",
    "COMPLETED": "Completed",
    "CANCELLED": "Cancelled",
    "FAILED": "Failed",
}


class CommandInput(Input):
    argument_hint = reactive("")

    @property
    def _value(self) -> Text:
        text = super()._value
        if self.has_focus and self.cursor_position == len(self.value):
            text.append(self.argument_hint, style=MUTED)
        return text

    BINDINGS = [
        Binding("up", "previous", show=False),
        Binding("down", "next", show=False),
        Binding("tab", "complete", show=False),
        Binding("escape", "close", show=False),
    ]

    class MenuAction(Message):
        def __init__(self, action: str) -> None:
            super().__init__()
            self.action = action

    def action_previous(self) -> None:
        self.post_message(self.MenuAction("up"))

    def action_next(self) -> None:
        self.post_message(self.MenuAction("down"))

    def action_complete(self) -> None:
        self.post_message(self.MenuAction("complete"))

    def action_close(self) -> None:
        self.post_message(self.MenuAction("close"))


class TranscriptLog(RichLog):
    def render_line(self, y: int) -> Strip:
        gap = max(0, self.scrollable_content_region.height - len(self.lines))
        if y < gap:
            return Strip.blank(self.scrollable_content_region.width, self.rich_style)
        return super().render_line(y - gap)

    def on_resize(self) -> None:
        self.call_after_refresh(self.scroll_end, animate=False, x_axis=False)


class TranscriptSelection(TextArea):
    BINDINGS = [
        Binding("ctrl+a", "select_all", show=False),
        Binding("ctrl+c", "request_copy", show=False, priority=True),
    ]

    class CopyRequested(Message):
        pass

    def action_request_copy(self) -> None:
        self.post_message(self.CopyRequested())


class FactorTerminal(App):
    TITLE = "Factor Engine · Research Terminal"
    CSS_PATH = "terminal.tcss"
    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", priority=True),
        Binding("ctrl+n", "new_run", "New job"),
        Binding("ctrl+l", "focus_command", "Command"),
        Binding("enter", "confirm_editor", show=False, priority=True),
        Binding("ctrl+s", "save_factor", show=False, priority=True),
        Binding("f2", "toggle_selection", "Select text", priority=True),
        Binding("f8", "copy_text", show=False, priority=True),
        Binding("ctrl+shift+c", "copy_text", show=False, priority=True),
        Binding("escape", "close_selection", show=False, priority=True),
    ]

    def __init__(
        self,
        backend: DemoBackend | None = None,
        *,
        online: RemoteClient | None = None,
        resource_store: ResourceStore | None = None,
        date_range_store: DateRangeStore | None = None,
        workspace: Workspace | None = None,
    ) -> None:
        if backend is not None and online is not None:
            raise ValueError("Choose either demo or online mode.")
        super().__init__()
        self.register_theme(
            Theme(
                name="fe-night",
                primary=MINT,
                secondary=BLUE,
                accent=BLUE,
                foreground=TEXT,
                background=BG,
                surface=BG,
                panel=SURFACE,
                warning=PEACH,
                error=ROSE,
                success=MINT,
                boost="#ffffff",
                variables={"footer-key-foreground": MUTED},
            )
        )
        self.theme = "fe-night"
        self.online = online
        self.resource_store = resource_store
        self.resources = resource_store.load() if resource_store is not None else ResourceRequest()
        self.date_range_store = date_range_store
        self.date_range = date_range_store.load() if date_range_store is not None else DateRange()
        self.backend = backend if backend is not None else DemoBackend() if online is None else None
        self.commands = ONLINE_COMMANDS if online is not None else COMMANDS
        self._query_pending = False
        self._connection_status = "Not checked"
        self._config_callback: Callable[[object], None] | None = None
        self._factor_editor: FactorEditor | None = None
        self._factor_callback: Callable[[FactorDefinition | None], None] | None = None
        self._run_editor: RunEditor | None = None
        self._run_callback: Callable[[dict | None], None] | None = None
        self._range_editor: DateRangeEditor | None = None
        self._copy_mode = False
        self._transcript_text: list[str] = []
        self.library = LibraryController(self, workspace)
        self.run_actions = OnlineRuns(self)

    def mode_text(self) -> str:
        if self.online is None:
            return "LOCAL DEMO · Offline"
        return f"ONLINE · {self._connection_status}"

    def resource_text(self) -> Text:
        text = Text("Next task  ", style=MUTED)
        text.append(self.resource_summary(), style=MINT)
        return text

    def resource_summary(self) -> str:
        request = self.resources
        label = request.node.removeprefix("quant-").upper()
        return f"Node {label} / {request.cpus} CPU / {request.memory_gb} GB"

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "new_run" and self.editing:
            return False
        if action == "save_factor":
            return self._factor_editor is not None and not self._copy_mode
        if action == "confirm_editor":
            if self._copy_mode or not self.editing:
                return False
            if isinstance(self.focused, CommandInput):
                return not self.focused.value.strip()
            return isinstance(self.focused, (Input, TextArea))
        if action == "close_selection":
            return self._copy_mode
        if action == "quit" and self._copy_mode:
            return False
        if action == "copy_text":
            return self._copy_mode or isinstance(self.focused, TextArea)
        return super().check_action(action, parameters)

    @property
    def editing(self) -> bool:
        return any(
            editor is not None
            for editor in (self._factor_editor, self._run_editor, self._range_editor)
        )

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static("FE / research", id="brand")
            yield Static(self.mode_text(), id="mode", markup=False)
        with Vertical(id="workspace"):
            yield TranscriptLog(id="transcript", wrap=True, markup=False, highlight=False)
            yield TranscriptSelection(
                id="transcript-selection", read_only=True, show_line_numbers=False, soft_wrap=True
            )
            yield Static("", id="copy-hint", markup=False)
        with Vertical(id="composer"):
            yield OptionList(id="commands", wrap=False)
            yield InlinePicker(id="config-picker")
            yield Vertical(id="factor-editor-host")
            yield Vertical(id="run-editor-host")
            yield Vertical(id="range-editor-host")
            yield CommandInput(placeholder="Type / for commands", id="command")
            yield Static(self.resource_text(), id="resource-status")
        yield Footer()

    def on_mount(self) -> None:
        self.home.query_one("#commands").display = False
        self.write("Factor Engine", MINT)
        if self.online is not None:
            self.write("Select a saved /factor, then /run to submit one job.")
            self.write(
                "Use /results JOB_ID after completion to validate and save its values locally.",
                MUTED,
            )
        else:
            self.write("Use /run to create a demo job. Type / to explore commands.")
            self.write(
                "Demo jobs produce synthetic reports. No engine or market data is used.", "#999999"
            )
        self.action_focus_command()

    def on_resize(self, event: events.Resize) -> None:
        self.set_class(event.size.width < 80, "compact")
        self.set_class(event.size.height < 26, "short")
        if self._factor_editor is not None:
            self.size_factor_editor()
        if self._run_editor is not None:
            self.size_run_editor()

    @property
    def home(self) -> Screen:
        """Keep command output on the main workspace."""
        return self.screen_stack[0]

    def write(self, message: str, color: str | None = None) -> None:
        if message.startswith(("Error:", "Query failed:", "Could not")):
            color = ROSE
        self.append_output(Text(message, style=color or TEXT))

    def append_output(self, content: RenderableType) -> None:
        if isinstance(content, Text):
            self._transcript_text.append(content.plain + "\n")
        else:
            console = Console(
                width=max(80, self.size.width - 4), color_system=None, markup=False,
                highlight=False, soft_wrap=True,
            )
            with console.capture() as capture:
                console.print(content)
            self._transcript_text.append(capture.get())
        log = self.home.query_one("#transcript", RichLog)
        log.write(content)
        self.call_after_refresh(log.scroll_end, animate=False, x_axis=False)

    def action_toggle_selection(self) -> None:
        if self._copy_mode:
            self.action_close_selection()
            return
        self._copy_mode = True
        self.home.query_one("#transcript").display = False
        field = self.home.query_one("#transcript-selection", TranscriptSelection)
        field.load_text("".join(self._transcript_text))
        field.display = True
        field.move_cursor(field.document.end)
        hint = self.home.query_one("#copy-hint", Static)
        hint.display = True
        hint.update("Select text · Ctrl+C / F8 copy · Ctrl+A all · Esc return")
        field.focus()
        self.refresh_bindings()

    def action_close_selection(self) -> None:
        self._copy_mode = False
        self.home.query_one("#transcript-selection").display = False
        self.home.query_one("#copy-hint").display = False
        self.home.query_one("#transcript").display = True
        self.call_after_refresh(
            self.home.query_one("#transcript", RichLog).scroll_end, animate=False, x_axis=False
        )
        self.action_focus_command()
        self.refresh_bindings()

    @on(TranscriptSelection.CopyRequested)
    def copy_requested(self) -> None:
        self.action_copy_text()

    def action_copy_text(self) -> None:
        field = self.focused
        if not isinstance(field, TextArea):
            field = self.home.query_one("#transcript-selection", TranscriptSelection)
        if field.selected_text:
            self.copy_to_clipboard(field.selected_text)
            message = "Copy request sent to terminal · Esc return"
        else:
            message = "Select text first · Ctrl+A all · Esc return"
        if self._copy_mode:
            self.home.query_one("#copy-hint", Static).update(message)

    def show_jobs(self) -> None:
        """Print a fresh status snapshot only when /jobs is requested."""
        if self.backend is None:
            raise ValueError("Demo jobs are unavailable in online mode.")
        jobs = self.backend.jobs()
        if not jobs:
            self.write("No jobs yet. Use /run to create a demo job.")
            return
        active = sum(j.status in {"PENDING", "RUNNING"} for j in jobs)
        self.write(f"Jobs · {len(jobs)} total · {active} active", "#eeeeee")
        table = Table(box=None, pad_edge=False, header_style="bold #b0b0b0", style="#d0d0d0")
        show_dates = self.home.query_one("#transcript", RichLog).content_size.width >= 100
        headings = ["Job", "Factor", "Status", "Node", "Request"]
        if show_dates:
            headings.append("Date range")
        for heading in headings:
            table.add_column(heading)
        for job in jobs:
            cells = [
                job.id,
                Text(job.factor),
                Text(
                    STATUS_NAMES[job.status],
                    style={
                        "COMPLETED": MINT,
                        "RUNNING": BLUE,
                        "PENDING": PEACH,
                        "FAILED": ROSE,
                        "CANCELLED": MUTED,
                    }[job.status],
                ),
                job.node,
                f"{job.cpus} CPU / {job.memory}",
            ]
            if show_dates:
                cells.append(f"{job.start} → {job.end}")
            table.add_row(*cells)
        self.append_output(table)
        self.write("/results job-id for a report · /jobs to refresh", "#999999")

    @on(Input.Changed, "#command")
    def update_menu(self, event: Input.Changed) -> None:
        self.home.query_one("#command", CommandInput).argument_hint = ""
        menu = self.home.query_one("#commands", OptionList)
        if self.editing:
            menu.display = False
            return
        picker = self.home.query_one("#config-picker", InlinePicker)
        if picker.display:
            if not event.value.strip():
                menu.display = False
                return
            self.close_config_picker()
        value = event.value
        choices = (
            [cmd for cmd in self.commands if cmd.startswith(value)]
            if value.startswith("/") and not any(char.isspace() for char in value)
            else []
        )
        menu.clear_options()
        for cmd in choices:
            label = Text(f"{cmd:<12}", style=BLUE)
            label.append(self.commands[cmd], style=MUTED)
            menu.add_option(Option(label, id=cmd))
        menu.display = bool(choices)
        if choices:
            menu.highlighted = 0
        self.update_command_hint()

    @on(OptionList.OptionHighlighted, "#commands")
    def highlight_command(self, event: OptionList.OptionHighlighted) -> None:
        self.update_command_hint()

    def command_arguments(self, command: str) -> str:
        if self.online is not None and command in {"/run", "/results"}:
            return "[FACTOR_ID]" if command == "/run" else "[JOB_ID | REQUEST_ID]"
        return COMMAND_ARGUMENTS.get(command, "")

    def update_command_hint(self) -> None:
        field = self.home.query_one("#command", CommandInput)
        menu = self.home.query_one("#commands", OptionList)
        value = field.value
        field.argument_hint = ""
        if not value.startswith("/") or self.editing:
            return
        command, _, arguments = value.partition(" ")
        if " " not in value:
            selected = command
            if menu.display and menu.highlighted is not None:
                selected = menu.get_option_at_index(menu.highlighted).id or command
            if selected in self.commands and selected.startswith(value):
                hint = self.command_arguments(selected)
                field.argument_hint = selected[len(value) :] + (f" {hint}" if hint else "")
        elif command in self.commands:
            hint = self.command_arguments(command) if not arguments.strip() else ""
            if command == "/factor" and arguments.strip() in {"show", "edit", "delete", "use"}:
                hint = "ID"
            if hint:
                field.argument_hint = ("" if value.endswith(" ") else " ") + hint

    @on(CommandInput.MenuAction)
    def navigate_menu(self, event: CommandInput.MenuAction) -> None:
        if self._range_editor is not None and event.action == "close":
            self._range_editor.action_dismiss_form()
            return
        if self._run_editor is not None and event.action == "close":
            self._run_editor.action_dismiss_form()
            return
        if self._factor_editor is not None and event.action == "close":
            self._factor_editor.action_dismiss_form()
            return
        picker = self.home.query_one("#config-picker", InlinePicker)
        if picker.display:
            if event.action in {"up", "down"}:
                picker.move(-1 if event.action == "up" else 1)
            elif event.action == "close":
                self.close_config_picker()
            return
        menu = self.home.query_one("#commands", OptionList)
        if not menu.display:
            if event.action == "complete":
                self.screen.focus_next()
            return
        if event.action == "up":
            menu.action_cursor_up()
        elif event.action == "down":
            menu.action_cursor_down()
        elif event.action == "close":
            menu.display = False
            self.update_command_hint()
        elif event.action == "complete" and menu.highlighted is not None:
            self.complete(menu.get_option_at_index(menu.highlighted).id or "")

    def complete(self, command: str) -> None:
        field = self.home.query_one("#command", Input)
        field.value = command + " "
        field.cursor_position = len(field.value)
        field.focus()

    @on(OptionList.OptionSelected, "#commands")
    def select_command(self, event: OptionList.OptionSelected) -> None:
        self.complete(event.option_id or "")

    @on(Input.Submitted, "#command")
    def submit_command(self, event: Input.Submitted) -> None:
        picker = self.home.query_one("#config-picker", InlinePicker)
        if picker.display:
            picker.choose()
            return
        line = event.value.strip()
        if not line:
            return
        menu = self.home.query_one("#commands", OptionList)
        if (
            " " not in line
            and line not in self.commands
            and menu.display
            and menu.highlighted is not None
        ):
            line = menu.get_option_at_index(menu.highlighted).id or line
        self.home.query_one("#command", Input).value = ""
        menu.display = False
        self.write("")
        self.write(f"› {line}", BLUE)
        try:
            parts = shlex.split(line)
            command, args = parts[0], parts[1:]
            if self._range_editor is not None and command not in {
                "/quit", "/help", "/jobs", "/history", "/envs", "/setup", "/results", "/runs",
            }:
                raise ValueError("Save the date range or press Esc to cancel before another edit.")
            if self._run_editor is not None and command not in {
                "/quit", "/help", "/jobs", "/history", "/envs", "/setup", "/results", "/runs",
            }:
                raise ValueError("Submit the run or press Esc to cancel before another edit.")
            if command == "/runs":
                if args:
                    raise ValueError("Usage: /runs")
                self.run_actions.start("runs")
                return
            if self._factor_editor is not None and command not in {
                "/quit",
                "/help",
                "/jobs",
                "/history",
                "/envs",
                "/setup",
            }:
                raise ValueError("Save the factor or press Esc to cancel before another edit.")
            if command in {"/init", "/factor"}:
                self.library.handle(command, args)
                return
            if command in {"/node", "/resources"}:
                self.dispatch_resources(command, args)
                return
            if command == "/range":
                self.dispatch_range(args)
                return
            if self.online is not None:
                self.dispatch_online(command, args)
                return
            if command not in COMMANDS:
                raise ValueError("Unknown command. Use /help to see available commands.")
            if command not in {"/results", "/cancel"} and args:
                raise ValueError(f"{command} takes no arguments.")
            if command == "/quit":
                self.exit()
            elif command == "/run":
                self.action_new_run()
            elif command == "/jobs":
                self.show_jobs()
            elif command == "/results":
                raw = bool(args and args[-1] == "--raw")
                if raw:
                    args = args[:-1]
                if len(args) > 1 or (args and args[0].startswith("--")):
                    raise ValueError("Usage: /results [job-id] [--raw]")
                jobs = self.backend.jobs()
                if not args and not jobs:
                    raise ValueError("No jobs yet. Start with /run.")
                self.show_job(args[0] if args else jobs[0].id, raw=raw)
            elif command == "/cancel":
                if len(args) != 1:
                    raise ValueError("Usage: /cancel job-id")
                job = self.backend.cancel(args[0])
                self.write(f"Cancelled local demo job {job.id}.")
            elif command == "/setup":
                self.write(
                    "Mode: local demo. SSH access is not checked in this mode.\n"
                    f"Next task: {self.resource_summary()}. "
                    "Restart with --online for server queries."
                )
            elif command == "/data":
                self.write(
                    "Example field: ret (daily return). "
                    f"Default range: {self.date_range.start} to {self.date_range.end}.\n"
                    "Real data and coverage are not verified. Historical PIT: UNKNOWN."
                )
            else:
                self.write(
                    "\n".join(f"{cmd:<10} {description}" for cmd, description in COMMANDS.items())
                )
        except (ValueError, OSError) as exc:
            self.write(f"Error: {exc}", "#ffffff")

    def dispatch_resources(self, command: str, args: list[str]) -> None:
        if len(args) > 1:
            raise ValueError(
                f"Usage: {command} [A|C]" if command == "/node" else "Usage: /resources [CPUs]"
            )
        if not args:
            if command == "/node":
                choices = []
                for node, limit in NODE_CPU_LIMITS.items():
                    request = ResourceRequest(node, min(self.resources.cpus, limit))
                    preview = f"{request.cpus} CPU / {request.memory_gb} GB for the next task"
                    if request.cpus != self.resources.cpus:
                        preview += f" · Will cap {self.resources.cpus} → {request.cpus} CPU"
                    choices.append(
                        Choice(
                            f"Node {node[-1].upper()}  ·  up to {limit} CPU / {limit * 3} GB",
                            request,
                            preview,
                        )
                    )
                selected = list(NODE_CPU_LIMITS).index(self.resources.node)
                title = "Choose node"
            else:
                choices = [
                    Choice(
                        f"{cpus} CPU / {cpus * 3} GB",
                        ResourceRequest(self.resources.node, cpus),
                        f"Node {self.resources.node[-1].upper()} · 1 CPU + 3 GB per level",
                    )
                    for cpus in range(1, NODE_CPU_LIMITS[self.resources.node] + 1)
                ]
                selected = self.resources.cpus - 1
                title = "Choose resources"
            self.open_config_picker(
                title,
                choices,
                selected,
                lambda value: self.apply_resources(cast(ResourceRequest, value)),
            )
        elif command == "/node":
            self.apply_resources(ResourceRequest(node=args[0], cpus=self.resources.cpus))
        else:
            try:
                cpus = int(args[0])
            except ValueError:
                raise ValueError("Usage: /resources [integer CPUs]") from None
            self.apply_resources(ResourceRequest(node=self.resources.node, cpus=cpus))

    def dispatch_range(self, args: list[str]) -> None:
        if self.editing:
            raise ValueError("Finish the current edit before changing the default date range.")
        if len(args) == 2:
            self.apply_date_range(DateRange(*args))
        elif args:
            raise ValueError("Usage: /range [YYYY-MM-DD YYYY-MM-DD]")
        else:
            self.close_config_picker()
            self.home.query_one("#commands", OptionList).display = False
            editor = DateRangeEditor(self.date_range, on_save=self.apply_date_range)
            self._range_editor = editor
            host = self.home.query_one("#range-editor-host", Vertical)
            host.display = True
            self.add_class("editing-range")
            host.mount(editor)

    def apply_date_range(self, value: DateRange) -> None:
        if self.date_range_store is not None:
            self.date_range_store.save(value)
        self.date_range = value
        self.write(f"Default date range: {value.start} → {value.end}", MINT)
        self.write(
            "Saved locally for future runs. Existing jobs are unchanged."
            if self.date_range_store is not None
            else "Applies to future runs in this session. Existing jobs are unchanged.",
            MUTED,
        )

    @on(DateRangeEditor.Closed)
    async def finish_range_editor(self, event: DateRangeEditor.Closed) -> None:
        if event.editor is not self._range_editor:
            return
        self._range_editor = None
        host = self.home.query_one("#range-editor-host", Vertical)
        host.display = False
        await host.remove_children()
        self.remove_class("editing-range")
        self.action_focus_command()

    def open_config_picker(
        self,
        title: str,
        choices: list[Choice],
        selected: int,
        on_select: Callable[[object], None],
    ) -> None:
        if self.editing:
            self.write("Finish the current edit before opening another selector.", PEACH)
            return
        self.home.query_one("#commands", OptionList).display = False
        self._config_callback = on_select
        self.home.query_one("#config-picker", InlinePicker).open(title, choices, selected)
        self.action_focus_command()

    def open_factor_editor(
        self, editor: FactorEditor, on_close: Callable[[FactorDefinition | None], None]
    ) -> None:
        if self.editing:
            self.write("Finish the current edit first.", PEACH)
            return
        self.close_config_picker()
        self.home.query_one("#commands", OptionList).display = False
        self._factor_editor = editor
        self._factor_callback = on_close
        host = self.home.query_one("#factor-editor-host", Vertical)
        host.display = True
        self.add_class("editing-factor")
        self.size_factor_editor()
        host.mount(editor)

    def size_factor_editor(self) -> None:
        self.home.query_one("#factor-editor-host").styles.height = max(
            7, min(12, self.size.height - 10)
        )

    def action_save_factor(self) -> None:
        if self._factor_editor is not None:
            self._factor_editor.action_save_factor()

    def action_confirm_editor(self) -> None:
        if self._range_editor is not None:
            self._range_editor.action_save_range()
        elif self._run_editor is not None:
            self._run_editor.action_confirm()
        else:
            self.action_save_factor()

    def open_run_editor(self, editor: RunEditor, on_close: Callable[[dict | None], None]) -> None:
        if self.editing:
            raise ValueError("Finish the current edit first.")
        self.close_config_picker()
        self.home.query_one("#commands", OptionList).display = False
        self._run_editor = editor
        self._run_callback = on_close
        host = self.home.query_one("#run-editor-host", Vertical)
        host.display = True
        self.add_class("editing-run")
        self.size_run_editor()
        host.mount(editor)

    def size_run_editor(self) -> None:
        self.home.query_one("#run-editor-host").styles.height = max(
            7, min(12, self.size.height - 10)
        )

    @on(RunEditor.Closed)
    async def finish_run_editor(self, event: RunEditor.Closed) -> None:
        if event.editor is not self._run_editor:
            return
        callback = self._run_callback
        if callback is not None:
            try:
                callback(event.values)
            except (ValueError, OSError) as exc:
                event.editor.set_error(f"Could not submit: {exc}")
                return
        self._run_editor = None
        self._run_callback = None
        host = self.home.query_one("#run-editor-host", Vertical)
        host.display = False
        await host.remove_children()
        self.remove_class("editing-run")
        self.action_focus_command()

    @on(FactorEditor.Closed)
    async def finish_factor_editor(self, event: FactorEditor.Closed) -> None:
        if self._factor_editor is None:
            return
        callback = self._factor_callback
        self._factor_editor = None
        self._factor_callback = None
        host = self.home.query_one("#factor-editor-host", Vertical)
        host.display = False
        await host.remove_children()
        self.remove_class("editing-factor")
        if callback is not None:
            callback(event.factor)
        self.action_focus_command()

    def close_config_picker(self) -> None:
        self.home.query_one("#config-picker", InlinePicker).close()
        self._config_callback = None
        self.action_focus_command()

    @on(InlinePicker.Chosen)
    def choose_config(self, event: InlinePicker.Chosen) -> None:
        callback = self._config_callback
        self.close_config_picker()
        if callback is not None:
            callback(event.value)

    @on(InlinePicker.Cancelled)
    def cancel_config(self) -> None:
        self.close_config_picker()

    def apply_resources(self, request: ResourceRequest | None) -> None:
        if request is None:
            self.action_focus_command()
            return
        try:
            if self.resource_store is not None:
                self.resource_store.save(request)
            self.resources = request
            self.home.query_one("#mode", Static).update(self.mode_text())
            self.home.query_one("#resource-status", Static).update(self.resource_text())
            self.write(f"Next task: {self.resource_summary()}", MINT)
            self.write(
                "Selection saved locally. No cluster resources have been allocated."
                if self.resource_store is not None
                else "Selection applies to this session. No resources have been allocated.",
                "#999999",
            )
        except (ValueError, OSError) as exc:
            self.write(f"Could not save resource selection: {exc}", "#ffffff")
        self.action_focus_command()

    def dispatch_online(self, command: str, args: list[str]) -> None:
        if command == "/run":
            if len(args) > 1:
                raise ValueError("Usage: /run [FACTOR_ID]")
            self.run_actions.open(args[0] if args else None)
            return
        if command in {"/results", "/collect"}:
            if len(args) > 2 or (command == "/collect" and not args):
                raise ValueError(f"Usage: {command} JOB_ID [REQUEST_ID]")
            self.run_actions.start("collect", *args)
            return
        if command in {"/cancel", "/data"}:
            raise ValueError(
                "This command is unavailable online. Cancellation and server changes remain manual."
            )
        if command not in ONLINE_COMMANDS:
            raise ValueError("Unknown online command. Use /help.")
        if command == "/history":
            if args not in ([], ["full"]):
                raise ValueError("Usage: /history [full]")
        elif args:
            raise ValueError(f"{command} takes no arguments.")
        if command == "/quit":
            self.exit()
        elif command == "/help":
            self.write("\n".join(f"{cmd:<10} {desc}" for cmd, desc in ONLINE_COMMANDS.items()))
        elif self._query_pending:
            self.write("A server query is in progress. Please wait.", "#999999")
        else:
            self._query_pending = True
            self.query_online(
                "ssh-diagnostic" if command == "/setup" else REMOTE_QUERIES[command],
                history_limit=10 if command == "/history" and not args else None,
            )

    @work(group="remote-query", exclusive=True)
    async def query_online(self, command: str, *, history_limit: int | None = None) -> None:
        if self.online is None:
            self._query_pending = False
            return
        self._connection_status = "Querying"
        self.home.query_one("#mode", Static).update(self.mode_text())
        self.write(
            "Checking SSH connection and authentication…"
            if command == "ssh-diagnostic"
            else f"Querying server · {command}",
            MUTED,
        )
        try:
            if command == "ssh-diagnostic":
                result = await self.online.diagnose()
                self.write("SSH connection & authentication", BLUE)
                self.write(f"Target  {result.host}:{result.port}")
                self.write(f"Account  {result.user or '(from SSH config)'}")
                self.write(f"Connection  {result.connection}")
                self.write(f"Authentication  {result.authentication}", MINT if result.ok else PEACH)
                if result.ok and result.key_fingerprint:
                    self.write(f"Authenticated key  {result.key_fingerprint}", MINT)
                    if result.key_type:
                        self.write(f"Key type  {result.key_type}", MUTED)
                elif result.ok:
                    self.write("Authenticated key fingerprint was not reported by SSH.", MUTED)
                if result.detail:
                    self.write(result.detail, MUTED if result.ok else PEACH)
                self._connection_status = "Authenticated" if result.ok else "Check failed"
                return
            result = await self.online.query(command)
            output = (
                recent_history(result.stdout, history_limit)
                if history_limit is not None
                else result.stdout
            )
            self.write(output.rstrip() or "Query completed with no output.")
            if history_limit is not None:
                self.write("Up to 10 recent records · /history full for full server history", MUTED)
            if result.stderr.strip():
                self.write(f"SSH notice: {result.stderr.rstrip()}", "#999999")
            if command == "cluster-envs":
                self.write(
                    "SSH query succeeded; FE installation and data access remain unverified.",
                    "#999999",
                )
            self._connection_status = "Last query succeeded"
        except (ValueError, OSError) as exc:
            self.write(f"Query failed: {exc}", "#ffffff")
            self._connection_status = "Last query failed"
        finally:
            self._query_pending = False
            self.home.query_one("#mode", Static).update(self.mode_text())

    def action_new_run(self) -> None:
        if self.editing:
            return
        self.close_config_picker()
        if self.online is not None:
            try:
                self.run_actions.open()
            except (ValueError, OSError) as exc:
                self.write(f"Could not prepare run: {exc}", ROSE)
            return
        if len(self.screen_stack) == 1:
            try:
                factor = self.library.selected()
                values = (
                    {}
                    if factor is None
                    else {"factor": factor.factor_id, "formula": factor.expression}
                )
                self.open_run_editor(
                    RunEditor(resources=self.resources, date_range=self.date_range, **values),
                    self.create_job,
                )
            except (ValueError, OSError) as exc:
                self.write(f"Could not load selected factor: {exc}", ROSE)

    @work(group="run-action")
    async def run_delivery_action(self, action: str, args: tuple) -> None:
        await self.run_actions.execute(action, args)

    def create_job(self, values: dict[str, str | int] | None) -> None:
        if self.backend is None:
            self.write("Demo jobs are unavailable in online mode.")
            return
        if values is not None:
            try:
                job = self.backend.submit(**values)
                self.write(
                    f"Created {job.id} · {job.factor} · Synthetic report ready in about 5 seconds."
                )
                self.write(f"Request: {job.node} / {job.cpus} CPU / {job.memory}", "#999999")
                self.write(f"Report: /results {job.id}    Cancel: /cancel {job.id}", "#999999")
            except (ValueError, OSError) as exc:
                self.write(f"Could not create job: {exc}", "#ffffff")
        self.action_focus_command()

    def show_job(self, job_id: str, *, raw: bool = False) -> None:
        if self.backend is None:
            self.write("Demo reports are unavailable in online mode.")
            return
        try:
            job = self.backend.get(job_id)
            self.write(f"{job.id} · {job.factor} · {STATUS_NAMES[job.status]}")
            self.write(f"Request: {job.node} / {job.cpus} CPU / {job.memory}", "#999999")
            self.write(f"Window: {job.start} → {job.end}", "#999999")
            if job.status == "COMPLETED":
                report = self.backend.report(job_id)
                self.write("Demo report · Synthetic data; no FE execution", PEACH)
                if raw:
                    self.append_output(JSON.from_data(report, highlight=False))
                else:
                    self.write(
                        f"Simulated elapsed  {report['timing']['elapsed_seconds']:.3f} s", MUTED
                    )
                    self.write(
                        f"Rows  {report['output_rows']:,}   Valid  {report['valid_rows']:,}", MINT
                    )
                    self.write(f"Historical PIT  {report['historical_pit']}", PEACH)
                    self.write(f"Full report: /results {job.id} --raw", MUTED)
            else:
                self.write(job.error or "No completed report is available for this job.")
        except (ValueError, OSError) as exc:
            self.write(f"Error: {exc}", "#ffffff")

    def action_focus_command(self) -> None:
        if len(self.screen_stack) == 1:
            self.home.query_one("#command", Input).focus()
