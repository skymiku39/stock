"""微笑曲線策略核心 — 標準價逢低加碼、回彈獲利賣出（每筆賣須淨利 > 0）。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from bot.models import QtyUnit
from bot.trade_cost import (
    buy_cash_required,
    max_affordable_qty,
    net_pnl_pct,
    net_pnl_twd,
)

if TYPE_CHECKING:
    from bot.config import Settings

SmileActionKind = Literal["buy", "sell", "reset"]


@dataclass
class SmileBuyTier:
    """自標準價回落達此 % 時，買進 base_lot × multiplier 張。"""
    drop_pct: float
    multiplier: int


@dataclass
class SmileLot:
    entry_price: float
    quantity: int
    unit: QtyUnit
    entry_date: str
    tier_label: str = ""


@dataclass
class SmileAction:
    kind: SmileActionKind
    date: str
    price: float
    quantity: int
    unit: QtyUnit
    reason: str
    reference: float = 0.0


@dataclass
class SmileRoundTrip:
    """單筆 lot 買賣回合（回測輸出）。"""
    symbol: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    quantity: int
    unit: QtyUnit
    entry_reason: str
    exit_reason: str
    pnl_twd: float
    pnl_pct: float
    reference: float


@dataclass
class SmileCycleState:
    reference: float = 0.0
    lots: list[SmileLot] = field(default_factory=list)
    tiers_done: set[int] = field(default_factory=set)
    bottom_dca_done_dates: set[str] = field(default_factory=set)
    cycle_index: int = 0


def parse_smile_buy_tiers(raw: str) -> list[SmileBuyTier]:
    """解析 '1:1,3:2,5:3,8:4' → 跌幅% : 加碼倍數。"""
    tiers: list[SmileBuyTier] = []
    for item in raw.replace(";", ",").split(","):
        part = item.strip()
        if not part:
            continue
        bits = part.split(":")
        if len(bits) != 2:
            raise ValueError(
                f"SMILE_BUY_TIERS 格式為 DROP_PCT:MULT，收到 {part!r}",
            )
        drop_pct, mult = float(bits[0].strip()), int(bits[1].strip())
        if drop_pct <= 0 or mult <= 0:
            raise ValueError(f"SMILE_BUY_TIERS 數值須 > 0: {part!r}")
        tiers.append(SmileBuyTier(drop_pct=drop_pct, multiplier=mult))
    tiers.sort(key=lambda t: t.drop_pct)
    if not tiers:
        raise ValueError("SMILE_BUY_TIERS 不可為空")
    return tiers


def resolve_smile_reference(
    symbol: str,
    price: float,
    settings: Settings,
) -> float:
    """決定週期標準價：手動指定 > 當日價。"""
    refs: dict[str, float] = getattr(settings, "smile_reference_prices", {}) or {}
    if symbol in refs and refs[symbol] > 0:
        return float(refs[symbol])
    return price


class SmileCurveEngine:
    """單檔微笑曲線狀態機（日 K / Tick 共用邏輯）。"""

    def __init__(self, symbol: str, settings: Settings):
        self.symbol = symbol
        self.settings = settings
        self.state = SmileCycleState()
        self.tiers = parse_smile_buy_tiers(
            getattr(settings, "smile_buy_tiers", "1:1,3:2,5:3,8:4"),
        )
        self.base_lot = max(1, int(getattr(settings, "smile_base_lot", 1)))
        self.sell_tax = float(getattr(settings, "smile_regular_tax_rate", 0.003))
        self.round_trips: list[SmileRoundTrip] = []
        self._cash_used = 0.0

    def _fund_remaining(self) -> float:
        cap = float(self.settings.effective_fund_cap())
        return max(0.0, cap - self._cash_used)

    def _lot_cost(self, lots: Sequence[SmileLot]) -> float:
        return sum(
            buy_cash_required(l.entry_price, l.quantity, l.unit, settings=self.settings)
            for l in lots
        )

    def _affordable_qty(self, price: float, want: int, unit: QtyUnit) -> int:
        if price <= 0 or want <= 0:
            return 0
        budget = self._fund_remaining()
        cap = max_affordable_qty(
            price, budget, unit,
            max_qty=want,
            settings=self.settings,
        )
        max_per_symbol = int(self.settings.max_lot_per_symbol)
        if unit == "lot" and max_per_symbol > 0:
            held = sum(l.quantity for l in self.state.lots if l.unit == "lot")
            cap = min(cap, max(0, max_per_symbol - held))
        return min(want, cap)

    def _drop_pct(self, price: float) -> float:
        ref = self.state.reference
        if ref <= 0 or price <= 0:
            return 0.0
        return 100.0 * (ref - price) / ref

    def _is_profitable_sell(self, lot: SmileLot, price: float) -> bool:
        return net_pnl_pct(
            lot.entry_price, price, lot.quantity, lot.unit,
            settings=self.settings,
            sell_tax_rate=self.sell_tax,
        ) > 0.0

    def _record_sell(self, lot: SmileLot, price: float, date: str, reason: str) -> SmileAction:
        pnl_pct = net_pnl_pct(
            lot.entry_price, price, lot.quantity, lot.unit,
            settings=self.settings,
            sell_tax_rate=self.sell_tax,
        )
        pnl_twd = net_pnl_twd(
            lot.entry_price, price, lot.quantity, lot.unit,
            settings=self.settings,
            sell_tax_rate=self.sell_tax,
        )
        self.round_trips.append(SmileRoundTrip(
            symbol=self.symbol,
            entry_date=lot.entry_date,
            exit_date=date,
            entry_price=lot.entry_price,
            exit_price=price,
            quantity=lot.quantity,
            unit=lot.unit,
            entry_reason=lot.tier_label,
            exit_reason=reason,
            pnl_twd=pnl_twd,
            pnl_pct=pnl_pct,
            reference=self.state.reference,
        ))
        cost = buy_cash_required(
            lot.entry_price, lot.quantity, lot.unit, settings=self.settings,
        )
        self._cash_used = max(0.0, self._cash_used - cost)
        return SmileAction(
            kind="sell",
            date=date,
            price=price,
            quantity=lot.quantity,
            unit=lot.unit,
            reason=reason,
            reference=self.state.reference,
        )

    def _record_buy(
        self,
        price: float,
        qty: int,
        unit: QtyUnit,
        date: str,
        reason: str,
    ) -> SmileAction | None:
        qty = self._affordable_qty(price, qty, unit)
        if qty <= 0:
            return None
        self.state.lots.append(SmileLot(
            entry_price=price,
            quantity=qty,
            unit=unit,
            entry_date=date,
            tier_label=reason,
        ))
        self._cash_used += buy_cash_required(
            price, qty, unit, settings=self.settings,
        )
        return SmileAction(
            kind="buy",
            date=date,
            price=price,
            quantity=qty,
            unit=unit,
            reason=reason,
            reference=self.state.reference,
        )

    def _reset_cycle(self, price: float, date: str) -> SmileAction:
        self.state.cycle_index += 1
        self.state.reference = resolve_smile_reference(self.symbol, price, self.settings)
        self.state.tiers_done.clear()
        self.state.bottom_dca_done_dates.clear()
        return SmileAction(
            kind="reset",
            date=date,
            price=price,
            quantity=0,
            unit="lot",
            reason=f"cycle_{self.state.cycle_index}",
            reference=self.state.reference,
        )

    def _init_reference(self, price: float, date: str) -> list[SmileAction]:
        self.state.reference = resolve_smile_reference(self.symbol, price, self.settings)
        return [self._reset_cycle(price, date)]

    def _rebound_sell(self, date: str, price: float) -> list[SmileAction]:
        actions: list[SmileAction] = []
        sold_any = False
        remaining: list[SmileLot] = []
        for lot in self.state.lots:
            if self._is_profitable_sell(lot, price):
                actions.append(self._record_sell(lot, price, date, "rebound"))
                sold_any = True
            else:
                remaining.append(lot)
        self.state.lots = remaining

        if sold_any and not self.state.lots:
            actions.append(self._reset_cycle(price, date))
        elif sold_any:
            self.state.tiers_done.clear()
            self.state.bottom_dca_done_dates.clear()
        return actions

    def _dip_buy(self, date: str, price: float) -> list[SmileAction]:
        actions: list[SmileAction] = []
        drop = self._drop_pct(price)
        unit: QtyUnit = "share" if self.settings.use_odd_lot else "lot"

        for idx, tier in enumerate(self.tiers):
            if drop + 1e-9 < tier.drop_pct:
                continue
            if idx in self.state.tiers_done:
                continue
            want = self.base_lot * tier.multiplier
            act = self._record_buy(
                price, want, unit, date,
                reason=f"dip_{tier.drop_pct:.1f}%",
            )
            if act:
                actions.append(act)
                self.state.tiers_done.add(idx)

        deepest = self.tiers[-1].drop_pct
        if drop + 1e-9 >= deepest and date not in self.state.bottom_dca_done_dates:
            act = self._record_buy(
                price, self.base_lot, unit, date,
                reason=f"bottom_dca_{deepest:.1f}%",
            )
            if act:
                actions.append(act)
                self.state.bottom_dca_done_dates.add(date)
        return actions

    def on_bar(self, date: str, close: float) -> list[SmileAction]:
        """處理單根 K 線收盤價，回傳當日動作序列。"""
        return self.on_bar_ohlc(date, close, close, close, close)

    def on_bar_ohlc(
        self,
        date: str,
        open_: float,
        high: float,
        low: float,
        close: float,
    ) -> list[SmileAction]:
        """日 K OHLC：先以當日低點評估加碼，再以高點評估獲利了結。"""
        actions: list[SmileAction] = []
        close = float(close)
        high = float(high)
        low = float(low)
        if close <= 0 or high <= 0 or low <= 0:
            return actions

        if self.state.reference <= 0:
            return self._init_reference(close, date)

        ref = self.state.reference
        if low < ref:
            actions.extend(self._dip_buy(date, low))
        if high >= ref:
            actions.extend(self._rebound_sell(date, high))
        return actions

    @property
    def open_lots(self) -> list[SmileLot]:
        return list(self.state.lots)

    @property
    def realized_pnl_twd(self) -> float:
        return sum(r.pnl_twd for r in self.round_trips)
