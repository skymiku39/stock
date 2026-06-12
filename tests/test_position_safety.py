"""position_safety 啟動安全檢查測試。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bot.config import Settings
from bot.portfolio import (
    BrokerPosition,
    BrokerPositionsSnapshot,
    PortfolioPosition,
)
from bot.position_safety import (
    audit_manual_overlap,
    reconcile_broker_vs_bot_records,
    run_startup_safety_checks,
    symbols_to_exclude_for_trading,
)


def _snapshot(positions: list[tuple[str, float]]) -> BrokerPositionsSnapshot:
    return BrokerPositionsSnapshot(
        broker="shioaji",
        account="test",
        asof="",
        simulation=True,
        positions=[
            BrokerPosition(symbol=s, qty=q) for s, q in positions
        ],
    )


def test_audit_manual_overlap_detects_mixed_holding(tmp_path: Path, monkeypatch) -> None:
    trades = tmp_path / "data"
    trades.mkdir()
    (trades / "trades_2026.csv").write_text(
        "symbol,side,qty,price,owner_tag\n"
        "2330,buy,1,900,AI\n",
        encoding="utf-8",
    )
    settings = Settings(symbols=["2330", "0050"])

    monkeypatch.chdir(tmp_path)
    issues = audit_manual_overlap(
        settings,
        broker_snapshot=_snapshot([("2330", 2.0)]),
    )
    assert len(issues) == 1
    assert issues[0].symbol == "2330"
    assert issues[0].manual_qty == pytest.approx(1.0)


def test_reconcile_bot_exceeds_broker(tmp_path: Path, monkeypatch) -> None:
    trades = tmp_path / "data"
    trades.mkdir()
    (trades / "trades_2026.csv").write_text(
        "symbol,side,qty,price,owner_tag\n"
        "0050,buy,2,180,AI\n",
        encoding="utf-8",
    )
    settings = Settings(symbols=["0050"])
    monkeypatch.chdir(tmp_path)

    issues = reconcile_broker_vs_bot_records(
        settings,
        broker_snapshot=_snapshot([("0050", 1.0)]),
    )
    assert len(issues) == 1
    assert issues[0].kind == "bot_exceeds_broker"


def test_manual_hold_symbols_merged_into_blacklist() -> None:
    settings = Settings(
        blacklist_symbols=["2498"],
        manual_hold_symbols=["2330"],
    )
    excluded = symbols_to_exclude_for_trading(settings)
    assert excluded == ["2330", "2498"]


def test_startup_safety_engages_kill_switch_on_overlap(tmp_path: Path, monkeypatch) -> None:
    trades = tmp_path / "data"
    trades.mkdir()
    (trades / "trades_2026.csv").write_text(
        "symbol,side,qty,price,owner_tag\n"
        "2330,buy,1,900,AI\n",
        encoding="utf-8",
    )
    settings = Settings(symbols=["2330"])
    monkeypatch.chdir(tmp_path)

    risk = MagicMock()
    report = run_startup_safety_checks(
        settings,
        broker=None,
        risk=risk,
        engage_kill_switch=True,
        broker_snapshot=_snapshot([("2330", 2.0)]),
    )
    assert not report.ok
    risk.engage_kill_switch.assert_called_once()


def test_manual_hold_skips_overlap_block(tmp_path: Path, monkeypatch) -> None:
    trades = tmp_path / "data"
    trades.mkdir()
    (trades / "trades_2026.csv").write_text(
        "symbol,side,qty,price,owner_tag\n"
        "2330,buy,1,900,AI\n",
        encoding="utf-8",
    )
    settings = Settings(symbols=["2330"], manual_hold_symbols=["2330"])
    monkeypatch.chdir(tmp_path)

    risk = MagicMock()
    report = run_startup_safety_checks(
        settings,
        risk=risk,
        broker_snapshot=_snapshot([("2330", 2.0)]),
    )
    assert report.ok
    risk.engage_kill_switch.assert_not_called()


def test_startup_broker_fetch_fail_closed(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(symbols=["2330"], _env_file=None)  # type: ignore[call-arg]
    monkeypatch.chdir(tmp_path)
    risk = MagicMock()

    report = run_startup_safety_checks(
        settings,
        broker=None,
        broker_snapshot=None,
        risk=risk,
        require_broker_snapshot=True,
    )

    assert not report.ok
    assert report.broker_fetch_error
    risk.engage_kill_switch.assert_called_once()


def test_startup_ok_with_empty_broker_snapshot(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(symbols=["2330"], _env_file=None)  # type: ignore[call-arg]
    monkeypatch.chdir(tmp_path)
    risk = MagicMock()

    report = run_startup_safety_checks(
        settings,
        risk=risk,
        broker_snapshot=_snapshot([]),
    )

    assert report.ok
    risk.engage_kill_switch.assert_not_called()
