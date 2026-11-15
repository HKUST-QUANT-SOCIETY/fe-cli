import asyncio
import os
import signal
import sys
import time
from unittest.mock import AsyncMock

import pytest

from fe_cli.config import ConnectionConfig
from fe_cli.remote import (
    MAX_OUTPUT_BYTES,
    RemoteClient,
    RemoteError,
    _plain_output,
    build_ssh_argv,
)


class FakeProcess:
    def __init__(self, stdout=b"", stderr=b"", returncode=0, hanging=False):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stderr.feed_data(stderr)
        self.returncode = None if hanging else returncode
        self.killed = False
        self.waited = False
        self.finished = asyncio.Event()
        if not hanging:
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self.finished.set()

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.finished.set()

    async def wait(self):
        await self.finished.wait()
        self.waited = True
        return self.returncode


@pytest.mark.parametrize("identity", [r"C:\Users\First Last\.ssh\id_ed25519", "~/.ssh/my key"])
def test_argv_preserves_identity_and_strict_flags(identity):
    config = ConnectionConfig(
        "qcl-alias", "researcher", "/mnt/c/Windows/System32/OpenSSH/ssh.exe", identity,
        port=2222, connect_timeout=7,
    )
    assert build_ssh_argv(config, "cluster-status") == [
        config.ssh_executable, "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=7", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2",
        "-p", "2222", "-i", identity, "-o", "IdentitiesOnly=yes", "-l", "researcher",
        "qcl-alias", "cluster-status",
    ]


@pytest.mark.parametrize("command", ["cluster-status; id", "cluster-status ", "ls", "", None, []])
async def test_whitelist_rejects_before_launch(command, monkeypatch):
    launch = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    with pytest.raises(RemoteError, match="Only cluster"):
        await RemoteClient(ConnectionConfig("qcl")).query(command)
    launch.assert_not_called()


async def test_success_sanitizes_output_and_preserves_stderr(monkeypatch):
    process = FakeProcess(b"\x1b[31mquant-a\x1b[0m\x00\n\xff", b"warning\x07\n")
    launch = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    result = await RemoteClient(ConnectionConfig("qcl")).query("cluster-envs")
    assert result.stdout == "quant-a\n\ufffd"
    assert result.stderr == "warning\n"
    assert result.command == "cluster-envs"
    assert result.returncode == 0
    assert process.waited and not process.killed
    assert launch.call_args.kwargs["stdin"] == asyncio.subprocess.DEVNULL
    assert "shell" not in launch.call_args.kwargs


async def test_terminal_osc_and_dcs_sequences_are_removed(monkeypatch):
    process = FakeProcess(b"a\x1b]8;;https://bad\x1b\\b\x1b]8;;\x07\x1bPpayload\x1b\\c\r")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    assert (await RemoteClient(ConnectionConfig("qcl")).query("cluster-history")).stdout == "abc"


def test_unterminated_terminal_controls_have_bounded_processing():
    started = time.monotonic()
    assert _plain_output(b"\x1bP" * 50000) == ""
    assert time.monotonic() - started < 1


async def test_nonzero_exit_exposes_plain_error(monkeypatch):
    process = FakeProcess(stderr=b"\x1b[31mHost key verification failed.\x1b[0m", returncode=255)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    with pytest.raises(RemoteError, match=r"exit 255.*Host key verification failed"):
        await RemoteClient(ConnectionConfig("qcl")).query("cluster-status")
    assert process.waited


async def test_missing_executable(monkeypatch):
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(side_effect=FileNotFoundError())
    )
    with pytest.raises(RemoteError, match="Could not start SSH"):
        await RemoteClient(ConnectionConfig("qcl")).query("cluster-status")


async def test_timeout_kills_and_reaps(monkeypatch):
    process = FakeProcess(hanging=True)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    with pytest.raises(RemoteError, match="timed out"):
        await RemoteClient(ConnectionConfig("qcl", timeout=1)).query("cluster-status")
    assert process.killed and process.waited


async def test_cancellation_kills_and_reaps(monkeypatch):
    process = FakeProcess(hanging=True)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    task = asyncio.create_task(RemoteClient(ConnectionConfig("qcl")).query("cluster-status"))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.killed and process.waited


async def test_cancellation_during_launch_reaps_late_child(monkeypatch):
    process = FakeProcess(hanging=True)
    release = asyncio.Event()

    async def launch(*args, **kwargs):
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    task = asyncio.create_task(RemoteClient(ConnectionConfig("qcl")).query("cluster-status"))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await asyncio.sleep(0.01)
    assert process.killed and process.waited


async def test_combined_output_limit_kills_and_reaps(monkeypatch):
    process = FakeProcess(b"a" * (MAX_OUTPUT_BYTES // 2 + 1), b"b" * (MAX_OUTPUT_BYTES // 2),
                          hanging=True)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    with pytest.raises(RemoteError, match="1 MiB"):
        await RemoteClient(ConnectionConfig("qcl")).query("cluster-history")
    assert process.killed and process.waited


@pytest.mark.parametrize("mode", ["overflow", "timeout", "cancel", "descendant", "detached"])
async def test_local_fake_executable_cleanup(tmp_path, monkeypatch, mode):
    """Exercise actual OS pipes and child cleanup without executing SSH."""
    executable = tmp_path / "fake_ssh.py"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os, subprocess, sys, time\n"
        "if sys.argv[-2] == 'overflow':\n"
        "    while True:\n"
        "        os.write(1, b'x' * 65536)\n"
        "elif sys.argv[-2] == 'descendant':\n"
        "    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "    time.sleep(30)\n"
        "elif sys.argv[-2] == 'detached':\n"
        "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
        "start_new_session=True)\n"
        f"    open({str(tmp_path / 'child.pid')!r}, 'w').write(str(child.pid))\n"
        "    time.sleep(30)\n"
        "else:\n"
        "    time.sleep(30)\n"
    )
    executable.chmod(0o700)
    processes = []
    launched = asyncio.Event()
    real_launch = asyncio.create_subprocess_exec

    async def launch(*args, **kwargs):
        process = await real_launch(*args, **kwargs)
        processes.append(process)
        launched.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    config = ConnectionConfig(mode, ssh_executable=str(executable), timeout=1)
    task = asyncio.create_task(RemoteClient(config).query("cluster-status"))
    await launched.wait()
    if mode == "cancel":
        await asyncio.sleep(0.01)
        task.cancel()
        expected = asyncio.CancelledError
        message = None
    else:
        expected = RemoteError
        message = "1 MiB" if mode == "overflow" else "timed out"
    try:
        with pytest.raises(expected, match=message):
            await asyncio.wait_for(task, timeout=4)
        assert processes[0].returncode is not None
        assert await processes[0].wait() != 0
    finally:
        # The detached fixture intentionally escapes the owned process group.
        child_file = tmp_path / "child.pid"
        if child_file.exists():
            try:
                os.kill(int(child_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
