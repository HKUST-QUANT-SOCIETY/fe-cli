"""Local factor definitions stored as standard CSV; expressions are never evaluated."""

from __future__ import annotations

import csv
import fcntl
import hashlib
import io
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

CSV_HEADERS = ["factor_id", "name", "expression", "enabled", "description"]
_FACTOR_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")


@dataclass(frozen=True)
class FactorDefinition:
    factor_id: str
    name: str
    expression: str
    enabled: bool = True
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.factor_id, str) or not _FACTOR_ID.fullmatch(self.factor_id):
            raise ValueError("factor_id must be 1–80 ASCII letters, numbers, '_', '.' or '-'.")
        for field, limit, required in (
            ("name", 160, True), ("expression", 16384, True), ("description", 4096, False)
        ):
            value = getattr(self, field)
            if not isinstance(value, str) or "\x00" in value or len(value) > limit:
                raise ValueError(f"{field} must be text of at most {limit} characters without NUL.")
            if required and not value.strip():
                raise ValueError(f"{field} must not be empty.")
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be true or false.")

    @property
    def definition_hash(self) -> str:
        return hashlib.sha256(self.expression.strip().encode("utf-8")).hexdigest()


class FactorCatalog:
    """Reload on every operation and protect complete-file edits with a local lock."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser().resolve()

    def _read(self) -> tuple[bytes, list[FactorDefinition]]:
        try:
            payload = self.path.read_bytes()
        except FileNotFoundError as exc:
            raise FileNotFoundError(
                f"Factor catalog not found at {self.path}. Run fe init first."
            ) from exc
        try:
            reader = csv.reader(io.StringIO(payload.decode("utf-8-sig"), newline=""), strict=True)
            if next(reader, None) != CSV_HEADERS:
                raise ValueError(f"Expected CSV header: {','.join(CSV_HEADERS)}.")
            factors = []
            identifiers: set[str] = set()
            for row in reader:
                if row == []:
                    continue
                if len(row) != len(CSV_HEADERS):
                    raise ValueError(f"CSV row ending at line {reader.line_num} needs 5 columns.")
                value = row[3].strip().lower()
                if value not in {"true", "false", "1", "0"}:
                    raise ValueError(f"Invalid enabled value at line {reader.line_num}.")
                factor = FactorDefinition(row[0], row[1], row[2], value in {"true", "1"}, row[4])
                if factor.factor_id in identifiers:
                    raise ValueError(f"Duplicate factor_id: {factor.factor_id}.")
                identifiers.add(factor.factor_id)
                factors.append(factor)
            return payload, factors
        except (UnicodeError, csv.Error, ValueError) as exc:
            raise ValueError(f"Invalid factor catalog at {self.path}: {exc}") from exc

    def list(self) -> list[FactorDefinition]:
        return self._read()[1]

    @staticmethod
    def _find(factors: list[FactorDefinition], factor_id: str) -> FactorDefinition:
        for factor in factors:
            if factor.factor_id == factor_id:
                return factor
        raise ValueError(f"Unknown factor_id: {factor_id}.")

    def get(self, factor_id: str) -> FactorDefinition:
        return self._find(self.list(), factor_id)

    def _publish(self, original: bytes, factors: list[FactorDefinition]) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                os.fchmod(handle.fileno(), 0o600)
                writer = csv.writer(handle)
                writer.writerow(CSV_HEADERS)
                writer.writerows(
                    [f.factor_id, f.name, f.expression, str(f.enabled).lower(), f.description]
                    for f in factors
                )
                handle.flush()
                os.fsync(handle.fileno())
            try:
                current = self.path.read_bytes()
            except FileNotFoundError as exc:
                raise ValueError("Catalog changed during editing; reload and retry.") from exc
            if hashlib.sha256(current).digest() != hashlib.sha256(original).digest():
                raise ValueError("Catalog changed during editing; reload and retry.")
            # An editor ignoring the advisory lock can still race after this final check.
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _mutate(
        self, change: Callable[[list[FactorDefinition]], FactorDefinition]
    ) -> FactorDefinition:
        # Read first so a missing catalog never creates a directory or a lock file.
        self._read()
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            original, factors = self._read()
            result = change(factors)
            self._publish(original, factors)
            return result

    def add(self, factor: FactorDefinition) -> FactorDefinition:
        if not isinstance(factor, FactorDefinition):
            raise ValueError("Expected a FactorDefinition.")
        factor.__post_init__()

        def change(factors: list[FactorDefinition]) -> FactorDefinition:
            if any(existing.factor_id == factor.factor_id for existing in factors):
                raise ValueError(f"Duplicate factor_id: {factor.factor_id}.")
            factors.append(factor)
            return factor

        return self._mutate(change)

    @staticmethod
    def _check_expected(factor: FactorDefinition, expected: FactorDefinition | None) -> None:
        if expected is not None and factor != expected:
            raise ValueError("Factor changed since it was opened; reload and retry.")

    def update(
        self,
        factor_id: str,
        *,
        name: str | None = None,
        expression: str | None = None,
        enabled: bool | None = None,
        description: str | None = None,
        expected: FactorDefinition | None = None,
    ) -> FactorDefinition:
        def change(factors: list[FactorDefinition]) -> FactorDefinition:
            factor = self._find(factors, factor_id)
            self._check_expected(factor, expected)
            updates = {key: value for key, value in {
                "name": name, "expression": expression,
                "enabled": enabled, "description": description,
            }.items() if value is not None}
            updated = replace(factor, **updates)
            factors[factors.index(factor)] = updated
            return updated

        return self._mutate(change)

    def delete(
        self, factor_id: str, *, expected: FactorDefinition | None = None
    ) -> FactorDefinition:
        def change(factors: list[FactorDefinition]) -> FactorDefinition:
            factor = self._find(factors, factor_id)
            self._check_expected(factor, expected)
            factors.remove(factor)
            return factor

        return self._mutate(change)
