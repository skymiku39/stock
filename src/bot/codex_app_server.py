"""Small synchronous JSON-RPC client for the local Codex App Server WebSocket."""

from __future__ import annotations

import ipaddress
import json
import time
from urllib.parse import urlparse
from typing import Any, Dict, Optional

from websockets.sync.client import connect


class CodexAppServerError(RuntimeError):
    """Raised when the local Codex App Server cannot complete a turn."""


class CodexAppServerClient:
    def __init__(
        self,
        *,
        url: str,
        model: str = "gpt-6-sol",
        effort: str = "max",
        cwd: Optional[str] = None,
        timeout: float = 300,
    ) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "ws" or not parsed.hostname or parsed.port is None:
            raise ValueError("Codex App Server URL must be ws://loopback:PORT")
        try:
            is_loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            is_loopback = parsed.hostname.lower() == "localhost"
        if not is_loopback:
            raise ValueError("Codex App Server endpoint must use a loopback address")
        self.url = url
        self.model = model
        self.effort = effort
        self.cwd = cwd
        self.timeout = timeout
        self._next_id = 0

    def generate(self, prompt: str) -> tuple[str, Dict[str, Any]]:
        """Run one isolated turn and return its final agent message."""
        started = time.monotonic()
        metadata: Dict[str, Any] = {"sdk": "codex-app-server", "model": self.model}
        try:
            with connect(
                self.url,
                open_timeout=10,
                close_timeout=2,
                max_size=16 * 1024 * 1024,
            ) as socket:
                self._rpc(socket, "initialize", {
                    "clientInfo": {
                        "name": "stock-bot",
                        "title": "Stock Bot",
                        "version": "0.1.0",
                    },
                    "capabilities": {"experimentalApi": True},
                })
                socket.send(json.dumps({"method": "initialized", "params": {}}))
                thread_result = self._rpc(socket, "thread/start", {
                    "model": self.model,
                    "ephemeral": True,
                })
                thread_id = (thread_result.get("thread") or {}).get("id")
                if not thread_id:
                    raise CodexAppServerError("Codex App Server returned no thread id")

                self._next_id += 1
                turn_id = self._next_id
                params: Dict[str, Any] = {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": prompt}],
                    "approvalPolicy": "never",
                    "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
                    "effort": self.effort,
                }
                if self.cwd:
                    params["cwd"] = self.cwd
                socket.send(json.dumps({"id": turn_id, "method": "turn/start", "params": params}))
                self._await_response(socket, turn_id, time.monotonic() + 30)

                deadline = time.monotonic() + self.timeout
                chunks: list[str] = []
                while time.monotonic() < deadline:
                    message = self._receive(socket, deadline)
                    if message.get("error"):
                        raise CodexAppServerError(self._error_text(message["error"]))
                    method = message.get("method", "")
                    event_params = message.get("params") or {}
                    if method == "item/agentMessage/delta":
                        delta = event_params.get("delta")
                        if isinstance(delta, str):
                            chunks.append(delta)
                    elif method == "turn/completed":
                        turn = event_params.get("turn") or {}
                        metadata["turn_status"] = turn.get("status", "completed")
                        if turn.get("status") == "failed":
                            error = turn.get("error") or {}
                            raise CodexAppServerError(str(error.get("message") or error or "turn failed"))
                        if not chunks:
                            chunks.extend(self._message_texts(turn.get("items") or []))
                        text = "".join(chunks)
                        metadata["latency_ms"] = int((time.monotonic() - started) * 1000)
                        return text, metadata
                raise TimeoutError("Codex App Server turn timed out")
        except Exception as exc:
            metadata["latency_ms"] = int((time.monotonic() - started) * 1000)
            metadata["error"] = str(exc)
            return "", metadata

    def _rpc(self, socket: Any, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        socket.send(json.dumps({"id": request_id, "method": method, "params": params}))
        return self._await_response(socket, request_id, time.monotonic() + 30)

    def _await_response(self, socket: Any, request_id: int, deadline: float) -> Dict[str, Any]:
        while time.monotonic() < deadline:
            message = self._receive(socket, deadline)
            if message.get("id") != request_id:
                continue
            if message.get("error"):
                raise CodexAppServerError(self._error_text(message["error"]))
            result = message.get("result") or {}
            return result if isinstance(result, dict) else {"value": result}
        raise TimeoutError("Codex App Server JSON-RPC request timed out")

    @staticmethod
    def _receive(socket: Any, deadline: float) -> Dict[str, Any]:
        remaining = max(0.1, deadline - time.monotonic())
        try:
            raw = socket.recv(timeout=remaining)
        except TimeoutError as exc:
            raise TimeoutError("Codex App Server response timed out") from exc
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise CodexAppServerError("Codex App Server returned an invalid JSON-RPC message")
        return value

    @staticmethod
    def _message_texts(items: list[Any]) -> list[str]:
        texts: list[str] = []
        for item in items:
            if not isinstance(item, dict) or item.get("type") not in {"agentMessage", "agent_message"}:
                continue
            content = item.get("content")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                texts.extend(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and isinstance(part.get("text"), str)
                )
        return texts

    @staticmethod
    def _error_text(error: Any) -> str:
        if isinstance(error, dict):
            return str(error.get("message") or error)
        return str(error)
