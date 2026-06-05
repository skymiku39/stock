"""模擬交易監測 — 從 signals CSV 與 risk_state 彙整狀態。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from bot.models import QtyUnit, qty_multiplier


@dataclass
class SimPosition:
    symbol: str
    quantity: int
    unit: QtyUnit
    avg_price: float


@dataclass
class SimFundState:
    max_fund: float
    fund_used: float = 0.0
    positions: List[SimPosition] = field(default_factory=list)
    signal_count: int = 0

    @property
    def remaining(self) -> float:
        return max(0.0, self.max_fund - self.fund_used)


def load_risk_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def fund_state_from_signals(df: Optional[pd.DataFrame], max_fund: float) -> SimFundState:
    """由 signals CSV 重建虛擬資金與持倉。"""
    state = SimFundState(max_fund=max_fund)
    if df is None or df.empty:
        return state

    state.signal_count = len(df)
    work = df.copy()
    if "ts" in work.columns:
        work = work.sort_values("ts")

    holdings: Dict[str, SimPosition] = {}
    fund_used = 0.0

    for _, row in work.iterrows():
        sym = str(row.get("symbol", ""))
        action = str(row.get("action", ""))
        price = float(row.get("price", 0) or 0)
        qty = int(row.get("quantity", 0) or 0)
        unit: QtyUnit = "share" if str(row.get("unit", "lot")) == "share" else "lot"
        mult = qty_multiplier(unit)

        if action == "would-buy" and qty > 0 and price > 0:
            cost = price * qty * mult
            fund_used += cost
            if sym in holdings:
                pos = holdings[sym]
                total = pos.avg_price * pos.quantity + price * qty
                pos.quantity += qty
                pos.avg_price = total / pos.quantity if pos.quantity else price
            else:
                holdings[sym] = SimPosition(sym, qty, unit, price)

        elif action == "would-sell" and sym in holdings:
            pos = holdings[sym]
            sell_qty = min(qty, pos.quantity) if qty > 0 else pos.quantity
            released = price * sell_qty * mult
            fund_used = max(0.0, fund_used - released)
            pos.quantity -= sell_qty
            if pos.quantity <= 0:
                del holdings[sym]

    state.fund_used = fund_used
    state.positions = list(holdings.values())
    return state


SIM_TEMPLATES: Dict[str, Dict[str, str]] = {
    "watch_10k": {
        "RUN_MODE": "watch",
        "MAX_FUND": "10000",
        "PER_ORDER_MAX_COST_TWD": "10000",
        "MAX_OPEN_POSITIONS": "3",
        "USE_ODD_LOT": "true",
        "STRATEGY_TYPE": "configurable",
        "MIN_PCT_CHG_ON_ENTRY": "1.0",
        "MAX_PCT_CHG_ON_ENTRY": "5.0",
        "DAILY_MAX_LOSS_TWD": "500",
        "DAILY_MAX_LOSS_PCT": "5",
        "MIN_PRICE": "10",
        "MAX_PRICE": "200",
    },
    "shioaji_sim_10k": {
        "RUN_MODE": "trade",
        "SIMULATION": "true",
        "MAX_FUND": "10000",
        "PER_ORDER_MAX_COST_TWD": "10000",
        "MAX_OPEN_POSITIONS": "3",
        "USE_ODD_LOT": "true",
        "STRATEGY_TYPE": "configurable",
        "MIN_PCT_CHG_ON_ENTRY": "1.0",
        "MAX_PCT_CHG_ON_ENTRY": "5.0",
        "DAILY_MAX_LOSS_TWD": "500",
        "DAILY_MAX_LOSS_PCT": "5",
        "MIN_PRICE": "10",
        "MAX_PRICE": "200",
        "LLM_GATE_ENABLED": "true",
    },
}
