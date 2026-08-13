"""test_llm_clients -- GeminiGatewayClient / CursorGatewayClient / ChainedLlmClient 測試。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from bot.chained_llm import ChainedLlmClient
from bot.cursor_llm import CursorGatewayClient
from bot.gemini_gateway import GeminiGatewayClient
from bot.protocols.llm import LlmClient

# ---------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------


class TestProtocolConformance:
    def test_gemini_gateway_is_llm_client(self):
        client = GeminiGatewayClient(base_url="", check_health=False)
        assert isinstance(client, LlmClient)

    def test_cursor_gateway_is_llm_client(self):
        client = CursorGatewayClient(base_url="", check_health=False)
        assert isinstance(client, LlmClient)

    def test_chained_is_llm_client(self):
        client = ChainedLlmClient(backends=[])
        assert isinstance(client, LlmClient)


# ---------------------------------------------------------------
# GeminiGatewayClient
# ---------------------------------------------------------------


class TestGeminiGatewayClient:
    def test_disabled_when_no_url(self):
        client = GeminiGatewayClient(base_url="", check_health=False)
        assert not client.enabled
        text, meta = client.generate_raw("hello")
        assert text is None
        assert "error" in meta

    def test_health_check_failure(self):
        with patch("bot.gemini_gateway.requests.get") as mock_get:
            import requests as req
            mock_get.side_effect = req.ConnectionError("refused")
            client = GeminiGatewayClient(
                base_url="http://127.0.0.1:9999",
                check_health=True,
            )
            assert not client.enabled

    def test_successful_generate(self):
        client = GeminiGatewayClient(
            base_url="http://127.0.0.1:8816",
            check_health=False,
        )
        client._enabled = True
        client._health_checked = True

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"reply": "Hello!", "model": "gemini-auto"}

        with patch("bot.gemini_gateway.requests.post", return_value=mock_resp):
            text, meta = client.generate_raw("test prompt")

        assert text == "Hello!"
        assert meta["gateway_model"] == "gemini-auto"
        assert meta["sdk"] == "gemini_gateway"

    def test_http_error(self):
        client = GeminiGatewayClient(
            base_url="http://127.0.0.1:8816",
            check_health=False,
        )
        client._enabled = True
        client._health_checked = True

        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_resp.text = "Service Unavailable"

        with patch("bot.gemini_gateway.requests.post", return_value=mock_resp):
            text, meta = client.generate_raw("test")

        assert text is None
        assert "503" in meta["error"]

    def test_model_property(self):
        client = GeminiGatewayClient(
            base_url="http://x", model_label="test-model", check_health=False
        )
        assert client.model == "test-model"


# ---------------------------------------------------------------
# CursorGatewayClient
# ---------------------------------------------------------------


class TestCursorGatewayClient:
    def test_disabled_when_no_url(self):
        client = CursorGatewayClient(base_url="", check_health=False)
        assert not client.enabled

    def test_successful_generate(self):
        client = CursorGatewayClient(
            base_url="http://127.0.0.1:8815",
            check_health=False,
        )
        client._enabled = True
        client._health_checked = True

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"reply": "Cursor OK", "model": "gpt-4o"}

        with patch("bot.cursor_llm.requests.post", return_value=mock_resp):
            text, meta = client.generate_raw("test")

        assert text == "Cursor OK"
        assert meta["sdk"] == "cursor_gateway"


# ---------------------------------------------------------------
# ChainedLlmClient
# ---------------------------------------------------------------


class TestChainedLlmClient:
    def _make_backend(self, *, enabled=True, reply=None, error=None):
        b = MagicMock()
        b.enabled = enabled
        b.model = "mock"
        b.__class__.__name__ = "MockBackend"
        if reply:
            b.generate_raw.return_value = (reply, {"latency_ms": 100})
        elif error:
            b.generate_raw.return_value = (None, {"error": error})
        else:
            b.generate_raw.return_value = (None, {"error": "no reply"})
        return b

    def test_first_enabled_wins(self):
        b1 = self._make_backend(reply="First!")
        b2 = self._make_backend(reply="Second!")
        chain = ChainedLlmClient(backends=[b1, b2])

        text, meta = chain.generate_raw("hello")
        assert text == "First!"
        b2.generate_raw.assert_not_called()

    def test_fallthrough_on_failure(self):
        b1 = self._make_backend(error="timeout")
        b2 = self._make_backend(reply="Fallback!")
        chain = ChainedLlmClient(backends=[b1, b2])

        text, meta = chain.generate_raw("hello")
        assert text == "Fallback!"
        assert "chain_backend" in meta

    def test_skip_disabled(self):
        b1 = self._make_backend(enabled=False)
        b2 = self._make_backend(reply="Active!")
        chain = ChainedLlmClient(backends=[b1, b2])

        text, meta = chain.generate_raw("hello")
        assert text == "Active!"
        b1.generate_raw.assert_not_called()

    def test_all_fail(self):
        b1 = self._make_backend(error="err1")
        b2 = self._make_backend(error="err2")
        chain = ChainedLlmClient(backends=[b1, b2])

        text, meta = chain.generate_raw("hello")
        assert text is None
        assert "all backends failed" in meta["error"]
        assert len(meta["chain_errors"]) == 2

    def test_empty_backends(self):
        chain = ChainedLlmClient(backends=[])
        assert not chain.enabled
        assert chain.model == "none"

    def test_active_backend_count(self):
        b1 = self._make_backend(enabled=True)
        b2 = self._make_backend(enabled=False)
        b3 = self._make_backend(enabled=True)
        chain = ChainedLlmClient(backends=[b1, b2, b3])
        assert chain.active_backend_count == 2


# ---------------------------------------------------------------
# create_llm_client factory
# ---------------------------------------------------------------


class TestCreateLlmClient:
    def test_chain_provider(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "chain")
        monkeypatch.setenv("GEMINI_GATEWAY_BASE_URL", "http://127.0.0.1:8816")
        monkeypatch.setenv("CURSOR_LLM_BASE_URL", "http://127.0.0.1:8815")
        monkeypatch.setenv("GEMINI_API_KEY", "")

        from bot.llm_analyzer import create_llm_client
        client = create_llm_client()
        assert isinstance(client, ChainedLlmClient)

    def test_gemini_gateway_provider(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "gemini_gateway")
        monkeypatch.setenv("GEMINI_GATEWAY_BASE_URL", "http://127.0.0.1:8816")

        from bot.llm_analyzer import create_llm_client
        client = create_llm_client()
        assert isinstance(client, GeminiGatewayClient)

    def test_cursor_provider(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "cursor")
        monkeypatch.setenv("CURSOR_LLM_BASE_URL", "http://127.0.0.1:8815")

        from bot.llm_analyzer import create_llm_client
        client = create_llm_client()
        assert isinstance(client, CursorGatewayClient)

    def test_llm_ready_chain(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "chain")
        monkeypatch.setenv("GEMINI_GATEWAY_BASE_URL", "http://127.0.0.1:8816")
        monkeypatch.setenv("CURSOR_LLM_BASE_URL", "")
        monkeypatch.setenv("GEMINI_API_KEY", "")

        from bot.llm_analyzer import llm_ready
        assert llm_ready() is True

    def test_llm_ready_no_backends(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "chain")
        monkeypatch.setenv("GEMINI_GATEWAY_BASE_URL", "")
        monkeypatch.setenv("CURSOR_LLM_BASE_URL", "")
        monkeypatch.setenv("GEMINI_API_KEY", "")

        from bot.llm_analyzer import llm_ready
        assert llm_ready() is False
