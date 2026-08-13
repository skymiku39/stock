"""Slow queue runner for local-first fundamental data refreshes."""

from __future__ import annotations

import argparse
import datetime as dt
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from bot.cloud_file_cache import read_json_cache, write_json_cache
from bot.config import Settings
from bot.fundamentals_fetcher import (
    build_fundamental_snapshot,
    snapshot_to_dict,
)
from bot.utils import get_logger, now_tw

QUEUE_FILE = "refresh_queue.json"
RETRY_MIN_SECONDS = 6 * 3600
RETRY_MAX_SECONDS = 72 * 3600


def _cache_root(root: Path | None = None) -> Path:
    base = (root or Path.cwd()) / "data" / "fundamentals"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _queue_path(root: Path | None = None) -> Path:
    return _cache_root(root) / QUEUE_FILE


def _parse_time(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now_tw().tzinfo)
    return parsed


def _normalize_symbol(symbol: str) -> str:
    return "".join(ch for ch in str(symbol).strip() if ch.isalnum())


def _split_symbols(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        for part in str(raw).split(","):
            sym = _normalize_symbol(part)
            if sym and sym not in seen:
                seen.add(sym)
                out.append(sym)
    return out


def load_refresh_queue(root: Path | None = None) -> dict[str, dict[str, Any]]:
    raw = read_json_cache(_queue_path(root), root=root) or {}
    raw_items = raw.get("items", {}) if isinstance(raw, dict) else {}
    if isinstance(raw_items, dict):
        items = raw_items.values()
    elif isinstance(raw_items, list):
        items = raw_items
    else:
        items = []

    out: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        symbol = _normalize_symbol(str(item.get("symbol") or ""))
        if not symbol:
            continue
        normalized = dict(item)
        normalized["symbol"] = symbol
        out[symbol] = normalized
    return out


def save_refresh_queue(
    items: dict[str, dict[str, Any]],
    *,
    root: Path | None = None,
) -> Path:
    ordered = sorted(
        items.values(),
        key=lambda i: (str(i.get("next_attempt_at") or ""), str(i.get("symbol") or "")),
    )
    payload = {
        "updated_at": now_tw().isoformat(timespec="seconds"),
        "items": ordered,
    }
    return write_json_cache(_queue_path(root), payload, root=root, indent=2)


def enqueue_refresh(
    symbol: str,
    *,
    root: Path | None = None,
    reason: str = "stale_or_missing_cache",
    delay_minutes: int = 0,
) -> dict[str, Any]:
    symbol = _normalize_symbol(symbol)
    if not symbol:
        raise ValueError("symbol is required")

    items = load_refresh_queue(root)
    now = now_tw()
    next_attempt = now + dt.timedelta(minutes=max(0, delay_minutes))
    item = dict(items.get(symbol) or {})
    item.setdefault("attempts", 0)
    item.setdefault("last_attempt_at", "")
    item.setdefault("last_success_at", "")
    item.setdefault("last_error", "")
    item["symbol"] = symbol
    item["reason"] = reason or str(item.get("reason") or "manual")
    if not _parse_time(item.get("next_attempt_at")):
        item["next_attempt_at"] = next_attempt.isoformat(timespec="seconds")
    items[symbol] = item
    save_refresh_queue(items, root=root)
    return item


def _needs_refresh(symbol: str, *, root: Path | None, stale_days: int) -> bool:
    ticker_dir = _cache_root(root) / symbol
    paths = [
        ticker_dir / "monthly_revenue.json",
        ticker_dir / "valuation_latest.json",
        ticker_dir / "dividends.json",
        ticker_dir / "quarterlies_raw.json",
        ticker_dir / "snapshot.json",
    ]
    existing = [p for p in paths if p.exists()]
    if not existing:
        return True
    if stale_days <= 0:
        return True
    newest = max(p.stat().st_mtime for p in existing)
    return (time.time() - newest) > stale_days * 86400


def seed_refresh_queue(
    symbols: Iterable[str],
    *,
    root: Path | None = None,
    stale_days: int = 30,
    force: bool = False,
    reason: str = "stale_or_missing_cache",
) -> int:
    added = 0
    items = load_refresh_queue(root)
    for symbol in _split_symbols(symbols):
        if not force and not _needs_refresh(symbol, root=root, stale_days=stale_days):
            continue
        if symbol not in items:
            added += 1
        item = dict(items.get(symbol) or {})
        item.setdefault("attempts", 0)
        item.setdefault("last_attempt_at", "")
        item.setdefault("last_success_at", "")
        item.setdefault("last_error", "")
        item.setdefault("next_attempt_at", now_tw().isoformat(timespec="seconds"))
        item["symbol"] = symbol
        item["reason"] = reason
        items[symbol] = item
    save_refresh_queue(items, root=root)
    return added


def _mark_failure(item: dict[str, Any], error: Exception) -> dict[str, Any]:
    attempts = int(item.get("attempts") or 0) + 1
    delay = min(RETRY_MIN_SECONDS * (2 ** max(0, attempts - 1)), RETRY_MAX_SECONDS)
    now = now_tw()
    item = dict(item)
    item["attempts"] = attempts
    item["last_attempt_at"] = now.isoformat(timespec="seconds")
    item["last_error"] = f"{type(error).__name__}: {error}"[:500]
    item["next_attempt_at"] = (
        now + dt.timedelta(seconds=delay)
    ).isoformat(timespec="seconds")
    return item


def _due_items(items: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    now = now_tw()
    due = []
    for item in items.values():
        next_attempt = _parse_time(item.get("next_attempt_at"))
        if next_attempt is None or next_attempt <= now:
            due.append(item)
    return sorted(
        due,
        key=lambda i: (str(i.get("next_attempt_at") or ""), str(i.get("symbol") or "")),
    )


def run_refresh_queue(
    *,
    root: Path | None = None,
    limit: int = 3,
    delay_seconds: int = 30,
    dry_run: bool = False,
    logger: Any = None,
) -> dict[str, int]:
    log = logger or get_logger("fundamentals-refresh")
    items = load_refresh_queue(root)
    due = _due_items(items)
    stats = {
        "queued": len(items),
        "due": len(due),
        "processed": 0,
        "succeeded": 0,
        "failed": 0,
        "skipped": 0,
    }
    if limit <= 0:
        return stats

    selected = due[:limit]
    for idx, item in enumerate(selected):
        symbol = _normalize_symbol(str(item.get("symbol") or ""))
        if not symbol:
            stats["skipped"] += 1
            continue
        if dry_run:
            log.info("dry-run: would refresh fundamentals for %s", symbol)
            stats["skipped"] += 1
            continue

        stats["processed"] += 1
        try:
            snap = build_fundamental_snapshot(symbol, root=root, refresh=True)
            if not snap.has_data:
                raise RuntimeError("empty fundamental snapshot")
            ticker_dir = _cache_root(root) / symbol
            ticker_dir.mkdir(parents=True, exist_ok=True)
            write_json_cache(
                ticker_dir / "snapshot.json",
                snapshot_to_dict(snap),
                root=root,
                indent=2,
            )
            items.pop(symbol, None)
            stats["succeeded"] += 1
            log.info("refreshed fundamentals for %s", symbol)
        except Exception as exc:
            items[symbol] = _mark_failure(item, exc)
            stats["failed"] += 1
            log.warning("fundamentals refresh failed for %s: %s", symbol, exc)
        finally:
            save_refresh_queue(items, root=root)

        if idx < len(selected) - 1 and delay_seconds > 0:
            time.sleep(delay_seconds)

    stats["queued"] = len(items)
    return stats


def _known_symbols(settings: Settings, root: Path, explicit: list[str]) -> list[str]:
    symbols = _split_symbols(explicit)
    symbols.extend(_split_symbols(settings.symbols))

    try:
        from bot import watchlist

        symbols.extend(item.ticker for item in watchlist.load(root).items)
    except Exception:
        pass

    base = _cache_root(root)
    try:
        symbols.extend(p.name for p in base.iterdir() if p.is_dir())
    except Exception:
        pass

    return _split_symbols(symbols)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stock-fundamentals-refresh",
        description="Run a small batch from the local fundamental refresh queue.",
    )
    parser.add_argument("--symbols", action="append", default=[], help="Comma-separated symbols to enqueue")
    parser.add_argument("--limit", type=int, default=3, help="Max queued symbols to refresh this run")
    parser.add_argument("--stale-days", type=int, default=30, help="Enqueue symbols whose local cache is older")
    parser.add_argument("--delay-seconds", type=int, default=30, help="Delay between symbols")
    parser.add_argument("--force", action="store_true", help="Enqueue symbols even when local cache is fresh")
    parser.add_argument("--enqueue-only", action="store_true", help="Only update the queue")
    parser.add_argument("--dry-run", action="store_true", help="Print work without refreshing")
    args = parser.parse_args(argv)

    root = Path.cwd()
    settings = Settings()
    log = get_logger("fundamentals-refresh")
    symbols = _known_symbols(settings, root, args.symbols)
    added = seed_refresh_queue(
        symbols,
        root=root,
        stale_days=args.stale_days,
        force=args.force,
    )
    log.info("seeded fundamentals refresh queue: added=%d symbols=%d", added, len(symbols))
    if args.enqueue_only:
        return 0

    stats = run_refresh_queue(
        root=root,
        limit=args.limit,
        delay_seconds=args.delay_seconds,
        dry_run=args.dry_run,
        logger=log,
    )
    log.info("fundamentals refresh stats: %s", stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "enqueue_refresh",
    "load_refresh_queue",
    "run_refresh_queue",
    "save_refresh_queue",
    "seed_refresh_queue",
]
