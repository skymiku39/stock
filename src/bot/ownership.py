"""Ownership tags for orders created by this bot."""

from __future__ import annotations

import re
from typing import Optional


BOT_OWNER_TAG = "AI"
BOT_BUY_FIELD = "AIBUY"
BOT_SELL_FIELD = "AISEL"

_CUSTOM_FIELD_RE = re.compile(r"[^A-Za-z0-9]+")
_SELL_REASON_FIELDS = {
    "target": "AITP",
    "takeprofit": "AITP",
    "tp": "AITP",
    "trail": "AITRL",
    "trailing": "AITRL",
    "sl": "AISL",
    "stop": "AISL",
    "stoploss": "AISL",
    "close": "AICLS",
}
_LEGACY_BOT_FIELDS = {
    "enter",
    "trail",
    "sl",
    "stop",
    "close",
}


def clean_order_field(value: str) -> str:
    """Return a Shioaji-safe custom field."""
    return _CUSTOM_FIELD_RE.sub("", value or "")[:6]


def bot_buy_field(_reason: str = "") -> str:
    """Custom field used on buy orders created by this bot."""
    return BOT_BUY_FIELD


def bot_sell_field(reason: str = "") -> str:
    """Custom field used on sell orders created by this bot."""
    normalized = clean_order_field(reason).lower()
    return _SELL_REASON_FIELDS.get(normalized, BOT_SELL_FIELD)


def is_bot_order_field(value: str, *, include_legacy: bool = False) -> bool:
    """Whether a custom field marks an order as bot-owned."""
    cleaned = clean_order_field(value).upper()
    if cleaned.startswith(BOT_OWNER_TAG):
        return True
    if include_legacy and cleaned.lower() in _LEGACY_BOT_FIELDS:
        return True
    return False


def infer_owner_tag(
    *,
    custom_field: str = "",
    explicit_owner: Optional[str] = None,
    include_legacy: bool = False,
) -> str:
    """Infer the owner tag stored on local portfolio records."""
    if explicit_owner:
        owner = clean_order_field(explicit_owner).upper()
        if owner:
            return owner
    if is_bot_order_field(custom_field, include_legacy=include_legacy):
        return BOT_OWNER_TAG
    return ""


def is_bot_owner(owner_tag: str) -> bool:
    return clean_order_field(owner_tag).upper() == BOT_OWNER_TAG


__all__ = [
    "BOT_BUY_FIELD",
    "BOT_OWNER_TAG",
    "BOT_SELL_FIELD",
    "bot_buy_field",
    "bot_sell_field",
    "clean_order_field",
    "infer_owner_tag",
    "is_bot_order_field",
    "is_bot_owner",
]
