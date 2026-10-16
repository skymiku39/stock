"""smile_curve_backtest -- 以日 K 回測微笑曲線策略。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
from pathlib import Path
from typing import List, Optional, Sequence

from bot.config import Settings
from bot.smile_curve import SmileCurveEngine, SmileRoundTrip, SmileLot
from bot.models import qty_multiplier
from bot.trade_cost import buy_cash_required, sell_cash_received, net_pnl_twd, max_affordable_qty
from bot.stock_db import StockDB, default_db_path


@dataclass
class SmileSymbolResult:
    symbol: str
    round_trips: List[SmileRoundTrip] = field(default_factory=list)
    bar_count: int = 0
    cycle_count: int = 0
    open_lots: int = 0
    skip_reason: str = ""
    unrealized_pnl_twd: float = 0.0
    open_cost_twd: float = 0.0
    last_price_date: str = ""
    pending_orders: int = 0

    @property
    def net_pnl_twd(self) -> float:
        return self.total_pnl_twd + self.unrealized_pnl_twd

    @property
    def trade_count(self) -> int:
        return len(self.round_trips)

    @property
    def total_pnl_twd(self) -> float:
        return sum(r.pnl_twd for r in self.round_trips)

    @property
    def win_count(self) -> int:
        return sum(1 for r in self.round_trips if r.pnl_twd > 0)

    @property
    def win_rate(self) -> float:
        if not self.round_trips:
            return 0.0
        return 100.0 * self.win_count / len(self.round_trips)


@dataclass
class SmileBacktestSummary:
    results: List[SmileSymbolResult]
    settings_note: str = ""
    start: str = ""
    end: str = ""
    initial_cash: float = 0.0
    cash: float = 0.0
    equity_curve: list[dict] = field(default_factory=list)
    orders: list[dict] = field(default_factory=list)
    max_drawdown_twd: float = 0.0
    max_drawdown_pct: float = 0.0

    @property
    def unrealized_pnl_twd(self) -> float:
        return sum(r.unrealized_pnl_twd for r in self.results)

    @property
    def net_pnl_twd(self) -> float:
        return self.total_pnl_twd + self.unrealized_pnl_twd

    @property
    def return_pct(self) -> float:
        return 100 * self.net_pnl_twd / self.initial_cash if self.initial_cash else 0.0

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload.update(realized_pnl_twd=self.total_pnl_twd,
                       total_pnl_twd=self.total_pnl_twd,  # legacy alias: realized only
                       unrealized_pnl_twd=self.unrealized_pnl_twd,
                       net_pnl_twd=self.net_pnl_twd, return_pct=self.return_pct,
                       final_equity=self.initial_cash + self.net_pnl_twd,
                       win_rate=self.win_rate, trade_count=len(self.all_round_trips),
                       execution_mode="close_signal_next_open")
        payload["symbols"] = [dict(symbol=r.symbol, trade_count=r.trade_count,
                                   total_pnl_twd=r.total_pnl_twd, net_pnl_twd=r.net_pnl_twd,
                                   unrealized_pnl_twd=r.unrealized_pnl_twd, win_rate=r.win_rate,
                                   open_lots=r.open_lots, cycle_count=r.cycle_count,
                                   skip_reason=r.skip_reason) for r in self.results]
        return payload

    @property
    def all_round_trips(self) -> List[SmileRoundTrip]:
        out: List[SmileRoundTrip] = []
        for r in self.results:
            out.extend(r.round_trips)
        return out

    @property
    def total_pnl_twd(self) -> float:
        return sum(r.total_pnl_twd for r in self.results)

    @property
    def win_rate(self) -> float:
        trips = self.all_round_trips
        if not trips:
            return 0.0
        return 100.0 * sum(1 for t in trips if t.pnl_twd > 0) / len(trips)


class SmileCurveBacktester:
    """日 K 微笑曲線回測。"""

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or Settings()

    def run_symbol(
        self,
        db: StockDB,
        symbol: str,
        *,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> SmileSymbolResult:
        return self.run_many([symbol], db=db, start=start, end=end).results[0]

    def run_many(
        self,
        symbols: Sequence[str],
        *,
        root: Optional[Path] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        db: Optional[StockDB] = None,
    ) -> SmileBacktestSummary:
        owns_db = db is None
        database = db or StockDB.open(path=default_db_path(root or Path.cwd()))
        try:
            histories = {sym: database.get_price_history(sym, start=start, end=end, ascending=True)
                         for sym in sorted(set(symbols))}
            return self.run_histories(histories, start=start or "", end=end or "")
        finally:
            if owns_db:
                database.close()

    def run_histories(self, histories: dict, *, start: str = "", end: str = "") -> SmileBacktestSummary:
        """One cash account. Signals at close, fills at the next observed session's open.

        Buy orders use only cash available BEFORE that morning's sales, avoiding an
        assumed same-auction sell/buy order. Ties are resolved by symbol then tier.
        Missing sessions retain orders; an invalid observed open cancels that order.
        Valuation is net liquidation value (including estimated exit fees/tax).
        """
        cap = float(self.settings.effective_fund_cap())
        if not math.isfinite(cap) or cap <= 0:
            raise ValueError("fund cap must be finite and positive")
        engines = {s: SmileCurveEngine(s, self.settings) for s in sorted(histories)}
        results = {s: SmileSymbolResult(s, bar_count=len(histories[s])) for s in engines}
        calendar: dict = {}
        for sym, bars in histories.items():
            for bar in bars:
                date = bar.date[:10]
                if sym in calendar.setdefault(date, {}):
                    raise ValueError(f"duplicate daily bar: {sym} {date}")
                calendar[date][sym] = bar
        out = SmileBacktestSummary(list(results.values()), start=start, end=end,
                                  initial_cash=cap, cash=cap,
                                  settings_note=(f"fund_cap={cap:.0f}; tiers={self.settings.smile_buy_tiers}; "
                                                 f"base_lot={self.settings.smile_base_lot}; "
                                                 f"max_lot_per_symbol={self.settings.max_lot_per_symbol}; "
                                                 f"odd_lot={self.settings.use_odd_lot}; "
                                                 f"reference={self.settings.smile_reference_prices}; "
                                                 f"tax={self.settings.smile_regular_tax_rate}; "
                                                 f"fee_discount={self.settings.broker_fee_discount}; "
                                                 f"min_fee={self.settings.broker_min_fee}; "
                                                 "收盤訊號→次日開盤；多檔共用現金；同日賣款不供開盤買單；"
                                                 "代號排序分配；權益含預估出清費稅；無滑價/流動性模型"))
        pending: dict[str, list[dict]] = {s: [] for s in engines}
        marks: dict[str, float] = {}
        peak = cap

        def valid(value):
            return value is not None and math.isfinite(float(value)) and float(value) > 0

        def cost(lot):
            return buy_cash_required(lot.entry_price, lot.quantity, lot.unit, settings=self.settings)

        def exposure():
            return sum(cost(lot) for e in engines.values() for lot in e.open_lots)

        for date, today in sorted(calendar.items()):
            opening_budget = min(out.cash, max(0.0, cap - exposure()))
            for sym in sorted(today):
                e, bar = engines[sym], today[sym]
                orders, pending[sym] = pending[sym], []
                for order in orders:
                    log = {k: v for k, v in order.items() if k != "lot"}
                    log.update(symbol=sym, execution_date=date, status="cancelled", filled_quantity=0)
                    out.orders.append(log)
                    if not valid(bar.open):
                        log["reason"] = "invalid_open"
                        continue
                    px = float(bar.open)
                    log["execution_price"] = px
                    if order["side"] == "sell":
                        lot = order["lot"]
                        if not e._is_profitable_sell(lot, px):
                            log["reason"] = "unprofitable_open"
                            continue
                        e._record_sell(lot, px, date, "close_signal_next_open")
                        e.state.lots.remove(lot)
                        out.cash += sell_cash_received(px, lot.quantity, lot.unit,
                                                      settings=self.settings, sell_tax_rate=e.sell_tax)
                        log.update(status="filled", filled_quantity=lot.quantity)
                        if not e.open_lots:
                            e._reset_cycle(px, date)
                        else:
                            e.state.tiers_done.clear()
                            e.state.bottom_dca_done_dates.clear()
                    else:
                        unit = "share" if self.settings.use_odd_lot else "lot"
                        want = order["quantity"]
                        # Apply the same share-equivalent cap in both quantity modes.
                        limit = self.settings.max_lot_per_symbol * 1000
                        held = sum(l.quantity * qty_multiplier(l.unit) for l in e.open_lots)
                        if limit > 0:
                            want = min(want, max(0, int((limit - held) / qty_multiplier(unit))))
                        qty = max_affordable_qty(px, opening_budget, unit, max_qty=want,
                                                 settings=self.settings)
                        if qty <= 0:
                            log["reason"] = "cash_or_position_limit"
                            continue
                        paid = buy_cash_required(px, qty, unit, settings=self.settings)
                        e.state.lots.append(SmileLot(px, qty, unit, date, order["label"]))
                        e._cash_used += paid
                        out.cash -= paid
                        opening_budget -= paid
                        if order["tier"] is not None:
                            e.state.tiers_done.add(order["tier"])
                        log.update(status="filled", filled_quantity=qty)

            # Only now is today's close available to the strategy.
            for sym in sorted(today):
                e, bar = engines[sym], today[sym]
                if not valid(bar.close):
                    continue
                close = float(bar.close)
                marks[sym] = close
                results[sym].last_price_date = date
                if e.state.reference <= 0:
                    e._init_reference(close, date)
                    continue
                if close >= e.state.reference:
                    pending[sym] = [dict(side="sell", signal_date=date, lot=l, quantity=l.quantity)
                                    for l in e.open_lots if e._is_profitable_sell(l, close)]
                else:
                    drop = e._drop_pct(close)
                    for idx, tier in enumerate(e.tiers):
                        if idx not in e.state.tiers_done and drop + 1e-9 >= tier.drop_pct:
                            pending[sym].append(dict(side="buy", signal_date=date, tier=idx,
                                                    quantity=e.base_lot * tier.multiplier,
                                                    label=f"dip_{tier.drop_pct:.1f}%"))
                    if drop + 1e-9 >= e.tiers[-1].drop_pct:
                        pending[sym].append(dict(side="buy", signal_date=date, tier=None,
                                                quantity=e.base_lot, label="bottom_dca"))
            realized = sum(e.realized_pnl_twd for e in engines.values())
            unrealized = sum(net_pnl_twd(l.entry_price, marks[s], l.quantity, l.unit,
                                        settings=self.settings, sell_tax_rate=e.sell_tax)
                             for s, e in engines.items() for l in e.open_lots)
            equity = cap + realized + unrealized
            peak = max(peak, equity)
            dd = peak - equity
            dd_pct = 100 * dd / peak
            out.max_drawdown_twd = max(out.max_drawdown_twd, dd)
            out.max_drawdown_pct = max(out.max_drawdown_pct, dd_pct)
            out.equity_curve.append(dict(date=date, cash=out.cash, equity=equity,
                                         realized_pnl_twd=realized, unrealized_pnl_twd=unrealized,
                                         cost_exposure=exposure(), drawdown_pct=dd_pct,
                                         stale_marks=[s for s, e in engines.items() if e.open_lots
                                                      and results[s].last_price_date != date]))
        for sym, e in engines.items():
            r = results[sym]
            r.round_trips = list(e.round_trips)
            r.cycle_count = e.state.cycle_index
            r.open_lots = len(e.open_lots)
            r.open_cost_twd = sum(cost(l) for l in e.open_lots)
            r.unrealized_pnl_twd = sum(net_pnl_twd(l.entry_price, marks[sym], l.quantity, l.unit,
                                                settings=self.settings, sell_tax_rate=e.sell_tax)
                                       for l in e.open_lots)
            r.pending_orders = len(pending[sym])
            if not histories[sym]:
                r.skip_reason = "no_daily_bars"
            elif sym not in marks:
                r.skip_reason = "no_valid_close"
        return out
