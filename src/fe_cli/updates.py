"""Bounded public release discovery and best-effort update-notice caching."""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

REPOSITORY = "HKUST-QUANT-SOCIETY/fe-cli"
LATEST_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
INSTALLER_LIMIT = 1024 * 1024
_VERSION = re.compile(r"(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\."
                      r"(0|[1-9][0-9]{0,8})(?:rc(0|[1-9][0-9]{0,8}))?")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CACHE_LIMIT = 8192


class UpdateError(ValueError):
    """Release discovery or verified download could not be completed."""


def version_key(value: str) -> tuple[int, int, int, int, int]:
    """Compare canonical numeric releases, with stable releases above their RCs."""
    match = _VERSION.fullmatch(value) if isinstance(value, str) and len(value) <= 64 else None
    if match is None:
        raise UpdateError("Invalid release version")
    major, minor, patch, candidate = match.groups()
    return int(major), int(minor), int(patch), int(candidate is None), int(candidate or 0)


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    installer_sha256: str

    def __post_init__(self) -> None:
        if version_key(self.version)[3] != 1:
            raise UpdateError("An update must be a stable release")
        if not isinstance(self.installer_sha256, str) or not _SHA256.fullmatch(
            self.installer_sha256
        ):
            raise UpdateError("Release installer SHA256 is missing or invalid")

    @property
    def release_url(self) -> str:
        return f"https://github.com/{REPOSITORY}/releases/tag/v{self.version}"

    @property
    def installer_url(self) -> str:
        return f"https://github.com/{REPOSITORY}/releases/download/v{self.version}/install.sh"


def _require_https(url: str) -> None:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname
            or parsed.username is not None or parsed.password is not None):
        raise UpdateError("Release downloads require HTTPS")


class _HTTPSRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _require_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_bytes(url: str, *, limit: int, timeout: float = 3.0) -> bytes:
    """Fetch public data with a hard body limit and no HTTPS downgrade redirects."""
    try:
        _require_https(url)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise UpdateError("Invalid download size limit")
        if not math.isfinite(timeout) or timeout <= 0:
            raise UpdateError("Invalid download timeout")
        request = Request(url, headers={"User-Agent": "fe-cli-update-check",
                                        "Accept": "application/vnd.github+json"})
        with build_opener(_HTTPSRedirectHandler()).open(request, timeout=timeout) as response:
            _require_https(response.geturl())
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdecimal() or int(length) > limit):
                raise UpdateError("Release download exceeds its size limit")
            payload = response.read(limit + 1)
            if len(payload) > limit:
                raise UpdateError("Release download exceeds its size limit")
            return payload
    except UpdateError:
        raise
    except (OSError, HTTPException, ValueError, TypeError) as exc:
        raise UpdateError(f"Could not download release data: {exc}") from exc


def fetch_latest() -> ReleaseInfo:
    """Require a stable GitHub release with one digest-identified installer asset."""
    try:
        release = json.loads(fetch_bytes(LATEST_URL, limit=256 * 1024))
        if not isinstance(release, dict):
            raise UpdateError("Invalid release metadata")
        if release.get("draft") is not False or release.get("prerelease") is not False:
            raise UpdateError("Latest release is not stable")
        tag = release.get("tag_name")
        if not isinstance(tag, str) or not tag.startswith("v"):
            raise UpdateError("Invalid release tag")
        version = tag[1:]
        if version_key(version)[3] != 1:
            raise UpdateError("Latest release is not stable")
        assets = release.get("assets")
        if not isinstance(assets, list):
            raise UpdateError("Release installer is missing")
        installers = [asset for asset in assets
                      if isinstance(asset, dict) and asset.get("name") == "install.sh"]
        if len(installers) != 1:
            raise UpdateError("Release requires exactly one install.sh asset")
        asset = installers[0]
        digest = asset.get("digest")
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            raise UpdateError("Release installer SHA256 is missing or invalid")
        info = ReleaseInfo(version, digest[7:])
        size = asset.get("size")
        if (asset.get("state") != "uploaded" or type(size) is not int
                or not 0 < size <= INSTALLER_LIMIT
                or asset.get("browser_download_url") != info.installer_url):
            raise UpdateError("Release installer metadata is invalid")
        return info
    except UpdateError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise UpdateError("Invalid release metadata") from exc


class UpdateChecker:
    """Cache successful notices for 24 hours and failed attempts for one hour."""

    def __init__(self, cache_path: Path | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
        self.cache_path = (
            cache_path if cache_path is not None else root / "fe-cli/update-check.json"
        )
        self.clock = clock

    def _read(self, now: float) -> tuple[bool, ReleaseInfo | None]:
        try:
            with self.cache_path.open("rb") as stream:
                raw = stream.read(_CACHE_LIMIT + 1)
            if len(raw) > _CACHE_LIMIT:
                return False, None
            data = json.loads(raw)
            stamp = data["checked_at"]
            if type(stamp) not in (int, float) or not math.isfinite(stamp):
                return False, None
            saved = data["release"]
            info = None if saved is None else ReleaseInfo(**saved)
            age = now - stamp
            return 0 <= age < (86400 if info is not None else 3600), info
        except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
            return False, None

    def _write(self, now: float, info: ReleaseInfo | None) -> None:
        temporary = None
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            data = {"checked_at": now, "release": None if info is None else {
                "version": info.version, "installer_sha256": info.installer_sha256}}
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False,
                                             dir=self.cache_path.parent) as stream:
                temporary = Path(stream.name)
                json.dump(data, stream)
            os.replace(temporary, self.cache_path)
        except (OSError, ValueError, TypeError):
            pass
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def check(self, *, force: bool = False) -> ReleaseInfo | None:
        now = self.clock()
        if not force:
            cached, info = self._read(now)
            if cached:
                return info
        try:
            info = fetch_latest()
        except UpdateError:
            self._write(now, None)
            if force:
                raise
            return None
        self._write(now, info)
        return info
