"""Daily news and watched-price collection, independent of model research."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
import time
from pathlib import Path

from bot.news_fetcher import fetch_today_news, load_cached_news_only
from bot.rss_archive import collect_rss_archive
from bot.price_crosscheck import describe as describe_crosscheck, run as crosscheck_prices
from bot.source_registry import SourceRegistry, resolve_source_document_path
from bot.stock_db import StockDB
from bot.technicals import fetch_recent_kline, load_kline_from_db
from bot.utils import now_tw
from bot.verified_research_batch import write_json
from bot.runtime_state import FileLock, StateBusy, atomic_json, atomic_text, read_json


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
    items = fetch_today_news(root=root, limit=200, force_refresh=True, cumulative=True)
    cached = load_cached_news_only(root=root, date=dt.date.fromisoformat(date), limit=5000)
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


def collect_price(root, ticker, months=1):
    started = now_tw().replace(microsecond=0)
    fetched = fetch_recent_kline(ticker, months=months, root=root, incremental=True)
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

# 擴充名單：追蹤的 ETF 本身、各 ETF 最新一期的台股持股、回測名單。
# 海外持股（如 "NVDA US"）沒有 TWSE／TPEx 日 K 來源，不在此收。
_TW_SYMBOL = re.compile(r"^\d{4,6}[A-Z]?$")
EXTENDED_BUDGET_SECONDS = 240      # 每次執行最多花在擴充名單的時間；沒抓完下次接續
EXTENDED_DELAY_SECONDS = 2.0       # 每檔之間的間隔，避免 TWSE 擋連線
EXTENDED_MAX_ATTEMPTS = 3          # 同一檔同一個目標日最多嘗試幾次
EXTENDED_BEHIND_RETRY_SECONDS = 3600  # 來源日仍落後的標的，距上次抓取多久後可再抓一次
RSS_REFRESH_SECONDS = 1800         # RSS 存檔全部來源輪過一遍後，隔多久再抓
EXTENDED_RETIRED_DAYS = 90         # 最新日 K 比目標日舊這麼多天的標的視為已下市或長期停牌
EXTENDED_MAX_MONTHS = 6            # 落後太久時一次最多補幾個月；更早的交給 history_fetch


def extended_price_symbols(root):
    """回傳擴充名單的台股代號；任何一個來源讀不到都不影響其他來源。"""
    from bot.active_etf import load_tracked_etfs, list_holdings_dates, load_holdings
    symbols = set()
    try:
        etfs = list(load_tracked_etfs(root))
    except Exception:
        etfs = []
    for etf in etfs:
        symbols.add(str(etf.symbol).strip())
        # 單一 ETF 的持股讀不到只略過那一檔，否則一檔壞資料會讓所有 ETF 的持股都消失。
        try:
            dates = list_holdings_dates(etf.symbol, root)
            snapshot = load_holdings(etf.symbol, dates[0], root) if dates else None
            if snapshot:
                symbols.update(str(h.ticker).strip() for h in snapshot.holdings)
        except Exception:
            continue
    try:
        universe = json.loads((Path(root) / "config/backtest_universe.json").read_text(encoding="utf-8"))
        symbols.update(str(row.get("symbol", "")).strip() for row in universe.get("symbols", []))
    except (OSError, ValueError, AttributeError):
        pass
    return sorted(s for s in symbols if _TW_SYMBOL.match(s))


def latest_price_dates(root):
    db = StockDB.open(root=root)
    try:
        return {row[0]: row[1] for row in db.conn.execute(
            "SELECT symbol, MAX(date) FROM price_history GROUP BY symbol")}
    finally:
        db.close()


def _days_between(earlier, later):
    try:
        return (dt.date.fromisoformat(later[:10]) - dt.date.fromisoformat(earlier[:10])).days
    except ValueError:
        return 0


def _months_to_fetch(latest, today):
    try:
        last = dt.date.fromisoformat(latest)
    except (TypeError, ValueError):
        return 1
    return max(1, min(EXTENDED_MAX_MONTHS, (today.year - last.year) * 12 + today.month - last.month + 1))


def collect_extended_prices(root, symbols, target, previous, *, budget=None, delay=None):
    """把擴充名單的日 K 補到 target（主要追蹤標的最新的來源日）。

    已到 target 的不連線；落後的逐檔抓，受時間預算限制，沒抓完留到下次。
    抓過但來源日仍早於 target 的（停牌、下市、或來源當天還沒更新）記在 behind；
    距上次抓取超過 EXTENDED_BEHIND_RETRY_SECONDS 才會再抓，且仍受 EXTENDED_MAX_ATTEMPTS 限制。
    """
    budget = EXTENDED_BUDGET_SECONDS if budget is None else budget
    delay = EXTENDED_DELAY_SECONDS if delay is None else delay
    same = previous.get("target_date") == target
    checked = dict(previous.get("checked", {})) if same else {}
    attempts = dict(previous.get("attempts", {})) if same else {}
    failed = dict(previous.get("failed", {})) if same else {}
    checked_at = dict(previous.get("checked_at", {})) if same else {}
    out = {"target_date": target, "requested": len(symbols), "current": 0, "fetched": [],
           "pending": [], "behind": {}, "retired": {}, "failed": failed, "checked": checked,
           "checked_at": checked_at, "attempts": attempts, "complete": False}
    if not target:
        out["pending"] = list(symbols)
        out["note"] = "主要追蹤標的沒有成功的行情，無法判定目標日"
        return out
    latest = latest_price_dates(root)
    today = now_tw().date()
    started = time.monotonic()
    for ticker in symbols:
        have = str(latest.get(ticker) or "")
        if have >= target:
            out["current"] += 1
            failed.pop(ticker, None)
            continue
        if ticker in checked:
            # 來源可能比主要標的晚更新當天日 K；隔一段時間可再抓，但嘗試次數用完就維持 behind。
            try:
                idle = (now_tw() - dt.datetime.fromisoformat(checked_at[ticker])).total_seconds()
            except (KeyError, ValueError, TypeError):
                idle = float("inf")   # 舊狀態沒有時間戳：視為可重抓
            if idle < EXTENDED_BEHIND_RETRY_SECONDS or attempts.get(ticker, 0) >= EXTENDED_MAX_ATTEMPTS:
                out["behind"][ticker] = checked[ticker]
                continue
        if have and _days_between(have, target) > EXTENDED_RETIRED_DAYS:
            # 已經很久沒有日 K（下市、長期停牌）：不每天去問，也不算失敗。恢復交易時用 history_fetch 補。
            out["retired"][ticker] = have
            failed.pop(ticker, None)
            continue
        if attempts.get(ticker, 0) >= EXTENDED_MAX_ATTEMPTS:
            continue
        if time.monotonic() - started >= budget:
            out["pending"].append(ticker)
            continue
        attempts[ticker] = attempts.get(ticker, 0) + 1
        try:
            price = collect_price(root, ticker, _months_to_fetch(have, today))
            failed.pop(ticker, None)
            checked[ticker] = price["source_date"]
            checked_at[ticker] = now_tw().isoformat()
            out["fetched"].append(ticker)
            if price["source_date"] >= target:
                out["current"] += 1
            else:
                out["behind"][ticker] = price["source_date"]
        except Exception as exc:
            failed[ticker] = str(exc)
            if attempts[ticker] < EXTENDED_MAX_ATTEMPTS:
                out["pending"].append(ticker)
        if delay > 0:
            time.sleep(delay)
    out["complete"] = not out["pending"] and not failed
    return out


def collect_ex_rights(root):
    """更新今年的除權息事件（只存回測名單內的標的，與 `bot.ex_rights` 的預設一致）。

    為什麼每天做：回測靠這張表調整除權息跳空；只在回補時抓一次的話，新的除權息日會被當成下跌。
    """
    from bot import ex_rights
    from bot.backtest_universe import load_universe
    symbols = set(load_universe(Path(root) / "config/backtest_universe.json"))
    year = now_tw().year
    rows = [r for r in ex_rights.fetch_year(year) if r["symbol"] in symbols]
    db = StockDB.open(root=root)
    try:
        saved = ex_rights.save_events(db, rows)
        stored, latest = db.conn.execute(
            "SELECT COUNT(*), MAX(ex_date) FROM ex_rights_events WHERE ex_date >= ?", (f"{year}-01-01",)).fetchone()
    finally:
        db.close()
    if stored < saved:
        raise ValueError("ex-rights readback has fewer rows than were saved")
    return {"status": "ok", "collected_at": now_tw().isoformat(), "year": year, "saved": saved,
            "stored_this_year": stored, "latest_ex_date": latest}


def collect_daily_inputs(root, settings, *, force=False):
    with FileLock(Path(root) / 'data/.daily-inputs.lock'):
        return _collect_daily_inputs(root, settings, force=force)


def _collect_daily_inputs(root, settings, *, force=False):
    from bot.watchlist import load
    now = now_tw()
    date, period = now.date().isoformat(), collection_period(now)
    path = root / "data/daily_research" / date / "daily-inputs.json"
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    reuse = not force and previous.get("date") == date and previous.get("period") == period
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
        atomic_json(path.with_name('daily-inputs-progress.json'), result)
    # 擴充名單另外記錄，不影響 complete：單一檔抓不到不該讓每日研究永遠不完整。
    target = max((p.get("source_date", "") for p in result["prices"].values()
                  if p.get("status") == "ok"), default="")
    extended = []
    try:
        extended = [t for t in extended_price_symbols(root) if t not in symbols]
        result["extended"] = collect_extended_prices(
            root, extended, target, previous.get("extended", {}) if previous.get("date") == date else {})
    except Exception as exc:
        result["extended"] = {"complete": False, "error": str(exc)}
    # 用全市場單日行情核對目標日的日 K（另一個端點）；原始回應有快取，一天只連線一次。
    if target:
        try:
            result["crosscheck"] = crosscheck_prices(root, target, expected=sorted(set(symbols) | set(extended)))
        except Exception as exc:
            result["crosscheck"] = {"status": "pending", "date": target, "error": str(exc)}
    # RSS 存檔同樣另外記錄：九十幾個外部來源總有幾個暫時失敗，不該擋住 complete。
    old_rss = previous.get("rss", {}) if previous.get("date") == date else {}
    try:
        rss_age = (now - dt.datetime.fromisoformat(old_rss.get("collected_at", ""))).total_seconds()
    except (ValueError, TypeError):
        rss_age = float("inf")
    if (not force and old_rss.get("status") == "ok" and 0 <= rss_age < RSS_REFRESH_SECONDS
            and old_rss.get("feeds_seen", 0) >= old_rss.get("feeds_total", 1)):
        result["rss"] = old_rss
    else:
        try:
            result["rss"] = collect_rss_archive(root, tracked=sorted(set(symbols) | set(extended)))
        except Exception as exc:
            result["rss"] = {"status": "failed", "error": str(exc), "collected_at": now_tw().isoformat()}
    # 除權息事件一天更新一次；失敗下一輪再試，不影響 complete。
    old_ex = previous.get("ex_rights", {}) if previous.get("date") == date else {}
    if not force and old_ex.get("status") == "ok":
        result["ex_rights"] = old_ex
    else:
        try:
            result["ex_rights"] = collect_ex_rights(root)
        except Exception as exc:
            result["ex_rights"] = {"status": "failed", "error": str(exc), "collected_at": now_tw().isoformat()}
    result["complete"] = not result["errors"] and now_tw().date().isoformat() == date
    result["finished_at"] = now_tw().isoformat()
    atomic_json(path, result)
    return result


def write_daily_summary(root, state):
    """A readable report also exists when the LLM or full research is partial."""
    path = Path(root) / 'data/daily_research' / state['date'] / 'summary.txt'
    lock = FileLock(Path(root) / 'data/.daily-summary.lock')
    if not lock.acquire():
        return path
    try:
        latest = read_json(path.with_name('status.json'))
        if latest.get('date') == state['date'] and latest.get('attempted_at', '') >= state.get('attempted_at', ''):
            state = latest
        inputs = read_json(path.with_name('daily-inputs.json'))
        if inputs.get('date') == state['date'] and inputs.get('finished_at'):
            state = {**state, 'daily_inputs': inputs}
        return _write_daily_summary(root, state)
    finally:
        lock.release()


def _write_daily_summary(root, state):
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
    rss = inputs.get("rss") or {}
    if rss:
        lines += ["", "RSS 存檔（不需模型）",
                  f"- {rss.get('status', 'pending')}：來源 {rss.get('feeds_ok', 0)} / {rss.get('feeds_total', 0)} 正常，"
                  f"累積 {rss.get('total_items', 0)} 則，其中今天發布 {rss.get('today_items', 0)} 則，"
                  f"提到追蹤標的 {rss.get('tagged_items', 0)} 則。"]
        if rss.get("top_tickers"):
            lines.append("- 提到最多的標的：" + "、".join(f"{t}（{n}）" for t, n in rss["top_tickers"]))
        if rss.get("feeds_failed"):
            lines.append("- 失敗來源：" + "、".join(rss["feeds_failed"]))
        if rss.get("error"):
            lines.append(f"- {rss['error']}")
    xc = inputs.get("crosscheck") or {}
    if xc:
        lines += ["", "日 K 交叉核對（與交易所全市場日報比對）：" + (
            describe_crosscheck(xc) if "matched" in xc else f"{xc.get('date')} 尚未完成，{xc.get('error', 'unknown')}")]
    exr = inputs.get("ex_rights") or {}
    if exr:
        lines += ["", "除權息事件：" + (f"今年 {exr.get('stored_this_year', 0)} 筆，最後一筆 {exr.get('latest_ex_date')}"
                                    if exr.get("status") == "ok" else f"未完成，{exr.get('error', 'unknown')}")]
    forward = read_json(Path(root) / "data/forward_tracking/latest.json")
    if forward.get("strategies"):
        lines += ["", f"前瞻追蹤（凍結參數，{forward.get('forward_start')}～{forward.get('as_of')}，"
                      f"{forward.get('trading_days')} 個交易日；樣本少時不能當證據）"]
        for key, label in (("smile", "微笑方案"), ("swing", "波段原型")):
            item = forward["strategies"].get(key)
            if item:
                m = item["metrics"]
                lines.append(f"- {label}：權益 {item['equity']:,.0f}（{m['return_pct']:.2f}%），已平倉 {m['round_count']} 輪，"
                             f"未平倉 {len(item['open_positions'])} 檔，最大回撤 {m['max_drawdown_pct']:.2f}%")
    ext = inputs.get("extended") or {}
    if ext:
        lines += ["", "擴充名單行情（ETF、ETF 台股持股、回測名單）",
                  f"- 目標日 {ext.get('target_date') or '未確認'}：{ext.get('current', 0)} / {ext.get('requested', 0)} 檔已到目標日；"
                  f"待抓 {len(ext.get('pending', []))}、失敗 {len(ext.get('failed', {}))}、"
                  f"來源日較舊 {len(ext.get('behind', {}))}、已無日 K {len(ext.get('retired', {}))}。"]
        for label, key in (("失敗", "failed"), ("來源日較舊", "behind"), ("已無日 K（下市或長期停牌）", "retired")):
            if ext.get(key):
                lines.append(f"- {label}：" + "、".join(f"{t}（{v}）" for t, v in sorted(ext[key].items())))
        if ext.get("error") or ext.get("note"):
            lines.append(f"- {ext.get('error') or ext.get('note')}")
    freshness = read_json(Path(root) / "data/freshness_status.json")
    if freshness.get("checks"):
        from bot.data_freshness import format_report
        lines += [""] + format_report(freshness)
    gaps = (inputs.get("errors", []) + state.get("collection_errors", [])
            + state.get("collection_warnings", []) + state.get("official_errors", []))
    if state.get("error"):
        gaps.append(state["error"])
    if gaps:
        lines += ["", "缺口與限制"] + [f"- {gap}" for gap in gaps]
    path = root / "data/daily_research" / date / "summary.txt"
    atomic_text(path, "\n".join(lines) + "\n")
    return path


COLLECTION_FALLBACK_SECONDS = 90 * 60     # 比專屬的 collection 工作（60 分鐘）長，所以它在的時候輪不到這裡


def collection_fallback_due(root, now=None):
    """排程器還沒有 collection 工作時，由 daily_inputs 代為啟動「只收集」。回傳 (要不要跑, 原因)。

    為什麼需要：排程器是常駐程序，重啟前不會出現新增的工作；ETF 持股、籌碼與官方財務
    不該因此停更。專屬工作出現後，它每 60 分鐘就會更新 status.json，這裡自然不再觸發。
    """
    now = now or now_tw()
    status = read_json(Path(root) / 'data/daily_research' / now.date().isoformat() / 'status.json')
    if status.get('complete') or (status.get('collection_complete') and status.get('official_complete')):
        return False, 'complete'
    job = (read_json(Path(root) / 'data/scheduler_status.json').get('jobs') or {}).get('collection') or {}
    for stamp, reason in ((job.get('started_at'), 'scheduled'), (status.get('attempted_at'), 'recent')):
        try:
            if (now - dt.datetime.fromisoformat(stamp)).total_seconds() < COLLECTION_FALLBACK_SECONDS:
                return False, reason
        except (TypeError, ValueError):
            continue
    return True, 'due'


def main(argv=None):
    import argparse
    from bot.config import Settings
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--force-refresh', action='store_true')
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        settings = Settings(_env_file=str(root / '.env'))
        result = collect_daily_inputs(root, settings, force=args.force_refresh)
        try:
            # 工作成功不代表資料有更新；每輪都把各類資料的最新日期寫下來，摘要會引用。
            from bot.data_freshness import check_data_freshness
            atomic_json(root / 'data/freshness_status.json', check_data_freshness(root, settings=settings))
        except Exception:
            pass
        try:
            # 凍結參數的前瞻追蹤：資料日有前進且名單涵蓋率足夠時才重算。
            from bot.forward_tracker import update as update_forward
            update_forward(root)
        except Exception:
            pass
        try:
            # ETF 關注名單：只讀已存的持股快照，重算很快，每輪都更新。
            from bot.etf_focus import update as update_etf_focus
            update_etf_focus(root)
        except Exception:
            pass
        state = read_json(root / 'data/daily_research' / result['date'] / 'status.json')
        state = state or {'date': result['date'], 'phase': 'awaiting_research', 'complete': False}
        write_daily_summary(root, {**state, 'daily_inputs': result})
        print(json.dumps(result, ensure_ascii=False))
        try:
            due, reason = collection_fallback_due(root)
            if due:
                from bot.daily_research import main as collect_only
                print(f'collection fallback: exit={collect_only(["--root", str(root), "--collect-only"])}', flush=True)
        except Exception as exc:
            print(f'collection fallback failed: {exc}', flush=True)
        return 0 if result['complete'] else 2
    except StateBusy:
        return 4


if __name__ == '__main__':
    raise SystemExit(main())
