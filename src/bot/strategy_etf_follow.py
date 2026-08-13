"""strategy_etf_follow -- 主動 ETF 跟單策略。

繼承 BaseStrategy，在原有當沖框架之上加入：
1. 啟動時讀取最近兩日 ETF 持股快照，計算共識加碼/新建倉訊號
2. 把共識訊號中的個股自動納入 SYMBOLS 監控池
3. 收到該個股 tick 時，若漲幅落在進場區間 + 共識足夠強 → 進場
4. 出場條件沿用 BaseStrategy 的停損 / 停利 / 移動停利 / 收盤全出

watch / report 模式同樣有效，會以虛擬部位記錄訊號便於回測比對。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from bot.active_etf import (
    HoldingsSnapshot,
    list_holdings_dates,
    load_active_etfs,
    load_holdings,
)
from bot.etf_consensus import (
    FollowSignal,
    consensus_additions,
    consensus_new_builds,
)
from bot.models import MarketTick
from bot.strategy import BaseStrategy
from bot.trade_cost import max_affordable_qty
from bot.utils import get_logger, now_tw_time

if TYPE_CHECKING:
    from bot.broker import SjBroker
    from bot.config import Settings
    from bot.market_source import TwsePublicMarketSource


class EtfFollowStrategy(BaseStrategy):
    """根據主動 ETF 共識持股變動跟單的策略。"""

    def __init__(
        self,
        broker: SjBroker | None,
        settings: Settings,
        market_source: TwsePublicMarketSource | None = None,
        logger: logging.Logger | None = None,
        publisher=None,
        *,
        wire_handlers: bool = True,
        min_consensus_new: int = 2,
        min_consensus_add: int = 3,
        max_pct_chg_on_entry: float = 4.0,
        project_root: Path | None = None,
    ):
        super().__init__(
            broker, settings, market_source, logger,
            publisher=publisher, wire_handlers=wire_handlers,
        )
        self.logger = logger or get_logger("strategy-etf")
        self.min_consensus_new = min_consensus_new
        self.min_consensus_add = min_consensus_add
        self.max_pct_chg_on_entry = max_pct_chg_on_entry
        self._project_root = project_root or Path.cwd()
        self._signals: dict[str, FollowSignal] = {}
        self._prepare_signals()

    # ------------------------------------------------------------------
    # 啟動：載入 ETF 共識訊號並擴充監控池
    # ------------------------------------------------------------------

    def _prepare_signals(self) -> None:
        etfs = load_active_etfs(self._project_root)
        if not etfs:
            self.logger.warning("無主動式 ETF 清單，跳過共識分析")
            return

        # 收集最近兩日的快照 (跨所有 ETF)
        snapshots_latest: dict[str, HoldingsSnapshot] = {}
        snapshots_prev: dict[str, HoldingsSnapshot] = {}
        for e in etfs:
            dates = list_holdings_dates(e.symbol, self._project_root)
            if len(dates) < 1:
                continue
            latest = load_holdings(e.symbol, dates[0], self._project_root)
            if latest:
                snapshots_latest[e.symbol] = latest
            if len(dates) >= 2:
                prev = load_holdings(e.symbol, dates[1], self._project_root)
                if prev:
                    snapshots_prev[e.symbol] = prev

        if not snapshots_latest:
            self.logger.info("尚無 ETF 持股快照可分析")
            return

        # 嘗試找一個共同的最近日期；簡化邏輯：把所有最新快照當作 'latest'
        # 把所有次新快照當作 'prev'，並逐 ETF 配對
        all_changes = []
        for sym, latest_snap in snapshots_latest.items():
            prev_snap = snapshots_prev.get(sym)
            if prev_snap:
                from bot.etf_consensus import diff_snapshots
                all_changes.extend(diff_snapshots(prev_snap, latest_snap))

        new_signals = consensus_new_builds(
            all_changes, min_etfs=self.min_consensus_new,
        )
        add_signals = consensus_additions(
            all_changes, min_etfs=self.min_consensus_add,
        )

        signals_by_ticker: dict[str, FollowSignal] = {}
        for s in new_signals + add_signals:
            existing = signals_by_ticker.get(s.ticker)
            if existing is None or s.etf_count > existing.etf_count:
                signals_by_ticker[s.ticker] = s
        self._signals = signals_by_ticker

        if not self._signals:
            self.logger.info("ETF 共識分析：未發現符合門檻的跟單訊號")
            return

        # 將共識個股加入監控
        original = set(self.settings.symbols)
        added: list[str] = []
        for ticker in self._signals.keys():
            if ticker not in original:
                self.settings.symbols.append(ticker)
                added.append(ticker)
        if added:
            self.logger.info(
                "ETF 共識跟單：新增監控 %d 檔 -> %s", len(added), added,
            )
        self.logger.info(
            "ETF 跟單訊號摘要: %s",
            {t: (s.signal_type, s.etf_count) for t, s in self._signals.items()},
        )

    @property
    def follow_signals(self) -> dict[str, FollowSignal]:
        """供 UI / 報表查詢的跟單訊號表。"""
        return dict(self._signals)

    # ------------------------------------------------------------------
    # Tick 邏輯
    # ------------------------------------------------------------------

    def on_tick(self, tick: MarketTick) -> None:
        symbol = tick.symbol
        price = tick.price
        self._last_price[symbol] = price
        cur_time = now_tw_time()

        if symbol not in self._prev_close:
            if tick.pct_chg:
                ref = price / (1 + tick.pct_chg / 100)
            else:
                ref = price
            self._prev_close[symbol] = ref

        prev_close = self._prev_close.get(symbol, 0)
        if prev_close <= 0:
            return

        pct_chg = 100 * (price - prev_close) / prev_close
        sig = self._signals.get(symbol)

        # ---- 進場 ----
        if (
            sig is not None
            and cur_time < self.settings.enter_cutoff_time
            and symbol not in self._enter_placed
            and not self._has_pending(symbol)
            and symbol not in self.positions
        ):
            if -1.0 < pct_chg < self.max_pct_chg_on_entry:
                lots = self._calc_lots(price, sig)
                if lots > 0:
                    self.logger.info(
                        "[跟單進場] %s %s 漲幅 %.2f%% price=%.2f lots=%d (%s)",
                        symbol, sig.name, pct_chg, price, lots, sig.note,
                    )
                    self._place_buy(
                        symbol, price, lots,
                        custom_field=sig.signal_type[:6],
                        pct_chg=pct_chg,
                    )

        # ---- 出場 (停損 / 移動停利 / 收盤全出) ----
        if cur_time < self.settings.exit_time:
            self._evaluate_standard_exits(symbol, price)

    # ------------------------------------------------------------------
    # 資金分配
    # ------------------------------------------------------------------

    def _calc_lots(self, price: float, signal: FollowSignal) -> int:
        """依共識強度動態調整下單張數 (基底 = MAX_LOT_PER_SYMBOL，含手續費)。"""
        if price <= 0:
            return 0
        base = self.settings.max_lot_per_symbol
        boost = 1
        if signal.signal_type == "consensus_new" and signal.etf_count >= 3 or signal.signal_type == "consensus_add" and signal.etf_count >= 5:
            boost = 2

        target_lots = base * boost
        remaining = self.risk.effective_fund_remaining()
        return max_affordable_qty(
            price, remaining, "lot",
            max_qty=target_lots,
            settings=self.settings,
        )


__all__ = ["EtfFollowStrategy"]
