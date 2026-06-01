"""macro_update -- 抓取美股 / 加權指 / ADR 溢價並（可選）產 LLM 簡報的 CLI。

用法:
    uv run stock-macro-update                  # 僅抓資料 (yfinance)
    uv run stock-macro-update --brief          # 抓 + 呼叫 Gemini 產出跨市場簡報
    uv run stock-macro-update --no-cache       # 強制重抓 (預設仍會用日快取)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from bot.cloud_file_cache import mirror_file_to_cloud
from bot.config import Settings
from bot.market_macro import (
    fetch_macro_snapshot,
    load_supply_chain,
    macro_to_dict,
)
from bot.utils import get_logger


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stock-macro-update",
        description="抓美股 / 加權指 / ADR 溢價 (可選 LLM 跨市場簡報)",
    )
    parser.add_argument("--no-cache", action="store_true", help="強制重抓，不使用日快取")
    parser.add_argument("--brief", action="store_true", help="呼叫 Gemini 產出跨市場簡報")
    args = parser.parse_args(argv)

    logger = get_logger("macro-update")
    settings = Settings()
    project_root = Path.cwd()

    logger.info("=== 抓 macro 開始 ===")
    snap = fetch_macro_snapshot(
        root=project_root,
        force_refresh=args.no_cache,
        use_cache=not args.no_cache,
        logger=logger,
    )
    logger.info(
        "完成: %d 指數, %d 個股, %d ADR 溢價, USDTWD=%.3f",
        len(snap.indices), len(snap.stocks), len(snap.adr_premiums), snap.usdtwd,
    )

    if not args.brief:
        return 0

    api_key = settings.gemini_api_key
    if not api_key:
        logger.warning("GEMINI_API_KEY 未設定，無法產出簡報")
        return 1

    from bot.llm_analyzer import GeminiClient, gemini_call

    client = GeminiClient(api_key=api_key, model=settings.gemini_model, logger=logger)
    if not client.enabled:
        logger.warning("LLM 啟用失敗")
        return 1

    data = macro_to_dict(snap)
    sc = load_supply_chain(project_root)
    raw, info = gemini_call(
        "us_market_brief",
        client=client,
        metadata={"task": "us_market_brief", "source": "cli"},
        asof_date=data.get("asof_date", ""),
        indices_json=json.dumps(data.get("indices", {}), ensure_ascii=False, indent=2),
        stocks_json=json.dumps(data.get("stocks", {}), ensure_ascii=False, indent=2),
        adr_premiums_json=json.dumps(data.get("adr_premiums", []), ensure_ascii=False, indent=2),
        supply_chain_json=json.dumps(sc.get("us_stocks", {}), ensure_ascii=False, indent=2),
    )
    if not raw:
        logger.warning("LLM 無回應")
        return 1

    out = project_root / "data" / "macro" / f"us_brief_{data.get('asof_date','today')}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(raw, encoding="utf-8")
    mirror_file_to_cloud(out, root=project_root)
    logger.info("簡報已寫入 %s (prompt=%s v%s)", out, info.get("prompt_id"), info.get("prompt_version"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
