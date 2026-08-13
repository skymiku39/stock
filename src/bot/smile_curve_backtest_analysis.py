"""smile_curve_backtest_analysis -- 微笑曲線多情境完整回測報告。"""

from __future__ import annotations

import datetime as dt
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from bot.config import Settings
from bot.smile_curve import SmileCurveEngine, SmileRoundTrip
from bot.stock_db import StockDB
from bot.trade_cost import buy_cash_required, net_pnl_twd

BacktestPriceMode = Literal["close", "ohlc"]


@dataclass
class BacktestScenario:
    label: str
    start: str
    end: str
    max_fund: int
    use_odd_lot: bool
    smile_buy_tiers: str
    smile_base_lot: int
    price_mode: BacktestPriceMode = "close"


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
    round_trips: list[SmileRoundTrip] = field(default_factory=list)
    skip_reason: str = ""
    notes: list[str] = field(default_factory=list)

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
        max_loss_twd=min(pnls),
        avg_hold_days=statistics.mean(holds) if holds else 0.0,
        avg_pnl_pct=statistics.mean(pcts),
    )


def run_scenario(
    db: StockDB,
    symbol: str,
    scenario: BacktestScenario,
    *,
    name: str = "",
    settings: Settings | None = None,
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
        report.notes.append("OHLC：當日低點加碼、高點獲利了結（同日樂觀假設）")
    else:
        report.notes.append("收盤價：僅用日 K 收盤（較保守）")
    if report.open_lots > 0:
        report.notes.append(f"期末仍有 {report.open_lots} 筆未平倉")
    return report


DEFAULT_SCENARIOS: list[BacktestScenario] = [
    BacktestScenario(
        label="長期整股標準",
        start="2020-01-01",
        end="2026-06-12",
        max_fund=600_000,
        use_odd_lot=False,
        smile_buy_tiers="1:1,3:2,5:3,8:4",
        smile_base_lot=1,
        price_mode="close",
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
        label="近期30萬配置收盤",
        start="2023-01-01",
        end="2026-06-12",
        max_fund=60_000,
        use_odd_lot=False,
        smile_buy_tiers="2:1,5:2,10:3",
        smile_base_lot=1,
        price_mode="close",
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


def audit_symbols(
    db: StockDB,
    symbols: Sequence[str],
    scenarios: Sequence[BacktestScenario] | None = None,
    *,
    names: dict[str, str] | None = None,
) -> list[ScenarioReport]:
    names = names or {}
    out: list[ScenarioReport] = []
    for sym in symbols:
        for sc in scenarios or DEFAULT_SCENARIOS:
            out.append(run_scenario(db, sym, sc, name=names.get(sym, sym)))
    return out


__all__ = [
    "DEFAULT_SCENARIOS",
    "BacktestScenario",
    "ScenarioReport",
    "audit_symbols",
    "run_scenario",
]
