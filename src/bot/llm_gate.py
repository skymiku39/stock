"""LLM 進出場閘門 — 讀本地 auto_llm 快取與當沖評分，決定是否允許進出場。"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bot.models import PositionInfo
from bot.scoring import compute_scorecard
from bot.utils import get_logger, now_tw

if TYPE_CHECKING:
    from bot.config import Settings
    from bot.risk_guard import RiskGuard

AUTO_LLM_DIR = "data/auto_llm"
DEFAULT_CACHE_HOURS = 12


@dataclass
class EntryVerdict:
    allowed: bool
    reason: str = ""
    sentiment_score: float = 0.0
    day_trade_score: float = 0.0
    sentiment: str = ""
    confidence: float = 0.0

    @property
    def summary(self) -> str:
        if self.allowed:
            return (
                f"allow ss={self.sentiment_score:.2f} "
                f"dt={self.day_trade_score:.1f}"
            )
        return f"block:{self.reason}"


@dataclass
class ExitVerdict:
    """賣出前 AI 閘門結果。"""
    allowed: bool
    reason: str = ""
    sentiment_score: float = 0.0
    day_trade_score: float = 0.0
    sentiment: str = ""
    confidence: float = 0.0

    @property
    def summary(self) -> str:
        if self.allowed:
            return (
                f"sell_ok ss={self.sentiment_score:.2f} "
                f"dt={self.day_trade_score:.1f} ({self.reason})"
            )
        return f"hold:{self.reason}"


@dataclass
class GateSnapshot:
    """供 dashboard 顯示的閘門狀態。"""
    symbol: str
    has_cache: bool
    sentiment: str = ""
    sentiment_score: float = 0.0
    confidence: float = 0.0
    day_trade_score: float = 0.0
    allow_entry: bool = True
    block_reason: str = ""
    fetched_at: str = ""


class LlmGate:
    """依 LLM 快取與評分系統守衛進出場。"""

    def __init__(
        self,
        settings: Settings,
        risk: RiskGuard | None = None,
        project_root: Path | None = None,
        logger: logging.Logger | None = None,
    ):
        self.settings = settings
        self.risk = risk
        self.project_root = project_root or Path.cwd()
        self.logger = logger or get_logger("llm-gate")
        self._refresh_lock = threading.Lock()
        self._refreshing: set[str] = set()

    def _llm_path(self, symbol: str) -> Path:
        return self.project_root / AUTO_LLM_DIR / f"{symbol}.json"

    def load_llm_cache(self, symbol: str) -> dict[str, Any] | None:
        path = self._llm_path(symbol)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            self.logger.exception("[%s] 讀取 auto_llm 快取失敗", symbol)
            return None

    def _day_trade_score(
        self,
        symbol: str,
        price: float,
        pct_chg: float,
        llm_data: dict[str, Any] | None,
    ) -> float:
        try:
            card = compute_scorecard(
                ticker=symbol,
                price=price,
                pct_change=pct_chg,
                llm_analysis=llm_data,
            )
            tf = card.timeframes.get("day_trade")
            if tf is None:
                return 0.0
            return float(tf.total)
        except Exception:
            self.logger.exception("[%s] compute_scorecard 失敗", symbol)
            return 0.0

    def _cache_stale(self, llm_data: dict[str, Any] | None) -> bool:
        if not llm_data:
            return True
        fetched = llm_data.get("fetched_at", "")
        if not fetched:
            return True
        try:
            from datetime import datetime

            ts = datetime.fromisoformat(str(fetched).replace("Z", "+00:00"))
            if ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
            age_h = (now_tw().replace(tzinfo=None) - ts).total_seconds() / 3600
            return age_h > DEFAULT_CACHE_HOURS
        except Exception:
            return True

    def _maybe_refresh_background(
        self,
        symbol: str,
        *,
        on_exit: bool = False,
    ) -> None:
        if on_exit:
            if not getattr(self.settings, "llm_refresh_on_exit", False):
                return
        elif not getattr(self.settings, "llm_refresh_on_entry", False):
            return
        if not self.settings.gemini_api_key:
            return
        with self._refresh_lock:
            if symbol in self._refreshing:
                return
            self._refreshing.add(symbol)

        def _worker() -> None:
            try:
                from bot.auto_llm import auto_analyze_ticker

                auto_analyze_ticker(
                    symbol,
                    root=self.project_root,
                    force_refresh=True,
                    enable_calendar=False,
                    logger=self.logger,
                )
            except Exception:
                self.logger.exception("[%s] 背景 LLM 刷新失敗", symbol)
            finally:
                with self._refresh_lock:
                    self._refreshing.discard(symbol)

        tag = "llm-refresh-exit" if on_exit else "llm-refresh"
        threading.Thread(
            target=_worker, daemon=True, name=f"{tag}-{symbol}",
        ).start()

    def allow_entry(
        self,
        symbol: str,
        price: float,
        pct_chg: float,
    ) -> EntryVerdict:
        """檢查 LLM 閘門是否允許進場。"""
        llm_data = self.load_llm_cache(symbol)
        if self._cache_stale(llm_data):
            self._maybe_refresh_background(symbol)
        return self._evaluate_entry(
            symbol, price, pct_chg, llm_data, record_block=True,
        )

    def has_limit_up_potential(self, symbol: str) -> bool:
        """LLM 判定具漲停潛力時，移動停利可略過 trailing_stop_pct 回撤規則。"""
        llm_data = self.load_llm_cache(symbol)
        if not llm_data:
            return False
        return bool(llm_data.get("limit_up_potential", False))

    def should_exit(self, symbol: str, position: PositionInfo) -> str | None:
        """持倉中若 LLM 轉負向，回傳出場原因字串。"""
        if not getattr(self.settings, "llm_exit_on_negative", False):
            return None
        if not getattr(self.settings, "llm_gate_enabled", False):
            return None

        llm_data = self.load_llm_cache(symbol)
        if not llm_data:
            return None

        sentiment = str(llm_data.get("sentiment", "")).lower()
        confidence = float(llm_data.get("confidence", 0.0) or 0.0)
        min_conf = float(getattr(self.settings, "llm_sell_min_confidence", 0.5))
        if sentiment == "negative" and confidence >= min_conf:
            return "llm_neg"
        return None

    def allow_exit(
        self,
        symbol: str,
        position: PositionInfo,
        price: float,
        pnl_pct: float,
        trigger_reason: str,
    ) -> ExitVerdict:
        """賣出前 AI 分析：策略觸發賣出訊號後，由此決定是否實際送單。"""
        if not getattr(self.settings, "llm_sell_gate_enabled", False):
            return ExitVerdict(True, "gate_disabled")

        normalized = (trigger_reason or "").lower()
        if (
            normalized in ("sl", "stop", "stoploss")
            and getattr(self.settings, "llm_sell_gate_bypass_stop_loss", True)
        ):
            return ExitVerdict(True, "stop_loss_bypass")

        if (
            normalized == "close"
            and getattr(self.settings, "llm_sell_gate_bypass_close", True)
        ):
            return ExitVerdict(True, "close_bypass")

        if (
            normalized == "afternoon"
            and getattr(self.settings, "llm_sell_gate_bypass_afternoon", True)
        ):
            return ExitVerdict(True, "afternoon_bypass")

        llm_data = self.load_llm_cache(symbol)
        if self._cache_stale(llm_data):
            self._maybe_refresh_background(symbol, on_exit=True)

        prev_close = price / (1 + pnl_pct / 100) if pnl_pct != -100 else price
        pct_chg = 0.0
        if prev_close > 0:
            pct_chg = 100 * (price - prev_close) / prev_close

        return self._evaluate_exit(
            symbol,
            position,
            price,
            pnl_pct,
            pct_chg,
            trigger_reason,
            llm_data,
            record_block=True,
        )

    def _evaluate_exit(
        self,
        symbol: str,
        position: PositionInfo,
        price: float,
        pnl_pct: float,
        pct_chg: float,
        trigger_reason: str,
        llm_data: dict[str, Any] | None,
        *,
        record_block: bool,
    ) -> ExitVerdict:
        """依 LLM 快取與觸發原因，判斷是否允許賣出。"""
        normalized = (trigger_reason or "").lower()

        if not llm_data:
            detail = f"無 {symbol} LLM 快取，暫不賣出"
            if record_block and self.risk is not None:
                self.risk.record_blocked_attempt(symbol, "llm_sell_gate", detail)
            return ExitVerdict(False, "no_llm_cache")

        sentiment = str(llm_data.get("sentiment", "neutral")).lower()
        sentiment_score = float(llm_data.get("sentiment_score", 0.0) or 0.0)
        confidence = float(llm_data.get("confidence", 0.0) or 0.0)
        day_score = self._day_trade_score(symbol, price, pct_chg, llm_data)
        min_conf = float(getattr(self.settings, "llm_sell_min_confidence", 0.5))

        def _allow(reason: str) -> ExitVerdict:
            return ExitVerdict(
                True, reason, sentiment_score, day_score, sentiment, confidence,
            )

        def _hold(reason: str, detail: str) -> ExitVerdict:
            if record_block and self.risk is not None:
                self.risk.record_blocked_attempt(symbol, "llm_sell_gate", detail)
            return ExitVerdict(
                False, reason, sentiment_score, day_score, sentiment, confidence,
            )

        if sentiment == "negative" and confidence >= min_conf:
            return _allow("negative_sentiment")

        if normalized in ("target", "llm_neg"):
            return _allow("trigger_confirmed")

        if pnl_pct < 0 and sentiment != "positive":
            return _allow("loss_with_neutral_or_negative")

        if (
            sentiment == "positive"
            and confidence >= min_conf
            and sentiment_score >= float(
                getattr(self.settings, "llm_min_sentiment_score", 0.2),
            )
        ):
            detail = (
                f"AI 建議續抱 sentiment={sentiment} "
                f"score={sentiment_score:.2f} conf={confidence:.2f} "
                f"[{trigger_reason}]"
            )
            return _hold("ai_hold_positive", detail)

        if normalized in ("trail", "target", "afternoon") and pnl_pct > 0:
            return _allow("take_profit_signal")

        if normalized == "close":
            if sentiment == "neutral" or pnl_pct <= 0:
                return _allow("close_neutral_or_loss")
            return _hold(
                "ai_hold_close",
                f"收盤觸發但 AI 偏多，暫不賣出 [{trigger_reason}]",
            )

        if normalized in ("sl", "stop", "stoploss"):
            return _allow("stop_loss_no_bypass")

        return _allow("default_allow")

    def _evaluate_entry(
        self,
        symbol: str,
        price: float,
        pct_chg: float,
        llm_data: dict[str, Any] | None,
        *,
        record_block: bool,
    ) -> EntryVerdict:
        """內部評估進場 (可選是否寫入 risk_state)。"""
        if not getattr(self.settings, "llm_gate_enabled", False):
            return EntryVerdict(True, "gate_disabled")

        if not llm_data:
            reason = "no_llm_cache"
            if record_block and self.risk is not None:
                self.risk.record_blocked_attempt(
                    symbol, "llm_gate", f"無 {symbol} LLM 快取",
                )
            return EntryVerdict(False, reason)

        sentiment = str(llm_data.get("sentiment", "neutral")).lower()
        sentiment_score = float(llm_data.get("sentiment_score", 0.0) or 0.0)
        confidence = float(llm_data.get("confidence", 0.0) or 0.0)
        day_score = self._day_trade_score(symbol, price, pct_chg, llm_data)

        min_sent = float(getattr(self.settings, "llm_min_sentiment_score", 0.2))
        min_dt = float(getattr(self.settings, "llm_min_day_trade_score", 62.0))

        def _block(reason: str, detail: str) -> EntryVerdict:
            if record_block and self.risk is not None:
                self.risk.record_blocked_attempt(symbol, "llm_gate", detail)
            return EntryVerdict(
                False, reason, sentiment_score, day_score, sentiment, confidence,
            )

        if sentiment == "negative":
            return _block(
                "negative_sentiment",
                f"sentiment=negative (score={sentiment_score:.2f})",
            )
        if sentiment_score < min_sent:
            return _block(
                f"sentiment_score<{min_sent}",
                f"sentiment_score {sentiment_score:.2f} < {min_sent}",
            )
        if day_score < min_dt:
            return _block(
                f"day_trade_score<{min_dt}",
                f"day_trade_score {day_score:.1f} < {min_dt}",
            )

        return EntryVerdict(
            True, "ok", sentiment_score, day_score, sentiment, confidence,
        )

    def snapshot(
        self,
        symbol: str,
        price: float = 0.0,
        pct_chg: float = 0.0,
    ) -> GateSnapshot:
        """產生供 dashboard 顯示的快照 (不寫入 blocked_attempts)。"""
        llm_data = self.load_llm_cache(symbol)
        if not llm_data:
            return GateSnapshot(
                symbol=symbol,
                has_cache=False,
                allow_entry=not getattr(self.settings, "llm_gate_enabled", False),
                block_reason="no_llm_cache" if self.settings.llm_gate_enabled else "",
            )

        eval_price = price if price > 0 else 1.0
        verdict = self._evaluate_entry(
            symbol, eval_price, pct_chg, llm_data, record_block=False,
        )

        return GateSnapshot(
            symbol=symbol,
            has_cache=True,
            sentiment=verdict.sentiment,
            sentiment_score=verdict.sentiment_score,
            confidence=verdict.confidence,
            day_trade_score=verdict.day_trade_score,
            allow_entry=verdict.allowed,
            block_reason=verdict.reason if not verdict.allowed else "",
            fetched_at=str(llm_data.get("fetched_at", "")),
        )
