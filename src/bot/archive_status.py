"""當沖交易模組封存狀態 — 集中管理封存旗標與啟動檢查。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from bot.config import Settings

ARCHIVE_DOC = "docs/archive/day-trading.md"


def day_trading_trade_blocked(settings: "Settings") -> bool:
    """trade 模式是否因封存而被阻擋。"""
    if settings.run_mode != "trade":
        return False
    if not getattr(settings, "day_trading_archived", True):
        return False
    return not getattr(settings, "day_trading_unfreeze", False)


def trade_block_message(settings: Optional["Settings"] = None) -> str:
    """回傳 trade 模式被阻擋時的說明文字。"""
    lines = [
        "當沖自動交易 (RUN_MODE=trade) 已封存，不再作為預設開發方向。",
        "請改用：",
        "  • RUN_MODE=watch  — 即時行情、記錄訊號、不下單",
        "  • RUN_MODE=report — 公開延遲資料、純分析報表",
        "  • stock-dashboard — 研究、K 線、法說、評分等工具",
        f"詳見 {ARCHIVE_DOC}",
        "若需暫時恢復舊版當沖實單，於 .env 設定 DAY_TRADING_UNFREEZE=true。",
    ]
    if settings is not None and settings.run_mode == "trade":
        lines.insert(
            1,
            f"（目前 RUN_MODE={settings.run_mode}）",
        )
    return "\n".join(lines)
