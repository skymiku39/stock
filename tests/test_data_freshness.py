import datetime as dt
import json
import sqlite3
from types import SimpleNamespace

from bot import data_freshness as fresh

TZ = dt.timezone(dt.timedelta(hours=8))


def test_expected_market_date_and_weekday_lag():
    tuesday_night = dt.datetime(2026, 11, 17, 1, tzinfo=TZ)
    assert fresh.expected_market_date(tuesday_night) == dt.date(2026, 11, 16)
    assert fresh.expected_market_date(dt.datetime(2026, 11, 17, 17, tzinfo=TZ)) == dt.date(2026, 11, 17)
    assert fresh.expected_market_date(dt.datetime(2026, 11, 15, 12, tzinfo=TZ)) == dt.date(2026, 11, 13)   # 週日
    assert fresh.expected_market_date(dt.datetime(2026, 11, 16, 9, tzinfo=TZ)) == dt.date(2026, 11, 13)    # 週一盤前
    assert fresh.weekday_lag("2026-11-16", dt.date(2026, 11, 16)) == 0
    assert fresh.weekday_lag("2026-11-11", dt.date(2026, 11, 16)) == 3      # 跨週末只算工作日
    assert fresh.weekday_lag("not a date", dt.date(2026, 11, 16)) is None


def _project(tmp_path, prices, *, heartbeat, jobs=None):
    data = tmp_path / "data"
    (data / "news").mkdir(parents=True)
    con = sqlite3.connect(data / "stock.db")
    con.execute("CREATE TABLE price_history (symbol TEXT, date TEXT)")
    con.execute("CREATE TABLE valuation_daily (symbol TEXT, as_of TEXT)")
    con.execute("CREATE TABLE monthly_revenue (symbol TEXT, year_month TEXT)")
    con.executemany("INSERT INTO price_history VALUES (?, ?)", prices)
    con.execute("INSERT INTO valuation_daily VALUES ('2330', '2026-11-16')")
    con.execute("INSERT INTO monthly_revenue VALUES ('2330', '2026-10')")
    con.commit()
    con.close()
    (data / "scheduler_status.json").write_text(json.dumps({"heartbeat_at": heartbeat, "jobs": jobs or {}}), encoding="utf-8")
    (data / "news/news_2026-11-17.json").write_text(json.dumps(
        {"items": [{"published_at": "2026-11-16T21:00:00+08:00"}]}), encoding="utf-8")
    (data / "news/rss_2026-11-17.json").write_text(json.dumps(
        {"feeds_total": 10, "feeds_seen": 10, "feeds_ok": 9, "total_items": 50, "collected_at": "2026-11-17T00:30:00+08:00"}), encoding="utf-8")
    for name in ("chips/2026-11-16", "macro"):
        (data / name).mkdir(parents=True)
    (data / "macro/macro_2026-11-16.json").write_text("{}", encoding="utf-8")
    return tmp_path


def _by_name(report):
    return {c["name"]: c for c in report["checks"]}


def test_stale_extended_prices_and_blocked_jobs_are_reported(tmp_path, monkeypatch):
    from bot import active_etf, daily_collection
    now = dt.datetime(2026, 11, 17, 1, tzinfo=TZ)
    root = _project(tmp_path, [("2330", "2026-11-16"), ("A", "2026-11-16"), ("B", "2026-11-11")],
                    heartbeat="2026-11-17T00:59:00+08:00",
                    jobs={"research": {"state": "blocked", "last_success_at": "2026-10-16T09:26:16+08:00"},
                          "macro": {"state": "succeeded"},
                          "nextday_draft": {"state": "interrupted", "finished_at": "2026-10-13T04:02:47+08:00"}})
    monkeypatch.setattr(daily_collection, "extended_price_symbols", lambda r: ["A", "B", "C"])
    monkeypatch.setattr(active_etf, "load_active_etfs", lambda r: [SimpleNamespace(symbol=s) for s in ("E1", "E2", "M1")])
    holdings = {"E1": [dt.date(2026, 11, 16)], "E2": [dt.date(2026, 11, 6), dt.date(2026, 11, 5)],
                "M1": [dt.date(2026, 10, 31), dt.date(2026, 9, 30)]}      # M1 每月揭露一次，不算過期
    monkeypatch.setattr(active_etf, "list_holdings_dates", lambda symbol, r: holdings[symbol])
    report = fresh.check_data_freshness(root, settings=SimpleNamespace(symbols=["2330"]), now=now)
    checks = _by_name(report)
    assert report["status"] == "fail" and report["expected_market_date"] == "2026-11-16"
    assert checks["排程器心跳"]["status"] == "ok" and checks["日 K（主要追蹤）"]["status"] == "ok"
    extended = checks["日 K（ETF、ETF 持股、回測名單）"]
    assert extended["status"] == "fail" and extended["extra"]["behind"] == ["B", "C"]
    assert checks["排程工作"]["status"] == "warn" and "research" in checks["排程工作"]["detail"]
    assert "macro" not in checks["排程工作"]["detail"]
    assert "nextday_draft：" not in checks["排程工作"]["detail"] and "視為已停用：nextday_draft" in checks["排程工作"]["detail"]
    assert checks["ETF 持股"]["status"] == "warn" and "E2" in checks["ETF 持股"]["detail"]
    assert "M1" not in checks["ETF 持股"]["detail"] and "2/3" in checks["ETF 持股"]["detail"]
    assert checks["月營收"]["status"] == "ok" and checks["RSS 存檔"]["status"] == "ok"
    assert checks["籌碼（三大法人、融資券）"]["status"] == "ok"
    assert any("落後" in line for line in fresh.format_report(report))


def test_dead_scheduler_and_missing_data_fail(tmp_path, monkeypatch):
    from bot import active_etf, daily_collection
    now = dt.datetime(2026, 11, 20, 18, tzinfo=TZ)
    root = _project(tmp_path, [("2330", "2026-11-16")], heartbeat="2026-11-17T00:59:00+08:00")
    monkeypatch.setattr(daily_collection, "extended_price_symbols", lambda r: [])
    monkeypatch.setattr(active_etf, "load_active_etfs", lambda r: [])
    checks = _by_name(fresh.check_data_freshness(root, settings=SimpleNamespace(symbols=["2330"]), now=now))
    assert checks["排程器心跳"]["status"] == "fail"
    assert checks["日 K（主要追蹤）"]["status"] == "fail" and checks["日 K（主要追蹤）"]["extra"]["lag"] == 4
    assert checks["新聞（鉅亨）"]["status"] == "fail" and checks["RSS 存檔"]["status"] == "fail"
    assert checks["月營收"]["status"] == "ok"      # 20 日已過公布期限，10 月資料已有


def test_collection_status_separates_advisory_from_blocking(tmp_path):
    now = dt.datetime(2026, 11, 17, 12, tzinfo=TZ)
    assert fresh._collection(tmp_path, now)["status"] == fresh.WARN          # 中午還沒有紀錄
    assert fresh._collection(tmp_path, now.replace(hour=3))["status"] == fresh.INFO
    folder = tmp_path / "data/daily_research/2026-11-17"
    folder.mkdir(parents=True)
    status = folder / "status.json"
    advisory = "macro: source_date_status=mixed"
    status.write_text(json.dumps({
        "attempts": 3, "collection_complete": False, "official_complete": True,
        "collection_errors": [], "collection_warnings": [advisory, "chips: 1 個焦點標的沒有可用交易日資料"],
        "collection_advisory_warnings": [advisory]}), encoding="utf-8")
    check = fresh._collection(tmp_path, now)
    assert check["status"] == fresh.WARN
    assert check["extra"]["blocking_warnings"] == ["chips: 1 個焦點標的沒有可用交易日資料"]
    assert "已嘗試 3 次" in check["detail"]
    status.write_text(json.dumps({
        "collection_complete": True, "official_complete": True, "collected_at": "2026-11-17T11:00:00+08:00",
        "collection_warnings": [advisory], "collection_advisory_warnings": [advisory]}), encoding="utf-8")
    check = fresh._collection(tmp_path, now)
    assert check["status"] == fresh.OK and "1 則只需註明限制" in check["detail"]
