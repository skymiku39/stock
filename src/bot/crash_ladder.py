"""crash_ladder -- 大盤大跌分批加碼策略的日 K 回測（只做回測，沒有盤中下單）。

研究依據：docs/reports/crash-rebound.md。6 次大跌事件中，跌越深買、之後 60 日越好，但事前不知道
會跌多深，「確認止跌」常是假確認，所以改用分批：

- 市場：回測名單的等權指數（收盤對收盤日報酬取平均；除權息日以還原後前收計算，同 bot.benchmark_index）。
- 事件：指數從前高回落；指數創新高時事件結束、加碼階數歸零。
- 加碼：回落第一次達 first_drop_pct、+step_pct、+2×step_pct ……（共 tranches 階）時，收盤發訊號、
  次日開盤買一批。同一天跨過幾階就買幾批的量。
- 買什麼：名單內當天有有效開盤價的全部標的，等金額（零股）。研究顯示大跌時買強勢或弱勢股沒有一致差異，
  所以不挑股。
- 每批金額：權益（起始資金 + 已實現損益）× 該階權重，不超過開盤前的現金。權重由 weighting 決定：
  equal＝每階 1/tranches；front＝第一階 1/2、其餘平分另一半；all_in＝第一階 100%、之後各階不買。
- 出場：每批持有 max_hold_days 日曆天，到期次日開盤賣出。沒有停損（大跌加碼本來就是承受再跌）。
- 成交假設沿用 SmileExecution（滑價、前一日成交量上限、漲跌停）；賣不完的部分隔天續賣。
- 除權息：持股收配發現金。資料中斷的標的以最後收盤價賣出（#34）。

random_entry：隨機進場基準。不看指數回落，每天以 "entry" 的機率買一批（同時最多 tranches 批），
出場與每批金額不變；用來檢驗「大跌時買」是否比「任意一天買」好。
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import asdict, dataclass, field
from typing import Optional

from bot.config import Settings
from bot.round_metrics import Round
from bot.smile_curve_backtest import ex_rights_adjustment
from bot.smile_execution import SmileExecution
from bot.trade_cost import buy_cash_required, max_affordable_qty, sell_cash_received

UNIT = "share"
SELL_TAX = 0.003


@dataclass(frozen=True)
class CrashLadderParams:
    first_drop_pct: float = 10.0
    step_pct: float = 5.0
    tranches: int = 3
    max_hold_days: int = 60
    weighting: str = "equal"   # equal／front／all_in（見模組說明）

    def __post_init__(self):
        if self.weighting not in ("equal", "front", "all_in"):
            raise ValueError("weighting must be equal, front or all_in")
        if not (math.isfinite(self.first_drop_pct) and 0 < self.first_drop_pct < 100):
            raise ValueError("first_drop_pct must be in (0, 100)")
        if not (math.isfinite(self.step_pct) and self.step_pct >= 0):
            raise ValueError("step_pct must be non-negative")
        if not isinstance(self.tranches, int) or self.tranches <= 0:
            raise ValueError("tranches must be a positive integer")
        if not isinstance(self.max_hold_days, int) or self.max_hold_days <= 0:
            raise ValueError("max_hold_days must be a positive integer")

    @property
    def levels(self) -> list[float]:
        return [self.first_drop_pct + k * self.step_pct for k in range(self.tranches)]

    @property
    def weights(self) -> list[float]:
        n = self.tranches
        if self.weighting == "all_in" or n == 1:
            return [1.0] + [0.0] * (n - 1)
        if self.weighting == "front":
            return [0.5] + [0.5 / (n - 1)] * (n - 1)
        return [1.0 / n] * n


@dataclass
class Tranche:
    entry_date: str
    label: str
    shares: dict = field(default_factory=dict)   # 代號 → 剩餘股數
    paid_twd: float = 0.0
    received_twd: float = 0.0
    distribution_twd: float = 0.0
    entry_symbols: int = 0
    exiting: bool = False

    def open_fraction(self, entry_shares: dict) -> float:
        total = sum(entry_shares.values())
        return sum(self.shares.values()) / total if total else 0.0


@dataclass
class CrashLadderSummary:
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


def _days(start: str, end: str) -> int:
    return (dt.date.fromisoformat(end[:10]) - dt.date.fromisoformat(start[:10])).days


class CrashLadderBacktester:
    def __init__(self, settings: Optional[Settings] = None, params: Optional[CrashLadderParams] = None, *,
                 execution: Optional[SmileExecution] = None, initial_cash: float = 500000.0,
                 data_end_sessions: int = 20):
        self.settings = settings or Settings()
        self.params = params or CrashLadderParams()
        self.execution = execution or SmileExecution()
        self.initial_cash = float(initial_cash)
        self.data_end_sessions = int(data_end_sessions)
        if not math.isfinite(self.initial_cash) or self.initial_cash <= 0:
            raise ValueError("initial_cash must be finite and positive")

    def run_histories(self, histories: dict, *, trade_start: str = "", ex_rights: Optional[dict] = None,
                      random_entry=None) -> CrashLadderSummary:
        """trade_start 之前只累積指數（用來找前高），不產生訊號、不記帳。"""
        p = self.params
        calendar: dict = {}
        for sym, bars in histories.items():
            for bar in bars:
                date = bar.date[:10]
                if sym in calendar.setdefault(date, {}):
                    raise ValueError(f"duplicate daily bar: {sym} {date}")
                calendar[date][sym] = bar
        sessions = sorted(calendar)
        out = CrashLadderSummary(params=asdict(p), start=sessions[0] if sessions else "",
                                 end=sessions[-1] if sessions else "", trade_start=trade_start,
                                 initial_cash=self.initial_cash, cash=self.initial_cash,
                                 execution=self.execution.to_dict())
        last_bar = {s: max(b.date[:10] for b in bars) for s, bars in histories.items() if bars}
        ended = {}
        if self.data_end_sessions > 0:
            for sym, last in sorted(last_bar.items()):
                after = len(sessions) - sessions.index(last) - 1
                if after > self.data_end_sessions:
                    ended[sym] = dict(symbol=sym, last_date=last, sessions_after=after, forced_exit_date=None)
            out.data_ended = list(ended.values())
        events = {s: sorted(ex_rights.get(s, []), key=lambda ev: str(ev["ex_date"]))
                  for s in histories} if ex_rights else {}
        marks: dict[str, float] = {}
        volumes: dict[str, float] = {}
        tranches: list[Tranche] = []
        entry_shares: dict[int, dict] = {}   # id(tranche) → 進場股數（估算剩餘成本用）
        pending_buy: Optional[dict] = None
        level, peak, crossed = 1.0, 1.0, 0

        def valid(value) -> bool:
            return value is not None and math.isfinite(float(value)) and float(value) > 0

        def equity_base() -> float:
            return self.initial_cash + out.realized_pnl_twd

        def sell(t: Tranche, sym: str, qty: int, px: float) -> None:
            got = sell_cash_received(px, qty, UNIT, settings=self.settings, sell_tax_rate=SELL_TAX)
            out.cash += got
            t.received_twd += got
            t.shares[sym] -= qty
            if t.shares[sym] <= 0:
                del t.shares[sym]

        def close_if_done(t: Tranche, date: str, reason: str) -> None:
            if t.shares:
                return
            pnl = t.received_twd + t.distribution_twd - t.paid_twd
            out.rounds.append(Round(symbol="BASKET", entry_date=t.entry_date, exit_date=date,
                                    lot_count=t.entry_symbols, cost_twd=t.paid_twd, pnl_twd=pnl,
                                    return_pct=100.0 * pnl / t.paid_twd if t.paid_twd else 0.0,
                                    hold_days=_days(t.entry_date, date), exit_reason=reason))
            tranches.remove(t)
            entry_shares.pop(id(t), None)

        for date in sessions:
            today = calendar[date]
            opening_cash = out.cash
            # 1. 除權息：持股收現金；估值用的前收同步還原。
            for sym in sorted(today):
                queue = events.get(sym)
                while queue and str(queue[0]["ex_date"])[:10] <= date:
                    adjustment = ex_rights_adjustment(queue.pop(0))
                    if adjustment is None:
                        continue
                    per_share, factor = adjustment
                    if sym in marks:
                        marks[sym] *= factor
                    for t in tranches:
                        if t.shares.get(sym):
                            amount = per_share * t.shares[sym]
                            t.distribution_twd += amount
                            out.cash += amount
                            out.distribution_total_twd += amount
            # 2. 資料中斷：最後一根日 K 之後的第一個交易日，以最後收盤價賣出。
            for sym, record in ended.items():
                if date > record["last_date"] and record["forced_exit_date"] is None:
                    record["forced_exit_date"] = date
                    px = self.execution.slipped(marks[sym], "sell", sym) if sym in marks else None
                    for t in list(tranches):
                        if t.shares.get(sym) and px:
                            sell(t, sym, t.shares[sym], px)
                            close_if_done(t, date, "data_end")
            # 3. 開盤：先賣後買；買單只用賣出前的現金。
            for t in list(tranches):
                if not t.exiting:
                    continue
                for sym in sorted(t.shares):
                    bar = today.get(sym)
                    if bar is None or not valid(bar.open):
                        continue
                    px, blocked = self.execution.quote(float(bar.open), "sell", marks.get(sym), sym)
                    if blocked:
                        continue
                    qty = t.shares[sym]
                    capacity = self.execution.capacity(volumes.get(sym))
                    if not math.isinf(capacity):
                        qty = min(qty, int(capacity))
                    if qty > 0:
                        sell(t, sym, qty, px)
                out.orders.append(dict(side="sell", execution_date=date, label=t.label,
                                       status="filled" if not t.shares else "partial",
                                       remaining_symbols=len(t.shares)))
                close_if_done(t, date, "expiry")
            if pending_buy is not None:
                order, pending_buy = pending_buy, None
                log = dict(order, side="buy", execution_date=date, status="cancelled", filled_quantity=0)
                out.orders.append(log)
                budget = min(opening_cash, order["amount"])
                names = [s for s in sorted(today) if valid(today[s].open)]
                t = Tranche(entry_date=date, label=order["label"])
                per_name = budget / len(names) if names else 0.0
                for sym in names:
                    px, blocked = self.execution.quote(float(today[sym].open), "buy", marks.get(sym), sym)
                    if blocked:
                        continue
                    want = int(per_name // px)
                    capacity = self.execution.capacity(volumes.get(sym))
                    if not math.isinf(capacity):
                        want = min(want, int(capacity))
                    qty = max_affordable_qty(px, min(per_name, out.cash), UNIT, max_qty=want,
                                             settings=self.settings) if want > 0 else 0
                    if qty <= 0:
                        continue
                    paid = buy_cash_required(px, qty, UNIT, settings=self.settings)
                    out.cash -= paid
                    t.paid_twd += paid
                    t.shares[sym] = qty
                out.buy_cost_total_twd += t.paid_twd
                if t.shares:
                    t.entry_symbols = len(t.shares)
                    tranches.append(t)
                    entry_shares[id(t)] = dict(t.shares)
                    log.update(status="filled", filled_quantity=len(t.shares), paid_twd=t.paid_twd)
                else:
                    log["blocked"] = "no_fill"
            # 4. 收盤：更新指數與標記價，決定明天的單。
            returns = []
            for sym in sorted(today):
                bar = today[sym]
                volumes[sym] = float(bar.volume) if bar.volume is not None else 0.0
                if not valid(bar.close):
                    continue
                close = float(bar.close)
                prior = marks.get(sym)
                if prior and prior > 0 and abs(close / prior - 1.0) < 0.3:
                    returns.append(close / prior - 1.0)
                marks[sym] = close
            if returns:
                level *= 1.0 + sum(returns) / len(returns)
            trading = not trade_start or date >= trade_start
            if level >= peak:
                peak, crossed = level, 0
            elif trading and random_entry is None:
                drop = 100.0 * (1.0 - level / peak)
                newly = sum(1 for x in p.levels[crossed:] if drop + 1e-9 >= x)
                if newly and pending_buy is None:
                    weight = sum(p.weights[crossed:crossed + newly])
                    crossed += newly
                    if weight > 0:
                        pending_buy = dict(signal_date=date, label=f"drop_{p.levels[crossed - 1]:g}%",
                                           amount=equity_base() * weight, drop_pct=drop)
            if trading and random_entry is not None:
                fired = random_entry.fires("entry")  # 每天都抽，亂數序列才不會錯位
                # 隨機基準的第 k 批（依目前持有批數）用第 k 階的權重；權重為 0 的階不買。
                slot = len(tranches)
                if fired and slot < p.tranches and pending_buy is None and p.weights[slot] > 0:
                    pending_buy = dict(signal_date=date, label="random",
                                       amount=equity_base() * p.weights[slot])
            for t in tranches:
                if not t.exiting and _days(t.entry_date, date) >= p.max_hold_days:
                    t.exiting = True
            # 5. 估值。
            liquidation = sum(
                sell_cash_received(self.execution.slipped(marks[s], "sell", s), q, UNIT,
                                   settings=self.settings, sell_tax_rate=SELL_TAX)
                for t in tranches for s, q in t.shares.items() if s in marks)
            exposure = sum(t.paid_twd * t.open_fraction(entry_shares.get(id(t), {})) for t in tranches)
            if not trading:
                continue
            out.equity_curve.append(dict(date=date, cash=out.cash, equity=out.cash + liquidation,
                                         realized_pnl_twd=out.realized_pnl_twd, cost_exposure=exposure,
                                         index_drawdown_pct=100.0 * (1.0 - level / peak),
                                         tranches=len(tranches)))
        out.open_positions = [dict(symbol="BASKET", entry_date=t.entry_date, label=t.label,
                                   symbols=len(t.shares), paid_twd=t.paid_twd) for t in tranches]
        return out


def crash_entry_rate(summary) -> dict:
    """隨機基準的每日進場機率 = 成交批數 ÷ 可交易日數。"""
    days = len(summary.equity_curve)
    fills = sum(1 for o in summary.orders if o["side"] == "buy" and o["status"] == "filled")
    return {"entry": fills / days} if days and fills else {}
