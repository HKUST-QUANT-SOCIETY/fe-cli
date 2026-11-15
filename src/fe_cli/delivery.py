"""Validate downloaded runs and publish immutable local Parquet deliveries."""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

from fe_cli.archive_io import (
    MAX_ARCHIVE_BYTES,
    MAX_ARCHIVE_FILES,
    ExecutionFailed,
    file_digest,
    read_json,
    unpack_result,
    validate_failed_manifest,
    validate_manifest,
    validate_parquet,
)
from fe_cli.run_contract import PROTOCOL, canonical_bytes, request_digest
from fe_cli.workspace import Workspace


class DeliveryStore:
    def __init__(
        self,
        workspace: Workspace,
        *,
        max_archive_bytes: int = MAX_ARCHIVE_BYTES,
        max_archive_files: int = MAX_ARCHIVE_FILES,
    ) -> None:
        if type(max_archive_bytes) is not int or not 1 <= max_archive_bytes <= MAX_ARCHIVE_BYTES:
            raise ValueError("Archive byte limit must be between 1 and 4 GiB")
        if type(max_archive_files) is not int or not 1 <= max_archive_files <= MAX_ARCHIVE_FILES:
            raise ValueError("Archive file limit must be between 1 and 10000")
        self.workspace = workspace
        self.max_archive_bytes = max_archive_bytes
        self.max_archive_files = max_archive_files

    def _directory(self, path: Path) -> None:
        relative = path.relative_to(self.workspace.root)
        current = self.workspace.root
        for part in (None, *relative.parts):
            if part is not None:
                current /= part
            if current.is_symlink() or (current.exists() and not current.is_dir()):
                raise ValueError(f"Delivery directory is occupied: {current}")
            if not current.exists():
                current.mkdir(mode=0o700, exist_ok=True, parents=True)
                self._sync_directory(current.parent)

    @staticmethod
    def _regular(path: Path) -> None:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Delivery file is missing or unsafe: {path}")

    @contextmanager
    def _lock(self):
        self._directory(self.workspace.runs_path)
        path = self.workspace.runs_path / ".delivery.lock"
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "r+b") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    @staticmethod
    def _sync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _write_json(self, path: Path, value: dict) -> None:
        if path.exists() or path.is_symlink():
            self._regular(path)
            if read_json(path) != value:
                raise ValueError(f"Existing delivery content conflicts: {path.name}")
            return
        descriptor, temporary = tempfile.mkstemp(prefix=".delivery-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(canonical_bytes(value) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            self._sync_directory(path.parent)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _publish_receipt(self, path: Path, receipt: dict) -> None:
        self._write_json(path, receipt)

    def import_archive(
        self,
        path: Path,
        *,
        expected_request: dict | None = None,
        expected_job_id: str | None = None,
    ) -> dict:
        staging = self.workspace.values_path / ".staging"
        self._directory(staging)
        with tempfile.TemporaryDirectory(prefix="import-", dir=staging) as temporary:
            stage = Path(temporary)
            unpack_result(
                Path(path),
                stage,
                max_bytes=self.max_archive_bytes,
                max_files=self.max_archive_files,
                allow_failure=True,
            )
            manifest = read_json(stage / "manifest.json")
            if manifest.get("status") == "FAILED":
                validate_failed_manifest(
                    manifest, expected_request=expected_request, expected_job_id=expected_job_id
                )
                if (stage / "result.parquet").exists():
                    raise ValueError("Failed execution archive must not contain result.parquet")
                raise ExecutionFailed(manifest)
            if not (stage / "result.parquet").is_file():
                raise ValueError("Archive requires fe-cli/manifest.json and fe-cli/result.parquet")
            request = validate_manifest(
                manifest, expected_request=expected_request, expected_job_id=expected_job_id
            )
            validate_parquet(stage / "result.parquet", manifest)
            factor = request["factor"]
            run = self.workspace.runs_path / request["request_id"]
            values = (
                self.workspace.values_path
                / factor["factor_id"]
                / factor["definition_hash"]
                / request["request_id"]
            )
            result_path = values / "result.parquet"
            manifest_path = run / "manifest.json"
            receipt = {
                "protocol": PROTOCOL,
                "status": "COMMITTED",
                "request_id": request["request_id"],
                "job_id": manifest["job_id"],
                "factor_id": factor["factor_id"],
                "definition_hash": factor["definition_hash"],
                "rows": manifest["files"][0]["rows"],
                "values_path": str(result_path),
                "manifest_path": str(manifest_path),
                "request_sha256": request_digest(request),
                "manifest_sha256": hashlib.sha256(canonical_bytes(manifest)).hexdigest(),
            }
            if "timing" in manifest:
                receipt["timing"] = manifest["timing"]
            with self._lock():
                self._directory(run)
                if (run / "commit.json").exists() or (run / "commit.json").is_symlink():
                    existing = self.get(request["request_id"])
                    if existing != receipt:
                        raise ValueError("Committed run conflicts with this result")
                    return existing
                # Check every existing identity before moving values. Prepared request
                # and submission records remain intact, including their formatting.
                for filename, content in (("request.json", request), ("manifest.json", manifest)):
                    candidate = run / filename
                    if candidate.exists() or candidate.is_symlink():
                        self._regular(candidate)
                        if read_json(candidate) != content:
                            raise ValueError(f"Existing delivery content conflicts: {filename}")
                self._directory(values)
                if result_path.exists() or result_path.is_symlink():
                    self._regular(result_path)
                    record = manifest["files"][0]
                    if file_digest(result_path) != (record["sha256"], record["bytes"]):
                        raise ValueError("Existing result file conflicts with delivery")
                else:
                    with (stage / "result.parquet").open("rb") as handle:
                        os.fsync(handle.fileno())
                    os.replace(stage / "result.parquet", result_path)
                    self._sync_directory(values)
                self._write_json(run / "request.json", request)
                self._write_json(manifest_path, manifest)
                self._publish_receipt(run / "commit.json", receipt)
                return receipt

    def get(self, request_id: str) -> dict:
        if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
            raise ValueError("Invalid request ID")
        run = self.workspace.runs_path / request_id
        # Reads must never initialize a run or reveal a pre-publication result.
        for directory in (self.workspace.runs_path, run):
            if directory.is_symlink() or not directory.is_dir():
                raise ValueError("Run has no committed delivery")
        commit = run / "commit.json"
        self._regular(commit)
        receipt = read_json(commit)
        manifest_path = run / "manifest.json"
        self._regular(manifest_path)
        manifest = read_json(manifest_path)
        request = validate_manifest(manifest)
        factor = request["factor"]
        values = (
            self.workspace.values_path
            / factor["factor_id"]
            / factor["definition_hash"]
            / request_id
            / "result.parquet"
        )
        expected = {
            "protocol": PROTOCOL,
            "status": "COMMITTED",
            "request_id": request_id,
            "job_id": manifest["job_id"],
            "factor_id": factor["factor_id"],
            "definition_hash": factor["definition_hash"],
            "rows": manifest["files"][0]["rows"],
            "values_path": str(values),
            "manifest_path": str(manifest_path),
            "request_sha256": request_digest(request),
            "manifest_sha256": hashlib.sha256(canonical_bytes(manifest)).hexdigest(),
        }
        if "timing" in manifest:
            expected["timing"] = manifest["timing"]
        if request["request_id"] != request_id or receipt != expected:
            raise ValueError("Committed receipt identity differs")
        request_path = run / "request.json"
        self._regular(request_path)
        if read_json(request_path) != request:
            raise ValueError("Committed request differs")
        for parent in values.parents:
            if parent == self.workspace.root:
                break
            if parent.is_symlink():
                raise ValueError("Committed result directory is unsafe")
        self._regular(values)
        record = manifest["files"][0]
        if file_digest(values) != (record["sha256"], record["bytes"]):
            raise ValueError("Committed result file digest or size differs")
        return receipt

    def find_job(self, job_id: str) -> dict | None:
        if not isinstance(job_id, str) or not re.fullmatch(r"[0-9]+", job_id):
            raise ValueError("Invalid job ID")
        if not self.workspace.runs_path.exists():
            return None
        matches = []
        for run in self.workspace.runs_path.iterdir():
            if re.fullmatch(r"[a-f0-9]{32}", run.name) and (run / "commit.json").exists():
                receipt = self.get(run.name)
                if receipt["job_id"] == job_id:
                    matches.append(receipt)
        if len(matches) > 1:
            raise ValueError("Job ID refers to multiple committed requests")
        return matches[0] if matches else None
