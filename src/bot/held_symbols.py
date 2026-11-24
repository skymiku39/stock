"""使用者實際持有（或曾經持有）的股票名單，併入每日收集。

兩個來源取聯集，只增不減：
- `config/held_symbols.json`：手動維護，`{"symbols": ["1514"]}`。
- `data/held_symbols_seen.json`：程式查到券商庫存時自動記下（目前是微笑盯盤每次讀庫存時）。
  賣掉後仍留在名單裡——曾經有紀錄的都繼續收集。要移除就手動刪這個檔裡的那一筆。

為什麼分兩個檔：`config/` 進版控，背景程式不該自己去改；自動記錄的放 `data/`。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable, List

from bot.runtime_state import atomic_json
from bot.utils import now_tw

_SYMBOL = re.compile(r"^\d{4,6}[A-Z]?$")


def manual_path(root: Path) -> Path:
    return Path(root) / "config" / "held_symbols.json"


def seen_path(root: Path) -> Path:
    return Path(root) / "data" / "held_symbols_seen.json"


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _clean(values: Iterable) -> List[str]:
    return [s for s in (str(v).strip() for v in values) if _SYMBOL.match(s)]


def load_seen(root: Path) -> dict:
    seen = _read(seen_path(root)).get("symbols", {})
    return {s: v for s, v in seen.items() if _SYMBOL.match(str(s))} if isinstance(seen, dict) else {}


def load_held_symbols(root: Path) -> List[str]:
    """手動名單與自動記錄的聯集；任何一個檔讀不到都不影響另一個。"""
    manual = _read(manual_path(root)).get("symbols", [])
    manual = _clean(manual) if isinstance(manual, list) else []
    return sorted(set(manual) | set(load_seen(root)))


def record_held(root: Path, symbols: Iterable, *, source: str = "broker") -> List[str]:
    """把查到的持股記下來（只新增）。回傳這次新記下的代號。"""
    seen = load_seen(root)
    fresh = [s for s in dict.fromkeys(_clean(symbols)) if s not in seen]
    if fresh:
        today = now_tw().date().isoformat()
        for symbol in fresh:
            seen[symbol] = {"first_seen": today, "source": source}
        atomic_json(seen_path(root), {"symbols": seen})
    return fresh
