"""cloud_sync 測試 — 用 mock worksheet 跑 push / pull / sync。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from bot.cloud_sync import (
    CloudConfig,
    CloudSyncDependencyError,
    GoogleSheetSync,
    TableSyncResult,
    _coerce_row,
    _max_updated_at,
    _sanitize_remote_rows,
    load_config_from_env,
    merge_rows_by_key,
)
from bot.stock_db import (
    SYNCABLE_TABLES,
    LlmDailyReportRow,
    PriceBar,
    StockDB,
    StockInfo,
    WatchlistRow,
)


@pytest.fixture
def db(tmp_path: Path) -> StockDB:
    return StockDB.open(path=tmp_path / "cloud.db")


@pytest.fixture
def sync(db: StockDB) -> GoogleSheetSync:
    cfg = CloudConfig(sheet_id="fake", service_account_json='{"type":"service_account"}')
    s = GoogleSheetSync(cfg, db=db)
    return s


def test_load_config_resolves_relative_service_account_from_root(tmp_path: Path) -> None:
    secret = tmp_path / ".secrets" / "service-account.json"
    secret.parent.mkdir(parents=True)
    secret.write_text('{"type":"service_account"}', encoding="utf-8")
    (tmp_path / ".env").write_text(
        "GOOGLE_SHEET_ID=test-sheet\nGOOGLE_SA_JSON_PATH=.secrets/service-account.json\n",
        encoding="utf-8",
    )

    cfg = load_config_from_env(tmp_path)
    assert cfg.enabled is True
    assert Path(cfg.service_account_json) == secret.resolve()
    assert cfg.credentials_dict()["type"] == "service_account"


class FakeWorksheet:
    """模擬 gspread worksheet 行為。"""

    def __init__(self, values: List[List[str]] | None = None) -> None:
        self.values: List[List[str]] = values or []
        self.updated: List[tuple] = []
        self.cleared: bool = False

    def get_all_values(self) -> List[List[str]]:
        return [list(r) for r in self.values]

    def row_values(self, row: int) -> List[str]:
        return list(self.values[row - 1]) if 0 < row <= len(self.values) else []

    def clear(self) -> None:
        self.cleared = True
        self.values = []

    def update(self, addr: str, data, value_input_option: str = "RAW") -> None:
        self.updated.append((addr, data))
        if addr.upper().startswith("A1"):
            while len(self.values) < len(data):
                self.values.append([])
            for index, row in enumerate(data):
                self.values[index] = list(row)

    def batch_clear(self, ranges: List[str]) -> None:
        for _range in ranges:
            start, end = _range.split(":")
            start_row = int("".join(ch for ch in start if ch.isdigit()))
            end_row = int("".join(ch for ch in end if ch.isdigit()))
            for index in range(start_row - 1, min(end_row, len(self.values))):
                self.values[index] = []
            while self.values and not self.values[-1]:
                self.values.pop()


def _patch_get_or_create(sync: GoogleSheetSync, table_ws: Dict[str, FakeWorksheet]):
    def get_or_create(table: str, columns: List[str], **_kwargs) -> FakeWorksheet:
        ws = table_ws.setdefault(table, FakeWorksheet())
        if not ws.values:
            ws.values = [list(columns)]
        return ws
    sync._get_or_create_worksheet = get_or_create  # type: ignore[method-assign]


class TestPush:
    def test_push_writes_all_rows(self, sync: GoogleSheetSync, db: StockDB) -> None:
        db.upsert_stock_info(StockInfo(symbol="2330", name="台積電"))
        db.upsert_stock_info(StockInfo(symbol="0050", name="元大 50"))
        table_ws: Dict[str, FakeWorksheet] = {}
        _patch_get_or_create(sync, table_ws)

        result = sync.push("stock_info")
        assert result.ok
        assert result.rows == 2
        ws = table_ws["stock_info"]
        assert not ws.cleared
        header = ws.values[0]
        assert "symbol" in header and "name" in header

    def test_push_clears_trailing_rows_only_after_new_payload_is_written(
        self, sync: GoogleSheetSync, db: StockDB,
    ) -> None:
        db.upsert_stock_info(StockInfo(symbol="2330", name="本地新版"))
        columns = db._table_columns("stock_info")
        old_row = {column: "" for column in columns}
        old_row.update({"symbol": "9999", "name": "雲端舊列"})
        ws = FakeWorksheet([columns, [old_row.get(column, "") for column in columns]])
        _patch_get_or_create(sync, {"stock_info": ws})

        result = sync.push("stock_info")

        assert result.ok
        assert [row[0] for row in ws.values[1:]] == ["2330"]
        assert ws.updated

    def test_push_failure_keeps_existing_remote_rows(self, sync: GoogleSheetSync, db: StockDB) -> None:
        db.upsert_stock_info(StockInfo(symbol="2330", name="本地資料"))
        columns = db._table_columns("stock_info")
        old_values = [columns, ["9999", "雲端舊資料"] + ["" for _ in columns[2:]]]

        class FailingWorksheet(FakeWorksheet):
            def update(self, addr: str, data, value_input_option: str = "RAW") -> None:
                self.updated.append((addr, data))
                raise RuntimeError("simulated network failure")

        ws = FailingWorksheet(old_values)
        _patch_get_or_create(sync, {"stock_info": ws})

        result = sync.push("stock_info")

        assert not result.ok
        assert ws.values == old_values

    def test_push_unknown_table(self, sync: GoogleSheetSync) -> None:
        result = sync.push("not_a_table")
        assert result.skipped
        assert "未知或不可同步 table" in result.error

    def test_public_api_rejects_local_only_tables(self, sync: GoogleSheetSync) -> None:
        for operation in (sync.push, sync.pull, sync.sync, sync.sync_row_level):
            result = operation("evidence_items")
            assert result.skipped
            assert "local-only" in result.error


class TestPull:
    def test_pull_replaces_local(self, sync: GoogleSheetSync, db: StockDB) -> None:
        cols = db._table_columns("stock_info")
        header = cols
        row_2330 = ["2330", "台積電", "", "TWSE", "半導體業",
                    "", "1994-09-05", "2593", "0", "", "", "2026-05-28T16:00:00"]
        # pad to header length
        while len(row_2330) < len(header):
            row_2330.append("")
        row_0050 = ["0050", "元大50", "", "TWSE"] + ["" for _ in range(len(header) - 4)]
        ws = FakeWorksheet([header, row_2330, row_0050])
        table_ws = {"stock_info": ws}
        _patch_get_or_create(sync, table_ws)

        db.upsert_stock_info(StockInfo(symbol="9999", name="即將消失"))
        result = sync.pull("stock_info")
        assert result.ok
        assert result.rows == 2
        symbols = {s.symbol for s in db.list_stock_info()}
        assert symbols == {"2330", "0050"}

    def test_pull_empty_sheet(self, sync: GoogleSheetSync, db: StockDB) -> None:
        ws = FakeWorksheet([])
        _patch_get_or_create(sync, {"stock_info": ws})
        # 預先放 header 之後再清空
        ws.values = []
        result = sync.pull("stock_info")
        assert result.ok

    def test_pull_rejects_invalid_canonical_rows_without_mutating_db(
        self, sync: GoogleSheetSync, db: StockDB,
    ) -> None:
        db.upsert_price_bar(PriceBar(
            symbol="2330", date="2026-05-20", open=900, high=910,
            low=895, close=905, volume=100,
        ))
        cols = db._table_columns("price_history")
        raw = {c: "" for c in cols}
        raw.update({
            "symbol": "2330", "date": "2026-05-20", "open": "900",
            "high": "890", "low": "895", "close": "905", "volume": "100",
        })
        ws = FakeWorksheet([cols, [raw[c] for c in cols]])
        _patch_get_or_create(sync, {"price_history": ws})

        result = sync.pull("price_history")
        assert not result.ok
        bar = db.get_price_history("2330")[0]
        assert bar.high == pytest.approx(910)

    def test_pull_preserves_local_provenance_for_unchanged_row(
        self, sync: GoogleSheetSync, db: StockDB,
    ) -> None:
        db.upsert_price_bar(PriceBar(
            symbol="2330", date="2026-05-20", open=900, high=910,
            low=895, close=905, volume=100,
            quality_status="ok", source_record_id="source-doc:twse:known",
        ))
        cols = db._table_columns("price_history")
        raw = {c: "" for c in cols}
        raw.update({
            "symbol": "2330", "date": "2026-05-20", "open": "900",
            "high": "910", "low": "895", "close": "905", "volume": "100",
        })
        ws = FakeWorksheet([cols, [raw[c] for c in cols]])
        _patch_get_or_create(sync, {"price_history": ws})

        result = sync.pull("price_history")
        assert result.ok
        row = db.fetch_all_rows("price_history")[0]
        assert row["source_record_id"] == "source-doc:twse:known"
        assert row["quality_status"] == "ok"


def test_sanitize_remote_rows_strips_forged_provenance_on_new_row() -> None:
    rows = _sanitize_remote_rows(
        "price_history",
        [],
        [{
            "symbol": "2330", "date": "2026-05-20", "open": 900,
            "high": 910, "low": 895, "close": 905, "volume": 100,
            "source_record_id": "source-doc:forged:1", "quality_status": "ok",
        }],
    )
    assert rows[0]["source_record_id"] == ""
    assert rows[0]["quality_status"] == "unknown"


class TestSync:
    def test_all_syncable_tables_support_empty_roundtrip(
        self, sync: GoogleSheetSync,
    ) -> None:
        """每張預設同步表都必須能完成基本 push/pull/逐列同步。"""
        table_ws: Dict[str, FakeWorksheet] = {}
        _patch_get_or_create(sync, table_ws)

        failures = []
        for table in SYNCABLE_TABLES:
            operations = (
                ("push", sync.push(table)),
                ("pull", sync.pull(table)),
                ("row-level", sync.sync_row_level(table)),
            )
            failures.extend(
                (table, operation, result.error)
                for operation, result in operations
                if not result.ok
            )

        assert not failures, failures

    def test_remote_newer_triggers_pull(self, sync: GoogleSheetSync, db: StockDB) -> None:
        cols = db._table_columns("watchlist")
        # 本地有舊資料
        db.upsert_watch(WatchlistRow(symbol="2330", name="本地版"))
        # 雲端有新資料 (updated_at 更晚)
        header = cols
        future = (dt.datetime.now() + dt.timedelta(days=1)).isoformat(timespec="seconds")
        row = ["2330", "雲端版", "", "雲端的 note", future, future]
        while len(row) < len(header):
            row.append("")
        ws = FakeWorksheet([header, row])
        _patch_get_or_create(sync, {"watchlist": ws})

        result = sync.sync("watchlist")
        assert result.direction == "sync-merge-pull"
        latest = db.get_watch("2330")
        assert latest is not None
        assert latest.name == "雲端版"

    def test_local_newer_triggers_push(self, sync: GoogleSheetSync, db: StockDB) -> None:
        cols = db._table_columns("watchlist")
        db.upsert_watch(WatchlistRow(symbol="2330", name="本地版"))
        header = cols
        past = "2020-01-01T00:00:00"
        row = ["2330", "雲端舊版", "", "", past, past]
        while len(row) < len(header):
            row.append("")
        ws = FakeWorksheet([header, row])
        _patch_get_or_create(sync, {"watchlist": ws})

        result = sync.sync("watchlist")
        assert result.direction == "sync-merge-push"
        # 雲端應該被寫入，但不需要先清空 worksheet
        assert ws.updated

    def test_equal_timestamps_noop(self, sync: GoogleSheetSync, db: StockDB) -> None:
        cols = db._table_columns("watchlist")
        ts = "2026-05-28T16:00:00"
        db.upsert_watch(WatchlistRow(symbol="2330", name="X"))
        # 修改本地 updated_at 對齊雲端
        db.conn.execute(
            "UPDATE watchlist SET updated_at = ? WHERE symbol = ?",
            (ts, "2330"),
        )
        header = cols
        row = [db.fetch_all_rows("watchlist")[0][col] for col in header]
        ws = FakeWorksheet([header, row])
        _patch_get_or_create(sync, {"watchlist": ws})

        result = sync.sync("watchlist")
        assert result.direction == "noop"


class TestCoerce:
    def test_int_field(self) -> None:
        out = _coerce_row("stock_info", ["shares_outstanding"], {"shares_outstanding": "1234"})
        assert out["shares_outstanding"] == 1234

    def test_float_field(self) -> None:
        out = _coerce_row("stock_info", ["capital"], {"capital": "2593.5"})
        assert out["capital"] == pytest.approx(2593.5)

    def test_invalid_numeric_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="capital"):
            _coerce_row("stock_info", ["capital", "shares_outstanding"], {
                "capital": "abc", "shares_outstanding": "",
            })

    def test_price_ohlcv_fields_are_float(self) -> None:
        cols = ["open", "high", "low", "close", "volume"]
        out = _coerce_row("price_history", cols, {
            "open": "900.5", "high": "910", "low": "895.2",
            "close": "905", "volume": "15000",
        })
        assert out["open"] == pytest.approx(900.5)
        assert out["high"] == pytest.approx(910.0)
        assert out["low"] == pytest.approx(895.2)
        assert out["close"] == pytest.approx(905.0)
        assert out["volume"] == pytest.approx(15000.0)


class TestPriceHistorySync:
    def test_push_price_history(self, sync: GoogleSheetSync, db: StockDB) -> None:
        db.bulk_upsert_price_bars([
            PriceBar(symbol="2330", date="2026-05-20",
                     open=900, high=910, low=895, close=905, volume=15_000),
            PriceBar(symbol="2330", date="2026-05-21",
                     open=905, high=920, low=900, close=918, volume=22_000),
        ])
        table_ws: Dict[str, FakeWorksheet] = {}
        _patch_get_or_create(sync, table_ws)

        result = sync.push("price_history")
        assert result.ok
        assert result.rows == 2
        ws = table_ws["price_history"]
        header = ws.values[0]
        assert {"symbol", "date", "open", "high", "low", "close", "volume"}.issubset(
            set(header)
        )

    def test_pull_price_history_replaces_local(
        self, sync: GoogleSheetSync, db: StockDB,
    ) -> None:
        cols = db._table_columns("price_history")
        header = cols
        # 雲端兩筆，本地一筆 (將被覆蓋)
        db.upsert_price_bar(PriceBar(
            symbol="0050", date="2026-05-01", open=180, high=182, low=179,
            close=181, volume=5_000,
        ))
        row1 = []
        row2 = []
        defaults = {
            "symbol": "2330", "date": "2026-05-20",
            "open": "900", "high": "910", "low": "895",
            "close": "905", "volume": "15000",
            "source": "twse", "note": "",
            "updated_at": "2026-05-21T16:00:00",
        }
        defaults2 = {**defaults, "date": "2026-05-21",
                     "open": "905", "high": "920", "low": "900",
                     "close": "918", "volume": "22000"}
        for c in header:
            row1.append(defaults.get(c, ""))
            row2.append(defaults2.get(c, ""))
        ws = FakeWorksheet([header, row1, row2])
        _patch_get_or_create(sync, {"price_history": ws})

        result = sync.pull("price_history")
        assert result.ok
        assert result.rows == 2
        # 本地舊資料消失，新資料寫入
        assert db.get_price_history("0050") == []
        bars = db.get_price_history("2330")
        assert [b.date for b in bars] == ["2026-05-20", "2026-05-21"]
        assert bars[0].open == pytest.approx(900.0)
        assert bars[1].close == pytest.approx(918.0)


class TestMaxUpdated:
    def test_picks_lexicographic_max(self) -> None:
        rows = [
            {"updated_at": "2026-01-01T00:00:00"},
            {"updated_at": "2026-05-28T16:00:00"},
            {"updated_at": "2025-12-31T23:59:59"},
        ]
        assert _max_updated_at(rows) == "2026-05-28T16:00:00"

    def test_empty(self) -> None:
        assert _max_updated_at([]) == ""


class TestRowMerge:
    def test_preserves_independent_historical_rows(self) -> None:
        result = merge_rows_by_key(
            "price_history",
            [{"symbol": "2330", "date": "2026-01-01", "close": 900,
              "updated_at": "2026-01-02T00:00:00"}],
            [{"symbol": "2330", "date": "2026-01-02", "close": 910,
              "updated_at": "2026-01-03T00:00:00"}],
        )
        assert len(result.rows) == 2
        assert result.local_only == 1
        assert result.remote_only == 1

    def test_newer_row_wins_and_equal_timestamp_is_reported(self) -> None:
        newer = merge_rows_by_key(
            "watchlist",
            [{"symbol": "2330", "name": "local", "updated_at": "2026-01-01T00:00:00"}],
            [{"symbol": "2330", "name": "remote", "updated_at": "2026-01-02T00:00:00"}],
        )
        assert newer.rows[0]["name"] == "remote"
        conflict = merge_rows_by_key(
            "watchlist",
            [{"symbol": "2330", "name": "local", "updated_at": "2026-01-02T00:00:00"}],
            [{"symbol": "2330", "name": "remote", "updated_at": "2026-01-02T00:00:00"}],
        )
        assert conflict.rows[0]["name"] == "local"
        assert len(conflict.conflicts) == 1

    def test_row_level_sync_writes_union_back_to_both_sides(
        self, sync: GoogleSheetSync, db: StockDB,
    ) -> None:
        db.upsert_price_bar(PriceBar(
            symbol="2330", date="2026-01-01", open=900, high=910,
            low=890, close=905, volume=1000,
        ))
        columns = db._table_columns("price_history")
        remote_values = {
            "symbol": "2330", "date": "2026-01-02", "open": "905",
            "high": "920", "low": "900", "close": "918", "volume": "1200",
            "updated_at": "2026-01-03T00:00:00",
        }
        remote_row = [remote_values.get(column, "") for column in columns]
        ws = FakeWorksheet([columns, remote_row])
        _patch_get_or_create(sync, {"price_history": ws})

        result = sync.sync_row_level("price_history")
        assert result.direction == "sync-merge"
        assert result.rows == 2
        assert {bar.date for bar in db.get_price_history("2330")} == {
            "2026-01-01", "2026-01-02",
        }
        assert len(ws.values) == 3

    def test_equal_timestamp_conflict_is_persisted_for_review(
        self, sync: GoogleSheetSync, db: StockDB,
    ) -> None:
        db.upsert_watch(WatchlistRow(symbol="2330", name="local"))
        ts = "2026-05-28T16:00:00"
        db.conn.execute(
            "UPDATE watchlist SET updated_at = ? WHERE symbol = ?",
            (ts, "2330"),
        )
        columns = db._table_columns("watchlist")
        remote = {
            "symbol": "2330",
            "name": "remote",
            "tags": "",
            "note": "",
            "added_at": ts,
            "updated_at": ts,
        }
        ws = FakeWorksheet([columns, [remote.get(column, "") for column in columns]])
        _patch_get_or_create(sync, {"watchlist": ws})

        result = sync.sync_row_level("watchlist")

        assert result.ok
        assert "conflicts=1" in result.note
        conflicts = db.list_sync_conflicts("watchlist")
        assert len(conflicts) == 1
        assert conflicts[0].status == "open"
        assert '"name": "local"' in conflicts[0].local_row_json
        assert '"name": "remote"' in conflicts[0].remote_row_json
        assert db.resolve_sync_conflict(
            conflicts[0].conflict_id,
            resolution="人工確認保留 local",
        ) is True
        assert db.list_sync_conflicts("watchlist", status="resolved")[0].resolution == "人工確認保留 local"


class TestLlmDailyReports:
    def test_push_llm_daily_reports(self, sync: GoogleSheetSync, db: StockDB) -> None:
        db.upsert_llm_daily_report(
            LlmDailyReportRow(
                report_type="next_day_watch",
                report_date="2026-06-04",
                mode="draft",
                asof="2026-06-03",
                generated_at="2026-06-03T18:00:00",
                market_tone="risk_on",
                brief_md="# brief",
                payload_json='{"target_date":"2026-06-04"}',
                updated_at="2026-06-03T18:00:00",
            )
        )
        table_ws: Dict[str, FakeWorksheet] = {}
        _patch_get_or_create(sync, table_ws)

        result = sync.push("llm_daily_reports")
        assert result.ok
        assert result.rows == 1
        ws = table_ws["llm_daily_reports"]
        assert ws.updated
        assert "next_day_watch" in str(ws.values)


class TestConfigDisabled:
    def test_disabled_raises(self, db: StockDB) -> None:
        cfg = CloudConfig()
        sync = GoogleSheetSync(cfg, db=db)
        with pytest.raises(CloudSyncDependencyError):
            sync._ensure_client()


@pytest.mark.parametrize("operation", ["sync", "sync_row_level", "pull", "push"])
def test_watchlist_tombstone_survives_stale_remote(operation, sync, db):
    db.upsert_watch(WatchlistRow(symbol="2330", note="before"))
    stale = db.fetch_all_rows("watchlist")[0]
    db.remove_watch("2330")
    # Even a clock-skewed old client cannot resurrect a deleted item.
    stale["updated_at"] = "2099-01-01T00:00:00+08:00"
    columns = db._table_columns("watchlist")
    ws = FakeWorksheet([columns, [stale.get(c, "") for c in columns]])
    _patch_get_or_create(sync, {"watchlist": ws})
    assert getattr(sync, operation)("watchlist").ok
    assert db.get_watch("2330") is None
    assert db.fetch_all_rows("watchlist")[0]["deleted_at"]


def test_explicit_readd_wins_its_observed_tombstone(sync, db):
    db.upsert_watch(WatchlistRow(symbol="2330"))
    db.remove_watch("2330")
    tombstone = db.fetch_all_rows("watchlist")[0]
    db.upsert_watch(WatchlistRow(symbol="2330", note="readded"))
    columns = db._table_columns("watchlist")
    ws = FakeWorksheet([columns, [tombstone[c] for c in columns]])
    _patch_get_or_create(sync, {"watchlist": ws})
    assert sync.sync_row_level("watchlist").ok
    assert db.get_watch("2330").note == "readded"
    assert ws.values[1][columns.index("deleted_at")] == ""


def test_watchlist_legacy_header_is_upgraded_with_rows(sync, db, monkeypatch):
    legacy = db._table_columns("watchlist")[:6]
    ws = FakeWorksheet([legacy, ["2330", "legacy", "", "keep", "", ""]])
    sheet = MagicMock()
    sheet.worksheet.return_value = ws
    monkeypatch.setattr(sync, "_ensure_sheet", lambda: sheet)
    assert sync.sync_row_level("watchlist").ok
    assert db.get_watch("2330").note == "keep"
    assert ws.values[0] == db._table_columns("watchlist")
    assert len(ws.updated) == 1  # No header-only rewrite ahead of the data.


def test_sync_rejects_local_edits_after_snapshot(sync, db, monkeypatch):
    columns = db._table_columns("watchlist")
    remote = {"symbol": "REMOTE", "note": "remote"}
    ws = FakeWorksheet([columns, [remote.get(c, "") for c in columns]])
    _patch_get_or_create(sync, {"watchlist": ws})
    original = db.replace_table_rows
    def concurrent_edit(table, rows, **kwargs):
        db.upsert_watch(WatchlistRow(symbol="LOCAL", note="keep"))
        return original(table, rows, **kwargs)
    monkeypatch.setattr(db, "replace_table_rows", concurrent_edit)
    result = sync.sync_row_level("watchlist")
    assert not result.ok
    assert db.get_watch("LOCAL").note == "keep"
    assert not ws.updated


def test_merge_compares_instants_with_different_timezones():
    local = {"symbol": "2330", "note": "older", "updated_at": "2026-10-07T10:00:00+08:00"}
    remote = {"symbol": "2330", "note": "newer", "updated_at": "2026-10-07T03:00:00Z"}
    assert merge_rows_by_key("watchlist", [local], [remote]).rows[0]["note"] == "newer"
