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
* **離線降級**：缺 `gspread` 或缺 SA 憑證時，模組仍可 import；呼叫同步會丟可讀錯誤
* **支援 Google Forms**：Forms 的回覆會自動寫進 Sheets，pull 時會一併進 DB

衝突處理策略
============
1. **table-level**：以整張表為單位比 `MAX(updated_at)`，誰新誰贏
2. **row-level (進階)**：未實作 — 簡單可靠優先；若需要更精細，
   可未來新增 `merge(table)` 方法做 per-row 比對
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bot.stock_db import (
    ALL_TABLES,
    SYNCABLE_TABLES,
    StockDB,
    get_db,
)
from bot.utils import get_logger

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
_DEFAULT_SCOPES: tuple[str, ...] = (
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
    scopes: tuple[str, ...] = _DEFAULT_SCOPES

    @property
    def enabled(self) -> bool:
        return bool(self.sheet_id) and bool(self.service_account_json)

    def credentials_dict(self) -> dict[str, Any]:
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
        db: StockDB | None = None,
        logger: logging.Logger | None = None,
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

    # ----- worksheet helpers -----

    def _get_or_create_worksheet(self, table: str, columns: list[str]):
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
            if existing != columns:
                ws.update("A1", [columns])
        return ws

    def _worksheet_to_rows(self, ws) -> tuple[list[str], list[dict[str, Any]]]:
        values: list[list[str]] = ws.get_all_values()
        if not values:
            return [], []
        header = values[0]
        rows: list[dict[str, Any]] = []
        for raw in values[1:]:
            if not any(c.strip() for c in raw):
                continue
            row = {h: (raw[i] if i < len(raw) else "") for i, h in enumerate(header)}
            rows.append(row)
        return header, rows

    # ----- push (本地 → 雲端) -----

    def push(self, table: str) -> TableSyncResult:
        if table not in ALL_TABLES:
            return TableSyncResult(table=table, direction="push",
                                    error=f"未知 table: {table}", skipped=True)
        try:
            cols = self.db._table_columns(table)
            rows = self.db.fetch_all_rows(table)
            ws = self._get_or_create_worksheet(table, cols)
            ws.clear()
            data = [cols] + [
                [_cell_repr(r.get(c, "")) for c in cols] for r in rows
            ]
            ws.update("A1", data, value_input_option="RAW")
            self.db.mark_pushed(table, len(rows))
            return TableSyncResult(table=table, direction="push", rows=len(rows))
        except Exception as e:
            self.log.exception("push table=%s 失敗", table)
            self.db.mark_sync_error(table, str(e))
            return TableSyncResult(table=table, direction="push", error=str(e))

    def push_all(self, tables: Iterable[str] | None = None) -> list[TableSyncResult]:
        tables = list(tables or SYNCABLE_TABLES)
        return [self.push(t) for t in tables]

    # ----- pull (雲端 → 本地) -----

    def pull(self, table: str) -> TableSyncResult:
        if table not in ALL_TABLES:
            return TableSyncResult(table=table, direction="pull",
                                    error=f"未知 table: {table}", skipped=True)
        try:
            cols_local = self.db._table_columns(table)
            ws = self._get_or_create_worksheet(table, cols_local)
            header, rows = self._worksheet_to_rows(ws)
            if not header:
                return TableSyncResult(table=table, direction="pull",
                                        rows=0, note="雲端 worksheet 是空的")
            typed_rows = [_coerce_row(table, cols_local, r) for r in rows]
            n = self.db.replace_table_rows(table, typed_rows)
            self.db.mark_pulled(table, n)
            return TableSyncResult(table=table, direction="pull", rows=n)
        except Exception as e:
            self.log.exception("pull table=%s 失敗", table)
            self.db.mark_sync_error(table, str(e))
            return TableSyncResult(table=table, direction="pull", error=str(e))

    def pull_all(self, tables: Iterable[str] | None = None) -> list[TableSyncResult]:
        tables = list(tables or SYNCABLE_TABLES)
        return [self.pull(t) for t in tables]

    # ----- sync (雙向自動，依 updated_at 最大值) -----

    def sync(self, table: str) -> TableSyncResult:
        if table not in ALL_TABLES:
            return TableSyncResult(table=table, direction="sync",
                                    error=f"未知 table: {table}", skipped=True)
        try:
            cols_local = self.db._table_columns(table)
            ws = self._get_or_create_worksheet(table, cols_local)
            _, remote_rows = self._worksheet_to_rows(ws)
            local_rows = self.db.fetch_all_rows(table)

            local_ts = _max_updated_at(local_rows)
            remote_ts = _max_updated_at(remote_rows)

            if remote_ts > local_ts:
                typed_rows = [_coerce_row(table, cols_local, r) for r in remote_rows]
                n = self.db.replace_table_rows(table, typed_rows)
                self.db.mark_pulled(table, n)
                return TableSyncResult(
                    table=table, direction="sync-pull", rows=n,
                    note=f"remote={remote_ts}, local={local_ts}",
                )
            elif local_ts > remote_ts:
                ws.clear()
                data = [cols_local] + [
                    [_cell_repr(r.get(c, "")) for c in cols_local]
                    for r in local_rows
                ]
                ws.update("A1", data, value_input_option="RAW")
                self.db.mark_pushed(table, len(local_rows))
                return TableSyncResult(
                    table=table, direction="sync-push", rows=len(local_rows),
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

    def sync_all(self, tables: Iterable[str] | None = None) -> list[TableSyncResult]:
        tables = list(tables or SYNCABLE_TABLES)
        return [self.sync(t) for t in tables]


# ----------------------------------------------------------------------
# Helper - 從 Settings 載入 CloudConfig
# ----------------------------------------------------------------------


def load_config_from_env() -> CloudConfig:
    """從 .env 透過 pydantic Settings 載入雲端設定。

    失敗時回傳 enabled=False 的空設定 (而非丟例外)，讓 UI 可以顯示「未啟用」。
    """
    try:
        from bot.config import Settings  # 延遲 import 避免循環
        s = Settings()  # type: ignore[call-arg]
        return CloudConfig(
            sheet_id=str(getattr(s, "google_sheet_id", "") or ""),
            service_account_json=str(getattr(s, "google_sa_json_path", "") or ""),
        )
    except Exception:
        return CloudConfig()


# ----------------------------------------------------------------------
# 內部 utility — 型別轉換
# ----------------------------------------------------------------------


_INT_COLS = {
    "shares_outstanding", "last_synced_rows",
}

_FLOAT_COLS = {
    "capital",
    "revenue", "yoy_pct", "mom_pct", "cumulative", "cum_yoy_pct",
    "eps", "gross_margin", "op_margin", "net_margin",
    # price_history (OHLCV)
    "open", "high", "low", "close", "volume",
}


def _coerce_row(table: str, cols: list[str], raw: dict[str, Any]) -> dict[str, Any]:
    """把 Sheets 拿回來的字串型 row 轉成符合 schema 的型別。"""
    out: dict[str, Any] = {}
    for c in cols:
        v = raw.get(c, "")
        if isinstance(v, str):
            v = v.strip()
        if c in _INT_COLS:
            try:
                out[c] = int(float(v)) if v != "" else 0
            except (TypeError, ValueError):
                out[c] = 0
        elif c in _FLOAT_COLS:
            try:
                out[c] = float(v) if v != "" else 0.0
            except (TypeError, ValueError):
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


def _max_updated_at(rows: Iterable[dict[str, Any]]) -> str:
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
    "TableSyncResult",
    "load_config_from_env",
]
