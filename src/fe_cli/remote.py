"""Bounded, read-only QCL queries over the user's local SSH executable."""

from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fe_cli.config import ConnectionConfig

if TYPE_CHECKING:
    from fe_cli.diagnostics import DiagnosticResult

ALLOWED_COMMANDS = frozenset({"cluster-envs", "cluster-status", "cluster-history"})
MAX_OUTPUT_BYTES = 1024 * 1024
_CLEANUP_TIMEOUT = 1


class RemoteError(ValueError):
    """An invalid query, SSH failure, or bounded transport failure."""


@dataclass(frozen=True)
class RemoteResult:
    command: str
    stdout: str
    stderr: str
    returncode: int = 0


def _validate_command(command: str) -> None:
    if not isinstance(command, str) or command not in ALLOWED_COMMANDS:
        raise RemoteError("Only cluster-envs, cluster-status and cluster-history are allowed.")


def build_ssh_argv(config: ConnectionConfig, command: str) -> list[str]:
    _validate_command(command)
    return [*build_ssh_prefix(config), command]


def build_ssh_prefix(config: ConnectionConfig) -> list[str]:
    """Build strict SSH transport options, ending with the validated host."""
    config.__post_init__()
    arguments = [
        config.ssh_executable,
        "-T",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"ConnectTimeout={config.connect_timeout}",
        "-o", "ServerAliveInterval=10",
        "-o", "ServerAliveCountMax=2",
        "-p", str(config.port),
    ]
    if config.identity_file is not None:
        arguments.extend(["-i", config.identity_file, "-o", "IdentitiesOnly=yes"])
    if config.user:
        arguments.extend(["-l", config.user])
    arguments.append(config.host)
    return arguments


def _plain_output(value: bytes) -> str:
    """Strip terminal escapes in one pass, including unterminated string controls."""
    decoded = value.decode("utf-8", errors="replace")
    output = []
    index = 0
    while index < len(decoded):
        char = decoded[index]
        index += 1
        if char == "\x1b" and index < len(decoded):
            control = decoded[index]
            index += 1
        elif char in "\x9b\x9d\x90\x9e\x9f":
            control = {"\x9b": "[", "\x9d": "]", "\x90": "P",
                       "\x9e": "^", "\x9f": "_"}[char]
        else:
            if char in "\n\t" or (ord(char) >= 32 and not 127 <= ord(char) <= 159):
                output.append(char)
            continue
        if control == "[":
            while index < len(decoded):
                final = decoded[index]
                index += 1
                if "@" <= final <= "~":
                    break
        elif control in "]P^_":
            while index < len(decoded):
                if decoded[index] == "\x9c" or (control == "]" and decoded[index] == "\x07"):
                    index += 1
                    break
                if decoded[index:index + 2] == "\x1b\\":
                    index += 2
                    break
                index += 1
    return "".join(output)


async def _stop(process: asyncio.subprocess.Process) -> None:
    # Each POSIX query owns its session, so inherited pipes cannot outlive a timeout.
    grouped = os.name == "posix" and getattr(process, "pid", None) is not None
    if grouped:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    async def discard(stream: asyncio.StreamReader | None) -> None:
        if stream is not None:
            while await stream.read(16384):
                pass

    try:
        # One total cleanup deadline also covers detached descendants retaining pipes.
        async with asyncio.timeout(_CLEANUP_TIMEOUT):
            try:
                async with asyncio.timeout(_CLEANUP_TIMEOUT / 2):
                    await asyncio.gather(
                        discard(process.stdout), discard(process.stderr), process.wait()
                    )
            except TimeoutError:
                # StreamReader has no public close API; close the pipe as a last resort.
                for stream in (process.stdout, process.stderr):
                    transport = getattr(stream, "_transport", None)
                    if transport is not None:
                        transport.close()
                await process.wait()
    except TimeoutError:
        pass


def _stop_late_launch(task: asyncio.Task) -> None:
    """Reap a child even when cancellation arrives during executable launch."""
    if not task.cancelled() and task.exception() is None:
        cleanup = asyncio.create_task(_stop(task.result()))
        cleanup.add_done_callback(lambda done: None if done.cancelled() else done.exception())


class RemoteClient:
    def __init__(self, config: ConnectionConfig):
        self.config = config

    async def diagnose(self) -> DiagnosticResult:
        from fe_cli.diagnostics import diagnose

        return await diagnose(self.config)

    async def query(self, command: str) -> RemoteResult:
        arguments = build_ssh_argv(self.config, command)
        process = None
        launch = None
        readers: list[asyncio.Task] = []
        total = 0

        async def read_stream(stream: asyncio.StreamReader) -> bytes:
            nonlocal total
            output = bytearray()
            while chunk := await stream.read(16384):
                total += len(chunk)
                if total > MAX_OUTPUT_BYTES:
                    raise RemoteError("SSH output exceeded the 1 MiB combined output limit.")
                output.extend(chunk)
            return bytes(output)

        try:
            async with asyncio.timeout(self.config.timeout):
                launch = asyncio.create_task(asyncio.create_subprocess_exec(
                    *arguments,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=32768,
                    start_new_session=os.name == "posix",
                ))
                process = await asyncio.shield(launch)
                readers = [asyncio.create_task(read_stream(stream))
                           for stream in (process.stdout, process.stderr)]
                stdout, stderr = await asyncio.gather(*readers)
                returncode = await process.wait()
            result = RemoteResult(command, _plain_output(stdout), _plain_output(stderr), returncode)
            if returncode:
                detail = result.stderr.strip() or "No error details returned."
                raise RemoteError(f"SSH query failed (exit {returncode}): {detail}")
            return result
        except TimeoutError as exc:
            raise RemoteError(f"SSH query timed out after {self.config.timeout} seconds.") from exc
        except OSError as exc:
            raise RemoteError(f"Could not start SSH executable: {exc}") from exc
        finally:
            for reader in readers:
                reader.cancel()
            if readers:
                await asyncio.gather(*readers, return_exceptions=True)
            if process is not None:
                await _stop(process)
            elif launch is not None:
                launch.add_done_callback(_stop_late_launch)
