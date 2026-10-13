"""cloud_sync -- 把 SQLite 各 table 與 Google Sheets 雙向同步。

設計
====
* **每張 table 對應一個 worksheet** (例如 `stock_info`, `watchlist`...)
* **欄位名稱完全一致** — Sheets 第一列是欄位名，其餘列為資料
* **gspread + service account** — 無需互動式 OAuth，CI/排程也能跑
* **雙向同步**：
  - `push(table)`：本地 → 雲端 (覆蓋整張 worksheet)
  - `pull(table)`：雲端 → 本地 (用 REPLACE 寫回 SQLite)
  - `sync(table)`：依「updated_at 最大值」判斷哪邊新；新的為勝
  - `sync_row_level(table)`：依主鍵逐列合併，適合歷史資料與多端同時修改
* **離線降級**：缺 `gspread` 或缺 SA 憑證時，模組仍可 import；呼叫同步會丟可讀錯誤
* **支援 Google Forms**：Forms 的回覆會自動寫進 Sheets，pull 時會一併進 DB

衝突處理策略
============
1. **table-level (相容模式)**：以整張表為單位比 `MAX(updated_at)`，誰新誰贏。
2. **row-level (建議模式)**：以資料表主鍵逐列比對 `updated_at`；只把較新的
  一列保留下來。時間相同但內容不同時保留本地列，並在本地
  ``sync_conflicts`` 保存 local／remote 兩份 row 與決策；結果 note 只提供
  快速摘要，避免無法追查的隨機覆蓋。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from bot.canonical_audit import CANONICAL_TABLES, audit_canonical_rows
from bot.stock_db import (
    ALL_TABLES,
    SYNCABLE_TABLES,
    StockDB,
    get_db,
)
from bot.utils import get_logger, now_tw


# ----------------------------------------------------------------------
# Lazy import gspread —— 缺套件時要能給出可讀錯誤
# ----------------------------------------------------------------------


class CloudSyncDependencyError(RuntimeError):
    """套件未安裝 / 認證資訊不齊全。"""


def _import_gspread():
    try:
        import gspread  # type: ignore[import-not-found]
        from google.oauth2.service_account import (  # type: ignore[import-not-found]
            Credentials,
        )
    except ImportError as e:
        raise CloudSyncDependencyError(
            "需要安裝雲端同步套件：`uv pip install gspread google-auth` "
            f"(原始錯誤：{e})"
        ) from e
    return gspread, Credentials


# Google Sheets API 必需的 scopes
_DEFAULT_SCOPES: Tuple[str, ...] = (
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
)


# ----------------------------------------------------------------------
# 設定資料模型
# ----------------------------------------------------------------------


@dataclass
class CloudConfig:
    """同步所需的最小設定。可由 .env 透過 from_settings() 載入。"""

    sheet_id: str = ""
    service_account_json: str = ""   # 檔案路徑 或 JSON 內文
    scopes: Tuple[str, ...] = _DEFAULT_SCOPES

    @property
    def enabled(self) -> bool:
        return bool(self.sheet_id) and bool(self.service_account_json)

    def credentials_dict(self) -> Dict[str, Any]:
        """允許 service_account_json 是路徑或直接 JSON 內文。"""
        s = self.service_account_json.strip()
        if not s:
            raise CloudSyncDependencyError("未設定 service account JSON")
        if s.startswith("{"):
            return json.loads(s)
        p = Path(s).expanduser()
        if not p.exists():
            raise CloudSyncDependencyError(f"Service account 檔案不存在：{p}")
        return json.loads(p.read_text(encoding="utf-8"))


@dataclass
class TableSyncResult:
    table: str
    direction: str       # "push" / "pull" / "sync-push" / "sync-pull" / "noop"
    rows: int = 0
    error: str = ""
    skipped: bool = False
    note: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass(frozen=True)
class RowConflict:
    table: str
    key: Tuple[str, ...]
    local_updated_at: str = ""
    remote_updated_at: str = ""
    decision: str = "local"
    local_row: Dict[str, Any] = field(default_factory=dict)
    remote_row: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RowMergeResult:
    rows: List[Dict[str, Any]] = field(default_factory=list)
    local_only: int = 0
    remote_only: int = 0
    conflicts: List[RowConflict] = field(default_factory=list)


# 同步表的真正資料身份。不能用 updated_at 或整張表最大時間代替，否則
# 新增一筆歷史 K 線會把另一端新增的其他日期整批覆蓋掉。
ROW_PRIMARY_KEYS: Dict[str, Tuple[str, ...]] = {
    "stock_info": ("symbol",),
    "etf_meta": ("symbol",),
    "monthly_revenue": ("symbol", "year_month"),
    "valuation_daily": ("symbol", "as_of"),
    "dividend_history": ("symbol", "year"),
    "quarterly_report": ("symbol", "period"),
    "watchlist": ("symbol",),
    "price_history": ("symbol", "date"),
    "llm_analysis_history": ("id",),
    "llm_daily_reports": ("report_type", "report_date", "mode"),
    "intraday_bars": ("symbol", "ts", "interval"),
}

# Google Sheets is an editable mirror, not the immutable evidence store. A
# pull must never accept provenance/evidence metadata from the sheet as if it
# proved a newly edited fact. Existing local metadata is retained only when
# the actual row values are unchanged; edited or new rows become explicitly
# unprovenanced instead of carrying stale or fabricated source IDs.
_PROVENANCE_COLUMNS: Dict[str, Tuple[str, ...]] = {
    table: (
        "source_url", "source_date", "retrieved_at", "quality_status",
        "source_record_id",
    )
    for table in (
        "stock_info", "etf_meta", "monthly_revenue", "valuation_daily",
        "dividend_history", "quarterly_report", "price_history",
        "intraday_bars",
    )
}
_PROVENANCE_COLUMNS["price_history"] += ("source",)
_PROVENANCE_COLUMNS["intraday_bars"] += ("source",)
_LLM_PROVENANCE_COLUMNS: Dict[str, Tuple[str, ...]] = {
    "llm_analysis_history": ("evidence_json", "quality_status", "source_record_id"),
    "llm_daily_reports": ("evidence_json", "quality_status", "source_record_id"),
}
_PROTECTED_COLUMNS = {**_PROVENANCE_COLUMNS, **_LLM_PROVENANCE_COLUMNS}


def _row_identity(
    table: str,
    row: Dict[str, Any],
    *,
    side: str,
    index: int,
) -> Tuple[str, ...]:
    columns = ROW_PRIMARY_KEYS.get(table, ("id",))
    values = tuple("" if row.get(c) is None else str(row.get(c)).strip() for c in columns)
    # 畸形列不能因為所有 key 都是空字串而互相吞掉；保留為 side-specific，
    # 讓 replace/upsert 的錯誤仍能被上層明確看見。
    if not all(values):
        return ("__missing_key__", side, str(index))
    return values


def _timestamp_value(value: Any) -> dt.datetime:
    text = "" if value is None else str(value).strip()
    if not text:
        return dt.datetime.min
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
        return parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
    except ValueError:
        # 不可解析的時間不應該贏過可解析的時間；同時保留原字串供衝突報告。
        return dt.datetime.min


def _sanitize_remote_rows(
    table: str,
    local_rows: Iterable[Dict[str, Any]],
    remote_rows: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Remove untrusted sheet provenance and preserve unchanged local metadata.

    Raw manifests and evidence tables are intentionally local-only. A sheet
    row can carry a copied ID for display, but it cannot establish that an
    edited value is still supported by that ID. When a row changes, clear all
    protected metadata and make its quality explicit as ``unknown``.
    """
    protected = _PROTECTED_COLUMNS.get(table)
    if not protected:
        return [dict(row) for row in remote_rows]

    local_map: Dict[Tuple[str, ...], Dict[str, Any]] = {}
    for index, row in enumerate(local_rows):
        local_map[_row_identity(table, row, side="local", index=index)] = dict(row)

    sanitized: List[Dict[str, Any]] = []
    for index, raw in enumerate(remote_rows):
        remote = dict(raw)
        local = local_map.get(
            _row_identity(table, remote, side="remote", index=index)
        )
        comparable_columns = [
            column for column in remote
            if column not in protected and column != "updated_at"
        ]
        unchanged = bool(local is not None) and all(
            _cell_repr(remote.get(column, "")) == _cell_repr(local.get(column, ""))
            for column in comparable_columns
        )
        if unchanged and local is not None:
            for column in protected:
                if column in remote:
                    remote[column] = local.get(column, "")
        else:
            for column in protected:
                if column in remote:
                    remote[column] = (
                        "unknown" if column == "quality_status"
                        else "[]" if column == "evidence_json"
                        else ""
                    )
        sanitized.append(remote)
    return sanitized


def _validate_remote_rows(table: str, rows: Iterable[Dict[str, Any]]) -> None:
    """Reject semantically invalid canonical rows before any DB replacement."""
    if table not in CANONICAL_TABLES:
        return
    audit = audit_canonical_rows(table, rows)
    if audit.quality_status == "invalid":
        first = audit.findings[0].message if audit.findings else "canonical row invalid"
        raise ValueError(f"拒絕同步 {table}：{first}")


def _prefer_row(
    left: Dict[str, Any],
    right: Dict[str, Any],
    table: str = "",
) -> Tuple[Dict[str, Any], str]:
    if table == "watchlist":
        from bot.watchlist_repository import choose_watchlist_row
        selected = choose_watchlist_row(left, right)
        if selected is not None:
            return selected, "left" if selected is left else "right"
    left_ts = _timestamp_value(left.get("updated_at"))
    right_ts = _timestamp_value(right.get("updated_at"))
    if right_ts > left_ts:
        return right, "right"
    return left, "left"


def merge_rows_by_key(
    table: str,
    local_rows: Iterable[Dict[str, Any]],
    remote_rows: Iterable[Dict[str, Any]],
) -> RowMergeResult:
    """以 table 主鍵逐列合併，回傳結果與可稽核的同時修改衝突。

    同一端若意外帶有重複主鍵，也依 ``updated_at`` 留較新的列；時間相同時
    保留先出現的列，結果仍可由資料源 audit 找出重複問題。
    """
    local_map: Dict[Tuple[str, ...], Dict[str, Any]] = {}
    remote_map: Dict[Tuple[str, ...], Dict[str, Any]] = {}
    for index, row in enumerate(local_rows):
        key = _row_identity(table, row, side="local", index=index)
        if key not in local_map:
            local_map[key] = dict(row)
        else:
            local_map[key], _ = _prefer_row(local_map[key], dict(row), table)
    for index, row in enumerate(remote_rows):
        key = _row_identity(table, row, side="remote", index=index)
        if key not in remote_map:
            remote_map[key] = dict(row)
        else:
            remote_map[key], _ = _prefer_row(remote_map[key], dict(row), table)

    ordered_keys = list(local_map)
    ordered_keys.extend(key for key in remote_map if key not in local_map)
    merged: List[Dict[str, Any]] = []
    conflicts: List[RowConflict] = []
    local_only = remote_only = 0
    for key in ordered_keys:
        local = local_map.get(key)
        remote = remote_map.get(key)
        if local is None:
            merged.append(remote or {})
            remote_only += 1
            continue
        if remote is None:
            merged.append(local)
            local_only += 1
            continue
        if local == remote:
            merged.append(local)
            continue
        local_ts = _timestamp_value(local.get("updated_at"))
        remote_ts = _timestamp_value(remote.get("updated_at"))
        selected, side = _prefer_row(local, remote, table)
        merged.append(selected)
        decision = "remote" if side == "right" else "local"
        if remote_ts == local_ts:
            conflicts.append(RowConflict(
                table=table,
                key=key,
                local_updated_at=str(local.get("updated_at") or ""),
                remote_updated_at=str(remote.get("updated_at") or ""),
                decision=decision,
                local_row=dict(local),
                remote_row=dict(remote),
            ))
    return RowMergeResult(
        rows=merged,
        local_only=local_only,
        remote_only=remote_only,
        conflicts=conflicts,
    )


# ----------------------------------------------------------------------
# 主類別
# ----------------------------------------------------------------------


class GoogleSheetSync:
    """gspread-based 雙向同步。

    使用範例
    --------
    >>> sync = GoogleSheetSync(CloudConfig(sheet_id="...", service_account_json="sa.json"))
    >>> sync.push_all()                # 本地 → 雲端 (全部 syncable tables)
    >>> sync.pull_all()                # 雲端 → 本地
    >>> sync.sync_all()                # 雙向自動 (依 updated_at)
    """

    def __init__(
        self,
        cfg: CloudConfig,
        db: Optional[StockDB] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.cfg = cfg
        self.db = db or get_db()
        self.log = logger or get_logger("cloud_sync")
        self._client = None
        self._sheet = None

    # ----- 連線 / 認證 -----

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if not self.cfg.enabled:
            raise CloudSyncDependencyError(
                "雲端同步未啟用 — 請在 .env 設定 GOOGLE_SHEET_ID 與 "
                "GOOGLE_SA_JSON_PATH"
            )
        gspread, Credentials = _import_gspread()
        creds_dict = self.cfg.credentials_dict()
        creds = Credentials.from_service_account_info(
            creds_dict, scopes=list(self.cfg.scopes),
        )
        self._client = gspread.authorize(creds)
        return self._client

    def _ensure_sheet(self):
        if self._sheet is not None:
            return self._sheet
        client = self._ensure_client()
        try:
            self._sheet = client.open_by_key(self.cfg.sheet_id)
        except Exception as e:
            raise CloudSyncDependencyError(
                f"無法開啟 Google Sheet (id={self.cfg.sheet_id[:10]}...): {e}. "
                "請確認 SA email 已被加為 Sheet 編輯者。"
            ) from e
        return self._sheet

    def ping(self) -> str:
        """檢測連線是否健康，回傳 sheet 的 title。"""
        sh = self._ensure_sheet()
        return sh.title

    @staticmethod
    def _unsupported_table_result(
        table: str,
        direction: str,
    ) -> TableSyncResult | None:
        """Prevent the public API from syncing local-only tables.

        The CLI already validates against ``SYNCABLE_TABLES``.  The class is
        also used directly by dashboard/jobs, so the same safety boundary must
        exist here instead of relying on one caller to enforce it.
        """
        if table in SYNCABLE_TABLES:
            return None
        if table in ALL_TABLES:
            return TableSyncResult(
                table=table,
                direction=direction,
                error=(
                    f"禁止同步 local-only table: {table}；"
                    "raw/evidence/research 只保留在本地 SQLite"
                ),
                skipped=True,
            )
        return TableSyncResult(
            table=table,
            direction=direction,
            error=f"未知或不可同步 table: {table}",
            skipped=True,
        )

    # ----- worksheet helpers -----

    def _get_or_create_worksheet(
        self,
        table: str,
        columns: List[str],
        *,
        repair_header: bool = True,
    ):
        sh = self._ensure_sheet()
        try:
            ws = sh.worksheet(table)
        except Exception:
            ws = sh.add_worksheet(
                title=table,
                rows=max(100, 10),
                cols=max(len(columns), 10),
            )
            ws.update("A1", [columns])
        else:
            existing = ws.row_values(1)
            if not existing:
                if repair_header:
                    ws.update("A1", [columns])
            elif existing != columns:
                if table == "watchlist" and existing == columns[:6]:
                    # Legacy schema is a known prefix. Read using that header;
                    # only the complete merged write may add the new columns.
                    return ws
                if not repair_header:
                    raise ValueError(
                        f"worksheet {table} 欄位不符；為保護資料，拒絕自動改寫 header"
                    )
                ws.update("A1", [columns])
        return ws

    def _worksheet_to_rows(self, ws) -> Tuple[List[str], List[Dict[str, Any]]]:
        values: List[List[str]] = ws.get_all_values()
        if not values:
            return [], []
        header = values[0]
        rows: List[Dict[str, Any]] = []
        for raw in values[1:]:
            if not any(c.strip() for c in raw):
                continue
            row = {h: (raw[i] if i < len(raw) else "") for i, h in enumerate(header)}
            rows.append(row)
        return header, rows

    # ----- push (本地 → 雲端) -----

    def push(self, table: str) -> TableSyncResult:
        if table == "watchlist":
            return self.sync_row_level(table)
        unsupported = self._unsupported_table_result(table, "push")
        if unsupported is not None:
            return unsupported
        try:
            cols = self.db._table_columns(table)
            rows = self.db.fetch_all_rows(table)
            ws = self._get_or_create_worksheet(table, cols)
            count = self._write_rows(ws, cols, rows)
            self.db.mark_pushed(table, count)
            return TableSyncResult(table=table, direction="push", rows=count)
        except Exception as e:
            self.log.exception("push table=%s 失敗", table)
            self.db.mark_sync_error(table, str(e))
            return TableSyncResult(table=table, direction="push", error=str(e))

    def push_all(self, tables: Optional[Iterable[str]] = None) -> List[TableSyncResult]:
        tables = list(tables or SYNCABLE_TABLES)
        return [self.push(t) for t in tables]

    # ----- pull (雲端 → 本地) -----

    def pull(self, table: str) -> TableSyncResult:
        unsupported = self._unsupported_table_result(table, "pull")
        if unsupported is not None:
            return unsupported
        try:
            cols_local = self.db._table_columns(table)
            ws = self._get_or_create_worksheet(
                table, cols_local, repair_header=False,
            )
            header, rows = self._worksheet_to_rows(ws)
            if not header:
                return TableSyncResult(table=table, direction="pull",
                                        rows=0, skipped=True,
                                        note="雲端 worksheet 是空的；保留本地資料")
            if header != cols_local and not (table == "watchlist" and header == cols_local[:6]):
                return TableSyncResult(
                    table=table,
                    direction="pull",
                    error=f"雲端欄位與本地 schema 不符: {table}",
                    skipped=True,
                )
            if not rows:
                return TableSyncResult(
                    table=table,
                    direction="pull",
                    rows=0,
                    skipped=True,
                    note="雲端只有 header；為避免誤清空，保留本地資料",
                )
            typed_rows = [_coerce_row(table, cols_local, r) for r in rows]
            local_rows = self.db.fetch_all_rows(table)
            safe_rows = _sanitize_remote_rows(table, local_rows, typed_rows)
            if table == "watchlist":
                safe_rows = merge_rows_by_key(table, local_rows, safe_rows).rows
            _validate_remote_rows(table, safe_rows)
            n = self.db.replace_table_rows(table, safe_rows, expected_rows=local_rows)
            self.db.mark_pulled(table, n)
            return TableSyncResult(table=table, direction="pull", rows=n)
        except Exception as e:
            self.log.exception("pull table=%s 失敗", table)
            self.db.mark_sync_error(table, str(e))
            return TableSyncResult(table=table, direction="pull", error=str(e))

    def pull_all(self, tables: Optional[Iterable[str]] = None) -> List[TableSyncResult]:
        tables = list(tables or SYNCABLE_TABLES)
        return [self.pull(t) for t in tables]

    def _write_rows(self, ws, columns: List[str], rows: Iterable[Dict[str, Any]]) -> int:
        materialized = list(rows)
        data = [columns] + [
            [_cell_repr(row.get(column, "")) for column in columns]
            for row in materialized
        ]
        # Do not clear first: a network failure between clear/update would
        # otherwise leave the remote worksheet empty.  Write the new payload
        # first, then remove only rows that are no longer needed.
        old_row_count = len(ws.get_all_values())
        ws.update("A1", data, value_input_option="RAW")
        new_row_count = len(data)
        if old_row_count > new_row_count:
            start_row = new_row_count + 1
            end_row = old_row_count
            end_column = _column_label(len(columns))
            cell_range = f"A{start_row}:{end_column}{end_row}"
            if hasattr(ws, "batch_clear"):
                ws.batch_clear([cell_range])
            elif hasattr(ws, "delete_rows"):
                ws.delete_rows(start_row, end_row)
            else:
                raise RuntimeError(
                    "worksheet 不支援安全清除多餘列；已完成新資料寫入，"
                    f"但尚未清理 {cell_range}"
                )
        return len(materialized)

    def sync_row_level(self, table: str) -> TableSyncResult:
        """依主鍵合併 local/remote，保留兩端各自新增的歷史列。

        Watchlist 的 sync/push 固定走此路徑以保留刪除標記。其他表仍可
        明確選擇舊的整表 sync，CLI 預設使用逐列合併。
        """
        unsupported = self._unsupported_table_result(table, "sync-row-level")
        if unsupported is not None:
            return unsupported
        if table not in ROW_PRIMARY_KEYS:
            return TableSyncResult(
                table=table,
                direction="sync-row-level",
                error=f"table 沒有 row-level 主鍵設定: {table}",
                skipped=True,
            )
        try:
            columns = self.db._table_columns(table)
            ws = self._get_or_create_worksheet(
                table, columns, repair_header=False,
            )
            remote_header, remote_raw = self._worksheet_to_rows(ws)
            remote_rows = [_coerce_row(table, columns, row) for row in remote_raw]
            local_rows = self.db.fetch_all_rows(table)
            safe_remote_rows = _sanitize_remote_rows(table, local_rows, remote_rows)
            merge = merge_rows_by_key(table, local_rows, safe_remote_rows)
            for conflict in merge.conflicts:
                self.db.record_sync_conflict(
                    table_name=conflict.table,
                    row_key=conflict.key,
                    local_row=conflict.local_row,
                    remote_row=conflict.remote_row,
                    decision=conflict.decision,
                    local_updated_at=conflict.local_updated_at,
                    remote_updated_at=conflict.remote_updated_at,
                )
            _validate_remote_rows(table, merge.rows)

            def signature(rows: Iterable[Dict[str, Any]]) -> List[Tuple[Any, ...]]:
                return sorted(
                    [tuple(_cell_repr(row.get(column, "")) for column in columns)
                     for row in rows],
                    key=repr,
                )

            local_changed = signature(local_rows) != signature(merge.rows)
            remote_changed = remote_header != columns or signature(remote_rows) != signature(merge.rows)
            if local_changed:
                n_local = self.db.replace_table_rows(table, merge.rows, expected_rows=local_rows)
                self.db.mark_pulled(table, n_local)
            if remote_changed:
                n_remote = self._write_rows(ws, columns, merge.rows)
                self.db.mark_pushed(table, n_remote)

            if local_changed and remote_changed:
                direction = "sync-merge"
            elif local_changed:
                direction = "sync-merge-pull"
            elif remote_changed:
                direction = "sync-merge-push"
            else:
                direction = "noop"
            note = (
                f"local_only={merge.local_only}, remote_only={merge.remote_only}, "
                f"conflicts={len(merge.conflicts)}"
            )
            return TableSyncResult(
                table=table,
                direction=direction,
                rows=len(merge.rows),
                note=note,
            )
        except Exception as e:
            self.log.exception("row-level sync table=%s 失敗", table)
            self.db.mark_sync_error(table, str(e))
            return TableSyncResult(table=table, direction="sync-row-level", error=str(e))

    # ----- sync (雙向自動，依 updated_at 最大值) -----

    def sync(self, table: str) -> TableSyncResult:
        if table == "watchlist":
            return self.sync_row_level(table)
        unsupported = self._unsupported_table_result(table, "sync")
        if unsupported is not None:
            return unsupported
        try:
            cols_local = self.db._table_columns(table)
            ws = self._get_or_create_worksheet(
                table, cols_local, repair_header=False,
            )
            _, remote_rows = self._worksheet_to_rows(ws)
            local_rows = self.db.fetch_all_rows(table)
            typed_remote_rows = [_coerce_row(table, cols_local, r) for r in remote_rows]
            safe_remote_rows = _sanitize_remote_rows(
                table, local_rows, typed_remote_rows,
            )
            _validate_remote_rows(table, safe_remote_rows)

            local_ts = _max_updated_at(local_rows)
            remote_ts = _max_updated_at(safe_remote_rows)

            if remote_ts > local_ts:
                n = self.db.replace_table_rows(table, safe_remote_rows)
                self.db.mark_pulled(table, n)
                return TableSyncResult(
                    table=table, direction="sync-pull", rows=n,
                    note=f"remote={remote_ts}, local={local_ts}",
                )
            elif local_ts > remote_ts:
                count = self._write_rows(ws, cols_local, local_rows)
                self.db.mark_pushed(table, count)
                return TableSyncResult(
                    table=table, direction="sync-push", rows=count,
                    note=f"remote={remote_ts}, local={local_ts}",
                )
            else:
                return TableSyncResult(
                    table=table, direction="noop",
                    rows=len(local_rows),
                    note=f"已同步 (updated_at={local_ts or '∅'})",
                )
        except Exception as e:
            self.log.exception("sync table=%s 失敗", table)
            self.db.mark_sync_error(table, str(e))
            return TableSyncResult(table=table, direction="sync", error=str(e))

    def sync_all(
        self,
        tables: Optional[Iterable[str]] = None,
        *,
        row_level: bool = True,
    ) -> List[TableSyncResult]:
        tables = list(tables or SYNCABLE_TABLES)
        fn = self.sync_row_level if row_level else self.sync
        return [fn(t) for t in tables]


# ----------------------------------------------------------------------
# Helper - 從 Settings 載入 CloudConfig
# ----------------------------------------------------------------------


def load_config_from_env(root: Optional[Path] = None) -> CloudConfig:
    """從 .env 透過 pydantic Settings 載入雲端設定。

    失敗時回傳 enabled=False 的空設定 (而非丟例外)，讓 UI 可以顯示「未啟用」。
    """
    try:
        from bot.config import Settings  # 延遲 import 避免循環
        project_root = (root or Path.cwd()).resolve()
        env_file = str(project_root / ".env")
        s = Settings(_env_file=env_file)  # type: ignore[call-arg]
        service_account = str(getattr(s, "google_sa_json_path", "") or "")
        if service_account and not service_account.lstrip().startswith("{"):
            service_path = Path(service_account).expanduser()
            if not service_path.is_absolute():
                service_account = str((project_root / service_path).resolve())
        return CloudConfig(
            sheet_id=str(getattr(s, "google_sheet_id", "") or ""),
            service_account_json=service_account,
        )
    except Exception:
        return CloudConfig()


# ----------------------------------------------------------------------
# 內部 utility — 型別轉換
# ----------------------------------------------------------------------


def _column_label(number: int) -> str:
    """Convert a positive 1-based column number to an A1 label."""
    if number <= 0:
        raise ValueError("column number must be positive")
    labels: List[str] = []
    value = number
    while value:
        value, remainder = divmod(value - 1, 26)
        labels.append(chr(ord("A") + remainder))
    return "".join(reversed(labels))


_INT_COLS = {
    "shares_outstanding", "last_synced_rows",
}

_FLOAT_COLS = {
    "capital",
    "revenue", "yoy_pct", "mom_pct", "cumulative", "cum_yoy_pct",
    "eps", "pe_ratio", "pb_ratio", "dividend_yield",
    "cash_dividend", "stock_dividend", "payout_ratio",
    "gross_margin", "op_margin", "net_margin",
    # price_history (OHLCV)
    "open", "high", "low", "close", "volume",
}


def _coerce_row(
    table: str,
    cols: List[str],
    raw: Dict[str, Any],
    *,
    strict: bool = True,
) -> Dict[str, Any]:
    """把 Sheets 拿回來的字串型 row 轉成符合 schema 的型別。"""
    out: Dict[str, Any] = {}
    for c in cols:
        v = raw.get(c, "")
        if isinstance(v, str):
            v = v.strip()
        if c in _INT_COLS:
            try:
                out[c] = int(float(v)) if v != "" else 0
            except (TypeError, ValueError):
                if strict:
                    raise ValueError(f"{table}.{c} 不是有效整數: {v!r}") from None
                out[c] = 0
        elif c in _FLOAT_COLS:
            try:
                out[c] = float(v) if v != "" else 0.0
            except (TypeError, ValueError):
                if strict:
                    raise ValueError(f"{table}.{c} 不是有效數值: {v!r}") from None
                out[c] = 0.0
        else:
            out[c] = "" if v is None else str(v)
    return out


def _cell_repr(v: Any) -> Any:
    """把 Python 值轉成 Sheets 友善的儲存格內容。"""
    if v is None:
        return ""
    if isinstance(v, (int, float, str)):
        return v
    return str(v)


def _max_updated_at(rows: Iterable[Dict[str, Any]]) -> str:
    best = ""
    for r in rows:
        v = r.get("updated_at", "")
        if isinstance(v, str) and v > best:
            best = v
    return best


__all__ = [
    "CloudConfig",
    "CloudSyncDependencyError",
    "GoogleSheetSync",
    "ROW_PRIMARY_KEYS",
    "RowConflict",
    "RowMergeResult",
    "TableSyncResult",
    "load_config_from_env",
    "merge_rows_by_key",
    "_sanitize_remote_rows",
    "_validate_remote_rows",
]
