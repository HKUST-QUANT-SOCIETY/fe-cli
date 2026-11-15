"""Bounded OpenSSH public-key authentication checks without a remote session."""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass

from fe_cli.config import ConnectionConfig
from fe_cli.remote import (
    MAX_OUTPUT_BYTES,
    _plain_output,
    _stop,
    _stop_late_launch,
    build_ssh_argv,
)


@dataclass(frozen=True)
class DiagnosticResult:
    host: str
    port: int
    user: str
    connection: str
    authentication: str
    key_fingerprint: str | None = None
    key_type: str | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.authentication == "authenticated"


def build_diagnostic_argv(config: ConnectionConfig) -> list[str]:
    arguments = build_ssh_argv(config, "cluster-envs")
    # Reuse connection validation and flags, but never send a remote command.
    return arguments[:-2] + [
        "-vv", "-N", "-o", "SessionType=none",
        "-o", "PreferredAuthentications=publickey",
        "-o", "ControlMaster=no", "-o", "ControlPath=none",
        "-o", "ClearAllForwardings=yes", "-o", "PermitLocalCommand=no",
        arguments[-2],
    ]


class _Authenticated(Exception):
    """Stop reading immediately after authentication, before any session exists."""


class _OutputLimit(Exception):
    pass


class _Progress:
    def __init__(self, config: ConnectionConfig):
        self.config = config
        self.user = config.user
        self.connection = "unknown"
        self.authentication = "unknown"
        self.key: tuple[str, str] | None = None
        self.detail = ""
        self.pending = b""

    def feed(self, chunk: bytes, *, eof: bool = False) -> None:
        self.pending += chunk
        lines = self.pending.split(b"\n")
        self.pending = lines.pop()
        if eof and self.pending:
            lines.append(self.pending)
            self.pending = b""
        for raw in lines:
            self.line(_plain_output(raw).strip())

    def line(self, line: str) -> None:
        lowered = line.lower()
        if "connection established." in lowered:
            self.connection = "connected"
        if not self.user:
            match = re.search(r"Authenticating to .+ as '([A-Za-z0-9_.-]+)'", line)
            if match:
                self.user = match[1]
        if "Server accepts key:" in line:
            # The key path is intentionally neither saved nor returned.
            record = line.split("Server accepts key:", 1)[1]
            match = re.search(r"\b([A-Za-z0-9][A-Za-z0-9@._+-]*) "
                              r"(SHA256:[A-Za-z0-9+/]+={0,2})(?:\s|$)", record)
            self.key = (match[1], match[2]) if match else None
        if (re.fullmatch(r'debug\d+: Authentication succeeded \(publickey\)\.', line)
                or re.fullmatch(r'Authenticated to .+ using "publickey"\.', line)):
            self.connection = "connected"
            self.authentication = "authenticated"
            self.detail = "Public-key authentication succeeded; no remote session was opened."
            raise _Authenticated
        if ("remote host identification has changed" in lowered
                or "host key verification failed" in lowered):
            self.connection = "connected"
            self.detail = "SSH host key verification failed. Verify the host key manually."
        elif "permission denied" in lowered and "publickey" in lowered:
            self.connection = "connected"
            self.authentication = "failed"
            self.detail = "Public-key authentication failed. Check the SSH user and key access."
        elif "could not resolve hostname" in lowered:
            self.connection = "failed"
            self.detail = "SSH hostname could not be resolved. Check the host or SSH alias."
        elif "connection refused" in lowered:
            self.connection = "failed"
            self.detail = "SSH connection was refused. Check the host, port, and SSH service."
        elif any(cause in lowered for cause in (
            "network is unreachable", "no route to host", "connection timed out",
            "operation timed out",
        )):
            self.connection = "failed"
            self.detail = "SSH could not reach the host. Check the network, host, and port."

    def result(self, detail: str = "") -> DiagnosticResult:
        key = self.key if self.authentication == "authenticated" else None
        return DiagnosticResult(
            self.config.host, self.config.port, self.user,
            self.connection, self.authentication,
            key_fingerprint=key[1] if key else None,
            key_type=key[0] if key else None,
            detail=detail or self.detail or "SSH exited before authentication was confirmed.",
        )


async def diagnose(config: ConnectionConfig) -> DiagnosticResult:
    """Authenticate with public keys, terminate SSH, and return only safe metadata."""
    arguments = build_diagnostic_argv(config)
    progress = _Progress(config)
    process = None
    launch = None
    readers: list[asyncio.Task] = []
    total = 0

    async def read_stream(stream: asyncio.StreamReader, *, debug: bool) -> None:
        nonlocal total
        while chunk := await stream.read(16384):
            total += len(chunk)
            if total > MAX_OUTPUT_BYTES:
                raise _OutputLimit
            if debug:
                progress.feed(chunk)
        if debug:
            progress.feed(b"", eof=True)

    try:
        async with asyncio.timeout(config.timeout):
            launch = asyncio.create_task(asyncio.create_subprocess_exec(
                *arguments,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=32768,
                start_new_session=os.name == "posix",
            ))
            process = await asyncio.shield(launch)
            readers = [
                asyncio.create_task(read_stream(process.stdout, debug=False)),
                asyncio.create_task(read_stream(process.stderr, debug=True)),
            ]
            await asyncio.gather(*readers)
            await process.wait()
        return progress.result()
    except _Authenticated:
        return progress.result()
    except _OutputLimit:
        return progress.result("SSH diagnostic exceeded the 1 MiB combined output limit.")
    except TimeoutError:
        if progress.connection == "unknown":
            progress.connection = "failed"
        return progress.result(f"SSH diagnostic timed out after {config.timeout} seconds.")
    except OSError:
        progress.connection = "failed"
        return progress.result("Could not start SSH. Check the configured SSH executable.")
    finally:
        for reader in readers:
            reader.cancel()
        if readers:
            await asyncio.gather(*readers, return_exceptions=True)
        if process is not None:
            await _stop(process)
        elif launch is not None:
            launch.add_done_callback(_stop_late_launch)
