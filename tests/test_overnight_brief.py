"""美股隔夜重點產生端：全部用 tmp_path 與假的行情／通知，不連網。"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

from bot import overnight_brief as ob
from bot.config import Settings

TZ = dt.timezone(dt.timedelta(hours=8))


def _t(day: int, hour: int, minute: int = 0, month: int = 11) -> dt.datetime:
    return dt.datetime(2026, month, day, hour, minute, tzinfo=TZ)


def _item(title, ts, *, feed="f1", url=None, region="us", stance="", tickers=None, seen=None):
    return {"title": title, "url": url or f"http://x/{title}", "desc": "", "feed": feed,
            "published_at": ts.isoformat(), "region": region, "lang": "en", "stance": stance,
            "first_seen_at": (seen or ts).isoformat(), "related_tickers": tickers or []}


def _write_archive(root: Path, day: str, items: list, collected: str = "") -> None:
    path = root / "data" / "news" / f"rss_{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"collected_at": collected, "items": items}), encoding="utf-8")


def _q(symbol, name, pct, source_date="2026-11-23", **kw):
    return SimpleNamespace(symbol=symbol, name=name, price=100.0, pct_change=pct,
                           source_date=source_date, **kw)


def _snap(gspc_date="2026-11-23"):
    return SimpleNamespace(
        fetched_at="2026-11-24T07:59:00+08:00", usdtwd=32.1,
        indices={"^GSPC": _q("^GSPC", "S&P 500", 5.06, gspc_date),
                 "^IXIC": _q("^IXIC", "NASDAQ", 6.91),
                 "^SOX": _q("^SOX", "費半", 14.11),
                 "^TWII": _q("^TWII", "加權", 1.0)},
        stocks={"TSM": _q("TSM", "台積電 ADR", 3.2, parent_tw_ticker="2330")},
        adr_premiums=[SimpleNamespace(adr_symbol="TSM", tw_ticker="2330", premium_pct=1.5,
                                      temporal_status="aligned")])


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


def _brief(root, now, slot="morning", snap=None, notifier=None, **kw):
    loader = kw.pop("macro_loader", None) or (lambda: snap or _snap())
    return ob.build_brief(root, now=now, slot=slot, macro_loader=loader, notifier=notifier, **kw)


def test_window_start_weekdays():
    assert ob.window_start(_t(23, 8)) == _t(20, 21)   # 週一 -> 上週五
    assert ob.window_start(_t(24, 8)) == _t(23, 21)   # 週二 -> 週一
    assert ob.window_start(_t(21, 8)) == _t(20, 21)   # 週六 -> 週五
    assert ob.window_start(_t(22, 8)) == _t(20, 21)   # 週日 -> 週五


def test_only_us_non_official_and_dedup(tmp_path):
    from bot.web_search import OFFICIAL_STANCE
    now = _t(24, 8, 0)
    _write_archive(tmp_path, "2026-11-23", [
        _item("Apple launches product today", _t(23, 22)),
        _item("Apple launches product today", _t(23, 22)),  # 同 url 重複
        _item("Taiwan local story here", _t(23, 22), region="tw"),
        _item("Official statement posted", _t(23, 22), stance=OFFICIAL_STANCE),
        _item("Old story before window", _t(23, 20)),
    ])
    brief = _brief(tmp_path, now, notify=False)
    assert brief["news"]["items_in_window"] == 1
    assert [c["title"] for c in brief["news"]["candidates"]] == ["Apple launches product today"]


def test_same_story_grouped_and_ranked_first(tmp_path):
    now = _t(24, 8, 0)
    _write_archive(tmp_path, "2026-11-23", [
        _item("Gardening club meets weekly downtown", _t(23, 23), feed="a"),
        _item("Fed signals rate cut next month", _t(23, 22), feed="a"),
        _item("Fed signals rate cut next month, says Powell", _t(23, 23, 30), feed="b", url="u2"),
        _item("Fed signals a rate cut next month", _t(24, 1), feed="c", url="u3"),
    ])
    c = _brief(tmp_path, now, notify=False)["news"]["candidates"]
    assert len(c) == 2
    top = c[0]
    assert top["title"] == "Fed signals rate cut next month"  # 最早發布者為代表
    assert top["feeds"] == ["a", "b", "c"] and top["source_count"] == 3
    assert top["score"] == 2 * 3 + 1 and top["topics"] == ["聯準會與利率"]
    assert c[1]["score"] == 2


def test_topic_and_tickers_bonus(tmp_path):
    now = _t(24, 8, 0)
    _write_archive(tmp_path, "2026-11-23", [
        _item("Quiet bakery opens downtown", _t(23, 22)),
        _item("Nvidia unveils new chips", _t(23, 22, 5), tickers=["NVDA"]),
        _item("Taiwan weather turns rainy", _t(23, 22, 10)),
    ])
    c = {x["title"]: x for x in _brief(tmp_path, now, notify=False)["news"]["candidates"]}
    assert c["Quiet bakery opens downtown"]["score"] == 2
    assert c["Nvidia unveils new chips"]["score"] == 2 + 2 + 1
    assert c["Nvidia unveils new chips"]["related_tickers"] == ["NVDA"]
    assert c["Taiwan weather turns rainy"]["topics"] == ["台灣"]
    assert c["Taiwan weather turns rainy"]["score"] == 3


def test_gap_detection(tmp_path):
    now = _t(24, 8, 0, )
    now = now.replace(second=12)
    items = []
    for h in [21, 22, 23, 0, 1, 4, 5, 6, 7]:  # 02、03 點沒有任何項目
        ts = _t(23, h) if h >= 21 else _t(24, h)
        # 非美國項目也算數
        items.append(_item(f"story number {h} here", ts + dt.timedelta(minutes=5), region="tw"))
    _write_archive(tmp_path, "2026-11-23", [i for i in items if i["first_seen_at"] < "2026-11-24"])
    _write_archive(tmp_path, "2026-11-24", [i for i in items if i["first_seen_at"] >= "2026-11-24"])
    brief = _brief(tmp_path, now, notify=False)
    assert brief["news"]["gaps"] == [{"start": _t(24, 2).isoformat(), "end": _t(24, 4).isoformat()}]
    assert any("11-24 02:00～04:00 沒有收到任何新項目" in w for w in brief["warnings"])


def test_market_failure_still_writes(tmp_path):
    def boom():
        raise RuntimeError("no network")

    brief = _brief(tmp_path, _t(24, 8), macro_loader=boom, notify=False)
    assert brief["market"]["status"] == "unavailable"
    assert "RuntimeError" in brief["market"]["error"] and "no network" in brief["market"]["error"]
    assert any("行情" in w for w in brief["warnings"])
    assert brief["summary"]["status"] == "pending_model"
    assert (tmp_path / "data" / "overnight_brief" / "2026-11-24.json").exists()
    assert (tmp_path / "data" / "overnight_brief" / "latest.json").exists()
    assert any("沒有任何美國新聞" in w for w in brief["warnings"])  # 仍然寫檔


def test_market_section_and_date_mismatch(tmp_path):
    brief = _brief(tmp_path, _t(24, 8), notify=False)
    m = brief["market"]
    assert m["status"] == "ok" and m["usdtwd"] == 32.1
    assert [r["symbol"] for r in m["indices"]] == ["^GSPC", "^IXIC", "^SOX"]  # 不含 ^TWII
    assert m["stocks"][0]["parent_tw_ticker"] == "2330"
    assert m["adr_premiums"][0]["adr_symbol"] == "TSM"
    assert not any("美股行情的日期" in w for w in brief["warnings"])

    brief = _brief(tmp_path, _t(24, 8), snap=_snap("2026-11-20"), notify=False)
    assert any("美股行情的日期是 2026-11-20，不是預期的 2026-11-23" in w for w in brief["warnings"])


def test_second_run_accumulates_and_notifies_once(tmp_path):
    n = FakeNotifier()
    first = _brief(tmp_path, _t(24, 8, 0), notifier=n)
    assert len(n.sent) == 1 and first["notified_at"] == first["generated_at"]
    again = _brief(tmp_path, _t(24, 8, 5), notifier=n)
    assert len(n.sent) == 1
    assert again["notified_at"] == first["notified_at"]
    assert [r["slot"] for r in again["runs"]] == ["morning", "morning"]
    pre = _brief(tmp_path, _t(24, 8, 50), slot="preopen", notifier=n)
    assert [r["slot"] for r in pre["runs"]] == ["morning", "morning", "preopen"]
    assert len(n.sent) == 1


def test_preopen_and_no_notify_do_not_notify(tmp_path):
    n = FakeNotifier()
    brief = _brief(tmp_path, _t(24, 8, 50), slot="preopen", notifier=n)
    assert n.sent == [] and brief["notified_at"] == ""
    _brief(tmp_path, _t(25, 8), notifier=n, notify=False)
    assert n.sent == []


def test_notification_text(tmp_path):
    n = FakeNotifier()
    _write_archive(tmp_path, "2026-11-23", [_item("Nvidia unveils new chips", _t(23, 22))])
    _brief(tmp_path, _t(24, 8), notifier=n)
    lines = n.sent[0].split("\n")
    assert lines[0] == "美股隔夜重點（11-24）"
    assert lines[1] == "S&P 500 +5.06%、NASDAQ +6.91%、費半 +14.11%、台積電 ADR +3.20%"
    assert lines[2] == "模型摘要尚未可用；候選新聞 1 則，詳見系統匣或儀表板"
    assert lines[3].startswith("注意：")  # 沒有存檔 collected_at 等警告

    n2 = FakeNotifier()
    def boom():
        raise RuntimeError("x")
    _brief(tmp_path / "other", _t(24, 8), macro_loader=boom, notifier=n2)
    assert n2.sent[0].split("\n")[1] == "行情暫時取不到"


def test_summarize_hook(tmp_path):
    ok = _brief(tmp_path, _t(24, 8), notify=False,
                summarize=lambda p: {"status": "ok", "points": ["重點一"], "model": "m",
                                     "generated_at": "t", "note": ""})
    assert ok["summary"]["points"] == ["重點一"]

    def bad(_):
        raise ValueError("quota")

    failed = _brief(tmp_path, _t(24, 8), notify=False, summarize=bad)
    assert failed["summary"]["status"] == "failed" and "quota" in failed["summary"]["note"]


def test_main_returns_2_on_error(tmp_path, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(ob, "build_brief", boom)
    assert ob.main(["--root", str(tmp_path), "--slot", "morning", "--no-notify", "--no-collect"]) == 2


def test_scheduler_has_overnight_jobs(tmp_path):
    from bot.scheduler import Scheduler

    sch = Scheduler(Settings(symbols=["2330"], _env_file=None), project_root=tmp_path, dry_run=True)
    jobs = {j.name: j for j in sch.jobs}
    a, b = jobs["overnight_brief"], jobs["overnight_brief_update"]
    assert a.module == b.module == "bot.overnight_brief" and a.console_script == ""
    assert a.run_once_per_day and b.run_once_per_day and not a.market_hours_only
    assert a.extra_args == ["--root", str(tmp_path), "--slot", "morning"]
    assert b.extra_args == ["--root", str(tmp_path), "--slot", "preopen"]
    assert (a.window_start, a.window_end) == (dt.time(8, 0), dt.time(8, 49))
    assert (b.window_start, b.window_end) == (dt.time(8, 50), dt.time(10, 0))
    assert a.report_kind == "" and a.target_date_mode == ""


# ---- 第三階段：API 新聞接線（docs/free-sources/finnhub.md B1～B11） ----

SECRET = "SECRET-FINNHUB-KEY-123"


def _api(title, ts, *, source="finnhub", sym="NVDA", url=None, tickers=None):
    return {"title": title, "url": url or f"http://api/{title}", "desc": "", "summary": "",
            "published_at": ts.astimezone(dt.timezone.utc).isoformat(), "source": "Reuters",
            "feed": f"{source}:{sym}", "region": "us", "lang": "en",
            "us_tickers": tickers if tickers is not None else [sym]}


def _write_api(root: Path, source: str, day: str, items) -> Path:
    path = root / "data" / "news" / f"api_{source}_{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items), encoding="utf-8")
    return path


def test_b1_read_api_news_generic_and_tolerant(tmp_path):
    _write_api(tmp_path, "finnhub", "2026-11-24", [_api("a", _t(24, 1)), "junk"])
    _write_api(tmp_path, "massive", "2026-11-23", [_api("b", _t(23, 22), source="massive")])
    _write_api(tmp_path, "marketaux", "2026-11-20", [_api("old", _t(20, 22))])  # 視窗外的日期
    (tmp_path / "data" / "news" / "api_fmp_2026-11-24.json").write_text("{bad", encoding="utf-8")
    _write_api(tmp_path, "x", "2026-11-24", {"not": "a list"})
    _write_archive(tmp_path, "2026-11-24", [_item("rss", _t(24, 1))])
    now = _t(24, 8)
    rows, sources = ob.read_api_news(tmp_path, ob.window_start(now), now)
    assert sorted(r["title"] for r in rows) == ["a", "b"]
    assert sources["finnhub"] == {"files": ["api_finnhub_2026-11-24.json"], "items": 1}
    assert sources["massive"]["items"] == 1
    assert sources["fmp"]["items"] == 0 and sources["x"]["items"] == 0
    assert "marketaux" not in sources and "rss" not in sources
    assert ob.read_api_news(tmp_path / "nope", ob.window_start(now), now) == ([], {})


def test_b2_api_merged_into_candidates_with_window_and_dedup(tmp_path):
    _write_archive(tmp_path, "2026-11-24", [_item("Fed holds rates steady", _t(24, 3), url="http://same")])
    _write_api(tmp_path, "finnhub", "2026-11-24", [
        _api("Nvidia unveils new chip", _t(24, 2)),
        _api("Fed holds rates steady dup", _t(24, 3), url="http://same"),   # 與 RSS 同 url
        _api("Ancient news", _t(22, 10)),                                    # 視窗外
    ])
    b = _brief(tmp_path, _t(24, 8), notify=False)
    titles = [c["title"] for c in b["news"]["candidates"]]
    assert "Nvidia unveils new chip" in titles and "Ancient news" not in titles
    assert "Fed holds rates steady" in titles and "Fed holds rates steady dup" not in titles
    assert b["news"]["items_in_window"] == 2
    assert b["news"]["rss_items_in_window"] == 1 and b["news"]["api_items_in_window"] == 1


def test_b3_gaps_only_from_rss(tmp_path):
    # RSS 只有 21 點一則，之後整晚沒有；API 每小時都有新聞也不能補掉缺口
    _write_archive(tmp_path, "2026-11-23", [_item("early", _t(23, 21, 10))])
    _write_api(tmp_path, "finnhub", "2026-11-24",
               [_api(f"api {h}", _t(24, h, 5)) for h in range(0, 7)])
    b = _brief(tmp_path, _t(24, 8), notify=False)
    assert b["news"]["gaps"] and b["news"]["gaps"][0]["start"].startswith("2026-11-23T22:00")
    assert b["news"]["api_items_in_window"] == 7


def test_b4_api_sources_status(tmp_path):
    _write_api(tmp_path, "finnhub", "2026-11-24", [_api("a", _t(24, 1)), _api("b", _t(24, 2))])
    _write_api(tmp_path, "massive", "2026-11-24", [_api("c", _t(24, 1), source="massive")])
    results = {"finnhub": {"status": "ok", "new_items": 2}, "marketaux": {"status": "skipped"}}
    b = _brief(tmp_path, _t(24, 8), notify=False, collect_results=results)
    src = b["news"]["api_sources"]
    assert src["finnhub"] == {"items_in_window": 2, "files": ["api_finnhub_2026-11-24.json"],
                              "collect": {"status": "ok", "new_items": 2}}
    assert src["massive"]["items_in_window"] == 1 and src["massive"]["collect"] is None
    assert src["marketaux"] == {"items_in_window": 0, "files": [], "collect": {"status": "skipped"}}
    assert not any("新聞收集" in w for w in b["warnings"])


def test_b5_candidate_us_tickers_union(tmp_path):
    _write_api(tmp_path, "finnhub", "2026-11-24", [
        _api("Nvidia AMD chip export curbs widen", _t(24, 1), tickers=["NVDA", "AMD"]),
        _api("Nvidia AMD chip export curbs widen further", _t(24, 2), sym="TSM", tickers=["TSM"]),
    ])
    b = _brief(tmp_path, _t(24, 8), notify=False)
    c = b["news"]["candidates"][0]
    assert c["us_tickers"] == ["AMD", "NVDA", "TSM"]
    assert c["feeds"] == ["finnhub:NVDA", "finnhub:TSM"]


def test_b13_source_count_by_publisher_for_api(tmp_path):
    same = [_api("Micron soars on record memory demand", _t(24, 1), sym="MU"),
            _api("Micron soars on record memory demand again", _t(24, 2), sym="AMD")]
    other = dict(_api("Micron soars on record memory demand today", _t(24, 3), sym="NVDA"),
                 source="Yahoo")
    _write_api(tmp_path, "finnhub", "2026-11-24", same + [other])
    _write_archive(tmp_path, "2026-11-24",
                   [_item("Micron soars on record memory demand rss", _t(24, 4), feed="rss1")])
    b = _brief(tmp_path, _t(24, 8), notify=False)
    c = b["news"]["candidates"][0]
    assert len(c["feeds"]) == 4                  # 顯示仍列出所有 feed
    assert c["source_count"] == 3                # Reuters、Yahoo、rss1
    assert c["score"] == 2 * 3 + 1               # 有主題（半導體：Micron）、無台股代號


def _fake_collect_writes(root, *, now):
    _write_api(Path(root), "finnhub", now.date().isoformat(), [_api("Collected story", _t(24, 6))])
    return {"status": "ok", "new_items": 1}


def _patch_main(monkeypatch, tmp_path, collectors):
    monkeypatch.setattr(ob, "API_NEWS_COLLECTORS", collectors)
    monkeypatch.setattr(ob, "now_tw", lambda: _t(24, 8))
    real = ob.build_brief

    def wrapped(root, **kw):
        return real(root, macro_loader=lambda: _snap(), **kw)

    monkeypatch.setattr(ob, "build_brief", wrapped)


def test_b6_main_collects_before_brief(tmp_path, monkeypatch, capsys):
    _patch_main(monkeypatch, tmp_path, {"finnhub": _fake_collect_writes})
    assert ob.main(["--root", str(tmp_path), "--slot", "morning", "--no-notify"]) == 0
    brief = json.loads((tmp_path / "data" / "overnight_brief" / "latest.json").read_text(encoding="utf-8"))
    assert [c["title"] for c in brief["news"]["candidates"]] == ["Collected story"]
    assert brief["news"]["api_sources"]["finnhub"]["collect"] == {"status": "ok", "new_items": 1}
    printed = json.loads(capsys.readouterr().out)
    assert printed["api_items_in_window"] == 1
    assert printed["api_sources"] == {"finnhub": {"items_in_window": 1, "collect": "ok"}}


def test_b7_collect_failures_only_warn(tmp_path, monkeypatch, capsys):
    def boom(root, *, now):
        raise RuntimeError(f"connect failed token={SECRET}")

    collectors = {
        "finnhub": boom,
        "massive": lambda root, *, now: {"status": "error", "errors": {"news": "auth: 401"}},
        "fmp": lambda root, *, now: {"status": "partial", "errors": {"NVDA": "x"}},
        "marketaux": lambda root, *, now: {"status": "quota_exceeded", "errors": {"AAPL": "q"}},
        "other": lambda root, *, now: {"status": "skipped"},
    }
    _patch_main(monkeypatch, tmp_path, collectors)
    assert ob.main(["--root", str(tmp_path), "--slot", "morning", "--no-notify"]) == 0
    text = (tmp_path / "data" / "overnight_brief" / "latest.json").read_text(encoding="utf-8")
    brief = json.loads(text)
    warns = [w for w in brief["warnings"] if "新聞收集" in w]
    assert len(warns) == 4
    assert any(w.startswith("finnhub 新聞收集失敗") for w in warns)
    assert any(w.startswith("fmp 新聞收集部分失敗") for w in warns)
    assert any(w.startswith("marketaux 新聞收集用量已達上限") for w in warns)
    assert not any(w.startswith("other") for w in warns)
    assert brief["news"]["api_sources"]["finnhub"]["collect"]["status"] == "failed"
    # B11：金鑰不出現在 brief 與輸出
    assert SECRET not in text and SECRET not in capsys.readouterr().out


def test_b8_no_collect_skips_collectors(tmp_path, monkeypatch):
    called = []
    _patch_main(monkeypatch, tmp_path, {"finnhub": lambda root, *, now: called.append(1) or {}})
    assert ob.main(["--root", str(tmp_path), "--slot", "preopen", "--no-collect"]) == 0
    assert called == []
    brief = json.loads((tmp_path / "data" / "overnight_brief" / "latest.json").read_text(encoding="utf-8"))
    assert brief["news"]["api_sources"] == {}


def test_b9_default_finnhub_collector_skips_without_key(tmp_path, monkeypatch):
    from bot.free_sources import collect_finnhub as cf

    assert ob.API_NEWS_COLLECTORS["finnhub"] == "bot.free_sources.collect_finnhub"

    def no_net(*a, **k):
        raise AssertionError("不應連網")

    monkeypatch.setattr(cf.FinnhubClient, "_get", no_net)
    monkeypatch.setattr("bot.config.Settings", lambda: SimpleNamespace(finnhub_api_key=""))
    results = ob.run_api_collectors(tmp_path, _t(24, 8))
    assert results == {"finnhub": {"status": "skipped"}}
    assert not (tmp_path / "data").exists()
