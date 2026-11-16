"""Explicit initialization of a local factor-definition workspace."""

from __future__ import annotations

import csv
import io
import os
from dataclasses import dataclass
from pathlib import Path

from fe_cli.catalog import CSV_HEADERS, FactorCatalog
from fe_cli.runtime_settings import RuntimeStore

_README = """# Local factor workspace

This workspace stores local factor definitions and validated single-run results.
Editing definitions does not evaluate expressions or validate Factor Engine DSL.

## Files

- `factors.csv`: editable UTF-8 standard CSV, initially containing only its header.
- `runtime.json`: standard server Python, Core entry and data location.
- `runs/`: frozen requests, submission state, result manifests and commit receipts.
- `values/FACTOR_ID/DEFINITION_HASH/REQUEST_ID/result.parquet`: immutable local values
  published only after a real result passes validation.

Initialization configures `/public/fe-core-env/bin/python`, `/public/fe-core`,
`/srv/quant/data/shared/ashare_lqtp_data` and node `quant-a`. New runs use the
server's current Core release; you do not need to configure a release digest
manually after Core updates. Initialization only writes local files and does not
connect to SSH or install anything on the server.
Use `fe configure-run --help` to customize approved server paths or pin a release
digest. Existing valid runtime settings are preserved by repeated initialization.
Use `fe --online run ID` to submit one saved factor, then `fe --online results JOB_ID`
after completion to download and validate its values. Incremental runs and run-all
are not supported yet. Default demo mode only simulates runs.

## Maintaining factors

The exact columns, in order, are:
`factor_id,name,expression,enabled,description`.

Each factor needs a unique ID (1–80 ASCII letters, numbers, underscores, dots or
hyphens, starting with a letter or number), a name, and a nonempty expression.
`enabled` accepts true/false or 1/0, ignoring case. Description is optional.
Use standard CSV quoting for commas, double quotes and multiline expressions;
spreadsheet editors can maintain these fields. Save as UTF-8 (a BOM is accepted).

Use `fe factor list`, `fe factor show ID`, `fe factor add`, `fe factor update ID`,
and `fe factor delete ID` to maintain definitions, or edit `factors.csv` directly.
Use `fe --workspace PATH ...` when this workspace is outside ~/fe-workspace.
Run `fe init PATH` to complete missing workspace files without replacing existing
valid files. Local edits check required fields and CSV structure only; successful
saving does not mean an expression is valid FE DSL or has been computed.

The CLI coordinates its writes with an advisory lock and checks for concurrent
file changes before replacing the CSV. Close other editors while saving: editors
that ignore the lock can still race with the final file replacement.
"""


@dataclass(frozen=True)
class InitResult:
    workspace: Path
    csv_path: Path
    readme_path: Path
    created: bool


class Workspace:
    def __init__(self, path: Path | None = None) -> None:
        self.root = (Path(path) if path is not None else Path.home() / "fe-workspace")
        self.root = self.root.expanduser().resolve()
        self.catalog_path = self.root / "factors.csv"
        self.values_path = self.root / "values"
        self.runs_path = self.root / "runs"
        self.readme_path = self.root / "README.md"
        self.runtime_store = RuntimeStore(self.root)

    @property
    def catalog(self) -> FactorCatalog:
        return FactorCatalog(self.catalog_path)

    def _check_existing(self) -> None:
        for directory in (self.root, self.values_path, self.runs_path):
            if (directory.exists() or directory.is_symlink()) and not directory.is_dir():
                raise ValueError(f"Workspace directory is occupied by a file: {directory}.")
        for file in (self.catalog_path, self.readme_path, self.runtime_store.path):
            if file.is_symlink() or (file.exists() and not file.is_file()):
                raise ValueError(f"Workspace file is occupied by another entry: {file}.")
        if self.catalog_path.exists():
            self.catalog.list()
        if self.runtime_store.path.exists():
            self.runtime_store.load()

    @staticmethod
    def _create_file(path: Path, content: str) -> bool:
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        return True

    def initialize(self) -> InitResult:
        self._check_existing()
        created = False
        for directory in (self.root, self.values_path, self.runs_path):
            if not directory.exists():
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                created = True
        header = io.StringIO(newline="")
        csv.writer(header).writerow(CSV_HEADERS)
        created = self._create_file(self.catalog_path, header.getvalue()) or created
        # Recheck races before writing README, including a catalog created by another init.
        self._check_existing()
        created = self._create_file(self.readme_path, _README) or created
        created = self.runtime_store.initialize() or created
        self._check_existing()
        return InitResult(self.root, self.catalog_path, self.readme_path, created)
