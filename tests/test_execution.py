import hashlib
import io
import json
import shutil
import tarfile
from datetime import date
from unittest.mock import AsyncMock

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from fe_cli.catalog import FactorDefinition
from fe_cli.config import ConnectionConfig
from fe_cli.delivery import DeliveryStore
from fe_cli.execution import RunService
from fe_cli.job_transport import SubmissionError
from fe_cli.resources import ResourceRequest
from fe_cli.run_contract import (
    PROTOCOL,
    RESULT_SCHEMA,
    make_request,
    request_digest,
    validate_request,
)
from fe_cli.runtime_settings import RuntimeSettings, RuntimeStore
from fe_cli.workspace import Workspace


def configured_runtime(python="/approved/env/bin/python", **changes):
    return RuntimeSettings(**({
        "remote_python": python,
        "data_root": "/fixture/data",
        "release_root": "/fixture/releases/core",
        "release_sha256": "a" * 64,
    } | changes))


@pytest.fixture
def workspace(tmp_path):
    result = Workspace(tmp_path / "workspace")
    result.initialize()
    result.catalog.add(
        FactorDefinition("mean3", "Mean return plus PB", "ts_mean(col('ret'), 3) + col('pb_ratio')")
    )
    RuntimeStore(result.root).save(configured_runtime())
    return result


def test_prepare_freezes_factor_runtime_and_bundle_without_connection(workspace):
    service = RunService(workspace)
    factor = workspace.catalog.get("mean3")
    record = service.prepare(factor, ResourceRequest(), "2026-01-03", "2026-01-05")
    workspace.catalog.update("mean3", expression="col('ret')")
    RuntimeStore(workspace.root).save(configured_runtime("/another/env/python"))
    restored = service.get(record["request_id"])
    assert restored["request"]["factor"]["expression"] == factor.expression
    assert restored["runtime"]["remote_python"] == "/approved/env/bin/python"
    assert restored["status"] == "PREPARED" and restored["job_id"] is None
    with tarfile.open(record["bundle_path"]) as bundle:
        assert bundle.getnames() == ["request.json"]
        assert json.load(bundle.extractfile("request.json")) == record["request"]
    assert not list(workspace.values_path.iterdir())


@pytest.mark.parametrize(
    "changes", [{"window": {"start": "2026-02-30", "end": "2026-03-01"}}, {"protocol": "future"}]
)
def test_request_invalid_contract(changes):
    request = make_request(
        {
            "factor_id": "f",
            "name": "F",
            "expression": "x",
            "definition_hash": hashlib.sha256(b"x").hexdigest(),
        },
        {"node": "quant-a", "cpus": 6, "memory": "18G"},
        "2026-01-01",
        "2026-01-02",
        "/public/data",
    )
    with pytest.raises(ValueError):
        validate_request(request | changes)


async def test_uncertain_submission_is_preserved_and_never_retried(workspace):
    transport = AsyncMock()
    transport.submit.side_effect = SubmissionError("Connection lost", uncertain=True)
    service = RunService(workspace, ConnectionConfig("cluster.example"), transport=transport)
    with pytest.raises(ValueError, match="SUBMISSION_UNKNOWN"):
        await service.submit(
            workspace.catalog.get("mean3"), ResourceRequest(), "2026-01-03", "2026-01-05"
        )
    transport.submit.assert_awaited_once()
    record = service.list_runs()[0]
    assert record["request"]["connection"] == {
        "host": "cluster.example", "user": "", "port": 22
    }
    assert record["status"] == "SUBMISSION_UNKNOWN" and record["job_id"] is None
    assert "Connection lost" in record["error"]


def test_wrong_node_and_missing_runtime_do_not_allocate_run(workspace):
    service = RunService(workspace)
    factor = workspace.catalog.get("mean3")
    with pytest.raises(ValueError, match="configured for quant-a"):
        service.prepare(factor, ResourceRequest("C", 8), "2026-01-03", "2026-01-05")
    RuntimeStore(workspace.root).path.unlink()
    with pytest.raises(ValueError, match="configure-run"):
        service.prepare(factor, ResourceRequest(), "2026-01-03", "2026-01-05")
    assert not list(workspace.runs_path.iterdir())


def test_runtime_config_rejects_corruption_without_overwriting(workspace):
    store = RuntimeStore(workspace.root)
    store.path.write_text("{broken")
    with pytest.raises(ValueError, match="Invalid runtime"):
        store.save(configured_runtime("/approved/python"))
    assert store.path.read_text() == "{broken"


@pytest.mark.parametrize("changes,expected", [
    ({"release_root": None, "release_sha256": None}, "legacy code-upload mode"),
    ({"data_root": ""}, "data_root explicitly"),
])
async def test_incomplete_server_configuration_never_allocates_or_submits(
    workspace, changes, expected
):
    transport = AsyncMock()
    service = RunService(workspace, transport=transport, settings=configured_runtime(**changes))
    factor = workspace.catalog.get("mean3")
    with pytest.raises(ValueError, match=expected):
        service.prepare(factor, ResourceRequest(), "2026-01-03", "2026-01-05")
    with pytest.raises(ValueError, match=expected):
        await service.submit(factor, ResourceRequest(), "2026-01-03", "2026-01-05")
    transport.submit.assert_not_called()
    assert not list(workspace.runs_path.iterdir())


async def test_collection_failure_keeps_submission_and_reports_no_values(workspace):
    transport = AsyncMock()
    transport.submit.return_value = "123"
    transport.download.side_effect = ValueError("Result is not ready")
    service = RunService(workspace, ConnectionConfig("fixture.invalid"), transport=transport)
    submitted = await service.submit(
        workspace.catalog.get("mean3"), ResourceRequest(), "2026-01-03", "2026-01-05"
    )
    with pytest.raises(ValueError, match="not ready"):
        await service.collect("123", request_id=submitted["request_id"])
    saved = service.get(submitted["request_id"])
    assert saved["status"] == "SUBMITTED" and saved["job_id"] == "123"
    assert saved["collection_error"] == "Result is not ready"
    assert not list(workspace.values_path.iterdir())


@pytest.mark.parametrize("expression", [
    "ts_mean(ret, 3) + pb_ratio",
    'ts_mean(col("ret"), 3) + col("pb_ratio")',
    "ts_mean(ret, window=3) + pb_ratio",
    'ts_mean(col("ret"), window=3) + col("pb_ratio")',
])
async def test_field_spellings_are_submitted_without_rewriting(workspace, expression):
    transport = AsyncMock()
    transport.submit.return_value = "123"
    service = RunService(workspace, transport=transport)
    factor = workspace.catalog.update("mean3", expression=expression)
    csv_before = workspace.catalog_path.read_bytes()
    record = await service.submit(factor, ResourceRequest(), "2026-01-03", "2026-01-05")
    transport.submit.assert_awaited_once()
    sent = transport.submit.call_args.args[1]
    assert sent["factor"]["expression"] == expression
    assert sent["factor"]["definition_hash"] == factor.definition_hash
    with tarfile.open(record["bundle_path"]) as archive:
        assert archive.getnames() == ["request.json"]
        assert json.load(archive.extractfile("request.json")) == sent
    assert service.get(record["request_id"])["request"] == sent
    assert workspace.catalog_path.read_bytes() == csv_before


async def test_invalid_formula_syntax_fails_before_submission_or_allocation(workspace):
    transport = AsyncMock()
    service = RunService(workspace, transport=transport)
    workspace.catalog.update("mean3", expression="ts_mean(ret,")
    with pytest.raises(ValueError, match="not valid DSL syntax"):
        await service.submit(
            workspace.catalog.get("mean3"), ResourceRequest(), "2026-01-03", "2026-01-05"
        )
    transport.submit.assert_not_called()
    assert not list(workspace.runs_path.iterdir())


@pytest.mark.parametrize("via_import", [False, True])
async def test_verified_failure_recovers_unknown_job_without_values(
    workspace, tmp_path, via_import
):
    transport = AsyncMock()
    transport.submit.side_effect = SubmissionError("Missing acknowledgement", uncertain=True)
    service = RunService(workspace, ConnectionConfig("fixture.invalid"), transport=transport)
    with pytest.raises(ValueError, match="SUBMISSION_UNKNOWN"):
        await service.submit(
            workspace.catalog.get("mean3"), ResourceRequest(), "2026-01-03", "2026-01-05"
        )
    record = service.list_runs()[0]
    manifest = {
        "protocol": PROTOCOL, "status": "FAILED", "request": record["request"],
        "request_sha256": request_digest(record["request"]), "job_id": "1308", "files": [],
        "error": {"type": "NativeCompileError", "message": "catalog-bound FieldRef requires col"},
        "timing": {"elapsed_seconds": 0.125},
    }
    archive = tmp_path / "failed.tar.gz"
    content = json.dumps(manifest).encode()
    with tarfile.open(archive, "w:gz") as package:
        entry = tarfile.TarInfo("fe-cli/manifest.json")
        entry.size = len(content)
        package.addfile(entry, io.BytesIO(content))

    async def download(job_id, target):
        shutil.copyfile(archive, target)
        return target

    transport.download.side_effect = download
    with pytest.raises(ValueError, match="NativeCompileError"):
        if via_import:
            service.import_results(archive, request_id=record["request_id"])
        else:
            await service.collect("1308", request_id=record["request_id"])
    recovered = service.get("1308")
    assert recovered["status"] == "EXECUTION_FAILED"
    assert recovered["timing"] == {"elapsed_seconds": 0.125}
    assert recovered["error"] == (
        "Job 1308 failed: NativeCompileError: catalog-bound FieldRef requires col"
    )
    failure_path = workspace.runs_path / record["request_id"] / "failure.json"
    assert json.loads(failure_path.read_bytes()) == manifest
    assert not list(workspace.values_path.glob("*/*/*/result.parquet"))
    with pytest.raises(ValueError, match="Job 1308 failed"):
        await service.collect("1308")
    transport.submit.assert_awaited_once()


@pytest.mark.parametrize("legacy_request", [False, True])
async def test_synthetic_archive_to_local_committed_values(workspace, tmp_path, legacy_request):
    class LocalTransport:
        downloads = 0

        async def submit(self, payload, request, runtime):
            self.payload, self.request = payload, request
            return "12345"  # Explicit fixture identity; this test never contacts a cluster.

        async def download(self, job_id, target):
            self.downloads += 1
            shutil.copyfile(archive, target)
            return target

    transport = LocalTransport()
    service = RunService(workspace, ConnectionConfig("fixture.invalid"), transport=transport)
    submitted = await service.submit(
        workspace.catalog.get("mean3"), ResourceRequest(cpus=1), "2026-01-03", "2026-01-05"
    )
    with tarfile.open(transport.payload) as bundle:
        assert bundle.getnames() == ["request.json"]
    request_path = workspace.runs_path / submitted["request_id"] / "request.json"
    if legacy_request:
        # A historical record has no authorized release; reading/collection must stay supported.
        for runtime in (submitted["runtime"], submitted["request"]["runtime"]):
            runtime.pop("release_root")
            runtime.pop("release_sha256")
        request_path.write_text(json.dumps(submitted["request"]))
        (request_path.parent / "run.json").write_text(json.dumps(submitted))
    historical_request_bytes = request_path.read_bytes()
    assert service.get(submitted["request_id"])["request"] == submitted["request"]
    assert service.list_runs()[0]["request"] == submitted["request"]
    output = tmp_path / "synthetic-result"
    output.mkdir()
    table = pa.table({
        "ts": pa.array([date(2026, 1, 3)], type=pa.date32()),
        "inst": pa.array(["FIXTURE"], type=pa.string()),
        "value": pa.array([2.02], type=pa.float64()),
        "is_valid": pa.array([True], type=pa.bool_()),
        "is_deleted": pa.array([False], type=pa.bool_()),
        "invalid_reason": pa.array([None], type=pa.string()),
    })
    pq.write_table(table, output / "result.parquet")
    content = (output / "result.parquet").read_bytes()
    request = submitted["request"]
    manifest = {
        "protocol": PROTOCOL, "status": "COMPLETE", "request": request,
        "request_sha256": request_digest(request), "job_id": "12345",
        "engine": {"version": "synthetic-fixture", "compiler_sha256": "a" * 64,
                   "package_init_sha256": "b" * 64},
        "metadata": {"frequency": "1d", "result_contract": "polars_factor_result_v1",
                     "execution_identity": {"compiler_sha256": "a" * 64,
                                            "source_identity": "c" * 64}},
        "source": {"version": "native_parquet_source_v1", "manifest_state": "FROZEN",
                   "frequency": "daily", "request_window": request["window"],
                   "load_window": request["window"], "source_identity": "c" * 64,
                   "manifest_digest": "d" * 64, "source_snapshot_id": "fe-cli-daily-" + "e" * 64},
        "source_preparation": {
            "prepared_snapshot_sha256": "e" * 64, "prepared_rows": 1, "expected_output_rows": 1,
            "load_window": ["2026-01-03", "2026-01-05"],
            "raw_inputs": [{"date": "2026-01-03", "inputs": [
                {"path": "/fixture/data/input.parquet", "sha256": "f" * 64},
            ]}],
        },
        "files": [{"path": "result.parquet", "sha256": hashlib.sha256(content).hexdigest(),
                   "bytes": len(content), "rows": 1, "schema": RESULT_SCHEMA}],
    }
    (output / "manifest.json").write_text(json.dumps(manifest))
    archive = tmp_path / "results.tar.gz"
    with tarfile.open(archive, "w:gz") as package:
        for name in ("manifest.json", "result.parquet"):
            package.add(output / name, arcname="fe-cli/" + name)
    workspace.catalog.update("mean3", expression="col('ret')")
    receipt = await service.collect("12345")
    assert receipt["status"] == "COMMITTED" and receipt["rows"] == 1
    values = pq.read_table(receipt["values_path"]).to_pylist()
    assert values[0]["value"] == pytest.approx(2.02)
    assert service.get(submitted["request_id"]) == receipt
    assert await service.collect("12345") == receipt and transport.downloads == 1
    assert DeliveryStore(workspace).get(receipt["request_id"]) == receipt
    assert service.import_results(archive, request_id=receipt["request_id"]) == receipt
    assert request_path.read_bytes() == historical_request_bytes
