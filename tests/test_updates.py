import io
import json
from dataclasses import FrozenInstanceError
from http.client import IncompleteRead
from urllib.request import Request

import pytest

from fe_cli import updates
from fe_cli.updates import ReleaseInfo, UpdateChecker, UpdateError, version_key

SHA = "a" * 64
INFO = ReleaseInfo("0.2.0", SHA)


def release():
    return {
        "draft": False, "prerelease": False, "tag_name": "v0.2.0",
        "assets": [{"name": "install.sh", "state": "uploaded", "size": 100,
                    "digest": f"sha256:{SHA}", "browser_download_url": INFO.installer_url}],
    }


def test_numeric_version_order_and_fixed_urls():
    versions = ["1.2.0", "1.10.0", "1.10.1rc2", "1.10.1rc10", "1.10.1", "2.0.0rc1"]
    assert sorted(reversed(versions), key=version_key) == versions
    assert INFO.release_url == "https://github.com/HKUST-QUANT-SOCIETY/fe-cli/releases/tag/v0.2.0"
    with pytest.raises(FrozenInstanceError):
        INFO.version = "9.0.0"


@pytest.mark.parametrize("value", ["v1.2.3", "1.2", "01.2.3", "1.2.3rc01", "1.2.3post1",
                                   "1.2.3\n", "9" * 10000, None, "1000000000.1.0"])
def test_reject_noncanonical_versions(value):
    with pytest.raises(UpdateError):
        version_key(value)


def test_fetch_latest_uses_fixed_public_api(monkeypatch):
    def fetch(url, *, limit):
        assert url == updates.LATEST_URL
        assert limit == 256 * 1024
        return json.dumps(release()).encode()
    monkeypatch.setattr(updates, "fetch_bytes", fetch)
    assert updates.fetch_latest() == INFO


@pytest.mark.parametrize("change", [
    lambda r: r.update(draft=True),
    lambda r: r.update(prerelease=True),
    lambda r: r.update(tag_name="v0.2.0rc1"),
    lambda r: r.update(tag_name="v00.2.0"),
    lambda r: r.update(assets=[]),
    lambda r: r["assets"].append(dict(r["assets"][0])),
    lambda r: r["assets"][0].update(state="new"),
    lambda r: r["assets"][0].update(size=True),
    lambda r: r["assets"][0].update(size=0),
    lambda r: r["assets"][0].update(size=updates.INSTALLER_LIMIT + 1),
    lambda r: r["assets"][0].update(digest=None),
    lambda r: r["assets"][0].update(digest="sha256:" + "A" * 64),
    lambda r: r["assets"][0].update(digest="sha512:" + SHA),
    lambda r: r["assets"][0].update(browser_download_url=INFO.installer_url + "?x=1"),
    lambda r: r["assets"][0].update(browser_download_url="https://evil.example/install.sh"),
])
def test_reject_unsafe_or_incomplete_release_metadata(monkeypatch, change):
    metadata = release()
    change(metadata)
    monkeypatch.setattr(updates, "fetch_bytes", lambda *a, **kw: json.dumps(metadata).encode())
    with pytest.raises(UpdateError):
        updates.fetch_latest()


@pytest.mark.parametrize("payload", [b"not json", b"\xff", b"[]", b"null"])
def test_reject_malformed_api_response(monkeypatch, payload):
    monkeypatch.setattr(updates, "fetch_bytes", lambda *a, **kw: payload)
    with pytest.raises(UpdateError):
        updates.fetch_latest()


class Response(io.BytesIO):
    def __init__(self, payload=b"hello", *, headers=None, url="https://example.com/asset"):
        super().__init__(payload)
        self.headers = headers or {}
        self.url = url

    def geturl(self):
        return self.url


def transport(monkeypatch, response=None, error=None):
    class Opener:
        def open(self, request, *, timeout):
            assert request.get_header("Authorization") is None
            assert timeout == 3.0
            if error:
                raise error
            return response
    monkeypatch.setattr(updates, "build_opener", lambda *a: Opener())


def test_fetch_bytes_enforces_body_limit(monkeypatch):
    transport(monkeypatch, Response())
    assert updates.fetch_bytes("https://example.com/asset", limit=5) == b"hello"
    for response in [Response(b"123456"), Response(headers={"Content-Length": "6"}),
                     Response(url="http://example.com/asset")]:
        transport(monkeypatch, response)
        with pytest.raises(UpdateError):
            updates.fetch_bytes("https://example.com/asset", limit=5)


def test_fetch_bytes_normalizes_timeout_and_rejects_http(monkeypatch):
    transport(monkeypatch, error=TimeoutError("timed out"))
    with pytest.raises(UpdateError, match="timed out"):
        updates.fetch_bytes("https://example.com/asset", limit=5)
    with pytest.raises(UpdateError, match="HTTPS"):
        updates.fetch_bytes("http://example.com/asset", limit=5)
    handler = updates._HTTPSRedirectHandler()
    with pytest.raises(UpdateError, match="HTTPS"):
        handler.redirect_request(Request("https://example.com"), None, 302, "", {},
                                 "http://example.com/asset")


def test_incomplete_http_response_becomes_update_error(monkeypatch):
    transport(monkeypatch, error=IncompleteRead(b"partial", 10))
    with pytest.raises(UpdateError):
        updates.fetch_bytes("https://example.com/asset", limit=100)


def test_cache_success_expiry_and_force(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(updates, "fetch_latest", lambda: calls.append(1) or INFO)
    now = [100000.0]
    checker = UpdateChecker(tmp_path / "nested/cache.json", lambda: now[0])
    assert checker.check() == INFO
    assert checker.check() == INFO
    assert len(calls) == 1
    assert checker.check(force=True) == INFO
    assert len(calls) == 2
    now[0] += 86400
    assert checker.check() == INFO
    assert len(calls) == 3
    assert list(checker.cache_path.parent.iterdir()) == [checker.cache_path]


def test_failure_throttles_background_but_force_always_raises(monkeypatch, tmp_path):
    calls = []
    def fail():
        calls.append(1)
        raise UpdateError("offline")
    monkeypatch.setattr(updates, "fetch_latest", fail)
    now = [100000.0]
    checker = UpdateChecker(tmp_path / "cache.json", lambda: now[0])
    assert checker.check() is None
    assert checker.check() is None
    assert len(calls) == 1
    with pytest.raises(UpdateError, match="offline"):
        checker.check(force=True)
    now[0] += 3600
    assert checker.check() is None
    assert len(calls) == 3


@pytest.mark.parametrize("content", ["{", "[]", "x" * 8193,
    json.dumps({"checked_at": 200000, "release": None}),
    json.dumps({"checked_at": float("nan"), "release": None}),
    json.dumps({"checked_at": 99999, "release": {"version": "0.2.0", "url": "evil"}}),
])
def test_corrupt_oversized_and_future_caches_are_refreshed(monkeypatch, tmp_path, content):
    path = tmp_path / "cache.json"
    path.write_text(content)
    monkeypatch.setattr(updates, "fetch_latest", lambda: INFO)
    assert UpdateChecker(path, lambda: 100000).check() == INFO


def test_unwritable_cache_does_not_block_release_discovery(monkeypatch, tmp_path):
    parent = tmp_path / "file"
    parent.write_text("existing")
    monkeypatch.setattr(updates, "fetch_latest", lambda: INFO)
    assert UpdateChecker(parent / "cache.json").check() == INFO
    assert parent.read_text() == "existing"


def test_cache_defaults_to_xdg_location(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert UpdateChecker().cache_path == tmp_path / "fe-cli/update-check.json"
