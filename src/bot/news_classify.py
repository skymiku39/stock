"""classify_news -- 以 Gemini 將 MOPS／新聞主旨分類（可選 enrichment）。"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from bot.llm_analyzer import GeminiClient, extract_json, gemini_call
from bot.utils import get_logger


def classify_headline(
    *,
    ticker: str,
    subject: str,
    detail: str = "",
    client: Optional[GeminiClient] = None,
    logger: Optional[logging.Logger] = None,
) -> Optional[Dict[str, Any]]:
    """呼叫 ``classify_news`` prompt；失敗回 None。"""
    log = logger or get_logger("news-classify")
    if client is None or not getattr(client, "enabled", False):
        return None
    subject = (subject or "").strip()
    if not subject:
        return None
    raw, info = gemini_call(
        "classify_news",
        client=client,
        metadata={"ticker": ticker, "task": "classify_news"},
        ticker=ticker,
        subject=subject[:500],
        detail=(detail or "")[:3000],
    )
    if raw is None:
        return None
    data = extract_json(raw) or {}
    if not isinstance(data, dict):
        log.warning("[%s] classify_news 非 JSON 物件", ticker)
        return None
    data["_prompt"] = {
        "prompt_id": info.get("prompt_id", "classify_news"),
        "prompt_version": info.get("prompt_version", ""),
    }
    return data


def classify_material_batch(
    ticker: str,
    materials: List[Any],
    *,
    client: Optional[GeminiClient] = None,
    logger: Optional[logging.Logger] = None,
    limit: int = 3,
) -> List[Dict[str, Any]]:
    """對最近 N 筆重大訊息做分類；每筆含原始 subject + classification。"""
    out: List[Dict[str, Any]] = []
    if client is None or not getattr(client, "enabled", False):
        return out
    for m in list(materials)[: max(0, limit)]:
        subject = getattr(m, "subject", "") or ""
        detail = getattr(m, "detail", "") or ""
        classified = classify_headline(
            ticker=ticker,
            subject=subject,
            detail=detail,
            client=client,
            logger=logger,
        )
        if not classified:
            continue
        date_str = ""
        try:
            d = getattr(m, "date", None)
            date_str = d.isoformat() if hasattr(d, "isoformat") else str(d or "")
        except Exception:
            date_str = str(getattr(m, "date", "") or "")
        out.append({
            "date": date_str,
            "subject": subject,
            "classification": classified,
        })
    return out


__all__ = [
    "classify_headline",
    "classify_material_batch",
]
