"""protocols/llm -- LLM 客戶端依賴反轉介面。

所有管線/分析模組依賴此 Protocol，不直接依賴 Gemini SDK 或閘道實作。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LlmClient(Protocol):
    """統一 LLM 客戶端介面。"""

    @property
    def enabled(self) -> bool:
        """是否可用（健康且就緒）。"""
        ...

    @property
    def model(self) -> str:
        """目前使用的模型標籤。"""
        ...

    def generate_raw(
        self,
        prompt: str,
        *,
        max_output_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> tuple[str | None, dict[str, Any]]:
        """送出 prompt，回傳 (text | None, metadata)。"""
        ...
