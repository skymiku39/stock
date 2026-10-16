import datetime as dt
import json
from types import SimpleNamespace

from bot import daily_research as daily
from bot.config import Settings
from bot.verified_research_batch import write_json


def test_daily_all_scope_restart_retry_and_day_rollover(tmp_path, monkeypatch):
    from bot import data_pipeline, verified_research_batch
    day = [dt.datetime(2026, 10, 16, 9, tzinfo=dt.timezone(dt.timedelta(hours=8)))]
    monkeypatch.setattr(daily, "now_tw", lambda: day[0])
    monkeypatch.setattr(daily, "Settings", lambda **kw: SimpleNamespace(llm_enabled=True))
    tickers = [str(n) for n in range(1000, 1200)]
    monkeypatch.setattr(daily, "tracked_tickers", lambda *a: tickers)
    collection, calls = [], []
    def collect(config):
        collection.append(config)
        assert not config.auto_research_focus and not config.run_llm_analysis
        return SimpleNamespace(run_id="source", errors=[], warnings=[], focus_tickers=tickers)
    monkeypatch.setattr(data_pipeline, "run_full_pipeline", collect)
    monkeypatch.setattr(daily, "refresh_official", lambda *a: [])
    def verified(args):
        calls.append(args)
        targets = args[args.index("--tickers")+1:args.index("--output-dir")]
        assert len(targets) == 200 and "--require-collected-today" in args
        out = tmp_path / "data/daily_research" / day[0].date().isoformat() / "verified"
        results = {t: {"status": "completed"} for t in targets}
        if len(calls) == 1:
            results[targets[0]] = {"status": "excluded"}
        write_json(out / "verification-summary.json", {"results": results})
        return 3 if len(calls) == 1 else 0
    monkeypatch.setattr(verified_research_batch, "main", verified)
    assert daily.run_daily(tmp_path) == 3
    first = json.loads((tmp_path / "data/daily_research/2026-10-16/status.json").read_text())
    assert first["completed"] == 199 and not first["complete"] and len(first["pending"]) == 1
    assert daily.run_daily(tmp_path) == 0
    assert len(collection) == 1  # resume collection rather than repeat successes
    day[0] += dt.timedelta(days=1)
    assert daily.run_daily(tmp_path) == 0
    assert len(collection) == 2


def test_collection_failure_remains_partial_even_if_analysis_succeeds(tmp_path, monkeypatch):
    from bot import data_pipeline, verified_research_batch
    monkeypatch.setattr(daily, "Settings", lambda **kw: SimpleNamespace(llm_enabled=True))
    monkeypatch.setattr(daily, "tracked_tickers", lambda *a: ["2330", "UNKNOWN HK"])
    monkeypatch.setattr(daily, "refresh_official", lambda *a: [])
    monkeypatch.setattr(data_pipeline, "run_full_pipeline", lambda *a: SimpleNamespace(
        run_id="source", errors=["network"], warnings=[], focus_tickers=[]))
    def verified(args):
        out = args[args.index("--output-dir") + 1]
        from pathlib import Path
        write_json(Path(out) / "verification-summary.json", {"results": {"2330": {"status": "completed"}}})
        assert "UNKNOWN HK" not in args
        return 0
    monkeypatch.setattr(verified_research_batch, "main", verified)
    assert daily.run_daily(tmp_path) == 3
    status = json.loads(next((tmp_path/"data/daily_research").glob("*/status.json")).read_text())
    assert not status["complete"] and status["excluded"] == ["UNKNOWN HK"]


def test_scheduler_daily_mode_covers_weekends_without_ticker_limit(tmp_path):
    from bot.scheduler import Scheduler
    s = Scheduler(Settings(_env_file=None, scheduler_daily_all_enabled=True), project_root=tmp_path)
    job = next(j for j in s.jobs if j.name == "research")
    assert job.module == "bot.daily_research" and job.extra_args == []
    assert job.interval_min == 30 and not job.market_hours_only
    assert s._job_eligible(job, dt.datetime(2026, 10, 17, 20), once=False)


def test_tracking_includes_all_holdings_not_only_consensus(tmp_path, monkeypatch):
    from bot import watchlist, active_etf
    monkeypatch.setattr(watchlist, "load", lambda root: SimpleNamespace(items=[SimpleNamespace(ticker="2330")]))
    monkeypatch.setattr(active_etf, "load_active_etfs", lambda root: [SimpleNamespace(symbol="ETF")])
    monkeypatch.setattr(active_etf, "list_holdings_dates", lambda *a: [dt.date(2026, 10, 16)])
    monkeypatch.setattr(active_etf, "load_holdings", lambda *a: SimpleNamespace(holdings=[SimpleNamespace(ticker="NVDA US")]))
    assert daily.tracked_tickers(tmp_path, SimpleNamespace(symbols=["0050"])) == ["0050", "2330", "NVDA US"]


def test_old_raw_cannot_satisfy_daily_collection(tmp_path, monkeypatch):
    from bot.source_registry import SourceRegistry
    from bot import verified_research_batch as batch
    with SourceRegistry(tmp_path) as registry:
        source = registry.capture_document("monthly_revenue_twse", '[{"公司代號":"2330","營收":"100"}]',
            source_url="https://openapi.twse.com.tw/data", suffix="json")
        registry.db.conn.execute("UPDATE source_documents SET retrieved_at=? WHERE document_id=?",
                                 ("2026-01-01T00:00:00+08:00", source.document_id))
        registry.db.conn.commit()
    out = tmp_path / "output"
    assert batch.main(["--root", str(tmp_path), "--tickers", "2330", "--output-dir", str(out),
                       "--require-collected-today", "--execute"]) == 3
    summary = json.loads((out / "verification-summary.json").read_text())
    assert not summary["complete"] and summary["excluded_tickers"] == ["2330"]
    assert not list(out.glob("response-*.json"))


def test_daily_successes_are_not_resubmitted_with_pending_symbols(tmp_path, monkeypatch):
    from bot import data_pipeline, verified_research_batch, verified_research_audit
    date = daily.now_tw().date().isoformat()
    out = tmp_path / "data/daily_research" / date
    write_json(out / "status.json", {"collection_complete": True, "official_complete": True,
                                     "tickers": ["2330", "2317"]})
    write_json(out / "verified/reports/2330.json", {"generated_at": date + "T08:00:00+08:00", "input": {}})
    monkeypatch.setattr(daily, "Settings", lambda **kw: SimpleNamespace(llm_enabled=True))
    monkeypatch.setattr(daily, "tracked_tickers", lambda *a: ["2330", "2317"])
    monkeypatch.setattr(verified_research_audit, "verify_saved", lambda *a: None)
    def verified(args):
        assert args[args.index("--tickers") + 1:args.index("--output-dir")] == ["2317"]
        write_json(out / "verified/verification-summary.json", {"results": {"2317": {"status": "completed"}}})
        return 0
    monkeypatch.setattr(verified_research_batch, "main", verified)
    assert daily.run_daily(tmp_path) == 0
    result = json.loads((out / "status.json").read_text())
    assert result["completed"] == 2 and result["results"]["2330"]["reused"]
