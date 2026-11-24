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


def _finmind_report(root, day, **fields):
    folder = root / "data/reports"
    folder.mkdir(parents=True, exist_ok=True)
    body = {"status": "ok", "source": "FinMind", "summary": {"total": 9, "match": 9, "mismatch": 0, "db_missing": 0}}
    body.update(fields)
    (folder / f"finmind_crosscheck_{day}.json").write_text(json.dumps(body), encoding="utf-8")


def test_finmind_crosscheck_check_states(tmp_path):
    now = dt.datetime(2026, 11, 24, 10, tzinfo=TZ)
    assert fresh._finmind_crosscheck(tmp_path, now)["status"] == "info"
    _finmind_report(tmp_path, "2026-11-24")
    ok = fresh._finmind_crosscheck(tmp_path, now)
    assert ok["status"] == "ok" and "FinMind" in ok["detail"]
    _finmind_report(tmp_path, "2026-11-24", status="skipped")
    assert fresh._finmind_crosscheck(tmp_path, now)["status"] == "info"
    _finmind_report(tmp_path, "2026-11-24", status="quota_exceeded")
    assert fresh._finmind_crosscheck(tmp_path, now)["status"] == "warn"


def test_finmind_crosscheck_check_flags_mismatch_missing_and_stale(tmp_path):
    now = dt.datetime(2026, 11, 24, 10, tzinfo=TZ)
    _finmind_report(tmp_path, "2026-11-24",
                    summary={"total": 9, "match": 7, "mismatch": 1, "db_missing": 1},
                    items=[{"symbol": "2330", "period": "2026Q3", "field": "eps", "status": "mismatch"}])
    bad = fresh._finmind_crosscheck(tmp_path, now)
    assert bad["status"] == "warn" and "2330 2026Q3 eps" in bad["detail"] and "缺資料" in bad["detail"]
    _finmind_report(tmp_path, "2026-11-19")      # 比 11-24 舊，但取最新的一份（11-24）
    assert fresh._finmind_crosscheck(tmp_path, now)["status"] == "warn"
    old = tmp_path / "old"
    _finmind_report(old, "2026-11-18")
    stale = fresh._finmind_crosscheck(old, now)
    assert stale["status"] == "warn" and "沒更新" in stale["detail"]


def test_finmind_crosscheck_check_does_not_change_overall_without_report(tmp_path, monkeypatch):
    from bot import active_etf, daily_collection
    now = dt.datetime(2026, 11, 17, 1, tzinfo=TZ)
    root = _project(tmp_path, [("2330", "2026-11-16")], heartbeat="2026-11-17T00:59:00+08:00")
    monkeypatch.setattr(daily_collection, "extended_price_symbols", lambda r: [])
    monkeypatch.setattr(active_etf, "load_active_etfs", lambda r: [])
    checks = _by_name(fresh.check_data_freshness(root, settings=SimpleNamespace(symbols=["2330"]), now=now))
    assert checks["FinMind 交叉核對"]["status"] == "info"


def test_finmind_crosscheck_check_warns_outstanding(tmp_path):
    now = dt.datetime(2026, 11, 24, 22, tzinfo=TZ)
    _finmind_report(tmp_path, "2026-11-24",
                    outstanding=[{"symbol": "1514", "checked": "2026-11-23", "mismatch": 0, "db_missing": 6}])
    check = fresh._finmind_crosscheck(tmp_path, now)
    assert check["status"] == "warn" and "1514" in check["detail"] and check["extra"]["outstanding"] == 1


def _etf_check(monkeypatch, holdings, now):
    from bot import active_etf
    monkeypatch.setattr(active_etf, "load_tracked_etfs", lambda r: [SimpleNamespace(symbol=s) for s in holdings])
    monkeypatch.setattr(active_etf, "list_holdings_dates", lambda symbol, r: holdings[symbol])
    return fresh._etf_holdings(None, now)


def test_etf_holdings_compares_daily_etfs_with_peers(monkeypatch):
    now = dt.datetime(2026, 11, 25, 21, tzinfo=TZ)                        # 週三 21 點，預期日 11-25
    day = lambda d: dt.date(2026, 11, d)
    holdings = {"E1": [day(25), day(24)], "E2": [day(25), day(24)],
                "LATE": [day(23), day(20)],                              # 投信固定晚兩個交易日揭露
                "M1": [dt.date(2026, 10, 31), dt.date(2026, 9, 30)]}     # 每月揭露
    check = _etf_check(monkeypatch, holdings, now)
    assert check["status"] == "ok" and "4/4" in check["detail"] and "2026-11-25" in check["detail"]
    holdings["LATE"] = [day(20), day(19)]                                 # 落後參考日 3 個工作日
    check = _etf_check(monkeypatch, holdings, now)
    assert check["status"] == "warn" and "LATE（2026-11-20）" in check["detail"] and "M1" not in check["detail"]


def test_etf_holdings_warns_when_every_daily_etf_stops(monkeypatch):
    now = dt.datetime(2026, 11, 25, 21, tzinfo=TZ)
    old = [dt.date(2026, 11, 20), dt.date(2026, 11, 19)]                  # 落後預期日 3 個工作日
    check = _etf_check(monkeypatch, {"E1": old, "E2": old, "M1": [dt.date(2026, 10, 31), dt.date(2026, 9, 30)]}, now)
    assert check["status"] == "fail" and "E1" in check["detail"] and "E2" in check["detail"]
    assert "可能全部停更" in check["detail"] and "2026-11-20" in check["detail"] and "M1" not in check["detail"]


def test_etf_holdings_monthly_rule_unchanged(monkeypatch):
    now = dt.datetime(2026, 11, 25, 21, tzinfo=TZ)
    daily = [dt.date(2026, 11, 25), dt.date(2026, 11, 24)]
    ok = _etf_check(monkeypatch, {"E1": daily, "M1": [dt.date(2026, 10, 31), dt.date(2026, 9, 30)]}, now)
    assert ok["status"] == "ok"
    stale = _etf_check(monkeypatch, {"E1": daily, "M1": [dt.date(2026, 10, 1), dt.date(2026, 9, 1)]}, now)
    assert stale["status"] == "warn" and "M1（2026-10-01）" in stale["detail"]
