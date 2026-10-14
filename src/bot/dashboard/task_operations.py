"""UI-free operations used by the dashboard task queue."""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import math
import types
from pathlib import Path
from typing import Union, get_args, get_origin, get_type_hints

import numpy as np

from bot.dashboard.task_service import TaskResult


def to_data(value):
    # Pandas calculations retain NumPy scalar types, including bool_.
    if isinstance(value, np.generic):
        return to_data(value.item())
    if isinstance(value, np.ndarray):
        return to_data(value.tolist())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if dataclasses.is_dataclass(value):
        return {f.name: to_data(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(k): to_data(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_data(v) for v in value]
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value


def restore(cls, value):
    """Restore only types from our trusted dataclass schema, never from stored class names."""
    if value is None:
        return None
    origin, args = get_origin(cls), get_args(cls)
    if origin in (Union, types.UnionType):
        return restore(next(t for t in args if t is not type(None)), value)
    if origin is list:
        return [restore(args[0], v) for v in value]
    if origin is dict:
        return {k: restore(args[1], v) for k, v in value.items()}
    if cls in (dt.date, dt.datetime):
        return cls.fromisoformat(value)
    if cls is Path:
        return Path(value)
    if dataclasses.is_dataclass(cls):
        hints = get_type_hints(cls)
        return cls(**{f.name: restore(hints.get(f.name), value[f.name])
                      for f in dataclasses.fields(cls) if f.name in value})
    return value


def score_snapshot(snap):
    from bot.scoring import compute_scorecard
    from bot.fundamentals_fetcher import snapshot_to_dict as fund_dict
    from bot.technicals import snapshot_to_dict as tech_dict
    return compute_scorecard(
        ticker=snap.ticker, name=snap.name, price=snap.price,
        pct_change=snap.pct_change, volume=snap.volume,
        llm_analysis=snap.llm_analysis, logic_result=snap.logic_check,
        consensus=snap.consensus, new_build_signal=snap.new_build_signal,
        add_signal=snap.add_signal, chip_summary=snap.chip_summary,
        fundamental=fund_dict(snap.fundamentals) if snap.fundamentals else None,
        technical_snapshot=tech_dict(snap.technicals) if snap.technicals else None,
        distribution_label=snap.distribution_label, distribution_detail=snap.distribution_detail,
        distribution_score=snap.distribution_score,
        has_distribution=bool(snap.distribution_trend and snap.distribution_trend.weeks),
        macro_snapshot=snap.macro_snapshot, related_us_stocks=snap.us_related,
        adr_premium=snap.adr_premium, fetched_at=snap.fetched_at,
    )


def restore_cards(data):
    from bot.ticker_view import TickerSnapshot
    from bot.scoring import StockScorecard
    return [(restore(TickerSnapshot, row["snapshot"]), restore(StockScorecard, row["scorecard"]))
            for row in data]


def execute_task(kind, params, root, progress):
    if kind in ("ticker", "watchlist"):
        from bot.ticker_view import build_snapshot
        rows = []
        tickers = params["tickers"]
        options = {"auto_fill_missing": False, "auto_llm": False, **params.get("options", {})}
        for index, ticker in enumerate(tickers):
            progress(f"[{index + 1}/{len(tickers)}] {ticker}：讀取資料與計算評分", index / len(tickers))
            snap = build_snapshot(ticker, root, name_hint=params.get("names", {}).get(ticker, ""),
                                  macro_cache_only=not options.get("auto_fill_missing", False), **options)
            rows.append({"snapshot": to_data(snap), "scorecard": to_data(score_snapshot(snap))})
        return TaskResult(rows, f"已完成 {len(rows)} 檔分析")

    if kind == "research":
        from bot.auto_llm import auto_research_ticker
        from bot import watchlist
        names = {i.ticker: i.name for i in watchlist.load(root).items}
        rows, warnings = [], []
        for index, ticker in enumerate(params["tickers"]):
            progress(f"[{index + 1}/{len(params['tickers'])}] {ticker}：搜尋 MOPS / 新聞 / 網頁，整理 LLM 研究",
                     index / len(params["tickers"]))
            try:
                result = auto_research_ticker(ticker, root=root, name_hint=names.get(ticker, ""),
                                              force_refresh=params.get("force", False), refresh_calendar=False)
                if not result:
                    raise ValueError("素材不足或 LLM 未產生結果")
                rows.append({"ticker": ticker, "name": names.get(ticker, ""),
                             "sentiment": result.get("sentiment"), "score": result.get("sentiment_score"),
                             "confidence": result.get("confidence"), "catalyst_outlook": result.get("catalyst_outlook", ""),
                             "logic_verdict": (result.get("logic_check") or {}).get("verdict", ""),
                             "logic_suggestion": (result.get("logic_check") or {}).get("suggestion", ""), "_raw": result})
            except Exception as exc:
                warnings.append(f"{ticker}：{exc}")
                rows.append({"ticker": ticker, "error": str(exc)})
        if len(warnings) == len(rows):
            raise RuntimeError("；".join(warnings))
        return TaskResult(rows, f"研究完成：{len(rows) - len(warnings)} 檔成功、{len(warnings)} 檔未完成", warnings)

    if kind == "pipeline":
        from bot.config import Settings
        from bot.data_pipeline import PipelineConfig, PresentationInput, run_full_pipeline
        settings = Settings(_env_file=str(root / ".env"))
        config = PipelineConfig(project_root=root, llm_settings=settings,
                                gemini_api_key=settings.gemini_api_key, gemini_model=settings.gemini_model,
                                presentation_inputs=[PresentationInput(**p) for p in params.get("presentations", [])],
                                **params["config"])
        progress("抓取研究資料 → 計算共識 → 產出分析；各來源完成後保存結果")
        result = run_full_pipeline(config)
        return TaskResult({"run_id": result.run_id}, f"研究管線完成 · {result.run_id}", result.errors + result.warnings)

    if kind in ("intraday", "nextday"):
        from bot.config import Settings
        from bot.utils import now_tw
        settings = Settings(_env_file=str(root / ".env"))
        progress("整理新聞與行情，等待 LLM 產出報告")
        if kind == "intraday":
            if params["date"] != now_tw().date().isoformat():
                raise ValueError("已跨過報告日期，請重新選擇今天再送出")
            from bot.intraday_pipeline import run_intraday
            from bot.pipeline_shared import intraday_report_to_dict
            result = run_intraday(project_root=root, settings=settings, force_refresh_news=params["refresh"])
            data = intraday_report_to_dict(result)
        else:
            from bot.next_day_watch_pipeline import run_next_day_watch, _report_to_json
            result = run_next_day_watch(project_root=root, settings=settings, mode=params["mode"],
                                        target_date=dt.date.fromisoformat(params["date"]),
                                        force_refresh_news=params["mode"] == "draft",
                                        force_refresh_macro=params["mode"] == "update")
            data = _report_to_json(result)
        return TaskResult(data, "報告已保存", result.errors)

    if kind == "presentation":
        from bot.llm_analyzer import GeminiClient, analyze_presentation
        from bot.env_io import load_env
        env = load_env(root / ".env")
        progress("等待 LLM 解析逐字稿")
        client = GeminiClient(api_key=env.get("GEMINI_API_KEY", ""), model=env.get("GEMINI_MODEL", "gemini-2.5-flash"), root=root)
        result = analyze_presentation(params["text"], ticker=params["ticker"], client=client)
        if not result.enabled:
            raise ValueError("LLM 未啟用，沒有產生分析")
        return TaskResult(to_data(result), f"{params['ticker']} 逐字稿分析已保存", result.validation_errors)

    if kind == "portfolio_bundle":
        from bot.ticker_view import build_snapshot, snapshot_to_dict
        from bot.portfolio_analysis import build_portfolio_analysis_bundle
        snapshots = {}
        for index, row in enumerate(params["rows"]):
            symbol = str(row["代號"])
            progress(f"[{index + 1}/{len(params['rows'])}] {symbol}：整理持股資料", index / len(params["rows"]))
            snap = build_snapshot(symbol, root, name_hint=str(row.get("名稱") or ""),
                                  macro_cache_only=not params["options"].get("auto_fill_missing"), **params["options"])
            snapshots[symbol] = snapshot_to_dict(snap)
        bundle = build_portfolio_analysis_bundle(rows=params["rows"], snapshots=snapshots,
                                                 broker_meta=params["broker"], warnings=params["warnings"])
        directory = root / "data" / "portfolio_analysis"
        directory.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(bundle, ensure_ascii=False, indent=2)
        (directory / f"portfolio_bundle_{dt.datetime.now():%Y%m%d-%H%M%S}.json").write_text(encoded, encoding="utf-8")
        (directory / "latest_bundle.json").write_text(encoded, encoding="utf-8")
        return TaskResult(bundle, f"已整理 {len(params['rows'])} 檔持股資料")

    if kind in ("portfolio_analysis", "macro_brief"):
        from bot.config import Settings
        from bot.llm_analyzer import GeminiClient, gemini_call
        from bot.prompt_registry import get_registry
        settings = Settings(_env_file=str(root / ".env"))
        client = GeminiClient(settings=settings, model=settings.gemini_model, root=root)
        progress("等待 LLM 產出分析內容")
        inputs = dict(params["inputs"])
        metadata = {"task": kind, "source": "dashboard"}
        if kind == "macro_brief":
            from bot.pipeline_brief import prepare_brief_metadata
            metadata = prepare_brief_metadata(root=root, task="us_market_brief",
                run_id="dashboard:" + dt.datetime.now().isoformat(), variables=inputs,
                macro_snapshot=params.get("macro_snapshot"))
        raw, info = gemini_call("us_market_brief" if kind == "macro_brief" else kind,
                                client=client, root=root, registry=get_registry(root / "prompts"),
                                metadata=metadata, **inputs)
        if not raw:
            raise ValueError("LLM 未產出內容，請查看 LLM 呼叫紀錄")
        path = ""
        if kind == "portfolio_analysis":
            directory = root / "data" / "portfolio_analysis"
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f"portfolio_analysis_{dt.datetime.now():%Y%m%d-%H%M%S}.md"
            header = f"<!-- prompt={info.get('prompt_id', '')} version={info.get('prompt_version', '')} model={info.get('model', '')} -->\n\n"
            target.write_text(header + raw, encoding="utf-8")
            (directory / "latest_analysis.md").write_text(header + raw, encoding="utf-8")
            path = str(target)
        return TaskResult({"markdown": raw, "info": info, "path": path}, "分析內容已保存")

    if kind == "logic_check":
        from bot.config import Settings
        from bot.llm_analyzer import ChipsContext, GeminiClient, PresentationAnalysis, logic_check
        settings = Settings(_env_file=str(root / ".env"))
        client = GeminiClient(settings=settings, model=settings.gemini_model, root=root)
        progress("使用籌碼摘要反查分析結論")
        result = logic_check(PresentationAnalysis(**params["analysis"]), ChipsContext(**params["chips"]), client)
        return TaskResult(to_data(result), "言行反查已保存", result.validation_errors)

    if kind == "live_review":
        from bot.config import Settings
        from bot.intraday_live import build_live_tracking_rows, save_live_review
        from bot.llm_analyzer import GeminiClient, gemini_call
        from bot.market_macro import fetch_macro_snapshot, macro_to_dict
        from bot.news_fetcher import fetch_today_news, news_to_compact_text
        from bot.prompt_registry import get_registry
        from bot.utils import now_tw
        if params["date"] != now_tw().date().isoformat():
            raise ValueError("已跨過追蹤日期，請重新載入今天的報告")
        progress("刷新追蹤行情、跨市場與新聞資料")
        report = params["report"]
        tracking = build_live_tracking_rows(report, root=root, max_tickers=params["max_tickers"],
                                            refresh_quotes=True, refresh_technicals=True, refresh_chips=True,
                                            refresh_news=True, include_news=True)
        macro = fetch_macro_snapshot(root=root, force_refresh=True, use_cache=True)
        news = fetch_today_news(limit=120, use_cache=False, force_refresh=True, root=root)
        settings = Settings(_env_file=str(root / ".env"))
        progress("等待 LLM 檢討早盤判斷")
        raw, info = gemini_call(
            "intraday_live_review", client=GeminiClient(settings=settings, model=settings.gemini_model, root=root),
            registry=get_registry(root / "prompts"), metadata={"task": "intraday_live_review", "asof": params["date"]},
            asof_time=now_tw().isoformat(timespec="seconds"),
            original_brief=report.get("brief_md") or report.get("overall_brief") or "",
            themes_json=json.dumps(report.get("themes") or [], ensure_ascii=False, indent=2),
            live_rows_json=json.dumps(tracking.get("rows") or [], ensure_ascii=False, indent=2),
            macro_json=json.dumps(macro_to_dict(macro), ensure_ascii=False, indent=2),
            news_text=news_to_compact_text(news, max_chars=5000),
        )
        if not raw:
            raise ValueError("LLM 未產出內容，請查看 LLM 呼叫紀錄")
        payload = {"generated_at": now_tw().isoformat(timespec="seconds"), "prompt_id": info.get("prompt_id", ""),
                   "prompt_version": info.get("prompt_version", ""), "llm_info": info, "tracking": tracking}
        save_live_review(root=root, report_date=dt.date.fromisoformat(params["date"]), markdown=raw, payload=payload)
        return TaskResult({"markdown": raw, **payload}, "LLM 即時推演已保存")

    if kind == "macro":
        from bot.market_macro import fetch_macro_snapshot, macro_to_dict
        progress("讀取 yfinance / TWSE / TAIFEX 跨市場資料")
        data = macro_to_dict(fetch_macro_snapshot(root=root, force_refresh=True))
        if not data.get("indices") and not data.get("stocks"):
            raise ValueError("來源沒有回傳可用的跨市場資料")
        return TaskResult(data, "跨市場資料已更新")

    if kind == "movers":
        from bot.market_movers import fetch_market_movers
        progress("掃描全市場 MIS 報價，首次約需 10–20 秒")
        data = fetch_market_movers(root=root, **params).to_dict()
        if data.get("errors") and not data.get("rows"):
            raise ValueError("；".join(data["errors"]))
        return TaskResult(data, "市場搜尋已完成", data.get("errors") or [])

    raise ValueError(f"不支援的任務：{kind}")
