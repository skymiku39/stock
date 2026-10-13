"""受控分類與 LLM 題材輸出正規化。

官方產業別、投資題材、供應鏈角色與市場狀態是不同維度；本模組只處理
「輸入格式與分類邊界」，不把 LLM 自由文字直接當成可信資料。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple


TAXONOMY_VERSION = "2026-09-09"
VALID_MARKET_TONES = {"risk_on", "risk_off", "neutral", "mixed"}
ROLE_ALIASES = {
    "leader": "leader",
    "龍頭": "leader",
    "design": "design",
    "設計": "design",
    "equipment": "equipment",
    "設備": "equipment",
    "material": "material",
    "材料": "material",
    "supplier": "supplier",
    "供應商": "supplier",
    "downstream": "downstream",
    "下游": "downstream",
}
TICKER_RE = re.compile(r"^[0-9]{4,6}[A-Z]?$", re.IGNORECASE)


def normalize_ticker(value: Any) -> str:
    """正規化台股代號；不保證代號存在於交易所名冊。"""
    ticker = str(value or "").strip().upper()
    return ticker if TICKER_RE.fullmatch(ticker) else ""


def normalize_role(value: Any) -> Tuple[str, str]:
    raw = str(value or "").strip()
    if not raw:
        return "", ""
    return ROLE_ALIASES.get(raw.lower(), ROLE_ALIASES.get(raw, "other")), raw


def normalize_theme_payload(
    payload: Any,
    *,
    known_tickers: Optional[Iterable[str]] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """將 theme radar 結果轉成可下游使用的受控結構。

    ``known_tickers`` 有值時會拒絕不在標的名冊內的代號；沒有名冊時仍會
    允許格式正確的代號，避免在離線或測試流程誤刪合法候選。
    """
    warnings: List[str] = []
    obj = payload if isinstance(payload, Mapping) else {}
    tone = str(obj.get("market_tone") or "neutral").strip().lower()
    if tone not in VALID_MARKET_TONES:
        warnings.append(f"invalid market_tone={tone!r}; use neutral")
        tone = "neutral"

    known = {normalize_ticker(x) for x in (known_tickers or [])}
    themes: List[Dict[str, Any]] = []
    raw_themes = obj.get("themes") or []
    if not isinstance(raw_themes, list):
        warnings.append("themes is not a list")
        raw_themes = []

    for index, raw_theme in enumerate(raw_themes):
        if not isinstance(raw_theme, Mapping):
            warnings.append(f"themes[{index}] is not an object")
            continue
        name = str(raw_theme.get("theme") or "").strip()
        if not name:
            warnings.append(f"themes[{index}] missing theme")
            continue
        try:
            heat = int(float(raw_theme.get("heat", 0) or 0))
        except (TypeError, ValueError):
            heat = 0
            warnings.append(f"themes[{index}] invalid heat")
        if not 0 <= heat <= 5:
            warnings.append(f"themes[{index}] heat out of range")
            heat = max(0, min(5, heat))

        candidates: List[Dict[str, Any]] = []
        raw_candidates = raw_theme.get("candidate_tickers") or []
        if not isinstance(raw_candidates, list):
            warnings.append(f"themes[{index}] candidate_tickers is not a list")
            raw_candidates = []
        seen: set[str] = set()
        for cindex, candidate in enumerate(raw_candidates):
            if not isinstance(candidate, Mapping):
                warnings.append(f"themes[{index}].candidate_tickers[{cindex}] is not an object")
                continue
            ticker = normalize_ticker(candidate.get("ticker"))
            if not ticker:
                warnings.append(f"themes[{index}].candidate_tickers[{cindex}] invalid ticker")
                continue
            if known and ticker not in known:
                warnings.append(f"unknown ticker {ticker} removed from theme {name}")
                continue
            if ticker in seen:
                continue
            seen.add(ticker)
            role, role_raw = normalize_role(candidate.get("role"))
            item: Dict[str, Any] = {
                "ticker": ticker,
                "name": str(candidate.get("name") or "").strip(),
                "role": role,
            }
            if role_raw and role_raw != role:
                item["role_raw"] = role_raw
            # 只保留常用、可稽核的文字欄位；不把任意 LLM 物件往下游傳。
            for key in ("reason", "entry_logic", "catalyst"):
                value = str(candidate.get(key) or "").strip()
                if value:
                    item[key] = value[:500]
            candidates.append(item)

        themes.append({
            "theme": name[:80],
            "heat": heat,
            "role": str(raw_theme.get("role") or "").strip()[:40],
            "candidate_tickers": candidates,
            "taxonomy_version": TAXONOMY_VERSION,
        })

    return {
        "market_tone": tone,
        "overall_brief": str(obj.get("overall_brief") or "")[:2000],
        "themes": themes,
        "taxonomy_version": TAXONOMY_VERSION,
    }, warnings


__all__ = [
    "ROLE_ALIASES",
    "TAXONOMY_VERSION",
    "VALID_MARKET_TONES",
    "normalize_role",
    "normalize_theme_payload",
    "normalize_ticker",
]
