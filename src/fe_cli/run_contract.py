"""Public single-run request and result protocol for the client and server."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, date, datetime
from uuid import uuid4

PROTOCOL = "fe-cli-result-v1"
MAX_MANIFEST_BYTES = 64 * 1024**2
RESULT_SCHEMA = {
    "ts": "Date",
    "inst": "String",
    "value": "Float64",
    "is_valid": "Boolean",
    "is_deleted": "Boolean",
    "invalid_reason": "String",
}
_ID = re.compile(r"[a-f0-9]{32}\Z")
_FACTOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def request_digest(request: dict) -> str:
    return hashlib.sha256(canonical_bytes(request)).hexdigest()


def validate_request(request: dict) -> dict:
    try:
        if not isinstance(request, dict) or request.get("protocol") != PROTOCOL:
            raise ValueError("Unsupported run request protocol")
        if not isinstance(request["request_id"], str) or not _ID.fullmatch(request["request_id"]):
            raise ValueError("Invalid request ID")
        factor = request["factor"]
        if not isinstance(factor["factor_id"], str) or not _FACTOR.fullmatch(factor["factor_id"]):
            raise ValueError("Invalid factor ID")
        for field in ("name", "expression"):
            if not isinstance(factor[field], str) or not factor[field].strip():
                raise ValueError(f"Factor {field} is required")
        if len(factor["expression"]) > 16384:
            raise ValueError("Expression exceeds the supported size")
        expected = hashlib.sha256(factor["expression"].strip().encode("utf-8")).hexdigest()
        if factor["definition_hash"] != expected:
            raise ValueError("Expression digest differs")
        window = request["window"]
        for key in ("start", "end"):
            if date.fromisoformat(window[key]).isoformat() != window[key]:
                raise ValueError("Dates must use YYYY-MM-DD")
        if window["start"] > window["end"]:
            raise ValueError("Reversed request window")
        resources = request["resources"]
        limit = {"quant-a": 6, "quant-c": 8}.get(resources["node"], 0)
        cpus = resources["cpus"]
        if type(cpus) is not int or not 1 <= cpus <= limit or resources["memory"] != f"{cpus * 3}G":
            raise ValueError("Invalid resource request")
        source = request["source"]
        if source["kind"] != "qcl_daily_v1" or not isinstance(source["data_root"], str):
            raise ValueError("Unsupported source")
        if not source["data_root"].startswith("/") or any(ord(c) < 32 for c in source["data_root"]):
            raise ValueError("Source root must be an absolute server path")
        canonical_bytes(request)
    except (KeyError, TypeError, AttributeError, OverflowError) as exc:
        raise ValueError(f"Invalid run request: {exc}") from exc
    return request


def make_request(factor: dict, resources: dict, start: str, end: str, data_root: str) -> dict:
    return validate_request(
        {
            "protocol": PROTOCOL,
            "request_id": uuid4().hex,
            "created_at": datetime.now(UTC).isoformat(),
            "factor": dict(factor),
            "resources": dict(resources),
            "window": {"start": start, "end": end},
            "source": {"kind": "qcl_daily_v1", "data_root": data_root},
        }
    )
