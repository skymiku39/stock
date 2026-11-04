"""Daily news and watched-price collection, independent of model research."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math

from bot.news_fetcher import fetch_today_news, load_cached_news_only
from bot.source_registry import SourceRegistry, resolve_source_document_path
from bot.stock_db import StockDB
from bot.technicals import fetch_recent_kline, load_kline_from_db
from bot.utils import now_tw
from bot.verified_research_batch import write_json


def collection_period(now):
    # A midnight success must not suppress the day's market/news updates.
    if now.hour < 9:
        return "before_open"
    return "intraday" if (now.hour, now.minute) < (14, 30) else "after_close"


def verify_source(root, document_id):
    with SourceRegistry(root) as registry:
        source = registry.db.get_source_document(document_id)
        if source is None or source.quality_status not in {"ok", "partial"}:
            raise ValueError("source document is missing or unusable")
        raw = resolve_source_document_path(root, source).read_bytes()
        if hashlib.sha256(raw).hexdigest() != source.content_hash:
            raise ValueError("source archive hash mismatch")
        return source


def collect_news(root, date):
    items = fetch_today_news(root=root, limit=200, force_refresh=True)
    cached = load_cached_news_only(root=root, date=dt.date.fromisoformat(date), limit=1000)
    if not items or items != cached:
        raise ValueError("news fetch is empty or cache readback differs")
    ids = sorted({i.source_document_id for i in items})
    if not ids or not all(ids):
        raise ValueError("news source registration is missing")
    for sid in ids:
        verify_source(root, sid)
    today = {i.url or i.news_id: i for i in cached if i.published_at.startswith(date)}
    return {"status": "ok", "collected_at": now_tw().isoformat(),
            "cached_count": len(cached), "today_count": len(today), "source_ids": ids,
            "latest_published_at": max((i.published_at for i in cached), default=""),
            "headlines": [{"title": i.title, "url": i.url, "published_at": i.published_at}
                          for i in today.values()]}


def collect_price(root, ticker):
    started = now_tw().replace(microsecond=0)
    fetched = fetch_recent_kline(ticker, months=1, root=root, incremental=True)
    cached = load_kline_from_db(ticker, months=1, root=root)
    if fetched.empty or cached.empty:
        raise ValueError("price fetch or database readback is empty")
    last = fetched.iloc[-1]
    replay = cached.iloc[-1]
    if str(last["date"]) != str(replay["date"]) or float(last["close"]) != float(replay["close"]):
        raise ValueError("price cache and database disagree")
    for field in ("open", "high", "low", "close", "volume"):
        try:
            expected, actual = float(last[field]), float(replay[field])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"price cache and database disagree: missing or invalid {field}") from exc
        if not math.isfinite(expected) or not math.isfinite(actual) or expected != actual:
            raise ValueError(f"price cache and database disagree: {field}")
    db = StockDB.open(root=root)
    try:
        bar = db.get_price_history(ticker, limit=1, ascending=False)[0]
    finally:
        db.close()
    # fetch_recent_kline can return old cached rows after a network failure.
    # Such a fallback is readable, but is not a successful fresh collection.
    if not bar.retrieved_at or dt.datetime.fromisoformat(bar.retrieved_at) < started:
        raise ValueError("price fetch fell back to an older cached response")
    source = verify_source(root, bar.source_record_id)
    return {"status": "ok", "collected_at": now_tw().isoformat(), "ticker": ticker,
            "source_date": bar.date, "close": bar.close, "source_url": source.url,
            "source_document_id": source.document_id, "retrieved_at": bar.retrieved_at}


def collect_daily_inputs(root, settings):
    from bot.watchlist import load
    now = now_tw()
    date, period = now.date().isoformat(), collection_period(now)
    path = root / "data/daily_research" / date / "daily-inputs.json"
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    reuse = previous.get("date") == date and previous.get("period") == period
    symbols = sorted(set(settings.symbols) | {i.ticker for i in load(root).items})
    result = {"date": date, "period": period, "attempted_at": now.isoformat(),
              "complete": False, "news": {}, "prices": {}, "errors": []}
    jobs = [("news", lambda: collect_news(root, date))]
    jobs += [(ticker, lambda ticker=ticker: collect_price(root, ticker)) for ticker in symbols]
    for key, collect in jobs:
        old = previous.get("news", {}) if key == "news" else previous.get("prices", {}).get(key, {})
        # Successful steps are retained; only unfinished steps retry in this period.
        try:
            age = (now - dt.datetime.fromisoformat(old.get("collected_at", ""))).total_seconds()
        except (ValueError, TypeError):
            age = float("inf")
        ttl = 300 if key == "news" else 1800
        if reuse and old.get("status") == "ok" and 0 <= age < ttl:
            value = old
        else:
            try:
                value = collect()
                value.setdefault("collected_at", now_tw().isoformat())
            except Exception as exc:
                value = {"status": "failed", "error": str(exc), "attempted_at": now_tw().isoformat()}
        if key == "news":
            result["news"] = value
        else:
            result["prices"][key] = value
        if value.get("status") != "ok":
            result["errors"].append(f"{key}: {value.get('error', 'collection incomplete')}")
        write_json(path, result)
    result["complete"] = not result["errors"] and now_tw().date().isoformat() == date
    result["finished_at"] = now_tw().isoformat()
    write_json(path, result)
    return result


def write_daily_summary(root, state):
    """A readable report also exists when the LLM or full research is partial."""
    date = state["date"]
    inputs = state.get("daily_inputs", {})
    news = inputs.get("news", {})
    labels = {"before_open": "盤前", "intraday": "盤中", "after_close": "收盤後擷取"}
    lines = [f"{date} 每日資料整理", f"更新時間：{now_tw().isoformat()}",
             f"擷取時段：{labels.get(inputs.get('period'), '未確認')}；行情日期依原始來源標示。",
             f"整體狀態：{state.get('phase', 'collecting')}；完整：{state.get('complete', False)}",
             f"資料摘要：{state.get('completed', 0)} / {state.get('requested', 0)}；"
             f"排除 {len(state.get('excluded', []))}，待完成 {len(state.get('pending', []))}。",
             f"研究條件齊備：{state.get('research_completed', 0)} / {state.get('requested', 0)}；"
             f"不同資料組：{state.get('unique_input_sets', 0)}。", "",
             f"新聞：{news.get('status', 'pending')}；本次快取 {news.get('cached_count', 0)} 則，"
             f"今天發布 {news.get('today_count', 0)} 則。"]
    for item in news.get("headlines", []):
        lines += [f"- {item['published_at']} {item['title']}", f"  {item['url']}"]
    lines += ["", "主要追蹤行情（日 K；來源日期不等於擷取日期）"]
    for ticker, price in inputs.get("prices", {}).items():
        if price.get("status") == "ok":
            lines += [f"- {ticker}：來源日 {price['source_date']}，收盤 {price['close']}；"
                      f"擷取 {price['retrieved_at']}。", f"  {price['source_url']}"]
        else:
            lines.append(f"- {ticker}：未完成，{price.get('error', 'unknown')}")
    gaps = (inputs.get("errors", []) + state.get("collection_errors", [])
            + state.get("collection_warnings", []) + state.get("official_errors", []))
    if state.get("error"):
        gaps.append(state["error"])
    if gaps:
        lines += ["", "缺口與限制"] + [f"- {gap}" for gap in gaps]
    path = root / "data/daily_research" / date / "summary.txt"
    temporary = path.with_suffix(".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path
