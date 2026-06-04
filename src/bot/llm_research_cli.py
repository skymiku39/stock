"""Deprecated：請改用 ``stock-auto-research --llm-only`` 或 ``bot.auto_llm.llm_research_main``。"""

from __future__ import annotations

from bot.auto_llm import llm_research_main as main

__all__ = ["main"]
