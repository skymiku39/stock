"""Recheck a cached model outage without spending inference quota."""
from __future__ import annotations

from pathlib import Path
import math
import time

from bot.codex_app_server import CodexAppServerClient
from bot.runtime_state import FileLock, atomic_json, read_json


def refresh_blocked_model(root: Path, settings, *, now: float | None = None) -> float:
    """Return the last confirmed recovery time; throttle outage probes to 5 min.

    A quota reset is an estimate, not a reason to ignore restored access after
    an account/plan change. This only reopens the gate: a real model call still
    has to succeed and its answer still has to pass evidence verification.
    """
    if settings.llm_provider != "codex_app_server":
        return 0.0
    path = Path(root) / "data" / "llm_availability.json"
    state = read_json(path)
    if not isinstance(state, dict) or not state:
        return 0.0
    key = f"{settings.codex_app_server_url}|{settings.codex_app_server_model}|{settings.codex_app_server_effort}"
    stamp = time.time() if now is None else now

    def timestamp(value):
        try:
            number = float(value or 0)
            return number if math.isfinite(number) else 0.0
        except (TypeError, ValueError):
            return 0.0

    def pending(value):
        same_config = value.get("configuration") == key
        if value.get("status") != "blocked":
            return False
        checked = timestamp(value.get("recovery_checked_at") or value.get("checked_at"))
        return not same_config or stamp - checked >= 300

    if not pending(state):
        return (timestamp(state.get("recovered_at"))
                if state.get("configuration") == key and state.get("status") != "blocked" else 0.0)
    lock = FileLock(Path(root) / "data" / ".llm-call.lock")
    if not lock.acquire():
        return 0.0
    try:
        state = read_json(path)
        if not isinstance(state, dict) or not pending(state):
            return 0.0
        try:
            client = CodexAppServerClient(
                url=settings.codex_app_server_url, model=settings.codex_app_server_model,
                effort=settings.codex_app_server_effort,
            )
            health = client.health(timeout=10)
        except Exception:
            health = {"available": False, "error_kind": "health_probe_failed"}
        state.update(configuration=key, recovery_checked_at=stamp, health=health)
        if health.get("available"):
            state.update(status="recovered", next_retry_at=0, recovered_at=stamp,
                         reason="", error_kind="")
        atomic_json(path, state)
        return stamp if health.get("available") else 0.0
    finally:
        lock.release()
