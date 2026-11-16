"""round_metrics -- 以「回合」(一檔股票一次整倉出清) 為單位的雪球指標。

Strategy-agnostic, pure functions. 僅輸出統計，不影響任何交易、現金或損益。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass
from statistics import median
from typing import Callable, Iterable, Optional, Sequence

_REASON_MAP = {
    "close_signal_next_open": "take_profit",
    "rebound": "take_profit",
    "stop_loss_next_open": "stop_loss",
    "stop_loss": "stop_loss",
    "data_end": "data_end",  # 標的資料中斷（下市、合併）時以最後收盤價強制平倉
}  # "expiry" 保留給未來的到期出場


@dataclass
class Round:
    symbol: str
    entry_date: str
    exit_date: str
    lot_count: int
    cost_twd: float
    pnl_twd: float
    return_pct: float
    hold_days: int
    exit_reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def normalize_exit_reason(reason: str) -> str:
    return _REASON_MAP.get(reason, reason)


def _day(value: str) -> dt.date:
    return dt.date.fromisoformat(str(value)[:10])


def group_rounds(trips: Iterable, cost_fn: Callable) -> list[Round]:
    """Group lot round-trips by (symbol, exit_date) into rounds."""
    groups: dict[tuple[str, str], list] = {}
    for t in trips:
        groups.setdefault((t.symbol, str(t.exit_date)[:10]), []).append(t)
    rounds: list[Round] = []
    for (symbol, exit_date), lots in groups.items():
        entry = min(str(t.entry_date)[:10] for t in lots)
        cost = float(sum(cost_fn(t) for t in lots))
        pnl = float(sum(t.pnl_twd for t in lots))
        reasons = sorted({normalize_exit_reason(t.exit_reason) for t in lots})
        rounds.append(Round(
            symbol=symbol, entry_date=entry, exit_date=exit_date, lot_count=len(lots),
            cost_twd=cost, pnl_twd=pnl,
            return_pct=100.0 * pnl / cost if cost else 0.0,
            hold_days=(_day(exit_date) - _day(entry)).days,
            exit_reason="+".join(reasons)))
    rounds.sort(key=lambda r: (r.exit_date, r.symbol))
    return rounds


def summarize_rounds(rounds: Sequence[Round]) -> dict:
    n = len(rounds)
    if n == 0:
        return dict(round_count=0, win_count=0, win_rate=0.0, avg_win_twd=0.0,
                    avg_loss_twd=0.0, profit_factor=None, expectancy_twd=0.0,
                    expectancy_pct=0.0, hold_days_mean=0.0, hold_days_median=0.0,
                    hold_days_max=0, over_90_days_pct=0.0, exit_reason_counts={})
    wins = [r.pnl_twd for r in rounds if r.pnl_twd > 0]
    losses = [r.pnl_twd for r in rounds if r.pnl_twd < 0]
    gross_win, gross_loss = sum(wins), sum(losses)
    holds = [r.hold_days for r in rounds]
    reasons: dict[str, int] = {}
    for r in rounds:
        reasons[r.exit_reason] = reasons.get(r.exit_reason, 0) + 1
    return dict(
        round_count=n,
        win_count=len(wins),
        win_rate=100.0 * len(wins) / n,
        avg_win_twd=gross_win / len(wins) if wins else 0.0,
        avg_loss_twd=gross_loss / len(losses) if losses else 0.0,
        profit_factor=gross_win / abs(gross_loss) if losses else None,
        expectancy_twd=sum(r.pnl_twd for r in rounds) / n,
        expectancy_pct=sum(r.return_pct for r in rounds) / n,
        hold_days_mean=sum(holds) / n,
        hold_days_median=float(median(holds)),
        hold_days_max=max(holds),
        over_90_days_pct=100.0 * sum(1 for h in holds if h > 90) / n,
        exit_reason_counts=reasons)


def account_metrics(equity_curve: Sequence[dict], initial_cash: float,
                    buy_cost_total_twd: float) -> dict:
    out = dict(annualized_return_pct=0.0, max_drawdown_pct=0.0, return_over_mdd=None,
               avg_capital_utilization_pct=0.0, annual_turnover=None)
    if not equity_curve:
        return out
    days = (_day(equity_curve[-1]["date"]) - _day(equity_curve[0]["date"])).days
    years = days / 365.0 if days >= 1 else 0.0
    final = float(equity_curve[-1]["equity"])
    ann = 0.0
    if years > 0 and initial_cash > 0:
        ratio = final / initial_cash
        ann = 100.0 * (ratio ** (1.0 / years) - 1.0) if ratio > 0 else -100.0
    out["annualized_return_pct"] = ann
    peak, mdd = float(initial_cash), 0.0
    for p in equity_curve:
        eq = float(p["equity"])
        peak = max(peak, eq)
        if peak > 0:
            mdd = max(mdd, 100.0 * (peak - eq) / peak)
    out["max_drawdown_pct"] = mdd
    out["return_over_mdd"] = ann / mdd if mdd > 0 else None
    utils = [float(p.get("cost_exposure", 0.0)) / float(p["equity"])
             for p in equity_curve if float(p["equity"]) > 0]
    out["avg_capital_utilization_pct"] = 100.0 * sum(utils) / len(utils) if utils else 0.0
    mean_eq = sum(float(p["equity"]) for p in equity_curve) / len(equity_curve)
    out["annual_turnover"] = (buy_cost_total_twd / mean_eq / years
                              if years > 0 and mean_eq > 0 else None)
    return out
