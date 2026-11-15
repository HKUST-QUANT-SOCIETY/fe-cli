import hashlib
import io
import json
import tarfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from fe_cli.archive_io import MAX_ARCHIVE_BYTES, MAX_ARCHIVE_FILES, unpack_result
from fe_cli.delivery import DeliveryStore, ExecutionFailed
from fe_cli.run_contract import PROTOCOL, RESULT_SCHEMA, make_request, request_digest
from fe_cli.workspace import Workspace


@pytest.fixture
def run_request():
    expression = "$close"
    return make_request(
        {
            "factor_id": "close",
            "name": "Close",
            "expression": expression,
            "definition_hash": hashlib.sha256(expression.encode()).hexdigest(),
        },
        {"node": "quant-a", "cpus": 6, "memory": "18G"},
        "2026-01-01",
        "2026-01-03",
        "/approved/daily",
    )


@pytest.fixture
def store(tmp_path):
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    return DeliveryStore(workspace)


def result_table(rows=None):
    schema = pa.schema(
        [
            ("ts", pa.date32()),
            ("inst", pa.large_string()),
            ("value", pa.float64()),
            ("is_valid", pa.bool_()),
            ("is_deleted", pa.bool_()),
            ("invalid_reason", pa.string()),
        ]
    )
    if rows is None:
        rows = [
            (date(2026, 1, 1), "A", 1.123456789012345, True, False, None),
            (date(2026, 1, 2), "A", None, False, False, "IMMATURE"),
            (date(2026, 1, 3), "B", None, False, True, "DELETED"),
        ]
    return pa.Table.from_pylist(
        [dict(zip(RESULT_SCHEMA, row, strict=True)) for row in rows], schema
    )


def archive(
    tmp_path,
    run_request,
    *,
    table=None,
    mutate=None,
    prefix="job/output/results/fe-cli",
    extra=(),
    raw_manifest=None,
    parquet_bytes=None,
    compression="gz",
):
    buffer = io.BytesIO()
    pq.write_table(table if table is not None else result_table(), buffer, row_group_size=1)
    content = buffer.getvalue() if parquet_bytes is None else parquet_bytes
    manifest = {
        "protocol": PROTOCOL,
        "status": "COMPLETE",
        "request": run_request,
        "request_sha256": request_digest(run_request),
        "job_id": "123",
        "engine": {
            "version": "verified-build",
            "compiler_sha256": "a" * 64,
            "package_init_sha256": "b" * 64,
        },
        "metadata": {
            "frequency": "1d",
            "result_contract": "polars_factor_result_v1",
            "execution_identity": {"compiler_sha256": "a" * 64, "source_identity": "c" * 64},
        },
        "source": {
            "version": "native_parquet_source_v1",
            "manifest_state": "FROZEN",
            "frequency": "daily",
            "request_window": dict(run_request["window"]),
            "load_window": dict(run_request["window"]),
            "source_identity": "c" * 64,
            "manifest_digest": "d" * 64,
            "source_snapshot_id": "fe-cli-daily-" + "e" * 64,
        },
        "source_preparation": {
            "prepared_snapshot_sha256": "e" * 64,
            "prepared_rows": (table if table is not None else result_table()).num_rows,
            "expected_output_rows": (table if table is not None else result_table()).num_rows,
            "load_window": [run_request["window"][key] for key in ("start", "end")],
            "observed_window": [run_request["window"][key] for key in ("start", "end")],
            "raw_inputs": [
                {
                    "date": day,
                    "inputs": [
                        {
                            "path": "/approved/daily/bars/2026-01-01.parquet",
                            "sha256": "f" * 64,
                        }
                    ],
                }
                for day in sorted(set(run_request["window"].values()))
            ],
        },
        "files": [
            {
                "path": "result.parquet",
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
                "rows": (table if table is not None else result_table()).num_rows,
                "schema": RESULT_SCHEMA,
            }
        ],
    }
    if mutate:
        mutate(manifest)
    destination = tmp_path / f"archive-{len(list(tmp_path.glob('archive-*')))}.tar"
    with tarfile.open(destination, f"w:{compression}") as handle:
        for name, data in [
            ("job/logs/stdout", b"ordinary outer log"),
            (f"{prefix}/manifest.json", raw_manifest or json.dumps(manifest).encode()),
            (f"{prefix}/result.parquet", content),
            *extra,
        ]:
            if isinstance(name, tarfile.TarInfo):
                handle.addfile(name, io.BytesIO(data))
            else:
                member = tarfile.TarInfo(name)
                member.size = len(data)
                handle.addfile(member, io.BytesIO(data))
    return destination, manifest


def test_real_parquet_roundtrip_and_prepared_files_preserved(tmp_path, run_request, store):
    run = store.workspace.runs_path / run_request["request_id"]
    run.mkdir()
    prepared = json.dumps(run_request, indent=3).encode()
    (run / "request.json").write_bytes(prepared)
    (run / "submission.json").write_bytes(b'{"job_id":"123"}\n')
    source, manifest = archive(tmp_path, run_request)
    receipt = store.import_archive(source, expected_request=run_request, expected_job_id="123")
    assert receipt["status"] == "COMMITTED"
    assert receipt["rows"] == 3
    assert receipt == store.get(run_request["request_id"]) == store.find_job("123")
    assert store.find_job("999") is None
    assert pq.read_table(receipt["values_path"]).to_pylist() == result_table().to_pylist()
    assert json.loads(Path(receipt["manifest_path"]).read_text()) == manifest
    assert (run / "request.json").read_bytes() == prepared
    assert (run / "submission.json").read_bytes() == b'{"job_id":"123"}\n'
    assert list((store.workspace.values_path / ".staging").iterdir()) == []


def test_timing_survives_commit_reload_and_reimport(tmp_path, run_request, store):
    source, _ = archive(
        tmp_path, run_request, mutate=lambda m: m.update(timing={"elapsed_seconds": 12.345})
    )
    receipt = store.import_archive(source)
    assert receipt["timing"] == {"elapsed_seconds": 12.345}
    assert DeliveryStore(store.workspace).get(run_request["request_id"]) == receipt
    assert store.import_archive(source) == receipt


@pytest.mark.parametrize("elapsed", [-1, True, "1.5", None, float("nan"), float("inf")])
@pytest.mark.parametrize("failed", [False, True])
def test_invalid_timing_never_commits(tmp_path, run_request, store, elapsed, failed):
    make_archive = failed_archive if failed else archive
    source, _ = make_archive(
        tmp_path, run_request, mutate=lambda m: m.update(timing={"elapsed_seconds": elapsed})
    )
    with pytest.raises(ValueError, match="elapsed_seconds|Invalid JSON constant"):
        store.import_archive(source)
    assert not (store.workspace.runs_path / run_request["request_id"] / "commit.json").exists()


@pytest.mark.parametrize("rows", [[], [(date(2026, 1, 1), "A", None, False, False, "SOURCE_NULL")]])
def test_empty_and_all_invalid_are_legitimate(tmp_path, run_request, store, rows):
    source, _ = archive(
        tmp_path, run_request, table=result_table(rows), prefix="fe-cli", compression=""
    )
    assert store.import_archive(source)["rows"] == len(rows)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.update(status="FAILED"),
        lambda m: m.update(request_sha256="0" * 64),
        lambda m: m.update(job_id="not-a-job"),
        lambda m: m.update(engine="unknown"),
        lambda m: m["files"][0].update(sha256="0" * 64),
        lambda m: m["files"][0].update(bytes=0),
        lambda m: m["files"][0].update(rows=999),
        lambda m: m["files"][0].update(rows=True),
        lambda m: m["files"][0].update(path="../../outside"),
        lambda m: m["files"].append(dict(m["files"][0])),
        lambda m: m["files"][0].update(schema={}),
    ],
)
def test_manifest_or_content_corruption_never_commits(tmp_path, run_request, store, mutate):
    source, _ = archive(tmp_path, run_request, mutate=mutate)
    with pytest.raises(ValueError):
        store.import_archive(source)
    assert not list(store.workspace.runs_path.glob("*/commit.json"))
    assert list((store.workspace.values_path / ".staging").iterdir()) == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.update(engine={}),
        lambda m: m.update(source={}),
        lambda m: m.update(metadata={}),
        lambda m: m.pop("source_preparation"),
        lambda m: m["engine"].update(version=""),
        lambda m: m["engine"].update(compiler_sha256="unknown"),
        lambda m: m["engine"].pop("package_init_sha256"),
        lambda m: m["source"].update(source_identity=""),
        lambda m: m["source"].update(manifest_state="PREPARING"),
        lambda m: m["source"].update(source_snapshot_id="other-snapshot"),
        lambda m: m["source"]["request_window"].update(start="2025-12-31"),
        lambda m: m["source"]["load_window"].update(end="2026-01-02"),
        lambda m: m["source_preparation"].update(load_window=["2025-12-31", "2026-01-03"]),
        lambda m: m["source_preparation"].update(prepared_rows=0),
        lambda m: m["source_preparation"].update(expected_output_rows=True),
        lambda m: m["source_preparation"].update(raw_inputs=[]),
        lambda m: m["source_preparation"]["raw_inputs"][0]["inputs"][0].update(sha256=""),
        lambda m: m["metadata"].update(frequency="1m"),
        lambda m: m["metadata"]["execution_identity"].update(compiler_sha256="0" * 64),
        lambda m: m["metadata"]["execution_identity"].update(source_identity="0" * 64),
    ],
)
def test_missing_or_inconsistent_worker_provenance_never_commits(
    tmp_path,
    run_request,
    store,
    mutate,
):
    source, _ = archive(tmp_path, run_request, mutate=mutate)
    with pytest.raises(ValueError):
        store.import_archive(source)
    assert not list(store.workspace.runs_path.glob("*/commit.json"))
    assert list((store.workspace.values_path / ".staging").iterdir()) == []


def test_empty_output_cannot_hide_expected_source_rows(tmp_path, run_request, store):
    def expect_nonempty(manifest):
        manifest["source_preparation"].update(prepared_rows=100, expected_output_rows=100)

    source, _ = archive(tmp_path, run_request, table=result_table([]), mutate=expect_nonempty)
    with pytest.raises(ValueError, match="expected output rows differ"):
        store.import_archive(source)
    assert not list(store.workspace.runs_path.glob("*/commit.json"))


def test_consistent_warmup_source_window_is_supported(tmp_path, run_request, store):
    def warmup(manifest):
        manifest["source"]["load_window"]["start"] = "2025-12-30"
        manifest["source_preparation"]["load_window"][0] = "2025-12-30"
        manifest["source_preparation"]["observed_window"][0] = "2025-12-30"
        manifest["source_preparation"]["prepared_rows"] += 10
        record = dict(manifest["source_preparation"]["raw_inputs"][0], date="2025-12-30")
        manifest["source_preparation"]["raw_inputs"].insert(0, record)

    source, _ = archive(tmp_path, run_request, mutate=warmup)
    assert store.import_archive(source)["rows"] == 3


@pytest.mark.parametrize(
    "start,end",
    [
        ("2026-01-01", "2026-01-02"),
        ("2026-01-02", "2026-01-04"),
        ("2026-01-01", "2026-01-04"),
    ],
)
def test_non_session_request_endpoints_do_not_require_source_rows(
    tmp_path,
    run_request,
    store,
    start,
    end,
):
    run_request["window"] = {"start": start, "end": end}

    def observed_session(manifest):
        prepared = manifest["source_preparation"]
        prepared["observed_window"] = ["2026-01-02", "2026-01-02"]
        prepared["raw_inputs"] = [dict(prepared["raw_inputs"][0], date="2026-01-02")]

    table = result_table([(date(2026, 1, 2), "A", 1.0, True, False, None)])
    source, _ = archive(tmp_path, run_request, table=table, mutate=observed_session)
    assert store.import_archive(source)["rows"] == 1


def test_observed_window_is_optional_for_existing_provenance(tmp_path, run_request, store):
    source, _ = archive(
        tmp_path,
        run_request,
        mutate=lambda manifest: manifest["source_preparation"].pop("observed_window"),
    )
    assert store.import_archive(source)["rows"] == 3


@pytest.mark.parametrize("kind", ["future", "disjoint", "observed_range", "no_requested_session"])
def test_source_session_windows_cannot_hide_invalid_provenance(
    tmp_path,
    run_request,
    store,
    kind,
):
    def corrupt(manifest):
        prepared = manifest["source_preparation"]
        if kind == "future":
            manifest["source"]["load_window"]["end"] = "2026-01-04"
            prepared["load_window"][1] = "2026-01-04"
        elif kind == "disjoint":
            manifest["source"]["load_window"] = {"start": "2025-12-29", "end": "2025-12-30"}
            prepared["load_window"] = ["2025-12-29", "2025-12-30"]
        elif kind == "observed_range":
            prepared["observed_window"] = ["2026-01-02", "2026-01-03"]
        else:
            manifest["source"]["load_window"]["start"] = "2025-12-30"
            prepared["load_window"][0] = "2025-12-30"
            prepared["raw_inputs"] = [dict(prepared["raw_inputs"][0], date="2025-12-30")]

    source, _ = archive(tmp_path, run_request, mutate=corrupt)
    with pytest.raises(ValueError):
        store.import_archive(source)
    assert not list(store.workspace.runs_path.glob("*/commit.json"))


@pytest.mark.parametrize(
    "row",
    [
        (date(2025, 12, 31), "A", 1.0, True, False, None),
        (None, "A", 1.0, True, False, None),
        (date(2026, 1, 1), None, 1.0, True, False, None),
        (date(2026, 1, 1), "", 1.0, True, False, None),
        (date(2026, 1, 1), "A", 1.0, None, False, None),
        (date(2026, 1, 1), "A", 1.0, True, None, None),
        (date(2026, 1, 1), "A", float("nan"), True, False, None),
        (date(2026, 1, 1), "A", float("inf"), True, False, None),
        (date(2026, 1, 1), "A", None, True, False, None),
        (date(2026, 1, 1), "A", 1.0, False, False, "SOURCE_NULL"),
        (date(2026, 1, 1), "A", None, False, False, "UNKNOWN"),
        (date(2026, 1, 1), "A", None, False, False, None),
        (date(2026, 1, 1), "A", 1.0, True, False, "SOURCE_NULL"),
        (date(2026, 1, 1), "A", None, False, False, "DELETED"),
        (date(2026, 1, 1), "A", None, False, True, "SOURCE_NULL"),
    ],
)
def test_invalid_rows_rejected(tmp_path, run_request, store, row):
    source, _ = archive(tmp_path, run_request, table=result_table([row]))
    with pytest.raises(ValueError):
        store.import_archive(source)


@pytest.mark.parametrize("kind", ["duplicate", "unsorted", "schema", "column_order"])
def test_actual_key_and_schema_validation(tmp_path, run_request, store, kind):
    table = result_table()
    if kind == "duplicate":
        table = pa.concat_tables([table.slice(0, 1), table.slice(0, 1)])
    elif kind == "unsorted":
        table = table.take(pa.array([2, 0, 1]))
    elif kind == "schema":
        table = table.set_column(2, "value", table["value"].cast(pa.float32()))
    else:
        table = table.select(list(reversed(table.column_names)))
    source, _ = archive(tmp_path, run_request, table=table)
    with pytest.raises(ValueError):
        store.import_archive(source)


@pytest.mark.parametrize("name", ["../escape", "/absolute", "logs/../../escape", "logs\\escape"])
def test_unsafe_outer_archive_paths_rejected(tmp_path, run_request, store, name):
    source, _ = archive(tmp_path, run_request, extra=[(name, b"bad")])
    with pytest.raises(ValueError, match="Unsafe archive path"):
        store.import_archive(source)
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize(
    "kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE]
)
def test_special_archive_members_rejected(tmp_path, run_request, store, kind):
    member = tarfile.TarInfo("logs/special")
    member.type = kind
    member.linkname = "/outside"
    source, _ = archive(tmp_path, run_request, extra=[(member, b"")])
    with pytest.raises(ValueError, match="ordinary files"):
        store.import_archive(source)


def test_duplicate_members_and_json_keys_rejected(tmp_path, run_request, store):
    source, _ = archive(tmp_path, run_request, extra=[("./job/logs/stdout", b"duplicate")])
    with pytest.raises(ValueError, match="Duplicate archive"):
        store.import_archive(source)
    source, _ = archive(
        tmp_path, run_request, raw_manifest=b'{"status":"COMPLETE","status":"COMPLETE"}'
    )
    with pytest.raises(ValueError, match="Duplicate JSON key"):
        store.import_archive(source)


def test_archive_caps_and_unique_pair(tmp_path, run_request, store):
    source, _ = archive(tmp_path, run_request)
    for options in ({"max_archive_bytes": 5}, {"max_archive_files": 1}):
        with pytest.raises(ValueError, match="exceeds limit"):
            DeliveryStore(store.workspace, **options).import_archive(source)
    source, _ = archive(tmp_path, run_request, extra=[("other/fe-cli/result.parquet", b"extra")])
    with pytest.raises(ValueError, match="Multiple"):
        store.import_archive(source)


def test_large_real_shaped_manifest_above_eight_mib_can_commit(tmp_path, run_request, store):
    def detailed_provenance(manifest):
        manifest["metadata"]["source_audit_padding"] = "x" * (9 * 1024**2)

    source, manifest = archive(tmp_path, run_request, mutate=detailed_provenance)
    with tarfile.open(source) as package:
        assert package.getmember("job/output/results/fe-cli/manifest.json").size > 8 * 1024**2
    receipt = store.import_archive(source, expected_request=run_request, expected_job_id="123")
    assert receipt["rows"] == 3
    assert store.get(run_request["request_id"]) == receipt
    assert json.loads(Path(receipt["manifest_path"]).read_text()) == manifest


def test_manifest_size_limit_reports_actual_size_and_configured_limit(
    tmp_path,
    run_request,
    store,
    monkeypatch,
):
    import fe_cli.archive_io as archive_io
    from fe_cli.run_contract import MAX_MANIFEST_BYTES

    assert MAX_MANIFEST_BYTES == 64 * 1024**2
    source, _ = archive(tmp_path, run_request)
    with tarfile.open(source) as package:
        size = package.getmember("job/output/results/fe-cli/manifest.json").size
    limit = size - 1
    monkeypatch.setattr(archive_io, "MAX_MANIFEST_BYTES", limit)
    with pytest.raises(ValueError, match=rf"size {size} bytes exceeds limit {limit} bytes"):
        store.import_archive(source)
    assert not list(store.workspace.runs_path.glob("*/commit.json"))
    assert list((store.workspace.values_path / ".staging").iterdir()) == []


def test_larger_manifest_limit_does_not_relax_total_archive_size_limit(
    tmp_path,
    run_request,
    store,
):
    source, _ = archive(tmp_path, run_request)
    with tarfile.open(source) as package:
        member = package.getmember("job/output/results/fe-cli/manifest.json")
        limit = member.size
    capped_store = DeliveryStore(store.workspace, max_archive_bytes=limit)
    with pytest.raises(ValueError, match="Archive size exceeds limit"):
        capped_store.import_archive(source)
    assert not list(store.workspace.runs_path.glob("*/commit.json"))


def test_expected_identity_and_existing_prepared_request(tmp_path, run_request, store):
    source, _ = archive(tmp_path, run_request)
    with pytest.raises(ValueError, match="job ID differs"):
        store.import_archive(source, expected_job_id="999")
    other = dict(run_request, created_at="other")
    with pytest.raises(ValueError, match="prepared request"):
        store.import_archive(source, expected_request=other)
    run = store.workspace.runs_path / run_request["request_id"]
    run.mkdir()
    (run / "request.json").write_text(json.dumps(other))
    with pytest.raises(ValueError, match="conflicts"):
        store.import_archive(source)
    assert json.loads((run / "request.json").read_text()) == other


def test_idempotence_conflicts_and_missing_committed_file(tmp_path, run_request, store):
    source, _ = archive(tmp_path, run_request)
    first = store.import_archive(source)
    assert store.import_archive(source) == first
    conflicting, _ = archive(tmp_path, run_request, table=result_table([]))
    with pytest.raises(ValueError, match="conflicts"):
        store.import_archive(conflicting)
    assert pq.read_table(first["values_path"]).num_rows == 3
    Path(first["values_path"]).unlink()
    with pytest.raises(ValueError, match="missing"):
        store.get(run_request["request_id"])


def test_crash_after_value_publication_recovers_only_identical_content(
    tmp_path, run_request, store, monkeypatch
):
    source, _ = archive(tmp_path, run_request)
    publish = store._publish_receipt

    def crash(*args):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(store, "_publish_receipt", crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        store.import_archive(source)
    with pytest.raises(ValueError):
        store.get(run_request["request_id"])
    monkeypatch.setattr(store, "_publish_receipt", publish)
    conflicting, _ = archive(tmp_path, run_request, table=result_table([]))
    with pytest.raises(ValueError, match="conflicts"):
        store.import_archive(conflicting)
    receipt = store.import_archive(source)
    assert store.get(run_request["request_id"]) == receipt


def test_concurrent_retries_share_one_commit(tmp_path, run_request, store):
    source, _ = archive(tmp_path, run_request)
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda _: store.import_archive(source), range(2)))
    assert receipts[0] == receipts[1] == store.get(run_request["request_id"])


def test_tampered_commit_and_symlink_values_are_rejected(tmp_path, run_request, store):
    source, _ = archive(tmp_path, run_request)
    receipt = store.import_archive(source)
    value_path = Path(receipt["values_path"])
    content = value_path.read_bytes()
    value_path.write_bytes(content + b"tampered")
    with pytest.raises(ValueError, match="digest or size"):
        store.get(run_request["request_id"])
    value_path.unlink()
    outside = tmp_path / "outside.parquet"
    outside.write_bytes(content)
    value_path.symlink_to(outside)
    with pytest.raises(ValueError, match="unsafe"):
        store.get(run_request["request_id"])


def test_sorting_is_checked_across_batch_boundaries(tmp_path, run_request, store):
    rows = [(date(2026, 1, 1), f"{index:06}", 1.0, True, False, None) for index in range(65536)]
    rows.append(rows[-1])
    source, _ = archive(tmp_path, run_request, table=result_table(rows))
    with pytest.raises(ValueError, match="globally sorted and unique"):
        store.import_archive(source)


def test_orphan_values_are_preserved_on_conflict_and_identical_retry_recovers(
    tmp_path,
    run_request,
    store,
):
    source, _ = archive(tmp_path, run_request)
    factor = run_request["factor"]
    directory = (
        store.workspace.values_path
        / factor["factor_id"]
        / factor["definition_hash"]
        / run_request["request_id"]
    )
    directory.mkdir(parents=True)
    result = directory / "result.parquet"
    result.write_bytes(b"existing orphan result")
    with pytest.raises(ValueError, match="Existing result file conflicts"):
        store.import_archive(source)
    assert result.read_bytes() == b"existing orphan result"
    assert not list(store.workspace.runs_path.glob("*/commit.json"))
    with tarfile.open(source) as handle:
        result.write_bytes(handle.extractfile("job/output/results/fe-cli/result.parquet").read())
    assert store.import_archive(source)["rows"] == 3


def test_commit_marker_tampering_is_rejected(tmp_path, run_request, store):
    source, _ = archive(tmp_path, run_request)
    receipt = store.import_archive(source)
    commit = store.workspace.runs_path / run_request["request_id"] / "commit.json"
    commit.write_text(json.dumps(dict(receipt, values_path="/untrusted/result.parquet")))
    with pytest.raises(ValueError, match="identity differs"):
        store.get(run_request["request_id"])


def test_placeholder_report_cannot_pass_as_result_parquet(tmp_path, run_request, store):
    source, _ = archive(tmp_path, run_request, parquet_bytes=b"placeholder result report")
    with pytest.raises(ValueError, match="Cannot validate result Parquet"):
        store.import_archive(source)


@pytest.mark.parametrize(
    "reason",
    [
        "SOURCE_NULL",
        "SOURCE_NAN",
        "IMMATURE",
        "INSUFFICIENT_SAMPLES",
        "INSUFFICIENT_SURVIVAL_SUPPORT",
        "DECLARED_NON_FINITE",
        "OPERATOR_NULL",
        "DELETED",
    ],
)
def test_all_current_fe_invalid_reasons_are_supported(tmp_path, run_request, store, reason):
    table = result_table([(date(2026, 1, 1), "A", None, False, reason == "DELETED", reason)])
    source, _ = archive(tmp_path, run_request, table=table)
    assert store.import_archive(source)["rows"] == 1


def test_missing_pair_and_wrong_result_directory_rejected(tmp_path, run_request, store):
    source = tmp_path / "missing.tar"
    with tarfile.open(source, "w") as handle:
        member = tarfile.TarInfo("fe-cli/manifest.json")
        member.size = 2
        handle.addfile(member, io.BytesIO(b"{}"))
    with pytest.raises(ValueError, match="Archive requires"):
        store.import_archive(source)
    with tarfile.open(source, "a") as handle:
        member = tarfile.TarInfo("different/fe-cli/result.parquet")
        member.size = 0
        handle.addfile(member, io.BytesIO(b""))
    with pytest.raises(ValueError, match="same fe-cli directory"):
        store.import_archive(source)


def failed_archive(tmp_path, run_request, *, mutate=None, extra=()):
    manifest = {
        "protocol": PROTOCOL,
        "status": "FAILED",
        "request": run_request,
        "request_sha256": request_digest(run_request),
        "job_id": "1308",
        "files": [],
        "error": {
            "type": "NativeCompileError",
            "message": "catalog-bound FieldRef requires a separate native compiler contract",
        },
    }
    if mutate:
        mutate(manifest)
    destination = tmp_path / "failed-results.tar.gz"
    with tarfile.open(destination, "w:gz") as package:
        for name, content in [
            ("exit.json", b'{"exit_code":1}'),
            ("fe-cli/manifest.json", json.dumps(manifest).encode()),
            ("stderr.log", b"Worker failed; see fe-cli/manifest.json"),
            ("stdout.log", b""),
            *extra,
        ]:
            member = tarfile.TarInfo(name)
            member.size = len(content)
            package.addfile(member, io.BytesIO(content))
    return destination, manifest


def test_failed_worker_archive_preserves_validated_failure_without_commit(
    tmp_path,
    run_request,
    store,
):
    source, manifest = failed_archive(tmp_path, run_request)
    run = store.workspace.runs_path / run_request["request_id"]
    run.mkdir()
    prepared = json.dumps(run_request, indent=2).encode()
    (run / "request.json").write_bytes(prepared)
    with pytest.raises(ExecutionFailed, match="NativeCompileError") as failure:
        store.import_archive(source, expected_request=run_request, expected_job_id="1308")
    assert failure.value.manifest == manifest
    assert "catalog-bound FieldRef" in str(failure.value)
    assert (run / "request.json").read_bytes() == prepared
    assert list(run.iterdir()) == [run / "request.json"]
    assert list(store.workspace.values_path.iterdir()) == [store.workspace.values_path / ".staging"]
    assert list((store.workspace.values_path / ".staging").iterdir()) == []
    with pytest.raises(ValueError):
        store.get(run_request["request_id"])


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.update(protocol="unrecognized"),
        lambda m: m.update(status="COMPLETE"),
        lambda m: m.update(request_sha256="0" * 64),
        lambda m: m["request"].update(created_at="different request snapshot"),
        lambda m: m.update(job_id="999"),
        lambda m: m.update(job_id=None),
        lambda m: m.update(job_id="not-numeric"),
        lambda m: m.update(files=[{"path": "result.parquet"}]),
        lambda m: m.update(files={}),
        lambda m: m.update(error={}),
        lambda m: m["error"].update(type=""),
        lambda m: m["error"].update(message="  "),
        lambda m: m["error"].update(message=None),
    ],
)
def test_untrusted_failed_archive_is_not_reported_as_validated_execution_failure(
    tmp_path,
    run_request,
    store,
    mutate,
):
    expected = json.loads(json.dumps(run_request))
    source, _ = failed_archive(tmp_path, run_request, mutate=mutate)
    with pytest.raises(ValueError) as failure:
        store.import_archive(source, expected_request=expected, expected_job_id="1308")
    assert not isinstance(failure.value, ExecutionFailed)
    assert not list(store.workspace.runs_path.glob("*/commit.json"))
    assert list((store.workspace.values_path / ".staging").iterdir()) == []


def test_failed_archive_cannot_smuggle_result_or_unsafe_outer_member(tmp_path, run_request, store):
    for extra in (
        [("fe-cli/result.parquet", b"unexpected result")],
        [("../escape", b"unsafe outer log")],
    ):
        source, _ = failed_archive(tmp_path, run_request, extra=extra)
        with pytest.raises(ValueError) as failure:
            store.import_archive(source)
        assert not isinstance(failure.value, ExecutionFailed)
        assert list((store.workspace.values_path / ".staging").iterdir()) == []


def test_failed_archive_with_valid_digest_must_match_exact_prepared_request(
    tmp_path,
    run_request,
    store,
):
    expected = json.loads(json.dumps(run_request))
    run_request["created_at"] = "different frozen request snapshot"
    source, _ = failed_archive(tmp_path, run_request)
    with pytest.raises(ValueError, match="differs from the prepared request") as failure:
        store.import_archive(source, expected_request=expected, expected_job_id="1308")
    assert not isinstance(failure.value, ExecutionFailed)


def test_unpack_failure_mode_is_explicit_and_keeps_complete_pair_requirement(tmp_path, run_request):
    source, _ = failed_archive(tmp_path, run_request)
    stage = tmp_path / "strict-stage"
    stage.mkdir()
    with pytest.raises(ValueError, match="Archive requires"):
        unpack_result(source, stage, max_bytes=MAX_ARCHIVE_BYTES, max_files=MAX_ARCHIVE_FILES)
    allowed = tmp_path / "failure-stage"
    allowed.mkdir()
    unpack_result(
        source,
        allowed,
        max_bytes=MAX_ARCHIVE_BYTES,
        max_files=MAX_ARCHIVE_FILES,
        allow_failure=True,
    )
    assert list(allowed.iterdir()) == [allowed / "manifest.json"]
