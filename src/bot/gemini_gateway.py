"""gemini_gateway -- 透過本機 Gemini 瀏覽器閘道呼叫 Google AI。

閘道專案：D:\\skymiku\\蹭google的geminiAI
啟動方式：在該專案內 uv run gemini-gateway
端點：GET /health, POST /v1/chat
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from bot.utils import get_logger

DEFAULT_BASE_URL = "http://127.0.0.1:8816"
DEFAULT_TIMEOUT = 180
DEFAULT_MODEL_LABEL = "gemini-auto"


class GeminiGatewayClient:
    """HTTP 客戶端：POST /v1/chat → 本機 Gemini 瀏覽器閘道。"""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        model_label: str = DEFAULT_MODEL_LABEL,
        logger: logging.Logger | None = None,
        check_health: bool = True,
    ) -> None:
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/") + "/"
        self.timeout = float(timeout) if timeout else DEFAULT_TIMEOUT
        self._model = model_label or DEFAULT_MODEL_LABEL
        self.logger = logger or get_logger("gemini-gateway")
        self._enabled = bool(base_url)
        self._health_checked = not check_health
        if check_health and self._enabled:
            self._refresh_health()

    @property
    def model(self) -> str:
        return self._model

    @property
    def enabled(self) -> bool:
        if self._enabled and not self._health_checked:
            self._refresh_health()
        return self._enabled

    def _url(self, path: str) -> str:
        from urllib.parse import urljoin
        return urljoin(self.base_url, path.lstrip("/"))

    def _refresh_health(self) -> bool:
        self._health_checked = True
        try:
            resp = requests.get(self._url("health"), timeout=min(10.0, self.timeout))
            if resp.status_code != 200:
                self.logger.warning(
                    "Gemini 閘道 /health HTTP %s：%s", resp.status_code, resp.text[:200]
                )
                self._enabled = False
                return False
            data = resp.json()
            ready = bool(data.get("ready"))
            if not ready:
                self.logger.warning(
                    "Gemini 閘道未就緒 (ready=false, browser_ready=%s)",
                    data.get("browser_ready"),
                )
            self._enabled = ready
            if ready:
                self.logger.info(
                    "Gemini 閘道已就緒 (model=%s, url=%s)",
                    self._model,
                    self.base_url,
                )
            return ready
        except requests.RequestException as exc:
            self.logger.warning("無法連線 Gemini 閘道 %s：%s", self.base_url, exc)
            self._enabled = False
            return False

    def generate_raw(
        self,
        prompt: str,
        *,
        max_output_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> tuple[str | None, dict[str, Any]]:
        """送 prompt 至 Gemini 瀏覽器閘道；回傳 (text, metadata)。"""
        meta: dict[str, Any] = {
            "sdk": "gemini_gateway",
            "max_output_tokens": max_output_tokens,
            "temperature": temperature,
            "base_url": self.base_url,
        }
        if not self.enabled:
            meta["error"] = "Gemini gateway disabled or not ready"
            return None, meta

        t0 = time.time()
        try:
            resp = requests.post(
                self._url("v1/chat"),
                json={"message": prompt},
                timeout=self.timeout,
                headers={"Content-Type": "application/json"},
            )
            meta["latency_ms"] = int((time.time() - t0) * 1000)
            meta["http_status"] = resp.status_code
            if resp.status_code != 200:
                meta["error"] = f"HTTP {resp.status_code}: {resp.text[:300]}"
                self.logger.error("Gemini 閘道呼叫失敗：%s", meta["error"])
                return None, meta
            data = resp.json()
            reply = data.get("reply")
            if isinstance(reply, str) and reply.strip():
                if data.get("model"):
                    meta["gateway_model"] = data["model"]
                return reply, meta
            meta["error"] = "empty reply from gateway"
            return None, meta
        except requests.RequestException as exc:
            meta["latency_ms"] = int((time.time() - t0) * 1000)
            meta["error"] = str(exc)
            self.logger.exception("Gemini 閘道請求例外")
            return None, meta
