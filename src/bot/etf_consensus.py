"""etf_consensus -- 共識持股、加碼/建倉偵測、抬轎效應計算。

核心輸出：
* ConsensusHolding -- 同一檔個股被多檔 ETF 持有的彙總
* HoldingChange   -- 一檔個股在兩個日期間的權重/張數變動
* FollowSignal    -- 跟單訊號 (建倉 / 共識加碼 / 抬轎候選)
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass, field

from bot.active_etf import (
    ActiveEtf,
    HoldingsSnapshot,
    list_holdings_dates,
)

# ----------------------------------------------------------------------
# 共識持股
# ----------------------------------------------------------------------


@dataclass
class EtfWeight:
    etf_symbol: str
    etf_name: str
    weight_pct: float
    shares: float = 0.0
    value: float = 0.0


@dataclass
class ConsensusHolding:
    """同一檔個股被多少 ETF 持有的彙總。"""

    ticker: str
    name: str
    held_by: list[EtfWeight] = field(default_factory=list)

    @property
    def etf_count(self) -> int:
        return len(self.held_by)

    @property
    def total_weight(self) -> float:
        return sum(w.weight_pct for w in self.held_by)

    @property
    def total_value(self) -> float:
        return sum(w.value for w in self.held_by)

    @property
    def avg_weight(self) -> float:
        return self.total_weight / max(1, self.etf_count)


def build_consensus(
    snapshots: dict[str, HoldingsSnapshot],
    etf_meta: dict[str, ActiveEtf],
    min_etf_count: int = 1,
) -> list[ConsensusHolding]:
    """跨多檔 ETF 彙總每檔個股的持有資訊，依被持有 ETF 數量遞減排序。"""
    by_ticker: dict[str, ConsensusHolding] = {}
    for etf_symbol, snap in snapshots.items():
        meta = etf_meta.get(etf_symbol)
        etf_name = meta.name if meta else etf_symbol
        for h in snap.holdings:
            if not h.ticker:
                continue
            ch = by_ticker.setdefault(
                h.ticker, ConsensusHolding(ticker=h.ticker, name=h.name),
            )
            if not ch.name and h.name:
                ch.name = h.name
            ch.held_by.append(EtfWeight(
                etf_symbol=etf_symbol,
                etf_name=etf_name,
                weight_pct=h.weight_pct,
                shares=h.shares,
                value=h.value,
            ))
    result = [c for c in by_ticker.values() if c.etf_count >= min_etf_count]
    result.sort(key=lambda c: (c.etf_count, c.total_weight), reverse=True)
    return result


# ----------------------------------------------------------------------
# 持股變動偵測
# ----------------------------------------------------------------------


@dataclass
class HoldingChange:
    """個股在某 ETF 兩個日期間的變動。"""

    etf_symbol: str
    ticker: str
    name: str = ""
    weight_pct_before: float = 0.0
    weight_pct_after: float = 0.0
    shares_before: float = 0.0
    shares_after: float = 0.0

    @property
    def weight_delta(self) -> float:
        return self.weight_pct_after - self.weight_pct_before

    @property
    def shares_delta(self) -> float:
        return self.shares_after - self.shares_before

    @property
    def change_type(self) -> str:
        if self.weight_pct_before == 0 and self.weight_pct_after > 0:
            return "新建倉"
        if self.weight_pct_after == 0 and self.weight_pct_before > 0:
            return "清倉"
        if self.weight_delta > 0.05:
            return "加碼"
        if self.weight_delta < -0.05:
            return "減碼"
        return "持平"


def diff_snapshots(
    before: HoldingsSnapshot,
    after: HoldingsSnapshot,
) -> list[HoldingChange]:
    """比較同一 ETF 兩個日期的持股，回傳所有非持平變動。"""
    assert before.symbol == after.symbol
    b_map = before.by_ticker()
    a_map = after.by_ticker()
    tickers = set(b_map) | set(a_map)
    changes: list[HoldingChange] = []
    for t in tickers:
        b = b_map.get(t)
        a = a_map.get(t)
        change = HoldingChange(
            etf_symbol=after.symbol,
            ticker=t,
            name=(a.name if a else b.name) if a or b else "",
            weight_pct_before=b.weight_pct if b else 0.0,
            weight_pct_after=a.weight_pct if a else 0.0,
            shares_before=b.shares if b else 0.0,
            shares_after=a.shares if a else 0.0,
        )
        if change.change_type != "持平":
            changes.append(change)
    changes.sort(key=lambda c: abs(c.weight_delta), reverse=True)
    return changes


def latest_two_dates(
    symbol: str,
    root: object | None = None,
) -> tuple | None:
    """取得指定 ETF 最近兩個持股快照日期，不足兩日則回 None。"""
    dates = list_holdings_dates(symbol, root)
    if len(dates) < 2:
        return None
    return dates[1], dates[0]


def detect_changes_across_etfs(
    snapshots_by_date: dict[dt.date, dict[str, HoldingsSnapshot]],
    latest_date: dt.date,
    previous_date: dt.date,
) -> list[HoldingChange]:
    """在指定的兩個快照日期之間，跨所有 ETF 彙整出所有持股變動。"""
    latest = snapshots_by_date.get(latest_date, {})
    previous = snapshots_by_date.get(previous_date, {})
    all_changes: list[HoldingChange] = []
    for sym, snap_after in latest.items():
        snap_before = previous.get(sym)
        if snap_before is None:
            continue
        all_changes.extend(diff_snapshots(snap_before, snap_after))
    return all_changes


# ----------------------------------------------------------------------
# 跟單訊號
# ----------------------------------------------------------------------


@dataclass
class FollowSignal:
    """跟單候選訊號。"""

    ticker: str
    name: str
    signal_type: str  # "consensus_new" | "consensus_add" | "elevator"
    etf_count: int
    total_weight_delta: float
    note: str = ""
    related_etfs: list[str] = field(default_factory=list)


def consensus_new_builds(
    changes: Iterable[HoldingChange],
    min_etfs: int = 2,
) -> list[FollowSignal]:
    """新建倉共識：若同一個股在最近一次更新中被 >= N 檔 ETF 同步新建倉 → 強烈訊號。"""
    by_ticker: dict[str, list[HoldingChange]] = {}
    for c in changes:
        if c.change_type == "新建倉":
            by_ticker.setdefault(c.ticker, []).append(c)
    signals: list[FollowSignal] = []
    for ticker, items in by_ticker.items():
        if len(items) >= min_etfs:
            signals.append(FollowSignal(
                ticker=ticker,
                name=items[0].name,
                signal_type="consensus_new",
                etf_count=len(items),
                total_weight_delta=sum(i.weight_delta for i in items),
                note=f"{len(items)} 檔主動 ETF 同步新建倉",
                related_etfs=[i.etf_symbol for i in items],
            ))
    signals.sort(key=lambda s: s.etf_count, reverse=True)
    return signals


def consensus_additions(
    changes: Iterable[HoldingChange],
    min_etfs: int = 3,
    min_weight_delta: float = 0.1,
) -> list[FollowSignal]:
    """共識加碼：>= N 檔 ETF 同步加碼且累計權重增幅達門檻。"""
    by_ticker: dict[str, list[HoldingChange]] = {}
    for c in changes:
        if c.change_type == "加碼":
            by_ticker.setdefault(c.ticker, []).append(c)
    signals: list[FollowSignal] = []
    for ticker, items in by_ticker.items():
        if len(items) < min_etfs:
            continue
        total = sum(i.weight_delta for i in items)
        if total < min_weight_delta:
            continue
        signals.append(FollowSignal(
            ticker=ticker,
            name=items[0].name,
            signal_type="consensus_add",
            etf_count=len(items),
            total_weight_delta=total,
            note=f"{len(items)} 檔主動 ETF 共識加碼 (+{total:.2f}%)",
            related_etfs=[i.etf_symbol for i in items],
        ))
    signals.sort(key=lambda s: (s.etf_count, s.total_weight_delta), reverse=True)
    return signals


def elevator_candidates(
    consensus: list[ConsensusHolding],
    market_cap_provider: dict[str, float],
    threshold_rank: int = 50,
    window: int = 50,
) -> list[FollowSignal]:
    """抬轎候選：個股當前市值排名落在 (threshold_rank, threshold_rank + window]
    區間，且被多檔主動 ETF 持有 → 可能在未來指數調整時被被動 ETF 強制納入。

    market_cap_provider: {ticker: market_cap}
    """
    if not market_cap_provider:
        return []
    sorted_by_cap = sorted(
        market_cap_provider.items(), key=lambda x: x[1], reverse=True,
    )
    rank_map: dict[str, int] = {t: i + 1 for i, (t, _) in enumerate(sorted_by_cap)}

    signals: list[FollowSignal] = []
    for c in consensus:
        rank = rank_map.get(c.ticker)
        if rank is None:
            continue
        if not (threshold_rank < rank <= threshold_rank + window):
            continue
        if c.etf_count < 2:
            continue
        signals.append(FollowSignal(
            ticker=c.ticker,
            name=c.name,
            signal_type="elevator",
            etf_count=c.etf_count,
            total_weight_delta=c.total_weight,
            note=(
                f"市值排名 {rank} (前 {threshold_rank} 名門檻)，"
                f"被 {c.etf_count} 檔主動 ETF 持有"
            ),
            related_etfs=[w.etf_symbol for w in c.held_by],
        ))
    signals.sort(key=lambda s: (s.etf_count, -rank_map.get(s.ticker, 9999)))
    return signals


__all__ = [
    "ConsensusHolding",
    "EtfWeight",
    "FollowSignal",
    "HoldingChange",
    "build_consensus",
    "consensus_additions",
    "consensus_new_builds",
    "detect_changes_across_etfs",
    "diff_snapshots",
    "elevator_candidates",
    "latest_two_dates",
]
