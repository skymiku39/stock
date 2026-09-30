"""Windows CLI 輸出編碼初始化。"""

from __future__ import annotations

import sys


def configure_console() -> None:
    """盡力將 stdout/stderr 設為 UTF-8，不讓初始化阻斷 CLI。"""

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            continue


__all__ = ["configure_console"]
