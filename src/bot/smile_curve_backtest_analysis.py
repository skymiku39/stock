"""smile_curve_backtest_analysis -- 微笑曲線多情境完整回測報告。"""

from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass, field
from typing import List, Literal, Optional, Sequence

from bot.config import Settings
from bot.smile_curve import SmileCurveEngine, SmileRoundTrip
from bot.smile_curve_backtest import SmileCurveBacktester, SmileBacktestSummary
from bot.stock_db import StockDB
from bot.trade_cost import buy_cash_required, net_pnl_pct, net_pnl_twd

BacktestPriceMode = Literal["next_open", "close", "ohlc"]


@dataclass
class BacktestScenario:
    label: str
    start: str
    end: str
    max_fund: int
    use_odd_lot: bool
    smile_buy_tiers: str
    smile_base_lot: int
    price_mode: BacktestPriceMode = "next_open"


@dataclass
class TradeStats:
    trade_count: int = 0
    win_count: int = 0
    win_rate: float = 0.0
    total_pnl_twd: float = 0.0
    avg_pnl_twd: float = 0.0
    median_pnl_twd: float = 0.0
    max_win_twd: float = 0.0
    max_loss_twd: float = 0.0
    avg_hold_days: float = 0.0
    avg_pnl_pct: float = 0.0


@dataclass
class ScenarioReport:
    scenario: BacktestScenario
    symbol: str
    name: str
    bar_count: int = 0
    first_date: str = ""
    last_date: str = ""
    start_price: float = 0.0
    end_price: float = 0.0
    buy_hold_return_pct: float = 0.0
    trade_stats: TradeStats = field(default_factory=TradeStats)
    cycle_count: int = 0
    open_lots: int = 0
    open_lots_cost_twd: float = 0.0
    open_lots_mtm_pnl_twd: float = 0.0
    round_trips: List[SmileRoundTrip] = field(default_factory=list)
    skip_reason: str = ""
    notes: List[str] = field(default_factory=list)
    portfolio: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        s = self.trade_stats
        sc = self.scenario
        return {
            "scenario": sc.label,
            "symbol": self.symbol,
            "name": self.name,
            "period": f"{sc.start} ~ {sc.end}",
            "price_mode": sc.price_mode,
            "max_fund": sc.max_fund,
            "use_odd_lot": sc.use_odd_lot,
            "tiers": sc.smile_buy_tiers,
            "bar_count": self.bar_count,
            "buy_hold_return_pct": round(self.buy_hold_return_pct, 2),
            "trade_count": s.trade_count,
            "win_rate": round(s.win_rate, 1),
            "total_pnl_twd": round(s.total_pnl_twd, 0),
            "avg_pnl_twd": round(s.avg_pnl_twd, 0),
            "avg_hold_days": round(s.avg_hold_days, 1),
            "open_lots": self.open_lots,
            "open_lots_mtm_pnl_twd": round(self.open_lots_mtm_pnl_twd, 0),
            "notes": self.notes,
            "net_pnl_twd": s.total_pnl_twd + self.open_lots_mtm_pnl_twd,
            "portfolio": self.portfolio,
        }


def _hold_days(entry: str, exit_: str) -> int:
    try:
        d0 = dt.date.fromisoformat(entry[:10])
        d1 = dt.date.fromisoformat(exit_[:10])
        return max(0, (d1 - d0).days)
    except ValueError:
        return 0


def _trade_stats(trips: Sequence[SmileRoundTrip]) -> TradeStats:
    if not trips:
        return TradeStats()
    pnls = [t.pnl_twd for t in trips]
    pcts = [t.pnl_pct for t in trips]
    wins = sum(1 for p in pnls if p > 0)
    holds = [_hold_days(t.entry_date, t.exit_date) for t in trips]
    return TradeStats(
        trade_count=len(trips),
        win_count=wins,
        win_rate=100.0 * wins / len(trips),
        total_pnl_twd=sum(pnls),
        avg_pnl_twd=statistics.mean(pnls),
        median_pnl_twd=statistics.median(pnls),
        max_win_twd=max(pnls),
        max_loss_twd=min(0.0, min(pnls)),
        avg_hold_days=statistics.mean(holds) if holds else 0.0,
        avg_pnl_pct=statistics.mean(pcts),
    )


def run_scenario(
    db: StockDB,
    symbol: str,
    scenario: BacktestScenario,
    *,
    name: str = "",
    settings: Optional[Settings] = None,
    portfolio_summary: Optional[SmileBacktestSummary] = None,
) -> ScenarioReport:
    bars = db.get_price_history(symbol, start=scenario.start, end=scenario.end, ascending=True)
    report = ScenarioReport(
        scenario=scenario,
        symbol=symbol,
        name=name or symbol,
        bar_count=len(bars),
    )
    if not bars:
        report.skip_reason = "no_daily_bars"
        return report

    report.first_date = bars[0].date[:10]
    report.last_date = bars[-1].date[:10]
    report.start_price = float(bars[0].close)
    report.end_price = float(bars[-1].close)
    if report.start_price > 0:
        report.buy_hold_return_pct = 100.0 * (
            report.end_price / report.start_price - 1.0
        )

    bt_settings = settings or Settings(
        max_fund=scenario.max_fund,
        use_odd_lot=scenario.use_odd_lot,
        smile_buy_tiers=scenario.smile_buy_tiers,
        smile_base_lot=scenario.smile_base_lot,
        _env_file=None,
    )
    if scenario.price_mode == "next_open":
        summary = portfolio_summary or SmileCurveBacktester(bt_settings).run_many(
            [symbol], db=db, start=scenario.start, end=scenario.end,
        )
        result = next(r for r in summary.results if r.symbol == symbol)
        report.round_trips = result.round_trips
        report.trade_stats = _trade_stats(result.round_trips)
        report.cycle_count = result.cycle_count
        report.open_lots = result.open_lots
        report.open_lots_cost_twd = result.open_cost_twd
        report.open_lots_mtm_pnl_twd = result.unrealized_pnl_twd
        report.portfolio = dict(initial_cash=summary.initial_cash,
                                net_pnl_twd=summary.net_pnl_twd,
                                return_pct=summary.return_pct,
                                max_drawdown_pct=summary.max_drawdown_pct,
                                max_drawdown_twd=summary.max_drawdown_twd,
                                symbols=[r.symbol for r in summary.results])
        report.notes = [summary.settings_note, "portfolio 為共用帳戶指標，不可逐檔加總"]
        return report
    if scenario.price_mode not in ("close", "ohlc"):
        raise ValueError(f"unsupported price mode: {scenario.price_mode}")
    engine = SmileCurveEngine(symbol, bt_settings)
    for bar in bars:
        trade_date = bar.date[:10]
        if scenario.price_mode == "ohlc":
            engine.on_bar_ohlc(
                trade_date,
                float(bar.open),
                float(bar.high),
                float(bar.low),
                float(bar.close),
            )
        else:
            engine.on_bar(trade_date, float(bar.close))

    trips = list(engine.round_trips)
    report.round_trips = trips
    report.trade_stats = _trade_stats(trips)
    report.cycle_count = engine.state.cycle_index
    report.open_lots = len(engine.open_lots)
    end_px = report.end_price
    for lot in engine.open_lots:
        cost = buy_cash_required(
            lot.entry_price, lot.quantity, lot.unit, settings=bt_settings,
        )
        report.open_lots_cost_twd += cost
        report.open_lots_mtm_pnl_twd += net_pnl_twd(
            lot.entry_price, end_px, lot.quantity, lot.unit,
            settings=bt_settings,
            sell_tax_rate=bt_settings.smile_regular_tax_rate,
        )

    if scenario.price_mode == "ohlc":
        report.notes.append("歷史偏誤對照 ONLY：當日低點加碼、高點獲利了結，不可作績效證據")
    else:
        report.notes.append("歷史偏誤對照 ONLY：收盤訊號同價成交，非次日成交模型")
    if report.open_lots > 0:
        report.notes.append(f"期末仍有 {report.open_lots} 筆未平倉")
    return report


COMPARISON_SCENARIOS: List[BacktestScenario] = [
    BacktestScenario(
        label="長期整股次日開盤",
        start="2020-01-01",
        end="2026-06-12",
        max_fund=600_000,
        use_odd_lot=False,
        smile_buy_tiers="1:1,3:2,5:3,8:4",
        smile_base_lot=1,
        price_mode="next_open",
    ),
    BacktestScenario(
        label="長期整股OHLC",
        start="2020-01-01",
        end="2026-06-12",
        max_fund=600_000,
        use_odd_lot=False,
        smile_buy_tiers="1:1,3:2,5:3,8:4",
        smile_base_lot=1,
        price_mode="ohlc",
    ),
    BacktestScenario(
        label="近期6萬配置次日開盤",
        start="2023-01-01",
        end="2026-06-12",
        max_fund=60_000,
        use_odd_lot=False,
        smile_buy_tiers="2:1,5:2,10:3",
        smile_base_lot=1,
        price_mode="next_open",
    ),
    BacktestScenario(
        label="近期30萬配置OHLC",
        start="2023-01-01",
        end="2026-06-12",
        max_fund=60_000,
        use_odd_lot=False,
        smile_buy_tiers="2:1,5:2,10:3",
        smile_base_lot=1,
        price_mode="ohlc",
    ),
    BacktestScenario(
        label="熊市區間整股",
        start="2022-01-01",
        end="2024-12-31",
        max_fund=600_000,
        use_odd_lot=False,
        smile_buy_tiers="1:1,3:2,5:3,8:4",
        smile_base_lot=1,
        price_mode="ohlc",
    ),
]

# Biased legacy fills are opt-in only; never present them as the default audit.
DEFAULT_SCENARIOS = [s for s in COMPARISON_SCENARIOS if s.price_mode == "next_open"]


def audit_symbols(
    db: StockDB,
    symbols: Sequence[str],
    scenarios: Optional[Sequence[BacktestScenario]] = None,
    *,
    names: Optional[dict[str, str]] = None,
) -> List[ScenarioReport]:
    names = names or {}
    out: List[ScenarioReport] = []
    for sc in scenarios or DEFAULT_SCENARIOS:
        summary = None
        if sc.price_mode == "next_open":
            settings = Settings(max_fund=sc.max_fund, daily_fund_budget=0,
                                use_odd_lot=sc.use_odd_lot, smile_buy_tiers=sc.smile_buy_tiers,
                                smile_base_lot=sc.smile_base_lot, _env_file=None)
            summary = SmileCurveBacktester(settings).run_many(
                symbols, db=db, start=sc.start, end=sc.end,
            )
        for sym in symbols:
            out.append(run_scenario(db, sym, sc, name=names.get(sym, sym),
                                    portfolio_summary=summary))
    return out


__all__ = [
    "BacktestScenario",
    "ScenarioReport",
    "DEFAULT_SCENARIOS",
    "audit_symbols",
    "run_scenario",
]
