import asyncio
import os
import sys
from dataclasses import asdict
from unittest.mock import AsyncMock

import pytest

from fe_cli.config import ConnectionConfig
from fe_cli.diagnostics import DiagnosticResult, build_diagnostic_argv, diagnose
from fe_cli.remote import MAX_OUTPUT_BYTES

FINGERPRINT = "SHA256:abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"
ACCEPTED = f"debug1: Server accepts key: /private/key path ED25519 {FINGERPRINT} explicit"
SUCCESS = 'Authenticated to qcl ([10.0.0.1]:22) using "publickey".'


class FakeProcess:
    def __init__(self, *, stderr=b"", stdout=b"", hanging=False, returncode=255):
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


def test_argv_has_no_remote_command_and_reuses_strict_connection_flags():
    config = ConnectionConfig(
        "qcl", "researcher", "/mnt/c/Windows/System32/OpenSSH/ssh.exe",
        r"C:\Users\First Last\.ssh\id_ed25519", port=2222, connect_timeout=7,
    )
    argv = build_diagnostic_argv(config)
    assert argv[-1] == "qcl"
    assert not any(command in argv for command in (
        "cluster-envs", "cluster-status", "cluster-history", "env", "groups",
    ))
    assert argv[0] == config.ssh_executable
    assert argv[argv.index("-i") + 1] == config.identity_file
    assert argv[argv.index("-p") + 1] == "2222"
    assert argv[argv.index("-l") + 1] == "researcher"
    assert {"-vv", "-N", "BatchMode=yes", "StrictHostKeyChecking=yes",
            "ConnectTimeout=7", "IdentitiesOnly=yes", "SessionType=none",
            "PreferredAuthentications=publickey", "ControlMaster=no", "ControlPath=none",
            "ClearAllForwardings=yes", "PermitLocalCommand=no"}.issubset(argv)


@pytest.mark.parametrize("success", [SUCCESS, "debug1: Authentication succeeded (publickey)."])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
async def test_success_reports_accepted_key_and_immediately_kills_without_session(
    monkeypatch, success, newline,
):
    log = newline.join([
        "debug1: Connection established.",
        "debug1: Authenticating to qcl:22 as 'researcher'",
        ACCEPTED,
        success,
        "",
    ])
    process = FakeProcess(stderr=log.encode(), hanging=True)
    launch = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    result = await asyncio.wait_for(diagnose(ConnectionConfig("qcl")), timeout=0.5)
    assert result == DiagnosticResult(
        "qcl", 22, "researcher", "connected", "authenticated", FINGERPRINT, "ED25519",
        "Public-key authentication succeeded; no remote session was opened.",
    )
    assert result.ok and process.killed and process.waited
    assert "/private" not in repr(asdict(result))
    assert launch.call_args.kwargs["stdin"] == asyncio.subprocess.DEVNULL
    assert "shell" not in launch.call_args.kwargs


async def test_success_across_stream_chunks_uses_last_accepted_key_before_success(monkeypatch):
    process = FakeProcess(hanging=True)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    task = asyncio.create_task(diagnose(ConnectionConfig("qcl", "configured-user")))
    for fragment in [
        ACCEPTED + "\n",
        "debug1: Authenticating to qcl:22 as 'other-user'\n",
        "debug1: Server accepts key: C:\\Users\\Me\\new key RSA SHA256:newfingerprint agent\r\n",
        'Authenticated to qcl using "public',
        'key".\r\n',
        "debug1: Server accepts key: later ED25519 SHA256:later agent\n",
    ]:
        process.stderr.feed_data(fragment.encode())
        await asyncio.sleep(0)
    result = await task
    assert result.ok and result.key_fingerprint == "SHA256:newfingerprint"
    assert result.key_type == "RSA" and result.user == "configured-user"


@pytest.mark.parametrize("log", [
    SUCCESS,
    ACCEPTED + "\ndebug1: Server accepts key: unidentifiable\n" + SUCCESS,
    "debug1: Server host key: ssh-ed25519 " + FINGERPRINT + "\n" + SUCCESS,
])
async def test_authenticated_without_identifiable_current_key_has_no_fingerprint(monkeypatch, log):
    process = FakeProcess(stderr=log.encode())
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    result = await diagnose(ConnectionConfig("qcl"))
    assert result.ok and result.key_fingerprint is None and result.key_type is None


@pytest.mark.parametrize("tail", [
    "", "qcl: Permission denied (publickey).",
    'Authenticated to qcl using "password".',
    "debug1: Authenticated using publickey with partial success.",
])
async def test_accepted_key_alone_never_authenticates_or_exposes_key(monkeypatch, tail):
    process = FakeProcess(stderr=(ACCEPTED + "\n" + tail).encode())
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    result = await diagnose(ConnectionConfig("qcl"))
    assert not result.ok and result.key_fingerprint is None and result.key_type is None
    assert "key path" not in result.detail


@pytest.mark.parametrize("log,connection,authentication,cause", [
    ("qcl: Permission denied (publickey).", "connected", "failed", "Public-key"),
    ("Host key verification failed.", "connected", "unknown", "host key verification"),
    ("WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!", "connected", "unknown", "host key"),
    ("ssh: Could not resolve hostname private-host: Name not known", "failed", "unknown",
     "hostname"),
    ("connect to host private-host port 22: Connection refused", "failed", "unknown", "refused"),
    ("connect to host private-host port 22: Connection timed out", "failed", "unknown", "reach"),
    ("connect to host private-host port 22: No route to host", "failed", "unknown", "reach"),
])
async def test_failures_are_distinct_and_only_return_safe_causes(
    monkeypatch, log, connection, authentication, cause,
):
    process = FakeProcess(stderr=(log + "\nsecret raw /private/path").encode())
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    result = await diagnose(ConnectionConfig("qcl"))
    assert (result.connection, result.authentication) == (connection, authentication)
    assert cause in result.detail
    assert not result.ok and "secret raw" not in result.detail and "private" not in result.detail
    assert process.waited


async def test_missing_executable_returns_actionable_safe_result(monkeypatch):
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(
        side_effect=FileNotFoundError("/private/ssh was not found"),
    ))
    result = await diagnose(ConnectionConfig("qcl"))
    assert result.connection == "failed" and result.authentication == "unknown"
    assert "SSH executable" in result.detail and "/private" not in result.detail


@pytest.mark.parametrize("connected", [False, True])
async def test_timeout_preserves_connection_evidence_and_reaps(monkeypatch, connected):
    process = FakeProcess(
        stderr=b"debug1: Connection established.\n" if connected else b"", hanging=True,
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    result = await diagnose(ConnectionConfig("qcl", timeout=1))
    assert result.connection == ("connected" if connected else "failed")
    assert result.authentication == "unknown" and "timed out" in result.detail
    assert process.killed and process.waited


async def test_combined_output_limit_terminates_and_reaps(monkeypatch):
    process = FakeProcess(stdout=b"a" * (MAX_OUTPUT_BYTES // 2 + 1),
                          stderr=b"b" * (MAX_OUTPUT_BYTES // 2), hanging=True)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    result = await diagnose(ConnectionConfig("qcl"))
    assert "1 MiB" in result.detail and not result.ok
    assert process.killed and process.waited


async def test_cancellation_reaps_process(monkeypatch):
    process = FakeProcess(hanging=True)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    task = asyncio.create_task(diagnose(ConnectionConfig("qcl")))
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
    task = asyncio.create_task(diagnose(ConnectionConfig("qcl")))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await asyncio.sleep(0.01)
    assert process.killed and process.waited


@pytest.mark.skipif(os.name != "posix", reason="Executable fixture uses a POSIX shebang")
@pytest.mark.parametrize("mode", ["success", "overflow", "timeout", "cancel"])
async def test_local_fake_executable_uses_real_pipes_and_is_reaped(tmp_path, monkeypatch, mode):
    executable = tmp_path / "fake_ssh.py"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, time\n"
        "assert sys.argv[-1] in ('success', 'overflow', 'timeout', 'cancel')\n"
        "assert 'cluster-envs' not in sys.argv\n"
        "if sys.argv[-1] == 'success':\n"
        f"    os.write(2, {(ACCEPTED + chr(10) + SUCCESS + chr(10)).encode()!r})\n"
        "elif sys.argv[-1] == 'overflow':\n"
        "    while True:\n"
        "        os.write(1, b'x' * 65536)\n"
        "time.sleep(30)\n"
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
    task = asyncio.create_task(diagnose(ConnectionConfig(
        mode, ssh_executable=str(executable), timeout=1,
    )))
    await launched.wait()
    if mode == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=3)
    else:
        result = await asyncio.wait_for(task, timeout=3)
        assert result.ok == (mode == "success")
        if mode != "success":
            assert ("1 MiB" if mode == "overflow" else "timed out") in result.detail
    # Cancellation can race the shielded launch; its callback still reaps the child.
    assert await asyncio.wait_for(processes[0].wait(), timeout=2) != 0
    assert processes[0].returncode is not None
