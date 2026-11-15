"""Terminal presentation for explicit single-factor submissions and collection."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

from textual.widgets import Static

from fe_cli.archive_io import ExecutionFailed
from fe_cli.execution import RunService
from fe_cli.resources import ResourceRequest
from fe_cli.runtime_settings import RuntimeSettings, RuntimeStore
from fe_cli.screens import RunEditor
from fe_cli.theme import MINT, MUTED, PEACH

if TYPE_CHECKING:
    from fe_cli.app import FactorTerminal


class OnlineRuns:
    def __init__(self, app: FactorTerminal) -> None:
        self.app = app
        self.runtime: RuntimeSettings | None = None
        self.last_request_id: str | None = None

    def service(self) -> RunService:
        return RunService(
            self.app.library.workspace,
            self.app.online.config if self.app.online is not None else None,
            settings=self.runtime,
        )

    def show_timing(self, record: dict) -> None:
        timing = record.get("timing")
        self.app.write(
            f"Run elapsed  {timing['elapsed_seconds']:.3f} s · excludes queue and download"
            if timing is not None
            else "Run elapsed  unavailable · no timing recorded in this result",
            MUTED,
        )

    def open(self, factor_id: str | None = None) -> None:
        if len(self.app.screen_stack) != 1:
            return
        if self.app._query_pending:
            raise ValueError("A server action is in progress. Please wait.")
        if factor_id is not None:
            factor = self.app.library.workspace.catalog.get(factor_id)
        else:
            factor = self.app.library.selected()
        if factor is None:
            raise ValueError("Select a saved factor with /factor, or use /run FACTOR_ID.")
        if not factor.enabled:
            raise ValueError("Enable this factor before running it.")
        self.runtime = RuntimeStore(self.app.library.workspace.root).load()
        if self.runtime.node not in {self.app.resources.node, "shared"}:
            raise ValueError(f"Runtime is configured for {self.runtime.node}; change /node first.")
        self.app.open_run_editor(
            RunEditor(
                resources=self.app.resources,
                factor=factor.factor_id,
                formula=factor.expression,
                online=True,
                source_label=f"Source: {self.runtime.data_root}",
                date_range=self.app.date_range,
            ),
            self.submit,
        )

    def submit(self, values: dict | None) -> None:
        if values is None:
            self.app.action_focus_command()
            return
        factor = self.app.library.workspace.catalog.get(values["factor"])
        if not factor.enabled:
            raise ValueError("This factor is disabled.")
        factor = replace(factor, expression=values["formula"])
        resources = ResourceRequest(values["node"], values["cpus"])
        self.start("submit", factor, resources, values["start"], values["end"])
        self.app.action_focus_command()

    def start(self, action: str, *args) -> None:
        if self.app._query_pending:
            raise ValueError("A server action is in progress. Please wait.")
        self.app._query_pending = True
        self.app.run_delivery_action(action, args)

    async def execute(self, action: str, args: tuple) -> None:
        service = self.service()
        self.app._connection_status = "Submitting" if action == "submit" else "Collecting"
        self.app.home.query_one("#mode", Static).update(self.app.mode_text())
        try:
            if action == "submit":
                self.app.write("Submitting one factor job…", MUTED)
                record = await service.submit(*args)
                self.last_request_id = record["request_id"]
                self.app.write(f"Submitted job {record['job_id']}", MINT)
                self.app.write(f"Request: {record['request_id']}", MUTED)
                self.app.write(f"After completion: /results {record['job_id']}")
                self.app._connection_status = "Job submitted"
            elif action == "collect":
                identifier = (
                    args[1] if len(args) == 2 else args[0] if args else self.last_request_id
                )
                if not identifier:
                    raise ValueError("Usage: /results JOB_ID or /results REQUEST_ID")
                record = await asyncio.to_thread(service.get, identifier)
                if record["status"] == "EXECUTION_FAILED":
                    self.show_timing(record)
                    raise ValueError(record["error"])
                if record["status"] != "COMMITTED":
                    job_id = args[0] if len(args) == 2 else record.get("job_id")
                    if not job_id:
                        raise ValueError(
                            "No confirmed Job ID. Check /history; then use "
                            "/results JOB_ID REQUEST_ID to verify the original request."
                        )
                    self.app.write("Downloading and checking the real result…", MUTED)
                    record = await service.collect(
                        job_id, request_id=record["request_id"]
                    )
                self.app.write(f"Local result committed · {record['rows']} rows", MINT)
                self.show_timing(record)
                self.app.write(f"Parquet: {record['values_path']}")
                self.app.write(f"Manifest: {record['manifest_path']}", MUTED)
                self.app._connection_status = "Result saved locally"
            elif action == "runs":
                records = await asyncio.to_thread(service.list_runs)
                for record in records:
                    self.app.write(
                        f"{record['request_id']} · {record['status']} · "
                        f"job {record.get('job_id') or 'unconfirmed'}"
                    )
                if not records:
                    self.app.write("No local single-factor runs yet.", MUTED)
                self.app._connection_status = "Local runs"
        except (ValueError, OSError) as exc:
            if isinstance(exc, ExecutionFailed):
                self.show_timing(exc.manifest)
            self.app.write(f"Run action failed: {exc}", PEACH)
            self.app._connection_status = "Run action failed"
        finally:
            self.app._query_pending = False
            self.app.home.query_one("#mode", Static).update(self.app.mode_text())
