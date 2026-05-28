"""llm_analyzer -- Google Gemini 法說會語意解析與邏輯反查。

V2 重構：
* 所有 prompt 從 PromptRegistry 載入 (prompts/*.yaml)
* 每筆 LLM 呼叫透過 LlmCallLogger 寫入 JSONL
* 提供統一的 gemini_call() helper，給 etf_holdings_fetcher / data_pipeline 共用
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from bot.llm_log import LlmCallLogger, get_call_logger
from bot.prompt_registry import PromptRegistry, PromptTemplate, get_registry
from bot.utils import get_logger

try:
    from google import genai  # type: ignore
    _HAS_GENAI = True
except Exception:
    try:
        import google.generativeai as genai  # type: ignore  # noqa: F401
        _HAS_GENAI = True
    except Exception:
        _HAS_GENAI = False


DEFAULT_MODEL = "gemini-2.5-flash"


# ----------------------------------------------------------------------
# 資料模型 (保留與 v1 相容)
# ----------------------------------------------------------------------


@dataclass
class PresentationAnalysis:
    ticker: str
    summary: str = ""
    sentiment: str = "neutral"
    sentiment_score: float = 0.0
    confidence: float = 0.5
    key_metrics: Dict[str, Any] = field(default_factory=dict)
    growth_drivers: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    capex_signal: str = ""
    margin_outlook: str = ""
    raw_response: str = ""
    model: str = DEFAULT_MODEL
    enabled: bool = True
    prompt_id: str = ""
    prompt_version: str = ""


@dataclass
class ChipsContext:
    foreign_net: float = 0.0
    investment_trust_net: float = 0.0
    dealer_net: float = 0.0
    margin_buy_change_pct: float = 0.0
    short_borrow_change_pct: float = 0.0
    block_trade_net: float = 0.0
    notes: str = ""


@dataclass
class LogicCheckResult:
    ticker: str
    verdict: str = "inconclusive"
    reasoning: str = ""
    confidence: float = 0.5
    suggestion: str = "hold"
    prompt_id: str = ""
    prompt_version: str = ""


# ----------------------------------------------------------------------
# Gemini Client (薄包裝)
# ----------------------------------------------------------------------


class GeminiClient:
    """Gemini API 包裝 — 自動偵測新版 (google-genai) 或舊版 (google-generativeai)。"""

    def __init__(
        self,
        api_key: str = "",
        model: str = DEFAULT_MODEL,
        logger: Optional[logging.Logger] = None,
    ):
        self.api_key = api_key
        self.model = model
        self.logger = logger or get_logger("llm")
        self._client: Optional[Any] = None
        self._mode: str = "none"
        self._enabled = bool(api_key) and _HAS_GENAI
        if self._enabled:
            try:
                if hasattr(genai, "Client"):
                    self._client = genai.Client(api_key=api_key)
                    self._mode = "new"
                else:
                    genai.configure(api_key=api_key)  # type: ignore[attr-defined]
                    self._client = genai.GenerativeModel(model)  # type: ignore[attr-defined]
                    self._mode = "legacy"
                self.logger.info("Gemini 已啟用 (model=%s, sdk=%s)", model, self._mode)
            except Exception:
                self.logger.exception("Gemini 初始化失敗")
                self._enabled = False
        elif not _HAS_GENAI:
            self.logger.info("未安裝 google-genai，LLM 停用")
        else:
            self.logger.info("未提供 GEMINI_API_KEY，LLM 停用")

    @property
    def enabled(self) -> bool:
        return self._enabled

    def generate_raw(
        self,
        prompt: str,
        *,
        max_output_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> tuple[Optional[str], Dict[str, Any]]:
        """回傳 (text, metadata)。metadata 包含 latency_ms / tokens 等。"""
        meta: Dict[str, Any] = {"sdk": self._mode}
        if not self._enabled or self._client is None:
            return None, meta

        t0 = time.time()
        # gemini-2.5 系列預設會 reasoning，會把 max_output_tokens 吃掉但不輸出
        # → 在這類「資料 → 結構化文字」任務上要求 thinking_budget=0
        is_25 = "2.5" in (self.model or "")
        try:
            if self._mode == "new":
                cfg: Dict[str, Any] = {
                    "temperature": temperature,
                    "top_p": 0.9,
                    "max_output_tokens": max_output_tokens,
                }
                if is_25:
                    cfg["thinking_config"] = {"thinking_budget": 0}
                resp = self._client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=cfg,
                )
            else:
                gcfg: Dict[str, Any] = {
                    "temperature": temperature,
                    "top_p": 0.9,
                    "max_output_tokens": max_output_tokens,
                }
                resp = self._client.generate_content(
                    prompt,
                    generation_config=gcfg,
                )
            text = getattr(resp, "text", "") or ""
            usage = getattr(resp, "usage_metadata", None)
            if usage is not None:
                meta["tokens_in"] = getattr(usage, "prompt_token_count", None)
                meta["tokens_out"] = getattr(usage, "candidates_token_count", None)
                meta["thoughts_tokens"] = getattr(usage, "thoughts_token_count", None)
            meta["latency_ms"] = int((time.time() - t0) * 1000)
            return text, meta
        except Exception as e:
            meta["latency_ms"] = int((time.time() - t0) * 1000)
            meta["error"] = str(e)
            self.logger.exception("Gemini 呼叫失敗")
            return None, meta


# ----------------------------------------------------------------------
# 統一 helper：渲染 prompt → 呼叫 LLM → 寫 log → 回傳結果
# ----------------------------------------------------------------------


def gemini_call(
    prompt_id: str,
    *,
    client: GeminiClient,
    registry: Optional[PromptRegistry] = None,
    call_logger: Optional[LlmCallLogger] = None,
    metadata: Optional[Dict[str, Any]] = None,
    **vars: Any,
) -> tuple[Optional[str], Dict[str, Any]]:
    """渲染指定 prompt + 呼叫 Gemini + 自動記錄 JSONL。

    Returns:
        (raw_text, info)，info 包含 prompt_id/prompt_version/latency_ms/tokens 等。
    """
    reg = registry or get_registry()
    log = call_logger or get_call_logger()
    prompt = reg.get(prompt_id)
    if prompt is None:
        raise KeyError(f"Prompt 不存在: {prompt_id}")

    rendered = prompt.render(**vars)
    info: Dict[str, Any] = {
        "prompt_id": prompt.id,
        "prompt_version": prompt.version,
        "model": client.model,
    }

    if not client.enabled:
        log.record(
            prompt_id=prompt.id,
            prompt_version=prompt.version,
            model=client.model,
            input_text=rendered,
            output_text="",
            success=False,
            error="LLM disabled (no API key or SDK)",
            metadata=metadata or {},
        )
        info["disabled"] = True
        return None, info

    text, meta = client.generate_raw(
        rendered,
        max_output_tokens=prompt.max_output_tokens,
        temperature=prompt.temperature,
    )
    info.update(meta)
    success = text is not None and not meta.get("error")
    log.record(
        prompt_id=prompt.id,
        prompt_version=prompt.version,
        model=client.model,
        input_text=rendered,
        output_text=text or "",
        latency_ms=meta.get("latency_ms", 0),
        tokens_in=meta.get("tokens_in"),
        tokens_out=meta.get("tokens_out"),
        success=success,
        error=str(meta.get("error", "")),
        metadata=metadata or {},
    )
    return text, info


# ----------------------------------------------------------------------
# JSON 抽取
# ----------------------------------------------------------------------


def extract_json(text: str) -> Optional[Any]:
    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except Exception:
        # 試著找最外層的 {...} 或 [...]
        for pattern in (r"\{.*\}", r"\[.*\]"):
            m = re.search(pattern, cleaned, re.S)
            if m:
                try:
                    return json.loads(m.group(0))
                except Exception:
                    continue
        return None


# ----------------------------------------------------------------------
# 高階 API: 分析法說會 & 言行反查
# ----------------------------------------------------------------------


def analyze_presentation(
    text: str,
    ticker: str = "",
    client: Optional[GeminiClient] = None,
    registry: Optional[PromptRegistry] = None,
    call_logger: Optional[LlmCallLogger] = None,
    logger: Optional[logging.Logger] = None,
) -> PresentationAnalysis:
    log = logger or get_logger("llm")
    if client is None or not client.enabled:
        return PresentationAnalysis(
            ticker=ticker, enabled=False,
            summary="(LLM 未啟用，請設定 GEMINI_API_KEY)",
            prompt_id="analyze_presentation",
        )

    raw, info = gemini_call(
        "analyze_presentation",
        client=client,
        registry=registry,
        call_logger=call_logger,
        metadata={"ticker": ticker, "task": "analyze_presentation"},
        ticker=ticker or "Unknown",
        text=text,
    )

    if raw is None:
        return PresentationAnalysis(
            ticker=ticker, enabled=True,
            summary="(Gemini 回應失敗)",
            prompt_id=info.get("prompt_id", ""),
            prompt_version=info.get("prompt_version", ""),
        )

    data = extract_json(raw) or {}
    if not isinstance(data, dict):
        data = {}
    analysis = PresentationAnalysis(
        ticker=ticker,
        summary=str(data.get("summary", "")),
        sentiment=str(data.get("sentiment", "neutral")),
        sentiment_score=float(data.get("sentiment_score", 0.0)),
        confidence=float(data.get("confidence", 0.5)),
        key_metrics=dict(data.get("key_metrics", {}) or {}),
        growth_drivers=list(data.get("growth_drivers", []) or []),
        risks=list(data.get("risks", []) or []),
        capex_signal=str(data.get("capex_signal", "")),
        margin_outlook=str(data.get("margin_outlook", "")),
        raw_response=raw,
        model=client.model,
        prompt_id=info.get("prompt_id", ""),
        prompt_version=info.get("prompt_version", ""),
    )
    log.info(
        "LLM 解析 %s -> sentiment=%s score=%.2f confidence=%.2f",
        ticker, analysis.sentiment, analysis.sentiment_score, analysis.confidence,
    )
    return analysis


def logic_check(
    analysis: PresentationAnalysis,
    chips: ChipsContext,
    client: Optional[GeminiClient] = None,
    registry: Optional[PromptRegistry] = None,
    call_logger: Optional[LlmCallLogger] = None,
    logger: Optional[logging.Logger] = None,
) -> LogicCheckResult:
    log = logger or get_logger("llm")
    if client is not None and client.enabled and analysis.enabled:
        raw, info = gemini_call(
            "logic_check",
            client=client,
            registry=registry,
            call_logger=call_logger,
            metadata={"ticker": analysis.ticker, "task": "logic_check"},
            ticker=analysis.ticker,
            sentiment=analysis.sentiment,
            sentiment_score=analysis.sentiment_score,
            summary=analysis.summary[:1000],
            drivers="; ".join(analysis.growth_drivers),
            risks="; ".join(analysis.risks),
            foreign=chips.foreign_net,
            trust=chips.investment_trust_net,
            dealer=chips.dealer_net,
            margin=chips.margin_buy_change_pct,
            short_borrow=chips.short_borrow_change_pct,
            block=chips.block_trade_net,
        )
        if raw:
            data = extract_json(raw) or {}
            if not isinstance(data, dict):
                data = {}
            return LogicCheckResult(
                ticker=analysis.ticker,
                verdict=str(data.get("verdict", "inconclusive")),
                reasoning=str(data.get("reasoning", "")),
                confidence=float(data.get("confidence", 0.5)),
                suggestion=str(data.get("suggestion", "hold")),
                prompt_id=info.get("prompt_id", ""),
                prompt_version=info.get("prompt_version", ""),
            )
    return _rule_based_logic_check(analysis, chips, log)


def _rule_based_logic_check(
    analysis: PresentationAnalysis,
    chips: ChipsContext,
    logger: logging.Logger,
) -> LogicCheckResult:
    sentiment_pos = analysis.sentiment_score > 0.2
    sentiment_neg = analysis.sentiment_score < -0.2

    smart_money_buying = (
        chips.foreign_net > 0 and chips.investment_trust_net > 0
        and chips.short_borrow_change_pct < 5
    )
    smart_money_selling = (
        chips.foreign_net < 0 or chips.short_borrow_change_pct > 20
        or chips.block_trade_net < -1000
    )

    verdict = "inconclusive"
    suggestion = "hold"
    reasoning = ""

    if sentiment_pos and smart_money_selling:
        verdict = "suspicious_distribution"
        suggestion = "avoid"
        reasoning = (
            f"法說會正向但外資/借券放空增加，疑似出貨 "
            f"(外資 {chips.foreign_net:+.0f}, 借券+{chips.short_borrow_change_pct:.1f}%)"
        )
    elif sentiment_neg and smart_money_buying:
        verdict = "suspicious_accumulation"
        suggestion = "buy"
        reasoning = "法說保守但外資/投信暗中吸籌，疑似低調吸貨"
    elif sentiment_pos and smart_money_buying:
        verdict = "consistent"
        suggestion = "buy"
        reasoning = "法說正向且籌碼同步流入，言行一致"
    elif sentiment_neg and smart_money_selling:
        verdict = "consistent"
        suggestion = "avoid"
        reasoning = "法說保守且資金同步流出，言行一致"
    else:
        verdict = "inconclusive"
        suggestion = "hold"
        reasoning = "情緒與籌碼方向不明顯，建議觀望"

    logger.info("規則式 logic check %s -> %s", analysis.ticker, verdict)
    return LogicCheckResult(
        ticker=analysis.ticker,
        verdict=verdict,
        reasoning=reasoning,
        confidence=0.4,
        suggestion=suggestion,
        prompt_id="(rule_based)",
        prompt_version="-",
    )


# ----------------------------------------------------------------------
# 序列化
# ----------------------------------------------------------------------


def analysis_to_dict(a: PresentationAnalysis) -> Dict[str, Any]:
    return dataclasses.asdict(a)


def logic_to_dict(r: LogicCheckResult) -> Dict[str, Any]:
    return dataclasses.asdict(r)


__all__ = [
    "ChipsContext",
    "GeminiClient",
    "LogicCheckResult",
    "PresentationAnalysis",
    "analyze_presentation",
    "analysis_to_dict",
    "extract_json",
    "gemini_call",
    "logic_check",
    "logic_to_dict",
]
