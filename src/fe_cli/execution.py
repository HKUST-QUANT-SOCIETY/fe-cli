"""Single-run preparation, explicit submission and local result delivery."""

from __future__ import annotations

import ast
import asyncio
import io
import json
import os
import re
import shlex
import tarfile
import tempfile
from dataclasses import asdict
from pathlib import Path

from fe_cli.catalog import FactorDefinition
from fe_cli.config import ConnectionConfig
from fe_cli.resources import ResourceRequest
from fe_cli.run_contract import make_request, validate_request
from fe_cli.runtime_settings import RuntimeSettings, RuntimeStore
from fe_cli.workspace import Workspace

_REQUEST_ID = re.compile(r"[a-f0-9]{32}\Z")
# Public DSL names only; source mapping and data conversion belong to the server.
_DSL_FIELDS = frozenset({
    "open", "high", "low", "close", "pre_close", "ret", "volume", "amount",
    "pb_ratio", "pe_ratio", "ps_ratio", "pcf_ratio", "turnover_ratio",
})


def atomic_json(path: Path, value: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class RunService:
    def __init__(
        self,
        workspace: Workspace,
        config: ConnectionConfig | None = None,
        *,
        transport=None,
        settings: RuntimeSettings | None = None,
    ) -> None:
        self.workspace = workspace
        self.config = config
        self.transport = transport
        self.settings = settings

    def _transport(self):
        if self.transport is None:
            if self.config is None:
                raise ValueError("SSH connection is not configured")
            from fe_cli.job_transport import JobTransport

            self.transport = JobTransport(self.config)
        return self.transport

    def prepare(
        self, factor: FactorDefinition, resources: ResourceRequest, start: str, end: str
    ) -> dict:
        self.workspace.catalog.list()
        if not factor.enabled:
            raise ValueError("Disabled factors cannot run")
        runtime = self.settings or RuntimeStore(self.workspace.root).load()
        if not runtime.release_root or not runtime.release_sha256:
            raise ValueError(
                "Configure authorized server release_root and release_sha256 with configure-run; "
                "legacy code-upload mode is no longer supported."
            )
        if not runtime.data_root:
            raise ValueError(
                "Configure the authorized server data_root explicitly with configure-run."
            )
        if runtime.node != "shared" and runtime.node != resources.node:
            raise ValueError(
                f"This runtime is configured for {runtime.node}; select that node or configure-run."
            )
        try:
            syntax = ast.parse(factor.expression, mode="eval")
        except (SyntaxError, ValueError) as exc:
            raise ValueError("Expression is not valid DSL syntax") from exc
        bare = sorted({
            node.id for node in ast.walk(syntax)
            if isinstance(node, ast.Name) and node.id in _DSL_FIELDS
        })
        if bare:
            columns = ", ".join(f'col("{name}")' for name in bare)
            raise ValueError(f"Disk-source expressions require {columns} instead of bare fields.")
        request = make_request(
            asdict(factor) | {"definition_hash": factor.definition_hash},
            asdict(resources) | {"memory": resources.memory},
            start,
            end,
            runtime.data_root,
        )
        request["runtime"] = asdict(runtime)
        if self.config is not None:
            request["connection"] = self._connection()
        validate_request(request)
        folder = self.workspace.runs_path / request["request_id"]
        folder.mkdir(mode=0o700, parents=True, exist_ok=False)
        atomic_json(folder / "request.json", request)
        bundle = folder / "payload.tar.gz"
        with tarfile.open(bundle, "w:gz") as archive:
            payload = (folder / "request.json").read_bytes()
            entry = tarfile.TarInfo("request.json")
            entry.size, entry.mode, entry.mtime = len(payload), 0o600, 0
            archive.addfile(entry, io.BytesIO(payload))
        record = {
            "request_id": request["request_id"],
            "status": "PREPARED",
            "request": request,
            "runtime": asdict(runtime),
            "bundle_path": str(bundle),
            "job_id": None,
        }
        if self.config is not None:
            from fe_cli.job_transport import build_submit_argv

            record["connection"] = self._connection()
            record["manual_command"] = (
                shlex.join(build_submit_argv(self.config, request, runtime))
                + " < "
                + shlex.quote(str(bundle))
            )
        self._save(record)
        return record

    def _connection(self) -> dict:
        if self.config is None:
            return {}
        return {key: getattr(self.config, key) for key in ("host", "user", "port")}

    def _save(self, record: dict) -> None:
        atomic_json(self.workspace.runs_path / record["request_id"] / "run.json", record)

    async def submit(
        self, factor: FactorDefinition, resources: ResourceRequest, start: str, end: str
    ) -> dict:
        from fe_cli.job_transport import SubmissionError

        transport = self._transport()
        record = self.prepare(factor, resources, start, end)
        record["status"] = "SUBMITTING"
        self._save(record)
        try:
            record["job_id"] = await transport.submit(
                Path(record["bundle_path"]), record["request"], RuntimeSettings(**record["runtime"])
            )
        except asyncio.CancelledError:
            record["status"] = "SUBMISSION_UNKNOWN"
            record["error"] = "Submission interrupted; check server history before retrying."
            self._save(record)
            raise
        except (ValueError, OSError) as exc:
            uncertain = isinstance(exc, SubmissionError) and exc.uncertain
            record["status"] = "SUBMISSION_UNKNOWN" if uncertain else "SUBMISSION_FAILED"
            record["error"] = str(exc)
            self._save(record)
            raise ValueError(
                f"{exc} Request {record['request_id']} is {record['status']}; "
                "check /jobs and /history before submitting again."
            ) from exc
        record["status"] = "SUBMITTED"
        self._save(record)
        return record

    def get(self, identifier: str) -> dict:
        if _REQUEST_ID.fullmatch(identifier):
            folder = self.workspace.runs_path / identifier
            if (folder / "commit.json").is_file():
                from fe_cli.delivery import DeliveryStore

                return DeliveryStore(self.workspace).get(identifier)
            try:
                record = json.loads((folder / "run.json").read_bytes())
                validate_request(record["request"])
                if (
                    record["request_id"] != identifier
                    or record["request"]["request_id"] != identifier
                ):
                    raise ValueError("Run record identity differs")
                return record
            except (KeyError, TypeError, RecursionError) as exc:
                raise ValueError("Invalid local run record") from exc
        if re.fullmatch(r"[1-9][0-9]*", identifier):
            matches = [item for item in self.list_runs() if item.get("job_id") == identifier]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise ValueError("Job ID is ambiguous; use a local request ID")
        raise ValueError(f"Unknown local run: {identifier}")

    def list_runs(self) -> list[dict]:
        if not self.workspace.runs_path.exists():
            return []
        return [
            self.get(path.name)
            for path in sorted(
                self.workspace.runs_path.iterdir(), key=lambda p: p.stat().st_mtime_ns, reverse=True
            )
            if _REQUEST_ID.fullmatch(path.name)
            and path.is_dir()
            and ((path / "run.json").is_file() or (path / "commit.json").is_file())
        ]

    async def collect(self, job_id: str, *, request_id: str | None = None) -> dict:
        from fe_cli.archive_io import ExecutionFailed
        from fe_cli.delivery import DeliveryStore

        record = await asyncio.to_thread(self.get, request_id or job_id)
        if record.get("status") == "COMMITTED":
            if record.get("job_id") != job_id:
                raise ValueError("Committed result belongs to another Job ID")
            return record
        if record.get("status") == "EXECUTION_FAILED" and record.get("job_id") == job_id:
            raise ValueError(record["error"])
        if record.get("job_id") not in {None, job_id}:
            raise ValueError("Job ID differs from the saved submission")
        if record.get("connection") and record["connection"] != self._connection():
            raise ValueError("Use the original SSH account and host to collect this run")
        expected = validate_request(record["request"])
        transport = self._transport()
        with tempfile.TemporaryDirectory(prefix=".collect-", dir=self.workspace.runs_path) as tmp:
            try:
                archive = await transport.download(job_id, Path(tmp) / "results.tar.gz")
            except (ValueError, OSError) as exc:
                record["collection_error"] = str(exc)
                self._save(record)
                raise
            commit = asyncio.create_task(
                asyncio.to_thread(
                    DeliveryStore(self.workspace).import_archive,
                    archive,
                    expected_request=expected,
                    expected_job_id=job_id,
                )
            )
            try:
                receipt = await asyncio.shield(commit)
            except asyncio.CancelledError:
                # Keep the downloaded archive alive until an atomic local commit finishes.
                await commit
                raise
            except ExecutionFailed as exc:
                self._record_failure(record, exc.manifest)
                raise
            except (ValueError, OSError) as exc:
                record["collection_error"] = str(exc)
                self._save(record)
                raise
        return receipt

    def _record_failure(self, record: dict, manifest: dict) -> None:
        record["status"] = "EXECUTION_FAILED"
        record["job_id"] = manifest["job_id"]
        if "timing" in manifest:
            record["timing"] = manifest["timing"]
        error = manifest["error"]
        record["error"] = f"Job {record['job_id']} failed: {error['type']}: {error['message']}"
        if "catalog-bound FieldRef" in error["message"]:
            record["error"] += ' Use col("ret") / col("pb_ratio") for disk-source fields.'
        atomic_json(self.workspace.runs_path / record["request_id"] / "failure.json", manifest)
        self._save(record)

    def import_results(self, archive: Path, *, request_id: str | None = None) -> dict:
        from fe_cli.archive_io import ExecutionFailed
        from fe_cli.delivery import DeliveryStore

        record = self.get(request_id) if request_id else None
        if record and record["status"] == "COMMITTED":
            # get() has verified the immutable manifest and receipt together.
            manifest = json.loads(Path(record["manifest_path"]).read_bytes())
            expected = manifest["request"]
        else:
            expected = record["request"] if record else None
        try:
            return DeliveryStore(self.workspace).import_archive(
                archive,
                expected_request=expected,
                expected_job_id=record.get("job_id") if record else None,
            )
        except ExecutionFailed as exc:
            if record and record["status"] != "COMMITTED":
                self._record_failure(record, exc.manifest)
            raise
