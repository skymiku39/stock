from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from bot.portfolio import (
    PortfolioTrade,
    broker_position_from_shioaji,
    build_positions,
    classify_bot_ownership,
    load_bot_portfolio,
    load_portfolio,
)
from bot.ticker_view import _scan_trades_for


def test_build_positions_uses_fifo_cost_basis() -> None:
    positions = build_positions([
        PortfolioTrade(symbol="2330", side="buy", qty=2, price=100),
        PortfolioTrade(symbol="2330", side="buy", qty=1, price=120),
        PortfolioTrade(symbol="2330", side="sell", qty=1, price=110),
    ])

    pos = positions["2330"]

    assert pos.qty == pytest.approx(2)
    assert pos.avg_cost == pytest.approx(110)
    assert pos.cost_basis == pytest.approx(220_000)


def test_load_portfolio_reads_trade_csv_variants(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "trades_2026-06-03.csv").write_text(
        "datetime,code,action,quantity,price,note\n"
        "2026-06-03 09:01,0050,Buy,2,100,entry\n"
        "2026-06-03 10:31,0050,Sell,1,108,trim\n"
        "2026-06-03 11:02,2330,Buy,1,900,entry\n",
        encoding="utf-8",
    )

    trades, positions = load_portfolio(tmp_path)

    assert [t.symbol for t in trades] == ["0050", "0050", "2330"]
    assert positions["0050"].qty == pytest.approx(1)
    assert positions["0050"].avg_cost == pytest.approx(100)
    assert positions["2330"].cost_basis == pytest.approx(900_000)


def test_load_bot_portfolio_uses_ai_owner_tag_only(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "trades_2026-06-03.csv").write_text(
        "datetime,code,action,quantity,price,custom_field,owner_tag\n"
        "2026-06-03 09:01,0050,Buy,2,100,AIBUY,AI\n"
        "2026-06-03 09:05,0050,Buy,3,101,manual,\n"
        "2026-06-03 10:31,0050,Sell,1,108,AITP,AI\n",
        encoding="utf-8",
    )

    trades, positions = load_bot_portfolio(tmp_path)

    assert len(trades) == 3
    assert positions["0050"].qty == pytest.approx(1)
    assert positions["0050"].avg_cost == pytest.approx(100)
    assert positions["0050"].owner_tag == "AI"


def test_build_positions_owner_filter_ignores_manual_trades() -> None:
    positions = build_positions(
        [
            PortfolioTrade(symbol="2330", side="buy", qty=2, price=100, owner_tag="AI"),
            PortfolioTrade(symbol="2330", side="buy", qty=5, price=90, owner_tag=""),
            PortfolioTrade(symbol="2330", side="sell", qty=1, price=110, owner_tag="AI"),
        ],
        owner_filter="AI",
    )

    assert positions["2330"].qty == pytest.approx(1)
    assert positions["2330"].avg_cost == pytest.approx(100)


def test_ticker_view_scan_trades_reuses_fifo_portfolio(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "trades_2026-06-03.csv").write_text(
        "ts,ticker,side,qty,price\n"
        "09:00,2330,buy,2,100\n"
        "10:00,2330,sell,1,110\n",
        encoding="utf-8",
    )

    trades, qty, avg = _scan_trades_for("2330", tmp_path)

    assert len(trades) == 2
    assert qty == pytest.approx(1)
    assert avg == pytest.approx(100)


def test_classify_bot_ownership_marks_full_partial_and_external() -> None:
    bot_positions = build_positions([
        PortfolioTrade(symbol="2330", side="buy", qty=2, price=100),
    ])

    assert classify_bot_ownership(2, bot_positions["2330"]).label == "本工具"
    partial = classify_bot_ownership(3, bot_positions["2330"])
    assert partial.label == "部分本工具"
    assert partial.bot_qty == pytest.approx(2)
    assert partial.manual_qty == pytest.approx(1)
    assert classify_bot_ownership(1, None).label == "非本工具"


def test_broker_position_from_shioaji_normalizes_fields() -> None:
    raw = SimpleNamespace(
        id=7,
        code="2330",
        quantity=2,
        price=900.0,
        last_price=930.0,
        pnl=60_000.0,
        direction=SimpleNamespace(value="Buy"),
        yd_quantity=1,
        cond=SimpleNamespace(value="Cash"),
    )

    pos = broker_position_from_shioaji(raw, account="9A95-***1234")

    assert pos.symbol == "2330"
    assert pos.qty == pytest.approx(2)
    assert pos.avg_price == pytest.approx(900)
    assert pos.market_value == pytest.approx(1_860_000)
    assert pos.cost_basis == pytest.approx(1_800_000)
    assert pos.direction == "Buy"
    assert pos.cond == "Cash"
    assert pos.account == "9A95-***1234"
