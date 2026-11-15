import pytest
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, Collapsible, Input, Select, Static, TextArea
from textual.widgets._select import InvalidSelectValueError

from fe_cli.resources import ResourceRequest
from fe_cli.screens import ResourceFields, RunEditor


class FormApp(App):
    CSS = """
    #run-editor-host { height: 12; }
    #command { height: 3; }
    """

    def __init__(self, form: RunEditor) -> None:
        super().__init__()
        self.form = form
        self.results = []

    def compose(self) -> ComposeResult:
        yield Static("Session output")
        with Vertical(id="run-editor-host"):
            yield self.form
        yield Input(id="command")

    def on_run_editor_closed(self, event: RunEditor.Closed) -> None:
        assert event.editor is self.form
        self.results.append(event.values)
        self.query_one("#run-editor-host").display = False
        self.query_one("#command", Input).focus()


async def test_run_resource_fields_cap_cpu_when_switching_nodes() -> None:
    form = RunEditor(ResourceRequest("C", 8))
    app = FormApp(form)
    async with app.run_test(size=(80, 24)) as pilot:
        form.query_one("#run-node", Select).value = "quant-a"
        await pilot.pause()
        cpu_select = form.query_one("#run-cpus", Select)
        assert cpu_select.value == 6
        assert form.query_one(ResourceFields).resources == ResourceRequest("A", 6)
        assert "Adjusted from 8 to 6 CPU" in str(
            form.query_one("#run-resources-summary", Static).render()
        )
        with pytest.raises(InvalidSelectValueError, match="Illegal select value"):
            cpu_select.value = 8


async def test_run_default_resources_and_per_job_payload() -> None:
    initial = ResourceRequest()
    form = RunEditor(initial)
    app = FormApp(form)
    async with app.run_test(size=(80, 24)) as pilot:
        assert form.query_one("#run-node", Select).value == "quant-a"
        assert form.query_one("#run-cpus", Select).value == 6
        assert form.query_one("#factor-name", Input).has_focus
        assert len(app.screen_stack) == 1
        assert form.query_one(Collapsible).collapsed
        assert not form.query(Button)
        assert form.query_one("#run-shortcuts").region.bottom <= 24
        form.query_one("#run-node", Select).value = "quant-c"
        await pilot.pause()
        form.query_one("#run-cpus", Select).value = 8
        await pilot.pause()
        await pilot.press("enter")
        result = app.results[0]
        assert result["node"] == "quant-c"
        assert result["cpus"] == 8
        assert result["factor"] == "f01"
        assert result["start"] == "2026-10-01"
        assert result["end"] == "2026-10-31"
        assert result["formula"]
        assert "memory" not in result
        assert initial == ResourceRequest()


async def test_run_without_preferences_uses_default_pair() -> None:
    app = FormApp(RunEditor())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("enter")
        assert app.results[0]["node"] == "quant-a"
        assert app.results[0]["cpus"] == 6


@pytest.mark.parametrize("newline_key", ["ctrl+j", "shift+enter"])
async def test_run_expression_newline_shortcuts_and_enter_confirms(newline_key: str) -> None:
    form = RunEditor(online=True, source_label="Disk source [literal]")
    app = FormApp(form)
    async with app.run_test(size=(80, 24)) as pilot:
        formula = form.query_one("#formula", TextArea)
        formula.load_text('col("ret")')
        formula.focus()
        await pilot.press("end", newline_key, "+", "1")
        assert formula.text == 'col("ret")\n+1'
        assert app.results == []
        await pilot.press("enter")
        assert app.results[0]["formula"] == 'col("ret")\n+1'
        assert app.query_one("#command", Input).has_focus


@pytest.mark.parametrize(
    "field, value, message",
    [
        ("#factor-name", " ", "Enter a factor name and formula."),
        ("#start-date", "2026-2-01", "Check parameters:"),
        ("#start-date", "2026-11-01", "Start date must not be after end date."),
    ],
)
async def test_run_invalid_parameters_keep_draft(field: str, value: str, message: str) -> None:
    form = RunEditor()
    app = FormApp(form)
    async with app.run_test(size=(80, 24)) as pilot:
        form.query_one(field, Input).value = value
        await pilot.press("enter")
        assert app.results == []
        assert form.query_one(field, Input).value == value
        assert message in str(form.query_one("#run-error", Static).render())
        form.query_one(field, Input).value = (
            "f01" if field == "#factor-name" else "2026-10-01"
        )
        await pilot.press("enter")
        assert len(app.results) == 1


async def test_run_cancel_and_confirmation_close_only_once() -> None:
    form = RunEditor()
    app = FormApp(form)
    async with app.run_test(size=(80, 24)) as pilot:
        form.query_one("#formula", TextArea).focus()
        await pilot.press("escape")
        form.action_dismiss_form()
        form.action_confirm()
        await pilot.pause()
        assert app.results == [None]


async def test_run_rejected_confirmation_can_retry_without_duplicate_acceptance() -> None:
    class RejectOnceApp(FormApp):
        rejected = False

        def on_run_editor_closed(self, event: RunEditor.Closed) -> None:
            event.prevent_default()
            if not self.rejected:
                self.rejected = True
                event.editor.set_error("Submission rejected; revise and try again.")
                return
            super().on_run_editor_closed(event)

    form = RunEditor()
    app = RejectOnceApp(form)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("enter")
        assert app.results == []
        assert "Submission rejected" in str(form.query_one("#run-error", Static).render())
        assert form.query_one("#factor-name", Input).value == "f01"
        form.query_one("#factor-name", Input).value = "revised"
        await pilot.press("enter")
        form.action_confirm()
        form.action_dismiss_form()
        await pilot.pause()
        assert len(app.results) == 1
        assert app.results[0]["factor"] == "revised"


async def test_run_enter_keeps_resource_selection_local() -> None:
    form = RunEditor()
    app = FormApp(form)
    async with app.run_test(size=(80, 24)) as pilot:
        details = form.query_one(Collapsible)
        details.query_one("CollapsibleTitle").focus()
        await pilot.press("enter")
        assert not details.collapsed
        assert app.results == []
        node = form.query_one("#run-node", Select)
        node.focus()
        await pilot.press("enter")
        assert node.expanded
        assert app.results == []
        await pilot.press("down", "enter")
        assert not node.expanded
        assert node.value == "quant-c"
        assert app.results == []


async def test_run_enter_does_not_confirm_during_copy_mode() -> None:
    form = RunEditor()
    app = FormApp(form)
    async with app.run_test(size=(80, 24)) as pilot:
        app._copy_mode = True
        await pilot.press("enter")
        assert app.results == []
        app._copy_mode = False
        await pilot.press("enter")
        assert len(app.results) == 1
