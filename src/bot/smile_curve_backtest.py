"""smile_curve_backtest -- 以日 K 回測微笑曲線策略。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from bot.config import Settings
from bot.smile_curve import SmileCurveEngine, SmileRoundTrip
from bot.stock_db import StockDB, default_db_path


@dataclass
class SmileSymbolResult:
    symbol: str
    round_trips: list[SmileRoundTrip] = field(default_factory=list)
    bar_count: int = 0
    cycle_count: int = 0
    open_lots: int = 0
    skip_reason: str = ""

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
    results: list[SmileSymbolResult]
    settings_note: str = ""
    start: str = ""
    end: str = ""

    @property
    def all_round_trips(self) -> list[SmileRoundTrip]:
        out: list[SmileRoundTrip] = []
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

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()

    def run_symbol(
        self,
        db: StockDB,
        symbol: str,
        *,
        start: str | None = None,
        end: str | None = None,
    ) -> SmileSymbolResult:
        bars = db.get_price_history(symbol, start=start, end=end, ascending=True)
        result = SmileSymbolResult(symbol=symbol, bar_count=len(bars))
        if not bars:
            result.skip_reason = "no_daily_bars"
            return result

        engine = SmileCurveEngine(symbol, self.settings)
        for bar in bars:
            trade_date = bar.date[:10]
            engine.on_bar(trade_date, float(bar.close))

        result.round_trips = list(engine.round_trips)
        result.cycle_count = engine.state.cycle_index
        result.open_lots = len(engine.open_lots)
        return result

    def run_many(
        self,
        symbols: Sequence[str],
        *,
        root: Path | None = None,
        start: str | None = None,
        end: str | None = None,
        db: StockDB | None = None,
    ) -> SmileBacktestSummary:
        database = db or StockDB.open(path=default_db_path(root or Path.cwd()))
        results = [
            self.run_symbol(database, sym, start=start, end=end)
            for sym in symbols
        ]
        tiers = getattr(self.settings, "smile_buy_tiers", "")
        note = (
            f"[微笑曲線] tiers={tiers} | base_lot={self.settings.smile_base_lot} | "
            f"tax={self.settings.smile_regular_tax_rate:.4f} | "
            f"fund_cap={self.settings.effective_fund_cap():.0f}"
        )
        return SmileBacktestSummary(
            results=results,
            settings_note=note,
            start=start or "",
            end=end or "",
        )
