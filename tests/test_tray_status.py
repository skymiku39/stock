"""系統匣狀態快照：只讀狀態檔、壞檔不當機（不碰專案 data/）。"""
import datetime as dt
import json

from bot.tray import status as tray_status

TZ = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime(2026, 11, 20, 10, 0, tzinfo=TZ)


def write(root, relative, payload):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False),
                    encoding="utf-8")


def scheduler(age_seconds=10, jobs=None):
    return {"state": "running",
            "heartbeat_at": (NOW - dt.timedelta(seconds=age_seconds)).isoformat(),
            "jobs": jobs or {}}


def freshness(checks, age_seconds=60):
    return {"checked_at": (NOW - dt.timedelta(seconds=age_seconds)).isoformat(),
            "checks": [{"name": n, "status": s, "detail": d} for n, s, d in checks]}


def test_empty_root_is_unknown_not_crash(tmp_path):
    snap = tray_status.read_snapshot(tmp_path, NOW)
    assert snap.level == tray_status.UNKNOWN
    assert snap.headline == "讀不到排程器狀態"
    assert snap.holdings == [] and snap.notifications == [] and snap.focus.focus_count == 0


def test_all_ok(tmp_path):
    write(tmp_path, "data/scheduler_status.json",
          scheduler(jobs={"smile_watch": {"state": "running"}, "macro": {"state": "succeeded"}}))
    write(tmp_path, "data/freshness_status.json", freshness([("每日收集", "ok", "今天已完成")]))
    snap = tray_status.read_snapshot(tmp_path, NOW)
    assert snap.level == tray_status.OK and snap.headline == "資料與排程都正常"
    assert snap.running_jobs == ["smile_watch"]


def test_warning_and_failure_headline_names_the_failures_first(tmp_path):
    write(tmp_path, "data/scheduler_status.json", scheduler())
    write(tmp_path, "data/freshness_status.json", freshness([
        ("模型服務", "warn", "被擋"), ("每日收集", "fail", "今天未完成"), ("新聞", "ok", "")]))
    snap = tray_status.read_snapshot(tmp_path, NOW)
    assert snap.level == tray_status.FAIL
    assert snap.headline == "2 項要注意：每日收集"
    assert [c.name for c in snap.problems()] == ["模型服務", "每日收集"]


def test_stale_heartbeat_is_failure_even_if_checks_look_fine(tmp_path):
    write(tmp_path, "data/scheduler_status.json", scheduler(age_seconds=1200))
    write(tmp_path, "data/freshness_status.json", freshness([("每日收集", "ok", "")]))
    snap = tray_status.read_snapshot(tmp_path, NOW)
    assert snap.level == tray_status.FAIL
    assert snap.headline == "排程器沒有回應（20 分鐘沒有心跳）"


def test_stale_freshness_file_adds_warning(tmp_path):
    write(tmp_path, "data/scheduler_status.json", scheduler())
    write(tmp_path, "data/freshness_status.json", freshness([("每日收集", "ok", "")], age_seconds=7200))
    snap = tray_status.read_snapshot(tmp_path, NOW)
    assert snap.level == tray_status.WARN and snap.checks[0].name == "狀態檢查"


def test_holdings_stop_gap_and_stale_quote(tmp_path):
    base = {"symbol": "1514", "price": 68.0, "lots": 5, "avg_price": 82.12, "pnl_twd": -72000.0,
            "pnl_pct": -17.5, "breakeven_price": 82.5, "stop_price": 61.2, "signals": []}
    write(tmp_path, "data/smile_watch/status-1514.json", {**base, "ts": "2026-11-20T09:30:00+08:00"})
    write(tmp_path, "data/smile_watch/status-2603.json", {**base, "symbol": "2603", "stop_price": None,
                                                           "ts": "2026-11-19T13:00:00+08:00"})
    write(tmp_path, "data/smile_watch/status-bad.json", "{broken")
    first, second = tray_status.read_holdings(tmp_path, NOW)
    assert round(first.stop_gap_pct, 1) == 10.0 and not first.stale
    assert "距停損 10.0%" in first.line() and "損益 -72,000（-17.5%）" in first.line()
    assert second.stop_gap_pct is None and second.stale and "2026-11-19 的報價" in second.line()


def test_holdings_show_fallback_price_source(tmp_path):
    base = {"symbol": "1514", "price": 76.4, "lots": 4, "avg_price": 85.65, "pnl_twd": -38175.0,
            "pnl_pct": -11.1, "breakeven_price": 85.98, "stop_price": 64.48, "signals": [],
            "ts": "2026-11-20T11:35:44+08:00"}
    write(tmp_path, "data/smile_watch/status-1514.json", {
        **base, "price_source": "fugle", "price_note": "鎖漲停、最後成交價 76.4、時間 11:01:16"})
    write(tmp_path, "data/smile_watch/status-2330.json", {
        **base, "symbol": "2330", "price_source": "fugle", "price_note": "最後成交價 1000、時間 11:30:00"})
    write(tmp_path, "data/smile_watch/status-2603.json", {**base, "symbol": "2603",
                                                           "price_source": "primary", "price_note": ""})
    write(tmp_path, "data/smile_watch/status-2881.json", {**base, "symbol": "2881"})  # 舊狀態檔
    locked, plain, primary, legacy = tray_status.read_holdings(tmp_path, NOW)
    assert locked.source_tag == "富果備援・鎖漲停" and locked.line().endswith("富果備援・鎖漲停")
    assert locked.source_note == "報價來源：富果備援｜鎖漲停、最後成交價 76.4、時間 11:01:16"
    assert len(locked.line()) <= 80
    assert plain.source_tag == "富果備援" and plain.source_note.endswith("最後成交價 1000、時間 11:30:00")
    for h in (primary, legacy):
        assert h.price_source == "primary" and h.source_tag == "" and h.source_note == ""
        assert "備援" not in h.line()


def test_notifications_newest_first_and_skip_bad_lines(tmp_path):
    rows = [json.dumps({"title": f"第{i}則", "ts": f"2026-11-20T09:0{i}:00+08:00"}, ensure_ascii=False)
            for i in range(7)]
    write(tmp_path, "data/notifications/history.jsonl", "\n".join(rows[:3] + ["{bad"] + rows[3:]))
    got = tray_status.read_notifications(tmp_path, limit=3)
    assert [r["title"] for r in got] == ["第6則", "第5則", "第4則"]


def test_focus_summary(tmp_path):
    write(tmp_path, "data/etf_focus/latest.json", {
        "as_of": "2026-11-19", "focus": ["2454", "3711"], "caution": ["2303"],
        "stocks": [{"ticker": "2454", "name": "聯發科", "active_net": 12},
                   {"ticker": "3711", "name": "日月光投控", "active_net": 11},
                   {"ticker": "2303", "name": "聯電", "active_net": -9},
                   {"ticker": "2330", "name": "台積電", "active_net": 0}]})
    focus = tray_status.read_focus(tmp_path)
    assert (focus.as_of, focus.focus_count, focus.caution_count) == ("2026-11-19", 2, 1)
    assert focus.top_adds == ["2454 聯發科（+12）", "3711 日月光投控（+11）"]
    assert focus.top_reduces == ["2303 聯電（-9）"]


def test_tooltip_fits_windows_limit(tmp_path):
    write(tmp_path, "data/scheduler_status.json", scheduler())
    write(tmp_path, "data/freshness_status.json",
          freshness([(f"很長的檢查項目名稱{i}", "warn", "") for i in range(20)]))
    snap = tray_status.read_snapshot(tmp_path, NOW)
    assert len(snap.tooltip()) <= 127 and snap.tooltip().startswith("Stock Lab：有警告")


def test_all_holdings_listed_and_watched_ones_not_repeated(tmp_path):
    import datetime as dt
    import json

    folder = tmp_path / "data" / "smile_watch"
    folder.mkdir(parents=True)
    (folder / "status-1514.json").write_text(json.dumps({
        "symbol": "1514", "ts": "2026-11-20T13:29:40+08:00", "price": 66.9, "lots": 5,
        "avg_price": 82.12, "pnl_twd": -77400.79, "pnl_pct": -18.84, "breakeven_price": 82.43,
        "stop_price": 61.82, "signals": []}), encoding="utf-8")
    (folder / "holdings.json").write_text(json.dumps({
        "asof": "2026-11-20T13:00:00+08:00", "holdings": [
            {"symbol": "1514", "shares": 5000, "avg_price": 82.12, "last_price": 66.9, "pnl_twd": -77400},
            {"symbol": "2603", "shares": 300, "avg_price": 200.0, "last_price": 210.0, "pnl_twd": 2900},
            {"symbol": "0050", "shares": 2000, "avg_price": 90.0, "last_price": 95.0, "pnl_twd": 9800},
            {"symbol": "9999", "shares": 0, "avg_price": 1, "last_price": 1, "pnl_twd": 0},
            {"symbol": "bad"}]}), encoding="utf-8")
    now = dt.datetime(2026, 11, 20, 14, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    snap = tray_status.read_snapshot(tmp_path, now)
    assert [h.symbol for h in snap.holdings] == ["1514"]
    assert [h.symbol for h in snap.other_holdings] == ["2603", "0050"]
    assert snap.holdings_asof == "2026-11-20T13:00:00+08:00"
    odd, whole = snap.other_holdings
    assert odd.quantity_text == "300 股" and whole.quantity_text == "2 張"
    assert round(odd.pnl_pct, 1) == 4.8
    assert "2603" in odd.line() and "+2,900" in odd.line()


def test_no_holdings_file_keeps_watched_only(tmp_path):
    import datetime as dt

    snap = tray_status.read_snapshot(tmp_path, dt.datetime(2026, 11, 20, 14, 0,
                                                           tzinfo=dt.timezone(dt.timedelta(hours=8))))
    assert snap.other_holdings == [] and snap.holdings_asof == ""


def brief_payload(**over):
    data = {
        "date": "2026-11-20", "generated_at": "2026-11-20T08:00:12+08:00",
        "market": {"status": "ok", "error": "",
                   "indices": [{"symbol": "^GSPC", "name": "S&P 500", "price": 7260.2, "pct_change": 5.06},
                               {"symbol": "^VIX", "name": "VIX", "price": 15.0, "pct_change": -23.74},
                               {"symbol": "^XYZ", "name": "其他", "price": 1.0, "pct_change": 1.0}],
                   "stocks": [{"symbol": "NVDA", "pct_change": 32.66}, {"symbol": "TSM", "pct_change": 3.2},
                              {"symbol": "AAPL", "pct_change": 1.0}]},
        "news": {"candidates": [{"title": "a"}, {"title": "b"}]},
        "summary": {"status": "pending_model", "points": [{"text": "不該出現", "urls": []}]},
        "warnings": ["注意"],
    }
    data.update(over)
    return data


def test_overnight_normal(tmp_path):
    write(tmp_path, "data/overnight_brief/latest.json", brief_payload())
    brief = tray_status.read_overnight(tmp_path, NOW)
    assert brief.market_lines == ["S&P 500 +5.06%　VIX -23.74%　其他 +1.00%",
                                  "台積電 ADR +3.20%　NVDA +32.66%"]
    assert brief.date == "2026-11-20" and not brief.stale
    assert brief.candidate_count == 2 and brief.warnings == ["注意"]
    assert brief.summary_status == "pending_model" and brief.points == []


def test_overnight_missing_or_broken_file(tmp_path):
    assert tray_status.read_overnight(tmp_path, NOW) == tray_status.OvernightBrief()
    write(tmp_path, "data/overnight_brief/latest.json", "{壞掉")
    assert tray_status.read_overnight(tmp_path, NOW) == tray_status.OvernightBrief()
    write(tmp_path, "data/overnight_brief/latest.json", {"date": "2026-11-20"})
    assert tray_status.read_overnight(tmp_path, NOW).market_lines == ["行情暫時取不到"]


def test_overnight_market_unavailable(tmp_path):
    write(tmp_path, "data/overnight_brief/latest.json",
          brief_payload(market={"status": "unavailable", "error": "x"}))
    assert tray_status.read_overnight(tmp_path, NOW).market_lines == ["行情暫時取不到"]


def test_overnight_not_today_is_stale(tmp_path):
    write(tmp_path, "data/overnight_brief/latest.json", brief_payload(date="2026-11-19"))
    assert tray_status.read_overnight(tmp_path, NOW).stale


def test_overnight_summary_ok_takes_up_to_three_points(tmp_path):
    points = [{"text": f"重點{i}", "urls": []} for i in range(5)]
    write(tmp_path, "data/overnight_brief/latest.json",
          brief_payload(summary={"status": "ok", "points": points}))
    brief = tray_status.read_overnight(tmp_path, NOW)
    assert brief.summary_status == "ok" and brief.points == ["重點0", "重點1", "重點2"]


def test_overnight_does_not_affect_level(tmp_path):
    write(tmp_path, "data/scheduler_status.json", scheduler())
    write(tmp_path, "data/freshness_status.json", freshness([("每日收集", "ok", "x")]))
    before = tray_status.read_snapshot(tmp_path, NOW)
    write(tmp_path, "data/overnight_brief/latest.json", brief_payload(date="2026-11-01"))
    after = tray_status.read_snapshot(tmp_path, NOW)
    assert after.overnight.stale
    assert (after.level, after.headline) == (before.level, before.headline)


def test_overnight_night_futures_line(tmp_path):
    night = {"status": "ok", "front": {"last": 38482.0, "pct_change": -4.57, "closed": True}}
    write(tmp_path, "data/overnight_brief/latest.json", brief_payload(night_futures=night))
    lines = tray_status.read_overnight(tmp_path, NOW).market_lines
    assert lines[-1] == "台指期夜盤 38,482（-4.57%）"

    night["front"]["closed"] = False
    write(tmp_path, "data/overnight_brief/latest.json", brief_payload(night_futures=night))
    assert tray_status.read_overnight(tmp_path, NOW).market_lines[-1].endswith("未收盤")

    write(tmp_path, "data/overnight_brief/latest.json",
          brief_payload(night_futures={"status": "error", "front": None}))
    assert len(tray_status.read_overnight(tmp_path, NOW).market_lines) == 2
