"""微笑情緒分數每日紀錄與彙總：只記錄，不影響任何交易行為。

用法：
  python -m bot.smile_mood_log --root <專案根目錄>             # 算出當下分數並追加到 data/smile_mood/daily.jsonl
  python -m bot.smile_mood_log --root <專案根目錄> --summary   # 印出累積分布，供日後決定門檻

判定一律呼叫 bot.smile_sentiment 的 evaluate_mood（與策略同一份邏輯）。
同一天同一時段（morning＝中午前、midday＝中午後）重跑會覆蓋，不重複。
新聞只讀本機 RSS 存檔；0050 漲跌幅唯讀查 TWSE 公開報價。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Callable, Optional

from bot.smile_sentiment import REQUIRE_MODES, evaluate_mood, load_recent_items, market_news_mood
from bot.utils import configure_utf8_stdio, get_logger, now_tw

LOG_RELATIVE = Path("data") / "smile_mood" / "daily.jsonl"
REQUIRE_LOGGED = ("both", "news", "price")
MARKET_SYMBOL = "0050"


def log_path(root) -> Path:
    return Path(root) / LOG_RELATIVE


def slot_for(now: dt.datetime) -> str:
    return "morning" if now.hour < 12 else "midday"


def fetch_market_change_pct(logger=None) -> Optional[float]:
    """唯讀查 0050 即時漲跌幅（%）；失敗或沒有報價回 None。"""
    try:
        from bot.market_source import TwsePublicMarketSource
        quote = TwsePublicMarketSource([MARKET_SYMBOL], logger=logger).get_quotes().get(MARKET_SYMBOL)
    except Exception as exc:  # 行情只是紀錄用，失敗不可讓整個工作失敗
        if logger:
            logger.warning("查 %s 報價失敗：%s: %s", MARKET_SYMBOL, type(exc).__name__, exc)
        return None
    if not quote or quote.get("pct_chg") is None:
        return None
    return float(quote["pct_chg"])


def compute_row(root, settings, now: dt.datetime, market_change_pct: Optional[float]) -> dict:
    items, collected = load_recent_items(root, now, settings.smile_news_window_hours)
    news = market_news_mood(items)
    age = None
    if collected:
        try:
            ts = dt.datetime.fromisoformat(collected)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=now.tzinfo)
            age = max(0.0, (now - ts).total_seconds() / 60)
        except ValueError:
            age = None
    verdicts = {}
    base = None
    for mode in REQUIRE_LOGGED:
        mood = evaluate_mood(news=news, news_age_minutes=age, market_change_pct=market_change_pct,
                             settings=settings.model_copy(update={"smile_mood_require": mode}))
        verdicts[mode] = mood.pessimistic
        base = base or mood
    fresh = base.news_pessimistic is not None
    thresholds = {
        "news_min_negative": settings.smile_mood_news_min_negative,
        "news_score": settings.smile_mood_news_score,
        "market_drop_pct": settings.smile_mood_market_drop_pct,
        "news_max_age_minutes": settings.smile_news_max_age_minutes,
        "news_window_hours": settings.smile_news_window_hours,
        "require_current": settings.smile_mood_require,
    }
    return {
        "date": now.date().isoformat(),
        "slot": slot_for(now),
        "computed_at": now.isoformat(timespec="seconds"),
        "news_items": len(items),
        "news_fresh": fresh,
        "news_age_minutes": None if age is None else round(age, 1),
        "news_negative": base.news_negative if fresh else None,
        "news_positive": base.news_positive if fresh else None,
        "news_score": round(base.news_score, 4) if fresh else None,
        "market_symbol": MARKET_SYMBOL,
        "market_change_pct": market_change_pct,
        "news_negative_reached": (base.news_negative >= settings.smile_mood_news_min_negative
                                  if fresh else None),
        "news_score_reached": (base.news_score >= settings.smile_mood_news_score
                               if fresh else None),
        "price_drop_reached": base.price_pessimistic,
        "news_pessimistic": base.news_pessimistic,
        "price_pessimistic": base.price_pessimistic,
        "pessimistic": verdicts,
        "thresholds": thresholds,
    }


def read_rows(path: Path) -> list:
    rows = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def write_row(root, row: dict) -> Path:
    """同日同時段覆蓋（先濾掉舊的再追加），其餘行原樣保留。"""
    path = log_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    kept = [r for r in read_rows(path)
            if (r.get("date"), r.get("slot")) != (row["date"], row["slot"])]
    kept.append(row)
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False, allow_nan=False) + "\n" for r in kept),
                   encoding="utf-8")
    tmp.replace(path)
    return path


def run_log(root, settings, *, now: Optional[dt.datetime] = None,
            fetch_change: Callable[[], Optional[float]] = fetch_market_change_pct,
            write: bool = True) -> dict:
    now = now or now_tw()
    row = compute_row(root, settings, now, fetch_change())
    if write:
        write_row(root, row)
    return row


def percentiles(values: list) -> Optional[dict]:
    vals = sorted(values)
    if not vals:
        return None

    def q(p: float) -> float:
        pos = (len(vals) - 1) * p
        lo = int(pos)
        hi = min(lo + 1, len(vals) - 1)
        return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)

    return {"min": vals[0], "p25": q(0.25), "median": q(0.5), "p75": q(0.75), "max": vals[-1]}


def summarize(rows: list, settings) -> dict:
    """用「現行門檻」對累積的原始數值重算會觸發幾個樣本（日期＋時段）與幾個不同日期。"""
    out = {"days": len({r.get("date") for r in rows}), "samples": len(rows), "metrics": {}, "triggers": {}}
    for key in ("news_negative", "news_score", "market_change_pct"):
        out["metrics"][key] = percentiles(
            [r[key] for r in rows if isinstance(r.get(key), (int, float))])
    hits = {m: [] for m in REQUIRE_LOGGED}
    for r in rows:
        neg, score, chg = r.get("news_negative"), r.get("news_score"), r.get("market_change_pct")
        news_p = (None if neg is None or score is None else
                  neg >= settings.smile_mood_news_min_negative
                  and score >= settings.smile_mood_news_score)
        price_p = None if chg is None else chg <= -settings.smile_mood_market_drop_pct
        verdict = {"both": news_p is True and price_p is True, "news": news_p is True,
                   "price": price_p is True}
        for mode, hit in verdict.items():
            if hit:
                hits[mode].append(r.get("date"))
    for mode, dates in hits.items():
        out["triggers"][mode] = {"samples": len(dates), "days": len(set(dates))}
    out["current_thresholds"] = {
        "news_min_negative": settings.smile_mood_news_min_negative,
        "news_score": settings.smile_mood_news_score,
        "market_drop_pct": settings.smile_mood_market_drop_pct}
    return out


def format_summary(s: dict) -> str:
    lines = [f"已累積 {s['days']} 天、{s['samples']} 筆樣本"]
    names = {"news_negative": "新聞負面則數", "news_score": "新聞分數",
             "market_change_pct": "0050 漲跌幅%"}
    for key, label in names.items():
        p = s["metrics"].get(key)
        if p is None:
            lines.append(f"{label}：沒有資料")
        else:
            lines.append(f"{label}：最小 {p['min']:.2f}、25% {p['p25']:.2f}、中位 {p['median']:.2f}、"
                         f"75% {p['p75']:.2f}、最大 {p['max']:.2f}")
    t = s["current_thresholds"]
    lines.append(f"現行門檻：負面 >= {t['news_min_negative']} 則、分數 >= {t['news_score']}、"
                 f"0050 跌幅 >= {t['market_drop_pct']}%")
    for mode in REQUIRE_LOGGED:
        c = s["triggers"][mode]
        lines.append(f"REQUIRE={mode}：會觸發 {c['days']} 天（{c['samples']} 筆）")
    return "\n".join(lines)


def main(argv=None) -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="微笑情緒分數每日紀錄與彙總（只記錄，不交易）")
    parser.add_argument("--root", default=".", help="專案根目錄（RSS 在 data/news，輸出在 data/smile_mood）")
    parser.add_argument("--summary", action="store_true", help="印出累積分布與現行門檻的觸發天數")
    parser.add_argument("--no-write", action="store_true", help="只計算並印出，不寫檔")
    args = parser.parse_args(argv)

    from bot.config import Settings
    settings = Settings()
    if args.summary:
        print(format_summary(summarize(read_rows(log_path(args.root)), settings)))
        return 0
    logger = get_logger("smile-mood-log")
    row = run_log(args.root, settings, fetch_change=lambda: fetch_market_change_pct(logger),
                  write=not args.no_write)
    print(json.dumps(row, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
