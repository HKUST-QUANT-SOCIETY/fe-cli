import json
import stat
from dataclasses import FrozenInstanceError

import pytest

from fe_cli.config import ConnectionConfig, default_config_path, load_config, save_config


def test_default_path_does_not_create_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "settings"))
    assert default_config_path() == tmp_path / "settings/fe-cli/connection.json"
    assert not (tmp_path / "settings").exists()


def test_default_path_uses_home(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_config_path() == tmp_path / ".config/fe-cli/connection.json"


def test_round_trip_atomic_private_settings(tmp_path):
    path = tmp_path / "nested/connection.json"
    config = ConnectionConfig("qcl", "researcher", identity_file=r"C:\Users\me\.ssh\id_ed25519")
    assert save_config(config, path) == path
    assert load_config(path) == config
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["version"] == 1
    replacement = ConnectionConfig("quant-a", identity_file="~/.ssh/id_ed25519")
    save_config(replacement, path)
    assert load_config(path) == replacement
    assert list(path.parent.iterdir()) == [path]
    with pytest.raises(FrozenInstanceError):
        config.host = "changed"


@pytest.mark.parametrize("values", [
    {"host": "-oProxyCommand=bad"}, {"host": "user@host"}, {"host": "a b"},
    {"host": "host\n"}, {"host": ""}, {"host": None}, {"user": "-root"},
    {"user": "a;whoami"}, {"ssh_executable": ""}, {"ssh_executable": "ssh\x00"},
    {"identity_file": ""}, {"identity_file": "key\r"}, {"port": True},
    {"port": 65536}, {"connect_timeout": 0}, {"timeout": 3601}, {"timeout": 1.5},
])
def test_reject_invalid_values(values):
    with pytest.raises(ValueError):
        ConnectionConfig(**({"host": "qcl"} | values))


@pytest.mark.parametrize("document", [
    "{", "[]", '{"version":2,"connection":{"host":"qcl"}}',
    '{"version":true,"connection":{"host":"qcl"}}',
    '{"version":1,"connection":[]}', '{"version":1,"connection":{}}',
    '{"version":1,"connection":{"host":"qcl","password":"secret"}}',
    '{"version":1,"connection":{"host":"qcl"},"extra":true}',
])
def test_corrupt_or_unsupported_config_is_protected(tmp_path, document):
    path = tmp_path / "connection.json"
    path.write_text(document)
    with pytest.raises(ValueError):
        load_config(path)
    with pytest.raises(ValueError):
        save_config(ConnectionConfig("replacement"), path)
    assert path.read_text() == document


def test_missing_and_invalid_encoding(tmp_path):
    path = tmp_path / "missing/connection.json"
    with pytest.raises(FileNotFoundError, match="fe configure"):
        load_config(path)
    assert not path.parent.exists()
    path.parent.mkdir()
    path.write_bytes(b"\xff")
    with pytest.raises(ValueError, match="UTF-8"):
        load_config(path)


def test_large_config_is_rejected(tmp_path):
    path = tmp_path / "connection.json"
    path.write_text(" " * 65537)
    with pytest.raises(ValueError, match="64 KiB"):
        load_config(path)


def test_excessive_nesting_is_reported_and_preserved(tmp_path):
    path = tmp_path / "connection.json"
    document = "[" * 10000 + "]" * 10000
    path.write_text(document)
    with pytest.raises(ValueError, match="repair it"):
        load_config(path)
    with pytest.raises(ValueError):
        save_config(ConnectionConfig("qcl"), path)
    assert path.read_text() == document
