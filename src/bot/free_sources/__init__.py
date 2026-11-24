"""免費資料來源客戶端（第一階段：設定、HTTP、用量上限、探測；尚未接進排程）。

規格見 docs/free-data-sources-plan.md。
"""
from __future__ import annotations

from bot.free_sources.base import SourceError, redact
from bot.free_sources.quota import QuotaExceeded, QuotaTracker, default_quota

__all__ = ["QuotaExceeded", "QuotaTracker", "SourceError", "default_quota", "redact"]
