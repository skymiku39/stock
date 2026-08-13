"""chained_llm -- 容錯鏈 LLM 客戶端。

依序嘗試多個 LLM 後端，第一個成功回覆的勝出。
典型順序：Gemini 閘道 → Cursor 閘道 → Gemini SDK API。
"""

from __future__ import annotations

import logging
from typing import Any

from bot.utils import get_logger


class ChainedLlmClient:
    """依序嘗試多個 LlmClient 後端。

    - 跳過 enabled=False 的後端
    - 呼叫 generate_raw 後若回傳 None，嘗試下一個
    - 所有後端都失敗時回傳 (None, merged_meta)
    """

    def __init__(
        self,
        backends: list[Any],
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._backends = [b for b in backends if b is not None]
        self.logger = logger or get_logger("chained-llm")

    @property
    def enabled(self) -> bool:
        return any(b.enabled for b in self._backends)

    @property
    def model(self) -> str:
        for b in self._backends:
            if b.enabled:
                return b.model
        return "none"

    @property
    def active_backend_count(self) -> int:
        return sum(1 for b in self._backends if b.enabled)

    def generate_raw(
        self,
        prompt: str,
        *,
        max_output_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> tuple[str | None, dict[str, Any]]:
        """依序嘗試各後端直到成功。"""
        all_errors: list[str] = []
        last_meta: dict[str, Any] = {}

        for backend in self._backends:
            if not backend.enabled:
                continue

            backend_name = getattr(backend, "__class__", type(backend)).__name__
            try:
                text, meta = backend.generate_raw(
                    prompt,
                    max_output_tokens=max_output_tokens,
                    temperature=temperature,
                )
                if text is not None:
                    meta["chain_backend"] = backend_name
                    return text, meta
                error = meta.get("error", "empty reply")
                all_errors.append(f"{backend_name}: {error}")
                last_meta = meta
                self.logger.warning(
                    "ChainedLLM: %s 失敗 (%s)，嘗試下一個", backend_name, error
                )
            except Exception as exc:
                all_errors.append(f"{backend_name}: {exc}")
                self.logger.warning(
                    "ChainedLLM: %s 例外 (%s)，嘗試下一個", backend_name, exc
                )

        last_meta["chain_errors"] = all_errors
        last_meta["error"] = f"all backends failed: {'; '.join(all_errors)}"
        return None, last_meta
