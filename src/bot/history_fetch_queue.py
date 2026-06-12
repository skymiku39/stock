"""history_fetch_queue -- 慢速排程補齊日 K / 分 K 歷史。

以「每次只抓少量月份」避免 TWSE / Shioaji 限流；狀態寫入
``data/history_fetch/state.json`` 可中斷後續跑。
"""

from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from bot.cloud_file_cache import mirror_file_to_cloud
from bot.llm_symbol_picks import collect_llm_symbols
from bot.stock_db import StockDB, default_db_path
from bot.technicals import (
    MonthlyKlineResult,
    _cache_dir,
    _coerce_df,
    _enumerate_months,
    _resolve_market,
    _save_df_to_db,
    _session,
    fetch_monthly_kline_with_meta,
    fetch_monthly_kline_yfinance,
)
from bot.utils import get_logger, mk_folder, now_tw

STATE_DIR_REL = "data/history_fetch"
STATE_FILE = "state.json"

# 程序內快取：TWSE 若已 403，後續批次直接走 yfinance，省掉無效 HTTP
_twse_blocked_cache: Optional[bool] = None


@dataclass
class WorkItem:
    symbol: str
    year: int
    month: int
    kind: str = "daily"  # daily | 1m


@dataclass
class FetchRunResult:
    item: Optional[WorkItem]
    ok: bool = False
    rows: int = 0
    http_status: int = 0
    message: str = ""
    rate_limited: bool = False


@dataclass
class QueueState:
    version: int = 1
    start_date: str = "2020-01-01"
    end_date: str = ""
    symbols: List[str] = field(default_factory=list)
    symbol_index: int = 0
    month_index: int = 0
    months: List[Tuple[int, int]] = field(default_factory=list)
    kind: str = "daily"
    cooldown_until: Dict[str, str] = field(default_factory=dict)
    stats_ok: int = 0
    stats_fail: int = 0
    stats_rows: int = 0
    last_item: str = ""
    last_message: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["months"] = [[y, m] for y, m in self.months]
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> QueueState:
        months_raw = data.get("months") or []
        months = [(int(y), int(m)) for y, m in months_raw]
        return cls(
            version=int(data.get("version", 1)),
            start_date=str(data.get("start_date", "2020-01-01")),
            end_date=str(data.get("end_date", "")),
            symbols=list(data.get("symbols") or []),
            symbol_index=int(data.get("symbol_index", 0)),
            month_index=int(data.get("month_index", 0)),
            months=months,
            kind=str(data.get("kind", "daily")),
            cooldown_until=dict(data.get("cooldown_until") or {}),
            stats_ok=int(data.get("stats_ok", 0)),
            stats_fail=int(data.get("stats_fail", 0)),
            stats_rows=int(data.get("stats_rows", 0)),
            last_item=str(data.get("last_item", "")),
            last_message=str(data.get("last_message", "")),
            updated_at=str(data.get("updated_at", "")),
        )


def state_path(root: Optional[Path] = None) -> Path:
    base = (root or Path.cwd()) / STATE_DIR_REL
    mk_folder(str(base))
    return base / STATE_FILE


def load_state(root: Optional[Path] = None) -> QueueState:
    path = state_path(root)
    if not path.exists():
        return QueueState()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return QueueState.from_dict(data)
    except Exception:
        return QueueState()


def save_state(state: QueueState, root: Optional[Path] = None) -> None:
    state.updated_at = now_tw().isoformat(timespec="seconds")
    path = state_path(root)
    path.write_text(
        json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def init_state(
    *,
    root: Optional[Path] = None,
    start_date: str = "2020-01-01",
    end_date: Optional[str] = None,
    symbols: Optional[List[str]] = None,
    kind: str = "daily",
    lookback_days: int = 5,
) -> QueueState:
    root = root or Path.cwd()
    end = end_date or now_tw().date().isoformat()
    start_d = dt.date.fromisoformat(start_date)
    end_d = dt.date.fromisoformat(end)
    syms = symbols or collect_llm_symbols(root, lookback_days=lookback_days)
    months = _enumerate_months(start_d, end_d)
    months.reverse()  # 由近到遠，優先補近期回測所需資料
    state = QueueState(
        start_date=start_date,
        end_date=end,
        symbols=syms,
        symbol_index=0,
        month_index=0,
        months=months,
        kind=kind,
    )
    save_state(state, root)
    return state


def _month_covered(
    db: StockDB,
    symbol: str,
    year: int,
    month: int,
    *,
    kind: str,
) -> bool:
    if kind == "1m":
        start = dt.date(year, month, 1)
        if month == 12:
            end = dt.date(year, 12, 31)
        else:
            end = dt.date(year, month + 1, 1) - dt.timedelta(days=1)
        n = db.count_intraday_bars(
            symbol, interval="1m",
            start=start.isoformat(), end=end.isoformat(),
        )
        return n >= 150
    start = f"{year:04d}-{month:02d}-01"
    if month == 12:
        end = f"{year:12}-31"
    else:
        end = (dt.date(year, month + 1, 1) - dt.timedelta(days=1)).isoformat()
    bars = db.get_price_history(symbol, start=start, end=end)
    return len(bars) >= 10


def _in_cooldown(state: QueueState, symbol: str, now: Optional[dt.datetime] = None) -> bool:
    raw = state.cooldown_until.get(symbol)
    if not raw:
        return False
    try:
        until = dt.datetime.fromisoformat(raw)
        if until.tzinfo:
            until = until.replace(tzinfo=None)
        now_naive = (now or now_tw()).replace(tzinfo=None)
        return now_naive < until
    except Exception:
        return False


def _set_cooldown(
    state: QueueState,
    symbol: str,
    minutes: int,
    now: Optional[dt.datetime] = None,
) -> None:
    until = (now or now_tw()) + dt.timedelta(minutes=minutes)
    state.cooldown_until[symbol] = until.isoformat(timespec="seconds")


def next_work_item(
    state: QueueState,
    db: StockDB,
    *,
    now: Optional[dt.datetime] = None,
) -> Optional[WorkItem]:
    if not state.symbols or not state.months:
        return None
    n_sym = len(state.symbols)
    n_mon = len(state.months)
    for _ in range(n_sym * n_mon):
        if state.symbol_index >= n_sym:
            return None
        sym = state.symbols[state.symbol_index]
        if _in_cooldown(state, sym, now):
            state.symbol_index += 1
            state.month_index = 0
            continue
        while state.month_index < n_mon:
            y, m = state.months[state.month_index]
            state.month_index += 1
            if _month_covered(db, sym, y, m, kind=state.kind):
                continue
            return WorkItem(symbol=sym, year=y, month=m, kind=state.kind)
        state.symbol_index += 1
        state.month_index = 0
    return None


def twse_is_available(session=None, *, force_check: bool = False) -> bool:
    """探測 TWSE 是否可用；403 時回傳 False 並快取結果。"""
    global _twse_blocked_cache
    if not force_check and _twse_blocked_cache is not None:
        return not _twse_blocked_cache
    sess = session or _session()
    try:
        url = (
            "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
            "?response=json&date=20250601&stockNo=2330"
        )
        resp = sess.get(url, timeout=15)
        ok = int(resp.status_code) == 200
    except Exception:
        ok = False
    _twse_blocked_cache = not ok
    return ok


def _save_daily_rows(
    symbol: str,
    rows: List[Dict[str, Any]],
    *,
    root: Path,
    db: StockDB,
    logger,
) -> int:
    if not rows:
        return 0
    df = _coerce_df(pd.DataFrame(rows))
    cache_path = _cache_dir(symbol, root) / "daily_kline.csv"
    existing = None
    if cache_path.exists():
        try:
            existing = pd.read_csv(cache_path, dtype={"date": str})
            existing = _coerce_df(existing)
        except Exception:
            existing = None
    if existing is not None and not existing.empty:
        merged = pd.concat([existing, df], ignore_index=True)
    else:
        merged = df
    merged = merged.drop_duplicates(subset=["date"], keep="last")
    merged = merged.sort_values("date").reset_index(drop=True)
    merged.to_csv(cache_path, index=False, encoding="utf-8")
    mirror_file_to_cloud(cache_path, root=root)
    _save_df_to_db(symbol, df, root=root, logger=logger)
    return len(rows)


def run_one_daily(
    item: WorkItem,
    *,
    root: Path,
    db: StockDB,
    logger,
    session=None,
    use_yfinance_fallback: bool = True,
) -> FetchRunResult:
    market = _resolve_market(item.symbol, root, logger=logger)
    rows: List[Dict[str, Any]] = []
    meta = MonthlyKlineResult(rows=[], http_status=0)
    source = "twse"
    if market == "twse" and not twse_is_available(session):
        source = "yfinance"
    else:
        meta = fetch_monthly_kline_with_meta(
            item.symbol, item.year, item.month,
            market=market, session=session, logger=logger,
        )
        rows = meta.rows
        if meta.http_status in (403, 429):
            global _twse_blocked_cache
            _twse_blocked_cache = True
    if not rows and use_yfinance_fallback and (
        source == "yfinance" or meta.http_status in (0, 403, 429)
    ):
        rows = fetch_monthly_kline_yfinance(
            item.symbol, item.year, item.month,
            market=market, logger=logger,
        )
        if rows:
            source = "yfinance"
            logger.info(
                "%s %04d/%02d TWSE 不可用，改以 yfinance 補 %d 根",
                item.symbol, item.year, item.month, len(rows),
            )
    if meta.http_status in (403, 429) and not rows:
        return FetchRunResult(
            item=item, ok=False, http_status=meta.http_status,
            message=f"HTTP {meta.http_status} 限流且 yfinance 無資料",
            rate_limited=True,
        )
    if rows:
        n = _save_daily_rows(item.symbol, rows, root=root, db=db, logger=logger)
        return FetchRunResult(
            item=item, ok=True, rows=n, http_status=meta.http_status,
            message=f"寫入 {n} 根日K ({source})",
        )
    return FetchRunResult(
        item=item, ok=False, http_status=meta.http_status,
        message=meta.error or "無資料",
    )


def process_batch(
    *,
    root: Optional[Path] = None,
    batch_size: int = 1,
    delay_sec: float = 2.0,
    cooldown_minutes: int = 45,
    reinit_if_done: bool = False,
    lookback_days: int = 5,
    use_yfinance_fallback: bool = True,
    logger=None,
) -> List[FetchRunResult]:
    """處理最多 batch_size 個工作項目；回傳每筆結果。"""
    log = logger or get_logger("history-fetch")
    root = root or Path.cwd()
    db = StockDB.open(path=default_db_path(root))
    state = load_state(root)
    if not state.symbols:
        state = init_state(root=root, lookback_days=lookback_days)
        log.info("初始化佇列：%d 檔 × %d 月", len(state.symbols), len(state.months))

    sess = _session()
    results: List[FetchRunResult] = []
    for _ in range(max(1, batch_size)):
        item = next_work_item(state, db)
        if item is None:
            if reinit_if_done:
                log.info("佇列已跑完一輪，重新掃描缺漏月份 …")
                state = init_state(
                    root=root,
                    start_date=state.start_date,
                    end_date=state.end_date or now_tw().date().isoformat(),
                    symbols=state.symbols,
                    kind=state.kind,
                    lookback_days=lookback_days,
                )
                item = next_work_item(state, db)
            if item is None:
                log.info("目前無待補項目（可能已全部齊備）")
                break

        log.info(
            "抓取 %s %04d/%02d (%s)",
            item.symbol, item.year, item.month, item.kind,
        )
        if item.kind == "daily":
            result = run_one_daily(
                item, root=root, db=db, logger=log, session=sess,
                use_yfinance_fallback=use_yfinance_fallback,
            )
        else:
            result = FetchRunResult(item=item, ok=False, message="1m 請用 stock-intraday-fetch")
        results.append(result)

        state.last_item = f"{item.symbol}:{item.year}/{item.month:02d}"
        state.last_message = result.message
        if result.ok:
            state.stats_ok += 1
            state.stats_rows += result.rows
        else:
            state.stats_fail += 1
            if result.rate_limited:
                _set_cooldown(state, item.symbol, cooldown_minutes)
                log.warning(
                    "%s 觸發限流，冷卻 %d 分鐘",
                    item.symbol, cooldown_minutes,
                )
        save_state(state, root)

        if delay_sec > 0:
            time.sleep(delay_sec)

    return results


def status_summary(root: Optional[Path] = None) -> Dict[str, Any]:
    root = root or Path.cwd()
    state = load_state(root)
    db = StockDB.open(path=default_db_path(root))
    pending = 0
    for sym in state.symbols:
        for y, m in state.months:
            if not _month_covered(db, sym, y, m, kind=state.kind):
                pending += 1
    return {
        "symbols": len(state.symbols),
        "months_per_symbol": len(state.months),
        "pending_months_est": pending,
        "cursor": {
            "symbol_index": state.symbol_index,
            "month_index": state.month_index,
            "current_symbol": (
                state.symbols[state.symbol_index]
                if state.symbols and state.symbol_index < len(state.symbols)
                else ""
            ),
        },
        "stats_ok": state.stats_ok,
        "stats_fail": state.stats_fail,
        "stats_rows": state.stats_rows,
        "last_item": state.last_item,
        "last_message": state.last_message,
        "cooldown_symbols": list(state.cooldown_until.keys()),
        "updated_at": state.updated_at,
        "range": f"{state.start_date} ~ {state.end_date or now_tw().date().isoformat()}",
    }
