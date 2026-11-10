from bot.config import Settings
from bot.smile_curve import SmileCurveEngine


def engine(**overrides):
    values = dict(_env_file=None, max_fund=500000, daily_fund_budget=0, use_odd_lot=False,
                  smile_buy_tiers="1:1,5:1", smile_base_lot=1, max_lot_per_symbol=5,
                  broker_fee_discount=0, broker_min_fee=0, smile_regular_tax_rate=0,
                  smile_max_unrealized_loss_pct=5, smile_max_bottom_dca_days=0)
    values.update(overrides)
    return SmileCurveEngine("2330", Settings(**values))


def test_halted_stops_buys_but_still_sells():
    e = engine()
    assert not e.halted
    e.on_bar_ohlc("d1", 100, 100, 100, 100)
    assert [a.kind for a in e.on_bar_ohlc("d2", 100, 99, 95, 96)].count("buy") == 3
    assert not e.halted
    n = len(e.open_lots)
    assert not [a for a in e.on_bar_ohlc("d3", 96, 97, 60, 61) if a.kind == "buy"]  # loss -> halt
    assert e.halted and len(e.open_lots) == n
    assert not [a for a in e.on_bar_ohlc("d4", 61, 62, 50, 55) if a.kind == "buy"]
    acts = e.on_bar_ohlc("d5", 100, 120, 100, 110)
    assert any(a.kind == "sell" for a in acts)
    assert not e.open_lots and e.halted


def test_zero_pct_never_halts():
    e = engine(smile_max_unrealized_loss_pct=0)
    e.on_bar_ohlc("d1", 100, 100, 100, 100)
    e.on_bar_ohlc("d2", 100, 99, 95, 96)
    e.on_bar_ohlc("d3", 96, 97, 60, 61)
    assert not e.halted


def test_bottom_dca_day_cap():
    for cap, expected in ((2, 2), (0, 3)):
        e = engine(smile_max_unrealized_loss_pct=0, smile_max_bottom_dca_days=cap)
        e.on_bar_ohlc("d0", 100, 100, 100, 100)
        acts = []
        for i in range(1, 8):
            acts += e.on_bar_ohlc(f"d{i}", 90, 90, 90, 90)
        assert len([a for a in acts if a.reason.startswith("bottom_dca")]) == expected
