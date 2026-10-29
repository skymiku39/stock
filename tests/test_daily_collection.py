import datetime as dt
from types import SimpleNamespace

import pandas as pd
import pytest

from bot import daily_collection as collection


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


def test_price_fallback_is_not_a_successful_refresh(tmp_path, monkeypatch):
    now = dt.datetime(2026, 10, 27, 11, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    monkeypatch.setattr(collection, "now_tw", lambda: now)
    frame = pd.DataFrame([{"date": "2026-10-22", "close": 2385.0}])
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
    frame = pd.DataFrame([{"date": "2026-10-22", "close": 2385.0}])
    def fetch(*a, **kw):
        with SourceRegistry(tmp_path) as registry:
            source = registry.capture_document("twse_stock_day", '{"close":2385}',
                source_url="https://www.twse.com.tw/exchangeReport/STOCK_DAY", suffix="json")
        db = StockDB.open(root=tmp_path)
        try:
            db.upsert_price_bar(PriceBar(symbol="2330", date="2026-10-22", close=2385.0,
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
