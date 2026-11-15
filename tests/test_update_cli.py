"""Update commands are explicit and independent of workspaces and SSH."""

import sys
from types import ModuleType
from unittest.mock import Mock

import pytest

from fe_cli import cli
from fe_cli.config import ConnectionConfig
from fe_cli.updates import ReleaseInfo, UpdateError


@pytest.fixture
def update_service(monkeypatch):
    monkeypatch.setattr(cli, "__version__", "0.1.0")
    service = Mock()
    service.check.return_value = ReleaseInfo("0.2.0", "a" * 64)
    monkeypatch.setattr(cli, "UpdateChecker", Mock(return_value=service))
    module = ModuleType("fe_cli.self_update")
    module.run_update = Mock()
    monkeypatch.setitem(sys.modules, "fe_cli.self_update", module)
    monkeypatch.setattr(cli, "DemoBackend", Mock(side_effect=AssertionError("no state")))
    monkeypatch.setattr(cli, "load_config", Mock(side_effect=AssertionError("no SSH config")))
    return service, module.run_update


def test_explicit_check_reports_version_without_installing(update_service, capsys):
    service, install = update_service
    assert cli.main(["--online", "--check-update"]) == 0
    service.check.assert_called_once_with(force=True)
    install.assert_not_called()
    output = capsys.readouterr().out
    assert "0.1.0" in output and "0.2.0" in output and "fe-cli --update" in output


def test_explicit_update_installs_and_requests_restart(update_service, capsys):
    service, install = update_service
    assert cli.main(["--update"]) == 0
    install.assert_called_once_with(service.check.return_value)
    assert "Run fe-cli again" in capsys.readouterr().out


@pytest.mark.parametrize("latest", ["0.1.0", "0.0.9"])
def test_update_never_reinstalls_or_downgrades(update_service, capsys, latest):
    service, install = update_service
    service.check.return_value = ReleaseInfo(latest, "a" * 64)
    assert cli.main(["--update"]) == 0
    install.assert_not_called()
    assert "No newer stable" in capsys.readouterr().out


@pytest.mark.parametrize("failure", ["check", "install"])
def test_update_failure_has_nonzero_exit_without_false_success(update_service, capsys, failure):
    service, install = update_service
    (service.check if failure == "check" else install).side_effect = UpdateError("fixture failure")
    assert cli.main(["--update"]) == 1
    output = capsys.readouterr()
    assert "fixture failure" in output.err
    assert "Updated to" not in output.out and "Traceback" not in output.err


@pytest.mark.parametrize("args", [["--update", "--check-update"], ["--update", "run"],
                                  ["--check-update", "jobs"]])
def test_maintenance_flags_reject_ambiguous_commands(update_service, args):
    service, install = update_service
    with pytest.raises(SystemExit) as result:
        cli.main(args)
    assert result.value.code == 2
    service.check.assert_not_called()
    install.assert_not_called()


def test_background_opt_out_keeps_explicit_check_available(update_service, monkeypatch):
    service, _ = update_service
    monkeypatch.setenv("FE_CLI_NO_UPDATE_CHECK", "1")
    assert cli.main(["--no-update-check", "--check-update"]) == 0
    service.check.assert_called_once_with(force=True)


@pytest.mark.parametrize("args", [["--version"], ["--help"]])
def test_informational_flags_never_check_network(monkeypatch, args):
    monkeypatch.setattr(cli, "UpdateChecker", Mock(side_effect=AssertionError("no network")))
    with pytest.raises(SystemExit) as result:
        cli.main(args)
    assert result.value.code == 0


def test_json_command_never_constructs_update_service(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "UpdateChecker", Mock(side_effect=AssertionError("no network")))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert cli.main(["--state-dir", str(tmp_path / "state"), "jobs"]) == 0
    assert capsys.readouterr().out.startswith('{"demo": true, "jobs":')


@pytest.mark.parametrize("mode", [[], ["--online"]])
@pytest.mark.parametrize("opt_out", [None, "flag", "env"])
def test_only_terminal_launch_receives_background_checker(tmp_path, monkeypatch, mode, opt_out):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("FE_CLI_NO_UPDATE_CHECK", raising=False)
    if opt_out == "env":
        monkeypatch.setenv("FE_CLI_NO_UPDATE_CHECK", "1")
    checker = Mock()
    factory = Mock(return_value=checker)
    monkeypatch.setattr(cli, "UpdateChecker", factory)
    module = ModuleType("fe_cli.app")
    module.FactorTerminal = Mock()
    monkeypatch.setitem(sys.modules, "fe_cli.app", module)
    monkeypatch.setattr(cli, "load_config", lambda _: ConnectionConfig("fixture.invalid"))
    args = ["--state-dir", str(tmp_path / "state"), *mode]
    if opt_out == "flag":
        args.append("--no-update-check")
    assert cli.main(args) == 0
    assert module.FactorTerminal.call_args.kwargs["update_checker"] is (
        checker if opt_out is None else None
    )
    checker.check.assert_not_called()
