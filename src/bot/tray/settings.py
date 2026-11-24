"""系統匣程式自己的偏好設定（data/tray/settings.json）。讀不到或格式不對就用預設值。"""
from __future__ import annotations

import json
from pathlib import Path

DEFAULTS = {
    "minimize_to_tray": True,  # 按視窗的 X：True 收進系統匣，False 結束系統匣程式
}


def settings_path(root: Path) -> Path:
    return Path(root) / "data" / "tray" / "settings.json"


def load(root: Path) -> dict:
    settings = dict(DEFAULTS)
    try:
        saved = json.loads(settings_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return settings
    if isinstance(saved, dict):
        for key, default in DEFAULTS.items():
            if isinstance(saved.get(key), type(default)):
                settings[key] = saved[key]
    return settings


def save(root: Path, settings: dict) -> None:
    path = settings_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    known = {key: settings.get(key, default) for key, default in DEFAULTS.items()}
    path.write_text(json.dumps(known, ensure_ascii=False, indent=2), encoding="utf-8")
