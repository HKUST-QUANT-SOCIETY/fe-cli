import csv
import stat
from dataclasses import FrozenInstanceError

import pytest

from fe_cli.catalog import CSV_HEADERS, FactorDefinition
from fe_cli.workspace import Workspace


def test_initialization_creates_header_only_private_csv_and_reserved_directories(tmp_path):
    workspace = Workspace(tmp_path / "nested/research")
    result = workspace.initialize()
    assert result.workspace == workspace.root
    assert result.csv_path == workspace.catalog_path
    assert result.readme_path == workspace.readme_path
    assert result.created is True
    with workspace.catalog_path.open(newline="") as handle:
        assert list(csv.reader(handle)) == [CSV_HEADERS]
    assert workspace.catalog.list() == []
    assert stat.S_IMODE(workspace.catalog_path.stat().st_mode) == 0o600
    assert workspace.values_path.is_dir()
    assert workspace.runs_path.is_dir()
    readme = workspace.readme_path.read_text()
    assert "local factor definitions" in readme
    assert "immutable local values" in readme
    assert "fe --online run ID" in readme
    with pytest.raises(FrozenInstanceError):
        result.created = False


def test_default_and_expanded_paths_do_not_create_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert Workspace().root == tmp_path / "fe-workspace"
    assert Workspace("~/custom/../research").root == tmp_path / "research"
    assert not (tmp_path / "fe-workspace").exists()
    assert not (tmp_path / "research").exists()


def test_idempotent_existing_files_preserved_byte_for_byte(tmp_path):
    workspace = Workspace(tmp_path)
    workspace.initialize()
    csv = b"\xef\xbb\xbffactor_id,name,expression,enabled,description\na,Alpha,x,1,\n"
    workspace.catalog_path.write_bytes(csv)
    workspace.readme_path.write_bytes(b"User's own documentation\n")
    before = {path: path.read_bytes() for path in (workspace.catalog_path, workspace.readme_path)}
    assert workspace.initialize().created is False
    assert {path: path.read_bytes() for path in before} == before


def test_partial_workspace_completed_without_replacing_catalog(tmp_path):
    workspace = Workspace(tmp_path)
    workspace.catalog_path.write_text(
        ",".join(CSV_HEADERS) + "\na,Alpha,x,true,existing\n", encoding="utf-8"
    )
    before = workspace.catalog_path.read_bytes()
    workspace.values_path.mkdir()
    assert workspace.initialize().created is True
    assert workspace.catalog_path.read_bytes() == before
    assert workspace.catalog.list() == [FactorDefinition("a", "Alpha", "x", True, "existing")]
    assert workspace.runs_path.is_dir()
    assert workspace.readme_path.exists()


@pytest.mark.parametrize("entry", ["root", "values", "runs", "catalog", "readme"])
def test_path_collisions_are_refused_before_other_creation(tmp_path, entry):
    root = tmp_path / "workspace"
    workspace = Workspace(root)
    if entry == "root":
        root.write_text("occupied")
        collision = root
    else:
        root.mkdir()
        collision = {
            "values": workspace.values_path, "runs": workspace.runs_path,
            "catalog": workspace.catalog_path, "readme": workspace.readme_path,
        }[entry]
        if entry in {"catalog", "readme"}:
            collision.mkdir()
        else:
            collision.write_text("occupied")
    with pytest.raises(ValueError, match="occupied"):
        workspace.initialize()
    if entry == "root":
        assert root.read_text() == "occupied"
    else:
        assert list(root.iterdir()) == [collision]


def test_invalid_existing_catalog_is_never_overwritten_or_completed(tmp_path):
    workspace = Workspace(tmp_path)
    invalid = b"my,broken,headers\n"
    workspace.catalog_path.write_bytes(invalid)
    with pytest.raises(ValueError, match="Invalid factor catalog"):
        workspace.initialize()
    assert workspace.catalog_path.read_bytes() == invalid
    assert list(tmp_path.iterdir()) == [workspace.catalog_path]


def test_symlink_catalog_is_refused_without_changing_target(tmp_path):
    target = tmp_path / "outside.csv"
    target.write_text(
        ",".join(CSV_HEADERS) + "\n", encoding="utf-8"
    )
    workspace = Workspace(tmp_path / "workspace")
    workspace.root.mkdir()
    workspace.catalog_path.symlink_to(target)
    with pytest.raises(ValueError, match="occupied"):
        workspace.initialize()
    assert target.read_text() == ",".join(CSV_HEADERS) + "\n"
    assert not workspace.values_path.exists()
