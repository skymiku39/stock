"""stock_db DAO 基本測試。

聚焦在純本地 SQLite 行為 (Cache-Aside / upsert / sync_meta)；雲端同步用
mock 在 test_cloud_sync.py 另寫，避免測試需要真實的 Google 認證。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from bot.stock_db import (
    ALL_TABLES,
    SYNCABLE_TABLES,
    EtfMeta,
    MonthlyRevenue,
    PriceBar,
    QuarterlyReport,
    StockDB,
    StockInfo,
    WatchlistRow,
)


@pytest.fixture
def db(tmp_path: Path) -> StockDB:
    return StockDB.open(path=tmp_path / "test.db")


class TestSchema:
    def test_creates_all_tables(self, db: StockDB) -> None:
        cur = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        names = [r["name"] for r in cur.fetchall()]
        for t in ALL_TABLES:
            assert t in names

    def test_sync_meta_pre_populated(self, db: StockDB) -> None:
        metas = db.list_sync_meta()
        table_names = {m.table_name for m in metas}
        for t in SYNCABLE_TABLES:
            assert t in table_names


class TestStockInfo:
    def test_upsert_then_get(self, db: StockDB) -> None:
        info = StockInfo(
            symbol="2330", name="台積電",
            industry="半導體業", market="TWSE",
            listed_date="1994-09-05", capital=2593.0,
        )
        db.upsert_stock_info(info)
        got = db.get_stock_info("2330")
        assert got is not None
        assert got.name == "台積電"
        assert got.industry == "半導體業"
        assert got.capital == pytest.approx(2593.0)
        assert got.updated_at  # 自動填入

    def test_upsert_overwrites(self, db: StockDB) -> None:
        db.upsert_stock_info(StockInfo(symbol="2330", name="舊名"))
        db.upsert_stock_info(StockInfo(symbol="2330", name="台積電"))
        got = db.get_stock_info("2330")
        assert got is not None and got.name == "台積電"

    def test_list_filter_by_industry(self, db: StockDB) -> None:
        db.upsert_stock_info(StockInfo(symbol="2330", industry="半導體業"))
        db.upsert_stock_info(StockInfo(symbol="2454", industry="半導體業"))
        db.upsert_stock_info(StockInfo(symbol="2881", industry="金融保險業"))
        semis = db.list_stock_info(industry="半導體業")
        assert {s.symbol for s in semis} == {"2330", "2454"}

    def test_cache_aside_hit(self, db: StockDB) -> None:
        db.upsert_stock_info(StockInfo(symbol="2330", name="台積電"))
        called = []

        def fetcher(sym: str):
            called.append(sym)
            return StockInfo(symbol=sym, name="不應該被呼叫")

        got = db.get_or_fetch_stock_info("2330", fetcher)
        assert got is not None and got.name == "台積電"
        assert called == []  # cache hit

    def test_cache_aside_miss(self, db: StockDB) -> None:
        called = []

        def fetcher(sym: str):
            called.append(sym)
            return StockInfo(symbol=sym, name="台積電")

        got = db.get_or_fetch_stock_info("2330", fetcher)
        assert got is not None and got.name == "台積電"
        assert called == ["2330"]
        assert db.get_stock_info("2330") is not None

    def test_cache_aside_stale_refetches(self, db: StockDB) -> None:
        old = (dt.datetime.now() - dt.timedelta(days=400)).isoformat(timespec="seconds")
        db.upsert_stock_info(StockInfo(symbol="2330", name="舊資料", updated_at=old))
        called = []

        def fetcher(sym: str):
            called.append(sym)
            return StockInfo(symbol=sym, name="新資料")

        got = db.get_or_fetch_stock_info("2330", fetcher, max_age_days=30)
        assert called == ["2330"]
        assert got is not None and got.name == "新資料"


class TestWatchlist:
    def test_upsert_and_list(self, db: StockDB) -> None:
        db.upsert_watch(WatchlistRow(symbol="2330", name="台積電", tags="半導體"))
        db.upsert_watch(WatchlistRow(symbol="0050", name="元大 50"))
        rows = db.list_watchlist()
        symbols = {r.symbol for r in rows}
        assert symbols == {"2330", "0050"}

    def test_remove(self, db: StockDB) -> None:
        db.upsert_watch(WatchlistRow(symbol="2330"))
        db.remove_watch("2330")
        assert db.get_watch("2330") is None


class TestMonthlyRevenue:
    def test_upsert_pk_pair(self, db: StockDB) -> None:
        db.upsert_monthly_revenue(MonthlyRevenue(
            symbol="2330", year_month="2026-04", revenue=300_000.0, yoy_pct=10.0,
        ))
        db.upsert_monthly_revenue(MonthlyRevenue(
            symbol="2330", year_month="2026-04", revenue=305_000.0, yoy_pct=11.0,
        ))
        rows = db.get_monthly_revenue("2330", "2026-04")
        assert len(rows) == 1
        assert rows[0].revenue == pytest.approx(305_000.0)

    def test_get_history_desc(self, db: StockDB) -> None:
        for m in ["2026-01", "2026-02", "2026-03"]:
            db.upsert_monthly_revenue(MonthlyRevenue(symbol="2330", year_month=m))
        rows = db.get_monthly_revenue("2330")
        assert [r.year_month for r in rows] == ["2026-03", "2026-02", "2026-01"]


class TestQuarterlyReport:
    def test_upsert_and_get(self, db: StockDB) -> None:
        db.upsert_quarterly_report(QuarterlyReport(
            symbol="2330", period="2026Q1",
            eps=10.0, gross_margin=58.0, op_margin=46.0, net_margin=40.0,
        ))
        rows = db.get_quarterly_report("2330", "2026Q1")
        assert len(rows) == 1
        assert rows[0].eps == pytest.approx(10.0)


class TestPriceHistory:
    def _sample_bars(self, symbol: str = "2330"):
        return [
            PriceBar(symbol=symbol, date="2026-05-20",
                     open=900, high=910, low=895, close=905, volume=15_000),
            PriceBar(symbol=symbol, date="2026-05-21",
                     open=905, high=920, low=900, close=918, volume=22_000),
            PriceBar(symbol=symbol, date="2026-05-22",
                     open=918, high=925, low=915, close=922, volume=18_000),
        ]

    def test_price_history_in_tables(self) -> None:
        assert "price_history" in ALL_TABLES
        assert "price_history" in SYNCABLE_TABLES

    def test_upsert_then_get(self, db: StockDB) -> None:
        for bar in self._sample_bars():
            db.upsert_price_bar(bar)
        rows = db.get_price_history("2330")
        assert [b.date for b in rows] == ["2026-05-20", "2026-05-21", "2026-05-22"]
        assert rows[-1].close == pytest.approx(922)
        # updated_at 自動填入
        assert all(b.updated_at for b in rows)

    def test_upsert_replaces_same_date(self, db: StockDB) -> None:
        db.upsert_price_bar(PriceBar(
            symbol="2330", date="2026-05-20",
            open=900, high=905, low=895, close=900, volume=10_000,
        ))
        # 重複寫入同日期 → 覆蓋
        db.upsert_price_bar(PriceBar(
            symbol="2330", date="2026-05-20",
            open=900, high=915, low=890, close=910, volume=12_000,
        ))
        rows = db.get_price_history("2330")
        assert len(rows) == 1
        assert rows[0].close == pytest.approx(910)
        assert rows[0].high == pytest.approx(915)

    def test_bulk_upsert_returns_count(self, db: StockDB) -> None:
        n = db.bulk_upsert_price_bars(self._sample_bars())
        assert n == 3
        assert len(db.get_price_history("2330")) == 3

    def test_bulk_upsert_empty(self, db: StockDB) -> None:
        assert db.bulk_upsert_price_bars([]) == 0

    def test_get_history_range(self, db: StockDB) -> None:
        db.bulk_upsert_price_bars(self._sample_bars())
        rows = db.get_price_history(
            "2330", start="2026-05-21", end="2026-05-22",
        )
        assert [b.date for b in rows] == ["2026-05-21", "2026-05-22"]

    def test_get_history_limit_returns_latest(self, db: StockDB) -> None:
        db.bulk_upsert_price_bars(self._sample_bars())
        rows = db.get_price_history("2330", limit=2, ascending=True)
        # 最新兩根 → 仍依日期升冪
        assert [b.date for b in rows] == ["2026-05-21", "2026-05-22"]

    def test_latest_price_date(self, db: StockDB) -> None:
        assert db.latest_price_date("2330") is None
        db.bulk_upsert_price_bars(self._sample_bars())
        assert db.latest_price_date("2330") == "2026-05-22"

    def test_list_price_symbols(self, db: StockDB) -> None:
        db.bulk_upsert_price_bars(self._sample_bars("2330"))
        db.bulk_upsert_price_bars(self._sample_bars("2454"))
        assert db.list_price_symbols() == ["2330", "2454"]

    def test_price_history_summary(self, db: StockDB) -> None:
        db.bulk_upsert_price_bars(self._sample_bars("2330"))
        db.bulk_upsert_price_bars(self._sample_bars("2454"))
        summary = db.price_history_summary()
        assert len(summary) == 2
        by_sym = {s["symbol"]: s for s in summary}
        assert by_sym["2330"]["rows"] == 3
        assert by_sym["2330"]["last_date"] == "2026-05-22"
        assert by_sym["2330"]["first_date"] == "2026-05-20"
        assert by_sym["2330"]["last_close"] == pytest.approx(922)

    def test_isolated_per_symbol(self, db: StockDB) -> None:
        db.bulk_upsert_price_bars(self._sample_bars("2330"))
        db.bulk_upsert_price_bars(self._sample_bars("2454"))
        rows_2330 = db.get_price_history("2330")
        rows_2454 = db.get_price_history("2454")
        assert len(rows_2330) == 3
        assert len(rows_2454) == 3
        assert all(b.symbol == "2330" for b in rows_2330)
        assert all(b.symbol == "2454" for b in rows_2454)


class TestSyncMeta:
    def test_mark_pushed(self, db: StockDB) -> None:
        db.mark_pushed("stock_info", 42)
        meta = db.get_sync_meta("stock_info")
        assert meta.last_push_at != ""
        assert meta.last_synced_rows == 42
        assert meta.last_error == ""

    def test_mark_sync_error_then_clear(self, db: StockDB) -> None:
        db.mark_sync_error("watchlist", "boom")
        assert db.get_sync_meta("watchlist").last_error == "boom"
        db.mark_pulled("watchlist", 3)
        meta = db.get_sync_meta("watchlist")
        assert meta.last_error == ""
        assert meta.last_synced_rows == 3


class TestBulkOps:
    def test_fetch_all_rows(self, db: StockDB) -> None:
        db.upsert_stock_info(StockInfo(symbol="2330"))
        db.upsert_stock_info(StockInfo(symbol="2454"))
        rows = db.fetch_all_rows("stock_info")
        assert len(rows) == 2
        assert {r["symbol"] for r in rows} == {"2330", "2454"}

    def test_replace_table_rows(self, db: StockDB) -> None:
        db.upsert_stock_info(StockInfo(symbol="2330"))
        n = db.replace_table_rows("stock_info", [
            {"symbol": "0050", "name": "元大 50"},
            {"symbol": "2881", "name": "富邦金"},
        ])
        assert n == 2
        rows = db.fetch_all_rows("stock_info")
        assert {r["symbol"] for r in rows} == {"0050", "2881"}

    def test_upsert_rows_keeps_locals(self, db: StockDB) -> None:
        db.upsert_stock_info(StockInfo(symbol="2330"))
        db.upsert_rows("stock_info", [
            {"symbol": "0050", "name": "元大 50"},
        ])
        rows = db.fetch_all_rows("stock_info")
        assert {r["symbol"] for r in rows} == {"2330", "0050"}
