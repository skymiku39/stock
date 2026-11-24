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
    assert ob.main(["--root", str(tmp_path), "--slot", "morning", "--no-notify"]) == 2


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
