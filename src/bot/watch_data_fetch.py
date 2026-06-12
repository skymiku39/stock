"""watch_data_fetch -- 只看不買：量化交易 + 微笑曲線所需樣本資料一鍵補齊。

抓取項目
========
* 公司基本資料 (stock-company-update)
* 日 K 線 → stock.db price_history（微笑曲線回測、技術面）
* 基本面（月營收、PER/PBR、股利）
* 籌碼面（三大法人、融資券等）
* 技術指標快照
* 跨市場 macro（yfinance + 台指期）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

import datetime as dt

import pandas as pd

from bot.config import Settings
from bot.env_io import load_env
from bot.events import DailyKlineFetched, QuantDataFetchCompleted
from bot.events.protocols import EventPublisher
from bot.events.wiring import publish_if_bus
from bot.fundamentals_refresh import run_refresh_queue, seed_refresh_queue
from bot.market_meta import load_market_map
from bot.smile_curve_backtest_cli import _ensure_daily_data, _normalize_symbol
from bot.stock_db import StockDB, default_db_path
from bot.ticker_view import build_snapshot
from bot.utils import get_logger, now_tw

MIN_DAILY_BARS = 400


def resolve_symbols(
    raw: Optional[str] = None,
    *,
    root: Optional[Path] = None,
) -> List[str]:
    if raw:
        parts = [_normalize_symbol(s) for s in raw.split(",") if s.strip()]
        return list(dict.fromkeys(parts))
    env = load_env()
    from_env = [
        _normalize_symbol(s)
        for s in (env.get("SYMBOLS") or Settings().symbols or "").split(",")
        if s.strip()
    ]
    if from_env:
        return list(dict.fromkeys(from_env))
    from bot.price_band_heat import DEFAULT_WATCH_SYMBOLS
    return list(DEFAULT_WATCH_SYMBOLS)


def _backfill_yfinance_if_sparse(
    ticker: str,
    *,
    start: str,
    end: str,
    root: Path,
    db: StockDB,
    logger,
    min_bars: int = MIN_DAILY_BARS,
) -> int:
    """TWSE 307/403 時以 yfinance 補日 K。"""
    existing = db.get_price_history(ticker, limit=20_000, ascending=True)
    if len(existing) >= min_bars:
        return len(existing)

    try:
        import yfinance as yf  # type: ignore
    except ImportError:
        logger.warning("%s yfinance 未安裝，無法備援日 K", ticker)
        return len(existing)

    market_map = load_market_map(root=root)
    market = market_map.get(ticker, "twse")
    suffixes = (".TWO", ".TW") if market == "tpex" else (".TW", ".TWO")
    start_d = dt.date.fromisoformat(start)
    end_d = dt.date.fromisoformat(end)
    end_excl = (end_d + dt.timedelta(days=1)).isoformat()

    rows: list[dict] = []
    for suffix in suffixes:
        sym = f"{ticker}{suffix}"
        try:
            hist = yf.Ticker(sym).history(
                start=start_d.isoformat(),
                end=end_excl,
                auto_adjust=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s yfinance 例外: %s", sym, exc)
            continue
        if hist is None or hist.empty:
            continue
        for idx, row in hist.iterrows():
            try:
                d = idx.date() if hasattr(idx, "date") else dt.date.fromisoformat(str(idx)[:10])
            except Exception:
                continue
            if d < start_d or d > end_d:
                continue
            rows.append({
                "date": d.isoformat(),
                "open": float(row.get("Open", 0) or 0),
                "high": float(row.get("High", 0) or 0),
                "low": float(row.get("Low", 0) or 0),
                "close": float(row.get("Close", 0) or 0),
                "volume": float(row.get("Volume", 0) or 0) / 1000.0,
            })
        if rows:
            break

    if not rows:
        return len(existing)

    from bot.technicals import _cache_dir, _coerce_df, _save_df_to_db

    df = _coerce_df(pd.DataFrame(rows))
    cache_path = _cache_dir(ticker, root) / "daily_kline.csv"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    if cache_path.exists():
        try:
            old = _coerce_df(pd.read_csv(cache_path, dtype={"date": str}))
            df = _coerce_df(pd.concat([old, df], ignore_index=True))
        except Exception:
            pass
    df = df.drop_duplicates(subset=["date"], keep="last").sort_values("date")
    df.to_csv(cache_path, index=False, encoding="utf-8")
    _save_df_to_db(ticker, df, root=root, logger=logger)
    logger.info("%s yfinance 備援日 K → %d 筆", ticker, len(df))
    return len(df)


def _publish_kline_events(
    publisher: Optional[EventPublisher],
    symbols: List[str],
    *,
    start: str,
    end: str,
    price_bars: dict[str, int],
) -> None:
    for sym in symbols:
        publish_if_bus(
            publisher,
            DailyKlineFetched(
                symbol=sym,
                bar_count=int(price_bars.get(sym, 0)),
                start=start,
                end=end,
            ),
        )


def fetch_watch_quant_data(
    symbols: List[str],
    *,
    root: Optional[Path] = None,
    start: str = "2023-01-01",
    end: Optional[str] = None,
    chip_days: int = 10,
    skip_company: bool = False,
    skip_macro: bool = False,
    fundamentals_delay: int = 8,
    logger=None,
    publisher: Optional[EventPublisher] = None,
) -> dict:
    """補齊監控池的量化 / 微笑曲線樣本資料。"""
    log = logger or get_logger("watch-data-fetch")
    project = root or Path.cwd()
    end_s = end or now_tw().date().isoformat()
    summary: dict = {
        "asof": now_tw().isoformat(timespec="seconds"),
        "symbols": symbols,
        "start": start,
        "end": end_s,
        "steps": [],
        "errors": [],
        "price_bars": {},
    }

    if not symbols:
        summary["errors"].append("監控池為空")
        return summary

    if not skip_company:
        try:
            from bot.company_update import main as company_main
            log.info("=== [1/5] 公司基本資料 ===")
            rc = company_main([])
            summary["steps"].append({"name": "company", "ok": rc == 0})
        except Exception as exc:  # noqa: BLE001
            msg = f"company_update: {exc}"
            log.warning(msg)
            summary["errors"].append(msg)

    db = StockDB.open(path=default_db_path(project))
    log.info("=== [2/5] 日 K 線 (%s ~ %s) ===", start, end_s)
    try:
        _ensure_daily_data(symbols, start, end_s, root=project, db=db, logger=log)
        for sym in symbols:
            n = _backfill_yfinance_if_sparse(
                sym, start=start, end=end_s, root=project, db=db, logger=log,
            )
            summary["price_bars"][sym] = n
        summary["steps"].append({"name": "daily_k", "ok": True, "bars": summary["price_bars"]})
        _publish_kline_events(
            publisher, symbols, start=start, end=end_s, price_bars=summary["price_bars"],
        )
    except Exception as exc:  # noqa: BLE001
        msg = f"daily_k: {exc}"
        log.exception(msg)
        summary["errors"].append(msg)

    log.info("=== [3/5] 基本面 ===")
    try:
        added = seed_refresh_queue(symbols, root=project, stale_days=0, force=True)
        stats = run_refresh_queue(
            root=project,
            limit=len(symbols),
            delay_seconds=fundamentals_delay,
            logger=log,
        )
        summary["steps"].append({"name": "fundamentals", "queued": added, "stats": stats})
    except Exception as exc:  # noqa: BLE001
        msg = f"fundamentals: {exc}"
        log.warning(msg)
        summary["errors"].append(msg)

    log.info("=== [4/5] 籌碼 + 技術面 (%d 檔) ===", len(symbols))
    snap_ok = 0
    for i, sym in enumerate(symbols, start=1):
        try:
            log.info("[%d/%d] build_snapshot %s", i, len(symbols), sym)
            build_snapshot(
                sym,
                project,
                refresh_chips=True,
                chip_days=chip_days,
                refresh_fundamentals=False,
                refresh_technicals=True,
                refresh_distribution=True,
                refresh_macro=False,
                auto_fill_missing=True,
                auto_llm=False,
                macro_cache_only=True,
            )
            snap_ok += 1
        except Exception as exc:  # noqa: BLE001
            msg = f"snapshot {sym}: {exc}"
            log.warning(msg)
            summary["errors"].append(msg)
    summary["steps"].append({"name": "snapshots", "ok": snap_ok, "total": len(symbols)})

    if not skip_macro:
        log.info("=== [5/5] 跨市場 macro ===")
        try:
            from bot.market_macro import fetch_macro_snapshot
            fetch_macro_snapshot(root=project, force_refresh=False, use_cache=True)
            summary["steps"].append({"name": "macro", "ok": True})
        except Exception as exc:  # noqa: BLE001
            msg = f"macro: {exc}"
            log.warning(msg)
            summary["errors"].append(msg)

    log.info(
        "watch quant data done: symbols=%d errors=%d",
        len(symbols),
        len(summary["errors"]),
    )
    publish_if_bus(
        publisher,
        QuantDataFetchCompleted(
            symbols=tuple(symbols),
            start=start,
            end=end_s,
            price_bars=dict(summary.get("price_bars", {})),
            errors=tuple(summary.get("errors", [])),
        ),
    )
    return summary


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="補齊監控池量化交易 / 微笑曲線樣本資料",
    )
    parser.add_argument("--symbols", default="", help="逗號分隔；預設讀 .env SYMBOLS")
    parser.add_argument("--start", default="2023-01-01", help="日 K 起點")
    parser.add_argument("--end", default="", help="日 K 終點 (預設今天)")
    parser.add_argument("--chip-days", type=int, default=10, help="籌碼回顧天數")
    parser.add_argument("--skip-company", action="store_true")
    parser.add_argument("--skip-macro", action="store_true")
    parser.add_argument("--kline-only", action="store_true", help="只補日 K（含 yfinance 備援）")
    parser.add_argument("--json", action="store_true", help="印 JSON 摘要")
    args = parser.parse_args(argv)

    symbols = resolve_symbols(args.symbols or None)
    logger = get_logger("watch-data-fetch")
    from bot.app_bootstrap import get_or_create_bus

    publisher = get_or_create_bus()
    logger.info("目標代號: %s", ",".join(symbols))
    if args.kline_only:
        project = Path.cwd()
        db = StockDB.open(path=default_db_path(project))
        end_s = args.end or now_tw().date().isoformat()
        summary = {
            "asof": now_tw().isoformat(timespec="seconds"),
            "symbols": symbols,
            "price_bars": {},
            "errors": [],
        }
        _ensure_daily_data(symbols, args.start, end_s, root=project, db=db, logger=logger)
        for sym in symbols:
            summary["price_bars"][sym] = _backfill_yfinance_if_sparse(
                sym, start=args.start, end=end_s, root=project, db=db, logger=logger,
            )
        _publish_kline_events(
            publisher, symbols, start=args.start, end=end_s, price_bars=summary["price_bars"],
        )
        publish_if_bus(
            publisher,
            QuantDataFetchCompleted(
                symbols=tuple(symbols),
                start=args.start,
                end=end_s,
                price_bars=dict(summary["price_bars"]),
                errors=tuple(summary["errors"]),
            ),
        )
    else:
        summary = fetch_watch_quant_data(
            symbols,
            start=args.start,
            end=args.end or None,
            chip_days=args.chip_days,
            skip_company=args.skip_company,
            skip_macro=args.skip_macro,
            logger=logger,
            publisher=publisher,
        )
    out_dir = Path.cwd() / "data" / "watch_snapshots"
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f"quant_fetch_{now_tw().strftime('%Y%m%d_%H%M%S')}.json"
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"report: {report_path}")
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print("price_bars:", summary.get("price_bars"))
        if summary.get("errors"):
            print("errors:", len(summary["errors"]))
            for err in summary["errors"][:5]:
                print(" -", err)
    return 0 if not summary.get("errors") else 2


if __name__ == "__main__":
    raise SystemExit(main())
