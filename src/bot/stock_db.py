"""stock_db -- 集中管理股票冷/溫資料的本地 SQLite 資料庫。

設計理念
========
1. **本地優先 (Local-first)**：所有 read 直接打 SQLite，零網路延遲。
2. **Cache-Aside 模式**：找不到資料才呼叫外部 API，並把結果寫回 DB。
3. **雲端同步可選 (Optional Cloud Sync)**：DB 可單純當本地檔案用；
   要多機共用時透過 `cloud_sync.py` 把每張 table push/pull 到 Google Sheets。
4. **schema 與 Sheets 對齊**：每個 table 對應一個 worksheet，欄位名稱完全一致，
   方便手動編輯或從 Google Forms 寫入。

資料分層
========
* **冷資料 (Cold)**：`stock_info`, `etf_meta`, `industry_meta`
  - 極少變動 (公司名/產業/上市日)，遇缺才抓
* **溫資料 (Warm)**：`monthly_revenue`, `quarterly_report`, `dividend_history`
  - 月/季/年更新，可由背景排程或手動觸發
* **使用者資料 (User)**：`watchlist`, `notes`, `tags`
  - 個人筆記、追蹤清單，靠雲端同步在多機共用
* **同步狀態 (Meta)**：`sync_meta`
  - 紀錄每張 table 最後 push/pull 的時間戳，用於衝突解決

線程安全
========
SQLite 預設使用 WAL 模式，多 reader + 單 writer 可並行；模組層提供連線池，
每個 thread 取得自己的 Connection。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

from bot.utils import get_logger, now_tw


# ----------------------------------------------------------------------
# 資料模型 (Dataclass，與 SQLite 欄位 1:1 對應)
# ----------------------------------------------------------------------


@dataclass
class StockInfo:
    """股票冷資料 — 極少變動的基本面欄位。"""

    symbol: str                  # 股票代碼，PK，e.g. "2330"
    name: str = ""               # 公司名稱，e.g. "台積電"
    short_name: str = ""         # 簡稱，e.g. "TSMC"
    market: str = "TWSE"         # 市場別 (TWSE / TPEX / EMG)
    industry: str = ""           # 產業類別 (半導體業 / 電子工業 / ...)
    isin: str = ""               # ISIN 代碼 (e.g. TW0002330008)
    listed_date: str = ""        # 上市日期 ISO，e.g. "1994-09-05"
    capital: float = 0.0         # 實收資本額 (新台幣億)
    shares_outstanding: int = 0  # 流通股數
    cfi_code: str = ""           # CFI 證券分類
    note: str = ""               # 自訂備註
    updated_at: str = ""         # 最後更新時間 ISO


@dataclass
class EtfMeta:
    """主動式 ETF 基本資料 (與 active_etf.py 互通)。"""

    symbol: str
    name: str = ""
    manager: str = ""            # 投信公司
    inception_date: str = ""
    holdings_url: str = ""       # 持股 PDF/HTML 來源
    note: str = ""
    updated_at: str = ""


@dataclass
class MonthlyRevenue:
    """每月營收 (溫資料)。"""

    symbol: str
    year_month: str              # "2026-04"，PK 之一
    revenue: float = 0.0         # 月營收 (新台幣千元)
    yoy_pct: float = 0.0         # 年增率 %
    mom_pct: float = 0.0         # 月增率 %
    cumulative: float = 0.0      # 累計營收
    cum_yoy_pct: float = 0.0
    note: str = ""
    updated_at: str = ""


@dataclass
class QuarterlyReport:
    """季報關鍵指標 (EPS / 三率)。"""

    symbol: str
    period: str                  # "2026Q1"，PK 之一
    eps: float = 0.0
    revenue: float = 0.0         # 季營收
    gross_margin: float = 0.0    # 毛利率 %
    op_margin: float = 0.0       # 營業利益率 %
    net_margin: float = 0.0      # 淨利率 %
    note: str = ""
    updated_at: str = ""


@dataclass
class WatchlistRow:
    """使用者自訂監控清單 (取代/相容 watchlist.json)。"""

    symbol: str
    name: str = ""
    tags: str = ""               # 逗號分隔 ("半導體,共識持股")
    note: str = ""
    added_at: str = ""
    updated_at: str = ""


@dataclass
class PriceBar:
    """單檔股票的單日 K 線 (OHLCV)。

    PK = (symbol, date)；date 為 ISO `YYYY-MM-DD` 字串。
    volume 單位為「張」(以 TWSE 為準)。
    """

    symbol: str
    date: str                    # ISO YYYY-MM-DD
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0          # 張數
    source: str = "twse"         # 資料來源 (twse / shioaji / manual)
    note: str = ""
    updated_at: str = ""


@dataclass
class SyncMeta:
    """每張 table 的同步狀態紀錄。"""

    table_name: str
    last_push_at: str = ""
    last_pull_at: str = ""
    last_synced_rows: int = 0
    last_error: str = ""


@dataclass
class LlmAnalysisRow:
    """LLM 分析歷史紀錄 (任何 prompt、任何 ticker 的單次結果)。

    可同步到 Google Sheets 供日後回放/檢視。
    """

    id: str                       # ts + prompt_id + ticker 拼出的 PK
    ts: str                       # ISO 時戳 (台灣時區)
    ticker: str = ""              # 對應股票 (空字串代表全市場 prompt)
    prompt_id: str = ""           # e.g. analyze_presentation
    prompt_version: str = ""
    model: str = ""
    sentiment: str = ""           # positive / neutral / negative
    sentiment_score: float = 0.0  # -1.0 ~ 1.0
    confidence: float = 0.0
    summary: str = ""             # 200 字內中文摘要
    drivers: str = ""             # 多筆用 ; 連接
    risks: str = ""               # 多筆用 ; 連接
    raw_output: str = ""          # 完整 JSON / 文字輸出
    latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    success: int = 1              # 1=成功, 0=失敗
    error: str = ""
    source: str = ""              # auto_llm / pipeline / manual
    updated_at: str = ""


@dataclass
class LlmDailyReportRow:
    """每日 LLM 報告紀錄，供 dashboard 依日期回放。

    PK = (report_type, report_date, mode)。intraday 的 report_date 是
    asof 日期；next_day_watch 的 report_date 是 target_date。
    """

    report_type: str                # intraday / next_day_watch
    report_date: str                # ISO YYYY-MM-DD
    mode: str = ""                  # intraday="", next_day_watch=draft/update
    asof: str = ""
    generated_at: str = ""
    market_tone: str = ""
    prompt_id: str = ""
    prompt_version: str = ""
    brief_md: str = ""
    payload_json: str = ""          # full report JSON
    updated_at: str = ""


# ----------------------------------------------------------------------
# Schema (CREATE TABLE 語句)
# ----------------------------------------------------------------------


_SCHEMA: Dict[str, str] = {
    "stock_info": """
        CREATE TABLE IF NOT EXISTS stock_info (
            symbol              TEXT PRIMARY KEY,
            name                TEXT NOT NULL DEFAULT '',
            short_name          TEXT NOT NULL DEFAULT '',
            market              TEXT NOT NULL DEFAULT 'TWSE',
            industry            TEXT NOT NULL DEFAULT '',
            isin                TEXT NOT NULL DEFAULT '',
            listed_date         TEXT NOT NULL DEFAULT '',
            capital             REAL NOT NULL DEFAULT 0,
            shares_outstanding  INTEGER NOT NULL DEFAULT 0,
            cfi_code            TEXT NOT NULL DEFAULT '',
            note                TEXT NOT NULL DEFAULT '',
            updated_at          TEXT NOT NULL DEFAULT ''
        )
    """,
    "etf_meta": """
        CREATE TABLE IF NOT EXISTS etf_meta (
            symbol          TEXT PRIMARY KEY,
            name            TEXT NOT NULL DEFAULT '',
            manager         TEXT NOT NULL DEFAULT '',
            inception_date  TEXT NOT NULL DEFAULT '',
            holdings_url    TEXT NOT NULL DEFAULT '',
            note            TEXT NOT NULL DEFAULT '',
            updated_at      TEXT NOT NULL DEFAULT ''
        )
    """,
    "monthly_revenue": """
        CREATE TABLE IF NOT EXISTS monthly_revenue (
            symbol         TEXT NOT NULL,
            year_month     TEXT NOT NULL,
            revenue        REAL NOT NULL DEFAULT 0,
            yoy_pct        REAL NOT NULL DEFAULT 0,
            mom_pct        REAL NOT NULL DEFAULT 0,
            cumulative     REAL NOT NULL DEFAULT 0,
            cum_yoy_pct    REAL NOT NULL DEFAULT 0,
            note           TEXT NOT NULL DEFAULT '',
            updated_at     TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (symbol, year_month)
        )
    """,
    "quarterly_report": """
        CREATE TABLE IF NOT EXISTS quarterly_report (
            symbol        TEXT NOT NULL,
            period        TEXT NOT NULL,
            eps           REAL NOT NULL DEFAULT 0,
            revenue       REAL NOT NULL DEFAULT 0,
            gross_margin  REAL NOT NULL DEFAULT 0,
            op_margin     REAL NOT NULL DEFAULT 0,
            net_margin    REAL NOT NULL DEFAULT 0,
            note          TEXT NOT NULL DEFAULT '',
            updated_at    TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (symbol, period)
        )
    """,
    "watchlist": """
        CREATE TABLE IF NOT EXISTS watchlist (
            symbol      TEXT PRIMARY KEY,
            name        TEXT NOT NULL DEFAULT '',
            tags        TEXT NOT NULL DEFAULT '',
            note        TEXT NOT NULL DEFAULT '',
            added_at    TEXT NOT NULL DEFAULT '',
            updated_at  TEXT NOT NULL DEFAULT ''
        )
    """,
    "price_history": """
        CREATE TABLE IF NOT EXISTS price_history (
            symbol      TEXT NOT NULL,
            date        TEXT NOT NULL,
            open        REAL NOT NULL DEFAULT 0,
            high        REAL NOT NULL DEFAULT 0,
            low         REAL NOT NULL DEFAULT 0,
            close       REAL NOT NULL DEFAULT 0,
            volume      REAL NOT NULL DEFAULT 0,
            source      TEXT NOT NULL DEFAULT 'twse',
            note        TEXT NOT NULL DEFAULT '',
            updated_at  TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (symbol, date)
        )
    """,
    "sync_meta": """
        CREATE TABLE IF NOT EXISTS sync_meta (
            table_name        TEXT PRIMARY KEY,
            last_push_at      TEXT NOT NULL DEFAULT '',
            last_pull_at      TEXT NOT NULL DEFAULT '',
            last_synced_rows  INTEGER NOT NULL DEFAULT 0,
            last_error        TEXT NOT NULL DEFAULT ''
        )
    """,
    "llm_analysis_history": """
        CREATE TABLE IF NOT EXISTS llm_analysis_history (
            id                 TEXT PRIMARY KEY,
            ts                 TEXT NOT NULL DEFAULT '',
            ticker             TEXT NOT NULL DEFAULT '',
            prompt_id          TEXT NOT NULL DEFAULT '',
            prompt_version     TEXT NOT NULL DEFAULT '',
            model              TEXT NOT NULL DEFAULT '',
            sentiment          TEXT NOT NULL DEFAULT '',
            sentiment_score    REAL NOT NULL DEFAULT 0,
            confidence         REAL NOT NULL DEFAULT 0,
            summary            TEXT NOT NULL DEFAULT '',
            drivers            TEXT NOT NULL DEFAULT '',
            risks              TEXT NOT NULL DEFAULT '',
            raw_output         TEXT NOT NULL DEFAULT '',
            latency_ms         INTEGER NOT NULL DEFAULT 0,
            tokens_in          INTEGER NOT NULL DEFAULT 0,
            tokens_out         INTEGER NOT NULL DEFAULT 0,
            success            INTEGER NOT NULL DEFAULT 1,
            error              TEXT NOT NULL DEFAULT '',
            source             TEXT NOT NULL DEFAULT '',
            updated_at         TEXT NOT NULL DEFAULT ''
        )
    """,
    "llm_daily_reports": """
        CREATE TABLE IF NOT EXISTS llm_daily_reports (
            report_type        TEXT NOT NULL,
            report_date        TEXT NOT NULL,
            mode               TEXT NOT NULL DEFAULT '',
            asof               TEXT NOT NULL DEFAULT '',
            generated_at       TEXT NOT NULL DEFAULT '',
            market_tone        TEXT NOT NULL DEFAULT '',
            prompt_id          TEXT NOT NULL DEFAULT '',
            prompt_version     TEXT NOT NULL DEFAULT '',
            brief_md           TEXT NOT NULL DEFAULT '',
            payload_json       TEXT NOT NULL DEFAULT '',
            updated_at         TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (report_type, report_date, mode)
        )
    """,
}

ALL_TABLES: Tuple[str, ...] = tuple(_SCHEMA.keys())

# 預設 sync 範圍：使用者通常想同步的表 (sync_meta 不需要)
SYNCABLE_TABLES: Tuple[str, ...] = (
    "stock_info",
    "etf_meta",
    "monthly_revenue",
    "quarterly_report",
    "watchlist",
    "price_history",
    "llm_analysis_history",
    "llm_daily_reports",
)


# ----------------------------------------------------------------------
# 連線管理
# ----------------------------------------------------------------------


_DEFAULT_DB_REL = "data/stock.db"


def default_db_path(root: Optional[Path] = None) -> Path:
    """DB 預設位置 (專案根/data/stock.db)。可由 .env 覆寫。"""
    return (root or Path.cwd()) / _DEFAULT_DB_REL


class StockDB:
    """應用層唯一的 DB 入口。

    使用範例
    --------
    >>> db = StockDB.open()                       # 自動建表
    >>> db.upsert_stock_info(StockInfo("2330", "台積電", industry="半導體業"))
    >>> info = db.get_stock_info("2330")
    >>> all_watch = db.list_watchlist()
    """

    def __init__(self, path: Path, logger: Optional[logging.Logger] = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._log = logger or get_logger("stock_db")
        self._local = threading.local()
        self._init_schema()

    @classmethod
    def open(
        cls,
        path: Optional[Path] = None,
        root: Optional[Path] = None,
    ) -> "StockDB":
        return cls(path or default_db_path(root))

    # ----- 連線 (per-thread) -----

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.path),
            detect_types=sqlite3.PARSE_DECLTYPES,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._connect()
            self._local.conn = c
        return c

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """簡單 BEGIN/COMMIT 包裝。"""
        c = self.conn
        try:
            c.execute("BEGIN")
            yield c
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    # ----- Schema -----

    def _init_schema(self) -> None:
        with self.transaction() as c:
            for ddl in _SCHEMA.values():
                c.execute(ddl)
            for tbl in SYNCABLE_TABLES:
                c.execute(
                    "INSERT OR IGNORE INTO sync_meta (table_name) VALUES (?)",
                    (tbl,),
                )

    # ----- 通用 helpers -----

    def _row_count(self, table: str) -> int:
        cur = self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}")
        return int(cur.fetchone()["n"])

    def table_counts(self) -> Dict[str, int]:
        return {t: self._row_count(t) for t in ALL_TABLES}

    def fetch_all_rows(self, table: str) -> List[Dict[str, Any]]:
        """同步用：把整張 table 拉成 list[dict]。"""
        cur = self.conn.execute(f"SELECT * FROM {table}")
        return [dict(r) for r in cur.fetchall()]

    def replace_table_rows(
        self,
        table: str,
        rows: Iterable[Dict[str, Any]],
    ) -> int:
        """同步用：用雲端的整批資料覆蓋本地。回傳寫入筆數。"""
        rows = list(rows)
        cols_sql = self._table_columns(table)
        col_names = [c for c in cols_sql]
        placeholders = ",".join(["?"] * len(col_names))
        sql = (
            f"INSERT OR REPLACE INTO {table} "
            f"({','.join(col_names)}) VALUES ({placeholders})"
        )
        with self.transaction() as c:
            c.execute(f"DELETE FROM {table}")
            for r in rows:
                values = [r.get(name, _default_for(table, name)) for name in col_names]
                c.execute(sql, values)
        return len(rows)

    def upsert_rows(
        self,
        table: str,
        rows: Iterable[Dict[str, Any]],
    ) -> int:
        """同步用：增量合併 (不刪除本地獨有)。回傳寫入筆數。"""
        rows = list(rows)
        col_names = self._table_columns(table)
        placeholders = ",".join(["?"] * len(col_names))
        sql = (
            f"INSERT OR REPLACE INTO {table} "
            f"({','.join(col_names)}) VALUES ({placeholders})"
        )
        with self.transaction() as c:
            for r in rows:
                values = [r.get(name, _default_for(table, name)) for name in col_names]
                c.execute(sql, values)
        return len(rows)

    def _table_columns(self, table: str) -> List[str]:
        cur = self.conn.execute(f"PRAGMA table_info({table})")
        return [r["name"] for r in cur.fetchall()]

    # =================================================================
    # stock_info DAO
    # =================================================================

    def get_stock_info(self, symbol: str) -> Optional[StockInfo]:
        row = self.conn.execute(
            "SELECT * FROM stock_info WHERE symbol = ?", (symbol,)
        ).fetchone()
        return _row_to_dc(StockInfo, row) if row else None

    def list_stock_info(
        self,
        *,
        industry: Optional[str] = None,
        symbols: Optional[Iterable[str]] = None,
    ) -> List[StockInfo]:
        sql = "SELECT * FROM stock_info WHERE 1=1"
        args: List[Any] = []
        if industry:
            sql += " AND industry = ?"
            args.append(industry)
        if symbols is not None:
            symbols = list(symbols)
            if not symbols:
                return []
            sql += f" AND symbol IN ({','.join(['?'] * len(symbols))})"
            args.extend(symbols)
        sql += " ORDER BY symbol"
        rows = self.conn.execute(sql, args).fetchall()
        return [_row_to_dc(StockInfo, r) for r in rows]

    def upsert_stock_info(self, info: StockInfo) -> None:
        if not info.updated_at:
            info.updated_at = now_tw().isoformat(timespec="seconds")
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO stock_info
                  (symbol, name, short_name, market, industry, isin,
                   listed_date, capital, shares_outstanding, cfi_code,
                   note, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol) DO UPDATE SET
                  name = excluded.name,
                  short_name = excluded.short_name,
                  market = excluded.market,
                  industry = excluded.industry,
                  isin = excluded.isin,
                  listed_date = excluded.listed_date,
                  capital = excluded.capital,
                  shares_outstanding = excluded.shares_outstanding,
                  cfi_code = excluded.cfi_code,
                  note = excluded.note,
                  updated_at = excluded.updated_at
                """,
                (
                    info.symbol, info.name, info.short_name, info.market,
                    info.industry, info.isin, info.listed_date, info.capital,
                    info.shares_outstanding, info.cfi_code, info.note,
                    info.updated_at,
                ),
            )

    def get_or_fetch_stock_info(
        self,
        symbol: str,
        fetcher: Callable[[str], Optional[StockInfo]],
        *,
        max_age_days: Optional[int] = None,
    ) -> Optional[StockInfo]:
        """Cache-Aside 模式。

        1. 找 DB；命中且未過期就回傳
        2. 沒命中 → 呼叫 fetcher 抓 → 寫回 DB → 回傳
        """
        cached = self.get_stock_info(symbol)
        if cached and not _is_stale(cached.updated_at, max_age_days):
            return cached
        fetched = fetcher(symbol)
        if fetched is not None:
            self.upsert_stock_info(fetched)
        return fetched or cached

    # =================================================================
    # etf_meta DAO
    # =================================================================

    def get_etf_meta(self, symbol: str) -> Optional[EtfMeta]:
        row = self.conn.execute(
            "SELECT * FROM etf_meta WHERE symbol = ?", (symbol,)
        ).fetchone()
        return _row_to_dc(EtfMeta, row) if row else None

    def list_etf_meta(self) -> List[EtfMeta]:
        rows = self.conn.execute("SELECT * FROM etf_meta ORDER BY symbol").fetchall()
        return [_row_to_dc(EtfMeta, r) for r in rows]

    def upsert_etf_meta(self, meta: EtfMeta) -> None:
        if not meta.updated_at:
            meta.updated_at = now_tw().isoformat(timespec="seconds")
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO etf_meta
                  (symbol, name, manager, inception_date, holdings_url,
                   note, updated_at)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(symbol) DO UPDATE SET
                  name = excluded.name,
                  manager = excluded.manager,
                  inception_date = excluded.inception_date,
                  holdings_url = excluded.holdings_url,
                  note = excluded.note,
                  updated_at = excluded.updated_at
                """,
                (
                    meta.symbol, meta.name, meta.manager, meta.inception_date,
                    meta.holdings_url, meta.note, meta.updated_at,
                ),
            )

    # =================================================================
    # monthly_revenue DAO
    # =================================================================

    def get_monthly_revenue(
        self,
        symbol: str,
        year_month: Optional[str] = None,
    ) -> List[MonthlyRevenue]:
        if year_month:
            row = self.conn.execute(
                "SELECT * FROM monthly_revenue WHERE symbol = ? AND year_month = ?",
                (symbol, year_month),
            ).fetchone()
            return [_row_to_dc(MonthlyRevenue, row)] if row else []
        rows = self.conn.execute(
            "SELECT * FROM monthly_revenue WHERE symbol = ? "
            "ORDER BY year_month DESC",
            (symbol,),
        ).fetchall()
        return [_row_to_dc(MonthlyRevenue, r) for r in rows]

    def upsert_monthly_revenue(self, rev: MonthlyRevenue) -> None:
        if not rev.updated_at:
            rev.updated_at = now_tw().isoformat(timespec="seconds")
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO monthly_revenue
                  (symbol, year_month, revenue, yoy_pct, mom_pct,
                   cumulative, cum_yoy_pct, note, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol, year_month) DO UPDATE SET
                  revenue = excluded.revenue,
                  yoy_pct = excluded.yoy_pct,
                  mom_pct = excluded.mom_pct,
                  cumulative = excluded.cumulative,
                  cum_yoy_pct = excluded.cum_yoy_pct,
                  note = excluded.note,
                  updated_at = excluded.updated_at
                """,
                (
                    rev.symbol, rev.year_month, rev.revenue, rev.yoy_pct,
                    rev.mom_pct, rev.cumulative, rev.cum_yoy_pct, rev.note,
                    rev.updated_at,
                ),
            )

    # =================================================================
    # quarterly_report DAO
    # =================================================================

    def get_quarterly_report(
        self,
        symbol: str,
        period: Optional[str] = None,
    ) -> List[QuarterlyReport]:
        if period:
            row = self.conn.execute(
                "SELECT * FROM quarterly_report WHERE symbol = ? AND period = ?",
                (symbol, period),
            ).fetchone()
            return [_row_to_dc(QuarterlyReport, row)] if row else []
        rows = self.conn.execute(
            "SELECT * FROM quarterly_report WHERE symbol = ? ORDER BY period DESC",
            (symbol,),
        ).fetchall()
        return [_row_to_dc(QuarterlyReport, r) for r in rows]

    def upsert_quarterly_report(self, rep: QuarterlyReport) -> None:
        if not rep.updated_at:
            rep.updated_at = now_tw().isoformat(timespec="seconds")
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO quarterly_report
                  (symbol, period, eps, revenue, gross_margin, op_margin,
                   net_margin, note, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol, period) DO UPDATE SET
                  eps = excluded.eps,
                  revenue = excluded.revenue,
                  gross_margin = excluded.gross_margin,
                  op_margin = excluded.op_margin,
                  net_margin = excluded.net_margin,
                  note = excluded.note,
                  updated_at = excluded.updated_at
                """,
                (
                    rep.symbol, rep.period, rep.eps, rep.revenue,
                    rep.gross_margin, rep.op_margin, rep.net_margin,
                    rep.note, rep.updated_at,
                ),
            )

    # =================================================================
    # watchlist DAO (取代 watchlist.json，向後相容)
    # =================================================================

    def get_watch(self, symbol: str) -> Optional[WatchlistRow]:
        row = self.conn.execute(
            "SELECT * FROM watchlist WHERE symbol = ?", (symbol,)
        ).fetchone()
        return _row_to_dc(WatchlistRow, row) if row else None

    def list_watchlist(self) -> List[WatchlistRow]:
        rows = self.conn.execute(
            "SELECT * FROM watchlist ORDER BY added_at DESC, symbol"
        ).fetchall()
        return [_row_to_dc(WatchlistRow, r) for r in rows]

    def upsert_watch(self, item: WatchlistRow) -> None:
        now_iso = now_tw().isoformat(timespec="seconds")
        if not item.added_at:
            item.added_at = now_iso
        item.updated_at = now_iso
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO watchlist
                  (symbol, name, tags, note, added_at, updated_at)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(symbol) DO UPDATE SET
                  name = CASE WHEN excluded.name != '' THEN excluded.name ELSE watchlist.name END,
                  tags = excluded.tags,
                  note = excluded.note,
                  updated_at = excluded.updated_at
                """,
                (
                    item.symbol, item.name, item.tags, item.note,
                    item.added_at, item.updated_at,
                ),
            )

    def remove_watch(self, symbol: str) -> None:
        with self.transaction() as c:
            c.execute("DELETE FROM watchlist WHERE symbol = ?", (symbol,))

    # =================================================================
    # price_history DAO  (歷史 K 線 OHLCV)
    # =================================================================

    def upsert_price_bar(self, bar: PriceBar) -> None:
        """寫入單一根 K 線。"""
        if not bar.updated_at:
            bar.updated_at = now_tw().isoformat(timespec="seconds")
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO price_history
                  (symbol, date, open, high, low, close, volume,
                   source, note, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol, date) DO UPDATE SET
                  open = excluded.open,
                  high = excluded.high,
                  low = excluded.low,
                  close = excluded.close,
                  volume = excluded.volume,
                  source = excluded.source,
                  note = excluded.note,
                  updated_at = excluded.updated_at
                """,
                (
                    bar.symbol, bar.date, bar.open, bar.high, bar.low,
                    bar.close, bar.volume, bar.source, bar.note,
                    bar.updated_at,
                ),
            )

    def bulk_upsert_price_bars(self, bars: Iterable[PriceBar]) -> int:
        """批次寫入 K 線；同一 transaction 內執行以加速。回傳寫入筆數。"""
        ts_default = now_tw().isoformat(timespec="seconds")
        rows: List[Tuple[Any, ...]] = []
        for b in bars:
            rows.append((
                b.symbol, b.date, b.open, b.high, b.low, b.close, b.volume,
                b.source or "twse", b.note,
                b.updated_at or ts_default,
            ))
        if not rows:
            return 0
        with self.transaction() as c:
            c.executemany(
                """
                INSERT INTO price_history
                  (symbol, date, open, high, low, close, volume,
                   source, note, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol, date) DO UPDATE SET
                  open = excluded.open,
                  high = excluded.high,
                  low = excluded.low,
                  close = excluded.close,
                  volume = excluded.volume,
                  source = excluded.source,
                  note = excluded.note,
                  updated_at = excluded.updated_at
                """,
                rows,
            )
        return len(rows)

    def get_price_history(
        self,
        symbol: str,
        *,
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: Optional[int] = None,
        ascending: bool = True,
    ) -> List[PriceBar]:
        """讀取某檔的 K 線 (依日期區間)。

        - `start` / `end`：ISO 字串 (含)，留空則不限制。
        - `limit`：最多回傳筆數 (取最新 N 筆時用)。
        - `ascending=True`：依日期由舊至新；繪圖時較直觀。
        """
        sql = "SELECT * FROM price_history WHERE symbol = ?"
        args: List[Any] = [symbol]
        if start:
            sql += " AND date >= ?"
            args.append(start)
        if end:
            sql += " AND date <= ?"
            args.append(end)
        order = "ASC" if ascending else "DESC"
        sql += f" ORDER BY date {order}"
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        rows = self.conn.execute(sql, args).fetchall()
        bars = [_row_to_dc(PriceBar, r) for r in rows]
        # 若指定 limit 但要 ascending，先抓最新再反轉
        if limit and ascending and not (start or end):
            # 先用 DESC + LIMIT 抓最新 N 筆，再翻轉回 ASC
            sql2 = (
                "SELECT * FROM price_history WHERE symbol = ? "
                "ORDER BY date DESC LIMIT ?"
            )
            rows = self.conn.execute(sql2, (symbol, int(limit))).fetchall()
            bars = [_row_to_dc(PriceBar, r) for r in rows]
            bars.reverse()
        return bars

    def latest_price_date(self, symbol: str) -> Optional[str]:
        """回傳該檔 DB 中最新的日期 (ISO 字串)；查無回傳 None。"""
        row = self.conn.execute(
            "SELECT MAX(date) AS d FROM price_history WHERE symbol = ?",
            (symbol,),
        ).fetchone()
        return (row["d"] if row and row["d"] else None)

    def list_price_symbols(self) -> List[str]:
        """列出所有有 K 線資料的 symbol。"""
        rows = self.conn.execute(
            "SELECT symbol, COUNT(*) AS n FROM price_history "
            "GROUP BY symbol ORDER BY symbol"
        ).fetchall()
        return [r["symbol"] for r in rows]

    def price_history_summary(self) -> List[Dict[str, Any]]:
        """每檔 symbol 的 K 線筆數 + 最新日期 + 最新收盤。"""
        sql = """
            SELECT symbol,
                   COUNT(*) AS rows,
                   MIN(date) AS first_date,
                   MAX(date) AS last_date
            FROM price_history
            GROUP BY symbol
            ORDER BY symbol
        """
        rows = self.conn.execute(sql).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            sym = r["symbol"]
            last_row = self.conn.execute(
                "SELECT close FROM price_history "
                "WHERE symbol = ? AND date = ?",
                (sym, r["last_date"]),
            ).fetchone()
            out.append({
                "symbol": sym,
                "rows": int(r["rows"]),
                "first_date": r["first_date"],
                "last_date": r["last_date"],
                "last_close": float(last_row["close"]) if last_row else 0.0,
            })
        return out

    # =================================================================
    # llm_analysis_history DAO
    # =================================================================

    def upsert_llm_analysis(self, row: LlmAnalysisRow) -> None:
        """寫入單筆 LLM 分析歷史。"""
        if not row.updated_at:
            row.updated_at = now_tw().isoformat(timespec="seconds")
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO llm_analysis_history
                  (id, ts, ticker, prompt_id, prompt_version, model,
                   sentiment, sentiment_score, confidence, summary,
                   drivers, risks, raw_output, latency_ms, tokens_in,
                   tokens_out, success, error, source, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                  ts = excluded.ts,
                  ticker = excluded.ticker,
                  prompt_id = excluded.prompt_id,
                  prompt_version = excluded.prompt_version,
                  model = excluded.model,
                  sentiment = excluded.sentiment,
                  sentiment_score = excluded.sentiment_score,
                  confidence = excluded.confidence,
                  summary = excluded.summary,
                  drivers = excluded.drivers,
                  risks = excluded.risks,
                  raw_output = excluded.raw_output,
                  latency_ms = excluded.latency_ms,
                  tokens_in = excluded.tokens_in,
                  tokens_out = excluded.tokens_out,
                  success = excluded.success,
                  error = excluded.error,
                  source = excluded.source,
                  updated_at = excluded.updated_at
                """,
                (
                    row.id, row.ts, row.ticker, row.prompt_id,
                    row.prompt_version, row.model, row.sentiment,
                    row.sentiment_score, row.confidence, row.summary,
                    row.drivers, row.risks, row.raw_output,
                    int(row.latency_ms), int(row.tokens_in),
                    int(row.tokens_out), int(row.success), row.error,
                    row.source, row.updated_at,
                ),
            )

    def list_llm_analysis(
        self,
        *,
        ticker: Optional[str] = None,
        prompt_id: Optional[str] = None,
        limit: int = 200,
    ) -> List[LlmAnalysisRow]:
        sql = "SELECT * FROM llm_analysis_history WHERE 1=1"
        args: List[Any] = []
        if ticker:
            sql += " AND ticker = ?"
            args.append(ticker)
        if prompt_id:
            sql += " AND prompt_id = ?"
            args.append(prompt_id)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(int(limit))
        rows = self.conn.execute(sql, args).fetchall()
        return [_row_to_dc(LlmAnalysisRow, r) for r in rows]

    # =================================================================
    # llm_daily_reports DAO
    # =================================================================

    def upsert_llm_daily_report(self, row: LlmDailyReportRow) -> None:
        """寫入每日 LLM 報告。"""
        now_iso = now_tw().isoformat(timespec="seconds")
        if not row.generated_at:
            row.generated_at = now_iso
        if not row.updated_at:
            row.updated_at = now_iso
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO llm_daily_reports
                  (report_type, report_date, mode, asof, generated_at,
                   market_tone, prompt_id, prompt_version, brief_md,
                   payload_json, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(report_type, report_date, mode) DO UPDATE SET
                  asof = excluded.asof,
                  generated_at = excluded.generated_at,
                  market_tone = excluded.market_tone,
                  prompt_id = excluded.prompt_id,
                  prompt_version = excluded.prompt_version,
                  brief_md = excluded.brief_md,
                  payload_json = excluded.payload_json,
                  updated_at = excluded.updated_at
                """,
                (
                    row.report_type, row.report_date, row.mode, row.asof,
                    row.generated_at, row.market_tone, row.prompt_id,
                    row.prompt_version, row.brief_md, row.payload_json,
                    row.updated_at,
                ),
            )

    def get_llm_daily_report(
        self,
        report_type: str,
        report_date: str,
        *,
        mode: str = "",
    ) -> Optional[LlmDailyReportRow]:
        row = self.conn.execute(
            """
            SELECT * FROM llm_daily_reports
            WHERE report_type = ? AND report_date = ? AND mode = ?
            """,
            (report_type, report_date, mode),
        ).fetchone()
        return _row_to_dc(LlmDailyReportRow, row) if row else None

    def get_latest_llm_daily_report(
        self,
        report_type: str,
        *,
        mode: Optional[str] = None,
    ) -> Optional[LlmDailyReportRow]:
        sql = "SELECT * FROM llm_daily_reports WHERE report_type = ?"
        args: List[Any] = [report_type]
        if mode is not None:
            sql += " AND mode = ?"
            args.append(mode)
        sql += " ORDER BY report_date DESC, generated_at DESC LIMIT 1"
        row = self.conn.execute(sql, args).fetchone()
        return _row_to_dc(LlmDailyReportRow, row) if row else None

    def list_llm_daily_reports(
        self,
        *,
        report_type: Optional[str] = None,
        mode: Optional[str] = None,
        limit: int = 200,
    ) -> List[LlmDailyReportRow]:
        sql = "SELECT * FROM llm_daily_reports WHERE 1=1"
        args: List[Any] = []
        if report_type:
            sql += " AND report_type = ?"
            args.append(report_type)
        if mode is not None:
            sql += " AND mode = ?"
            args.append(mode)
        sql += " ORDER BY report_date DESC, generated_at DESC LIMIT ?"
        args.append(int(limit))
        rows = self.conn.execute(sql, args).fetchall()
        return [_row_to_dc(LlmDailyReportRow, r) for r in rows]

    # =================================================================
    # sync_meta DAO
    # =================================================================

    def get_sync_meta(self, table: str) -> SyncMeta:
        row = self.conn.execute(
            "SELECT * FROM sync_meta WHERE table_name = ?", (table,)
        ).fetchone()
        if row is None:
            return SyncMeta(table_name=table)
        return _row_to_dc(SyncMeta, row)

    def list_sync_meta(self) -> List[SyncMeta]:
        rows = self.conn.execute("SELECT * FROM sync_meta ORDER BY table_name").fetchall()
        return [_row_to_dc(SyncMeta, r) for r in rows]

    def mark_pushed(self, table: str, rows: int) -> None:
        ts = now_tw().isoformat(timespec="seconds")
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO sync_meta (table_name, last_push_at, last_synced_rows, last_error)
                VALUES (?, ?, ?, '')
                ON CONFLICT(table_name) DO UPDATE SET
                  last_push_at = excluded.last_push_at,
                  last_synced_rows = excluded.last_synced_rows,
                  last_error = ''
                """,
                (table, ts, rows),
            )

    def mark_pulled(self, table: str, rows: int) -> None:
        ts = now_tw().isoformat(timespec="seconds")
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO sync_meta (table_name, last_pull_at, last_synced_rows, last_error)
                VALUES (?, ?, ?, '')
                ON CONFLICT(table_name) DO UPDATE SET
                  last_pull_at = excluded.last_pull_at,
                  last_synced_rows = excluded.last_synced_rows,
                  last_error = ''
                """,
                (table, ts, rows),
            )

    def mark_sync_error(self, table: str, err: str) -> None:
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO sync_meta (table_name, last_error)
                VALUES (?, ?)
                ON CONFLICT(table_name) DO UPDATE SET
                  last_error = excluded.last_error
                """,
                (table, err[:500]),
            )


# ----------------------------------------------------------------------
# 模組層 helpers
# ----------------------------------------------------------------------


_singleton_lock = threading.Lock()
_singleton: Optional[StockDB] = None


def get_db(path: Optional[Path] = None, root: Optional[Path] = None) -> StockDB:
    """模組層單例 (給 dashboard / cli 共用)。"""
    global _singleton
    with _singleton_lock:
        if _singleton is None or (path is not None and Path(path) != _singleton.path):
            _singleton = StockDB.open(path=path, root=root)
        return _singleton


def reset_db_singleton() -> None:
    """測試用 — 強制重新建立 singleton。"""
    global _singleton
    with _singleton_lock:
        if _singleton is not None:
            _singleton.close()
        _singleton = None


# ----------------------------------------------------------------------
# 內部 utility
# ----------------------------------------------------------------------


def _row_to_dc(cls, row: sqlite3.Row):
    """sqlite3.Row → dataclass，缺欄位 fallback 到 dataclass 預設。"""
    field_names = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    kwargs = {k: row[k] for k in row.keys() if k in field_names}
    return cls(**kwargs)


def _default_for(table: str, col: str) -> Any:
    """同步寫入時找不到欄位的 fallback (對齊 schema 預設值)。"""
    # 簡單規則：數值欄位給 0，其餘給空字串
    numeric_cols = {
        "capital", "shares_outstanding",
        "revenue", "yoy_pct", "mom_pct", "cumulative", "cum_yoy_pct",
        "eps", "gross_margin", "op_margin", "net_margin",
        "open", "high", "low", "close", "volume",
        "last_synced_rows",
    }
    return 0 if col in numeric_cols else ""


def _is_stale(updated_at: str, max_age_days: Optional[int]) -> bool:
    if max_age_days is None:
        return False
    if not updated_at:
        return True
    try:
        ts = dt.datetime.fromisoformat(updated_at)
    except ValueError:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=now_tw().tzinfo)
    return (now_tw() - ts) > dt.timedelta(days=max_age_days)


__all__ = [
    "ALL_TABLES",
    "SYNCABLE_TABLES",
    "EtfMeta",
    "LlmAnalysisRow",
    "LlmDailyReportRow",
    "MonthlyRevenue",
    "PriceBar",
    "QuarterlyReport",
    "StockDB",
    "StockInfo",
    "SyncMeta",
    "WatchlistRow",
    "default_db_path",
    "get_db",
    "reset_db_singleton",
]
