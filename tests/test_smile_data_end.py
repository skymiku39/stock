"""資料中斷的標的（下市、合併）不讓部位凍結：以最後收盤價強制平倉並標記（#34）。"""
import datetime as dt

import pytest

from bot.config import Settings
from bot.round_metrics import summarize_rounds
from bot.smile_curve_backtest import SmileCurveBacktester
from bot.smile_execution import SmileExecution
from bot.stock_db import PriceBar

START = dt.date(2026, 1, 1)


def bars(symbol, closes):
    return [PriceBar(symbol, (START + dt.timedelta(days=i)).isoformat(), open=c, high=c + 1,
                     low=c - 1, close=c, volume=100000) for i, c in enumerate(closes)]


def run(histories, *, data_end_sessions=20, **overrides):
    values = dict(_env_file=None, max_fund=500000, daily_fund_budget=0,
                  smile_buy_tiers="1:1,50:1", smile_base_lot=1, max_lot_per_symbol=1,
                  broker_fee_discount=0, broker_min_fee=0, smile_regular_tax_rate=0,
                  smile_symbol_stop_loss_pct=0, smile_max_unrealized_loss_pct=0)
    values.update(overrides)
    return SmileCurveBacktester(Settings(**values), execution=SmileExecution.ideal(),
                                data_end_sessions=data_end_sessions).run_histories(histories)


def test_position_in_ended_symbol_is_closed_at_last_close():
    # A：第 2 天跌 2% 觸發買進，第 3 天開盤 98 成交，第 4 天收 90 後就沒有資料。
    h = {"A": bars("A", [100, 98, 98, 90]), "B": bars("B", [50] * 40)}
    r = run(h)
    record, = r.data_ended
    assert record == dict(symbol="A", last_date="2026-01-04", sessions_after=36,
                          forced_exit_date="2026-01-05", exit_price=90, lot_count=1,
                          pnl_twd=-8000)
    trip, = r.all_round_trips
    assert (trip.exit_date, trip.exit_price, trip.exit_reason) == ("2026-01-05", 90, "data_end")
    assert r.cash == 500000 - 8000 and r.net_pnl_twd == -8000 and r.unrealized_pnl_twd == 0
    assert r.results[0].open_lots == 0
    # 平倉後權益不再靠過期的價格估值。
    assert all(not p["stale_marks"] for p in r.equity_curve if p["date"] >= "2026-01-05")
    assert r.equity_curve[-1]["equity"] == 492000
    assert summarize_rounds(r.rounds)["exit_reason_counts"] == {"data_end": 1}
    payload = r.to_dict()
    assert payload["data_ended"] == [record]
    assert payload["open_positions"]["count"] == 0


def test_ended_symbol_without_position_is_listed_only():
    r = run({"A": bars("A", [100, 100, 100]), "B": bars("B", [50] * 40)})
    record, = r.data_ended
    assert (record["symbol"], record["forced_exit_date"], record["lot_count"]) == ("A", None, 0)
    assert r.all_round_trips == [] and r.cash == 500000


def test_short_gap_or_disabled_check_leaves_position_alone():
    h = {"A": bars("A", [100, 98, 98, 90]), "B": bars("B", [50] * 24)}  # A 之後只有 20 個交易日
    for result in (run(h), run({**h, "B": bars("B", [50] * 40)}, data_end_sessions=0)):
        assert result.data_ended == []
        assert result.results[0].open_lots == 1
        assert result.unrealized_pnl_twd == -8000


def test_forced_exit_pays_slippage_fees_and_tax():
    h = {"1101": bars("1101", [100, 98, 98, 90]), "1102": bars("1102", [50] * 40)}
    settings = Settings(_env_file=None, max_fund=500000, daily_fund_budget=0,
                        smile_buy_tiers="1:1,50:1", smile_base_lot=1, max_lot_per_symbol=1,
                        smile_symbol_stop_loss_pct=0, smile_max_unrealized_loss_pct=0)
    r = SmileCurveBacktester(settings, execution=SmileExecution(slippage_bps=10, participation=1)
                             ).run_histories(h)
    record, = r.data_ended
    assert record["exit_price"] < 90  # 賣出滑價
    trip, = r.all_round_trips
    assert trip.pnl_twd == pytest.approx(record["pnl_twd"])
    assert trip.pnl_twd < (record["exit_price"] - trip.entry_price) * 1000  # 另扣手續費與證交稅
    assert r.equity_curve[-1]["equity"] == pytest.approx(500000 + trip.pnl_twd)
