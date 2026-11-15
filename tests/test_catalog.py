import csv
import hashlib
import io
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError

import pytest

from fe_cli.catalog import CSV_HEADERS, FactorCatalog, FactorDefinition
from fe_cli.workspace import Workspace


@pytest.fixture
def catalog(tmp_path):
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    return workspace.catalog


def test_round_trip_standard_csv_preserves_fields(catalog):
    expression = ' ts_mean(col("close"), 20)\n+ rank(col("ret")) '
    factor = FactorDefinition("momentum.1", "动量, alpha", expression, False, 'A "quoted"\ntext')
    assert catalog.add(factor) == factor
    assert catalog.list() == [factor]
    assert catalog.get(factor.factor_id) == factor
    assert factor.definition_hash == hashlib.sha256(expression.strip().encode()).hexdigest()
    assert stat.S_IMODE(catalog.path.stat().st_mode) == 0o600
    with pytest.raises(FrozenInstanceError):
        factor.name = "different"


def test_reads_bom_boolean_variants_and_reloads(catalog):
    catalog.path.write_text(
        ",".join(CSV_HEADERS) + "\na,Alpha,col('close'),TrUe,\nb,Beta,col('ret'),0,\n",
        encoding="utf-8-sig",
    )
    assert [factor.enabled for factor in catalog.list()] == [True, False]
    catalog.path.write_text(
        ",".join(CSV_HEADERS) + "\nc,Gamma,x,1,external\n", encoding="utf-8"
    )
    assert catalog.get("c").description == "external"
    with pytest.raises(ValueError, match="Unknown factor_id"):
        catalog.get("a")


def test_bom_and_physically_blank_rows_preserved_by_init(catalog):
    original = (
        "\ufeff" + ",".join(CSV_HEADERS) + "\n\n"
        "a,Alpha,x,true,\n\n"
        'b,Beta,"y\n\nz",false,\n\n'
    ).encode("utf-8")
    catalog.path.write_bytes(original)
    expected = [FactorDefinition("a", "Alpha", "x"), FactorDefinition("b", "Beta", "y\n\nz", False)]
    assert catalog.list() == expected
    assert Workspace(catalog.path.parent).initialize().created is False
    assert catalog.path.read_bytes() == original


@pytest.mark.parametrize("row", [",,,,", " ", "a,A,x,true"])
def test_blank_fields_and_partial_rows_remain_invalid(catalog, row):
    original = (",".join(CSV_HEADERS) + "\n\n" + row + "\n\n").encode()
    catalog.path.write_bytes(original)
    with pytest.raises(ValueError, match="Invalid factor catalog"):
        catalog.list()
    assert catalog.path.read_bytes() == original


def test_update_disable_delete_preserve_other_rows(catalog):
    first = FactorDefinition("a", "Alpha", "x", description="original")
    second = FactorDefinition("b", "Beta", "y", False, "leave me")
    catalog.add(first)
    catalog.add(second)
    updated = catalog.update("a", name="New", expression="z", enabled=False, description="")
    assert updated == FactorDefinition("a", "New", "z", False, "")
    assert catalog.list() == [updated, second]
    assert catalog.delete("a", expected=updated) == updated
    assert catalog.list() == [second]
    with pytest.raises(ValueError, match="Unknown factor_id"):
        catalog.update("absent", name="Missing")
    with pytest.raises(ValueError, match="Unknown factor_id"):
        catalog.delete("absent")


def test_missing_catalog_does_not_initialize(tmp_path):
    catalog = FactorCatalog(tmp_path / "absent/factors.csv")
    for operation in (catalog.list, lambda: catalog.add(FactorDefinition("a", "Alpha", "x"))):
        with pytest.raises(FileNotFoundError, match="Run fe init"):
            operation()
    assert not catalog.path.parent.exists()


@pytest.mark.parametrize("values", [
    {"factor_id": ""}, {"factor_id": "-alpha"}, {"factor_id": "中文"},
    {"factor_id": "a" * 81}, {"factor_id": "x/y"}, {"name": " "},
    {"name": "a" * 161}, {"name": "nul\x00"}, {"expression": "\n"},
    {"expression": "x" * 16385}, {"expression": "x\x00"}, {"enabled": 1},
    {"description": "x" * 4097}, {"description": "\x00"}, {"name": None},
])
def test_definition_validation(values):
    with pytest.raises(ValueError):
        FactorDefinition(**({"factor_id": "alpha", "name": "Alpha", "expression": "x"} | values))


@pytest.mark.parametrize("content", [
    b"", b"factor_id,name,expression,enabled\n", b"name,factor_id,expression,enabled,description\n",
    b"factor_id,name,expression,enabled,description,extra\n",
    b"factor_id,name,expression,enabled,description\na,A,x,true,\na,B,y,false,\n",
    b"factor_id,name,expression,enabled,description\na,A,x,true\n",
    b"factor_id,name,expression,enabled,description\na,A,x,true,,extra\n",
    b"factor_id,name,expression,enabled,description\na,A,x,yes,\n",
    b"factor_id,name,expression,enabled,description\na,A,,true,\n",
    b'factor_id,name,expression,enabled,description\na,A,"unfinished,true,\n',
    b"factor_id,name,expression,enabled,description\na,A,x,true,\x00\n",
    b"factor_id,name,expression,enabled,description\n\xff",
])
def test_malformed_files_are_preserved(catalog, content):
    catalog.path.write_bytes(content)
    for operation in (
        catalog.list, lambda: catalog.add(FactorDefinition("z", "New", "z")),
        lambda: catalog.update("a", name="Changed"), lambda: catalog.delete("a"),
    ):
        with pytest.raises(ValueError, match="Invalid factor catalog"):
            operation()
        assert catalog.path.read_bytes() == content


def test_duplicate_add_invalid_update_and_stale_expected_preserve_file(catalog):
    original = catalog.add(FactorDefinition("a", "Alpha", "x"))
    before = catalog.path.read_bytes()
    with pytest.raises(ValueError, match="Duplicate"):
        catalog.add(original)
    with pytest.raises(ValueError, match="must not be empty"):
        catalog.update("a", expression="")
    assert catalog.path.read_bytes() == before
    current = catalog.update("a", name="Manual edit")
    before = catalog.path.read_bytes()
    for operation in (
        lambda: catalog.update("a", expression="y", expected=original),
        lambda: catalog.delete("a", expected=original),
    ):
        with pytest.raises(ValueError, match="changed since"):
            operation()
        assert catalog.path.read_bytes() == before
    assert catalog.get("a") == current


def test_concurrent_manual_edit_is_preserved(catalog, monkeypatch):
    import fe_cli.catalog as module

    catalog.add(FactorDefinition("a", "Alpha", "x"))
    external = io.StringIO(newline="")
    writer = csv.writer(external)
    writer.writerow(CSV_HEADERS)
    writer.writerow(["a", "Edited externally", "x", "true", ""])
    expected = external.getvalue().encode()
    real_fsync = module.os.fsync

    def change_during_flush(descriptor):
        real_fsync(descriptor)
        catalog.path.write_bytes(expected)

    monkeypatch.setattr(module.os, "fsync", change_during_flush)
    with pytest.raises(ValueError, match="changed during editing"):
        catalog.update("a", expression="y")
    assert catalog.path.read_bytes() == expected
    assert not list(catalog.path.parent.glob(".factors.csv.*[0-9]"))


def test_parallel_catalog_writes_preserve_every_factor(catalog):
    def add(index):
        FactorCatalog(catalog.path).add(FactorDefinition(f"f{index}", f"Factor {index}", "x"))

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(add, range(12)))
    assert {factor.factor_id for factor in catalog.list()} == {f"f{i}" for i in range(12)}
