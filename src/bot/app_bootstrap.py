"""app_bootstrap -- 應用程式啟動組裝 (單一組裝點, DIP)。

所有 CLI 與常駐程序應透過此模組取得事件匯流排，
避免各入口重複 wiring 邏輯。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from bot.events.bus import InMemoryEventBus
from bot.events.wiring import get_event_bus, wire_application_handlers


def bootstrap_event_bus(
    *,
    enable_jsonl: bool = True,
    jsonl_path: Optional[Path] = None,
    enable_chain_smile_screen: Optional[bool] = None,
    project_root: Optional[Path] = None,
) -> InMemoryEventBus:
    """初始化程序內事件匯流排與橫切 handler。"""
    root = (project_root or Path.cwd()).resolve()
    chain = enable_chain_smile_screen
    if chain is None:
        from bot.config import Settings

        chain = Settings(_env_file=str(root / ".env")).event_chain_smile_screen  # type: ignore[call-arg]
    bus = get_event_bus(project_root=root)
    return wire_application_handlers(
        bus,
        enable_jsonl=enable_jsonl,
        jsonl_path=jsonl_path,
        enable_chain_smile_screen=chain,
        project_root=root,
    )


def get_or_create_bus(*, project_root: Optional[Path] = None) -> InMemoryEventBus:
    """取得已 bootstrap 的匯流排（若尚未建立則自動 bootstrap）。"""
    root = (project_root or Path.cwd()).resolve()
    bus = get_event_bus(project_root=root)
    from bot.config import Settings

    settings = Settings(_env_file=str(root.resolve() / ".env"))  # type: ignore[call-arg]
    wire_application_handlers(
        bus,
        enable_chain_smile_screen=settings.event_chain_smile_screen,
        project_root=root,
    )
    return bus
