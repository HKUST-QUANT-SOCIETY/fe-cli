"""Bounded, untrusted archive ingestion for the daily result protocol."""

from __future__ import annotations

import hashlib
import json
import math
import re
import tarfile
from datetime import date
from pathlib import Path, PurePosixPath

from fe_cli.run_contract import (
    MAX_MANIFEST_BYTES,
    PROTOCOL,
    RESULT_SCHEMA,
    request_digest,
    validate_request,
)

MAX_ARCHIVE_BYTES = 4 * 1024**3
MAX_ARCHIVE_FILES = 10000
INVALID_REASONS = frozenset(
    {
        "SOURCE_NULL",
        "SOURCE_NAN",
        "IMMATURE",
        "INSUFFICIENT_SAMPLES",
        "INSUFFICIENT_SURVIVAL_SUPPORT",
        "DECLARED_NON_FINITE",
        "OPERATOR_NULL",
        "DELETED",
    }
)


def read_json(path: Path) -> dict:
    def unique_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"Duplicate JSON key: {key}")
            value[key] = item
        return value

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=unique_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"Invalid JSON constant: {value}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read result JSON: {path.name}") from exc
    if not isinstance(value, dict):
        raise ValueError("Result JSON must be an object")
    return value


def file_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            size += len(block)
            digest.update(block)
    return digest.hexdigest(), size


def unpack_result(
    archive: Path, stage: Path, *, max_bytes: int, max_files: int, allow_failure: bool = False
) -> None:
    """Extract only the result pair; validate every member, including outer logs."""
    seen = set()
    targets = {}
    total = 0
    count = 0
    try:
        with tarfile.open(archive, mode="r|*") as handle:
            for member in handle:
                count += 1
                if count > max_files:
                    raise ValueError("Archive file count exceeds limit")
                raw = member.name
                parts = PurePosixPath(raw).parts
                if (
                    not raw
                    or "\\" in raw
                    or raw.startswith("/")
                    or ".." in parts
                    or any(ord(char) < 32 for char in raw)
                ):
                    raise ValueError("Unsafe archive path")
                name = str(PurePosixPath(raw))
                if name in seen:
                    raise ValueError("Duplicate archive path")
                seen.add(name)
                if not (member.isfile() or member.isdir()):
                    raise ValueError("Archive must contain only ordinary files and directories")
                if member.size < 0:
                    raise ValueError("Invalid archive member size")
                total += member.size
                if total > max_bytes:
                    raise ValueError("Archive size exceeds limit")
                if not member.isfile() or len(parts) < 2 or parts[-2] != "fe-cli":
                    continue
                filename = parts[-1]
                if filename not in {"manifest.json", "result.parquet"}:
                    continue
                if filename in targets:
                    raise ValueError(f"Multiple {filename} results in archive")
                targets[filename] = PurePosixPath(name).parent
                if filename == "manifest.json" and member.size > MAX_MANIFEST_BYTES:
                    raise ValueError(
                        f"Result manifest size {member.size} bytes exceeds limit "
                        f"{MAX_MANIFEST_BYTES} bytes"
                    )
                source = handle.extractfile(member)
                if source is None:
                    raise ValueError("Unreadable archive member")
                remaining = member.size
                with source, (stage / filename).open("xb") as destination:
                    while remaining:
                        block = source.read(min(remaining, 1024 * 1024))
                        if not block:
                            raise ValueError("Truncated archive member")
                        destination.write(block)
                        remaining -= len(block)
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ValueError(f"Cannot read result archive: {exc}") from exc
    if allow_failure and set(targets) == {"manifest.json"}:
        return
    if set(targets) != {"manifest.json", "result.parquet"}:
        raise ValueError("Archive requires fe-cli/manifest.json and fe-cli/result.parquet")
    if targets["manifest.json"] != targets["result.parquet"]:
        raise ValueError("Manifest and result must share the same fe-cli directory")


def _validate_provenance(manifest: dict, request: dict, rows: int) -> None:
    """Check engine and data provenance against the public result contract."""

    def text(value, label):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Missing or invalid manifest provenance: {label}")

    def digest(value, label):
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
            raise ValueError(f"Missing or invalid manifest provenance digest: {label}")

    def window(value, label):
        if not isinstance(value, dict):
            raise ValueError(f"Invalid manifest {label}")
        for key in ("start", "end"):
            raw = value[key]
            if not isinstance(raw, str) or date.fromisoformat(raw).isoformat() != raw:
                raise ValueError(f"Invalid manifest {label} date")
        if value["start"] > value["end"]:
            raise ValueError(f"Reversed manifest {label}")
        return {key: value[key] for key in ("start", "end")}

    engine, source, metadata = (manifest[key] for key in ("engine", "source", "metadata"))
    text(engine["version"], "engine.version")
    for key in ("compiler_sha256", "package_init_sha256"):
        digest(engine[key], f"engine.{key}")
    runtime = request.get("runtime", {})
    if (
        isinstance(runtime, dict) and runtime.get("release_root")
        and "release_sha256" in runtime and runtime["release_sha256"] is None
    ):
        # Current selects a release at job start; the result must identify that exact release.
        digest(engine.get("release_sha256"), "engine.release_sha256")
        commit = engine.get("engine_commit")
        if not isinstance(commit, str) or not re.fullmatch(r"[a-f0-9]{40}", commit):
            raise ValueError("Missing or invalid manifest provenance: engine.engine_commit")
        wheel = engine.get("core_wheel")
        if not isinstance(wheel, dict) or set(wheel) != {"path", "sha256", "bytes"}:
            raise ValueError("Missing or invalid manifest provenance: engine.core_wheel")
        name = wheel["path"]
        if (
            not isinstance(name, str) or not name.endswith(".whl")
            or PurePosixPath(name).name != name or "\\" in name
            or any(ord(c) < 32 for c in name)
            or type(wheel["bytes"]) is not int or wheel["bytes"] <= 0
        ):
            raise ValueError("Invalid manifest provenance: engine.core_wheel")
        digest(wheel["sha256"], "engine.core_wheel.sha256")
    if (
        source["version"] != "native_parquet_source_v1"
        or source["manifest_state"] != "FROZEN"
        or source["frequency"] != "daily"
    ):
        raise ValueError("Unsupported source provenance contract")
    for key in ("source_identity", "manifest_digest"):
        digest(source[key], f"source.{key}")
    source_window = window(source["request_window"], "source request_window")
    load = window(source["load_window"], "source load_window")
    if source_window != request["window"]:
        raise ValueError("Source request window differs from run request")
    if load["end"] > source_window["end"]:
        raise ValueError("Source load window extends beyond the requested end")
    if load["start"] > source_window["start"] or load["end"] < source_window["end"]:
        raise ValueError("Source load window does not contain the request window")
    prepared = manifest["source_preparation"]
    if not isinstance(prepared, dict):
        raise ValueError("Source preparation provenance must be an object")
    digest(prepared["prepared_snapshot_sha256"], "source_preparation.prepared_snapshot_sha256")
    if source["source_snapshot_id"] != "fe-cli-daily-" + prepared["prepared_snapshot_sha256"]:
        raise ValueError("Source snapshot identity differs from preparation")
    for key in ("prepared_rows", "expected_output_rows"):
        if type(prepared[key]) is not int or prepared[key] < 0:
            raise ValueError("Source preparation rows must be non-negative integers")
    if (
        prepared["expected_output_rows"] != rows
        or prepared["prepared_rows"] < prepared["expected_output_rows"]
    ):
        raise ValueError("Source preparation expected output rows differ from result rows")
    if prepared["load_window"] != [load["start"], load["end"]]:
        raise ValueError("Source preparation load window differs from source")
    raw_inputs = prepared["raw_inputs"]
    if not isinstance(raw_inputs, list) or not raw_inputs:
        raise ValueError("Missing raw input provenance")
    observed = []
    for record in raw_inputs:
        day = record["date"]
        if (
            not isinstance(day, str)
            or date.fromisoformat(day).isoformat() != day
            or not load["start"] <= day <= load["end"]
        ):
            raise ValueError("Raw input date differs from source load window")
        if observed and day <= observed[-1]:
            raise ValueError("Raw input observed dates must be sorted and unique")
        observed.append(day)
        inputs = record["inputs"]
        if not isinstance(inputs, list) or not inputs:
            raise ValueError("Missing raw input identities")
        for item in inputs:
            text(item["path"], "raw input path")
            digest(item["sha256"], "raw input sha256")
    if "observed_window" in prepared and prepared["observed_window"] != [observed[0], observed[-1]]:
        raise ValueError("Raw input observed date range differs from preparation observed window")
    if not any(source_window["start"] <= day <= source_window["end"] for day in observed):
        raise ValueError("No raw input observed session in the requested window")
    if metadata["result_contract"] != "polars_factor_result_v1" or metadata["frequency"] != "1d":
        raise ValueError("Unsupported result metadata contract")
    execution = metadata["execution_identity"]
    if (
        not isinstance(execution, dict)
        or execution["compiler_sha256"] != engine["compiler_sha256"]
        or execution["source_identity"] != source["source_identity"]
    ):
        raise ValueError("Result execution identity differs from engine/source provenance")


class ExecutionFailed(ValueError):
    """A validated run identity reported failure instead of a result."""

    def __init__(self, manifest: dict) -> None:
        self.manifest = manifest
        error = manifest["error"]
        super().__init__(f"Execution failed ({error['type']}): {error['message']}")


def _validate_identity(
    manifest: dict, expected_request: dict | None, expected_job_id: str | None
) -> dict:
    request = validate_request(manifest["request"])
    if manifest["request_sha256"] != request_digest(request):
        raise ValueError("Manifest request digest differs")
    if expected_request is not None and request != validate_request(expected_request):
        raise ValueError("Result request differs from the prepared request")
    job_id = manifest["job_id"]
    if job_id is not None and (not isinstance(job_id, str) or not re.fullmatch(r"[0-9]+", job_id)):
        raise ValueError("Invalid result job ID")
    if expected_job_id is not None and job_id != expected_job_id:
        raise ValueError("Result job ID differs from the submitted job")
    if "timing" in manifest:
        timing = manifest["timing"]
        elapsed = timing.get("elapsed_seconds") if isinstance(timing, dict) else None
        if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
            raise ValueError("Result elapsed_seconds must be a finite non-negative number")
    return request


def validate_failed_manifest(
    manifest: dict, *, expected_request: dict | None = None, expected_job_id: str | None = None
) -> dict:
    try:
        if manifest["protocol"] != PROTOCOL or manifest["status"] != "FAILED":
            raise ValueError("Unsupported failed execution manifest")
        request = _validate_identity(manifest, expected_request, expected_job_id)
        if manifest["files"] != []:
            raise ValueError("Failed execution manifest must not declare result files")
        error = manifest["error"]
        if not isinstance(error, dict) or any(
            not isinstance(error[key], str) or not error[key].strip() for key in ("type", "message")
        ):
            raise ValueError("Failed execution manifest requires an error type and message")
    except (KeyError, TypeError, AttributeError, OverflowError) as exc:
        raise ValueError(f"Invalid failed execution manifest: {exc}") from exc
    return request


def validate_manifest(
    manifest: dict, *, expected_request: dict | None = None, expected_job_id: str | None = None
) -> dict:
    try:
        if manifest["protocol"] != PROTOCOL or manifest["status"] != "COMPLETE":
            raise ValueError("Result manifest is not COMPLETE in the supported protocol")
        request = _validate_identity(manifest, expected_request, expected_job_id)
        if any(not isinstance(manifest[key], dict) for key in ("engine", "metadata", "source")):
            raise ValueError("Manifest engine, metadata and source must be objects")
        files = manifest["files"]
        if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict):
            raise ValueError("Manifest must describe exactly one result file")
        record = files[0]
        if record["path"] != "result.parquet" or record["schema"] != RESULT_SCHEMA:
            raise ValueError("Manifest result path or schema differs")
        if not isinstance(record["sha256"], str) or not re.fullmatch(
            r"[a-f0-9]{64}", record["sha256"]
        ):
            raise ValueError("Invalid result digest")
        if any(type(record[key]) is not int or record[key] < 0 for key in ("bytes", "rows")):
            raise ValueError("Result bytes and rows must be non-negative integers")
        _validate_provenance(manifest, request, record["rows"])
    except (KeyError, TypeError, AttributeError, OverflowError) as exc:
        raise ValueError(f"Invalid result manifest: {exc}") from exc
    return request


def validate_parquet(path: Path, manifest: dict) -> None:
    # This intentionally does not install or import Factor Engine on the client.
    import pyarrow as pa
    import pyarrow.parquet as pq

    record = manifest["files"][0]
    if file_digest(path) != (record["sha256"], record["bytes"]):
        raise ValueError("Result file digest or byte count differs")
    try:
        parquet = pq.ParquetFile(path)
        schema = parquet.schema_arrow
        types = (
            pa.types.is_date32,
            lambda value: pa.types.is_string(value) or pa.types.is_large_string(value),
            pa.types.is_float64,
            pa.types.is_boolean,
            pa.types.is_boolean,
            lambda value: pa.types.is_string(value) or pa.types.is_large_string(value),
        )
        if schema.names != list(RESULT_SCHEMA) or not all(
            check(field.type) for check, field in zip(types, schema, strict=True)
        ):
            raise ValueError("Parquet result schema differs from daily result contract")
        window = manifest["request"]["window"]
        start, end = date.fromisoformat(window["start"]), date.fromisoformat(window["end"])
        previous = None
        rows = 0
        for batch in parquet.iter_batches(batch_size=65536):
            columns = [column.to_pylist() for column in batch.columns]
            for ts, inst, value, valid, deleted, reason in zip(*columns, strict=True):
                if ts is None or inst is None or valid is None or deleted is None or inst == "":
                    raise ValueError("Result has null keys/flags or an empty instrument")
                if not start <= ts <= end:
                    raise ValueError("Result date is outside the requested window")
                key = (ts, inst)
                if previous is not None and key <= previous:
                    raise ValueError("Result keys must be globally sorted and unique")
                previous = key
                if valid:
                    if value is None or not math.isfinite(value) or deleted or reason is not None:
                        raise ValueError("Valid result row violates value/flag/reason contract")
                elif (
                    value is not None
                    or reason not in INVALID_REASONS
                    or deleted != (reason == "DELETED")
                ):
                    raise ValueError("Invalid result row violates value/flag/reason contract")
                rows += 1
        if rows != record["rows"] or rows != parquet.metadata.num_rows:
            raise ValueError("Result row count differs")
    except (pa.ArrowException, OSError, OverflowError) as exc:
        raise ValueError(f"Cannot validate result Parquet: {exc}") from exc
