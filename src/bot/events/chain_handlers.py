"""事件鏈 handler — 訂閱上游事件觸發下游流程 (Open/Closed 擴充)。"""

from __future__ import annotations

import logging
from pathlib import Path

from bot.events.types import QuantDataFetchCompleted
from bot.stock_db import StockDB, default_db_path
from bot.utils import get_logger, now_tw


class SmileScreenOnDataReadyHandler:
    """``QuantDataFetchCompleted`` → 自動微笑曲線選股 (SRP)。"""

    def __init__(
        self,
        *,
        project_root: Path | None = None,
        total_fund: float = 300_000,
        top_n: int = 5,
        lookback_years: int = 3,
        publisher=None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._root = project_root or Path.cwd()
        self._total_fund = total_fund
        self._top_n = top_n
        self._lookback_years = lookback_years
        self._publisher = publisher
        self._logger = logger or get_logger("events.chain")

    def __call__(self, event: QuantDataFetchCompleted) -> None:
        if not event.symbols:
            return
        end = event.end or now_tw().date().isoformat()
        start_year = int(end[:4]) - self._lookback_years
        start = event.start or f"{start_year}-01-01"

        self._logger.info(
            "事件鏈：資料就緒 (%d 檔)，觸發微笑曲線選股",
            len(event.symbols),
        )
        try:
            from bot.smile_curve_screener import screen_smile_candidates

            db = StockDB.open(path=default_db_path(self._root))
            report = screen_smile_candidates(
                db,
                root=self._root,
                start=start,
                end=end,
                total_fund=self._total_fund,
                top_n=self._top_n,
                extra_symbols=tuple(event.symbols),
                publisher=self._publisher,
            )
            self._logger.info(
                "事件鏈選股完成：候選 %d → 入選 %s",
                report.candidate_count,
                ",".join(report.selected_symbols) or "(無)",
            )
        except Exception:
            self._logger.exception("事件鏈微笑曲線選股失敗")
