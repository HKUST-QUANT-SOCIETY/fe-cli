"""Transport checks use harmless local executables, never SSH or a server."""

import asyncio
import hashlib
import json
import os
import shlex
import signal
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from fe_cli.config import ConnectionConfig
from fe_cli.job_transport import (
    MAX_PAYLOAD_BYTES,
    DownloadError,
    JobTransport,
    SubmissionError,
)
from fe_cli.run_contract import make_request
from fe_cli.runtime_settings import RuntimeSettings


def configured_runtime(python="/bin/python"):
    return RuntimeSettings(
        python, data_root="/fixture/data", release_root="/fixture/releases/core",
        release_sha256="a" * 64,
    )


@pytest.fixture
def request_doc():
    expression = "close"
    request = make_request(
        {
            "factor_id": "close",
            "name": "Close",
            "expression": expression,
            "definition_hash": hashlib.sha256(expression.encode()).hexdigest(),
        },
        {"node": "quant-c", "cpus": 2, "memory": "6G"},
        "2026-01-01",
        "2026-01-02",
        "/fixture/data",
    )
    request["runtime"] = asdict(configured_runtime())
    return request


@pytest.fixture
def payload(tmp_path):
    target = tmp_path / "payload.tar.gz"
    target.write_bytes(b"\x1f\x8b\x00test-payload\x00")
    return target


def fake_ssh(tmp_path, body, *, timeout=5):
    executable = tmp_path / "fake ssh.py"
    executable.write_text(f"#!{sys.executable}\n" + body)
    executable.chmod(0o700)
    return ConnectionConfig(
        "qcl-test",
        user="researcher",
        ssh_executable=str(executable),
        identity_file="/local/key with ' quote",
        timeout=timeout,
    )


async def test_submit_quotes_one_remote_command_and_streams_file(tmp_path, payload, request_doc):
    capture = tmp_path / "capture.json"
    body = (
        "import json, sys\n"
        f"with open({str(capture)!r}, 'w') as f:\n"
        " json.dump({'args': sys.argv[1:], 'payload': list(sys.stdin.buffer.read())}, f)\n"
        "print('Welcome with unrelated number 77')\n"
        "print('[cluster] job=12345')\n"
    )
    config = fake_ssh(tmp_path, body)
    runtime = configured_runtime("/approved python's/bin/python")
    assert await JobTransport(config).submit(payload, request_doc, runtime) == "12345"
    recorded = json.loads(capture.read_text())
    assert bytes(recorded["payload"]) == payload.read_bytes()
    args = recorded["args"]
    assert args[-2] == "qcl-test"
    assert "BatchMode=yes" in args and "StrictHostKeyChecking=yes" in args
    assert args[args.index("-i") + 1] == config.identity_file
    assert shlex.split(args[-1]) == [
        "cluster-submit",
        "--env",
        "research",
        "--node",
        "quant-c",
        "--cpus",
        "2",
        "--mem",
        "6G",
        "--time",
        "00:30:00",
        "--name",
        f"fe-{request_doc['request_id'][:12]}",
        "--gpu-shares",
        "0",
        "--",
        runtime.remote_python,
        "-I",
        f"{runtime.release_root}/runner.py",
        "--release-sha256",
        runtime.release_sha256,
        "request.json",
    ]


@pytest.mark.parametrize(
    "stdout",
    [
        "Submitted batch job 123",
        "0",
        "0123",
        "１２３",
        "123456789012345678901",
        "123 trailing",
        "prefix 123",
        "123\n123",
        "[cluster] job=0",
        "[cluster] job=0123",
        "[cluster] job=１２３",
        "prefix [cluster] job=123",
        "[cluster] job=123 trailing",
        "[cluster] job=123\n[cluster] job=456",
        "\x1b[31m[cluster] job=123\x1b[0m",
        "[cluster] job=123456789012345678901",
        "",
    ],
)
async def test_submit_rejects_missing_or_ambiguous_ack(tmp_path, payload, request_doc, stdout):
    config = fake_ssh(tmp_path, f"import sys\nsys.stdout.write({stdout!r})\n")
    with pytest.raises(SubmissionError, match="acknowledgement") as error:
        await JobTransport(config).submit(payload, request_doc, configured_runtime())
    assert error.value.uncertain


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("receipt", ["1242", "[cluster] job=1242", "12345678901234567890"])
async def test_submit_accepts_one_exact_receipt_on_either_stream(
    tmp_path, payload, request_doc, stream, receipt
):
    config = fake_ssh(tmp_path, f"import sys\nsys.{stream}.write({receipt + chr(10)!r})\n")
    expected = receipt.removeprefix("[cluster] job=")
    assert (
        await JobTransport(config).submit(payload, request_doc, configured_runtime())
        == expected
    )


@pytest.mark.parametrize(
    "stdout,stderr",
    [
        ("1242\n", "1243\n"),
        ("1242\n", "[cluster] job=1242\n"),
        ("[cluster] job=1242\n", "[cluster] job=1243\n"),
        ("", "1242\n1242\n"),
    ],
)
async def test_submit_rejects_multiple_receipts_across_streams(
    tmp_path, payload, request_doc, stdout, stderr
):
    config = fake_ssh(
        tmp_path,
        f"import sys\nsys.stdout.write({stdout!r})\nsys.stderr.write({stderr!r})\n",
    )
    with pytest.raises(SubmissionError, match="acknowledgement") as error:
        await JobTransport(config).submit(payload, request_doc, configured_runtime())
    assert error.value.uncertain


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_submit_nonzero_with_bare_receipt_remains_uncertain(
    tmp_path, payload, request_doc, stream
):
    config = fake_ssh(tmp_path, f"import sys\nsys.{stream}.write('1242\\n')\nsys.exit(4)\n")
    with pytest.raises(SubmissionError, match="exit 4") as error:
        await JobTransport(config).submit(payload, request_doc, configured_runtime())
    assert error.value.uncertain


async def test_submit_nonzero_even_with_ack_is_uncertain_and_sanitized(
    tmp_path, payload, request_doc
):
    config = fake_ssh(
        tmp_path,
        "import sys\nprint('[cluster] job=123')\n"
        "sys.stderr.write('\\x1b[31mfailed\\x1b[0m\\x00')\nsys.exit(4)\n",
    )
    with pytest.raises(SubmissionError, match=r"exit 4.*failed") as error:
        await JobTransport(config).submit(payload, request_doc, configured_runtime())
    assert error.value.uncertain and "\x1b" not in str(error.value)


@pytest.mark.parametrize("invalid", ["request", "missing", "empty", "oversized", "executable"])
async def test_submit_local_failure_is_certain(tmp_path, payload, request_doc, invalid):
    marker = tmp_path / "launched"
    config = fake_ssh(tmp_path, f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    if invalid == "request":
        request_doc["resources"]["cpus"] = 99
    elif invalid == "missing":
        payload.unlink()
    elif invalid == "empty":
        payload.write_bytes(b"")
    elif invalid == "oversized":
        with payload.open("wb") as stream:
            stream.truncate(MAX_PAYLOAD_BYTES + 1)
    else:
        Path(config.ssh_executable).unlink()
    with pytest.raises(SubmissionError) as error:
        await JobTransport(config).submit(payload, request_doc, configured_runtime())
    assert not error.value.uncertain
    assert not marker.exists()


async def test_legacy_runtime_rejected_before_payload_open_or_ssh_launch(tmp_path, request_doc):
    marker = tmp_path / "launched"
    config = fake_ssh(tmp_path, f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    runtime = RuntimeSettings("/fixture/python", data_root="/fixture/data", release_root=None)
    with pytest.raises(SubmissionError, match="legacy code-upload mode") as error:
        await JobTransport(config).submit(tmp_path / "missing-payload", request_doc, runtime)
    assert error.value.uncertain is False
    assert not marker.exists()


@pytest.mark.parametrize("mode", ["timeout", "stdout_flood", "stderr_flood"])
async def test_submit_after_launch_failures_are_uncertain(tmp_path, payload, request_doc, mode):
    body = (
        "import time\ntime.sleep(30)\n"
        if mode == "timeout"
        else f"import os\nwhile True: os.write({1 if mode == 'stdout_flood' else 2}, b'x'*65536)\n"
    )
    config = fake_ssh(tmp_path, body, timeout=1)
    with pytest.raises(SubmissionError) as error:
        await JobTransport(config).submit(payload, request_doc, configured_runtime())
    assert error.value.uncertain


async def test_download_binary_over_one_mib_unchanged(tmp_path):
    # Terminal escapes, non-UTF8 and NUL bytes are all ordinary archive bytes.
    chunk = b"\x00\xff\x1b[31m\x1b]abc\x07" * 4096
    config = fake_ssh(
        tmp_path, f"import os\nfor _ in range(32): os.write(1, {chunk!r})\n", timeout=5
    )
    target = tmp_path / "result.tar.gz"
    assert await JobTransport(config).download("123", target) == target
    assert target.read_bytes() == chunk * 32
    assert target.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("*.partial"))


async def test_download_uses_only_validated_results_command(tmp_path):
    capture = tmp_path / "argv.json"
    config = fake_ssh(
        tmp_path,
        "import json, sys\n"
        f"open({str(capture)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
        "sys.stdout.buffer.write(b'archive')\n",
    )
    await JobTransport(config).download("12345", tmp_path / "result")
    args = json.loads(capture.read_text())
    assert args[-2:] == ["qcl-test", "cluster-results 12345"]


@pytest.mark.parametrize("job_id", ["0", "-1", "001", "１２３", "1; id", "1\n", "1" * 21, None])
async def test_download_invalid_id_does_not_launch(tmp_path, job_id):
    marker = tmp_path / "launched"
    config = fake_ssh(tmp_path, f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    with pytest.raises(DownloadError, match="ASCII"):
        await JobTransport(config).download(job_id, tmp_path / "result")
    assert not marker.exists() and not list(tmp_path.glob("*.partial"))


@pytest.mark.parametrize("kind", ["file", "symlink", "race"])
async def test_download_never_overwrites_target(tmp_path, kind):
    target = tmp_path / "result"
    protected = tmp_path / "protected"
    protected.write_bytes(b"original")
    config = fake_ssh(
        tmp_path,
        "from pathlib import Path\nimport sys\n"
        + (f"Path({str(target)!r}).write_bytes(b'racing')\n" if kind == "race" else "")
        + "sys.stdout.buffer.write(b'replacement')\n",
    )
    if kind == "file":
        target.write_bytes(b"original")
    elif kind == "symlink":
        target.symlink_to(protected)
    with pytest.raises(DownloadError):
        await JobTransport(config).download("123", target)
    assert target.read_bytes() == (b"racing" if kind == "race" else b"original")
    assert protected.read_bytes() == b"original"
    assert not list(tmp_path.glob("*.partial"))


@pytest.mark.parametrize(
    "mode", ["timeout", "cancel", "overflow", "stderr_flood", "nonzero", "descendant", "detached"]
)
async def test_failed_download_cleans_partial_and_pipes(tmp_path, monkeypatch, mode):
    target = tmp_path / "result.tar.gz"
    body = "import os, subprocess, sys, time\nos.write(1, b'partial\\x00')\n"
    if mode == "overflow":
        body += "os.write(1, b'x'*65536)\n"
    elif mode == "stderr_flood":
        body += "while True: os.write(2, b'x'*65536)\n"
    elif mode == "nonzero":
        body += "sys.stderr.write('\\x1b[31mfailed\\x1b[0m')\nsys.exit(3)\n"
    elif mode in {"descendant", "detached"}:
        body += (
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
            f"start_new_session={mode == 'detached'})\n"
            f"open({str(tmp_path / 'child.pid')!r}, 'w').write(str(child.pid))\n"
            "time.sleep(30)\n"
        )
    else:
        body += "time.sleep(30)\n"
    config = fake_ssh(tmp_path, body, timeout=1)
    processes = []
    launched = asyncio.Event()
    actual_launch = asyncio.create_subprocess_exec

    async def launch(*args, **kwargs):
        process = await actual_launch(*args, **kwargs)
        processes.append(process)
        launched.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    task = asyncio.create_task(
        JobTransport(config, max_download_bytes=1024).download("123", target)
    )
    await launched.wait()
    if mode == "cancel":
        await asyncio.sleep(0.03)
        task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError if mode == "cancel" else DownloadError):
            await asyncio.wait_for(task, timeout=4)
        assert processes[0].returncode is not None
        assert not target.exists() and not list(tmp_path.glob("*.partial"))
    finally:
        child_file = tmp_path / "child.pid"
        if child_file.exists():
            try:
                os.kill(int(child_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


async def test_cancel_during_download_launch_reaps_late_child(tmp_path, monkeypatch):
    config = fake_ssh(tmp_path, "import time\ntime.sleep(30)\n")
    actual_launch = asyncio.create_subprocess_exec
    release = asyncio.Event()
    entered = asyncio.Event()
    processes = []

    async def launch(*args, **kwargs):
        entered.set()
        await release.wait()
        process = await actual_launch(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    target = tmp_path / "result"
    task = asyncio.create_task(JobTransport(config).download("123", target))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    for _ in range(100):
        if processes and processes[0].returncode is not None:
            break
        await asyncio.sleep(0.01)
    assert processes and processes[0].returncode is not None
    assert not target.exists() and not list(tmp_path.glob("*.partial"))
