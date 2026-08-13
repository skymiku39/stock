"""台股當沖交易成本計算 — 手續費 + 證交稅，供淨利判斷與回落買回。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from bot.models import QtyUnit, qty_multiplier

if TYPE_CHECKING:
    from bot.config import Settings

STANDARD_FEE_RATE = 0.001425  # 牌告 0.1425%


@dataclass(frozen=True)
class TradeCostParams:
    fee_discount: float = 0.28
    min_fee: float = 1.0
    day_trade_tax_rate: float = 0.0015

    @property
    def fee_rate(self) -> float:
        return STANDARD_FEE_RATE * self.fee_discount


def params_from_settings(settings: Settings) -> TradeCostParams:
    return TradeCostParams(
        fee_discount=float(getattr(settings, "broker_fee_discount", 0.28)),
        min_fee=float(getattr(settings, "broker_min_fee", 1.0)),
        day_trade_tax_rate=float(getattr(settings, "day_trade_tax_rate", 0.0015)),
    )


def notional(price: float, qty: int, unit: QtyUnit) -> float:
    return price * qty * qty_multiplier(unit)


def _fee(amount: float, params: TradeCostParams) -> float:
    if amount <= 0:
        return 0.0
    return max(params.min_fee, amount * params.fee_rate)


def buy_cash_required(
    entry_price: float,
    qty: int,
    unit: QtyUnit,
    params: TradeCostParams | None = None,
    *,
    settings: Settings | None = None,
) -> float:
    """買進實際支出（含手續費）。"""
    p = params or (params_from_settings(settings) if settings else TradeCostParams())
    amt = notional(entry_price, qty, unit)
    return amt + _fee(amt, p)


def sell_cash_received(
    exit_price: float,
    qty: int,
    unit: QtyUnit,
    params: TradeCostParams | None = None,
    *,
    settings: Settings | None = None,
    sell_tax_rate: float | None = None,
) -> float:
    """賣出實際入帳（扣手續費與證交稅）。"""
    p = params or (params_from_settings(settings) if settings else TradeCostParams())
    amt = notional(exit_price, qty, unit)
    tax_rate = p.day_trade_tax_rate if sell_tax_rate is None else sell_tax_rate
    tax = amt * tax_rate
    return amt - _fee(amt, p) - tax


def net_pnl_twd(
    entry_price: float,
    exit_price: float,
    qty: int,
    unit: QtyUnit,
    params: TradeCostParams | None = None,
    *,
    settings: Settings | None = None,
    sell_tax_rate: float | None = None,
) -> float:
    """一趟買賣淨損益（元）。"""
    p = params or (params_from_settings(settings) if settings else TradeCostParams())
    cost = buy_cash_required(entry_price, qty, unit, p)
    recv = sell_cash_received(
        exit_price, qty, unit, p, sell_tax_rate=sell_tax_rate,
    )
    return recv - cost


def net_pnl_pct(
    entry_price: float,
    exit_price: float,
    qty: int,
    unit: QtyUnit,
    params: TradeCostParams | None = None,
    *,
    settings: Settings | None = None,
    sell_tax_rate: float | None = None,
) -> float:
    """一趟買賣淨報酬率 %（相對於買進總成本）。"""
    p = params or (params_from_settings(settings) if settings else TradeCostParams())
    cost = buy_cash_required(entry_price, qty, unit, p)
    if cost <= 0:
        return 0.0
    pnl = net_pnl_twd(
        entry_price, exit_price, qty, unit, p,
        sell_tax_rate=sell_tax_rate,
    )
    return 100.0 * pnl / cost


def position_net_pnl_pct(
    avg_entry: float,
    current_price: float,
    qty: int,
    unit: QtyUnit,
    *,
    settings: Settings | None = None,
) -> float:
    """持倉若於 current_price 賣出的預估淨利 %。"""
    if avg_entry <= 0 or current_price <= 0 or qty <= 0:
        return 0.0
    return net_pnl_pct(avg_entry, current_price, qty, unit, settings=settings)


def max_affordable_qty(
    price: float,
    budget: float,
    unit: QtyUnit,
    *,
    max_qty: int,
    settings: Settings | None = None,
    params: TradeCostParams | None = None,
) -> int:
    """在 budget 內可買的最大數量（含手續費），從 max_qty 往下試。"""
    if price <= 0 or budget <= 0 or max_qty <= 0:
        return 0
    for qty in range(max_qty, 0, -1):
        cost = buy_cash_required(price, qty, unit, params, settings=settings)
        if cost <= budget + 1e-9:
            return qty
    return 0


def rebuy_opportunity(
    last_exit_price: float,
    current_price: float,
    qty: int,
    unit: QtyUnit,
    *,
    settings: Settings,
) -> bool:
    """回落買回：現價低於上次賣出價，且買回後以賣出價平倉可達目標淨利 %。"""
    if not getattr(settings, "allow_same_day_reentry", True):
        return False
    if last_exit_price <= 0 or current_price <= 0 or qty <= 0:
        return False
    if current_price >= last_exit_price:
        return False
    target = float(getattr(settings, "rebuy_target_net_pct", 2.0))
    net_pct = net_pnl_pct(current_price, last_exit_price, qty, unit, settings=settings)
    return net_pct >= target


__all__ = [
    "STANDARD_FEE_RATE",
    "TradeCostParams",
    "buy_cash_required",
    "max_affordable_qty",
    "net_pnl_pct",
    "net_pnl_twd",
    "notional",
    "params_from_settings",
    "position_net_pnl_pct",
    "rebuy_opportunity",
    "sell_cash_received",
]
