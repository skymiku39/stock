"""cloud_sync 測試 — 用 mock worksheet 跑 push / pull / sync。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from bot.cloud_sync import (
    CloudConfig,
    CloudSyncDependencyError,
    GoogleSheetSync,
    _coerce_row,
    _max_updated_at,
)
from bot.stock_db import LlmDailyReportRow, PriceBar, StockDB, StockInfo, WatchlistRow


@pytest.fixture
def db(tmp_path: Path) -> StockDB:
    return StockDB.open(path=tmp_path / "cloud.db")


@pytest.fixture
def sync(db: StockDB) -> GoogleSheetSync:
    cfg = CloudConfig(sheet_id="fake", service_account_json='{"type":"service_account"}')
    s = GoogleSheetSync(cfg, db=db)
    return s


class FakeWorksheet:
    """模擬 gspread worksheet 行為。"""

    def __init__(self, values: list[list[str]] | None = None) -> None:
        self.values: list[list[str]] = values or []
        self.updated: list[tuple] = []
        self.cleared: bool = False

    def get_all_values(self) -> list[list[str]]:
        return [list(r) for r in self.values]

    def row_values(self, row: int) -> list[str]:
        return list(self.values[row - 1]) if 0 < row <= len(self.values) else []

    def clear(self) -> None:
        self.cleared = True
        self.values = []

    def update(self, addr: str, data, value_input_option: str = "RAW") -> None:
        self.updated.append((addr, data))
        if addr.upper().startswith("A1"):
            self.values = [list(r) for r in data]


def _patch_get_or_create(sync: GoogleSheetSync, table_ws: dict[str, FakeWorksheet]):
    def get_or_create(table: str, columns: list[str]) -> FakeWorksheet:
        ws = table_ws.setdefault(table, FakeWorksheet())
        if not ws.values:
            ws.values = [list(columns)]
        return ws
    sync._get_or_create_worksheet = get_or_create  # type: ignore[method-assign]


class TestPush:
    def test_push_writes_all_rows(self, sync: GoogleSheetSync, db: StockDB) -> None:
        db.upsert_stock_info(StockInfo(symbol="2330", name="台積電"))
        db.upsert_stock_info(StockInfo(symbol="0050", name="元大 50"))
        table_ws: dict[str, FakeWorksheet] = {}
        _patch_get_or_create(sync, table_ws)

        result = sync.push("stock_info")
        assert result.ok
        assert result.rows == 2
        ws = table_ws["stock_info"]
        assert ws.cleared
        header = ws.values[0]
        assert "symbol" in header and "name" in header

    def test_push_unknown_table(self, sync: GoogleSheetSync) -> None:
        result = sync.push("not_a_table")
        assert result.skipped
        assert "未知 table" in result.error


class TestPull:
    def test_pull_replaces_local(self, sync: GoogleSheetSync, db: StockDB) -> None:
        cols = db._table_columns("stock_info")
        header = cols
        row_2330 = ["2330", "台積電", "", "TWSE", "半導體業",
                    "", "1994-09-05", "2593", "0", "", "", "2026-05-28T16:00:00"]
        # pad to header length
        while len(row_2330) < len(header):
            row_2330.append("")
        row_0050 = ["0050"] + ["" for _ in range(len(header) - 1)]
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


class TestSync:
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
        assert result.direction == "sync-pull"
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
        assert result.direction == "sync-push"
        # 雲端應該被寫入
        assert ws.cleared

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
        row = ["2330", "X", "", "", ts, ts]
        while len(row) < len(header):
            row.append("")
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

    def test_invalid_falls_back_to_zero(self) -> None:
        out = _coerce_row("stock_info", ["capital", "shares_outstanding"], {
            "capital": "abc", "shares_outstanding": "",
        })
        assert out["capital"] == 0.0
        assert out["shares_outstanding"] == 0

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
        table_ws: dict[str, FakeWorksheet] = {}
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
        table_ws: dict[str, FakeWorksheet] = {}
        _patch_get_or_create(sync, table_ws)

        result = sync.push("llm_daily_reports")
        assert result.ok
        assert result.rows == 1
        ws = table_ws["llm_daily_reports"]
        assert ws.cleared
        assert "next_day_watch" in str(ws.values)


class TestConfigDisabled:
    def test_disabled_raises(self, db: StockDB) -> None:
        cfg = CloudConfig()
        sync = GoogleSheetSync(cfg, db=db)
        with pytest.raises(CloudSyncDependencyError):
            sync._ensure_client()
