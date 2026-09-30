from __future__ import annotations

import sys
import importlib

import pytest

from bot.dashboard import run as dashboard_run


def test_dashboard_help_is_handled_before_streamlit(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["stock-dashboard", "--help"])

    with pytest.raises(SystemExit) as exc_info:
        dashboard_run()

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "啟動 Stock Bot Streamlit 儀表板" in output


def test_dashboard_forwards_streamlit_server_flags(monkeypatch) -> None:
    runner = importlib.import_module("bot.dashboard.run")
    commands: list[list[str]] = []
    monkeypatch.setattr(sys, "argv", [
        "stock-dashboard", "--server.port", "8797", "--server.headless", "true",
    ])
    monkeypatch.setattr(runner.subprocess, "call", lambda command: commands.append(command) or 0)

    with pytest.raises(SystemExit) as exc_info:
        dashboard_run()

    assert exc_info.value.code == 0
    assert commands
    assert "--" not in commands[0]
    assert commands[0][-4:] == ["--server.port", "8797", "--server.headless", "true"]


def test_dashboard_root_is_consumed_and_not_forwarded_to_streamlit(
    monkeypatch, tmp_path,
) -> None:
    runner = importlib.import_module("bot.dashboard.run")
    commands: list[list[str]] = []
    monkeypatch.setattr(sys, "argv", [
        "stock-dashboard", "--root", str(tmp_path), "--server.port", "8798",
    ])
    monkeypatch.setattr(runner.subprocess, "call", lambda command: commands.append(command) or 0)

    with pytest.raises(SystemExit) as exc_info:
        dashboard_run()

    assert exc_info.value.code == 0
    assert commands
    assert "--root" not in commands[0]
    assert commands[0][-2:] == ["--server.port", "8798"]
    assert runner.os.environ["STOCK_DASHBOARD_ROOT"] == str(tmp_path.resolve())
