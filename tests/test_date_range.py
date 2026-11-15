"""Date range validation and durable local preference boundaries."""

import json
import stat
from dataclasses import FrozenInstanceError

import pytest

from fe_cli.date_range import DateRange, DateRangeStore


def test_defaults_are_immutable_and_accept_real_inclusive_dates():
    window = DateRange()
    assert (window.start, window.end) == ("2026-10-01", "2026-10-31")
    with pytest.raises(FrozenInstanceError):
        window.start = "2026-09-01"
    assert DateRange("2024-02-29", "2024-02-29").start == "2024-02-29"


@pytest.mark.parametrize("value", [
    "2026-2-01", "20261001", "2026-W40-4", "2026-10-01T00:00:00", " 2026-10-01",
    "2026-10-01\n", "２０２６-１０-０１", "2026-02-29", "2026-04-31", "0000-01-01",
    "2026-13-01", "", None, True, 20261001,
])
@pytest.mark.parametrize("field", ["start", "end"])
def test_invalid_bounds_are_rejected(value, field):
    with pytest.raises(ValueError, match=f"{field} date"):
        DateRange(**{field: value})


def test_reversed_dates_are_rejected():
    with pytest.raises(ValueError, match="start date must not be after end date"):
        DateRange("2026-11-01", "2026-10-31")


def test_missing_preferences_return_legacy_defaults_without_creating_files(tmp_path):
    store = DateRangeStore(tmp_path / "new" / "range.json")
    assert store.load() == DateRange()
    assert not store.path.parent.exists()


def test_preferences_persist_and_reload_with_private_atomic_file(tmp_path):
    store = DateRangeStore(tmp_path / "new" / "range.json")
    window = DateRange("2026-09-01", "2026-09-30")
    assert store.save(window) == store.path
    assert json.loads(store.path.read_text()) == {"version": 1, "range": {
        "start": "2026-09-01", "end": "2026-09-30",
    }}
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert DateRangeStore(store.path).load() == window
    store.save(DateRange())
    assert store.load() == DateRange()
    assert not list(store.path.parent.glob(".range-*"))


@pytest.mark.parametrize("content", [
    "{broken", "[]", '{"version":99,"range":{"start":"2026-10-01","end":"2026-10-31"}}',
    '{"version":true,"range":{"start":"2026-10-01","end":"2026-10-31"}}',
    '{"version":1,"range":{"start":"2026-10-01","end":"2026-10-31"},"extra":1}',
    '{"version":1,"range":{"start":"2026-10-01","end":"2026-10-31","extra":1}}',
    '{"version":1,"range":{"start":"2026-11-01","end":"2026-10-31"}}',
    '{"version":1,"range":{"start":"2026-02-29","end":"2026-10-31"}}',
    '{"version":1,"range":{"start":null,"end":"2026-10-31"}}',
    '{"version":1,"range":null}', '{"range":{}}', "[" * 10_000 + "]" * 10_000,
])
def test_corrupt_preferences_remain_untouched_on_load_and_save(tmp_path, content):
    store = DateRangeStore(tmp_path / "range.json")
    store.path.write_text(content, encoding="utf-8")
    for action in (store.load, lambda: store.save(DateRange())):
        with pytest.raises(ValueError, match="Invalid date range preferences.*not overwritten"):
            action()
        assert store.path.read_text(encoding="utf-8") == content


def test_invalid_utf8_is_preserved_on_load_and_save(tmp_path):
    store = DateRangeStore(tmp_path / "range.json")
    store.path.write_bytes(b"\xff")
    for action in (store.load, lambda: store.save(DateRange())):
        with pytest.raises(ValueError, match="expected UTF-8.*not overwritten"):
            action()
        assert store.path.read_bytes() == b"\xff"


def test_invalid_save_argument_creates_no_files(tmp_path):
    store = DateRangeStore(tmp_path / "new" / "range.json")
    with pytest.raises(ValueError, match="must be a DateRange"):
        store.save({"start": "2026-10-01", "end": "2026-10-31"})
    assert not store.path.parent.exists()


def test_failed_atomic_replacement_preserves_previous_preferences(tmp_path, monkeypatch):
    store = DateRangeStore(tmp_path / "range.json")
    store.save(DateRange())
    original = store.path.read_bytes()

    def reject_replace(*args):
        raise OSError("simulated filesystem failure")

    monkeypatch.setattr("fe_cli.date_range.os.replace", reject_replace)
    with pytest.raises(OSError, match="filesystem failure"):
        store.save(DateRange("2026-09-01", "2026-09-30"))
    assert store.path.read_bytes() == original
    assert not list(tmp_path.glob(".range-*"))


def test_preference_path_uses_xdg_config(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert DateRangeStore().path == tmp_path / "fe-cli" / "range.json"


@pytest.mark.parametrize("xdg", [None, ""])
def test_preference_path_falls_back_to_home(monkeypatch, tmp_path, xdg):
    if xdg is None:
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", xdg)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert DateRangeStore().path == tmp_path / ".config" / "fe-cli" / "range.json"
