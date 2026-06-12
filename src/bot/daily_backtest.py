"""daily_backtest -- 以日 K (price_history) 做長區間當沖策略近似回測。

2020 迄今的完整歷史以 TWSE 日 K 為主；單日內以 OHLC 路徑近似停損/停利。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from bot.config import Settings
from bot.entry_rules import in_entry_range, resolve_entry_range
from bot.intraday_backtest import BacktestSummary, BacktestTrade, SymbolBacktestResult
from bot.models import QtyUnit
from bot.stock_db import PriceBar, StockDB, default_db_path
from bot.trade_cost import (
    buy_cash_required,
    max_affordable_qty,
    net_pnl_twd,
    position_net_pnl_pct,
)


@dataclass
class _DaySim:
    entry_price: float = 0.0
    qty: int = 0
    unit: QtyUnit = "lot"
    peak_pnl: float = 0.0


class DailyBacktester:
    """日 K 近似回測（適合 2020~迄今長區間）。"""

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or Settings()

    def _calc_quantity(self, price: float, fund_remaining: float) -> Tuple[int, QtyUnit]:
        if price <= 0 or fund_remaining <= 0:
            return 0, "lot"
        qty = max_affordable_qty(
            price, fund_remaining, "lot",
            max_qty=self.settings.max_lot_per_symbol,
            settings=self.settings,
        )
        if qty >= 1:
            return qty, "lot"
        if self.settings.use_odd_lot:
            qty = max_affordable_qty(
                price, fund_remaining, "share",
                max_qty=self.settings.odd_lot_max_shares,
                settings=self.settings,
            )
            if qty >= 1:
                return qty, "share"
        return 0, "lot"

    def _fund_cap(self) -> float:
        return float(self.settings.effective_fund_cap())

    def _resolve_entry_price(
        self,
        symbol: str,
        prev_close: float,
        bar: PriceBar,
    ) -> Optional[float]:
        if prev_close <= 0:
            return None
        lo, hi = resolve_entry_range(symbol, self.settings)
        pct_open = 100.0 * (bar.open - prev_close) / prev_close
        pct_high = 100.0 * (bar.high - prev_close) / prev_close
        if pct_high < lo:
            return None
        if lo <= pct_open <= hi:
            entry = float(bar.open)
        elif pct_high >= lo:
            entry = prev_close * (1.0 + lo / 100.0)
        else:
            return None
        pct_entry = 100.0 * (entry - prev_close) / prev_close
        if pct_entry > hi:
            return None
        return entry

    def _simulate_day(
        self,
        symbol: str,
        trade_date: str,
        prev_close: float,
        bar: PriceBar,
    ) -> Optional[BacktestTrade]:
        entry = self._resolve_entry_price(symbol, prev_close, bar)
        if entry is None or entry <= 0:
            return None
        fund_remaining = self._fund_cap()
        qty, unit = self._calc_quantity(entry, fund_remaining)
        if qty <= 0:
            return None

        pct_entry = 100.0 * (entry - prev_close) / prev_close
        st = _DaySim(entry_price=entry, qty=qty, unit=unit)
        exit_price = float(bar.close)
        reason = "close"

        for px in (float(bar.high), float(bar.low), float(bar.close)):
            pnl = position_net_pnl_pct(
                entry, px, qty, unit, settings=self.settings,
            )
            if pnl > st.peak_pnl:
                st.peak_pnl = pnl

        pnl_low = position_net_pnl_pct(
            entry, float(bar.low), qty, unit, settings=self.settings,
        )
        if pnl_low <= self.settings.stop_loss_pct:
            exit_price = float(bar.low)
            reason = "sl"
        elif (
            st.peak_pnl >= self.settings.take_profit_pct
            and (st.peak_pnl - position_net_pnl_pct(
                entry, float(bar.close), qty, unit, settings=self.settings,
            )) >= self.settings.trailing_stop_pct
        ):
            exit_price = float(bar.close)
            reason = "trail"
        else:
            pnl_high = position_net_pnl_pct(
                entry, float(bar.high), qty, unit, settings=self.settings,
            )
            if (
                st.peak_pnl >= self.settings.take_profit_pct
                and (st.peak_pnl - pnl_high) >= self.settings.trailing_stop_pct
            ):
                exit_price = float(bar.high)
                reason = "trail"

        pnl_pct = position_net_pnl_pct(
            entry, exit_price, qty, unit, settings=self.settings,
        )
        pnl_twd = net_pnl_twd(entry, exit_price, qty, unit, settings=self.settings)
        return BacktestTrade(
            symbol=symbol,
            trade_date=trade_date,
            entry_ts=f"{trade_date} 09:00:00",
            exit_ts=f"{trade_date} 13:30:00",
            entry_price=entry,
            exit_price=exit_price,
            quantity=qty,
            unit=unit,
            entry_reason="enter",
            exit_reason=reason,
            pnl_twd=pnl_twd,
            pnl_pct=pnl_pct,
            entry_pct_chg=pct_entry,
        )

    def run_symbol(
        self,
        db: StockDB,
        symbol: str,
        *,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> SymbolBacktestResult:
        bars = db.get_price_history(symbol, start=start, end=end, ascending=True)
        result = SymbolBacktestResult(symbol=symbol, bar_count=len(bars))
        if len(bars) < 2:
            result.skip_reason = "no_daily_bars"
            return result

        result.bar_days = len(bars) - 1
        for i in range(1, len(bars)):
            prev_bar = bars[i - 1]
            bar = bars[i]
            prev_close = float(prev_bar.close)
            if prev_close <= 0:
                continue
            trade = self._simulate_day(symbol, bar.date[:10], prev_close, bar)
            if trade:
                result.trades.append(trade)
        return result

    def run_many(
        self,
        symbols: Sequence[str],
        *,
        root=None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        db: Optional[StockDB] = None,
    ) -> BacktestSummary:
        from pathlib import Path
        database = db or StockDB.open(path=default_db_path(Path(root or ".")))
        lo, hi = resolve_entry_range("", self.settings)
        results = [
            self.run_symbol(database, sym, start=start, end=end)
            for sym in symbols
        ]
        note = (
            f"[日K近似] entry {lo:.1f}~{hi:.1f}% | sl {self.settings.stop_loss_pct}% | "
            f"trail {self.settings.take_profit_pct}%/{self.settings.trailing_stop_pct}%"
        )
        return BacktestSummary(results=results, settings_note=note)
