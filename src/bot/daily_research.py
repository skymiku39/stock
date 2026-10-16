"""Calendar-day, all-tracked research with durable recovery and exact evidence."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from bot.config import Settings
from bot.utils import now_tw
from bot.verified_research_batch import write_json


def tracked_tickers(root, settings):
    from bot.watchlist import load
    from bot.active_etf import load_active_etfs, list_holdings_dates, load_holdings
    tickers = set(settings.symbols)
    tickers.update(item.ticker for item in load(root).items)
    for etf in load_active_etfs(root):
        dates = list_holdings_dates(etf.symbol, root)
        if dates:
            snapshot = load_holdings(etf.symbol, dates[0], root)
            if snapshot:
                tickers.update(h.ticker for h in snapshot.holdings)
    return sorted(t.strip() for t in tickers if t and t.strip())


def refresh_official(root):
    from bot import fundamentals_fetcher as fetcher
    errors = []
    for name in ("fetch_monthly_revenue_all", "fetch_valuation_all",
                 "fetch_quarterly_financials_all", "fetch_dividend_all"):
        try:
            rows = getattr(fetcher, name)(root=root, use_cache=False)
            if not rows:
                errors.append(name + ": no rows")
        except Exception as exc:
            errors.append(name + ": " + str(exc))
    return errors


def run_daily(root):
    from bot.data_pipeline import PipelineConfig, run_full_pipeline
    from bot.verified_research_batch import main as verified
    settings = Settings(_env_file=str(root / ".env"))
    date = now_tw().date().isoformat()
    out = root / "data/daily_research" / date
    path = out / "status.json"
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    state.update(complete=False, phase="discovering", attempted_at=now_tw().isoformat())
    write_json(path, state)
    # Preserve the day's membership and add newly followed symbols on each retry.
    tickers = sorted(set(state.get("tickers", [])) | set(tracked_tickers(root, settings)))
    state.update(date=date, tickers=tickers, requested=len(tickers), complete=False, phase="collecting",
                 attempted_at=now_tw().isoformat(), attempts=state.get("attempts", 0) + 1)
    write_json(path, state)
    if not state.get("collection_complete"):
        try:
            run = run_full_pipeline(PipelineConfig(
                project_root=root, llm_settings=settings, focus_tickers=tickers,
                auto_research_focus=False, run_llm_analysis=False,
                generate_brief=settings.llm_enabled, generate_us_brief=settings.llm_enabled))
            state["collection_run"] = run.run_id
            state["collection_errors"] = list(run.errors)
            state["collection_warnings"] = list(run.warnings)
            tickers = sorted(set(tickers) | set(run.focus_tickers) | set(tracked_tickers(root, settings)))
            state["collection_complete"] = not run.errors and not run.warnings
        except Exception as exc:
            state["collection_errors"] = [str(exc)]
            state["collection_complete"] = False
    # Retry bulk official sources only until a complete fetch is recorded today.
    retry_domestic = any(" " not in t for t in state.get("excluded", []))
    if not state.get("official_complete") or retry_domestic:
        state["official_errors"] = refresh_official(root)
        state["official_complete"] = not state["official_errors"]
    state.update(tickers=tickers, requested=len(tickers), phase="analysing")
    write_json(path, state)
    # Never infer identities for unrecognized source symbols.
    valid = [t for t in tickers if re.fullmatch(r"[A-Za-z0-9.^-]+(?: (?:US|JP|KS|GY))?", t)]
    unsupported = {t: {"status": "excluded", "reason": "unrecognized ticker identity"}
                   for t in tickers if t not in valid}
    if not settings.llm_enabled:
        state.update(phase="failed", error="LLM disabled; no analysis executed")
        write_json(path, state)
        return 2
    # Resume only the unfinished symbols; validated daily successes are not
    # invalidated by an unrelated source refresh's retrieval timestamp.
    from bot.verified_research_audit import verify_saved
    accepted = {}
    for ticker in valid:
        try:
            report = json.loads((out / "verified/reports" / (ticker + ".json")).read_text(encoding="utf-8"))
            if not report.get("generated_at", "").startswith(date):
                continue
            verify_saved(root, report, report["input"])
            accepted[ticker] = {"status": "completed", "reused": True}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    valid = [t for t in valid if t not in accepted]
    result = 0
    if valid:
        result = verified(["--root", str(root), "--tickers", *valid,
                           "--output-dir", str(out / "verified"),
                           "--require-collected-today", "--execute"])
    summary_path = out / "verified/verification-summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if valid and summary_path.exists() else {}
    state["results"] = {**accepted, **summary.get("results", {}), **unsupported}
    state["completed"] = sum(v.get("status") == "completed" for v in state["results"].values())
    state["excluded"] = [t for t, v in state["results"].items() if v.get("status") == "excluded"]
    state["pending"] = [t for t in tickers if state["results"].get(t, {}).get("status") != "completed"]
    state["complete"] = now_tw().date().isoformat() == date and bool(tickers) and not state["pending"] and state.get("collection_complete", False) and state.get("official_complete", False)
    state.update(phase="complete" if state["complete"] else "partial", finished_at=now_tw().isoformat())
    write_json(path, state)
    print(f"Daily {date}: requested={len(tickers)} completed={state['completed']} excluded={len(state['excluded'])} pending={len(state['pending'])}", flush=True)
    return 0 if state["complete"] else (result or 3)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    from bot.scheduler import _ProcessLock
    # Separate from the daemon lock; also protects manual CLI invocations.
    lock = _ProcessLock(args.root.resolve() / "data/daily_research")
    if not lock.acquire():
        return 4
    try:
        return run_daily(args.root.resolve())
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
