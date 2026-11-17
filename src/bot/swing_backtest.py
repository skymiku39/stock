"""swing_backtest -- 趨勢型波段策略的日 K 回測原型（#19）。只做回測，沒有盤中下單。

規則（參數是教科書式的起點，不是調出來的；見 docs/swing-strategy-design.md 第三節）：
- 進場：收盤價高於前 N 個交易日的最高收盤價，且短均線高於長均線；次日開盤買進。
- 初始停損：進場價 − initial_stop_atr × ATR。
- 移動停利：進場後最高收盤價 − trail_atr × ATR，只上移不下移；收盤跌破即次日開盤賣出。
- 持有期限：max_hold_days 日曆天，到期次日開盤賣出。
- 部位：每筆承擔權益的 risk_pct%（股數 = 權益 × risk_pct% ÷ (initial_stop_atr × ATR)），
  單檔不超過權益的 max_position_pct%，同時最多 max_positions 檔。權益 = 起始資金 + 已實現損益。
- 訊號多於空位時：依 momentum_days 日漲幅由高到低，再以代號排序。

進場規則二（entry="rs_revenue"，選股策略 C2，見 docs/reports/entry-timing.md）：不看突破與均線，
收盤時 momentum_days 日漲幅在當天有資料的名單內排名百分位 >= rs_min，且最近一期已公告的月營收
年增率 > yoy_min 就進場（月營收在次月 11 日才視為已知）。出場、部位與成交假設與規則一相同。

成交假設沿用 SmileExecution（次日開盤、滑價、前一日成交量上限、漲跌停），以零股計。
除權息：指標用的歷史價格在除權息日向前還原；持倉收到配發現金，成本與停損價同步下修。
資料中斷的標的以最後收盤價強制平倉（與微笑回測相同，#34）。
"""

from __future__ import annotations

import bisect
import datetime as dt
import math
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Optional, Sequence

from bot.config import Settings
from bot.round_metrics import Round, account_metrics, summarize_rounds
from bot.smile_curve_backtest import ex_rights_adjustment
from bot.smile_execution import SmileExecution
from bot.trade_cost import buy_cash_required, max_affordable_qty, sell_cash_received

UNIT = "share"
SELL_TAX = 0.003


@dataclass(frozen=True)
class SwingParams:
    breakout_days: int = 20
    fast_ma: int = 20
    slow_ma: int = 60
    atr_days: int = 20
    initial_stop_atr: float = 2.0
    trail_atr: float = 3.0
    max_hold_days: int = 90
    risk_pct: float = 1.0
    max_position_pct: float = 20.0
    max_positions: int = 6
    momentum_days: int = 60
    entry: str = "breakout"          # "breakout"（規則一）或 "rs_revenue"（規則二）
    rs_min: float = 0.6              # 規則二：相對強度百分位下限（0～1）
    yoy_min: float = 0.0             # 規則二：月營收年增率下限（%）

    def __post_init__(self):
        for name, value in asdict(self).items():
            if name in ("entry", "rs_min", "yoy_min"):
                continue
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.fast_ma >= self.slow_ma:
            raise ValueError("fast_ma must be shorter than slow_ma")
        if self.entry not in ("breakout", "rs_revenue"):
            raise ValueError("entry must be 'breakout' or 'rs_revenue'")
        if not (math.isfinite(self.rs_min) and 0 <= self.rs_min < 1):
            raise ValueError("rs_min must be in [0, 1)")
        if not math.isfinite(self.yoy_min):
            raise ValueError("yoy_min must be finite")

    @property
    def warmup(self) -> int:
        """算得出全部指標所需的日 K 根數（不含當日以外的額外一根前收）。"""
        return max(self.breakout_days, self.slow_ma, self.atr_days, self.momentum_days) + 1


@dataclass
class SwingPosition:
    symbol: str
    entry_date: str
    entry_price: float  # 已扣除持有期間的每股配發
    shares: int
    paid_twd: float  # 買進現金支出（含手續費），不因配發而改變
    stop: float
    initial_stop: float
    highest_close: float
    distribution_twd: float = 0.0
    # 出場受成交量上限、分多日賣完時：已賣部分的實收現金，以及進場股數（估算剩餘曝險用）。
    received_twd: float = 0.0
    entry_shares: int = 0

    def __post_init__(self):
        if self.entry_shares <= 0:
            self.entry_shares = self.shares

    @property
    def open_cost_twd(self) -> float:
        return self.paid_twd * self.shares / self.entry_shares if self.entry_shares else self.paid_twd


@dataclass
class SwingSummary:
    params: dict
    start: str = ""
    end: str = ""
    trade_start: str = ""
    initial_cash: float = 0.0
    cash: float = 0.0
    rounds: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)
    orders: list = field(default_factory=list)
    open_positions: list = field(default_factory=list)
    data_ended: list = field(default_factory=list)
    distribution_total_twd: float = 0.0
    buy_cost_total_twd: float = 0.0
    execution: dict = field(default_factory=dict)

    @property
    def realized_pnl_twd(self) -> float:
        return sum(r.pnl_twd for r in self.rounds)

    @property
    def net_pnl_twd(self) -> float:
        return (self.equity_curve[-1]["equity"] - self.initial_cash) if self.equity_curve else 0.0

    @property
    def return_pct(self) -> float:
        return 100 * self.net_pnl_twd / self.initial_cash if self.initial_cash else 0.0

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["rounds"] = [r.to_dict() for r in self.rounds]
        payload.update(realized_pnl_twd=self.realized_pnl_twd, net_pnl_twd=self.net_pnl_twd,
                       return_pct=self.return_pct,
                       round_metrics=summarize_rounds(self.rounds),
                       account_metrics=account_metrics(self.equity_curve, self.initial_cash,
                                                       self.buy_cost_total_twd))
        return payload


class _Series:
    """單一代號的指標用歷史（已向前還原除權息）。"""

    def __init__(self, size: int):
        self.highs: deque = deque(maxlen=size)
        self.lows: deque = deque(maxlen=size)
        self.closes: deque = deque(maxlen=size)

    def add(self, high: float, low: float, close: float) -> None:
        self.highs.append(high)
        self.lows.append(low)
        self.closes.append(close)

    def scale(self, factor: float) -> None:
        for values in (self.highs, self.lows, self.closes):
            scaled = [v * factor for v in values]
            values.clear()
            values.extend(scaled)

    def atr(self, days: int) -> Optional[float]:
        if len(self.closes) < days + 1:
            return None
        highs, lows, closes = list(self.highs), list(self.lows), list(self.closes)
        ranges = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
                  for i in range(len(closes) - days, len(closes))]
        return sum(ranges) / days

    def sma(self, days: int) -> Optional[float]:
        if len(self.closes) < days:
            return None
        return sum(list(self.closes)[-days:]) / days

    def breakout(self, days: int) -> bool:
        if len(self.closes) < days + 1:
            return False
        closes = list(self.closes)
        return closes[-1] > max(closes[-days - 1:-1])

    def momentum(self, days: int) -> Optional[float]:
        if len(self.closes) < days + 1:
            return None
        closes = list(self.closes)
        return closes[-1] / closes[-days - 1] - 1.0 if closes[-days - 1] > 0 else None


def _days(start: str, end: str) -> int:
    return (dt.date.fromisoformat(end[:10]) - dt.date.fromisoformat(start[:10])).days


class SwingBacktester:
    def __init__(self, settings: Optional[Settings] = None, params: Optional[SwingParams] = None, *,
                 execution: Optional[SmileExecution] = None, initial_cash: float = 500000.0,
                 data_end_sessions: int = 20):
        self.settings = settings or Settings()
        self.params = params or SwingParams()
        self.execution = execution or SmileExecution()
        self.initial_cash = float(initial_cash)
        self.data_end_sessions = int(data_end_sessions)
        if not math.isfinite(self.initial_cash) or self.initial_cash <= 0:
            raise ValueError("initial_cash must be finite and positive")

    def run_histories(self, histories: dict, *, trade_start: str = "",
                      ex_rights: Optional[dict] = None, random_entry=None,
                      revenue: Optional[dict] = None) -> SwingSummary:
        """Signals at close, fills at the next session's open. One cash account.

        trade_start: 這個日期之前的日 K 只用來暖機指標，不產生訊號。
        Buy orders live for one session and use only the cash available before that morning's
        sales. A sell that cannot fill (price band, volume) is retried on later sessions.
        random_entry: 隨機進場基準；給定時不看突破與均線，改用 "entry" 的機率決定。
        revenue: 規則二用的月營收 {代號: [(可用日期, 年增率%), ...]}（見 load_revenue）。
        """
        p = self.params
        if p.entry == "rs_revenue" and revenue is None and random_entry is None:
            raise ValueError("entry='rs_revenue' needs revenue data")
        revenue = revenue or {}
        revenue_dates = {s: [d for d, _ in rows] for s, rows in revenue.items()}

        def latest_yoy(sym: str, date: str) -> Optional[float]:
            i = bisect.bisect_right(revenue_dates.get(sym, []), date) - 1
            return revenue[sym][i][1] if i >= 0 else None

        calendar: dict = {}
        for sym, bars in histories.items():
            for bar in bars:
                date = bar.date[:10]
                if sym in calendar.setdefault(date, {}):
                    raise ValueError(f"duplicate daily bar: {sym} {date}")
                calendar[date][sym] = bar
        sessions = sorted(calendar)
        out = SwingSummary(params=asdict(p), start=sessions[0] if sessions else "",
                           end=sessions[-1] if sessions else "", trade_start=trade_start,
                           initial_cash=self.initial_cash, cash=self.initial_cash,
                           execution=self.execution.to_dict())
        if self.data_end_sessions > 0:
            for sym in sorted(histories):
                if histories[sym]:
                    last = max(b.date[:10] for b in histories[sym])
                    after = len(sessions) - sessions.index(last) - 1
                    if after > self.data_end_sessions:
                        out.data_ended.append(dict(symbol=sym, last_date=last, sessions_after=after,
                                                   forced_exit_date=None, exit_price=None,
                                                   pnl_twd=0.0))
        ended = {d["symbol"]: d for d in out.data_ended}
        events = {s: sorted(ex_rights.get(s, []), key=lambda ev: str(ev["ex_date"]))
                  for s in histories} if ex_rights else {}
        series = {s: _Series(p.warmup + 1) for s in histories}
        positions: dict[str, SwingPosition] = {}
        pending_buys: list[dict] = []
        pending_sells: dict[str, str] = {}  # 代號 → 出場原因
        marks: dict[str, float] = {}
        volumes: dict[str, float] = {}

        def valid(value) -> bool:
            return value is not None and math.isfinite(float(value)) and float(value) > 0

        def equity_base() -> float:
            return self.initial_cash + out.realized_pnl_twd

        def close_position(pos: SwingPosition, px: float, date: str, reason: str) -> float:
            received = sell_cash_received(px, pos.shares, UNIT, settings=self.settings,
                                          sell_tax_rate=SELL_TAX)
            out.cash += received
            pnl = received + pos.received_twd + pos.distribution_twd - pos.paid_twd
            out.rounds.append(Round(symbol=pos.symbol, entry_date=pos.entry_date, exit_date=date,
                                    lot_count=1, cost_twd=pos.paid_twd, pnl_twd=pnl,
                                    return_pct=100.0 * pnl / pos.paid_twd if pos.paid_twd else 0.0,
                                    hold_days=_days(pos.entry_date, date), exit_reason=reason))
            del positions[pos.symbol]
            pending_sells.pop(pos.symbol, None)
            return pnl

        for date in sessions:
            today = calendar[date]
            opening_cash = out.cash
            # 1. 除權息：還原指標歷史；持倉收現金，成本與停損價下修。
            for sym in sorted(today):
                queue = events.get(sym)
                while queue and str(queue[0]["ex_date"])[:10] <= date:
                    adjustment = ex_rights_adjustment(queue.pop(0))
                    if adjustment is None:
                        continue
                    per_share, factor = adjustment
                    series[sym].scale(factor)
                    if sym in marks:
                        marks[sym] *= factor
                    pos = positions.get(sym)
                    if pos:
                        cut = min(per_share, max(0.0, pos.entry_price - 0.01))
                        amount = cut * pos.shares
                        pos.entry_price -= cut
                        pos.distribution_twd += amount
                        pos.stop *= factor
                        pos.initial_stop *= factor
                        pos.highest_close *= factor
                        out.cash += amount
                        out.distribution_total_twd += amount
            # 2. 資料中斷：最後一根日 K 的下一個交易日以最後收盤價強制平倉。
            for sym, record in ended.items():
                if date > record["last_date"] and sym in positions:
                    px = self.execution.slipped(marks[sym], "sell", sym)
                    pnl = close_position(positions[sym], px, date, "data_end")
                    record.update(forced_exit_date=date, exit_price=px, pnl_twd=pnl)
            # 3. 開盤成交：先賣後買；買單只用賣出前的現金。
            for sym in sorted(pending_sells):
                bar, pos = today.get(sym), positions.get(sym)
                if bar is None or pos is None:
                    continue
                log = dict(symbol=sym, side="sell", execution_date=date, quantity=pos.shares,
                           reason=pending_sells[sym], status="cancelled", filled_quantity=0)
                out.orders.append(log)
                if not valid(bar.open):
                    log["blocked"] = "invalid_open"
                    continue
                px, blocked = self.execution.quote(float(bar.open), "sell", marks.get(sym), sym)
                qty = pos.shares
                if not blocked:
                    # 賣出與買進受同一個成交量上限；賣不完的部分留在佇列隔天續賣。
                    capacity = self.execution.capacity(volumes.get(sym))
                    if not math.isinf(capacity):
                        qty = min(qty, int(capacity))
                    if qty <= 0:
                        blocked = "volume_limit_partial_exit"
                if blocked:
                    log["blocked"] = blocked  # 留在佇列，下一個交易日再試
                    continue
                if qty < pos.shares:
                    got = sell_cash_received(px, qty, UNIT, settings=self.settings, sell_tax_rate=SELL_TAX)
                    out.cash += got
                    pos.received_twd += got
                    pos.shares -= qty
                    log.update(status="filled", filled_quantity=qty, execution_price=px, partial=True)
                    continue
                log.update(status="filled", filled_quantity=pos.shares, execution_price=px)
                close_position(pos, px, date, pending_sells[sym])
            budget = opening_cash
            orders, pending_buys = pending_buys, []
            for order in orders:
                sym, bar = order["symbol"], today.get(order["symbol"])
                log = dict(order, side="buy", execution_date=date, status="cancelled",
                           filled_quantity=0)
                out.orders.append(log)
                if bar is None or not valid(bar.open):
                    log["blocked"] = "no_bar_or_invalid_open"
                    continue
                if sym in positions or len(positions) >= p.max_positions:
                    log["blocked"] = "no_free_slot"
                    continue
                px, blocked = self.execution.quote(float(bar.open), "buy", marks.get(sym), sym)
                if blocked:
                    log["blocked"] = blocked
                    continue
                want = min(order["quantity"],
                           int(equity_base() * p.max_position_pct / 100.0 // px))
                capacity = self.execution.capacity(volumes.get(sym))
                if not math.isinf(capacity):
                    want = min(want, int(capacity))
                qty = max_affordable_qty(px, budget, UNIT, max_qty=want, settings=self.settings) \
                    if want > 0 else 0
                if qty <= 0:
                    log["blocked"] = "cash_volume_or_position_limit"
                    continue
                paid = buy_cash_required(px, qty, UNIT, settings=self.settings)
                out.cash -= paid
                budget -= paid
                out.buy_cost_total_twd += paid
                stop = px - p.initial_stop_atr * order["atr"]
                positions[sym] = SwingPosition(sym, date, px, qty, paid, stop, stop, px)
                log.update(status="filled", filled_quantity=qty, execution_price=px, stop=stop)
            # 4. 收盤：更新指標，決定明天的賣單與買單。
            candidates = []
            momenta: list[float] = []     # 規則二：當天所有算得出漲幅的代號，用來排名
            rs_pending: list[tuple] = []  # 規則二：等排名完才決定的候選
            for sym in sorted(today):
                bar = today[sym]
                volumes[sym] = float(bar.volume) if bar.volume is not None else 0.0
                if not (valid(bar.close) and valid(bar.high) and valid(bar.low)):
                    continue
                close = float(bar.close)
                marks[sym] = close
                s = series[sym]
                s.add(float(bar.high), float(bar.low), close)
                atr = s.atr(p.atr_days)
                if p.entry == "rs_revenue" and len(s.closes) >= p.warmup:
                    m = s.momentum(p.momentum_days)
                    if m is not None:
                        momenta.append(m)
                pos = positions.get(sym)
                if pos:
                    pos.highest_close = max(pos.highest_close, close)
                    if atr:
                        pos.stop = max(pos.stop, pos.highest_close - p.trail_atr * atr)
                    if sym not in pending_sells:
                        if close < pos.stop:
                            pending_sells[sym] = ("stop_loss" if pos.stop <= pos.initial_stop
                                                  else "trailing_stop")
                        elif _days(pos.entry_date, date) >= p.max_hold_days:
                            pending_sells[sym] = "expiry"
                    continue
                if sym in ended and date >= ended[sym]["last_date"]:
                    continue
                if (trade_start and date < trade_start) or not atr or len(s.closes) < p.warmup:
                    continue
                momentum = s.momentum(p.momentum_days)
                if momentum is None:
                    continue
                if random_entry is not None:
                    signal = random_entry.fires("entry")
                elif p.entry == "rs_revenue":
                    yoy = latest_yoy(sym, date)
                    if yoy is not None and yoy > p.yoy_min:
                        rs_pending.append((sym, momentum, atr))
                    continue
                else:
                    fast, slow = s.sma(p.fast_ma), s.sma(p.slow_ma)
                    signal = s.breakout(p.breakout_days) and fast is not None and fast > slow
                if signal:
                    quantity = int(equity_base() * p.risk_pct / 100.0 // (p.initial_stop_atr * atr))
                    if quantity > 0:
                        candidates.append((-momentum, sym, dict(symbol=sym, signal_date=date,
                                                                quantity=quantity, atr=atr,
                                                                momentum=momentum)))
            if rs_pending:
                ranked = sorted(momenta)
                n = len(ranked)
                for sym, momentum, atr in rs_pending:
                    rank = bisect.bisect_left(ranked, momentum) / (n - 1) if n > 1 else 0.5
                    if rank >= p.rs_min:
                        quantity = int(equity_base() * p.risk_pct / 100.0 // (p.initial_stop_atr * atr))
                        if quantity > 0:
                            candidates.append((-momentum, sym, dict(symbol=sym, signal_date=date,
                                                                    quantity=quantity, atr=atr,
                                                                    momentum=momentum, rs=rank)))
            slots = p.max_positions - len(positions)
            pending_buys = [c[2] for c in sorted(candidates, key=lambda c: c[:2])[:max(0, slots)]]
            # 5. 估值：持倉以收盤價扣出清滑價與費稅。
            liquidation = sum(
                sell_cash_received(self.execution.slipped(marks[s], "sell", s), pos.shares, UNIT,
                                   settings=self.settings, sell_tax_rate=SELL_TAX)
                for s, pos in positions.items())
            exposure = sum(pos.open_cost_twd for pos in positions.values())
            if trade_start and date < trade_start:
                # 暖機期只用來累積指標，不算帳戶期間；算進去會稀釋年化報酬、資金使用率與週轉率
                # （#19 驗證段的年化 20.9% 是用 2023-07 起算，交易期 2024-01 起算約 25%）。
                continue
            out.equity_curve.append(dict(
                date=date, cash=out.cash, equity=out.cash + liquidation,
                realized_pnl_twd=out.realized_pnl_twd, cost_exposure=exposure,
                positions=len(positions)))
        out.open_positions = [dict(asdict(pos), mark=marks.get(pos.symbol))
                              for pos in positions.values()]
        return out


def load_revenue(db, symbols: Sequence[str]) -> dict:
    """{代號: [(可用日期, 年增率%), ...]}。月營收法規在次月 10 日前公告，保守視為次月 11 日起才知道。"""
    wanted = sorted(set(symbols))
    out: dict = {s: [] for s in wanted}
    if not wanted:
        return out
    marks = ",".join("?" * len(wanted))
    rows = db.conn.execute(
        f"SELECT symbol, year_month, yoy_pct FROM monthly_revenue WHERE symbol IN ({marks}) "
        "AND yoy_pct IS NOT NULL ORDER BY symbol, year_month", wanted).fetchall()
    for sym, ym, yoy in rows:
        try:
            year, month, value = int(str(ym)[:4]), int(str(ym)[5:7]), float(yoy)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        out[sym].append((f"{year:04d}-{month:02d}-11", value))
    return out


def load_histories(db, symbols: Sequence[str], *, start: str = "", end: str = "",
                   with_ex_rights: bool = True) -> tuple:
    """(histories, ex_rights)，供 SwingBacktester 與 SmileCurveBacktester 共用同一份資料。"""
    from bot.smile_curve_backtest import load_ex_rights
    histories = {sym: db.get_price_history(sym, start=start or None, end=end or None,
                                           ascending=True) for sym in sorted(set(symbols))}
    events = load_ex_rights(db, list(histories), start=start, end=end) if with_ex_rights else None
    return histories, events
