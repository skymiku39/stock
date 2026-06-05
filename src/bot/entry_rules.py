"""進場漲幅範圍解析 — configurable 策略用。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Tuple

if TYPE_CHECKING:
    from bot.config import Settings

DEFAULT_ENTRY_MAX_PCT = 5.0


def resolve_entry_range(symbol: str, settings: Settings) -> Tuple[float, float]:
    """回傳 (min_pct, max_pct) 進場漲幅區間。"""
    targets = getattr(settings, "buy_entry_targets", {}) or {}
    if symbol in targets:
        lo, hi = targets[symbol]
        return float(lo), float(hi)

    min_pct = float(getattr(settings, "min_pct_chg_on_entry", 1.0))
    max_setting = float(settings.max_pct_chg_on_entry)
    max_pct = max_setting if max_setting > 0 else DEFAULT_ENTRY_MAX_PCT
    return min_pct, max_pct


def in_entry_range(symbol: str, pct_chg: float, settings: Settings) -> bool:
    """漲幅是否在允許的進場區間內 (不含邊界)。"""
    lo, hi = resolve_entry_range(symbol, settings)
    return lo < pct_chg < hi
