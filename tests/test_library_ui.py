from types import SimpleNamespace
from unittest.mock import AsyncMock

from textual.widgets import Button, Checkbox, Input, RichLog, Static, TextArea

from fe_cli.app import FactorTerminal
from fe_cli.backend import DemoBackend
from fe_cli.catalog import FactorDefinition
from fe_cli.config import ConnectionConfig
from fe_cli.factor_screen import FactorEditor
from fe_cli.picker import InlinePicker
from fe_cli.screens import RunEditor
from fe_cli.workspace import Workspace


async def command(app, pilot, value):
    app.home.query_one("#command", Input).value = value
    await pilot.press("enter")


def transcript(app):
    return "\n".join(line.text for line in app.home.query_one("#transcript", RichLog).lines)


def factor_editor(app):
    editor = app.query_one(FactorEditor)
    assert len(app.screen_stack) == 1
    assert editor.screen is app.home
    assert app.home.query_one("#factor-editor-host").display
    return editor


async def test_init_and_factor_creation_are_local_even_online(tmp_path):
    workspace = Workspace(tmp_path / "library")
    remote = SimpleNamespace(config=ConnectionConfig("cluster.example"), query=AsyncMock())
    app = FactorTerminal(online=remote, workspace=workspace)
    async with app.run_test(size=(80, 24)) as pilot:
        assert not workspace.root.exists()
        await command(app, pilot, "/init")
        assert workspace.catalog_path.exists()
        assert not workspace.catalog.list()
        assert str(workspace.catalog_path) in transcript(app).replace("\n", "")
        await command(app, pilot, "/factor new")
        editor = factor_editor(app)
        editor.query_one("#factor-id", Input).value = "ret_mean"
        expression = 'ts_mean(\n  col("ret"), 20\n)'
        editor.query_one("#factor-expression", TextArea).text = expression
        await pilot.press("enter")
        await pilot.pause()
        factor = workspace.catalog.get("ret_mean")
        assert factor.name == "ret_mean" and factor.expression == expression
        assert app.library.selected_id == "ret_mean"
        assert not app.query(FactorEditor)
        assert not app.home.query_one("#factor-editor-host").display
        assert app.home.query_one("#command", Input).has_focus
        assert app.backend is None
        remote.query.assert_not_called()
        assert not list(workspace.values_path.iterdir())


async def test_factor_editor_stays_above_command_and_restores_focus_at_80x24(tmp_path):
    workspace = Workspace(tmp_path / "library")
    workspace.initialize()
    app = FactorTerminal(backend=DemoBackend(tmp_path / "state"), workspace=workspace)
    async with app.run_test(size=(80, 24)) as pilot:
        home = app.screen
        previous_transcript = transcript(app)
        await command(app, pilot, "/factor new")
        await pilot.pause()
        editor = factor_editor(app)
        field = editor.query_one("#factor-id", Input)
        command_field = app.home.query_one("#command", Input)
        assert app.screen is home
        assert field.has_focus
        assert editor.region.height > 0
        assert editor.region.y >= 0
        assert editor.region.bottom <= command_field.region.y
        assert command_field.region.bottom <= app.screen.region.bottom
        shortcuts = editor.query_one("#factor-shortcuts", Static)
        assert shortcuts.region.height == 1
        assert editor.region.contains_region(shortcuts.region)
        assert "Enter" in str(shortcuts.render()) and "save" in str(shortcuts.render())
        assert "Ctrl+J newline" in str(shortcuts.render())
        assert "Esc cancel" in str(shortcuts.render())
        assert not editor.query(Button)
        assert previous_transcript in transcript(app)
        await pilot.press("d", "r", "a", "f", "t")
        assert field.value == "draft"
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is home
        assert len(app.screen_stack) == 1
        assert not app.query(FactorEditor)
        assert not app.home.query_one("#factor-editor-host").display
        assert command_field.has_focus
        assert workspace.catalog.list() == []


async def test_factor_formula_ctrl_j_adds_newline_then_enter_saves(tmp_path):
    workspace = Workspace(tmp_path / "library")
    workspace.initialize()
    backend = DemoBackend(tmp_path / "state")
    app = FactorTerminal(backend=backend, workspace=workspace)
    async with app.run_test(size=(80, 24)) as pilot:
        await command(app, pilot, "/factor new")
        editor = factor_editor(app)
        editor.query_one("#factor-id", Input).value = "multiline"
        expression = editor.query_one("#factor-expression", TextArea)
        expression.load_text('rank(\ncol("close"))')
        expression.move_cursor((0, 5))
        expression.focus()
        await pilot.press("ctrl+j")
        assert expression.text == 'rank(\n\ncol("close"))'
        assert factor_editor(app) is editor and workspace.catalog.list() == []
        assert not backend.jobs()
        await pilot.press("enter")
        await pilot.pause()
        assert workspace.catalog.get("multiline").expression == 'rank(\n\ncol("close"))'
        assert not app.query(FactorEditor) and not backend.jobs()
        assert app.home.query_one("#command", Input).has_focus


async def test_edit_preserves_fixed_id_and_disabled_factor_clears_selection(tmp_path):
    workspace = Workspace(tmp_path / "library")
    workspace.initialize()
    workspace.catalog.add(FactorDefinition("f01", "Original", 'col("ret")'))
    app = FactorTerminal(backend=DemoBackend(tmp_path / "state"), workspace=workspace)
    async with app.run_test(size=(80, 24)) as pilot:
        await command(app, pilot, "/factor use f01")
        await command(app, pilot, "/factor edit f01")
        editor = factor_editor(app)
        assert editor.query_one("#factor-id", Input).disabled
        assert editor.query_one("#factor-label", Input).has_focus
        editor.query_one("#factor-label", Input).value = "Updated"
        editor.query_one("#factor-enabled", Checkbox).value = False
        await pilot.press("enter")
        await pilot.pause()
        saved = workspace.catalog.get("f01")
        assert saved.name == "Updated"
        assert saved.enabled is False
        assert app.library.selected_id is None
        assert not app.query(FactorEditor)
        assert app.home.query_one("#command", Input).has_focus


async def test_picker_loads_latest_saved_definition_into_demo_run(tmp_path):
    workspace = Workspace(tmp_path / "library")
    workspace.initialize()
    factor = workspace.catalog.add(FactorDefinition("mean20", "Mean return", 'col("ret")'))
    backend = DemoBackend(tmp_path / "state", clock=lambda: 100)
    app = FactorTerminal(backend=backend, workspace=workspace)
    async with app.run_test(size=(100, 40)) as pilot:
        await command(app, pilot, "/factor")
        assert app.home.query_one(InlinePicker).display
        await pilot.press("enter")
        await pilot.pause()
        assert app.library.selected_id == factor.factor_id
        assert not app.home.query_one(InlinePicker).display
        updated = workspace.catalog.update("mean20", expression='ts_mean(col("ret"), 20)')
        await command(app, pilot, "/run")
        editor = app.query_one(RunEditor)
        assert len(app.screen_stack) == 1 and editor.screen is app.home
        assert editor.query_one("#factor-name", Input).value == "mean20"
        assert editor.query_one("#formula", TextArea).text == updated.expression
        editor.query_one("#formula", TextArea).text = 'ts_mean(col("ret"), 10)'
        await pilot.press("enter")
        await pilot.pause()
        assert backend.jobs()[0].formula == 'ts_mean(col("ret"), 10)'
        assert workspace.catalog.get("mean20") == updated


async def test_stale_factor_editor_does_not_overwrite_external_changes(tmp_path):
    workspace = Workspace(tmp_path / "library")
    workspace.initialize()
    workspace.catalog.add(FactorDefinition("f01", "Original", 'col("ret")'))
    app = FactorTerminal(backend=DemoBackend(tmp_path / "state"), workspace=workspace)
    async with app.run_test(size=(80, 24)) as pilot:
        await command(app, pilot, "/factor edit f01")
        editor = factor_editor(app)
        editor.query_one("#factor-label", Input).value = "My edit"
        external = workspace.catalog.update("f01", name="External edit")
        await pilot.press("enter")
        await pilot.pause()
        assert workspace.catalog.get("f01") == external
        assert factor_editor(app) is editor
        assert "Could not save factor" in str(
            editor.query_one("#factor-error", Static).render()
        )
        assert editor.query_one("#factor-label", Input).value == "My edit"
        await pilot.press("escape")
        assert not app.query(FactorEditor)
        assert app.home.query_one("#command", Input).has_focus


async def test_duplicate_id_keeps_unsaved_expression_available_for_retry(tmp_path):
    workspace = Workspace(tmp_path / "library")
    workspace.initialize()
    workspace.catalog.add(FactorDefinition("f01", "Existing", 'col("ret")'))
    app = FactorTerminal(backend=DemoBackend(tmp_path / "state"), workspace=workspace)
    async with app.run_test(size=(80, 24)) as pilot:
        await command(app, pilot, "/factor new")
        editor = factor_editor(app)
        editor.query_one("#factor-id", Input).value = "f01"
        expression = 'ts_mean(col("ret"), 20)'
        editor.query_one("#factor-expression", TextArea).text = expression
        await pilot.press("enter")
        await pilot.pause()
        assert factor_editor(app) is editor
        assert editor.query_one("#factor-expression", TextArea).text == expression
        assert "Could not save factor" in str(editor.query_one("#factor-error", Static).render())
        editor.query_one("#factor-id", Input).value = "f02"
        await pilot.press("enter")
        await pilot.pause()
        assert workspace.catalog.get("f02").expression == expression
        assert workspace.catalog.get("f01").expression == 'col("ret")'
        assert not app.query(FactorEditor)
        assert app.home.query_one("#command", Input).has_focus


async def test_deleting_definition_preserves_results_and_clears_selection(tmp_path):
    workspace = Workspace(tmp_path / "library")
    workspace.initialize()
    workspace.catalog.add(FactorDefinition("f01", "Example", 'col("ret")'))
    value_file = workspace.values_path / "preserved.txt"
    value_file.write_text("existing local output")
    app = FactorTerminal(backend=DemoBackend(tmp_path / "state"), workspace=workspace)
    async with app.run_test() as pilot:
        await command(app, pilot, "/factor use f01")
        await command(app, pilot, "/factor delete f01")
        assert app.library.selected_id is None
        assert workspace.catalog.list() == []
        assert value_file.read_text() == "existing local output"


async def test_uninitialized_disabled_and_missing_factors_do_not_run(tmp_path):
    workspace = Workspace(tmp_path / "library")
    backend = DemoBackend(tmp_path / "state", clock=lambda: 100)
    app = FactorTerminal(backend=backend, workspace=workspace)
    async with app.run_test() as pilot:
        await command(app, pilot, "/factor new")
        assert len(app.screen_stack) == 1 and not workspace.root.exists()
        await command(app, pilot, "/init")
        workspace.catalog.add(FactorDefinition("f01", "Disabled", 'col("ret")', False))
        await command(app, pilot, "/factor use f01")
        assert app.library.selected_id is None
        assert "disabled" in transcript(app)
        workspace.catalog.update("f01", enabled=True)
        await command(app, pilot, "/factor use f01")
        workspace.catalog.delete("f01")
        await command(app, pilot, "/run")
        assert len(app.screen_stack) == 1
        assert "Could not load selected factor" in transcript(app)
        assert not backend.jobs()


async def test_custom_init_is_idempotent_and_switches_only_current_session(tmp_path):
    workspace = Workspace(tmp_path / "first")
    custom = Workspace(tmp_path / "with spaces")
    app = FactorTerminal(backend=DemoBackend(tmp_path / "state"), workspace=workspace)
    async with app.run_test() as pilot:
        await command(app, pilot, f'/init "{custom.root}"')
        custom.catalog.add(FactorDefinition("f01", "Example", 'col("ret")'))
        original = custom.catalog_path.read_bytes()
        await command(app, pilot, "/factor use f01")
        await command(app, pilot, "/init")
        assert app.library.workspace.root == custom.root
        assert app.library.selected_id == "f01"
        assert custom.catalog_path.read_bytes() == original
        assert not workspace.root.exists()
