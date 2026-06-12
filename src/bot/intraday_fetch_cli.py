"""stock-intraday-fetch -- 從 Shioaji 補齊分 K / Tick 歷史至 SQLite。

用法
====
    uv run stock-intraday-fetch --symbol 2330 --start 2024-01-02 --end 2024-01-05
    uv run stock-intraday-fetch --symbol 2330 --days 5
    uv run stock-intraday-fetch --symbol 2330,0050 --days 10 --type 1m
    uv run stock-intraday-fetch --symbol 2330 --start 2024-01-02 --type tick

需求
====
* ``.env`` 需有 ``API_KEY`` / ``SECRET_KEY`` (歷史行情不需 CA)
* Shioaji 歷史資料有每日流量限制，建議分批、勿一次抓過長區間
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path
from typing import List, Optional

from bot.config import Settings
from bot.intraday_history import (
    IntervalKind,
    fetch_and_store_intraday,
    iter_trading_days,
)
from bot.stock_db import StockDB, default_db_path
from bot.utils import get_logger, now_tw


def _parse_date(text: str) -> dt.date:
    return dt.date.fromisoformat(text.strip())


def _resolve_range(
    *,
    start: Optional[str],
    end: Optional[str],
    days: Optional[int],
) -> tuple[dt.date, dt.date]:
    today = now_tw().date()
    if days is not None and days > 0:
        trading = iter_trading_days(
            today - dt.timedelta(days=max(days * 2, days + 10)),
            today,
        )
        if not trading:
            return today, today
        picked = trading[-days:]
        return picked[0], picked[-1]
    if not start:
        raise ValueError("請指定 --start/--end 或 --days")
    start_d = _parse_date(start)
    end_d = _parse_date(end) if end else start_d
    if start_d > end_d:
        raise ValueError("start 不可晚於 end")
    return start_d, end_d


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="從 Shioaji 補齊分 K / Tick 歷史至 SQLite",
    )
    parser.add_argument(
        "--symbol", required=True,
        help="股票代號，逗號分隔多檔",
    )
    parser.add_argument("--start", help="開始日期 YYYY-MM-DD")
    parser.add_argument("--end", help="結束日期 YYYY-MM-DD (預設=start)")
    parser.add_argument(
        "--days", type=int, default=0,
        help="往回 N 個交易日 (與 --start 擇一)",
    )
    parser.add_argument(
        "--type", choices=("1m", "tick"), default="1m",
        help="1m=分K (預設), tick=逐筆",
    )
    parser.add_argument(
        "--chunk-days", type=int, default=1,
        help="每次 API 請求涵蓋幾個交易日 (預設 1)",
    )
    parser.add_argument(
        "--no-skip-existing", action="store_true",
        help="不跳過 DB 已有資料的交易日",
    )
    parser.add_argument(
        "--no-csv", action="store_true",
        help="不寫入 data/intraday/ CSV 快取",
    )
    parser.add_argument(
        "--delay", type=float, default=0.5,
        help="每次請求間隔秒數 (預設 0.5)",
    )
    parser.add_argument(
        "--timeout", type=int, default=60_000,
        help="單次 API timeout 毫秒 (預設 60000)",
    )
    parser.add_argument(
        "--simulation", choices=("true", "false"), default=None,
        help="覆寫 SIMULATION (預設讀 .env)",
    )
    args = parser.parse_args(argv)

    logger = get_logger("intraday-fetch")
    try:
        start_d, end_d = _resolve_range(
            start=args.start,
            end=args.end,
            days=args.days if args.days > 0 else None,
        )
    except ValueError as exc:
        logger.error("%s", exc)
        return 2

    symbols = [s.strip() for s in args.symbol.split(",") if s.strip()]
    if not symbols:
        logger.error("symbol 不可為空")
        return 2

    settings = Settings()
    if args.simulation is not None:
        settings = Settings(simulation=(args.simulation == "true"))

    if not settings.api_key or not settings.secret_key:
        logger.error("缺少 API_KEY / SECRET_KEY，無法登入 Shioaji 抓歷史資料")
        return 1

    from bot.broker import SjBroker

    broker = SjBroker(settings, logger=logger)
    if not broker.login():
        logger.error("Shioaji 登入失敗")
        return 1

    root = Path.cwd()
    db = StockDB.open(path=default_db_path(root))
    interval: IntervalKind = args.type

    logger.info(
        "開始補齊 %s | %s | %s ~ %s | chunk=%d 天",
        ",".join(symbols), interval, start_d, end_d, args.chunk_days,
    )

    exit_code = 0
    try:
        for sym in symbols:
            written, skipped = fetch_and_store_intraday(
                broker,
                sym,
                start_d,
                end_d,
                interval=interval,
                chunk_days=args.chunk_days,
                skip_existing_days=not args.no_skip_existing,
                save_csv=not args.no_csv,
                root=root,
                db=db,
                request_delay_sec=args.delay,
                timeout_ms=args.timeout,
                logger=logger,
            )
            summary = db.intraday_summary(interval=interval)
            sym_row = next((r for r in summary if r["symbol"] == sym), None)
            logger.info(
                "%s 完成: 本次寫入 %d 筆, 跳過 %d 天; DB 累計 %s",
                sym,
                written,
                skipped,
                sym_row or "0 筆",
            )
            if written == 0 and skipped == 0:
                exit_code = max(exit_code, 1)
    finally:
        broker.logout()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
