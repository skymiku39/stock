"""intraday_backtest -- 以 SQLite 分 K 重播當沖策略（Configurable 規則，不含 LLM 閘門）。"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from bot.config import Settings
from bot.entry_rules import in_entry_range, resolve_entry_range
from bot.intraday_history import intraday_ts_to_datetime
from bot.models import QtyUnit
from bot.stock_db import IntradayBar, StockDB, PriceBar, default_db_path
from bot.trade_cost import (
    buy_cash_required,
    max_affordable_qty,
    net_pnl_pct,
    net_pnl_twd,
    position_net_pnl_pct,
    rebuy_opportunity,
)


@dataclass
class BacktestTrade:
    symbol: str
    trade_date: str
    entry_ts: str
    exit_ts: str
    entry_price: float
    exit_price: float
    quantity: int
    unit: QtyUnit
    entry_reason: str
    exit_reason: str
    pnl_twd: float
    pnl_pct: float
    entry_pct_chg: float = 0.0


@dataclass
class SymbolBacktestResult:
    symbol: str
    trades: List[BacktestTrade] = field(default_factory=list)
    bar_days: int = 0
    bar_count: int = 0
    skip_reason: str = ""

    @property
    def total_pnl_twd(self) -> float:
        return sum(t.pnl_twd for t in self.trades)

    @property
    def win_count(self) -> int:
        return sum(1 for t in self.trades if t.pnl_twd > 0)


@dataclass
class BacktestSummary:
    results: List[SymbolBacktestResult]
    settings_note: str = ""

    @property
    def all_trades(self) -> List[BacktestTrade]:
        out: List[BacktestTrade] = []
        for r in self.results:
            out.extend(r.trades)
        return out

    @property
    def total_pnl_twd(self) -> float:
        return sum(t.pnl_twd for t in self.all_trades)

    @property
    def win_rate(self) -> float:
        trades = self.all_trades
        if not trades:
            return 0.0
        return 100.0 * sum(1 for t in trades if t.pnl_twd > 0) / len(trades)


def _parse_time(ts_str: str) -> dt.time:
    return intraday_ts_to_datetime(ts_str).time()


def _session_filter(bars: Sequence[IntradayBar]) -> List[IntradayBar]:
    out: List[IntradayBar] = []
    for b in bars:
        t = _parse_time(b.ts)
        if dt.time(9, 0) <= t <= dt.time(13, 30):
            out.append(b)
    return out


def _group_by_date(bars: Sequence[IntradayBar]) -> Dict[str, List[IntradayBar]]:
    groups: Dict[str, List[IntradayBar]] = {}
    for b in bars:
        d = intraday_ts_to_datetime(b.ts).date().isoformat()
        groups.setdefault(d, []).append(b)
    for d in groups:
        groups[d].sort(key=lambda x: x.ts)
    return groups


def _prev_close_for_day(
    db: StockDB,
    symbol: str,
    trade_date: str,
    day_bars: Sequence[IntradayBar],
    daily_cache: Dict[str, List[PriceBar]],
) -> Optional[float]:
    if symbol not in daily_cache:
        daily_cache[symbol] = db.get_price_history(symbol, ascending=True)
    d = dt.date.fromisoformat(trade_date)
    prev = None
    for bar in daily_cache[symbol]:
        try:
            bd = dt.date.fromisoformat(bar.date[:10])
        except ValueError:
            continue
        if bd < d:
            prev = bar.close
        elif bd >= d:
            break
    if prev and prev > 0:
        return prev
    if day_bars:
        return float(day_bars[0].open or day_bars[0].close)
    return None


@dataclass
class _DayState:
    position: Optional[Tuple[float, int, QtyUnit, str]] = None
    peak_net_pnl: float = 0.0
    had_exit: bool = False
    last_exit_price: float = 0.0
    last_exit_net_pct: float = 0.0
    fund_used: float = 0.0


class IntradayBacktester:
    """以分 K close 價重播 ConfigurableStrategy 核心規則。"""

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

    def _sell_target(self, symbol: str) -> Optional[float]:
        targets = self.settings.sell_profit_targets or {}
        if symbol in targets:
            return float(targets[symbol])
        return None

    def _in_profit_exit_window(self, cur: dt.time) -> bool:
        start = self.settings.profit_exit_start_time
        if start is None:
            return False
        return start <= cur < self.settings.exit_time

    def _try_exit(
        self,
        symbol: str,
        price: float,
        ts_str: str,
        cur: dt.time,
        st: _DayState,
    ) -> Optional[BacktestTrade]:
        if st.position is None:
            return None
        entry_price, qty, unit, entry_ts = st.position
        pnl_pct = position_net_pnl_pct(
            entry_price, price, qty, unit, settings=self.settings,
        )
        if pnl_pct > st.peak_net_pnl:
            st.peak_net_pnl = pnl_pct

        target = self._sell_target(symbol)
        reason = ""
        if target is not None and pnl_pct >= target:
            reason = "target"
        elif self._in_profit_exit_window(cur) and pnl_pct > 0:
            reason = "afternoon"
        elif (
            st.peak_net_pnl >= self.settings.take_profit_pct
            and (st.peak_net_pnl - pnl_pct) >= self.settings.trailing_stop_pct
        ):
            reason = "trail"
        elif pnl_pct <= self.settings.stop_loss_pct:
            reason = "sl"
        elif cur >= self.settings.exit_time:
            reason = "close"

        if not reason:
            return None

        pnl_twd = net_pnl_twd(entry_price, price, qty, unit, settings=self.settings)
        trade = BacktestTrade(
            symbol=symbol,
            trade_date=intraday_ts_to_datetime(ts_str).date().isoformat(),
            entry_ts=entry_ts,
            exit_ts=ts_str,
            entry_price=entry_price,
            exit_price=price,
            quantity=qty,
            unit=unit,
            entry_reason="enter",
            exit_reason=reason,
            pnl_twd=pnl_twd,
            pnl_pct=pnl_pct,
        )
        st.position = None
        st.peak_net_pnl = 0.0
        st.had_exit = True
        st.last_exit_price = price
        st.last_exit_net_pct = pnl_pct
        st.fund_used = 0.0
        return trade

    def _try_entry(
        self,
        symbol: str,
        price: float,
        ts_str: str,
        cur: dt.time,
        pct_chg: float,
        prev_close: float,
        st: _DayState,
    ) -> bool:
        if st.position is not None or cur >= self.settings.exit_time:
            return False
        if not getattr(self.settings, "allow_same_day_reentry", True) and st.had_exit:
            return False

        fund_remaining = self._fund_cap() - st.fund_used
        qty, unit = self._calc_quantity(price, fund_remaining)
        if qty <= 0:
            return False

        if st.had_exit and st.last_exit_price > 0:
            if rebuy_opportunity(
                st.last_exit_price, price, qty, unit, settings=self.settings,
            ):
                cost = buy_cash_required(price, qty, unit, settings=self.settings)
                st.position = (price, qty, unit, ts_str)
                st.fund_used = cost
                return True

        if in_entry_range(symbol, pct_chg, self.settings):
            cost = buy_cash_required(price, qty, unit, settings=self.settings)
            st.position = (price, qty, unit, ts_str)
            st.fund_used = cost
            return True
        return False

    def run_symbol(
        self,
        db: StockDB,
        symbol: str,
        *,
        start: Optional[str] = None,
        end: Optional[str] = None,
        daily_cache: Optional[Dict[str, List[PriceBar]]] = None,
    ) -> SymbolBacktestResult:
        daily_cache = daily_cache if daily_cache is not None else {}
        raw = db.get_intraday_bars(symbol, interval="1m", start=start, end=end)
        bars = _session_filter(raw)
        result = SymbolBacktestResult(symbol=symbol, bar_count=len(bars))
        if not bars:
            result.skip_reason = "no_intraday_bars"
            return result

        by_day = _group_by_date(bars)
        result.bar_days = len(by_day)

        for day, day_bars in sorted(by_day.items()):
            prev_close = _prev_close_for_day(db, symbol, day, day_bars, daily_cache)
            if not prev_close or prev_close <= 0:
                continue
            st = _DayState()
            entry_pct = 0.0
            for bar in day_bars:
                price = float(bar.close)
                if price <= 0:
                    continue
                cur = _parse_time(bar.ts)
                pct_chg = 100.0 * (price - prev_close) / prev_close

                trade = self._try_exit(symbol, price, bar.ts, cur, st)
                if trade:
                    trade.entry_pct_chg = entry_pct
                    result.trades.append(trade)
                    continue

                if st.position is None:
                    if self._try_entry(symbol, price, bar.ts, cur, pct_chg, prev_close, st):
                        entry_pct = pct_chg

            if st.position is not None:
                last = day_bars[-1]
                cur = _parse_time(last.ts)
                if cur < self.settings.exit_time:
                    cur = self.settings.exit_time
                trade = self._try_exit(symbol, float(last.close), last.ts, cur, st)
                if trade:
                    trade.entry_pct_chg = entry_pct
                    trade.exit_reason = "eod"
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
        daily_cache: Dict[str, List[PriceBar]] = {}
        results = [
            self.run_symbol(
                database, sym, start=start, end=end, daily_cache=daily_cache,
            )
            for sym in symbols
        ]
        lo, hi = resolve_entry_range("", self.settings)
        note = (
            f"entry {lo:.1f}~{hi:.1f}% | sl {self.settings.stop_loss_pct}% | "
            f"trail {self.settings.take_profit_pct}%/{self.settings.trailing_stop_pct}% | "
            f"exit {self.settings.exit_time.strftime('%H:%M')}"
        )
        return BacktestSummary(results=results, settings_note=note)
