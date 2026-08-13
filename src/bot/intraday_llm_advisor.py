"""盤中 LLM 顧問 — 定時與成交事件觸發的新聞/個股快取刷新與策略檢討。"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from bot.utils import get_logger, now_tw

if TYPE_CHECKING:
    from bot.config import Settings
    from bot.llm_gate import LlmGate
    from bot.risk_guard import RiskGuard

_MARKET_OPEN = __import__("datetime").time(9, 0)
_MARKET_REVIEW_END = __import__("datetime").time(13, 30)


class IntradayLlmAdvisor:
    """在盤中定期刷新 LLM 快取並產出 intraday_live_review 簡報。"""

    def __init__(
        self,
        settings: Settings,
        *,
        project_root: Path | None = None,
        risk: RiskGuard | None = None,
        llm_gate: LlmGate | None = None,
        logger: logging.Logger | None = None,
    ):
        self.settings = settings
        self.project_root = project_root or Path.cwd()
        self.risk = risk
        self.llm_gate = llm_gate
        self.logger = logger or get_logger("intraday-advisor")
        self._lock = threading.Lock()
        self._trigger = threading.Event()
        self._last_run_epoch = 0.0
        self._pending_reason = "scheduled"

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "llm_intraday_review_enabled", False))

    def request_refresh(self, reason: str) -> None:
        """成交或外部事件觸發一次檢討（背景執行）。"""
        if not self.enabled:
            return
        self._pending_reason = reason
        self._trigger.set()

    def run_loop(self, is_running: Callable[[], bool]) -> None:
        """阻塞式背景迴圈，由策略執行緒啟動。"""
        tick = 15
        while is_running():
            if not self.enabled:
                time.sleep(tick)
                continue

            in_window = self._in_review_window()
            interval_sec = self._review_interval_sec()
            due = in_window and (
                self._last_run_epoch <= 0
                or (time.time() - self._last_run_epoch) >= interval_sec
            )
            triggered = self._trigger.wait(timeout=tick)
            if triggered:
                self._trigger.clear()
                reason = self._pending_reason
                self._pending_reason = "scheduled"
            elif due:
                reason = "scheduled"
            else:
                continue

            if not in_window:
                continue
            try:
                self._run_cycle(reason)
            except Exception:
                self.logger.exception("盤中 LLM 顧問週期失敗 (%s)", reason)

    def _in_review_window(self) -> bool:
        now = now_tw()
        if now.weekday() >= 5:
            return False
        t = now.time()
        return _MARKET_OPEN <= t <= _MARKET_REVIEW_END

    def _has_open_positions(self) -> bool:
        if self.risk is None:
            return False
        try:
            return int(self.risk.open_positions_count) > 0
        except (TypeError, ValueError):
            return False

    def _review_interval_sec(self) -> int:
        """有持倉用較長間隔；空手時每 N 分鐘刷新 LLM 以尋找進場機會。"""
        if self._has_open_positions():
            minutes = int(getattr(self.settings, "llm_intraday_review_interval_min", 60))
        else:
            minutes = int(
                getattr(self.settings, "llm_intraday_review_interval_flat_min", 10),
            )
        return max(60, minutes * 60)

    def _run_cycle(self, reason: str) -> None:
        if not self.settings.gemini_api_key:
            self.logger.warning("略過盤中 LLM 顧問：未設定 GEMINI_API_KEY")
            return

        with self._lock:
            symbols = [s for s in (self.settings.symbols or []) if s]
            if not symbols:
                return

            flat = not self._has_open_positions()
            self.logger.info(
                "盤中 LLM 顧問開始 (%s) symbols=%s pnl=%.0f flat=%s interval=%dm",
                reason,
                ",".join(symbols),
                float(getattr(self.risk.state, "realized_pnl_twd", 0.0)) if self.risk else 0.0,
                flat,
                self._review_interval_sec() // 60,
            )
            self._refresh_symbol_caches(symbols)
            self._maybe_run_live_review(symbols, reason)
            self._last_run_epoch = time.time()
            self.logger.info("盤中 LLM 顧問完成 (%s)", reason)

    def _refresh_symbol_caches(self, symbols: list[str]) -> None:
        from bot.auto_llm import auto_analyze_ticker

        for symbol in symbols:
            try:
                auto_analyze_ticker(
                    symbol,
                    root=self.project_root,
                    force_refresh=True,
                    enable_calendar=False,
                    logger=self.logger,
                )
            except Exception:
                self.logger.exception("[%s] 盤中 LLM 快取刷新失敗", symbol)

    def _maybe_run_live_review(self, symbols: list[str], reason: str) -> None:
        from bot.intraday_live import build_live_tracking_rows, save_live_review
        from bot.intraday_pipeline import load_intraday_by_date
        from bot.llm_analyzer import GeminiClient, gemini_call
        from bot.market_macro import fetch_macro_snapshot, macro_to_dict
        from bot.news_fetcher import fetch_today_news, news_to_compact_text
        from bot.prompt_registry import get_registry

        today = now_tw().date()
        report = load_intraday_by_date(self.project_root, today)
        if not report:
            self.logger.info("尚無今日當沖戰情室報告，僅刷新個股 LLM 快取")
            return

        tracking = build_live_tracking_rows(
            report,
            root=self.project_root,
            max_tickers=max(12, len(symbols) + 4),
            refresh_quotes=True,
            refresh_technicals=True,
            refresh_chips=False,
            refresh_news=True,
            include_news=True,
            logger=self.logger,
        )
        macro = fetch_macro_snapshot(
            root=self.project_root,
            force_refresh=False,
            use_cache=True,
        )
        news_items = fetch_today_news(
            limit=120,
            use_cache=False,
            force_refresh=True,
            root=self.project_root,
            logger=self.logger,
        )
        client = GeminiClient(
            api_key=self.settings.gemini_api_key,
            model=self.settings.gemini_model,
        )
        registry = get_registry(self.project_root / "prompts")
        registry.reload()
        raw, info = gemini_call(
            "intraday_live_review",
            client=client,
            registry=registry,
            metadata={
                "task": "intraday_live_review",
                "asof": today.isoformat(),
                "trigger": reason,
            },
            asof_time=now_tw().isoformat(timespec="seconds"),
            original_brief=report.get("brief_md") or report.get("overall_brief") or "",
            themes_json=json.dumps(report.get("themes") or [], ensure_ascii=False, indent=2),
            live_rows_json=json.dumps(tracking.get("rows") or [], ensure_ascii=False, indent=2),
            macro_json=json.dumps(macro_to_dict(macro), ensure_ascii=False, indent=2),
            news_text=news_to_compact_text(news_items, max_chars=5000),
        )
        if not raw:
            self.logger.warning("intraday_live_review 未產出內容")
            return

        payload = {
            "generated_at": now_tw().isoformat(timespec="seconds"),
            "trigger": reason,
            "prompt_id": info.get("prompt_id", ""),
            "prompt_version": info.get("prompt_version", ""),
            "llm_info": info,
            "tracking": tracking,
            "monitored_symbols": symbols,
            "realized_pnl_twd": (
                float(self.risk.state.realized_pnl_twd) if self.risk else 0.0
            ),
        }
        save_live_review(
            root=self.project_root,
            report_date=today,
            markdown=raw,
            payload=payload,
        )


__all__ = ["IntradayLlmAdvisor"]
