"""Release notices run in the background and never install while the UI is open."""

import threading
from unittest.mock import Mock

import pytest
from textual.widgets import Input

from fe_cli import app as app_module
from fe_cli.app import FactorTerminal
from fe_cli.backend import DemoBackend
from fe_cli.updates import ReleaseInfo, UpdateError


@pytest.mark.parametrize("latest,expected", [("0.2.0", True), ("0.1.0", False), ("0.0.9", False)])
async def test_update_notice_only_for_newer_release(tmp_path, monkeypatch, latest, expected):
    monkeypatch.setattr(app_module, "__version__", "0.1.0")
    checker = Mock()
    checker.check.return_value = ReleaseInfo(latest, "a" * 64)
    app = FactorTerminal(backend=DemoBackend(tmp_path), update_checker=checker)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        notice = "CLI update available:" in "\n".join(app._transcript_text)
        assert notice is expected
        if expected:
            assert "fe-cli --update" in "\n".join(app._transcript_text)
        assert not app._query_pending
    checker.check.assert_called_once_with()


async def test_failed_background_check_keeps_terminal_usable(tmp_path):
    checker = Mock()
    checker.check.side_effect = UpdateError("network unavailable")
    app = FactorTerminal(backend=DemoBackend(tmp_path), update_checker=checker)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.press("x")
        assert app.query_one("#command", Input).value == "x"
        assert "network unavailable" not in "\n".join(app._transcript_text)


async def test_slow_check_does_not_block_command_input(tmp_path):
    started, finish = threading.Event(), threading.Event()

    def slow_check():
        started.set()
        assert finish.wait(5), "test must release the background check"
        return None

    checker = Mock()
    checker.check.side_effect = slow_check
    app = FactorTerminal(backend=DemoBackend(tmp_path), update_checker=checker)
    async with app.run_test() as pilot:
        try:
            await pilot.press("x")
            assert started.is_set()
            assert app.query_one("#command", Input).value == "x"
        finally:
            finish.set()
        await app.workers.wait_for_complete()
