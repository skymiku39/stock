"""watch_snapshot -- 只看不買：定時儲存約 10 元熱度與期貨快照。

輸出目錄: data/watch_snapshots/YYYY-MM-DD/
"""

from __future__ import annotations

import argparse
import csv
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from bot.hot_stock_futures import fetch_hot_stock_futures
from bot.price_band_heat import (
    DEFAULT_PRICE_HIGH,
    DEFAULT_PRICE_LOW,
    fetch_price_band_heat,
)
from bot.utils import get_logger, now_tw

SNAPSHOT_DIR = "watch_snapshots"


def snapshot_dir(root: Path | None = None) -> Path:
    base = (root or Path.cwd()) / "data" / SNAPSHOT_DIR
    day = now_tw().date().isoformat()
    path = base / day
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def save_watch_snapshot(
    *,
    root: Path | None = None,
    price_low: float = DEFAULT_PRICE_LOW,
    price_high: float = DEFAULT_PRICE_HIGH,
    band_limit: int = 40,
    futures_limit: int = 20,
    watch_symbols: list[str] | None = None,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """抓取並寫入一輪觀察快照，回傳摘要。"""
    log = logger or get_logger("watch-snapshot")
    project = root or Path.cwd()
    out_dir = snapshot_dir(project)
    ts = now_tw().strftime("%H%M%S")
    summary: dict[str, Any] = {
        "asof": now_tw().isoformat(timespec="seconds"),
        "dir": str(out_dir),
        "files": [],
        "errors": [],
    }

    band = fetch_price_band_heat(
        root=project,
        price_low=price_low,
        price_high=price_high,
        limit=band_limit,
        exclude_dr=True,
        watch_symbols=watch_symbols,
        logger=log,
    )
    summary["errors"].extend(band.errors)
    band_path = out_dir / f"{ts}_price_band_{price_low:.1f}-{price_high:.1f}.csv"
    band_fields = [
        "asof", "band_rank", "ticker", "name", "price", "pct_chg", "volume",
        "market_volume_rank", "tick2_pct", "in_watchlist", "quote_time",
    ]
    band_rows = [
        {"asof": band.asof, **asdict(row)}
        for row in band.rows
    ]
    _write_csv(band_path, band_rows, band_fields)
    summary["files"].append(str(band_path))
    summary["band_count"] = band.band_count

    futures = fetch_hot_stock_futures(limit=futures_limit, direction="volume", logger=log)
    summary["errors"].extend(futures.errors)
    fut_path = out_dir / f"{ts}_hot_stock_futures.csv"
    fut_fields = [
        "asof", "spot_id", "symbol_id", "name", "last_price", "pct_chg",
        "volume", "amp_rate", "quote_time",
    ]
    fut_rows = [{"asof": futures.asof, **asdict(row)} for row in futures.rows]
    _write_csv(fut_path, fut_rows, fut_fields)
    summary["files"].append(str(fut_path))

    daily_log = out_dir / f"daily_log_{now_tw().date().isoformat()}.csv"
    log_fields = ["asof", "kind", "band_count", "top_ticker", "top_volume", "note"]
    top = band.rows[0] if band.rows else None
    log_row = {
        "asof": summary["asof"],
        "kind": "snapshot",
        "band_count": band.band_count,
        "top_ticker": top.ticker if top else "",
        "top_volume": top.volume if top else 0,
        "note": f"files={len(summary['files'])}",
    }
    write_header = not daily_log.exists()
    with daily_log.open("a", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=log_fields)
        if write_header:
            writer.writeheader()
        writer.writerow(log_row)
    summary["daily_log"] = str(daily_log)

    log.info(
        "watch snapshot saved: band=%d files=%d -> %s",
        band.band_count,
        len(summary["files"]),
        out_dir,
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="儲存只看不買觀察快照")
    parser.add_argument("--low", type=float, default=DEFAULT_PRICE_LOW)
    parser.add_argument("--high", type=float, default=DEFAULT_PRICE_HIGH)
    parser.add_argument("--band-limit", type=int, default=40)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    summary = save_watch_snapshot(
        root=args.root,
        price_low=args.low,
        price_high=args.high,
        band_limit=args.band_limit,
    )
    print(f"saved {len(summary['files'])} files under {summary['dir']}")
    for err in summary.get("errors") or []:
        print(f"warn: {err}")


if __name__ == "__main__":
    main()
