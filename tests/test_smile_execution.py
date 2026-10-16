import pytest
from bot.config import Settings
from bot.smile_curve_backtest import SmileCurveBacktester
from bot.smile_execution import SmileExecution, round_tick
from bot.stock_db import PriceBar


def history(prices, volumes):
    return [PriceBar("2330",f"2026-01-{i+1:02d}",open=o,close=c,high=max(o,c),low=min(o,c),volume=v)
            for i,((o,c),v) in enumerate(zip(prices,volumes))]


def run(bars, execution=None, **overrides):
    params = dict(_env_file=None,max_fund=1000000,daily_fund_budget=0,max_lot_per_symbol=5,
                  smile_buy_tiers="1:1,50:1",smile_base_lot=1,
                  broker_fee_discount=0,broker_min_fee=0,smile_regular_tax_rate=0)
    params.update(overrides)
    return SmileCurveBacktester(Settings(**params),execution=execution).run_histories({"2330":bars})


def test_slippage_both_sides_and_mtm():
    bars=history([(100,100),(100,98),(99,102),(103,103)],[100000]*4)
    result=run(bars)
    trade,=result.all_round_trips
    assert trade.entry_price == 99.1
    assert trade.exit_price == 102.5
    assert result.net_pnl_twd == pytest.approx(3400)
    assert result.equity_curve[2]["unrealized_pnl_twd"] == pytest.approx(2400)


def test_volume_cap_uses_previous_day_not_future_day():
    bars=history([(100,100),(100,98),(99,99)],[100000,1000,999999])
    result=run(bars,smile_base_lot=3)
    assert result.orders[0]["filled_quantity"] == 1
    bars[-1].volume=0
    other=run(bars,smile_base_lot=3)
    assert result.orders == other.orders


def test_capacity_shared_across_tiers():
    bars=history([(100,100),(100,94),(94,94)],[100000,1000,100000])
    result=run(bars,smile_buy_tiers="1:1,3:1,50:1")
    assert sum(o["filled_quantity"] for o in result.orders) == 1
    assert result.orders[1]["reason"] == "volume_limit"


def test_sell_insufficient_volume_keeps_whole_lot_and_cash():
    bars=history([(100,100),(100,98),(99,102),(103,103)],[100000,100000,10,100000])
    result=run(bars)
    assert result.orders[-1]["reason"] == "volume_limit_whole_lot_sell"
    assert result.results[0].open_lots == 1
    assert result.total_pnl_twd == 0


@pytest.mark.parametrize("volume",[0,float('nan'),-1])
def test_invalid_volume_blocks(volume):
    bars=history([(100,100),(100,98),(99,99)],[100000,volume,100000])
    assert run(bars).orders[0]["reason"] == "volume_limit"


def test_limit_direction_and_rounding():
    model=SmileExecution()
    assert model.quote(44.65,"buy",40.6)[1] == "limit_up_buy_blocked"
    assert model.quote(36.55,"sell",40.6)[1] == "limit_down_sell_blocked"
    assert model.quote(44.65,"sell",40.6)[0] is not None
    assert model.quote(36.55,"buy",40.6)[0] is not None
    assert model.quote(50,"buy",40.6)[1] == "outside_proxy_band_requires_reference"
    assert round_tick(1000.1,True) == 1005
    assert round_tick(49.99,True) == 50


def test_blocked_order_can_resignal_next_close():
    bars=history([(100,100),(100,95),(104.5,95),(95,95)],[100000]*4)
    result=run(bars)
    assert result.orders[0]["reason"] == "limit_up_buy_blocked"
    assert result.orders[1]["status"] == "filled"


def test_today_extremes_do_not_change_fills():
    bars=history([(100,100),(100,98),(99,99)],[100000]*3)
    first=run(bars)
    bars[-1].high=10000
    bars[-1].low=1
    assert first.orders == run(bars).orders


def test_reject_invalid_parameters_and_unsupported_etf():
    with pytest.raises(ValueError): SmileExecution(slippage_bps=-1)
    with pytest.raises(ValueError): SmileExecution(participation=0)
    with pytest.raises(ValueError):
        SmileCurveBacktester(Settings(_env_file=None)).run_histories({"0050":[]})
