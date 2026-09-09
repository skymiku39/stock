"""stock-preflight CLI：第 7 區輸出與 exit code 契約。"""

from __future__ import annotations

import json
from typing import List

from bot.preflight import CheckResult, PreflightReport
from bot.preflight_cli import main


def _report(*, fails: bool = False) -> PreflightReport:
    safety_status = "fail" if fails else "ok"
    return PreflightReport(
        fetched_at="2026-09-09T10:00:00+08:00",
        can_trade_now=not fails,
        can_simulate=not fails,
        section_env=[CheckResult("API_KEY", "ok", "已設定")],
        section_safety=[
            CheckResult("持倉對帳", safety_status, "測試用持倉檢查"),
        ],
        summary="測試報告",
    )


def _run_main(
    monkeypatch,
    argv: List[str],
    *,
    fails: bool = False,
) -> int:
    monkeypatch.setattr("bot.preflight_cli.Settings", lambda: object())
    monkeypatch.setattr(
        "bot.preflight_cli.run_preflight",
        lambda *args, **kwargs: _report(fails=fails),
    )
    monkeypatch.setattr("bot.preflight_cli.get_logger", lambda name: object())
    return main(argv)


def test_human_output_includes_section_safety(monkeypatch, capsys) -> None:
    code = _run_main(monkeypatch, ["--no-login"], fails=False)
    out = capsys.readouterr().out
    assert code == 0
    assert "7. 持倉安全" in out
    assert "持倉對帳" in out


def test_human_exit_2_on_fail(monkeypatch, capsys) -> None:
    code = _run_main(monkeypatch, ["--no-login"], fails=True)
    out = capsys.readouterr().out
    assert code == 2
    assert "7. 持倉安全" in out


def test_json_exit_2_on_fail(monkeypatch, capsys) -> None:
    code = _run_main(monkeypatch, ["--no-login", "--json"], fails=True)
    out = capsys.readouterr().out
    assert code == 2
    payload = json.loads(out)
    assert payload["can_simulate"] is False
    assert "safety" in payload["sections"]
    assert any(c["status"] == "fail" for c in payload["sections"]["safety"])


def test_json_exit_0_without_fail(monkeypatch, capsys) -> None:
    code = _run_main(monkeypatch, ["--no-login", "--json"], fails=False)
    out = capsys.readouterr().out
    assert code == 0
    payload = json.loads(out)
    assert payload["can_simulate"] is True
    assert payload["sections"]["safety"]
