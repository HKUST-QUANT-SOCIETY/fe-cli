"""Local factor library commands shared by the terminal's two modes."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from rich.table import Table
from rich.text import Text

from fe_cli.catalog import FactorDefinition
from fe_cli.factor_screen import FactorEditor
from fe_cli.picker import Choice
from fe_cli.theme import MINT, MUTED, ROSE
from fe_cli.workspace import Workspace

if TYPE_CHECKING:
    from fe_cli.app import FactorTerminal


class LibraryController:
    def __init__(self, app: FactorTerminal, workspace: Workspace | None = None) -> None:
        self.app = app
        self.workspace = workspace if workspace is not None else Workspace()
        self.selected_id: str | None = None

    def handle(self, command: str, args: list[str]) -> None:
        if command == "/init":
            if len(args) > 1:
                raise ValueError("Usage: /init [directory]")
            workspace = Workspace(args[0]) if args else self.workspace
            result = workspace.initialize()
            if workspace.root != self.workspace.root:
                self.selected_id = None
            self.workspace = workspace
            self.app.write(
                "Workspace ready" if result.created else "Workspace already initialized", MINT
            )
            self.app.write(f"CSV: {result.csv_path}")
            self.app.write("Edit this file directly, or use /factor new to create a factor.", MUTED)
            if workspace.root != Workspace().root:
                self.app.write(
                    f"Next start: fe --workspace {shlex.quote(str(workspace.root))}", MUTED
                )
            return
        if not args:
            self.browse()
            return
        action, *rest = args
        if action in {"new", "add"} and not rest:
            self.workspace.catalog.list()
            self.edit(None)
        elif action == "list" and not rest:
            self.list_factors()
        elif action == "show" and len(rest) == 1:
            self.show(self.workspace.catalog.get(rest[0]))
        elif action == "edit" and len(rest) == 1:
            self.edit(self.workspace.catalog.get(rest[0]))
        elif action == "delete" and len(rest) == 1:
            factor = self.workspace.catalog.delete(rest[0])
            if self.selected_id == factor.factor_id:
                self.selected_id = None
            self.app.write(f"Deleted definition {factor.factor_id}. Result files were preserved.")
        elif action == "use" and len(rest) == 1:
            self.select(rest[0])
        elif len(args) == 1 and action not in {
            "new",
            "add",
            "list",
            "show",
            "edit",
            "delete",
            "use",
        }:
            self.select(action)
        else:
            raise ValueError("Usage: /factor [list|new|show ID|edit ID|delete ID|use ID]")

    def list_factors(self) -> None:
        factors = self.workspace.catalog.list()
        if not factors:
            self.empty_message()
            return
        table = Table("ID", "Name", "Enabled", box=None, pad_edge=False, highlight=False)
        for factor in factors:
            table.add_row(
                Text(factor.factor_id), Text(factor.name), "yes" if factor.enabled else "no"
            )
        self.app.append_output(table)
        self.app.write(f"CSV: {self.workspace.catalog_path}", MUTED)

    def empty_message(self) -> None:
        self.app.write("Your factor library is empty. Use /factor new to add a definition.")
        self.app.write(f"Or edit the CSV directly: {self.workspace.catalog_path}", MUTED)

    def browse(self) -> None:
        factors = self.workspace.catalog.list()
        if not factors:
            self.empty_message()
            return
        choices = [
            Choice(
                f"{factor.factor_id} · {factor.name}"
                + (" · disabled" if not factor.enabled else ""),
                factor.factor_id,
                factor.expression,
            )
            for factor in factors
        ]
        selected = next((i for i, f in enumerate(factors) if f.factor_id == self.selected_id), 0)
        self.app.open_config_picker("Factor library", choices, selected, self.select)

    def select(self, value: object) -> None:
        try:
            factor = self.workspace.catalog.get(str(value))
            if not factor.enabled:
                raise ValueError(
                    f"Factor {factor.factor_id} is disabled. Enable it before running."
                )
            self.selected_id = factor.factor_id
            self.show(factor)
            self.app.write(
                "Selected for the next /run.", MUTED
            )
        except (ValueError, OSError) as exc:
            self.app.write(f"Could not select factor: {exc}", ROSE)

    def show(self, factor: FactorDefinition) -> None:
        self.app.write(f"{factor.factor_id} · {factor.name}", MINT)
        self.app.write(factor.expression)
        if factor.description:
            self.app.write(factor.description, MUTED)
        self.app.write(
            f"{'Enabled' if factor.enabled else 'Disabled'} · "
            f"Definition {factor.definition_hash[:12]} · Engine validation not performed",
            MUTED,
        )

    def edit(self, initial: FactorDefinition | None) -> None:
        def persist(factor: FactorDefinition) -> None:
            if initial is None:
                self.workspace.catalog.add(factor)
            else:
                self.workspace.catalog.update(
                    initial.factor_id,
                    name=factor.name,
                    expression=factor.expression,
                    enabled=factor.enabled,
                    description=factor.description,
                    expected=initial,
                )

        def saved(factor: FactorDefinition | None) -> None:
            if factor is not None:
                if factor.enabled:
                    self.selected_id = factor.factor_id
                elif self.selected_id == factor.factor_id:
                    self.selected_id = None
                self.app.write(f"Saved {factor.factor_id} to {self.workspace.catalog_path}", MINT)
            self.app.action_focus_command()

        self.app.open_factor_editor(FactorEditor(initial, on_save=persist), saved)

    def selected(self) -> FactorDefinition | None:
        if self.selected_id is None:
            return None
        factor = self.workspace.catalog.get(self.selected_id)
        if not factor.enabled:
            raise ValueError(f"Factor {factor.factor_id} is disabled. Enable it before running.")
        return factor
