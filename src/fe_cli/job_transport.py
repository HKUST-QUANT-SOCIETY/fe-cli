"""Explicit job submission and bounded binary retrieval over local OpenSSH."""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import stat
import tempfile
from pathlib import Path

from fe_cli.config import ConnectionConfig
from fe_cli.remote import (
    MAX_OUTPUT_BYTES,
    _plain_output,
    _stop,
    _stop_late_launch,
    build_ssh_prefix,
)
from fe_cli.run_contract import validate_request
from fe_cli.runtime_settings import RuntimeSettings

MAX_PAYLOAD_BYTES = 128 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 4 * 1024**3
_JOB_ID = re.compile(r"[1-9][0-9]{0,19}\Z", re.ASCII)
_ACK = re.compile(rb"(?:\[cluster\] job=)?([1-9][0-9]{0,19})")


class SubmissionError(ValueError):
    """Submission failed; uncertainty forbids an automatic resubmission."""

    def __init__(self, message: str, *, uncertain: bool = False) -> None:
        super().__init__(message)
        self.uncertain = uncertain


class DownloadError(ValueError):
    """A result transfer did not produce a complete, bounded local archive."""


def _job_id(value: str) -> str:
    if not isinstance(value, str) or not _JOB_ID.fullmatch(value):
        raise ValueError("Job ID must contain 1 to 20 positive ASCII digits.")
    return value


def build_submit_argv(
    config: ConnectionConfig, request: dict, runtime: RuntimeSettings
) -> list[str]:
    """Validate a frozen request and quote its single remote command argument."""
    validate_request(request)
    runtime.__post_init__()
    resource = request["resources"]
    if not runtime.release_root or not runtime.release_sha256:
        raise ValueError(
            "Configure an authorized server release_root and release_sha256 with configure-run; "
            "legacy code-upload mode is no longer supported."
        )
    frozen_runtime = request.get("runtime", {})
    if not isinstance(frozen_runtime, dict) or any(
        frozen_runtime.get(field) != getattr(runtime, field)
        for field in ("release_root", "release_sha256")
    ):
        raise ValueError("Public release differs from the frozen request runtime")
    entrypoint = [
        f"{runtime.release_root.rstrip('/')}/runner.py",
        "--release-sha256", runtime.release_sha256, "request.json",
    ]
    return [
        *build_ssh_prefix(config),
        shlex.join(
            [
                "cluster-submit",
                "--env",
                runtime.environment,
                "--node",
                resource["node"],
                "--cpus",
                str(resource["cpus"]),
                "--mem",
                resource["memory"],
                "--time",
                runtime.time_limit,
                "--name",
                f"fe-{request['request_id'][:12]}",
                "--gpu-shares",
                "0",
                "--",
                runtime.remote_python,
                "-I",
                *entrypoint,
            ]
        ),
    ]


async def _read_text(stream: asyncio.StreamReader, budget: list[int]) -> bytes:
    output = bytearray()
    while chunk := await stream.read(16384):
        budget[0] += len(chunk)
        if budget[0] > MAX_OUTPUT_BYTES:
            raise ValueError("SSH text output exceeded the 1 MiB limit.")
        output.extend(chunk)
    return bytes(output)


async def _cleanup(process, launch, readers: list[asyncio.Task]) -> None:
    for reader in readers:
        reader.cancel()
    if readers:
        await asyncio.gather(*readers, return_exceptions=True)
    if process is not None:
        await _stop(process)
    elif launch is not None:
        launch.add_done_callback(_stop_late_launch)


async def _exit_status(process: asyncio.subprocess.Process) -> int:
    # After both EOFs, the documented returncode is the completed exit status.
    # Some local Python builds lose an early wait() notification for short children.
    while process.returncode is None:
        await asyncio.sleep(0.01)
    return process.returncode


class JobTransport:
    def __init__(
        self, config: ConnectionConfig, *, max_download_bytes: int = MAX_DOWNLOAD_BYTES
    ) -> None:
        if type(max_download_bytes) is not int or max_download_bytes < 1:
            raise ValueError("Download size limit must be a positive integer.")
        self.config = config
        self.max_download_bytes = max_download_bytes

    async def submit(self, payload: Path, request: dict, runtime: RuntimeSettings) -> str:
        """Submit exactly once; only an unambiguous cluster acknowledgement succeeds."""
        process = launch = None
        readers: list[asyncio.Task] = []
        launched = False
        try:
            arguments = build_submit_argv(self.config, request, runtime)
            with Path(payload).open("rb") as source:
                metadata = os.fstat(source.fileno())
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or not 1 <= metadata.st_size <= MAX_PAYLOAD_BYTES
                ):
                    raise ValueError("Payload must be a regular file between 1 byte and 128 MiB.")
                async with asyncio.timeout(self.config.timeout):
                    launch = asyncio.create_task(
                        asyncio.create_subprocess_exec(
                            *arguments,
                            stdin=source,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            limit=32768,
                            start_new_session=os.name == "posix",
                        )
                    )
                    launched = True
                    process = await asyncio.shield(launch)
                    budget = [0]
                    readers = [
                        asyncio.create_task(_read_text(stream, budget))
                        for stream in (process.stdout, process.stderr)
                    ]
                    stdout, stderr = await asyncio.gather(*readers)
                    returncode = await _exit_status(process)
            if returncode:
                detail = _plain_output(stderr).strip() or "No error details returned."
                raise SubmissionError(
                    f"SSH submission failed (exit {returncode}): {detail}", uncertain=True
                )
            acknowledgements = [
                match.group(1).decode("ascii")
                for output in (stdout, stderr)
                for line in output.splitlines()
                if (match := _ACK.fullmatch(line))
            ]
            if len(acknowledgements) != 1:
                raise SubmissionError(
                    "Missing or ambiguous cluster job acknowledgement.", uncertain=True
                )
            return acknowledgements[0]
        except SubmissionError:
            raise
        except TimeoutError as exc:
            raise SubmissionError(
                f"SSH submission timed out after {self.config.timeout} seconds.", uncertain=launched
            ) from exc
        except OSError as exc:
            # Failure to launch the local executable cannot have submitted a job.
            started = process is not None
            raise SubmissionError(f"Could not submit job: {exc}", uncertain=started) from exc
        except (ValueError, TypeError, AttributeError) as exc:
            raise SubmissionError(str(exc), uncertain=launched) from exc
        finally:
            await _cleanup(process, launch, readers)

    async def download(self, job_id: str, target: Path) -> Path:
        """Stream raw archive bytes to a private sibling and publish without overwrite."""
        process = launch = None
        readers: list[asyncio.Task] = []
        temporary: Path | None = None
        try:
            job_id = _job_id(job_id)
            arguments = [*build_ssh_prefix(self.config), shlex.join(["cluster-results", job_id])]
            target = Path(target)
            if target.exists() or target.is_symlink():
                raise DownloadError("Result target already exists.")
            descriptor, name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".partial", dir=target.parent
            )
            temporary = Path(name)
            with os.fdopen(descriptor, "wb") as destination:

                async def read_binary() -> None:
                    count = 0
                    while chunk := await process.stdout.read(65536):
                        count += len(chunk)
                        if count > self.max_download_bytes:
                            raise DownloadError("Result archive exceeded the download size limit.")
                        destination.write(chunk)

                async with asyncio.timeout(self.config.timeout):
                    launch = asyncio.create_task(
                        asyncio.create_subprocess_exec(
                            *arguments,
                            stdin=asyncio.subprocess.DEVNULL,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            limit=32768,
                            start_new_session=os.name == "posix",
                        )
                    )
                    process = await asyncio.shield(launch)
                    readers = [
                        asyncio.create_task(read_binary()),
                        asyncio.create_task(_read_text(process.stderr, [0])),
                    ]
                    _, stderr = await asyncio.gather(*readers)
                    returncode = await _exit_status(process)
                    if returncode:
                        detail = _plain_output(stderr).strip() or "No error details returned."
                        raise DownloadError(
                            f"SSH result download failed (exit {returncode}): {detail}"
                        )
                    destination.flush()
                    os.fsync(destination.fileno())
            # A hard link atomically publishes the complete file and refuses a racing target.
            os.link(temporary, target)
            return target
        except TimeoutError as exc:
            raise DownloadError(
                f"SSH result download timed out after {self.config.timeout} seconds."
            ) from exc
        except DownloadError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise DownloadError(f"Could not download results: {exc}") from exc
        finally:
            await _cleanup(process, launch, readers)
            if temporary is not None:
                temporary.unlink(missing_ok=True)
