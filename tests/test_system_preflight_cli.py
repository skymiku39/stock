"""stock-system-preflight CLI 輸出與阻擋契約。"""

from __future__ import annotations

import json
from types import SimpleNamespace

from bot.system_preflight import SystemCheck, SystemPreflightReport
from bot.system_preflight_cli import main


def test_json_output_is_machine_readable(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        "bot.system_preflight_cli.Settings",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "bot.system_preflight_cli.run_system_preflight",
        lambda *args, **kwargs: SystemPreflightReport(
            fetched_at="2026-09-20T12:00:00+08:00",
            root=str(tmp_path),
            status="ready",
            checks=[SystemCheck("SQLite 完整性", "ok", "ok")],
            summary="ready",
        ),
    )

    assert main(["--root", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ready"
    assert payload["checks"][0]["name"] == "SQLite 完整性"


def test_blocked_report_returns_exit_two(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        "bot.system_preflight_cli.Settings",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "bot.system_preflight_cli.run_system_preflight",
        lambda *args, **kwargs: SystemPreflightReport(
            fetched_at="2026-09-20T12:00:00+08:00",
            root=str(tmp_path),
            status="blocked",
            checks=[SystemCheck("SQLite 完整性", "fail", "broken")],
            summary="blocked",
        ),
    )

    assert main(["--root", str(tmp_path)]) == 2
    assert "[FAIL]" in capsys.readouterr().out
