"""sim_monitor 單元測試。"""

from __future__ import annotations

import pandas as pd

from bot.sim_monitor import fund_state_from_signals


def test_fund_state_from_signals_buy_sell() -> None:
    df = pd.DataFrame([
        {
            "ts": "2026-06-05 09:15:00",
            "symbol": "2330",
            "action": "would-buy",
            "price": 50.0,
            "quantity": 100,
            "unit": "share",
        },
        {
            "ts": "2026-06-05 10:00:00",
            "symbol": "2330",
            "action": "would-sell",
            "price": 52.0,
            "quantity": 100,
            "unit": "share",
        },
    ])
    state = fund_state_from_signals(df, max_fund=10_000)
    assert state.fund_used == 0.0
    assert state.positions == []
    assert state.signal_count == 2
