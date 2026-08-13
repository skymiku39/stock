"""smile_curve_screener -- 微笑曲線複合選股（ETF 共識 + 波動回檔 + 資金可負擔 + 回測驗證）。"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from bot.active_etf import (
    HoldingsSnapshot,
    list_holdings_dates,
    load_active_etfs,
    load_holdings,
)
from bot.config import Settings
from bot.etf_consensus import (
    ConsensusHolding,
    build_consensus,
    consensus_additions,
    diff_snapshots,
)
from bot.events import SmileScreenCompleted
from bot.events.protocols import EventPublisher
from bot.events.wiring import publish_if_bus
from bot.smile_curve_backtest import SmileCurveBacktester, SmileSymbolResult


@dataclass
class PriceMetrics:
    symbol: str
    price: float = 0.0
    lot_cost: float = 0.0
    odd_shares_per_budget: int = 0
    ann_vol_pct: float = 0.0
    dip_3pct_days: int = 0
    dip_5pct_days: int = 0
    ref_resets: int = 0
    bar_count: int = 0
    skip_reason: str = ""


@dataclass
class SmileScreenCandidate:
    symbol: str
    name: str
    etf_count: int
    total_weight: float
    has_consensus_add: bool
    price: float
    lot_cost: float
    odd_shares_per_budget: int
    ann_vol_pct: float
    dip_5pct_days: int
    trade_count: int
    backtest_pnl_twd: float
    backtest_win_rate: float
    open_lots: int
    composite_score: float
    affordable: bool
    smile_fit: str
    rank: int = 0

    def to_dict(self) -> dict:
        return {
            "rank": self.rank,
            "symbol": self.symbol,
            "name": self.name,
            "etf_count": self.etf_count,
            "total_weight": round(self.total_weight, 2),
            "has_consensus_add": self.has_consensus_add,
            "price": round(self.price, 2),
            "lot_cost": int(self.lot_cost),
            "odd_shares_per_budget": self.odd_shares_per_budget,
            "ann_vol_pct": round(self.ann_vol_pct, 1),
            "dip_5pct_days": self.dip_5pct_days,
            "trade_count": self.trade_count,
            "backtest_pnl_twd": round(self.backtest_pnl_twd, 0),
            "backtest_win_rate": round(self.backtest_win_rate, 1),
            "open_lots": self.open_lots,
            "composite_score": round(self.composite_score, 1),
            "affordable": self.affordable,
            "smile_fit": self.smile_fit,
        }


@dataclass
class SmileScreenReport:
    candidates: list[SmileScreenCandidate] = field(default_factory=list)
    selected: list[SmileScreenCandidate] = field(default_factory=list)
    universe_size: int = 0
    filtered_size: int = 0
    start: str = ""
    end: str = ""
    total_fund: float = 0.0
    fund_per_symbol: float = 0.0
    top_n: int = 5
    notes: list[str] = field(default_factory=list)

    @property
    def settings_suggestion(self) -> dict:
        use_odd = self.fund_per_symbol < 150_000
        tiers = "2:1,5:2,10:3" if use_odd else "1:1,3:2,5:3,8:4"
        symbols = ",".join(c.symbol for c in self.selected)
        sell_targets = ",".join(f"{c.symbol}:8" for c in self.selected)
        return {
            "USE_ODD_LOT": str(use_odd).lower(),
            "SMILE_BUY_TIERS": tiers,
            "MAX_FUND": int(self.total_fund),
            "MAX_OPEN_POSITIONS": len(self.selected),
            "MANUAL_HOLD_SYMBOLS": symbols,
            "SELL_PROFIT_TARGETS": sell_targets,
            "symbols_per_slot": {
                c.symbol: int(self.fund_per_symbol) for c in self.selected
            },
        }


def load_latest_etf_snapshots(root: Path | None = None) -> dict[str, HoldingsSnapshot]:
    root = root or Path.cwd()
    out: dict[str, HoldingsSnapshot] = {}
    for etf in load_active_etfs(root):
        dates = list_holdings_dates(etf.symbol, root)
        if not dates:
            continue
        snap = load_holdings(etf.symbol, dates[0], root)
        if snap:
            out[etf.symbol] = snap
    return out


def load_prev_etf_snapshots(root: Path | None = None) -> dict[str, HoldingsSnapshot]:
    root = root or Path.cwd()
    out: dict[str, HoldingsSnapshot] = {}
    for etf in load_active_etfs(root):
        dates = list_holdings_dates(etf.symbol, root)
        if len(dates) < 2:
            continue
        snap = load_holdings(etf.symbol, dates[1], root)
        if snap:
            out[etf.symbol] = snap
    return out


def _consensus_add_tickers(root: Path, min_add: int = 3) -> set[str]:
    latest = load_latest_etf_snapshots(root)
    prev = load_prev_etf_snapshots(root)
    if not latest or not prev:
        return set()
    changes = []
    for sym, snap_latest in latest.items():
        snap_prev = prev.get(sym)
        if snap_prev:
            changes.extend(diff_snapshots(snap_prev, snap_latest))
    return {s.ticker for s in consensus_additions(changes, min_etfs=min_add)}


def compute_price_metrics(
    bars: Sequence,
    *,
    budget_per_symbol: float,
) -> PriceMetrics:
    if len(bars) < 50:
        sym = getattr(bars[0], "symbol", "") if bars else ""
        return PriceMetrics(symbol=sym, skip_reason="insufficient_bars")

    closes = [float(b.close) for b in bars]
    sym = bars[0].symbol
    dip_events = {3: 0, 5: 0}
    ref = closes[0]
    ref_resets = 0
    for c in closes:
        if c >= ref:
            if c > ref * 1.02:
                ref = c
                ref_resets += 1
        else:
            drop = 100.0 * (ref - c) / ref
            for t in (3, 5):
                if drop >= t:
                    dip_events[t] += 1

    rets = [(closes[i] / closes[i - 1] - 1.0) * 100.0 for i in range(1, len(closes))]
    vol = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    price = closes[-1]
    lot_cost = price * 1000.0
    odd_shares = int(budget_per_symbol / price) if price > 0 else 0

    return PriceMetrics(
        symbol=sym,
        price=price,
        lot_cost=lot_cost,
        odd_shares_per_budget=odd_shares,
        ann_vol_pct=vol * (252 ** 0.5),
        dip_3pct_days=dip_events[3],
        dip_5pct_days=dip_events[5],
        ref_resets=ref_resets,
        bar_count=len(bars),
    )


def _smile_fit_label(metrics: PriceMetrics, backtest: SmileSymbolResult) -> str:
    if metrics.skip_reason:
        return "資料不足"
    if backtest.trade_count >= 5:
        return "高適配"
    if backtest.trade_count >= 1:
        return "中適配"
    if metrics.dip_5pct_days >= 400 and metrics.ann_vol_pct >= 35:
        return "潛力（回測回合少）"
    if metrics.ann_vol_pct < 30:
        return "低波動"
    return "低適配"


def _composite_score(
    holding: ConsensusHolding,
    metrics: PriceMetrics,
    backtest: SmileSymbolResult,
    *,
    fund_per_symbol: float,
    use_odd_lot: bool,
    has_add: bool,
) -> float:
    affordable = (
        metrics.lot_cost <= fund_per_symbol * 2
        if not use_odd_lot
        else metrics.odd_shares_per_budget >= 10
    )
    score = 0.0
    score += min(holding.etf_count, 6) * 8.0
    score += min(metrics.ann_vol_pct, 60.0) * 0.4
    score += min(metrics.dip_5pct_days / 25.0, 20.0)
    score += min(backtest.trade_count * 2.5, 25.0)
    if backtest.total_pnl_twd > 0:
        score += min(backtest.total_pnl_twd / 5000.0, 10.0)
    if affordable:
        score += 12.0
    elif use_odd_lot and metrics.odd_shares_per_budget >= 5:
        score += 6.0
    if has_add:
        score += 8.0
    if backtest.open_lots > 5:
        score -= 5.0
    return score


def collect_universe_tickers(
    root: Path | None = None,
    *,
    min_etf_count: int = 2,
    universe_limit: int = 30,
    extra_symbols: Sequence[str] | None = None,
) -> list[str]:
    """取得待分析代號清單（共識池 + 額外指定）。"""
    root = root or Path.cwd()
    latest = load_latest_etf_snapshots(root)
    tickers: list[str] = []
    if latest:
        etf_meta = {e.symbol: e for e in load_active_etfs(root)}
        consensus_list = build_consensus(latest, etf_meta, min_etf_count=min_etf_count)
        if extra_symbols:
            by_ticker = {c.ticker: c for c in consensus_list}
            for sym in extra_symbols:
                tickers.append(sym)
            tickers = list(dict.fromkeys(tickers))
        else:
            tickers = [c.ticker for c in consensus_list[:universe_limit]]
    elif extra_symbols:
        tickers = list(extra_symbols)
    return tickers


def screen_smile_candidates(
    db,
    *,
    root: Path | None = None,
    start: str,
    end: str,
    total_fund: float = 300_000,
    top_n: int = 5,
    min_etf_count: int = 2,
    max_price: float = 300.0,
    min_ann_vol: float = 35.0,
    min_add_etfs: int = 3,
    use_odd_lot: bool | None = None,
    universe_limit: int = 30,
    extra_symbols: Sequence[str] | None = None,
    settings: Settings | None = None,
    publisher: EventPublisher | None = None,
) -> SmileScreenReport:
    """複合篩選：ETF 共識 → 價格/波動 → 微笑曲線回測 → 排序取 Top N。"""
    root = root or Path.cwd()
    fund_per_symbol = total_fund / max(1, top_n)
    if use_odd_lot is None:
        use_odd_lot = fund_per_symbol < 150_000

    report = SmileScreenReport(
        start=start,
        end=end,
        total_fund=total_fund,
        fund_per_symbol=fund_per_symbol,
        top_n=top_n,
    )

    latest = load_latest_etf_snapshots(root)
    if not latest and not extra_symbols:
        report.notes.append(
            "尚無 ETF 持股快照。請先到儀表板「主動 ETF 追蹤」匯入 CSV，"
            "或使用 --symbols 指定候選池。",
        )
        return report

    etf_meta = {e.symbol: e for e in load_active_etfs(root)}
    consensus_list = build_consensus(latest, etf_meta, min_etf_count=min_etf_count)
    consensus_by_ticker = {c.ticker: c for c in consensus_list}
    report.universe_size = len(consensus_list)

    add_tickers = _consensus_add_tickers(root, min_add=min_add_etfs)
    filtered: list[ConsensusHolding] = []
    if extra_symbols:
        for sym in extra_symbols:
            if sym in consensus_by_ticker:
                filtered.append(consensus_by_ticker[sym])
            else:
                filtered.append(ConsensusHolding(ticker=sym, name=sym))
    else:
        filtered = list(consensus_list[:universe_limit])
    report.filtered_size = len(filtered)

    bt_settings = settings or Settings(
        max_fund=int(fund_per_symbol),
        use_odd_lot=use_odd_lot,
        _env_file=None,
    )
    backtester = SmileCurveBacktester(bt_settings)

    candidates: list[SmileScreenCandidate] = []
    for holding in filtered:
        bars = db.get_price_history(holding.ticker, start=start, end=end, ascending=True)
        metrics = compute_price_metrics(bars, budget_per_symbol=fund_per_symbol)
        if metrics.skip_reason:
            continue
        if metrics.price <= 0:
            continue
        if max_price > 0 and metrics.price > max_price:
            continue
        if min_ann_vol > 0 and metrics.ann_vol_pct < min_ann_vol:
            continue

        bt = backtester.run_symbol(db, holding.ticker, start=start, end=end)
        affordable = (
            metrics.lot_cost <= fund_per_symbol * 2
            if not use_odd_lot
            else metrics.odd_shares_per_budget >= 10
        )
        score = _composite_score(
            holding,
            metrics,
            bt,
            fund_per_symbol=fund_per_symbol,
            use_odd_lot=use_odd_lot,
            has_add=holding.ticker in add_tickers,
        )
        candidates.append(SmileScreenCandidate(
            symbol=holding.ticker,
            name=holding.name,
            etf_count=holding.etf_count,
            total_weight=holding.total_weight,
            has_consensus_add=holding.ticker in add_tickers,
            price=metrics.price,
            lot_cost=metrics.lot_cost,
            odd_shares_per_budget=metrics.odd_shares_per_budget,
            ann_vol_pct=metrics.ann_vol_pct,
            dip_5pct_days=metrics.dip_5pct_days,
            trade_count=bt.trade_count,
            backtest_pnl_twd=bt.total_pnl_twd,
            backtest_win_rate=bt.win_rate,
            open_lots=bt.open_lots,
            composite_score=score,
            affordable=affordable,
            smile_fit=_smile_fit_label(metrics, bt),
        ))

    candidates.sort(
        key=lambda c: (c.composite_score, c.trade_count, c.etf_count),
        reverse=True,
    )
    for i, c in enumerate(candidates, start=1):
        c.rank = i
    report.candidates = candidates
    report.selected = candidates[:top_n]

    if not report.selected:
        report.notes.append(
            "篩選後無符合標的。可放寬 --max-price / --min-vol，或確認日 K 是否已入庫。",
        )
    else:
        report.notes.append(
            f"建議 {len(report.selected)} 檔，每檔預算約 {fund_per_symbol:,.0f} 元，"
            f"零股={'是' if use_odd_lot else '否'}。",
        )
    publish_if_bus(
        publisher,
        SmileScreenCompleted(
            total_fund=total_fund,
            top_n=top_n,
            selected_symbols=tuple(c.symbol for c in report.selected),
            candidate_count=len(report.candidates),
        ),
    )
    return report


__all__ = [
    "PriceMetrics",
    "SmileScreenCandidate",
    "SmileScreenReport",
    "collect_universe_tickers",
    "compute_price_metrics",
    "load_latest_etf_snapshots",
    "screen_smile_candidates",
]
