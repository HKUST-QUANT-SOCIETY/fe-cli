"""Resource selection and local preference persistence boundaries."""

import json
import stat
from dataclasses import FrozenInstanceError

import pytest

from fe_cli.resources import NODE_CPU_LIMITS, ResourceRequest, ResourceStore, normalize_node


def test_default_request_is_immutable():
    request = ResourceRequest()
    assert (request.node, request.cpus, request.memory_gb, request.memory) == (
        "quant-a", 6, 18, "18G"
    )
    with pytest.raises(FrozenInstanceError):
        request.cpus = 1


@pytest.mark.parametrize("alias", ["a", "A", "quant-a", "QUANT-A", " a "])
def test_a_aliases_are_canonical(alias):
    assert normalize_node(alias) == ResourceRequest(alias).node == "quant-a"


@pytest.mark.parametrize("alias", ["c", "C", "quant-c", "QUANT-C"])
def test_c_aliases_are_canonical(alias):
    assert normalize_node(alias) == ResourceRequest(alias).node == "quant-c"


@pytest.mark.parametrize("node,limit", NODE_CPU_LIMITS.items())
def test_each_integer_increment_pairs_one_cpu_with_three_gb(node, limit):
    for cpus in range(1, limit + 1):
        request = ResourceRequest(node, cpus)
        assert request.memory_gb == cpus * 3
        assert request.memory == f"{cpus * 3}G"
    with pytest.raises(ValueError, match=f"1 to {limit}"):
        ResourceRequest(node, limit + 1)


@pytest.mark.parametrize("cpus", [0, -1, True, False, 1.0, "3", None])
def test_invalid_cpu_counts_are_rejected(cpus):
    with pytest.raises(ValueError, match="CPU count"):
        ResourceRequest(cpus=cpus)


@pytest.mark.parametrize("node", ["b", "quant-b", "quant-a; ls", "", None, 1])
def test_unsupported_nodes_are_rejected(node):
    with pytest.raises(ValueError, match="Node must be A or C"):
        ResourceRequest(node=node)


def test_missing_preferences_return_defaults_without_writing(tmp_path):
    store = ResourceStore(tmp_path / "new" / "resources.json")
    assert store.load() == ResourceRequest()
    assert not store.path.parent.exists()


def test_preferences_round_trip_and_explicit_replacement(tmp_path):
    store = ResourceStore(tmp_path / "new" / "resources.json")
    assert store.save(ResourceRequest("C", 7)) == store.path
    assert json.loads(store.path.read_text()) == {
        "version": 1, "resources": {"node": "quant-c", "cpus": 7}
    }
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert ResourceStore(store.path).load() == ResourceRequest("quant-c", 7)
    store.save(ResourceRequest("a", 2))
    assert store.load() == ResourceRequest("a", 2)
    assert not list(store.path.parent.glob(".resources-*"))


@pytest.mark.parametrize("content", [
    "{broken", "[]", '{"version":99,"resources":{"node":"quant-a","cpus":6}}',
    '{"version":true,"resources":{"node":"quant-a","cpus":6}}',
    '{"version":1,"resources":{"node":"quant-a","cpus":6},"extra":1}',
    '{"version":1,"resources":{"node":"quant-a","cpus":6,"memory":"18G"}}',
    '{"version":1,"resources":{"node":"A","cpus":6}}',
    '{"version":1,"resources":{"node":"quant-c","cpus":9}}',
    '{"version":1,"resources":{"node":"quant-a","cpus":true}}',
    '{"version":1,"resources":null}', '{"resources":{}}',
    "[" * 10_000 + "]" * 10_000,
])
def test_corrupt_preferences_are_actionable_and_preserved(tmp_path, content):
    store = ResourceStore(tmp_path / "resources.json")
    store.path.write_text(content, encoding="utf-8")
    for action in (store.load, lambda: store.save(ResourceRequest())):
        with pytest.raises(ValueError, match="Invalid resource preferences.*not overwritten"):
            action()
        assert store.path.read_text(encoding="utf-8") == content


def test_invalid_utf8_is_preserved(tmp_path):
    store = ResourceStore(tmp_path / "resources.json")
    store.path.write_bytes(b"\xff")
    with pytest.raises(ValueError, match="expected UTF-8.*not overwritten"):
        store.load()
    assert store.path.read_bytes() == b"\xff"


def test_failed_atomic_replacement_preserves_previous_preferences(tmp_path, monkeypatch):
    store = ResourceStore(tmp_path / "resources.json")
    store.save(ResourceRequest())
    original = store.path.read_bytes()

    def reject_replace(*args):
        raise OSError("simulated filesystem failure")

    monkeypatch.setattr("fe_cli.resources.os.replace", reject_replace)
    with pytest.raises(OSError, match="filesystem failure"):
        store.save(ResourceRequest("C", 8))
    assert store.path.read_bytes() == original
    assert not list(tmp_path.glob(".resources-*"))


def test_preference_path_uses_xdg_config(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert ResourceStore().path == tmp_path / "fe-cli" / "resources.json"


def test_preference_path_falls_back_to_home(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert ResourceStore().path == tmp_path / ".config" / "fe-cli" / "resources.json"
