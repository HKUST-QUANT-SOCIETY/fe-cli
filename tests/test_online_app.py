import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from textual.widgets import Input, OptionList, RichLog, Static

from fe_cli.app import FactorTerminal
from fe_cli.config import ConnectionConfig
from fe_cli.diagnostics import DiagnosticResult
from fe_cli.remote import RemoteResult


def client(query=None):
    return SimpleNamespace(
        config=ConnectionConfig(host="cluster.example", user="researcher"),
        query=query or AsyncMock(return_value=RemoteResult("cluster-status", "server output", "")),
        diagnose=AsyncMock(
            return_value=DiagnosticResult(
                "cluster.example", 22, "researcher", "connected", "authenticated"
            )
        ),
    )


def transcript(app):
    return "\n".join(line.text for line in app.query_one("#transcript", RichLog).lines)


async def command(app, pilot, value):
    app.query_one("#command", Input).value = value
    await pilot.press("enter")


async def test_online_startup_has_no_connection_or_demo_state(monkeypatch):
    backend = Mock(side_effect=AssertionError("Demo backend must stay closed"))
    monkeypatch.setattr("fe_cli.app.DemoBackend", backend)
    remote = client()
    app = FactorTerminal(online=remote)
    async with app.run_test(size=(80, 24)) as pilot:
        assert "ONLINE" in str(app.query_one("#mode", Static).render())
        assert "Not checked" in str(app.query_one("#mode", Static).render())
        assert app.backend is None
        remote.query.assert_not_called()
        backend.assert_not_called()
        await pilot.press("/")
        assert app.query_one("#commands", OptionList).option_count == 15
        assert "/history-full" not in app.commands
        assert "/run" in app.commands
        assert "/status" in app.commands
        assert "/setup" not in app.commands


@pytest.mark.parametrize(
    "local,remote_command",
    [
        ("/envs", "cluster-envs"),
        ("/jobs", "cluster-status"),
        ("/history", "cluster-history"),
        ("/history full", "cluster-history"),
    ],
)
async def test_online_command_routing_and_literal_output(local, remote_command):
    remote = client(
        AsyncMock(
            return_value=RemoteResult(
                remote_command, "[red]server output[/red]\n", "read-only notice\n"
            )
        )
    )
    app = FactorTerminal(online=remote)
    async with app.run_test() as pilot:
        await command(app, pilot, local)
        await app.workers.wait_for_complete()
        await pilot.pause()
        remote.query.assert_awaited_once_with(remote_command)
        assert "[red]server output[/red]" in transcript(app)
        assert "SSH notice: read-only notice" in transcript(app)
        assert "Last query succeeded" in str(app.query_one("#mode", Static).render())
        assert not app._query_pending
        assert app.query_one("#command", Input).has_focus


async def test_online_query_keeps_ui_responsive_and_avoids_duplicates():
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_query(value):
        started.set()
        await release.wait()
        return RemoteResult(value, "finished", "")

    remote = client(AsyncMock(side_effect=slow_query))
    app = FactorTerminal(online=remote)
    async with app.run_test() as pilot:
        await command(app, pilot, "/jobs")
        await asyncio.wait_for(started.wait(), timeout=2)
        assert "Querying" in str(app.query_one("#mode", Static).render())
        await command(app, pilot, "/help")
        assert "Query server job history" in transcript(app)
        await command(app, pilot, "/history")
        assert "A server query is in progress" in transcript(app)
        remote.query.assert_awaited_once_with("cluster-status")
        release.set()
        await app.workers.wait_for_complete()
        assert "finished" in transcript(app)


async def test_online_query_failure_then_recovery():
    remote = client(
        AsyncMock(
            side_effect=[
                ValueError("Permission denied"),
                RemoteResult("cluster-envs", "research", ""),
            ]
        )
    )
    app = FactorTerminal(online=remote)
    async with app.run_test() as pilot:
        await command(app, pilot, "/envs")
        await app.workers.wait_for_complete()
        assert "Query failed: Permission denied" in transcript(app)
        assert "Last query failed" in str(app.query_one("#mode", Static).render())
        await command(app, pilot, "/envs")
        await app.workers.wait_for_complete()
        assert "FE installation and data access remain unverified" in transcript(app)
        assert "Last query succeeded" in str(app.query_one("#mode", Static).render())


@pytest.mark.parametrize("value", ["/cancel 123", "/data"])
async def test_online_unavailable_commands_never_create_demo_or_query(value):
    remote = client()
    app = FactorTerminal(online=remote)
    async with app.run_test() as pilot:
        await command(app, pilot, value)
        await pilot.press("ctrl+n")
        assert len(app.screen_stack) == 1
        assert "unavailable" in transcript(app)
        remote.query.assert_not_called()
        assert app.backend is None


async def test_online_query_is_cancelled_on_exit():
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def pending(value):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    app = FactorTerminal(online=client(AsyncMock(side_effect=pending)))
    async with app.run_test() as pilot:
        await command(app, pilot, "/jobs")
        await asyncio.wait_for(started.wait(), timeout=2)
        await command(app, pilot, "/quit")
    assert cancelled.is_set()


@pytest.mark.parametrize("value,expected", [("/history", 10), ("/history full", 13)])
async def test_history_recent_and_full_records(value, expected):
    output = "JobID JobName State\n----- ------- -----\n" + "".join(
        f"{number:04d} job{number} COMPLETED\n" for number in range(1, 14)
    )
    remote = client(AsyncMock(return_value=RemoteResult("cluster-history", output, "")))
    app = FactorTerminal(online=remote)
    async with app.run_test() as pilot:
        await command(app, pilot, value)
        await app.workers.wait_for_complete()
        lines = transcript(app).splitlines()
        records = [line for line in lines if line.split() and line.split()[0].isdigit()]
        assert len(records) == expected
        assert any("0013" in line for line in records)
        assert any("0001" in line for line in records) == (expected == 13)
        remote.query.assert_awaited_once_with("cluster-history")


@pytest.mark.parametrize("value", ["/history-full", "/history typo", "/history full extra"])
async def test_invalid_history_command_never_queries(value):
    remote = client()
    app = FactorTerminal(online=remote)
    async with app.run_test() as pilot:
        await command(app, pilot, value)
        assert "Error:" in transcript(app)
        remote.query.assert_not_called()


@pytest.mark.parametrize(
    "connection,authentication",
    [("connected", "authenticated"), ("connected", "failed"), ("failed", "unknown")],
)
@pytest.mark.parametrize("value", ["/status", "/setup"])
async def test_status_diagnoses_without_environment_query(value, connection, authentication):
    remote = client()
    remote.diagnose.return_value = DiagnosticResult(
        "cluster.example",
        22,
        "researcher",
        connection,
        authentication,
        key_fingerprint="SHA256:accepted-test-key",
        key_type="ED25519",
        detail="Check complete.",
    )
    app = FactorTerminal(online=remote)
    async with app.run_test() as pilot:
        await command(app, pilot, value)
        await app.workers.wait_for_complete()
        output = transcript(app)
        assert "SSH authorization status" in output
        assert authentication in output
        assert "cluster.example:22" in output
        assert ("SHA256:accepted-test-key" in output) == (authentication == "authenticated")
        assert "registered environments" not in output
        remote.diagnose.assert_awaited_once_with()
        remote.query.assert_not_called()
        assert not app._query_pending


@pytest.mark.parametrize("value", ["/status extra", "/setup extra"])
async def test_status_rejects_arguments_without_remote_calls(value):
    remote = client()
    app = FactorTerminal(online=remote)
    async with app.run_test() as pilot:
        await command(app, pilot, value)
        assert "/status takes no arguments." in transcript(app)
        remote.diagnose.assert_not_called()
        remote.query.assert_not_called()


async def test_status_is_the_visible_online_help_command():
    app = FactorTerminal(online=client())
    async with app.run_test() as pilot:
        await command(app, pilot, "/help")
        output = transcript(app)
        assert "/status" in output
        assert "Check SSH authorization status" in output
        assert "/setup" not in output
