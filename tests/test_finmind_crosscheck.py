"""FinMind 交叉核對（離線：假 client、暫存 SQLite）。對應 docs/free-sources/finmind.md 驗收標準。"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from types import SimpleNamespace

from bot.free_sources import finmind_crosscheck as fc
from bot.free_sources.base import SourceError
from bot.free_sources.quota import QuotaExceeded

NOW = dt.datetime(2026, 11, 24, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))


def make_db(path, revenue=(), quarters=()):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE monthly_revenue (symbol TEXT, year_month TEXT, revenue REAL)")
    con.execute("CREATE TABLE quarterly_report (symbol TEXT, period TEXT, eps REAL, revenue REAL, op_margin REAL)")
    con.executemany("INSERT INTO monthly_revenue VALUES (?,?,?)", revenue)
    con.executemany("INSERT INTO quarterly_report VALUES (?,?,?,?,?)", quarters)
    con.commit()
    con.close()
    return path


def rev_rows(sym="2330"):
    # date 是營收月份的下個月 1 日；revenue 單位元
    return [{"stock_id": sym, "date": f"2026-{m + 1:02d}-01", "revenue": v * 1000,
             "revenue_year": 2026, "revenue_month": m}
            for m, v in ((8, 500), (9, 600), (10, 700), (7, 400))]


def stmt_rows(sym="2330"):
    rows = []
    # Q1、Q2 單季：營收 1000/2000 千元（元 = x1000）、EPS 1.0/2.0、營業利益 300/800 千元
    for date, rev, eps, op in (("2026-03-31", 1000, 1.0, 300), ("2026-06-30", 2000, 2.0, 800)):
        rows += [{"date": date, "stock_id": sym, "type": "Revenue", "value": rev * 1000, "origin_name": ""},
                 {"date": date, "stock_id": sym, "type": "EPS", "value": eps, "origin_name": ""},
                 {"date": date, "stock_id": sym, "type": "OperatingIncome", "value": op * 1000, "origin_name": ""}]
    return rows


class FakeClient:
    def __init__(self, revenue=None, statements=None, exc=None):
        self.calls = []
        self.revenue = rev_rows() if revenue is None else revenue
        self.statements = stmt_rows() if statements is None else statements
        self.exc = exc

    def data(self, dataset, data_id, start, end):
        self.calls.append((dataset, data_id, start, end))
        if self.exc is not None:
            raise self.exc
        return self.revenue if dataset == fc.REVENUE_DATASET else self.statements


GOOD_REV = [("2330", "2026-08", 500.0), ("2330", "2026-09", 600.0), ("2330", "2026-10", 700.0)]
# 累計：Q1 = 1000 / 1.0 / 30%；Q2 = 3000 / 3.0 / (1100/3000=36.667%)
GOOD_Q = [("2330", "2026Q1", 1.0, 1000.0, 30.0), ("2330", "2026Q2", 3.0, 3000.0, 36.67)]


def statuses(report, kind=None):
    return {(i["period"], i["field"]): i["status"] for i in report["items"] if kind in (None, i["kind"])}


def test_all_match_and_report_written(tmp_path):
    db = make_db(tmp_path / "s.db", GOOD_REV, GOOD_Q)
    client = FakeClient()
    rep = fc.run_crosscheck(tmp_path, now=NOW, client=client, symbols=["2330"], db_path=db)
    assert rep["status"] == "ok" and rep["source"] == "FinMind" and rep["requests"] == 2
    assert rep["summary"]["mismatch"] == 0 and rep["summary"]["match"] == rep["summary"]["total"] == 9
    assert statuses(rep)[("2026-10", "revenue")] == "match"          # 元 -> 千元
    assert statuses(rep)[("2026Q2", "eps")] == "match"               # Q1+Q2 累計
    assert [c[0] for c in client.calls] == [fc.REVENUE_DATASET, fc.STATEMENT_DATASET]
    path = tmp_path / "data/reports/finmind_crosscheck_2026-11-24.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["source"] == "FinMind" and saved["date"] == "2026-11-24" and len(saved["items"]) == 9
    assert not list(path.parent.glob("*.tmp"))
    assert rep["inconsistent"] is False


def test_mismatch_and_tolerance(tmp_path):
    q = [("2330", "2026Q1", 1.04, 1000.5, 30.0),      # EPS 差 0.04、營收差 0.05% 都在容許內
         ("2330", "2026Q2", 3.2, 3000.0, 36.67)]      # EPS 差 0.2 超出
    rev = [("2330", "2026-08", 500.0), ("2330", "2026-09", 640.0), ("2330", "2026-10", 700.0)]
    db = make_db(tmp_path / "s.db", rev, q)
    rep = fc.run_crosscheck(tmp_path, now=NOW, client=FakeClient(), symbols=["2330"], db_path=db)
    st = statuses(rep)
    assert st[("2026Q1", "eps")] == "match" and st[("2026Q1", "revenue")] == "match"
    assert st[("2026Q2", "eps")] == "mismatch" and st[("2026-09", "revenue")] == "mismatch"
    assert rep["summary"]["mismatch"] == 2 and rep["inconsistent"] is True


def test_db_missing_and_source_missing(tmp_path):
    db = make_db(tmp_path / "s.db", GOOD_REV[:1], [("2330", "2026Q1", 0.0, 0.0, 0.0)])   # 缺月份、值為 0
    # FinMind 只有 Q2 單季（缺 Q1）→ 累計無法驗證
    only_q2 = [r for r in stmt_rows() if r["date"] == "2026-06-30"]
    rep = fc.run_crosscheck(tmp_path, now=NOW, client=FakeClient(statements=only_q2), symbols=["2330"], db_path=db)
    st = statuses(rep)
    assert st[("2026-10", "revenue")] == "db_missing" and st[("2026-08", "revenue")] == "match"
    assert st[("2026Q2", "eps")] == "source_missing" and st[("2026Q2", "revenue")] == "source_missing"
    rep2 = fc.run_crosscheck(tmp_path, now=NOW, client=FakeClient(), symbols=["2330"], db_path=db)
    assert statuses(rep2)[("2026Q1", "eps")] == "db_missing"      # 0 視為缺
    assert rep2["has_db_missing"] is True and rep2["inconsistent"] is False


def test_skipped_without_token(tmp_path):
    rep = fc.run_crosscheck(tmp_path, now=NOW, settings=SimpleNamespace(finmind_token=""),
                            symbols=["2330"], db_path=tmp_path / "none.db")
    assert rep["status"] == "skipped"
    assert not (tmp_path / "data/reports").exists()


def test_quota_exceeded_stops(tmp_path):
    db = make_db(tmp_path / "s.db", GOOD_REV, GOOD_Q)

    class Quota(FakeClient):
        def data(self, *a):
            self.calls.append(a)
            raise QuotaExceeded("finmind", "hourly", 500)

    client = Quota()
    rep = fc.run_crosscheck(tmp_path, now=NOW, client=client, symbols=["2330", "1514"], db_path=db)
    assert rep["status"] == "quota_exceeded" and rep["requests"] == 0 and len(client.calls) == 1
    assert (tmp_path / "data/reports/finmind_crosscheck_2026-11-24.json").exists()

    http402 = FakeClient(exc=SourceError("finmind", 402, "limit"))
    rep = fc.run_crosscheck(tmp_path, now=NOW, client=http402, symbols=["2330", "1514"], db_path=db)
    assert rep["status"] == "quota_exceeded" and len(http402.calls) == 1


def test_source_error_is_partial_and_redacted(tmp_path):
    db = make_db(tmp_path / "s.db", GOOD_REV, GOOD_Q)

    class Flaky(FakeClient):
        def data(self, dataset, *a):
            if dataset == fc.STATEMENT_DATASET:
                raise SourceError("finmind", 500, "boom token=SECRET123")
            return super().data(dataset, *a)

    rep = fc.run_crosscheck(tmp_path, now=NOW, client=Flaky(), symbols=["2330"], db_path=db)
    assert rep["status"] == "partial" and rep["summary"]["total"] == 3
    assert "SECRET123" not in json.dumps(rep) and "2330:statements" in rep["errors"]


def test_main_dry_run_and_default_symbols(tmp_path, capsys):
    (tmp_path / "data").mkdir()
    (tmp_path / "data/held_symbols_seen.json").write_text(
        json.dumps({"symbols": {"1514": {"first_seen": "2026-11-20"}}}), encoding="utf-8")
    assert fc.main(["--root", str(tmp_path), "--dry-run"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "dry_run" and out["symbols"] == ["1514"] and out["planned_requests"] == 2
    assert fc.main(["--root", str(tmp_path), "--symbols", "2330, 2317", "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["symbols"] == ["2330", "2317"]
    assert not (tmp_path / "data/reports").exists()
