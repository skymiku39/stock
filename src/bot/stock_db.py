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
import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

from bot.data_quality import normalize_quality_status, validate_ohlcv
from bot.utils import get_logger, now_tw


# SQLite allows many readers but only one writer.  A single process can still
# create several StockDB objects (for example, !ask's parallel resource
# adapters each opening a short-lived SourceRegistry).  Serialise write
# transactions for the same database path in-process so normal application
# concurrency does not turn into avoidable ``database is locked`` failures.
# ``RLock`` is intentional: schema initialisation and helper methods may share
# the same transaction boundary in one thread.
_DB_PATH_LOCKS: Dict[str, threading.RLock] = {}
_DB_PATH_LOCKS_GUARD = threading.Lock()


def _db_path_lock(path: Path) -> threading.RLock:
    key = str(path.resolve()).casefold()
    with _DB_PATH_LOCKS_GUARD:
        lock = _DB_PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _DB_PATH_LOCKS[key] = lock
        return lock


@contextmanager
def _schema_process_lock(path: Path, *, timeout: float = 30.0) -> Iterator[None]:
    """Serialize schema initialization across processes.

    The in-process ``RLock`` above cannot protect two CLI processes started at
    the same time.  Each process otherwise runs the compatibility DDL and
    migration bookkeeping during ``StockDB.open()``, which can make a parallel
    ``!ask`` startup fail even though SQLite WAL is enabled.  A tiny sidecar
    lock file provides only startup serialization; normal reads/writes still
    use SQLite's WAL and busy timeout.
    """
    lock_path = path.with_name(f"{path.name}.schema.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0.1, float(timeout))
    with lock_path.open("a+b") as handle:
        # Windows locking operates on a byte range and requires the file to
        # contain at least one byte.  The byte is only a lock carrier; the
        # database remains the single source of truth.
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)

        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise sqlite3.OperationalError(
                            f"schema lock timeout: {lock_path}"
                        ) from exc
                    time.sleep(0.05)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise sqlite3.OperationalError(
                            f"schema lock timeout: {lock_path}"
                        ) from exc
                    time.sleep(0.05)

        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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
    source_url: str = ""
    source_date: str = ""        # 來源資料日期，不等於本機抓取時間
    retrieved_at: str = ""
    quality_status: str = "unknown"
    source_record_id: str = ""


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
    source_url: str = ""
    source_date: str = ""
    retrieved_at: str = ""
    quality_status: str = "unknown"
    source_record_id: str = ""


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
    source_url: str = ""
    source_date: str = ""
    retrieved_at: str = ""
    quality_status: str = "unknown"
    source_record_id: str = ""


@dataclass
class ValuationDaily:
    """每日 PER / PBR / 殖利率快照。"""

    symbol: str
    as_of: str
    pe_ratio: float = 0.0
    pb_ratio: float = 0.0
    dividend_yield: float = 0.0
    dividend_year: str = ""
    note: str = ""
    updated_at: str = ""
    source_url: str = ""
    source_date: str = ""
    retrieved_at: str = ""
    quality_status: str = "unknown"
    source_record_id: str = ""


@dataclass
class DividendHistory:
    """年度股利分派彙總。"""

    symbol: str
    year: int
    cash_dividend: float = 0.0
    stock_dividend: float = 0.0
    ex_dividend_date: str = ""
    ex_right_date: str = ""
    fill_date: str = ""
    fill_days: Optional[int] = None
    payout_ratio: Optional[float] = None
    note: str = ""
    updated_at: str = ""
    source_url: str = ""
    source_date: str = ""
    retrieved_at: str = ""
    quality_status: str = "unknown"
    source_record_id: str = ""


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
    source_url: str = ""
    source_date: str = ""
    retrieved_at: str = ""
    quality_status: str = "unknown"
    source_record_id: str = ""


@dataclass
class WatchlistRow:
    """使用者自訂監控清單列（SQLite SSOT；JSON 見 bot.watchlist）。"""

    symbol: str
    name: str = ""
    tags: str = ""               # 逗號分隔 ("半導體,共識持股")
    note: str = ""
    added_at: str = ""
    updated_at: str = ""
    revision: str = ""
    deleted_at: str = ""
    restored_from: str = ""


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
    source_url: str = ""
    source_date: str = ""
    retrieved_at: str = ""
    quality_status: str = "unknown"
    source_record_id: str = ""


@dataclass
class IntradayBar:
    """盤中分 K 或 Tick 歷史 (Shioaji kbars/ticks)。

    PK = (symbol, ts, interval)。
    ts 為台北時間 ``YYYY-MM-DD HH:MM:SS``。
    interval: ``1m`` (分 K) 或 ``tick`` (逐筆)。
    volume 單位為股數 (Shioaji 原始欄位)。
    """

    symbol: str
    ts: str
    interval: str = "1m"
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0
    amount: float = 0.0
    source: str = "shioaji"
    updated_at: str = ""
    source_url: str = ""
    source_date: str = ""
    retrieved_at: str = ""
    quality_status: str = "unknown"
    source_record_id: str = ""


@dataclass
class SyncMeta:
    """每張 table 的同步狀態紀錄。"""

    table_name: str
    last_push_at: str = ""
    last_pull_at: str = ""
    last_synced_rows: int = 0
    last_error: str = ""


@dataclass
class SyncConflictRow:
    """兩端 row-level 同步衝突的本地稽核紀錄。"""

    conflict_id: str
    table_name: str
    row_key_json: str = "[]"
    local_row_json: str = "{}"
    remote_row_json: str = "{}"
    decision: str = "local"
    local_updated_at: str = ""
    remote_updated_at: str = ""
    status: str = "open"
    created_at: str = ""
    resolved_at: str = ""
    resolution: str = ""


@dataclass
class SourceRun:
    """一次資料來源 ingestion 的品質與溯源紀錄。"""

    run_id: str
    source: str
    source_url: str = ""
    status: str = "failed"       # running / ok / partial / invalid / failed / stale
    started_at: str = ""
    completed_at: str = ""
    source_date: str = ""
    row_count: int = 0
    raw_hash: str = ""
    schema_hash: str = ""
    error: str = ""
    details_json: str = "{}"


@dataclass
class SourceDocumentRow:
    """不可變原始來源文件的 manifest，不把大型內容直接塞進 SQLite。"""

    document_id: str
    source: str
    kind: str = "raw"
    source_ref: str = ""
    url: str = ""
    title: str = ""
    content_path: str = ""
    content_hash: str = ""
    mime_type: str = ""
    as_of: str = ""
    published_at: str = ""
    retrieved_at: str = ""
    valid_from: str = ""
    valid_to: str = ""
    ingestion_run_id: str = ""
    quality_status: str = "unknown"
    metadata_json: str = "{}"
    created_at: str = ""
    updated_at: str = ""


@dataclass
class IngestionRunRow:
    """一次來源取得／解析流程的可追蹤狀態。"""

    run_id: str
    source: str
    source_run_id: str = ""
    status: str = "running"  # running / ok / partial / invalid / failed
    started_at: str = ""
    completed_at: str = ""
    document_count: int = 0
    error: str = ""
    metadata_json: str = "{}"


@dataclass
class SourceHealthRow:
    """某個來源最近一次執行的摘要；詳細歷史仍保留在 ``source_runs``。"""

    source: str
    last_run_id: str = ""
    last_status: str = "unknown"
    checked_at: str = ""
    last_started_at: str = ""
    last_completed_at: str = ""
    last_source_date: str = ""
    last_row_count: int = 0
    last_raw_hash: str = ""
    last_schema_hash: str = ""
    consecutive_failures: int = 0
    last_error: str = ""
    updated_at: str = ""


@dataclass
class SchemaMigrationRow:
    """已套用的本地 schema 版本。"""

    version: int
    name: str
    checksum: str = ""
    applied_at: str = ""


@dataclass
class EntityClassification:
    """可版本化的多維分類，不覆蓋官方產業欄位。"""

    id: str
    entity_type: str              # stock / etf / theme / event
    entity_id: str
    taxonomy: str                 # official_industry / investment_theme / ...
    label: str
    confidence: float = 0.0
    source: str = ""
    source_ref: str = ""
    valid_from: str = ""
    valid_to: str = ""
    status: str = "active"       # active / proposed / rejected / expired
    created_at: str = ""
    updated_at: str = ""


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
    evidence_json: str = "[]"
    quality_status: str = "unknown"
    source_record_id: str = ""


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
    evidence_json: str = "[]"
    quality_status: str = "unknown"
    source_record_id: str = ""


@dataclass
class MemoryItemRow:
    """可追溯的短期／長期記憶項目。

    ``content`` 永遠保留原始記憶內容；``summary`` 只是一個可重建的檢索摘要，
    不能取代原文。短期記憶到期後只會轉為 archived，不做不可逆刪除。
    """

    memory_id: str
    memory_layer: str = "short"       # short / long
    memory_type: str = "context"      # context / fact / preference / decision / summary
    scope_type: str = "global"        # global / ticker / user / channel / session
    scope_id: str = ""
    subject: str = ""
    content: str = ""
    summary: str = ""
    keywords: str = ""
    importance: float = 0.5
    confidence: float = 0.0
    quality_status: str = "unknown"
    memory_status: str = "active"     # active / archived / superseded / deleted
    created_at: str = ""
    observed_at: str = ""
    expires_at: str = ""
    last_accessed_at: str = ""
    access_count: int = 0
    source: str = ""
    source_ref: str = ""
    source_record_id: str = ""
    evidence_json: str = "[]"
    metadata_json: str = "{}"
    parent_id: str = ""
    revision: int = 1
    updated_at: str = ""


@dataclass
class MemoryEventRow:
    """記憶生命週期事件；用於回放、稽核與日後重建索引。"""

    event_id: str
    memory_id: str
    event_type: str                 # created / updated / accessed / promoted / archived
    actor: str = "system"
    trace_id: str = ""
    payload_json: str = "{}"
    created_at: str = ""


@dataclass
class EvidenceItemRow:
    """中央證據索引；原始檔案／API response 仍由各來源保存。"""

    evidence_id: str
    kind: str = "unknown"
    source: str = ""
    source_ref: str = ""
    title: str = ""
    url: str = ""
    observed_at: str = ""
    as_of: str = ""
    published_at: str = ""
    retrieved_at: str = ""
    valid_from: str = ""
    valid_to: str = ""
    excerpt: str = ""
    quality_status: str = "unknown"
    content_hash: str = ""
    metadata_json: str = "{}"
    created_at: str = ""
    updated_at: str = ""
    source_document_id: str = ""


@dataclass
class ResearchDocumentRow:
    """可由原始文件重建的研究文件；摘要不能取代 canonical_text。"""

    document_id: str
    document_type: str = "research"
    symbol: str = ""
    title: str = ""
    published_at: str = ""
    source_document_id: str = ""
    canonical_text: str = ""
    status: str = "active"       # active / superseded / unresolved / archived
    metadata_json: str = "{}"
    created_at: str = ""
    updated_at: str = ""


@dataclass
class ResearchChunkRow:
    """研究文件的可重建檢索片段；embedding 僅是可選的快取。"""

    chunk_id: str
    document_id: str
    ordinal: int = 0
    text: str = ""
    token_count: int = 0
    embedding_model: str = ""
    embedding_json: str = ""
    created_at: str = ""


@dataclass
class EvidenceLinkRow:
    """證據對原始文件、資料表或明確 row key 的關聯。"""

    link_id: str
    evidence_id: str
    subject_type: str = ""
    subject_id: str = ""
    source_document_id: str = ""
    table_name: str = ""
    row_key: str = ""
    quote_or_summary: str = ""
    as_of: str = ""
    created_at: str = ""


@dataclass
class LlmRunRow:
    """一次可回放的 LLM 執行，與舊 JSONL/分析表並存。"""

    run_id: str
    task_type: str = ""
    model: str = ""
    prompt_id: str = ""
    prompt_version: str = ""
    started_at: str = ""
    finished_at: str = ""
    status: str = "running"
    input_hash: str = ""
    output_hash: str = ""
    error: str = ""
    trace_id: str = ""
    metadata_json: str = "{}"


@dataclass
class LlmRunEvidenceRow:
    run_id: str
    evidence_id: str
    ordinal: int = 0
    role: str = "context"


@dataclass
class LlmOutputRow:
    output_id: str
    run_id: str
    output_format: str = "text"
    structured_json: str = "{}"
    markdown: str = ""
    confidence: float = 0.0
    limitation: str = ""
    is_publishable: int = 0
    created_at: str = ""


@dataclass
class ClaimRow:
    claim_id: str
    run_id: str = ""
    text: str = ""
    claim_type: str = "fact"
    confidence: float = 0.0
    status: str = "unverified"  # verified / conflicted / unverified / rejected
    uncertainty: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass
class ClaimEvidenceRow:
    claim_id: str
    evidence_id: str
    relation: str = "supports"  # supports / contradicts / qualifies
    ordinal: int = 0


@dataclass
class MemoryProposalRow:
    """需經人工或政策核准才可進入長期記憶的提案。"""

    proposal_id: str
    proposal_type: str = "create"   # create / update / consolidate / delete
    target_memory_id: str = ""
    memory_layer: str = "long"
    memory_type: str = "summary"
    scope_type: str = "global"
    scope_id: str = ""
    subject: str = ""
    content: str = ""
    summary: str = ""
    keywords: str = ""
    importance: float = 0.5
    confidence: float = 0.0
    quality_status: str = "unknown"
    source: str = ""
    source_ref: str = ""
    evidence_json: str = "[]"
    metadata_json: str = "{}"
    status: str = "pending"      # pending / approved / rejected / superseded
    proposer: str = "system"
    reviewer: str = ""
    review_note: str = ""
    created_at: str = ""
    reviewed_at: str = ""
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
            updated_at          TEXT NOT NULL DEFAULT '',
            source_url          TEXT NOT NULL DEFAULT '',
            source_date         TEXT NOT NULL DEFAULT '',
            retrieved_at        TEXT NOT NULL DEFAULT '',
            quality_status      TEXT NOT NULL DEFAULT 'unknown',
            source_record_id    TEXT NOT NULL DEFAULT ''
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
            updated_at      TEXT NOT NULL DEFAULT '',
            source_url      TEXT NOT NULL DEFAULT '',
            source_date     TEXT NOT NULL DEFAULT '',
            retrieved_at    TEXT NOT NULL DEFAULT '',
            quality_status  TEXT NOT NULL DEFAULT 'unknown',
            source_record_id TEXT NOT NULL DEFAULT ''
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
            source_url     TEXT NOT NULL DEFAULT '',
            source_date    TEXT NOT NULL DEFAULT '',
            retrieved_at   TEXT NOT NULL DEFAULT '',
            quality_status TEXT NOT NULL DEFAULT 'unknown',
            source_record_id TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (symbol, year_month)
        )
    """,
    "valuation_daily": """
        CREATE TABLE IF NOT EXISTS valuation_daily (
            symbol           TEXT NOT NULL,
            as_of            TEXT NOT NULL,
            pe_ratio         REAL NOT NULL DEFAULT 0,
            pb_ratio         REAL NOT NULL DEFAULT 0,
            dividend_yield   REAL NOT NULL DEFAULT 0,
            dividend_year    TEXT NOT NULL DEFAULT '',
            note             TEXT NOT NULL DEFAULT '',
            updated_at       TEXT NOT NULL DEFAULT '',
            source_url       TEXT NOT NULL DEFAULT '',
            source_date      TEXT NOT NULL DEFAULT '',
            retrieved_at     TEXT NOT NULL DEFAULT '',
            quality_status   TEXT NOT NULL DEFAULT 'unknown',
            source_record_id TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (symbol, as_of)
        )
    """,
    "dividend_history": """
        CREATE TABLE IF NOT EXISTS dividend_history (
            symbol           TEXT NOT NULL,
            year             INTEGER NOT NULL,
            cash_dividend    REAL NOT NULL DEFAULT 0,
            stock_dividend   REAL NOT NULL DEFAULT 0,
            ex_dividend_date TEXT NOT NULL DEFAULT '',
            ex_right_date    TEXT NOT NULL DEFAULT '',
            fill_date        TEXT NOT NULL DEFAULT '',
            fill_days        INTEGER,
            payout_ratio     REAL,
            note             TEXT NOT NULL DEFAULT '',
            updated_at       TEXT NOT NULL DEFAULT '',
            source_url       TEXT NOT NULL DEFAULT '',
            source_date      TEXT NOT NULL DEFAULT '',
            retrieved_at     TEXT NOT NULL DEFAULT '',
            quality_status   TEXT NOT NULL DEFAULT 'unknown',
            source_record_id TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (symbol, year)
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
            source_url    TEXT NOT NULL DEFAULT '',
            source_date   TEXT NOT NULL DEFAULT '',
            retrieved_at  TEXT NOT NULL DEFAULT '',
            quality_status TEXT NOT NULL DEFAULT 'unknown',
            source_record_id TEXT NOT NULL DEFAULT '',
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
            source_url  TEXT NOT NULL DEFAULT '',
            source_date TEXT NOT NULL DEFAULT '',
            retrieved_at TEXT NOT NULL DEFAULT '',
            quality_status TEXT NOT NULL DEFAULT 'unknown',
            source_record_id TEXT NOT NULL DEFAULT '',
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
    "source_runs": """
        CREATE TABLE IF NOT EXISTS source_runs (
            run_id          TEXT PRIMARY KEY,
            source          TEXT NOT NULL,
            source_url      TEXT NOT NULL DEFAULT '',
            status          TEXT NOT NULL DEFAULT 'failed',
            started_at      TEXT NOT NULL DEFAULT '',
            completed_at    TEXT NOT NULL DEFAULT '',
            source_date     TEXT NOT NULL DEFAULT '',
            row_count       INTEGER NOT NULL DEFAULT 0,
            raw_hash        TEXT NOT NULL DEFAULT '',
            schema_hash     TEXT NOT NULL DEFAULT '',
            error           TEXT NOT NULL DEFAULT '',
            details_json    TEXT NOT NULL DEFAULT '{}'
        )
    """,
    "entity_classifications": """
        CREATE TABLE IF NOT EXISTS entity_classifications (
            id              TEXT PRIMARY KEY,
            entity_type     TEXT NOT NULL,
            entity_id       TEXT NOT NULL,
            taxonomy        TEXT NOT NULL,
            label           TEXT NOT NULL,
            confidence      REAL NOT NULL DEFAULT 0,
            source          TEXT NOT NULL DEFAULT '',
            source_ref      TEXT NOT NULL DEFAULT '',
            valid_from      TEXT NOT NULL DEFAULT '',
            valid_to        TEXT NOT NULL DEFAULT '',
            status          TEXT NOT NULL DEFAULT 'active',
            created_at      TEXT NOT NULL DEFAULT '',
            updated_at      TEXT NOT NULL DEFAULT ''
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
            updated_at         TEXT NOT NULL DEFAULT '',
            evidence_json      TEXT NOT NULL DEFAULT '[]',
            quality_status     TEXT NOT NULL DEFAULT 'unknown',
            source_record_id   TEXT NOT NULL DEFAULT ''
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
            evidence_json      TEXT NOT NULL DEFAULT '[]',
            quality_status     TEXT NOT NULL DEFAULT 'unknown',
            source_record_id   TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (report_type, report_date, mode)
        )
    """,
    "intraday_bars": """
        CREATE TABLE IF NOT EXISTS intraday_bars (
            symbol      TEXT NOT NULL,
            ts          TEXT NOT NULL,
            interval    TEXT NOT NULL DEFAULT '1m',
            open        REAL NOT NULL DEFAULT 0,
            high        REAL NOT NULL DEFAULT 0,
            low         REAL NOT NULL DEFAULT 0,
            close       REAL NOT NULL DEFAULT 0,
            volume      REAL NOT NULL DEFAULT 0,
            amount      REAL NOT NULL DEFAULT 0,
            source      TEXT NOT NULL DEFAULT 'shioaji',
            updated_at  TEXT NOT NULL DEFAULT '',
            source_url  TEXT NOT NULL DEFAULT '',
            source_date TEXT NOT NULL DEFAULT '',
            retrieved_at TEXT NOT NULL DEFAULT '',
            quality_status TEXT NOT NULL DEFAULT 'unknown',
            source_record_id TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (symbol, ts, interval)
        )
    """,
    "memory_items": """
        CREATE TABLE IF NOT EXISTS memory_items (
            memory_id          TEXT PRIMARY KEY,
            memory_layer       TEXT NOT NULL DEFAULT 'short',
            memory_type        TEXT NOT NULL DEFAULT 'context',
            scope_type         TEXT NOT NULL DEFAULT 'global',
            scope_id           TEXT NOT NULL DEFAULT '',
            subject            TEXT NOT NULL DEFAULT '',
            content            TEXT NOT NULL DEFAULT '',
            summary            TEXT NOT NULL DEFAULT '',
            keywords           TEXT NOT NULL DEFAULT '',
            importance         REAL NOT NULL DEFAULT 0.5,
            confidence         REAL NOT NULL DEFAULT 0.0,
            quality_status     TEXT NOT NULL DEFAULT 'unknown',
            memory_status      TEXT NOT NULL DEFAULT 'active',
            created_at         TEXT NOT NULL DEFAULT '',
            observed_at        TEXT NOT NULL DEFAULT '',
            expires_at         TEXT NOT NULL DEFAULT '',
            last_accessed_at   TEXT NOT NULL DEFAULT '',
            access_count       INTEGER NOT NULL DEFAULT 0,
            source             TEXT NOT NULL DEFAULT '',
            source_ref         TEXT NOT NULL DEFAULT '',
            source_record_id   TEXT NOT NULL DEFAULT '',
            evidence_json      TEXT NOT NULL DEFAULT '[]',
            metadata_json      TEXT NOT NULL DEFAULT '{}',
            parent_id          TEXT NOT NULL DEFAULT '',
            revision           INTEGER NOT NULL DEFAULT 1,
            updated_at         TEXT NOT NULL DEFAULT ''
        )
    """,
    "memory_events": """
        CREATE TABLE IF NOT EXISTS memory_events (
            event_id           TEXT PRIMARY KEY,
            memory_id          TEXT NOT NULL,
            event_type         TEXT NOT NULL,
            actor              TEXT NOT NULL DEFAULT 'system',
            trace_id           TEXT NOT NULL DEFAULT '',
            payload_json       TEXT NOT NULL DEFAULT '{}',
            created_at         TEXT NOT NULL DEFAULT ''
        )
    """,
    "evidence_items": """
        CREATE TABLE IF NOT EXISTS evidence_items (
            evidence_id        TEXT PRIMARY KEY,
            source_document_id TEXT NOT NULL DEFAULT '',
            kind               TEXT NOT NULL DEFAULT 'unknown',
            source             TEXT NOT NULL DEFAULT '',
            source_ref         TEXT NOT NULL DEFAULT '',
            title              TEXT NOT NULL DEFAULT '',
            url                TEXT NOT NULL DEFAULT '',
            observed_at        TEXT NOT NULL DEFAULT '',
            as_of              TEXT NOT NULL DEFAULT '',
            published_at       TEXT NOT NULL DEFAULT '',
            retrieved_at       TEXT NOT NULL DEFAULT '',
            valid_from         TEXT NOT NULL DEFAULT '',
            valid_to           TEXT NOT NULL DEFAULT '',
            excerpt            TEXT NOT NULL DEFAULT '',
            quality_status     TEXT NOT NULL DEFAULT 'unknown',
            content_hash       TEXT NOT NULL DEFAULT '',
            metadata_json      TEXT NOT NULL DEFAULT '{}',
            created_at         TEXT NOT NULL DEFAULT '',
            updated_at         TEXT NOT NULL DEFAULT ''
        )
    """,
    "memory_evidence": """
        CREATE TABLE IF NOT EXISTS memory_evidence (
            memory_id          TEXT NOT NULL,
            evidence_id        TEXT NOT NULL,
            relation            TEXT NOT NULL DEFAULT 'supports',
            created_at         TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (memory_id, evidence_id, relation),
            FOREIGN KEY (memory_id) REFERENCES memory_items(memory_id),
            FOREIGN KEY (evidence_id) REFERENCES evidence_items(evidence_id)
        )
    """,
    "memory_proposals": """
        CREATE TABLE IF NOT EXISTS memory_proposals (
            proposal_id        TEXT PRIMARY KEY,
            proposal_type      TEXT NOT NULL DEFAULT 'create',
            target_memory_id   TEXT NOT NULL DEFAULT '',
            memory_layer       TEXT NOT NULL DEFAULT 'long',
            memory_type        TEXT NOT NULL DEFAULT 'summary',
            scope_type         TEXT NOT NULL DEFAULT 'global',
            scope_id           TEXT NOT NULL DEFAULT '',
            subject            TEXT NOT NULL DEFAULT '',
            content            TEXT NOT NULL DEFAULT '',
            summary            TEXT NOT NULL DEFAULT '',
            keywords           TEXT NOT NULL DEFAULT '',
            importance         REAL NOT NULL DEFAULT 0.5,
            confidence         REAL NOT NULL DEFAULT 0.0,
            quality_status     TEXT NOT NULL DEFAULT 'unknown',
            source             TEXT NOT NULL DEFAULT '',
            source_ref         TEXT NOT NULL DEFAULT '',
            evidence_json      TEXT NOT NULL DEFAULT '[]',
            metadata_json      TEXT NOT NULL DEFAULT '{}',
            status              TEXT NOT NULL DEFAULT 'pending',
            proposer            TEXT NOT NULL DEFAULT 'system',
            reviewer            TEXT NOT NULL DEFAULT '',
            review_note        TEXT NOT NULL DEFAULT '',
            created_at         TEXT NOT NULL DEFAULT '',
            reviewed_at        TEXT NOT NULL DEFAULT '',
            updated_at         TEXT NOT NULL DEFAULT ''
        )
    """,
}

# Raw source manifests are intentionally local-only.  They are not included
# in ALL_TABLES/SYNCABLE_TABLES because Google Sheets is not an immutable raw
# archive and must not become the source of truth for downloaded documents.
_INTERNAL_SCHEMA: Dict[str, str] = {
    "source_documents": """
        CREATE TABLE IF NOT EXISTS source_documents (
            document_id       TEXT PRIMARY KEY,
            source            TEXT NOT NULL,
            kind              TEXT NOT NULL DEFAULT 'raw',
            source_ref        TEXT NOT NULL DEFAULT '',
            url               TEXT NOT NULL DEFAULT '',
            title             TEXT NOT NULL DEFAULT '',
            content_path      TEXT NOT NULL DEFAULT '',
            content_hash      TEXT NOT NULL DEFAULT '',
            mime_type         TEXT NOT NULL DEFAULT '',
            as_of             TEXT NOT NULL DEFAULT '',
            published_at      TEXT NOT NULL DEFAULT '',
            retrieved_at      TEXT NOT NULL DEFAULT '',
            valid_from        TEXT NOT NULL DEFAULT '',
            valid_to          TEXT NOT NULL DEFAULT '',
            ingestion_run_id  TEXT NOT NULL DEFAULT '',
            quality_status    TEXT NOT NULL DEFAULT 'unknown',
            metadata_json     TEXT NOT NULL DEFAULT '{}',
            created_at        TEXT NOT NULL DEFAULT '',
            updated_at        TEXT NOT NULL DEFAULT ''
        )
    """,
    "ingestion_runs": """
        CREATE TABLE IF NOT EXISTS ingestion_runs (
            run_id            TEXT PRIMARY KEY,
            source            TEXT NOT NULL,
            source_run_id     TEXT NOT NULL DEFAULT '',
            status            TEXT NOT NULL DEFAULT 'running',
            started_at        TEXT NOT NULL DEFAULT '',
            completed_at      TEXT NOT NULL DEFAULT '',
            document_count    INTEGER NOT NULL DEFAULT 0,
            error             TEXT NOT NULL DEFAULT '',
            metadata_json     TEXT NOT NULL DEFAULT '{}'
        )
    """,
    "research_documents": """
        CREATE TABLE IF NOT EXISTS research_documents (
            document_id        TEXT PRIMARY KEY,
            document_type      TEXT NOT NULL DEFAULT 'research',
            symbol             TEXT NOT NULL DEFAULT '',
            title              TEXT NOT NULL DEFAULT '',
            published_at       TEXT NOT NULL DEFAULT '',
            source_document_id TEXT NOT NULL DEFAULT '',
            canonical_text     TEXT NOT NULL DEFAULT '',
            status             TEXT NOT NULL DEFAULT 'active',
            metadata_json      TEXT NOT NULL DEFAULT '{}',
            created_at         TEXT NOT NULL DEFAULT '',
            updated_at         TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (source_document_id) REFERENCES source_documents(document_id)
        )
    """,
    "research_chunks": """
        CREATE TABLE IF NOT EXISTS research_chunks (
            chunk_id           TEXT PRIMARY KEY,
            document_id        TEXT NOT NULL,
            ordinal            INTEGER NOT NULL DEFAULT 0,
            text               TEXT NOT NULL DEFAULT '',
            token_count        INTEGER NOT NULL DEFAULT 0,
            embedding_model    TEXT NOT NULL DEFAULT '',
            embedding_json     TEXT NOT NULL DEFAULT '',
            created_at         TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (document_id) REFERENCES research_documents(document_id)
        )
    """,
    "evidence_links": """
        CREATE TABLE IF NOT EXISTS evidence_links (
            link_id            TEXT PRIMARY KEY,
            evidence_id        TEXT NOT NULL,
            subject_type       TEXT NOT NULL DEFAULT '',
            subject_id         TEXT NOT NULL DEFAULT '',
            source_document_id TEXT NOT NULL DEFAULT '',
            table_name         TEXT NOT NULL DEFAULT '',
            row_key            TEXT NOT NULL DEFAULT '',
            quote_or_summary   TEXT NOT NULL DEFAULT '',
            as_of              TEXT NOT NULL DEFAULT '',
            created_at         TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (evidence_id) REFERENCES evidence_items(evidence_id),
            FOREIGN KEY (source_document_id) REFERENCES source_documents(document_id)
        )
    """,
    "llm_runs": """
        CREATE TABLE IF NOT EXISTS llm_runs (
            run_id             TEXT PRIMARY KEY,
            task_type          TEXT NOT NULL DEFAULT '',
            model              TEXT NOT NULL DEFAULT '',
            prompt_id          TEXT NOT NULL DEFAULT '',
            prompt_version     TEXT NOT NULL DEFAULT '',
            started_at         TEXT NOT NULL DEFAULT '',
            finished_at        TEXT NOT NULL DEFAULT '',
            status             TEXT NOT NULL DEFAULT 'running',
            input_hash         TEXT NOT NULL DEFAULT '',
            output_hash        TEXT NOT NULL DEFAULT '',
            error              TEXT NOT NULL DEFAULT '',
            trace_id           TEXT NOT NULL DEFAULT '',
            metadata_json      TEXT NOT NULL DEFAULT '{}'
        )
    """,
    "llm_run_evidence": """
        CREATE TABLE IF NOT EXISTS llm_run_evidence (
            run_id             TEXT NOT NULL,
            evidence_id        TEXT NOT NULL,
            ordinal            INTEGER NOT NULL DEFAULT 0,
            role               TEXT NOT NULL DEFAULT 'context',
            PRIMARY KEY (run_id, evidence_id),
            FOREIGN KEY (run_id) REFERENCES llm_runs(run_id),
            FOREIGN KEY (evidence_id) REFERENCES evidence_items(evidence_id)
        )
    """,
    "llm_outputs": """
        CREATE TABLE IF NOT EXISTS llm_outputs (
            output_id          TEXT PRIMARY KEY,
            run_id             TEXT NOT NULL,
            output_format      TEXT NOT NULL DEFAULT 'text',
            structured_json    TEXT NOT NULL DEFAULT '{}',
            markdown           TEXT NOT NULL DEFAULT '',
            confidence         REAL NOT NULL DEFAULT 0.0,
            limitation         TEXT NOT NULL DEFAULT '',
            is_publishable     INTEGER NOT NULL DEFAULT 0,
            created_at         TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (run_id) REFERENCES llm_runs(run_id)
        )
    """,
    "claims": """
        CREATE TABLE IF NOT EXISTS claims (
            claim_id           TEXT PRIMARY KEY,
            run_id             TEXT NOT NULL DEFAULT '',
            text               TEXT NOT NULL DEFAULT '',
            claim_type         TEXT NOT NULL DEFAULT 'fact',
            confidence         REAL NOT NULL DEFAULT 0.0,
            status             TEXT NOT NULL DEFAULT 'unverified',
            uncertainty        TEXT NOT NULL DEFAULT '',
            created_at         TEXT NOT NULL DEFAULT '',
            updated_at         TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (run_id) REFERENCES llm_runs(run_id)
        )
    """,
    "claim_evidence": """
        CREATE TABLE IF NOT EXISTS claim_evidence (
            claim_id           TEXT NOT NULL,
            evidence_id        TEXT NOT NULL,
            relation            TEXT NOT NULL DEFAULT 'supports',
            ordinal            INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (claim_id, evidence_id, relation),
            FOREIGN KEY (claim_id) REFERENCES claims(claim_id),
            FOREIGN KEY (evidence_id) REFERENCES evidence_items(evidence_id)
        )
    """,
    "ask_runs": """
        CREATE TABLE IF NOT EXISTS ask_runs (
            trace_id            TEXT PRIMARY KEY,
            query               TEXT NOT NULL DEFAULT '',
            plan_json           TEXT NOT NULL DEFAULT '{}',
            evidence_json       TEXT NOT NULL DEFAULT '{}',
            pending_resources   TEXT NOT NULL DEFAULT '[]',
            status              TEXT NOT NULL DEFAULT 'partial',
            created_at          TEXT NOT NULL DEFAULT '',
            updated_at          TEXT NOT NULL DEFAULT ''
        )
    """,
}

# These tables are local operational metadata.  They are deliberately kept
# outside ``ALL_TABLES`` so Google Sheets cannot become the source of truth for
# migrations, raw ingestion state, or source health.
_OPERATIONS_SCHEMA: Dict[str, str] = {
    "schema_migrations": """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version       INTEGER PRIMARY KEY,
            name          TEXT NOT NULL,
            checksum      TEXT NOT NULL DEFAULT '',
            applied_at    TEXT NOT NULL DEFAULT ''
        )
    """,
    "source_health": """
        CREATE TABLE IF NOT EXISTS source_health (
            source                TEXT PRIMARY KEY,
            last_run_id           TEXT NOT NULL DEFAULT '',
            last_status           TEXT NOT NULL DEFAULT 'unknown',
            checked_at            TEXT NOT NULL DEFAULT '',
            last_started_at       TEXT NOT NULL DEFAULT '',
            last_completed_at     TEXT NOT NULL DEFAULT '',
            last_source_date      TEXT NOT NULL DEFAULT '',
            last_row_count        INTEGER NOT NULL DEFAULT 0,
            last_raw_hash          TEXT NOT NULL DEFAULT '',
            last_schema_hash       TEXT NOT NULL DEFAULT '',
            consecutive_failures  INTEGER NOT NULL DEFAULT 0,
            last_error             TEXT NOT NULL DEFAULT '',
            updated_at             TEXT NOT NULL DEFAULT ''
        )
    """,
    "sync_conflicts": """
        CREATE TABLE IF NOT EXISTS sync_conflicts (
            conflict_id       TEXT PRIMARY KEY,
            table_name        TEXT NOT NULL,
            row_key_json      TEXT NOT NULL DEFAULT '[]',
            local_row_json    TEXT NOT NULL DEFAULT '{}',
            remote_row_json   TEXT NOT NULL DEFAULT '{}',
            decision          TEXT NOT NULL DEFAULT 'local',
            local_updated_at  TEXT NOT NULL DEFAULT '',
            remote_updated_at TEXT NOT NULL DEFAULT '',
            status            TEXT NOT NULL DEFAULT 'open',
            created_at        TEXT NOT NULL DEFAULT '',
            resolved_at       TEXT NOT NULL DEFAULT '',
            resolution        TEXT NOT NULL DEFAULT ''
        )
    """,
}

# The existing DDL remains the compatibility baseline for old stock.db files.
# Future structural changes should append a migration entry instead of relying
# only on implicit CREATE/ALTER side effects.
_SCHEMA_MIGRATIONS: Tuple[Tuple[int, str], ...] = (
    (1, "legacy-compatible-provenance-and-operations"),
    (2, "research-evidence-claims-and-llm-runs"),
    (3, "valuation-daily-canonical"),
    (4, "dividend-history-canonical"),
    (5, "row-level-sync-conflict-log"),
)

ALL_TABLES: Tuple[str, ...] = tuple(_SCHEMA.keys())

# 預設 sync 範圍：使用者通常想同步的表 (sync_meta 不需要)
SYNCABLE_TABLES: Tuple[str, ...] = (
    "stock_info",
    "etf_meta",
    "monthly_revenue",
    "valuation_daily",
    "dividend_history",
    "quarterly_report",
    "watchlist",
    "price_history",
    "llm_analysis_history",
    "llm_daily_reports",
)

# 舊版 stock.db 的相容 migration。新增欄位全部有安全預設值，不改變既有 PK
# 與既有讀寫行為；新資料則由 DAO 寫入實際溯源資訊。
_PROVENANCE_COLUMNS: Dict[str, Dict[str, str]] = {
    table: {
        "source_url": "TEXT NOT NULL DEFAULT ''",
        "source_date": "TEXT NOT NULL DEFAULT ''",
        "retrieved_at": "TEXT NOT NULL DEFAULT ''",
        "quality_status": "TEXT NOT NULL DEFAULT 'unknown'",
        "source_record_id": "TEXT NOT NULL DEFAULT ''",
    }
    for table in (
        "stock_info", "etf_meta", "monthly_revenue", "valuation_daily", "dividend_history", "quarterly_report",
        "price_history", "intraday_bars",
    )
}
_LLM_METADATA_COLUMNS: Dict[str, Dict[str, str]] = {
    "llm_analysis_history": {
        "evidence_json": "TEXT NOT NULL DEFAULT '[]'",
        "quality_status": "TEXT NOT NULL DEFAULT 'unknown'",
        "source_record_id": "TEXT NOT NULL DEFAULT ''",
    },
    "llm_daily_reports": {
        "evidence_json": "TEXT NOT NULL DEFAULT '[]'",
        "quality_status": "TEXT NOT NULL DEFAULT 'unknown'",
        "source_record_id": "TEXT NOT NULL DEFAULT ''",
    },
}
_EVIDENCE_PROVENANCE_COLUMNS: Dict[str, Dict[str, str]] = {
    "evidence_items": {
        "source_document_id": "TEXT NOT NULL DEFAULT ''",
        "as_of": "TEXT NOT NULL DEFAULT ''",
        "published_at": "TEXT NOT NULL DEFAULT ''",
        "retrieved_at": "TEXT NOT NULL DEFAULT ''",
        "valid_from": "TEXT NOT NULL DEFAULT ''",
        "valid_to": "TEXT NOT NULL DEFAULT ''",
    },
}
_INTERNAL_COLUMNS: Dict[str, Dict[str, str]] = {
    "source_documents": {
        "ingestion_run_id": "TEXT NOT NULL DEFAULT ''",
    },
    "ingestion_runs": {
        "source_run_id": "TEXT NOT NULL DEFAULT ''",
    },
}


# ----------------------------------------------------------------------
# 連線管理
# ----------------------------------------------------------------------


_DEFAULT_DB_REL = "data/stock.db"


def _configured_stock_db_path(root: Optional[Path] = None) -> Optional[Path]:
    """讀取 ``STOCK_DB_PATH``（環境變數或 ``.env`` / Settings）。

    回傳絕對路徑；未設定則 ``None``（改用 ``<root>/data/stock.db``）。
    """
    import os

    raw = (os.environ.get("STOCK_DB_PATH") or "").strip()
    if not raw:
        try:
            from bot.config import Settings

            if root is not None:
                raw = (
                    Settings(_env_file=str(Path(root) / ".env")).stock_db_path
                    or ""
                ).strip()
            else:
                raw = (Settings().stock_db_path or "").strip()
        except Exception:
            raw = ""
    if not raw:
        return None
    path = Path(raw).expanduser()
    if root is not None and not path.is_absolute():
        path = Path(root) / path
    return path


def default_db_path(root: Optional[Path] = None) -> Path:
    """解析實際 DB 路徑。

    優先順序
    --------
    1. ``STOCK_DB_PATH`` / ``Settings.stock_db_path``（若有設定）。有明確
       ``root`` 時亦從 ``root/.env`` 載入，讓 CLI 的 ``--root`` 不依賴 cwd。
    2. ``<root or cwd>/data/stock.db``
    """
    # 保留與舊版測試／整合程式的零參數 monkeypatch 相容性。
    try:
        override = _configured_stock_db_path(root)
    except TypeError:
        override = _configured_stock_db_path()
    if override is not None:
        return override
    return (root or Path.cwd()) / _DEFAULT_DB_REL


def backup_stock_db(
    *,
    src: Optional[Path] = None,
    dest_dir: Optional[Path] = None,
    root: Optional[Path] = None,
    keep: int = 14,
    label: str = "",
) -> Path:
    """用 SQLite online backup API 複製資料庫（含進行中寫入的安全快照）。

    Parameters
    ----------
    src:
        來源 DB；預設 ``default_db_path(root)``。
    dest_dir:
        備份目錄；預設 ``<src.parent>/backups``。
    keep:
        保留最近幾份 ``stock_*.db``；``<=0`` 表示不清理。
    label:
        檔名附加標籤（例如 ``pre-pull``）。

    Returns
    -------
    Path
        新備份檔路徑。
    """
    source = Path(src) if src is not None else default_db_path(root)
    if not source.is_file():
        raise FileNotFoundError(f"找不到資料庫: {source}")

    out_dir = Path(dest_dir) if dest_dir is not None else source.parent / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)

    stamp = now_tw().strftime("%Y%m%d_%H%M%S")
    suffix = f"_{label}" if label else ""
    dest = out_dir / f"stock_{stamp}{suffix}.db"

    fd, temporary_name = tempfile.mkstemp(
        dir=out_dir,
        prefix=f".{dest.name}.",
        suffix=".tmp",
    )
    os.close(fd)
    temporary = Path(temporary_name)

    src_conn = sqlite3.connect(str(source))
    try:
        dst_conn = sqlite3.connect(str(temporary))
        try:
            with dst_conn:
                src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
        os.replace(temporary, dest)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        src_conn.close()

    if keep > 0:
        existing = sorted(
            out_dir.glob("stock_*.db"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in existing[keep:]:
            try:
                old.unlink()
            except OSError:
                pass

    return dest


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
        self._path_lock = _db_path_lock(self.path)
        self._init_schema()

    @classmethod
    def open(
        cls,
        path: Optional[Path | str] = None,
        root: Optional[Path] = None,
    ) -> "StockDB":
        if path is not None:
            return cls(Path(path))
        return cls(default_db_path(root))

    # ----- 連線 (per-thread) -----

    def _connect(self) -> sqlite3.Connection:
        # journal_mode=WAL is not a purely read-only PRAGMA: opening a new
        # connection can briefly contend with a schema or writer transaction.
        # Keep the whole connection setup under the same per-path lock used by
        # transaction(), including callers that only perform a read.
        with self._path_lock:
            conn = sqlite3.connect(
                str(self.path),
                detect_types=sqlite3.PARSE_DECLTYPES,
                isolation_level=None,
                timeout=30.0,
            )
            try:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA synchronous = NORMAL")
                conn.execute("PRAGMA foreign_keys = ON")
                # Dashboard、scheduler、LLM audit 與 fetcher 可能同時寫入同一個
                # local-first DB。SQLite 預設 busy timeout 幾乎為零，會把正常的
                # 短暫 writer contention 誤報成資料流程失敗。
                conn.execute("PRAGMA busy_timeout = 30000")
                return conn
            except Exception:
                conn.close()
                raise

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._connect()
            self._local.conn = c
        return c

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Atomic writes; nested DAO calls use savepoints in the outer transaction."""
        # Keep connection creation inside the lock as well.  _connect() sets
        # journal_mode=WAL, which is itself a SQLite write-like operation and
        # can briefly contend with another connection during startup.
        with self._path_lock:
            c = self.conn
            began = False
            savepoint = "dao_" + uuid.uuid4().hex if c.in_transaction else ""
            try:
                c.execute(f"SAVEPOINT {savepoint}" if savepoint else "BEGIN")
                began = True
                yield c
                c.execute(f"RELEASE SAVEPOINT {savepoint}" if savepoint else "COMMIT")
            except Exception:
                if began:
                    try:
                        if savepoint:
                            c.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                            c.execute(f"RELEASE SAVEPOINT {savepoint}")
                        else:
                            c.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                raise

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    # ----- Schema -----

    def _init_schema(self) -> None:
        # SQLite's busy timeout handles ordinary writer contention; this
        # sidecar lock handles the special case where several independent CLI
        # processes all try to run compatibility DDL during startup.
        with _schema_process_lock(self.path):
            with self.transaction() as c:
                for ddl in _SCHEMA.values():
                    c.execute(ddl)
                for ddl in _INTERNAL_SCHEMA.values():
                    c.execute(ddl)
                for ddl in _OPERATIONS_SCHEMA.values():
                    c.execute(ddl)
                self._ensure_provenance_columns(c)
                self._ensure_columns(c, _LLM_METADATA_COLUMNS)
                self._ensure_columns(c, _EVIDENCE_PROVENANCE_COLUMNS)
                self._ensure_columns(c, _INTERNAL_COLUMNS)
                self._record_schema_migrations(c)
                from bot.schema_migrations import apply_migrations
                apply_migrations(c)
                self._backfill_source_health(c)
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_intraday_bars_symbol_interval_ts "
                    "ON intraday_bars(symbol, interval, ts)"
                )
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_source_runs_source_completed "
                    "ON source_runs(source, completed_at)"
                )
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_source_documents_source_time "
                    "ON source_documents(source, retrieved_at)"
                )
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ingestion_runs_source_time "
                    "ON ingestion_runs(source, completed_at)"
                )
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ingestion_runs_source_run "
                    "ON ingestion_runs(source_run_id)"
                )
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_source_health_status "
                    "ON source_health(last_status, checked_at)"
                )
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_entity_classifications_entity "
                    "ON entity_classifications(entity_type, entity_id, taxonomy, status)"
                )
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_memory_items_scope "
                    "ON memory_items(memory_layer, scope_type, scope_id, memory_status)"
                )
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_memory_items_expiry "
                    "ON memory_items(memory_layer, memory_status, expires_at)"
                )
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_memory_events_memory "
                    "ON memory_events(memory_id, created_at)"
                )
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_evidence_items_source_time "
                    "ON evidence_items(source, observed_at)"
                )
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_memory_evidence_evidence "
                    "ON memory_evidence(evidence_id, created_at)"
                )
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_memory_proposals_status_time "
                    "ON memory_proposals(status, created_at)"
                )
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_evidence_items_document "
                    "ON evidence_items(source_document_id, updated_at)"
                )
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_research_documents_source "
                    "ON research_documents(source_document_id, updated_at)"
                )
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_research_chunks_document "
                    "ON research_chunks(document_id, ordinal)"
                )
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_evidence_links_subject "
                    "ON evidence_links(subject_type, subject_id, created_at)"
                )
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_llm_runs_trace "
                    "ON llm_runs(trace_id, started_at)"
                )
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_claims_run_status "
                    "ON claims(run_id, status, updated_at)"
                )
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ask_runs_status_time "
                    "ON ask_runs(status, updated_at)"
                )
                self._ensure_research_fts(c)
                self._ensure_memory_fts(c)
                for tbl in SYNCABLE_TABLES:
                    c.execute(
                        "INSERT OR IGNORE INTO sync_meta (table_name) VALUES (?)",
                        (tbl,),
                    )

    @staticmethod
    def _record_schema_migrations(conn: sqlite3.Connection) -> None:
        """Record the compatibility baseline without rewriting old databases.

        The existing project already has live databases created by the older
        ``CREATE TABLE IF NOT EXISTS`` plus ``ADD COLUMN`` path.  This records
        that baseline as version 1 so future structural changes have an
        explicit place to land, while retaining the old compatibility logic.
        """
        ddl_fingerprint = "\n".join(
            (*_SCHEMA.values(), *_INTERNAL_SCHEMA.values(), *_OPERATIONS_SCHEMA.values())
        )
        applied_at = now_tw().isoformat(timespec="seconds")
        for version, name in _SCHEMA_MIGRATIONS:
            checksum = hashlib.sha256(
                f"{version}:{name}:{ddl_fingerprint}".encode("utf-8")
            ).hexdigest()
            conn.execute(
                """
                INSERT INTO schema_migrations(version, name, checksum, applied_at)
                VALUES (?,?,?,?)
                ON CONFLICT(version) DO NOTHING
                """,
                (version, name, checksum, applied_at),
            )

    @staticmethod
    def _ensure_research_fts(conn: sqlite3.Connection) -> None:
        """建立可刪除重建的研究片段 FTS；SQLite 不支援時保留關聯資料。

        SQLite 3.50+ 對一般 fts5 表不再接受
        ``INSERT INTO fts(fts, rowid) VALUES('delete', ...)``（會回
        ``SQL logic error``），改用 ``DELETE FROM fts WHERE rowid = ...``。
        """
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS research_chunks_fts "
                "USING fts5(chunk_id UNINDEXED, document_id UNINDEXED, text)"
            )
            conn.execute("DROP TRIGGER IF EXISTS research_chunks_ai")
            conn.execute("DROP TRIGGER IF EXISTS research_chunks_ad")
            conn.execute("DROP TRIGGER IF EXISTS research_chunks_au")
            conn.execute(
                "CREATE TRIGGER research_chunks_ai AFTER INSERT ON research_chunks "
                "BEGIN INSERT INTO research_chunks_fts(rowid, chunk_id, document_id, text) "
                "VALUES (new.rowid, new.chunk_id, new.document_id, new.text); END"
            )
            conn.execute(
                "CREATE TRIGGER research_chunks_ad AFTER DELETE ON research_chunks "
                "BEGIN DELETE FROM research_chunks_fts WHERE rowid = old.rowid; END"
            )
            conn.execute(
                "CREATE TRIGGER research_chunks_au AFTER UPDATE ON research_chunks "
                "BEGIN "
                "DELETE FROM research_chunks_fts WHERE rowid = old.rowid; "
                "INSERT INTO research_chunks_fts(rowid, chunk_id, document_id, text) "
                "VALUES (new.rowid, new.chunk_id, new.document_id, new.text); "
                "END"
            )
            actual = int(conn.execute("SELECT COUNT(*) FROM research_chunks").fetchone()[0])
            indexed = int(conn.execute("SELECT COUNT(*) FROM research_chunks_fts").fetchone()[0])
            if actual != indexed:
                conn.execute("INSERT INTO research_chunks_fts(research_chunks_fts) VALUES ('rebuild')")
        except sqlite3.OperationalError:
            # FTS5 is an optimization only.  The canonical research_chunks table remains usable.
            return

    @staticmethod
    def _backfill_source_health(conn: sqlite3.Connection) -> None:
        """由舊的 ``source_runs`` 建立一次最新來源健康摘要。"""
        existing = int(
            conn.execute("SELECT COUNT(*) FROM source_health").fetchone()[0]
        )
        if existing:
            return
        sources = [
            str(row[0])
            for row in conn.execute(
                "SELECT DISTINCT source FROM source_runs WHERE source <> ''"
            ).fetchall()
        ]
        for source in sources:
            rows = conn.execute(
                "SELECT * FROM source_runs WHERE source = ? "
                "ORDER BY NULLIF(completed_at, '') DESC, "
                "NULLIF(started_at, '') DESC, run_id DESC",
                (source,),
            ).fetchall()
            if not rows:
                continue
            latest = rows[0]
            failures = 0
            for row in rows:
                if str(row["status"] or "") in {"ok", "partial"}:
                    break
                if str(row["status"] or "") not in {"running", "pending"}:
                    failures += 1
            checked_at = str(latest["completed_at"] or latest["started_at"] or "")
            conn.execute(
                """
                INSERT OR IGNORE INTO source_health
                  (source, last_run_id, last_status, checked_at,
                   last_started_at, last_completed_at, last_source_date,
                   last_row_count, last_raw_hash, last_schema_hash,
                   consecutive_failures, last_error, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    source, str(latest["run_id"] or ""),
                    str(latest["status"] or "unknown"), checked_at,
                    str(latest["started_at"] or ""),
                    str(latest["completed_at"] or ""),
                    str(latest["source_date"] or ""),
                    int(latest["row_count"] or 0),
                    str(latest["raw_hash"] or ""),
                    str(latest["schema_hash"] or ""), failures,
                    str(latest["error"] or "")[:2000], checked_at,
                ),
            )

    @staticmethod
    def _ensure_memory_fts(conn: sqlite3.Connection) -> None:
        """建立可重建的 FTS5 索引；沒有 FTS5 時由 memory_store fallback 到 LIKE。"""
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memory_items_fts "
                "USING fts5(memory_id UNINDEXED, content, summary, keywords, subject)"
            )
            conn.execute(
                "CREATE TRIGGER IF NOT EXISTS memory_items_ai AFTER INSERT ON memory_items "
                "BEGIN INSERT INTO memory_items_fts(memory_id, content, summary, keywords, subject) "
                "VALUES (new.memory_id, new.content, new.summary, new.keywords, new.subject); END"
            )
            conn.execute(
                "CREATE TRIGGER IF NOT EXISTS memory_items_au AFTER UPDATE ON memory_items "
                "BEGIN DELETE FROM memory_items_fts WHERE memory_id = old.memory_id; "
                "INSERT INTO memory_items_fts(memory_id, content, summary, keywords, subject) "
                "VALUES (new.memory_id, new.content, new.summary, new.keywords, new.subject); END"
            )
            conn.execute(
                "CREATE TRIGGER IF NOT EXISTS memory_items_ad AFTER DELETE ON memory_items "
                "BEGIN DELETE FROM memory_items_fts WHERE memory_id = old.memory_id; END"
            )
            base_count = int(
                conn.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0]
            )
            fts_count = int(
                conn.execute("SELECT COUNT(*) FROM memory_items_fts").fetchone()[0]
            )
            # INSERT/UPDATE/DELETE triggers keep the index current after the
            # first build. Rebuilding on every connection creates avoidable
            # startup cost and write locks as memory grows.
            if base_count != fts_count:
                conn.execute("DELETE FROM memory_items_fts")
                conn.execute(
                    "INSERT INTO memory_items_fts(memory_id, content, summary, keywords, subject) "
                    "SELECT memory_id, content, summary, keywords, subject FROM memory_items"
                )
        except sqlite3.OperationalError:
            # SQLite builds without FTS5 remain usable; search_memory() uses LIKE.
            return None

    def _ensure_provenance_columns(self, conn: sqlite3.Connection) -> None:
        """對既有資料庫補欄位；SQLite ``ADD COLUMN`` 只能逐欄執行。"""
        for table, columns in _PROVENANCE_COLUMNS.items():
            self._ensure_columns(conn, {table: columns})

    @staticmethod
    def _ensure_columns(
        conn: sqlite3.Connection,
        tables: Dict[str, Dict[str, str]],
    ) -> None:
        for table, columns in tables.items():
            existing = {
                str(row["name"])
                for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, definition in columns.items():
                if name not in existing:
                    conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                    )

    # ----- 通用 helpers -----

    def _row_count(self, table: str) -> int:
        cur = self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}")
        return int(cur.fetchone()["n"])

    def table_counts(self) -> Dict[str, int]:
        return {t: self._row_count(t) for t in ALL_TABLES}

    def schema_migrations(self) -> List[SchemaMigrationRow]:
        """回傳已記錄的 schema 版本，供 preflight／audit 使用。"""
        rows = self.conn.execute(
            "SELECT * FROM schema_migrations ORDER BY version"
        ).fetchall()
        return [_row_to_dc(SchemaMigrationRow, row) for row in rows]

    def schema_version(self) -> int:
        """相容舊版呼叫端的 baseline 版本。

        舊 CLI／試算表把這個欄位當作 v1 相容基線；新研究層版本請使用
        :meth:`latest_schema_version` 或完整的 :meth:`schema_migrations`。
        """
        return min(self.latest_schema_version(), 1)

    def latest_schema_version(self) -> int:
        """回傳實際已套用的最新 migration 版本。"""
        row = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
        ).fetchone()
        return int(row["version"] if row else 0)

    def fetch_all_rows(self, table: str) -> List[Dict[str, Any]]:
        """同步用：把整張 table 拉成 list[dict]。"""
        cur = self.conn.execute(f"SELECT * FROM {table}")
        return [dict(r) for r in cur.fetchall()]

    def replace_table_rows(
        self,
        table: str,
        rows: Iterable[Dict[str, Any]],
        *,
        expected_rows: Optional[List[Dict[str, Any]]] = None,
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
            if expected_rows is not None:
                actual = self.fetch_all_rows(table)
                normalize = lambda data: sorted(json.dumps(r, sort_keys=True) for r in data)
                if normalize(actual) != normalize(expected_rows):
                    raise RuntimeError(f"{table} 在同步期間已更新，請重試同步")
            c.execute(f"DELETE FROM {table}")
            for r in rows:
                values = [r.get(name, _default_for(table, name)) for name in col_names]
                c.execute(sql, values)
            if table == "watchlist":
                from bot.watchlist_repository import WatchlistRepository
                WatchlistRepository.mark_initialized(c)
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
        _prepare_provenance(info)
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO stock_info
                  (symbol, name, short_name, market, industry, isin,
                   listed_date, capital, shares_outstanding, cfi_code,
                   note, updated_at, source_url, source_date, retrieved_at,
                   quality_status, source_record_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                  updated_at = excluded.updated_at,
                  source_url = excluded.source_url,
                  source_date = excluded.source_date,
                  retrieved_at = excluded.retrieved_at,
                  quality_status = excluded.quality_status,
                  source_record_id = excluded.source_record_id
                """,
                (
                    info.symbol, info.name, info.short_name, info.market,
                    info.industry, info.isin, info.listed_date, info.capital,
                    info.shares_outstanding, info.cfi_code, info.note,
                    info.updated_at, info.source_url, info.source_date,
                    info.retrieved_at, info.quality_status, info.source_record_id,
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
        _prepare_provenance(meta)
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO etf_meta
                  (symbol, name, manager, inception_date, holdings_url,
                   note, updated_at, source_url, source_date, retrieved_at,
                   quality_status, source_record_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol) DO UPDATE SET
                  name = excluded.name,
                  manager = excluded.manager,
                  inception_date = excluded.inception_date,
                  holdings_url = excluded.holdings_url,
                  note = excluded.note,
                  updated_at = excluded.updated_at,
                  source_url = excluded.source_url,
                  source_date = excluded.source_date,
                  retrieved_at = excluded.retrieved_at,
                  quality_status = excluded.quality_status,
                  source_record_id = excluded.source_record_id
                """,
                (
                    meta.symbol, meta.name, meta.manager, meta.inception_date,
                    meta.holdings_url, meta.note, meta.updated_at,
                    meta.source_url, meta.source_date, meta.retrieved_at,
                    meta.quality_status, meta.source_record_id,
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
        _prepare_provenance(rev)
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO monthly_revenue
                  (symbol, year_month, revenue, yoy_pct, mom_pct,
                   cumulative, cum_yoy_pct, note, updated_at, source_url,
                   source_date, retrieved_at, quality_status, source_record_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol, year_month) DO UPDATE SET
                  revenue = excluded.revenue,
                  yoy_pct = excluded.yoy_pct,
                  mom_pct = excluded.mom_pct,
                  cumulative = excluded.cumulative,
                  cum_yoy_pct = excluded.cum_yoy_pct,
                  note = excluded.note,
                  updated_at = excluded.updated_at,
                  source_url = excluded.source_url,
                  source_date = excluded.source_date,
                  retrieved_at = excluded.retrieved_at,
                  quality_status = excluded.quality_status,
                  source_record_id = excluded.source_record_id
                """,
                (
                    rev.symbol, rev.year_month, rev.revenue, rev.yoy_pct,
                    rev.mom_pct, rev.cumulative, rev.cum_yoy_pct, rev.note,
                    rev.updated_at, rev.source_url, rev.source_date,
                    rev.retrieved_at, rev.quality_status, rev.source_record_id,
                ),
            )

    # =================================================================
    # valuation_daily DAO
    # =================================================================

    def get_valuation(
        self,
        symbol: str,
        as_of: Optional[str] = None,
    ) -> List[ValuationDaily]:
        if as_of:
            row = self.conn.execute(
                "SELECT * FROM valuation_daily WHERE symbol=? AND as_of=?",
                (symbol, as_of),
            ).fetchone()
            return [_row_to_dc(ValuationDaily, row)] if row else []
        rows = self.conn.execute(
            "SELECT * FROM valuation_daily WHERE symbol=? ORDER BY as_of DESC",
            (symbol,),
        ).fetchall()
        return [_row_to_dc(ValuationDaily, row) for row in rows]

    def upsert_valuation(self, valuation: ValuationDaily) -> None:
        if not valuation.updated_at:
            valuation.updated_at = now_tw().isoformat(timespec="seconds")
        _prepare_provenance(valuation)
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO valuation_daily
                  (symbol, as_of, pe_ratio, pb_ratio, dividend_yield,
                   dividend_year, note, updated_at, source_url, source_date,
                   retrieved_at, quality_status, source_record_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol, as_of) DO UPDATE SET
                  pe_ratio=excluded.pe_ratio, pb_ratio=excluded.pb_ratio,
                  dividend_yield=excluded.dividend_yield,
                  dividend_year=excluded.dividend_year, note=excluded.note,
                  updated_at=excluded.updated_at, source_url=excluded.source_url,
                  source_date=excluded.source_date, retrieved_at=excluded.retrieved_at,
                  quality_status=excluded.quality_status,
                  source_record_id=excluded.source_record_id
                """,
                (
                    valuation.symbol, valuation.as_of, valuation.pe_ratio,
                    valuation.pb_ratio, valuation.dividend_yield,
                    valuation.dividend_year, valuation.note, valuation.updated_at,
                    valuation.source_url, valuation.source_date,
                    valuation.retrieved_at, valuation.quality_status,
                    valuation.source_record_id,
                ),
            )

    # =================================================================
    # dividend_history DAO
    # =================================================================

    def get_dividend_history(
        self,
        symbol: str,
        year: Optional[int] = None,
    ) -> List[DividendHistory]:
        if year is not None:
            row = self.conn.execute(
                "SELECT * FROM dividend_history WHERE symbol=? AND year=?",
                (symbol, int(year)),
            ).fetchone()
            return [_row_to_dc(DividendHistory, row)] if row else []
        rows = self.conn.execute(
            "SELECT * FROM dividend_history WHERE symbol=? ORDER BY year DESC",
            (symbol,),
        ).fetchall()
        return [_row_to_dc(DividendHistory, row) for row in rows]

    def upsert_dividend_history(self, dividend: DividendHistory) -> None:
        if not dividend.updated_at:
            dividend.updated_at = now_tw().isoformat(timespec="seconds")
        _prepare_provenance(dividend)
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO dividend_history
                  (symbol, year, cash_dividend, stock_dividend,
                   ex_dividend_date, ex_right_date, fill_date, fill_days,
                   payout_ratio, note, updated_at, source_url, source_date,
                   retrieved_at, quality_status, source_record_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol, year) DO UPDATE SET
                  cash_dividend=excluded.cash_dividend,
                  stock_dividend=excluded.stock_dividend,
                  ex_dividend_date=excluded.ex_dividend_date,
                  ex_right_date=excluded.ex_right_date,
                  fill_date=excluded.fill_date,
                  fill_days=excluded.fill_days,
                  payout_ratio=excluded.payout_ratio,
                  note=excluded.note,
                  updated_at=excluded.updated_at,
                  source_url=excluded.source_url,
                  source_date=excluded.source_date,
                  retrieved_at=excluded.retrieved_at,
                  quality_status=excluded.quality_status,
                  source_record_id=excluded.source_record_id
                """,
                (
                    dividend.symbol, int(dividend.year), dividend.cash_dividend,
                    dividend.stock_dividend, dividend.ex_dividend_date,
                    dividend.ex_right_date, dividend.fill_date,
                    dividend.fill_days, dividend.payout_ratio, dividend.note,
                    dividend.updated_at, dividend.source_url, dividend.source_date,
                    dividend.retrieved_at, dividend.quality_status,
                    dividend.source_record_id,
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
        _prepare_provenance(rep)
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO quarterly_report
                  (symbol, period, eps, revenue, gross_margin, op_margin,
                   net_margin, note, updated_at, source_url, source_date,
                   retrieved_at, quality_status, source_record_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol, period) DO UPDATE SET
                  eps = excluded.eps,
                  revenue = excluded.revenue,
                  gross_margin = excluded.gross_margin,
                  op_margin = excluded.op_margin,
                  net_margin = excluded.net_margin,
                  note = excluded.note,
                  updated_at = excluded.updated_at,
                  source_url = excluded.source_url,
                  source_date = excluded.source_date,
                  retrieved_at = excluded.retrieved_at,
                  quality_status = excluded.quality_status,
                  source_record_id = excluded.source_record_id
                """,
                (
                    rep.symbol, rep.period, rep.eps, rep.revenue,
                    rep.gross_margin, rep.op_margin, rep.net_margin,
                    rep.note, rep.updated_at, rep.source_url, rep.source_date,
                    rep.retrieved_at, rep.quality_status, rep.source_record_id,
                ),
            )

    # =================================================================
    # watchlist DAO — durable SSOT（JSON 僅 bootstrap／相容快照，見 bot.watchlist）
    # =================================================================

    def get_watch(self, symbol: str) -> Optional[WatchlistRow]:
        row = self.conn.execute(
            "SELECT * FROM watchlist WHERE symbol = ? AND deleted_at = ''", (symbol,)
        ).fetchone()
        return _row_to_dc(WatchlistRow, row) if row else None

    def list_watchlist(self) -> List[WatchlistRow]:
        rows = self.conn.execute(
            "SELECT * FROM watchlist WHERE deleted_at = '' ORDER BY added_at DESC, symbol"
        ).fetchall()
        return [_row_to_dc(WatchlistRow, r) for r in rows]

    def upsert_watch(self, item: WatchlistRow) -> None:
        from bot.watchlist_repository import WatchlistRepository
        repository = WatchlistRepository(self)
        repository.upsert(asdict(item))
        persisted = repository.get(item.symbol)
        for name in ("added_at", "updated_at", "revision", "deleted_at", "restored_from"):
            setattr(item, name, persisted[name])

    def remove_watch(self, symbol: str) -> None:
        from bot.watchlist_repository import WatchlistRepository
        WatchlistRepository(self).remove(symbol)

    # =================================================================
    # price_history DAO  (歷史 K 線 OHLCV)
    # =================================================================

    def upsert_price_bar(self, bar: PriceBar) -> None:
        """寫入單一根 K 線。"""
        if not bar.updated_at:
            bar.updated_at = now_tw().isoformat(timespec="seconds")
        _prepare_provenance(bar, ohlcv=True)
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO price_history
                  (symbol, date, open, high, low, close, volume,
                   source, note, updated_at, source_url, source_date,
                   retrieved_at, quality_status, source_record_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol, date) DO UPDATE SET
                  open = excluded.open,
                  high = excluded.high,
                  low = excluded.low,
                  close = excluded.close,
                  volume = excluded.volume,
                  source = excluded.source,
                  note = excluded.note,
                  updated_at = excluded.updated_at,
                  source_url = excluded.source_url,
                  source_date = excluded.source_date,
                  retrieved_at = excluded.retrieved_at,
                  quality_status = excluded.quality_status,
                  source_record_id = excluded.source_record_id
                """,
                (
                    bar.symbol, bar.date, bar.open, bar.high, bar.low,
                    bar.close, bar.volume, bar.source, bar.note,
                    bar.updated_at, bar.source_url, bar.source_date,
                    bar.retrieved_at, bar.quality_status, bar.source_record_id,
                ),
            )

    def bulk_upsert_price_bars(self, bars: Iterable[PriceBar]) -> int:
        """批次寫入 K 線；同一 transaction 內執行以加速。回傳寫入筆數。"""
        ts_default = now_tw().isoformat(timespec="seconds")
        rows: List[Tuple[Any, ...]] = []
        for b in bars:
            _prepare_provenance(b, ohlcv=True)
            rows.append((
                b.symbol, b.date, b.open, b.high, b.low, b.close, b.volume,
                b.source or "twse", b.note,
                b.updated_at or ts_default, b.source_url, b.source_date,
                b.retrieved_at, b.quality_status, b.source_record_id,
            ))
        if not rows:
            return 0
        with self.transaction() as c:
            c.executemany(
                """
                INSERT INTO price_history
                  (symbol, date, open, high, low, close, volume,
                   source, note, updated_at, source_url, source_date,
                   retrieved_at, quality_status, source_record_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol, date) DO UPDATE SET
                  open = excluded.open,
                  high = excluded.high,
                  low = excluded.low,
                  close = excluded.close,
                  volume = excluded.volume,
                  source = excluded.source,
                  note = excluded.note,
                  updated_at = excluded.updated_at,
                  source_url = excluded.source_url,
                  source_date = excluded.source_date,
                  retrieved_at = excluded.retrieved_at,
                  quality_status = excluded.quality_status,
                  source_record_id = excluded.source_record_id
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
    # intraday_bars DAO  (分 K / Tick 歷史)
    # =================================================================

    def bulk_upsert_intraday_bars(self, bars: Iterable[IntradayBar]) -> int:
        """批次寫入盤中 K 線或 Tick；回傳寫入筆數。"""
        ts_default = now_tw().isoformat(timespec="seconds")
        rows: List[Tuple[Any, ...]] = []
        for b in bars:
            _prepare_provenance(b, ohlcv=True)
            rows.append((
                b.symbol, b.ts, b.interval or "1m",
                b.open, b.high, b.low, b.close, b.volume, b.amount,
                b.source or "shioaji",
                b.updated_at or ts_default, b.source_url, b.source_date,
                b.retrieved_at, b.quality_status, b.source_record_id,
            ))
        if not rows:
            return 0
        with self.transaction() as c:
            c.executemany(
                """
                INSERT INTO intraday_bars
                  (symbol, ts, interval, open, high, low, close, volume, amount,
                   source, updated_at, source_url, source_date, retrieved_at,
                   quality_status, source_record_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol, ts, interval) DO UPDATE SET
                  open = excluded.open,
                  high = excluded.high,
                  low = excluded.low,
                  close = excluded.close,
                  volume = excluded.volume,
                  amount = excluded.amount,
                  source = excluded.source,
                  updated_at = excluded.updated_at,
                  source_url = excluded.source_url,
                  source_date = excluded.source_date,
                  retrieved_at = excluded.retrieved_at,
                  quality_status = excluded.quality_status,
                  source_record_id = excluded.source_record_id
                """,
                rows,
            )
        return len(rows)

    def get_intraday_bars(
        self,
        symbol: str,
        *,
        interval: str = "1m",
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: Optional[int] = None,
        ascending: bool = True,
    ) -> List[IntradayBar]:
        """讀取盤中歷史；start/end 為 ts 字串 (含)，格式 YYYY-MM-DD 或完整時間。"""
        sql = (
            "SELECT * FROM intraday_bars WHERE symbol = ? AND interval = ?"
        )
        args: List[Any] = [symbol, interval]
        if start:
            sql += " AND ts >= ?"
            args.append(start)
        if end:
            if len(end) == 10:
                end = f"{end} 23:59:59"
            sql += " AND ts <= ?"
            args.append(end)
        order = "ASC" if ascending else "DESC"
        sql += f" ORDER BY ts {order}"
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        rows = self.conn.execute(sql, args).fetchall()
        bars = [_row_to_dc(IntradayBar, r) for r in rows]
        if limit and ascending and not (start or end):
            sql2 = (
                "SELECT * FROM intraday_bars WHERE symbol = ? AND interval = ? "
                "ORDER BY ts DESC LIMIT ?"
            )
            rows = self.conn.execute(sql2, (symbol, interval, int(limit))).fetchall()
            bars = [_row_to_dc(IntradayBar, r) for r in rows]
            bars.reverse()
        return bars

    def latest_intraday_ts(
        self,
        symbol: str,
        *,
        interval: str = "1m",
    ) -> Optional[str]:
        row = self.conn.execute(
            "SELECT MAX(ts) AS t FROM intraday_bars "
            "WHERE symbol = ? AND interval = ?",
            (symbol, interval),
        ).fetchone()
        return row["t"] if row and row["t"] else None

    def list_intraday_symbols(self, *, interval: str = "1m") -> List[str]:
        rows = self.conn.execute(
            "SELECT symbol FROM intraday_bars WHERE interval = ? "
            "GROUP BY symbol ORDER BY symbol",
            (interval,),
        ).fetchall()
        return [r["symbol"] for r in rows]

    def intraday_summary(
        self,
        *,
        interval: str = "1m",
    ) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT symbol,
                   COUNT(*) AS rows,
                   MIN(ts) AS first_ts,
                   MAX(ts) AS last_ts
            FROM intraday_bars
            WHERE interval = ?
            GROUP BY symbol
            ORDER BY symbol
            """,
            (interval,),
        ).fetchall()
        return [
            {
                "symbol": r["symbol"],
                "rows": int(r["rows"]),
                "first_ts": r["first_ts"],
                "last_ts": r["last_ts"],
                "interval": interval,
            }
            for r in rows
        ]

    def count_intraday_bars(
        self,
        symbol: str,
        *,
        interval: str = "1m",
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> int:
        sql = (
            "SELECT COUNT(*) AS n FROM intraday_bars "
            "WHERE symbol = ? AND interval = ?"
        )
        args: List[Any] = [symbol, interval]
        if start:
            sql += " AND ts >= ?"
            args.append(start)
        if end:
            if len(end) == 10:
                end = f"{end} 23:59:59"
            sql += " AND ts <= ?"
            args.append(end)
        row = self.conn.execute(sql, args).fetchone()
        return int(row["n"]) if row else 0

    # =================================================================
    # llm_analysis_history DAO
    # =================================================================

    def upsert_llm_analysis(self, row: LlmAnalysisRow) -> None:
        """寫入單筆 LLM 分析歷史。"""
        if not row.updated_at:
            row.updated_at = now_tw().isoformat(timespec="seconds")
        row.evidence_json = row.evidence_json or "[]"
        row.quality_status = normalize_quality_status(row.quality_status)
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO llm_analysis_history
                  (id, ts, ticker, prompt_id, prompt_version, model,
                   sentiment, sentiment_score, confidence, summary,
                   drivers, risks, raw_output, latency_ms, tokens_in,
                   tokens_out, success, error, source, updated_at,
                   evidence_json, quality_status, source_record_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                  updated_at = excluded.updated_at,
                  evidence_json = excluded.evidence_json,
                  quality_status = excluded.quality_status,
                  source_record_id = excluded.source_record_id
                """,
                (
                    row.id, row.ts, row.ticker, row.prompt_id,
                    row.prompt_version, row.model, row.sentiment,
                    row.sentiment_score, row.confidence, row.summary,
                    row.drivers, row.risks, row.raw_output,
                    int(row.latency_ms), int(row.tokens_in),
                    int(row.tokens_out), int(row.success), row.error,
                    row.source, row.updated_at, row.evidence_json,
                    row.quality_status, row.source_record_id,
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

    def update_llm_analysis_evidence(
        self,
        analysis_id: str,
        evidence_ids: Iterable[str],
        *,
        quality_status: Optional[str] = None,
    ) -> bool:
        """補回 LLM 分析使用的 evidence IDs，不改寫模型原始輸出。"""
        values = list(dict.fromkeys(
            str(value).strip() for value in evidence_ids if str(value).strip()
        ))
        if not str(analysis_id or "").strip():
            return False
        assignments = ["evidence_json = ?", "updated_at = ?"]
        args: List[Any] = [json.dumps(values, ensure_ascii=False), now_tw().isoformat(timespec="seconds")]
        if quality_status is not None:
            assignments.append("quality_status = ?")
            args.append(normalize_quality_status(quality_status))
        args.append(str(analysis_id))
        with self.transaction() as c:
            cursor = c.execute(
                "UPDATE llm_analysis_history SET " + ", ".join(assignments)
                + " WHERE id = ?",
                args,
            )
        return cursor.rowcount > 0

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
        row.evidence_json = row.evidence_json or "[]"
        row.quality_status = normalize_quality_status(row.quality_status)
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO llm_daily_reports
                  (report_type, report_date, mode, asof, generated_at,
                   market_tone, prompt_id, prompt_version, brief_md,
                   payload_json, updated_at, evidence_json, quality_status,
                   source_record_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(report_type, report_date, mode) DO UPDATE SET
                  asof = excluded.asof,
                  generated_at = excluded.generated_at,
                  market_tone = excluded.market_tone,
                  prompt_id = excluded.prompt_id,
                  prompt_version = excluded.prompt_version,
                  brief_md = excluded.brief_md,
                  payload_json = excluded.payload_json,
                  updated_at = excluded.updated_at,
                  evidence_json = excluded.evidence_json,
                  quality_status = excluded.quality_status,
                  source_record_id = excluded.source_record_id
                """,
                (
                    row.report_type, row.report_date, row.mode, row.asof,
                    row.generated_at, row.market_tone, row.prompt_id,
                    row.prompt_version, row.brief_md, row.payload_json,
                    row.updated_at, row.evidence_json, row.quality_status,
                    row.source_record_id,
                ),
            )

        # LLM 報告是長期可回放記憶；記憶層失敗不能阻斷既有報告寫入。
        try:
            from bot.memory_store import MemoryStore

            MemoryStore(self).remember_daily_report(row)
        except Exception:
            self._log.debug("LLM daily report memory mirror 失敗", exc_info=True)

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
    # memory DAO — 短期／長期記憶與不可變生命週期事件
    # =================================================================

    def upsert_memory_item(self, row: MemoryItemRow) -> None:
        """寫入記憶項目；同一 memory_id 更新 revision，不刪除舊內容。"""
        now_iso = now_tw().isoformat(timespec="seconds")
        row.created_at = row.created_at or now_iso
        row.updated_at = row.updated_at or now_iso
        row.evidence_json = row.evidence_json or "[]"
        row.metadata_json = row.metadata_json or "{}"
        row.memory_layer = "long" if row.memory_layer == "long" else "short"
        row.memory_status = row.memory_status or "active"
        row.quality_status = normalize_quality_status(row.quality_status)
        row.importance = max(0.0, min(1.0, float(row.importance or 0.0)))
        row.confidence = max(0.0, min(1.0, float(row.confidence or 0.0)))
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO memory_items
                  (memory_id, memory_layer, memory_type, scope_type, scope_id,
                   subject, content, summary, keywords, importance, confidence,
                   quality_status, memory_status, created_at, observed_at,
                   expires_at, last_accessed_at, access_count, source, source_ref,
                   source_record_id, evidence_json, metadata_json, parent_id,
                   revision, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(memory_id) DO UPDATE SET
                  memory_layer = excluded.memory_layer,
                  memory_type = excluded.memory_type,
                  scope_type = excluded.scope_type,
                  scope_id = excluded.scope_id,
                  subject = excluded.subject,
                  content = excluded.content,
                  summary = excluded.summary,
                  keywords = excluded.keywords,
                  importance = excluded.importance,
                  confidence = excluded.confidence,
                  quality_status = excluded.quality_status,
                  memory_status = excluded.memory_status,
                  observed_at = excluded.observed_at,
                  expires_at = excluded.expires_at,
                  last_accessed_at = excluded.last_accessed_at,
                  access_count = excluded.access_count,
                  source = excluded.source,
                  source_ref = excluded.source_ref,
                  source_record_id = excluded.source_record_id,
                  evidence_json = excluded.evidence_json,
                  metadata_json = excluded.metadata_json,
                  parent_id = excluded.parent_id,
                  revision = excluded.revision,
                  updated_at = excluded.updated_at
                """,
                (
                    row.memory_id, row.memory_layer, row.memory_type,
                    row.scope_type, row.scope_id, row.subject, row.content,
                    row.summary, row.keywords, row.importance, row.confidence,
                    row.quality_status, row.memory_status, row.created_at,
                    row.observed_at, row.expires_at, row.last_accessed_at,
                    int(row.access_count), row.source, row.source_ref,
                    row.source_record_id, row.evidence_json, row.metadata_json,
                    row.parent_id, int(row.revision), row.updated_at,
                ),
            )
    def get_memory_item(self, memory_id: str) -> Optional[MemoryItemRow]:
        row = self.conn.execute(
            "SELECT * FROM memory_items WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        return _row_to_dc(MemoryItemRow, row) if row else None

    def list_memory_items(
        self,
        *,
        memory_layer: Optional[str] = None,
        memory_type: Optional[str] = None,
        memory_status: Optional[str] = "active",
        scope_type: Optional[str] = None,
        scope_id: Optional[str] = None,
        limit: int = 200,
    ) -> List[MemoryItemRow]:
        sql = "SELECT * FROM memory_items WHERE 1=1"
        args: List[Any] = []
        if memory_layer:
            sql += " AND memory_layer = ?"
            args.append(memory_layer)
        if memory_type:
            sql += " AND memory_type = ?"
            args.append(memory_type)
        if memory_status:
            sql += " AND memory_status = ?"
            args.append(memory_status)
        if scope_type:
            sql += " AND scope_type = ?"
            args.append(scope_type)
        if scope_id is not None:
            sql += " AND scope_id = ?"
            args.append(scope_id)
        sql += " ORDER BY importance DESC, updated_at DESC LIMIT ?"
        args.append(max(1, int(limit)))
        rows = self.conn.execute(sql, args).fetchall()
        return [_row_to_dc(MemoryItemRow, row) for row in rows]

    def search_memory_rows(
        self,
        query: str,
        *,
        memory_layers: Optional[Iterable[str]] = None,
        scope_type: Optional[str] = None,
        scope_id: Optional[str] = None,
        include_non_active: bool = False,
        include_low_quality: bool = False,
        limit: int = 50,
    ) -> List[MemoryItemRow]:
        """以 FTS5 搜尋記憶；無 FTS5 時退回 LIKE。"""
        layers = [str(v) for v in (memory_layers or []) if str(v)]
        statuses = None if include_non_active else ("active",)
        quality_excluded = () if include_low_quality else (
            "failed", "invalid", "unknown", "missing",
        )
        args: List[Any] = []
        where: List[str] = []
        if statuses:
            where.append("m.memory_status IN (?)")
            args.append(statuses[0])
            # Expired short memory must not leak into normal retrieval even if
            # the maintenance command has not yet archived it.  ``include_non_active``
            # remains available for audit/admin views.
            where.append("(m.expires_at = '' OR m.expires_at IS NULL OR m.expires_at > ?)")
            args.append(now_tw().isoformat(timespec="seconds"))
        if layers:
            where.append("m.memory_layer IN (" + ",".join("?" for _ in layers) + ")")
            args.extend(layers)
        if scope_type:
            where.append("m.scope_type = ?")
            args.append(scope_type)
        if scope_id is not None:
            where.append("m.scope_id = ?")
            args.append(scope_id)
        if quality_excluded:
            where.append("m.quality_status NOT IN (" + ",".join("?" for _ in quality_excluded) + ")")
            args.extend(quality_excluded)

        base_where = (" WHERE " + " AND ".join(where)) if where else ""
        safe_query = str(query or "").strip()
        rows: List[sqlite3.Row]
        try:
            if safe_query:
                sql = (
                    "SELECT m.* FROM memory_items m "
                    "JOIN memory_items_fts f ON f.memory_id = m.memory_id "
                    "WHERE memory_items_fts MATCH ?"
                )
                fts_args: List[Any] = [safe_query]
                if where:
                    sql += " AND " + " AND ".join(where)
                fts_args.extend(args)
                sql += " ORDER BY m.importance DESC, m.confidence DESC, m.updated_at DESC LIMIT ?"
                fts_args.append(max(1, int(limit)))
                rows = self.conn.execute(sql, fts_args).fetchall()
            else:
                sql = "SELECT m.* FROM memory_items m" + base_where
                sql += " ORDER BY m.importance DESC, m.updated_at DESC LIMIT ?"
                args.append(max(1, int(limit)))
                rows = self.conn.execute(sql, args).fetchall()
        except sqlite3.OperationalError:
            terms = [term for term in safe_query.split() if term]
            like_parts: List[str] = []
            like_args: List[Any] = []
            for term in terms:
                like_parts.append("(m.content LIKE ? OR m.summary LIKE ? OR m.keywords LIKE ? OR m.subject LIKE ?)")
                like_args.extend([f"%{term}%"] * 4)
            fallback_where = list(where)
            fallback_args = list(args)
            if like_parts:
                fallback_where.append(" AND ".join(like_parts))
                fallback_args.extend(like_args)
            sql = "SELECT m.* FROM memory_items m"
            if fallback_where:
                sql += " WHERE " + " AND ".join(fallback_where)
            sql += " ORDER BY m.importance DESC, m.confidence DESC, m.updated_at DESC LIMIT ?"
            fallback_args.append(max(1, int(limit)))
            rows = self.conn.execute(sql, fallback_args).fetchall()
        return [_row_to_dc(MemoryItemRow, row) for row in rows]

    def touch_memory_item(self, memory_id: str, *, accessed_at: str = "") -> None:
        accessed_at = accessed_at or now_tw().isoformat(timespec="seconds")
        with self.transaction() as c:
            c.execute(
                """
                UPDATE memory_items
                SET last_accessed_at = ?, access_count = access_count + 1,
                    updated_at = ?
                WHERE memory_id = ?
                """,
                (accessed_at, accessed_at, memory_id),
            )

    def update_memory_status(
        self,
        memory_id: str,
        memory_status: str,
        *,
        updated_at: str = "",
    ) -> None:
        updated_at = updated_at or now_tw().isoformat(timespec="seconds")
        with self.transaction() as c:
            c.execute(
                "UPDATE memory_items SET memory_status = ?, updated_at = ?, revision = revision + 1 "
                "WHERE memory_id = ?",
                (memory_status, updated_at, memory_id),
            )

    def append_memory_event(self, row: MemoryEventRow) -> None:
        row.created_at = row.created_at or now_tw().isoformat(timespec="seconds")
        row.payload_json = row.payload_json or "{}"
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO memory_events
                  (event_id, memory_id, event_type, actor, trace_id,
                   payload_json, created_at)
                VALUES (?,?,?,?,?,?,?)
                """,
                (row.event_id, row.memory_id, row.event_type, row.actor,
                 row.trace_id, row.payload_json, row.created_at),
            )

    def list_memory_events(
        self,
        memory_id: str,
        *,
        limit: int = 200,
    ) -> List[MemoryEventRow]:
        rows = self.conn.execute(
            "SELECT * FROM memory_events WHERE memory_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (memory_id, max(1, int(limit))),
        ).fetchall()
        return [_row_to_dc(MemoryEventRow, row) for row in rows]

    # =================================================================
    # evidence / memory proposal DAO — 中央證據登錄與記憶審核佇列
    # =================================================================

    def upsert_evidence(self, row: EvidenceItemRow) -> None:
        """登錄一筆可重用證據；同一 evidence_id 只更新目前 metadata。"""
        if row.source_document_id and not self.get_source_document(row.source_document_id):
            raise ValueError(f"unknown source_document_id: {row.source_document_id}")
        now_iso = now_tw().isoformat(timespec="seconds")
        row.created_at = row.created_at or now_iso
        row.updated_at = row.updated_at or now_iso
        row.metadata_json = row.metadata_json or "{}"
        row.quality_status = normalize_quality_status(row.quality_status)
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO evidence_items
                  (evidence_id, source_document_id, kind, source, source_ref, title, url,
                   observed_at, as_of, published_at, retrieved_at, valid_from,
                   valid_to, excerpt, quality_status, content_hash,
                   metadata_json, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(evidence_id) DO UPDATE SET
                  source_document_id = CASE
                    WHEN excluded.source_document_id <> '' THEN excluded.source_document_id
                    ELSE evidence_items.source_document_id
                  END,
                  kind = excluded.kind,
                  source = excluded.source,
                  source_ref = excluded.source_ref,
                  title = excluded.title,
                  url = excluded.url,
                  observed_at = excluded.observed_at,
                  as_of = excluded.as_of,
                  published_at = excluded.published_at,
                  retrieved_at = excluded.retrieved_at,
                  valid_from = excluded.valid_from,
                  valid_to = excluded.valid_to,
                  excerpt = excluded.excerpt,
                  quality_status = excluded.quality_status,
                  content_hash = excluded.content_hash,
                  metadata_json = excluded.metadata_json,
                  updated_at = excluded.updated_at
                """,
                (
                    row.evidence_id, row.source_document_id, row.kind, row.source, row.source_ref,
                    row.title, row.url, row.observed_at, row.as_of,
                    row.published_at, row.retrieved_at, row.valid_from,
                    row.valid_to, row.excerpt, row.quality_status,
                    row.content_hash, row.metadata_json, row.created_at,
                    row.updated_at,
                ),
            )

    def get_evidence(self, evidence_id: str) -> Optional[EvidenceItemRow]:
        row = self.conn.execute(
            "SELECT * FROM evidence_items WHERE evidence_id = ?",
            (evidence_id,),
        ).fetchone()
        return _row_to_dc(EvidenceItemRow, row) if row else None

    def list_evidence(
        self,
        *,
        memory_id: Optional[str] = None,
        source: Optional[str] = None,
        limit: int = 200,
    ) -> List[EvidenceItemRow]:
        sql = "SELECT e.* FROM evidence_items e"
        args: List[Any] = []
        where: List[str] = []
        if memory_id:
            sql += " JOIN memory_evidence me ON me.evidence_id = e.evidence_id"
            where.append("me.memory_id = ?")
            args.append(memory_id)
        if source:
            where.append("e.source = ?")
            args.append(source)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY e.observed_at DESC, e.updated_at DESC LIMIT ?"
        args.append(max(1, int(limit)))
        rows = self.conn.execute(sql, args).fetchall()
        return [_row_to_dc(EvidenceItemRow, row) for row in rows]

    def search_evidence(
        self,
        query: str,
        *,
        source: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 50,
    ) -> List[EvidenceItemRow]:
        """以標題、來源、引用片段搜尋中央證據索引。

        Evidence registry 的搜尋刻意保持為 SQLite LIKE；它是可重建的
        provenance 索引，不在 DAO 層偷偷引入不可追蹤的向量服務。
        """
        terms = [part.strip() for part in str(query or "").split() if part.strip()]
        sql = "SELECT e.* FROM evidence_items e"
        where: List[str] = ["e.quality_status IN ('ok', 'partial')"]
        args: List[Any] = []
        if terms:
            term_where: List[str] = []
            for term in terms:
                term_where.append(
                    "(e.title LIKE ? OR e.source LIKE ? OR e.source_ref LIKE ? "
                    "OR e.excerpt LIKE ?)"
                )
                args.extend([f"%{term}%"] * 4)
            where.append("(" + " OR ".join(term_where) + ")")
        if source:
            where.append("e.source = ?")
            args.append(source)
        if kind:
            where.append("e.kind = ?")
            args.append(kind)
        sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY e.observed_at DESC, e.updated_at DESC LIMIT ?"
        args.append(max(1, int(limit)))
        rows = self.conn.execute(sql, args).fetchall()
        return [_row_to_dc(EvidenceItemRow, row) for row in rows]

    # =================================================================
    # research / claims / LLM run DAO
    # =================================================================

    def find_source_document(
        self,
        *,
        document_id: str = "",
        content_hash: str = "",
    ) -> Optional[SourceDocumentRow]:
        """只接受呼叫端提供的 exact identity，不以 URL／標題猜測來源。"""
        if document_id:
            return self.get_source_document(document_id)
        if content_hash:
            row = self.conn.execute(
                "SELECT * FROM source_documents WHERE content_hash = ? "
                "ORDER BY retrieved_at DESC LIMIT 1",
                (content_hash,),
            ).fetchone()
            return _row_to_dc(SourceDocumentRow, row) if row else None
        return None

    def upsert_research_document(self, row: ResearchDocumentRow) -> None:
        now = now_tw().isoformat(timespec="seconds")
        row.created_at = row.created_at or now
        row.updated_at = row.updated_at or now
        row.metadata_json = row.metadata_json or "{}"
        if row.source_document_id and not self.get_source_document(row.source_document_id):
            raise ValueError(f"unknown source_document_id: {row.source_document_id}")
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO research_documents
                  (document_id, document_type, symbol, title, published_at,
                   source_document_id, canonical_text, status, metadata_json,
                   created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(document_id) DO UPDATE SET
                  document_type = excluded.document_type,
                  symbol = excluded.symbol,
                  title = excluded.title,
                  published_at = excluded.published_at,
                  source_document_id = excluded.source_document_id,
                  canonical_text = excluded.canonical_text,
                  status = excluded.status,
                  metadata_json = excluded.metadata_json,
                  updated_at = excluded.updated_at
                """,
                (row.document_id, row.document_type, row.symbol, row.title,
                 row.published_at, row.source_document_id, row.canonical_text,
                 row.status, row.metadata_json, row.created_at, row.updated_at),
            )

    def get_research_document(self, document_id: str) -> Optional[ResearchDocumentRow]:
        row = self.conn.execute(
            "SELECT * FROM research_documents WHERE document_id = ?",
            (document_id,),
        ).fetchone()
        return _row_to_dc(ResearchDocumentRow, row) if row else None

    def list_research_documents(
        self, *, symbol: Optional[str] = None, status: Optional[str] = None,
        limit: int = 200,
    ) -> List[ResearchDocumentRow]:
        sql = "SELECT * FROM research_documents WHERE 1=1"
        args: List[Any] = []
        if symbol:
            sql += " AND symbol = ?"
            args.append(symbol)
        if status:
            sql += " AND status = ?"
            args.append(status)
        sql += " ORDER BY published_at DESC, updated_at DESC LIMIT ?"
        args.append(max(1, int(limit)))
        return [_row_to_dc(ResearchDocumentRow, r) for r in self.conn.execute(sql, args).fetchall()]

    def upsert_research_chunk(self, row: ResearchChunkRow) -> None:
        if not self.get_research_document(row.document_id):
            raise ValueError(f"unknown research document: {row.document_id}")
        row.created_at = row.created_at or now_tw().isoformat(timespec="seconds")
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO research_chunks
                  (chunk_id, document_id, ordinal, text, token_count,
                   embedding_model, embedding_json, created_at)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(chunk_id) DO UPDATE SET
                  document_id = excluded.document_id,
                  ordinal = excluded.ordinal,
                  text = excluded.text,
                  token_count = excluded.token_count,
                  embedding_model = excluded.embedding_model,
                  embedding_json = excluded.embedding_json
                """,
                (row.chunk_id, row.document_id, int(row.ordinal), row.text,
                 int(row.token_count), row.embedding_model, row.embedding_json,
                 row.created_at),
            )

    def list_research_chunks(self, document_id: str, *, limit: int = 10_000) -> List[ResearchChunkRow]:
        rows = self.conn.execute(
            "SELECT * FROM research_chunks WHERE document_id = ? ORDER BY ordinal, chunk_id LIMIT ?",
            (document_id, max(1, int(limit))),
        ).fetchall()
        return [_row_to_dc(ResearchChunkRow, r) for r in rows]

    def search_research_chunks(self, query: str, *, limit: int = 50) -> List[ResearchChunkRow]:
        try:
            rows = self.conn.execute(
                "SELECT c.* FROM research_chunks c JOIN research_chunks_fts f "
                "ON f.rowid = c.rowid WHERE research_chunks_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (query, max(1, int(limit))),
            ).fetchall()
        except sqlite3.OperationalError:
            terms = [v for v in str(query or "").split() if v]
            where = " OR ".join("text LIKE ?" for _ in terms) or "1=0"
            rows = self.conn.execute(
                f"SELECT * FROM research_chunks WHERE {where} LIMIT ?",
                [*(f"%{v}%" for v in terms), max(1, int(limit))],
            ).fetchall()
        return [_row_to_dc(ResearchChunkRow, r) for r in rows]

    def upsert_evidence_link(self, row: EvidenceLinkRow) -> None:
        if not self.get_evidence(row.evidence_id):
            raise ValueError(f"unknown evidence_id: {row.evidence_id}")
        if row.source_document_id and not self.get_source_document(row.source_document_id):
            raise ValueError(f"unknown source_document_id: {row.source_document_id}")
        row.created_at = row.created_at or now_tw().isoformat(timespec="seconds")
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO evidence_links
                  (link_id, evidence_id, subject_type, subject_id,
                   source_document_id, table_name, row_key, quote_or_summary,
                   as_of, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(link_id) DO UPDATE SET
                  evidence_id = excluded.evidence_id,
                  subject_type = excluded.subject_type,
                  subject_id = excluded.subject_id,
                  source_document_id = excluded.source_document_id,
                  table_name = excluded.table_name,
                  row_key = excluded.row_key,
                  quote_or_summary = excluded.quote_or_summary,
                  as_of = excluded.as_of
                """,
                (row.link_id, row.evidence_id, row.subject_type, row.subject_id,
                 row.source_document_id, row.table_name, row.row_key,
                 row.quote_or_summary, row.as_of, row.created_at),
            )

    def list_evidence_links(self, evidence_id: str, *, limit: int = 200) -> List[EvidenceLinkRow]:
        rows = self.conn.execute(
            "SELECT * FROM evidence_links WHERE evidence_id = ? ORDER BY created_at LIMIT ?",
            (evidence_id, max(1, int(limit))),
        ).fetchall()
        return [_row_to_dc(EvidenceLinkRow, r) for r in rows]

    def upsert_llm_run(self, row: LlmRunRow) -> None:
        now = now_tw().isoformat(timespec="seconds")
        row.started_at = row.started_at or now
        row.metadata_json = row.metadata_json or "{}"
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO llm_runs
                  (run_id, task_type, model, prompt_id, prompt_version,
                   started_at, finished_at, status, input_hash, output_hash,
                   error, trace_id, metadata_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id) DO UPDATE SET
                  task_type = excluded.task_type, model = excluded.model,
                  prompt_id = excluded.prompt_id, prompt_version = excluded.prompt_version,
                  started_at = excluded.started_at, finished_at = excluded.finished_at,
                  status = excluded.status, input_hash = excluded.input_hash,
                  output_hash = excluded.output_hash, error = excluded.error,
                  trace_id = excluded.trace_id, metadata_json = excluded.metadata_json
                """,
                (row.run_id, row.task_type, row.model, row.prompt_id,
                 row.prompt_version, row.started_at, row.finished_at, row.status,
                 row.input_hash, row.output_hash, row.error, row.trace_id,
                 row.metadata_json),
            )

    def get_llm_run(self, run_id: str) -> Optional[LlmRunRow]:
        row = self.conn.execute("SELECT * FROM llm_runs WHERE run_id = ?", (run_id,)).fetchone()
        return _row_to_dc(LlmRunRow, row) if row else None

    def link_llm_run_evidence(self, row: LlmRunEvidenceRow) -> None:
        if not self.get_llm_run(row.run_id) or not self.get_evidence(row.evidence_id):
            raise ValueError("llm run or evidence does not exist")
        with self.transaction() as c:
            c.execute(
                "INSERT INTO llm_run_evidence(run_id, evidence_id, ordinal, role) "
                "VALUES (?,?,?,?) ON CONFLICT(run_id, evidence_id) DO UPDATE SET ordinal=excluded.ordinal, role=excluded.role",
                (row.run_id, row.evidence_id, int(row.ordinal), row.role),
            )

    def list_llm_run_evidence(self, run_id: str, *, limit: int = 500) -> List[EvidenceItemRow]:
        rows = self.conn.execute(
            "SELECT e.* FROM evidence_items e JOIN llm_run_evidence x "
            "ON x.evidence_id=e.evidence_id WHERE x.run_id=? ORDER BY x.ordinal LIMIT ?",
            (run_id, max(1, int(limit))),
        ).fetchall()
        return [_row_to_dc(EvidenceItemRow, r) for r in rows]

    def upsert_llm_output(self, row: LlmOutputRow) -> None:
        if not self.get_llm_run(row.run_id):
            raise ValueError(f"unknown llm run: {row.run_id}")
        row.created_at = row.created_at or now_tw().isoformat(timespec="seconds")
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO llm_outputs
                  (output_id, run_id, output_format, structured_json, markdown,
                   confidence, limitation, is_publishable, created_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(output_id) DO UPDATE SET
                  run_id=excluded.run_id, output_format=excluded.output_format,
                  structured_json=excluded.structured_json, markdown=excluded.markdown,
                  confidence=excluded.confidence, limitation=excluded.limitation,
                  is_publishable=excluded.is_publishable
                """,
                (row.output_id, row.run_id, row.output_format, row.structured_json,
                 row.markdown, max(0.0, min(1.0, float(row.confidence))),
                 row.limitation, int(bool(row.is_publishable)), row.created_at),
            )

    def upsert_claim(self, row: ClaimRow) -> None:
        if row.run_id and not self.get_llm_run(row.run_id):
            raise ValueError(f"unknown llm run: {row.run_id}")
        now = now_tw().isoformat(timespec="seconds")
        row.created_at = row.created_at or now
        row.updated_at = row.updated_at or now
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO claims
                  (claim_id, run_id, text, claim_type, confidence, status,
                   uncertainty, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(claim_id) DO UPDATE SET
                  run_id=excluded.run_id, text=excluded.text,
                  claim_type=excluded.claim_type, confidence=excluded.confidence,
                  status=excluded.status, uncertainty=excluded.uncertainty,
                  updated_at=excluded.updated_at
                """,
                (row.claim_id, row.run_id, row.text, row.claim_type,
                 max(0.0, min(1.0, float(row.confidence))), row.status,
                 row.uncertainty, row.created_at, row.updated_at),
            )

    def get_claim(self, claim_id: str) -> Optional[ClaimRow]:
        row = self.conn.execute(
            "SELECT * FROM claims WHERE claim_id = ?",
            (claim_id,),
        ).fetchone()
        return _row_to_dc(ClaimRow, row) if row else None

    def link_claim_evidence(self, row: ClaimEvidenceRow) -> None:
        if not self.conn.execute("SELECT 1 FROM claims WHERE claim_id=?", (row.claim_id,)).fetchone():
            raise ValueError(f"unknown claim_id: {row.claim_id}")
        if not self.get_evidence(row.evidence_id):
            raise ValueError(f"unknown evidence_id: {row.evidence_id}")
        with self.transaction() as c:
            c.execute(
                "INSERT INTO claim_evidence(claim_id,evidence_id,relation,ordinal) VALUES(?,?,?,?) "
                "ON CONFLICT(claim_id,evidence_id,relation) DO UPDATE SET ordinal=excluded.ordinal",
                (row.claim_id, row.evidence_id, row.relation, int(row.ordinal)),
            )

    def list_claim_evidence(self, claim_id: str, *, limit: int = 200) -> List[EvidenceItemRow]:
        rows = self.conn.execute(
            "SELECT e.* FROM evidence_items e JOIN claim_evidence x ON x.evidence_id=e.evidence_id "
            "WHERE x.claim_id=? ORDER BY x.ordinal LIMIT ?",
            (claim_id, max(1, int(limit))),
        ).fetchall()
        return [_row_to_dc(EvidenceItemRow, r) for r in rows]

    def upsert_ask_run(
        self,
        trace_id: str,
        *,
        query: str,
        plan_json: str,
        evidence_json: str,
        pending_resources: str = "[]",
        status: str = "partial",
    ) -> None:
        now = now_tw().isoformat(timespec="seconds")
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO ask_runs
                  (trace_id, query, plan_json, evidence_json, pending_resources,
                   status, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(trace_id) DO UPDATE SET
                  query=excluded.query, plan_json=excluded.plan_json,
                  evidence_json=excluded.evidence_json,
                  pending_resources=excluded.pending_resources,
                  status=excluded.status, updated_at=excluded.updated_at
                """,
                (trace_id, query, plan_json, evidence_json, pending_resources,
                 status, now, now),
            )

    def get_ask_run(self, trace_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM ask_runs WHERE trace_id = ?", (trace_id,)
        ).fetchone()
        return dict(row) if row else None

    def link_memory_evidence(
        self,
        memory_id: str,
        evidence_id: str,
        *,
        relation: str = "supports",
        created_at: str = "",
    ) -> None:
        created_at = created_at or now_tw().isoformat(timespec="seconds")
        with self.transaction() as c:
            c.execute(
                """
                INSERT OR IGNORE INTO memory_evidence
                  (memory_id, evidence_id, relation, created_at)
                VALUES (?,?,?,?)
                """,
                (memory_id, evidence_id, relation or "supports", created_at),
            )

    def list_memory_evidence(
        self,
        memory_id: str,
        *,
        relation: Optional[str] = None,
        limit: int = 200,
    ) -> List[EvidenceItemRow]:
        sql = (
            "SELECT e.* FROM evidence_items e "
            "JOIN memory_evidence me ON me.evidence_id = e.evidence_id "
            "WHERE me.memory_id = ?"
        )
        args: List[Any] = [memory_id]
        if relation:
            sql += " AND me.relation = ?"
            args.append(relation)
        sql += " ORDER BY e.observed_at DESC, e.updated_at DESC LIMIT ?"
        args.append(max(1, int(limit)))
        rows = self.conn.execute(sql, args).fetchall()
        return [_row_to_dc(EvidenceItemRow, row) for row in rows]

    def create_memory_proposal(self, row: MemoryProposalRow) -> None:
        """建立待審核記憶提案；提案不會自動進入 memory_items。"""
        now_iso = now_tw().isoformat(timespec="seconds")
        row.created_at = row.created_at or now_iso
        row.updated_at = row.updated_at or now_iso
        row.evidence_json = row.evidence_json or "[]"
        row.metadata_json = row.metadata_json or "{}"
        row.status = row.status or "pending"
        row.quality_status = normalize_quality_status(row.quality_status)
        row.memory_layer = "long" if row.memory_layer == "long" else "short"
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO memory_proposals
                  (proposal_id, proposal_type, target_memory_id,
                   memory_layer, memory_type, scope_type, scope_id, subject,
                   content, summary, keywords, importance, confidence,
                   quality_status, source, source_ref, evidence_json,
                   metadata_json, status, proposer, reviewer, review_note,
                   created_at, reviewed_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(proposal_id) DO UPDATE SET
                  proposal_type = excluded.proposal_type,
                  target_memory_id = excluded.target_memory_id,
                  memory_layer = excluded.memory_layer,
                  memory_type = excluded.memory_type,
                  scope_type = excluded.scope_type,
                  scope_id = excluded.scope_id,
                  subject = excluded.subject,
                  content = excluded.content,
                  summary = excluded.summary,
                  keywords = excluded.keywords,
                  importance = excluded.importance,
                  confidence = excluded.confidence,
                  quality_status = excluded.quality_status,
                  source = excluded.source,
                  source_ref = excluded.source_ref,
                  evidence_json = excluded.evidence_json,
                  metadata_json = excluded.metadata_json,
                  status = excluded.status,
                  reviewer = excluded.reviewer,
                  review_note = excluded.review_note,
                  reviewed_at = excluded.reviewed_at,
                  updated_at = excluded.updated_at
                """,
                (
                    row.proposal_id, row.proposal_type, row.target_memory_id,
                    row.memory_layer, row.memory_type, row.scope_type,
                    row.scope_id, row.subject, row.content, row.summary,
                    row.keywords, max(0.0, min(1.0, float(row.importance))),
                    max(0.0, min(1.0, float(row.confidence))),
                    row.quality_status, row.source, row.source_ref,
                    row.evidence_json, row.metadata_json, row.status,
                    row.proposer, row.reviewer, row.review_note,
                    row.created_at, row.reviewed_at, row.updated_at,
                ),
            )

    def get_memory_proposal(self, proposal_id: str) -> Optional[MemoryProposalRow]:
        row = self.conn.execute(
            "SELECT * FROM memory_proposals WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        return _row_to_dc(MemoryProposalRow, row) if row else None

    def list_memory_proposals(
        self,
        *,
        status: Optional[str] = "pending",
        limit: int = 200,
    ) -> List[MemoryProposalRow]:
        sql = "SELECT * FROM memory_proposals WHERE 1=1"
        args: List[Any] = []
        if status:
            sql += " AND status = ?"
            args.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, int(limit)))
        rows = self.conn.execute(sql, args).fetchall()
        return [_row_to_dc(MemoryProposalRow, row) for row in rows]

    def review_memory_proposal(
        self,
        proposal_id: str,
        status: str,
        *,
        reviewer: str = "",
        review_note: str = "",
        target_memory_id: Optional[str] = None,
        reviewed_at: str = "",
    ) -> bool:
        if status not in {"pending", "approved", "rejected", "superseded"}:
            raise ValueError("proposal status 必須是 pending、approved、rejected 或 superseded")
        reviewed_at = reviewed_at or now_tw().isoformat(timespec="seconds")
        fields = (
            "status = ?, reviewer = ?, review_note = ?, reviewed_at = ?, "
            "updated_at = ?"
        )
        args: List[Any] = [status, reviewer, review_note, reviewed_at, reviewed_at]
        if target_memory_id is not None:
            fields = "target_memory_id = ?, " + fields
            args.insert(0, target_memory_id)
        args.append(proposal_id)
        with self.transaction() as c:
            cur = c.execute(
                f"UPDATE memory_proposals SET {fields} WHERE proposal_id = ?",
                args,
            )
        return cur.rowcount > 0

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

    def record_sync_conflict(
        self,
        *,
        table_name: str,
        row_key: Tuple[str, ...],
        local_row: Dict[str, Any],
        remote_row: Dict[str, Any],
        decision: str = "local",
        local_updated_at: str = "",
        remote_updated_at: str = "",
    ) -> str:
        """保存 row-level 同步衝突；相同衝突重跑不重複建立紀錄。"""
        payload = json.dumps(
            {
                "table_name": table_name,
                "row_key": list(row_key),
                "local_row": local_row,
                "remote_row": remote_row,
                "decision": decision,
                "local_updated_at": local_updated_at,
                "remote_updated_at": remote_updated_at,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        conflict_id = "sync-conflict:" + hashlib.sha256(
            payload.encode("utf-8")
        ).hexdigest()[:32]
        created_at = now_tw().isoformat(timespec="seconds")
        with self.transaction() as c:
            c.execute(
                """
                INSERT OR IGNORE INTO sync_conflicts
                  (conflict_id, table_name, row_key_json, local_row_json,
                   remote_row_json, decision, local_updated_at,
                   remote_updated_at, status, created_at)
                VALUES (?,?,?,?,?,?,?,?, 'open', ?)
                """,
                (
                    conflict_id,
                    table_name,
                    json.dumps(list(row_key), ensure_ascii=False),
                    json.dumps(local_row, ensure_ascii=False, sort_keys=True, default=str),
                    json.dumps(remote_row, ensure_ascii=False, sort_keys=True, default=str),
                    decision,
                    local_updated_at,
                    remote_updated_at,
                    created_at,
                ),
            )
        return conflict_id

    def list_sync_conflicts(
        self,
        table_name: Optional[str] = None,
        *,
        status: Optional[str] = None,
        limit: int = 1000,
    ) -> List[SyncConflictRow]:
        """列出本地衝突紀錄，供 Dashboard／人工裁決／稽核使用。"""
        sql = "SELECT * FROM sync_conflicts WHERE 1=1"
        args: List[Any] = []
        if table_name:
            sql += " AND table_name = ?"
            args.append(table_name)
        if status:
            sql += " AND status = ?"
            args.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, int(limit)))
        rows = self.conn.execute(sql, args).fetchall()
        return [_row_to_dc(SyncConflictRow, row) for row in rows]

    def resolve_sync_conflict(
        self,
        conflict_id: str,
        *,
        resolution: str,
        status: str = "resolved",
    ) -> bool:
        if status not in {"open", "resolved", "ignored"}:
            raise ValueError("sync conflict status 必須是 open、resolved 或 ignored")
        resolved_at = now_tw().isoformat(timespec="seconds") if status != "open" else ""
        with self.transaction() as c:
            cur = c.execute(
                """
                UPDATE sync_conflicts
                   SET status = ?, resolved_at = ?, resolution = ?
                 WHERE conflict_id = ?
                """,
                (status, resolved_at, str(resolution or ""), conflict_id),
            )
        return cur.rowcount > 0

    # =================================================================
    # source_runs / entity_classifications DAO
    # =================================================================

    def record_source_run(self, run: SourceRun) -> None:
        """保存一次來源檢查或 ingestion 結果，不保存大型原始 payload。"""
        now = now_tw().isoformat(timespec="seconds")
        if not run.started_at:
            run.started_at = now
        if not run.completed_at and run.status not in {"running", "pending"}:
            run.completed_at = now
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO source_runs
                  (run_id, source, source_url, status, started_at, completed_at,
                   source_date, row_count, raw_hash, schema_hash, error, details_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id) DO UPDATE SET
                  source = excluded.source,
                  source_url = excluded.source_url,
                  status = excluded.status,
                  started_at = excluded.started_at,
                  completed_at = excluded.completed_at,
                  source_date = excluded.source_date,
                  row_count = excluded.row_count,
                  raw_hash = excluded.raw_hash,
                  schema_hash = excluded.schema_hash,
                  error = excluded.error,
                  details_json = excluded.details_json
                """,
                (
                    run.run_id, run.source, run.source_url, run.status,
                    run.started_at, run.completed_at, run.source_date,
                    int(run.row_count), run.raw_hash, run.schema_hash,
                    run.error, run.details_json,
                ),
            )
            previous = c.execute(
                "SELECT consecutive_failures FROM source_health WHERE source = ?",
                (run.source,),
            ).fetchone()
            healthy = run.status in {"ok", "partial"}
            if run.status in {"running", "pending"}:
                failures = int(previous[0] if previous else 0)
            else:
                failures = 0 if healthy else int(previous[0] if previous else 0) + 1
            c.execute(
                """
                INSERT INTO source_health
                  (source, last_run_id, last_status, checked_at,
                   last_started_at, last_completed_at, last_source_date,
                   last_row_count, last_raw_hash, last_schema_hash,
                   consecutive_failures, last_error, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source) DO UPDATE SET
                  last_run_id = excluded.last_run_id,
                  last_status = excluded.last_status,
                  checked_at = excluded.checked_at,
                  last_started_at = excluded.last_started_at,
                  last_completed_at = excluded.last_completed_at,
                  last_source_date = excluded.last_source_date,
                  last_row_count = excluded.last_row_count,
                  last_raw_hash = excluded.last_raw_hash,
                  last_schema_hash = excluded.last_schema_hash,
                  consecutive_failures = excluded.consecutive_failures,
                  last_error = excluded.last_error,
                  updated_at = excluded.updated_at
                """,
                (
                    run.source, run.run_id, run.status, now,
                    run.started_at, run.completed_at, run.source_date,
                    int(run.row_count), run.raw_hash, run.schema_hash,
                    failures, run.error[:2000], now,
                ),
            )

    def upsert_source_document(self, row: SourceDocumentRow) -> None:
        """保存 raw manifest；同一 document_id 只能指向同一份內容 hash。"""
        now = now_tw().isoformat(timespec="seconds")
        row.created_at = row.created_at or now
        row.updated_at = row.updated_at or now
        row.metadata_json = row.metadata_json or "{}"
        row.quality_status = normalize_quality_status(row.quality_status)
        with self.transaction() as c:
            existing = c.execute(
                "SELECT content_hash FROM source_documents WHERE document_id = ?",
                (row.document_id,),
            ).fetchone()
            if existing and existing["content_hash"] not in {"", row.content_hash}:
                raise ValueError(
                    f"source document hash conflict: {row.document_id}"
                )
            c.execute(
                """
                INSERT INTO source_documents
                  (document_id, source, kind, source_ref, url, title, content_path,
                   content_hash, mime_type, as_of, published_at, retrieved_at,
                   valid_from, valid_to, ingestion_run_id, quality_status, metadata_json,
                   created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(document_id) DO UPDATE SET
                  source = excluded.source,
                  kind = excluded.kind,
                  source_ref = excluded.source_ref,
                  url = excluded.url,
                  title = excluded.title,
                  content_path = excluded.content_path,
                  content_hash = excluded.content_hash,
                  mime_type = excluded.mime_type,
                  as_of = excluded.as_of,
                  published_at = excluded.published_at,
                  retrieved_at = excluded.retrieved_at,
                  valid_from = excluded.valid_from,
                  valid_to = excluded.valid_to,
                  ingestion_run_id = CASE
                    WHEN excluded.ingestion_run_id <> '' THEN excluded.ingestion_run_id
                    ELSE source_documents.ingestion_run_id
                  END,
                  quality_status = CASE
                    WHEN excluded.quality_status <> 'unknown' THEN excluded.quality_status
                    ELSE source_documents.quality_status
                  END,
                  metadata_json = CASE
                    WHEN excluded.metadata_json <> '{}' THEN excluded.metadata_json
                    ELSE source_documents.metadata_json
                  END,
                  updated_at = excluded.updated_at
                """,
                (
                    row.document_id, row.source, row.kind, row.source_ref,
                    row.url, row.title, row.content_path, row.content_hash,
                    row.mime_type, row.as_of, row.published_at, row.retrieved_at,
                    row.valid_from, row.valid_to, row.ingestion_run_id,
                    row.quality_status,
                    row.metadata_json, row.created_at, row.updated_at,
                ),
            )

    def get_source_document(self, document_id: str) -> Optional[SourceDocumentRow]:
        row = self.conn.execute(
            "SELECT * FROM source_documents WHERE document_id = ?",
            (document_id,),
        ).fetchone()
        return _row_to_dc(SourceDocumentRow, row) if row else None

    def list_source_documents(
        self,
        *,
        source: Optional[str] = None,
        kind: Optional[str] = None,
        ingestion_run_id: Optional[str] = None,
        limit: int = 200,
    ) -> List[SourceDocumentRow]:
        sql = "SELECT * FROM source_documents WHERE 1=1"
        args: List[Any] = []
        if source:
            sql += " AND source = ?"
            args.append(source)
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        if ingestion_run_id:
            sql += " AND ingestion_run_id = ?"
            args.append(ingestion_run_id)
        sql += " ORDER BY retrieved_at DESC, updated_at DESC LIMIT ?"
        args.append(max(1, int(limit)))
        rows = self.conn.execute(sql, args).fetchall()
        return [_row_to_dc(SourceDocumentRow, row) for row in rows]

    def upsert_ingestion_run(self, row: IngestionRunRow) -> None:
        now = now_tw().isoformat(timespec="seconds")
        row.started_at = row.started_at or now
        row.metadata_json = row.metadata_json or "{}"
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO ingestion_runs
                  (run_id, source, source_run_id, status, started_at,
                   completed_at, document_count, error, metadata_json)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id) DO UPDATE SET
                  source = excluded.source,
                  source_run_id = excluded.source_run_id,
                  status = excluded.status,
                  started_at = excluded.started_at,
                  completed_at = excluded.completed_at,
                  document_count = excluded.document_count,
                  error = excluded.error,
                  metadata_json = excluded.metadata_json
                """,
                (
                    row.run_id, row.source, row.source_run_id, row.status,
                    row.started_at, row.completed_at, int(row.document_count),
                    row.error, row.metadata_json,
                ),
            )

    def get_ingestion_run(self, run_id: str) -> Optional[IngestionRunRow]:
        row = self.conn.execute(
            "SELECT * FROM ingestion_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return _row_to_dc(IngestionRunRow, row) if row else None

    def list_ingestion_runs(
        self,
        *,
        source: Optional[str] = None,
        source_run_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[IngestionRunRow]:
        sql = "SELECT * FROM ingestion_runs WHERE 1=1"
        args: List[Any] = []
        if source:
            sql += " AND source = ?"
            args.append(source)
        if source_run_id:
            sql += " AND source_run_id = ?"
            args.append(source_run_id)
        sql += " ORDER BY started_at DESC LIMIT ?"
        args.append(max(1, int(limit)))
        rows = self.conn.execute(sql, args).fetchall()
        return [_row_to_dc(IngestionRunRow, row) for row in rows]

    def get_source_health(self, source: str) -> Optional[SourceHealthRow]:
        row = self.conn.execute(
            "SELECT * FROM source_health WHERE source = ?",
            (str(source or ""),),
        ).fetchone()
        return _row_to_dc(SourceHealthRow, row) if row else None

    def list_source_health(self, *, limit: int = 200) -> List[SourceHealthRow]:
        rows = self.conn.execute(
            "SELECT * FROM source_health ORDER BY checked_at DESC, source LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return [_row_to_dc(SourceHealthRow, row) for row in rows]

    def list_source_runs(
        self,
        *,
        source: Optional[str] = None,
        limit: int = 100,
    ) -> List[SourceRun]:
        sql = "SELECT * FROM source_runs WHERE 1=1"
        args: List[Any] = []
        if source:
            sql += " AND source = ?"
            args.append(source)
        sql += " ORDER BY completed_at DESC LIMIT ?"
        args.append(int(limit))
        rows = self.conn.execute(sql, args).fetchall()
        return [_row_to_dc(SourceRun, r) for r in rows]

    def upsert_classification(self, item: EntityClassification) -> None:
        now = now_tw().isoformat(timespec="seconds")
        if not item.created_at:
            item.created_at = now
        if not item.updated_at:
            item.updated_at = now
        item.confidence = max(0.0, min(1.0, float(item.confidence)))
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO entity_classifications
                  (id, entity_type, entity_id, taxonomy, label, confidence,
                   source, source_ref, valid_from, valid_to, status,
                   created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                  entity_type = excluded.entity_type,
                  entity_id = excluded.entity_id,
                  taxonomy = excluded.taxonomy,
                  label = excluded.label,
                  confidence = excluded.confidence,
                  source = excluded.source,
                  source_ref = excluded.source_ref,
                  valid_from = excluded.valid_from,
                  valid_to = excluded.valid_to,
                  status = excluded.status,
                  updated_at = excluded.updated_at
                """,
                (
                    item.id, item.entity_type, item.entity_id, item.taxonomy,
                    item.label, item.confidence, item.source, item.source_ref,
                    item.valid_from, item.valid_to, item.status,
                    item.created_at, item.updated_at,
                ),
            )

    def list_classifications(
        self,
        entity_id: Optional[str] = None,
        *,
        entity_type: Optional[str] = None,
        taxonomy: Optional[str] = None,
        active_only: bool = True,
    ) -> List[EntityClassification]:
        sql = "SELECT * FROM entity_classifications WHERE 1=1"
        args: List[Any] = []
        if entity_id:
            sql += " AND entity_id = ?"
            args.append(entity_id)
        if entity_type:
            sql += " AND entity_type = ?"
            args.append(entity_type)
        if taxonomy:
            sql += " AND taxonomy = ?"
            args.append(taxonomy)
        if active_only:
            sql += " AND status = 'active'"
        sql += " ORDER BY taxonomy, label"
        rows = self.conn.execute(sql, args).fetchall()
        return [_row_to_dc(EntityClassification, r) for r in rows]


# ----------------------------------------------------------------------
# 模組層 helpers
# ----------------------------------------------------------------------


_singleton_lock = threading.Lock()
_singleton: Optional[StockDB] = None


def get_db(path: Optional[Path] = None, root: Optional[Path] = None) -> StockDB:
    """模組層單例 (給 dashboard / cli 共用)。"""
    global _singleton
    requested = Path(path) if path is not None else default_db_path(root)
    with _singleton_lock:
        # ``root`` 也會改變實際 DB；只比較 ``path`` 會讓不同專案根目錄
        # 意外共用同一個 singleton，進而把研究結果寫進另一個專案。
        same_path = (
            _singleton is not None
            and Path(_singleton.path).resolve() == requested.resolve()
        )
        if not same_path:
            if _singleton is not None:
                _singleton.close()
            _singleton = StockDB.open(path=requested)
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
    if col == "quality_status":
        return "unknown"
    # 簡單規則：數值欄位給 0，其餘給空字串
    numeric_cols = {
        "capital", "shares_outstanding",
        "revenue", "yoy_pct", "mom_pct", "cumulative", "cum_yoy_pct",
        "eps", "gross_margin", "op_margin", "net_margin",
        "open", "high", "low", "close", "volume", "amount",
        "last_synced_rows",
    }
    return 0 if col in numeric_cols else ""


def _prepare_provenance(item: Any, *, ohlcv: bool = False) -> None:
    """在 DAO 邊界補本機取得時間並標準化品質狀態。"""
    if not getattr(item, "retrieved_at", ""):
        item.retrieved_at = now_tw().isoformat(timespec="seconds")
    item.quality_status = normalize_quality_status(
        getattr(item, "quality_status", "unknown")
    )
    if ohlcv:
        errors = validate_ohlcv(
            open_price=getattr(item, "open", 0),
            high=getattr(item, "high", 0),
            low=getattr(item, "low", 0),
            close=getattr(item, "close", 0),
            volume=getattr(item, "volume", 0),
            amount=getattr(item, "amount", 0),
        )
        if errors:
            item.quality_status = "invalid"
            if any("is not finite" in error for error in errors):
                raise ValueError("拒絕寫入非有限 OHLCV 數值: " + "; ".join(errors))
            if hasattr(item, "note"):
                note = str(getattr(item, "note", "") or "")
                marker = "quality: " + "; ".join(errors)
                item.note = f"{note}; {marker}".strip("; ")


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
    "DividendHistory",
    "EtfMeta",
    "EntityClassification",
    "EvidenceItemRow",
    "IngestionRunRow",
    "LlmAnalysisRow",
    "LlmDailyReportRow",
    "MemoryEventRow",
    "MemoryItemRow",
    "MemoryProposalRow",
    "MonthlyRevenue",
    "ValuationDaily",
    "PriceBar",
    "QuarterlyReport",
    "SchemaMigrationRow",
    "StockDB",
    "StockInfo",
    "SourceHealthRow",
    "SourceRun",
    "SourceDocumentRow",
    "SyncConflictRow",
    "SyncMeta",
    "WatchlistRow",
    "default_db_path",
    "backup_stock_db",
    "get_db",
    "reset_db_singleton",
]
