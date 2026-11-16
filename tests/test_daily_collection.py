import datetime as dt
from types import SimpleNamespace

import pandas as pd
import pytest

from bot import daily_collection as collection

REAL_COLLECT_EX_RIGHTS = collection.collect_ex_rights


@pytest.fixture(autouse=True)
def network_side_collections_are_stubbed(monkeypatch):
    monkeypatch.setattr(collection, "collect_rss_archive", lambda root: {
        "status": "ok", "collected_at": collection.now_tw().isoformat(), "feeds_total": 1, "feeds_seen": 1})
    monkeypatch.setattr(collection, "collect_ex_rights", lambda root: {"status": "ok"})
    monkeypatch.setattr(collection, "crosscheck_prices", lambda root, date, expected=(): {"status": "ok", "date": date})


def test_retry_only_failed_steps_refresh_after_close_and_roll_over(tmp_path, monkeypatch):
    from bot import watchlist
    day = [dt.datetime(2026, 10, 27, 10, tzinfo=dt.timezone(dt.timedelta(hours=8)))]
    monkeypatch.setattr(collection, "now_tw", lambda: day[0])
    monkeypatch.setattr(watchlist, "load", lambda root: SimpleNamespace(items=[]))
    calls = []
    def news(*a):
        calls.append("news")
        if calls.count("news") == 1:
            raise ValueError("network unavailable")
        return {"status": "ok"}
    def price(root, ticker):
        calls.append(ticker)
        return {"status": "ok"}
    monkeypatch.setattr(collection, "collect_news", news)
    monkeypatch.setattr(collection, "collect_price", price)
    settings = SimpleNamespace(symbols=["2330"])
    first = collection.collect_daily_inputs(tmp_path, settings)
    assert not first["complete"] and first["prices"]["2330"]["status"] == "ok"
    assert collection.collect_daily_inputs(tmp_path, settings)["complete"]
    assert calls == ["news", "2330", "news"]
    assert collection.collect_daily_inputs(tmp_path, settings)["complete"]
    assert len(calls) == 3
    day[0] = day[0].replace(hour=15)
    assert collection.collect_daily_inputs(tmp_path, settings)["period"] == "after_close"
    assert calls[-2:] == ["news", "2330"]
    day[0] += dt.timedelta(days=1)
    assert collection.collect_daily_inputs(tmp_path, settings)["date"] == "2026-10-28"
    assert calls.count("2330") == 3
    assert (tmp_path / "data/daily_research/2026-10-27/daily-inputs.json").exists()


def test_today_news_is_counted_by_publication_date_not_cache_date(tmp_path, monkeypatch):
    def item(id, published):
        return SimpleNamespace(news_id=id, source_document_id="source", published_at=published,
                               title=id, url="https://example.test/" + id)
    items = [item("old", "2026-10-26T23:00:00+08:00"), item("new", "2026-10-27T09:00:00+08:00")]
    monkeypatch.setattr(collection, "fetch_today_news", lambda **kw: items)
    monkeypatch.setattr(collection, "load_cached_news_only", lambda **kw: items)
    monkeypatch.setattr(collection, "verify_source", lambda *a: None)
    result = collection.collect_news(tmp_path, "2026-10-27")
    assert result["cached_count"] == 2 and result["today_count"] == 1
    assert [n["title"] for n in result["headlines"]] == ["new"]
    monkeypatch.setattr(collection, "load_cached_news_only", lambda **kw: items[:1])
    with pytest.raises(ValueError, match="readback"):
        collection.collect_news(tmp_path, "2026-10-27")


def test_successful_news_refreshes_within_the_same_market_period(tmp_path, monkeypatch):
    from bot import watchlist
    clock = [dt.datetime(2026, 11, 4, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))]
    monkeypatch.setattr(collection, "now_tw", lambda: clock[0])
    monkeypatch.setattr(watchlist, "load", lambda root: SimpleNamespace(items=[]))
    calls = []
    monkeypatch.setattr(collection, "collect_news", lambda *a: calls.append("news") or {"status": "ok"})
    monkeypatch.setattr(collection, "collect_price", lambda *a: calls.append("price") or {"status": "ok"})
    settings = SimpleNamespace(symbols=["2330"])
    assert collection.collect_daily_inputs(tmp_path, settings)["complete"]
    clock[0] += dt.timedelta(minutes=4)
    assert collection.collect_daily_inputs(tmp_path, settings)["complete"]
    assert calls == ["news", "price"]
    clock[0] += dt.timedelta(minutes=1)
    assert collection.collect_daily_inputs(tmp_path, settings)["complete"]
    assert calls == ["news", "price", "news"]
    clock[0] += dt.timedelta(minutes=25)
    assert collection.collect_daily_inputs(tmp_path, settings)["complete"]
    assert calls[-2:] == ["news", "price"]


def test_price_fallback_is_not_a_successful_refresh(tmp_path, monkeypatch):
    now = dt.datetime(2026, 10, 27, 11, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    monkeypatch.setattr(collection, "now_tw", lambda: now)
    frame = pd.DataFrame([{"date": "2026-10-22", "open": 2380.0, "high": 2390.0,
                           "low": 2375.0, "close": 2385.0, "volume": 1234.5}])
    monkeypatch.setattr(collection, "fetch_recent_kline", lambda *a, **kw: frame)
    monkeypatch.setattr(collection, "load_kline_from_db", lambda *a, **kw: frame)
    bar = SimpleNamespace(date="2026-10-22", close=2385.0, source_record_id="source",
                          retrieved_at="2026-10-27T09:00:00+08:00")
    monkeypatch.setattr(collection.StockDB, "open", lambda **kw:
        SimpleNamespace(get_price_history=lambda *a, **kw: [bar], close=lambda: None))
    monkeypatch.setattr(collection, "verify_source", lambda *a: SimpleNamespace(url="https://example.test", document_id="source"))
    with pytest.raises(ValueError, match="older cached response"):
        collection.collect_price(tmp_path, "2330")
    bar.retrieved_at = now.isoformat()
    result = collection.collect_price(tmp_path, "2330")
    assert result["source_date"] == "2026-10-22"  # Never relabel a prior close as today's.
    assert result["status"] == "ok"


def test_summary_never_calls_intraday_snapshot_a_close(tmp_path):
    path = tmp_path / "data/daily_research/2026-10-27"
    path.mkdir(parents=True)
    state = {"date": "2026-10-27", "phase": "partial", "complete": False,
             "requested": 2, "completed": 1, "pending": ["0050"], "excluded": ["0050"],
             "daily_inputs": {"period": "intraday", "news": {}, "prices": {"2330": {
                 "status": "ok", "source_date": "2026-10-22", "close": 2385.0,
                 "retrieved_at": "2026-10-27T11:00:00+08:00", "source_url": "https://example.test"}}}}
    text = collection.write_daily_summary(tmp_path, state).read_text(encoding="utf-8")
    assert "盤中" in text and "來源日 2026-10-22" in text
    assert "資料摘要：1 / 2" in text and "完整：False" in text
    assert "研究條件齊備：0 / 2" in text


def test_price_collection_reads_real_database_and_registered_raw(tmp_path, monkeypatch):
    from bot.source_registry import SourceRegistry
    from bot.stock_db import PriceBar, StockDB
    now = dt.datetime(2026, 10, 27, 11, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    monkeypatch.setattr(collection, "now_tw", lambda: now)
    frame = pd.DataFrame([{"date": "2026-10-22", "open": 2380.0, "high": 2390.0,
                           "low": 2375.0, "close": 2385.0, "volume": 1234.5}])
    def fetch(*a, **kw):
        with SourceRegistry(tmp_path) as registry:
            source = registry.capture_document("twse_stock_day", '{"close":2385}',
                source_url="https://www.twse.com.tw/exchangeReport/STOCK_DAY", suffix="json")
        db = StockDB.open(root=tmp_path)
        try:
            db.upsert_price_bar(PriceBar(symbol="2330", date="2026-10-22", close=2385.0,
                open=2380.0, high=2390.0, low=2375.0, volume=1234.5,
                retrieved_at=now.isoformat(), source_record_id=source.document_id))
        finally:
            db.close()
        return frame
    monkeypatch.setattr(collection, "fetch_recent_kline", fetch)
    value = collection.collect_price(tmp_path, "2330")
    assert value["status"] == "ok" and value["source_date"] == "2026-10-22"
    raw = next((tmp_path / "data/raw/twse_stock_day").glob("*.json"))
    raw.write_text("corrupt", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        collection.verify_source(tmp_path, value["source_document_id"])


@pytest.mark.parametrize("field,value", [
    ("title", "different headline"), ("summary", "different summary"),
    ("published_at", "2026-11-03T09:00:00+08:00"),
    ("related_tickers", ["0050"]), ("source_document_id", "other-source"),
])
def test_news_readback_detects_content_changes_with_the_same_id(tmp_path, monkeypatch, field, value):
    from dataclasses import replace
    from bot.news_fetcher import NewsItem

    item = NewsItem(news_id="1", title="original", source_document_id="source",
                    published_at="2026-11-04T09:00:00+08:00", related_tickers=["2330"])
    monkeypatch.setattr(collection, "fetch_today_news", lambda **kw: [item])
    monkeypatch.setattr(collection, "load_cached_news_only", lambda **kw: [replace(item, **{field: value})])
    monkeypatch.setattr(collection, "verify_source", lambda *a: None)
    with pytest.raises(ValueError, match="readback"):
        collection.collect_news(tmp_path, "2026-11-04")


@pytest.mark.parametrize("field,value", [
    ("open", 2381.0), ("high", 2391.0), ("low", 2374.0),
    ("volume", 1235.5), ("close", float("nan")), ("high", float("inf")),
])
def test_price_readback_checks_every_ohlcv_value(tmp_path, monkeypatch, field, value):
    now = dt.datetime(2026, 11, 4, 17, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    frame = pd.DataFrame([{"date": "2026-11-04", "open": 2380.0, "high": 2390.0,
                           "low": 2375.0, "close": 2385.0, "volume": 1234.5}])
    replay = frame.copy()
    replay.loc[0, field] = value
    monkeypatch.setattr(collection, "now_tw", lambda: now)
    monkeypatch.setattr(collection, "fetch_recent_kline", lambda *a, **kw: frame)
    monkeypatch.setattr(collection, "load_kline_from_db", lambda *a, **kw: replay)
    bar = SimpleNamespace(date="2026-11-04", close=2385.0, source_record_id="source",
                          retrieved_at=now.isoformat())
    monkeypatch.setattr(collection.StockDB, "open", lambda **kw:
        SimpleNamespace(get_price_history=lambda *a, **kw: [bar], close=lambda: None))
    monkeypatch.setattr(collection, "verify_source", lambda *a:
        SimpleNamespace(url="https://example.test", document_id="source"))
    with pytest.raises(ValueError, match="disagree"):
        collection.collect_price(tmp_path, "2330")


def test_extended_symbols_cover_etfs_taiwan_holdings_and_backtest_universe(tmp_path, monkeypatch):
    from bot import active_etf
    monkeypatch.setattr(active_etf, "load_active_etfs", lambda root: [SimpleNamespace(symbol="00981A")])
    monkeypatch.setattr(active_etf, "list_holdings_dates", lambda *a: [dt.date(2026, 11, 11)])
    monkeypatch.setattr(active_etf, "load_holdings", lambda *a: SimpleNamespace(
        holdings=[SimpleNamespace(ticker="2454"), SimpleNamespace(ticker="NVDA US"), SimpleNamespace(ticker="8306 JT")]))
    (tmp_path / "config").mkdir()
    (tmp_path / "config/backtest_universe.json").write_text('{"symbols": [{"symbol": "2303"}]}', encoding="utf-8")
    assert collection.extended_price_symbols(tmp_path) == ["00981A", "2303", "2454"]


def test_extended_prices_skip_current_resume_on_budget_and_do_not_refetch(tmp_path, monkeypatch):
    monkeypatch.setattr(collection, "now_tw", lambda: dt.datetime(2026, 11, 17, 15, tzinfo=dt.timezone(dt.timedelta(hours=8))))
    monkeypatch.setattr(collection, "latest_price_dates", lambda root: {"A1": "2026-11-16", "B1": "2026-10-15"})
    calls = []
    def price(root, ticker, months=1):
        calls.append((ticker, months))
        if ticker == "D1":
            raise ValueError("boom")
        return {"source_date": "2026-11-10" if ticker == "C1" else "2026-11-16"}
    monkeypatch.setattr(collection, "collect_price", price)
    symbols = ["A1", "B1", "C1", "D1"]
    # 預算 0：已到目標日的不連線，其餘留待下次。
    first = collection.collect_extended_prices(tmp_path, symbols, "2026-11-16", {}, budget=0, delay=0)
    assert first["current"] == 1 and first["pending"] == ["B1", "C1", "D1"] and not calls and not first["complete"]
    second = collection.collect_extended_prices(tmp_path, symbols, "2026-11-16", first, budget=60, delay=0)
    assert calls == [("B1", 2), ("C1", 1), ("D1", 1)]          # 落後一個月的補兩個月
    assert second["current"] == 2 and second["behind"] == {"C1": "2026-11-10"}
    assert second["failed"] == {"D1": "boom"} and second["pending"] == ["D1"]
    # 停牌的 C1 同一個目標日不重抓；失敗的 D1 最多三次。
    state = second
    for _ in range(4):
        state = collection.collect_extended_prices(tmp_path, symbols, "2026-11-16", state, budget=60, delay=0)
    assert [c[0] for c in calls].count("C1") == 1 and [c[0] for c in calls].count("D1") == 3
    assert state["pending"] == [] and state["failed"] == {"D1": "boom"} and not state["complete"]
    # 換目標日就重新開始。
    assert collection.collect_extended_prices(tmp_path, ["C1"], "2026-11-17", state, budget=60, delay=0)["fetched"] == ["C1"]
    assert collection.collect_extended_prices(tmp_path, symbols, "", {}, budget=60, delay=0)["pending"] == symbols


def test_ex_rights_refresh_keeps_universe_symbols_and_runs_once_per_day(tmp_path, monkeypatch):
    from bot import ex_rights
    from bot.stock_db import StockDB
    monkeypatch.setattr(collection, "now_tw", lambda: dt.datetime(2026, 11, 17, 15, tzinfo=dt.timezone(dt.timedelta(hours=8))))
    (tmp_path / "config").mkdir()
    (tmp_path / "config/backtest_universe.json").write_text('{"symbols": [{"symbol": "2303"}]}', encoding="utf-8")
    def row(symbol):
        return dict(symbol=symbol, ex_date="2026-11-10", kind="息", prev_close=50.0, reference_price=49.0,
                    value_total=1.0, cash_dividend=1.0, stock_per_1000=None, market="twse",
                    source_url="https://example.test", retrieved_at="2026-11-17T15:00:00")
    years = []
    monkeypatch.setattr(ex_rights, "fetch_year", lambda year: years.append(year) or [row("2303"), row("9999")])
    result = REAL_COLLECT_EX_RIGHTS(tmp_path)      # autouse fixture 換掉的是模組屬性，這裡測真的實作
    assert result["status"] == "ok" and result["saved"] == 1 and result["latest_ex_date"] == "2026-11-10" and years == [2026]
    db = StockDB.open(root=tmp_path)
    try:
        assert [r[0] for r in db.conn.execute("SELECT symbol FROM ex_rights_events")] == ["2303"]
    finally:
        db.close()



def test_daily_inputs_refresh_ex_rights_once_per_day_and_retry_failures(tmp_path, monkeypatch):
    from bot import watchlist
    clock = [dt.datetime(2026, 11, 17, 10, tzinfo=dt.timezone(dt.timedelta(hours=8)))]
    monkeypatch.setattr(collection, "now_tw", lambda: clock[0])
    monkeypatch.setattr(watchlist, "load", lambda root: SimpleNamespace(items=[]))
    monkeypatch.setattr(collection, "collect_news", lambda *a: {"status": "ok"})
    monkeypatch.setattr(collection, "collect_price", lambda *a: {"status": "ok"})
    outcomes = [ValueError("twse down"), {"status": "ok"}]
    calls = []
    def ex(root):
        calls.append(1)
        value = outcomes.pop(0)
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(collection, "collect_ex_rights", ex)
    settings = SimpleNamespace(symbols=["2330"])
    first = collection.collect_daily_inputs(tmp_path, settings)
    assert first["ex_rights"]["status"] == "failed" and first["complete"]      # 不影響 complete
    assert collection.collect_daily_inputs(tmp_path, settings)["ex_rights"]["status"] == "ok"
    collection.collect_daily_inputs(tmp_path, settings)
    assert len(calls) == 2
