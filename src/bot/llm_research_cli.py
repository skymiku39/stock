"""llm_research_cli -- 全自動 LLM 個股研究 CLI 入口。

用法
====
```
uv run stock-llm-research                    # 跑 watchlist 全部
uv run stock-llm-research 2330               # 只跑 2330
uv run stock-llm-research 2330,2317,3017     # 多檔
uv run stock-llm-research --upcoming         # 自動加入「未來 14 天有法說會」的個股
uv run stock-llm-research --refresh          # 略過快取，強制重跑
uv run stock-llm-research --no-web           # 略過網路搜尋
```

步驟
====
1. 更新法說會行事曆 (預設 -1 / 0 / +1 / +2 月)
2. 對每檔 ticker：
   * 抓 MOPS 重訊 + 鉅亨新聞 + 網頁搜尋 + 既有法說文件
   * 整成 prompt 餵 Gemini ``research_ticker`` (退回 ``analyze_presentation``)
   * 若拿得到籌碼面，順手跑言行反查
3. 結果存到 ``data/auto_llm/<ticker>.json``，並 append 到 ``data/auto_llm/research_log.jsonl``
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

from bot.auto_llm import auto_research_ticker
from bot.cloud_file_cache import mirror_file_to_cloud
from bot.config import Settings
from bot.conference_calendar import update_calendar, upcoming_tickers
from bot.utils import get_logger, mk_folder, now_tw
from bot import watchlist as wl


def _parse_tickers(args_tickers: List[str]) -> List[str]:
    out: List[str] = []
    for raw in args_tickers:
        for t in str(raw).split(","):
            t = t.strip()
            if t and t not in out:
                out.append(t)
    return out


def _load_watchlist_tickers(root: Path) -> List[str]:
    try:
        items = wl.load(root).items
    except Exception:
        return []
    return [i.ticker for i in items if i.ticker]


def _name_hint_for(ticker: str, root: Path) -> str:
    try:
        for it in wl.load(root).items:
            if it.ticker == ticker:
                return it.name or ""
    except Exception:
        pass
    try:
        from bot.stock_db import get_db
        info = get_db().get_stock_info(ticker)
        if info and info.name:
            return info.name
    except Exception:
        pass
    return ""


def _append_log(root: Path, entry: Dict[str, object]) -> None:
    log_path = root / "data" / "auto_llm" / "research_log.jsonl"
    mk_folder(str(log_path.parent))
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        mirror_file_to_cloud(log_path, root=root)
    except Exception:
        pass


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stock-llm-research",
        description="全自動 LLM 個股研究 (行事曆 + 上網搜尋 + LLM 分析 + 言行反查)",
    )
    parser.add_argument(
        "tickers", nargs="*",
        help="個股代號 (可逗號分隔；無參數則跑 watchlist)",
    )
    parser.add_argument(
        "--upcoming", action="store_true",
        help="另外納入未來 14 天有法說會的個股",
    )
    parser.add_argument(
        "--upcoming-days", type=int, default=14,
        help="upcoming 視窗天數 (預設 14)",
    )
    parser.add_argument(
        "--no-calendar", action="store_true",
        help="跳過行事曆自動更新 (用快取)",
    )
    parser.add_argument(
        "--no-web", action="store_true",
        help="跳過網頁搜尋",
    )
    parser.add_argument(
        "--refresh", action="store_true",
        help="略過 12h 快取，強制重跑 LLM",
    )
    parser.add_argument(
        "--days", type=int, default=5,
        help="籌碼面回顧天數 (預設 5)",
    )
    args = parser.parse_args(argv)

    logger = get_logger("llm-research")
    project_root = Path.cwd()
    settings = Settings()

    if not settings.gemini_api_key:
        logger.error(
            "未設定 GEMINI_API_KEY；無法跑自動研究。請到 .env 或 dashboard「組態設定」填入。"
        )
        return 2

    if not args.no_calendar:
        logger.info("=== 更新 MOPS 法說會行事曆 ===")
        try:
            summary = update_calendar(root=project_root, logger=logger)
            logger.info("行事曆更新完成: %s", summary)
        except Exception:
            logger.exception("行事曆更新失敗 (繼續)")

    tickers: List[str] = _parse_tickers(args.tickers)
    if not tickers:
        tickers = _load_watchlist_tickers(project_root)
        if tickers:
            logger.info("未指定 tickers，使用 watchlist 共 %d 檔", len(tickers))
    if args.upcoming:
        upcoming = upcoming_tickers(days=args.upcoming_days, root=project_root)
        new_add = [t for t in upcoming if t not in tickers]
        tickers.extend(new_add)
        logger.info("加入未來 %d 天有法說會的 %d 檔: %s",
                    args.upcoming_days, len(new_add), new_add[:10])

    if not tickers:
        logger.warning("沒有任何 ticker 可以分析。請傳參數或建立 watchlist。")
        return 1

    logger.info("=== 開始自動研究 %d 檔 ===", len(tickers))
    okay = 0
    failed = 0
    for i, ticker in enumerate(tickers, 1):
        name = _name_hint_for(ticker, project_root)
        logger.info("[%d/%d] 研究 %s %s ...", i, len(tickers), ticker, name)
        try:
            result = auto_research_ticker(
                ticker,
                root=project_root,
                name_hint=name,
                days=args.days,
                force_refresh=args.refresh,
                refresh_calendar=False,
                logger=logger,
            )
        except Exception:
            logger.exception("[%s] 自動研究例外", ticker)
            failed += 1
            _append_log(project_root, {
                "ts": now_tw().isoformat(timespec="seconds"),
                "ticker": ticker, "status": "exception",
            })
            continue
        if result is None:
            failed += 1
            _append_log(project_root, {
                "ts": now_tw().isoformat(timespec="seconds"),
                "ticker": ticker, "status": "no_result",
            })
            continue
        okay += 1
        _append_log(project_root, {
            "ts": now_tw().isoformat(timespec="seconds"),
            "ticker": ticker,
            "status": "ok",
            "sentiment": result.get("sentiment"),
            "sentiment_score": result.get("sentiment_score"),
            "confidence": result.get("confidence"),
            "catalyst_outlook": result.get("catalyst_outlook", "")[:80],
            "logic_verdict": (result.get("logic_check") or {}).get("verdict"),
        })

    logger.info("=== 完成：成功 %d 檔 / 失敗 %d 檔 ===", okay, failed)
    return 0 if okay > 0 else 2


if __name__ == "__main__":
    sys.exit(main())
