"""微笑方案盤中「時事＋即時股價」判斷：純函式，不連網、不呼叫 LLM。

只讀本機 RSS 存檔（data/news/rss_<date>.json）的標題做關鍵字比對。
關鍵字與門檻都是暫定值，未經驗證；歷史新聞不存在，無法回測。
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from bot.rss_archive import archive_path
from bot.web_search import OFFICIAL_STANCE

# 市場悲觀詞（避免單獨的「跌」「虧」這類太泛的詞）
PESSIMISTIC_KEYWORDS_ZH = (
    "重挫", "暴跌", "崩跌", "大跌", "跌停", "股災", "恐慌", "殺盤", "賣壓", "熔斷",
    "衰退", "避險", "狂瀉", "崩盤", "急殺", "慘跌", "血洗", "潰堤", "空頭", "破底",
)
PESSIMISTIC_KEYWORDS_EN = (
    "plunge", "plunges", "plunged", "plummet", "plummets", "sell-off", "selloff", "tumble",
    "tumbles", "slump", "slumps", "stock market crash", "market rout", "panic selling",
    "recession fears", "bloodbath", "meltdown",
)
# 市場樂觀詞
OPTIMISTIC_KEYWORDS_ZH = (
    "大漲", "飆漲", "創高", "新高", "反彈", "漲停", "強彈", "噴出", "狂飆", "大舉反攻",
    "多頭", "勁揚", "攻頂",
)
OPTIMISTIC_KEYWORDS_EN = (
    "rally", "rallies", "surge", "surges", "record high", "rebound", "rebounds", "soar",
    "soars", "all-time high", "bull run",
)
# 公司層級壞消息詞
BAD_NEWS_KEYWORDS_ZH = (
    "掏空", "背信", "內線交易", "財報不實", "下市", "停止交易", "全額交割", "跳票",
    "違約交割", "遭搜索", "遭約談", "起訴", "重大虧損", "裁罰", "停工", "假帳",
    "假交易", "掛牌異常", "重大訊息澄清", "遭檢調", "聲請重整", "破產",
)
BAD_NEWS_KEYWORDS_EN = (
    "fraud", "delisting", "delisted", "defaulted", "indicted", "bankruptcy",
    "accounting scandal", "insider trading", "trading halted", "raided",
)



def _matcher(zh, en):
    """中文用子字串；英文用字詞邊界（避免 rally 命中 generally 這類誤判）。"""
    pattern = "|".join([re.escape(k) for k in zh]
                       + [rf"(?<![A-Za-z]){re.escape(k)}(?![A-Za-z])" for k in en])
    return re.compile(pattern, re.IGNORECASE)


PESSIMISTIC_KEYWORDS = _matcher(PESSIMISTIC_KEYWORDS_ZH, PESSIMISTIC_KEYWORDS_EN)
OPTIMISTIC_KEYWORDS = _matcher(OPTIMISTIC_KEYWORDS_ZH, OPTIMISTIC_KEYWORDS_EN)
BAD_NEWS_KEYWORDS = _matcher(BAD_NEWS_KEYWORDS_ZH, BAD_NEWS_KEYWORDS_EN)

EVIDENCE_LIMIT = 5
REQUIRE_MODES = ("both", "any", "news", "price")


def _hit(text: str, keywords) -> bool:
    return bool(keywords.search(text))


def _evidence(item: dict) -> dict:
    return {"title": item.get("title", ""), "url": item.get("url", ""),
            "feed": item.get("feed", ""), "published_at": item.get("published_at", "")}


def _parse_ts(value: Any, tz) -> Optional[dt.datetime]:
    try:
        ts = dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=tz)


def _read_day(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def load_recent_items(root, now: dt.datetime, window_hours: float):
    """讀今天與昨天的 RSS 存檔，回傳 (視窗內項目, 最新 collected_at)。缺檔或損毀回空，不丟例外。

    項目時間用 published_at，沒有就用 first_seen_at；官媒立場的項目排除。
    collected_at 為 ISO 字串，沒有任何可用存檔時為 None。
    """
    items: list = []
    collected: list = []
    tz = now.tzinfo
    cutoff = now - dt.timedelta(hours=window_hours)
    for offset in (0, 1):
        day = (now.date() - dt.timedelta(days=offset)).isoformat()
        data = _read_day(archive_path(Path(root), day))
        if data is None:
            continue
        if data.get("collected_at"):
            collected.append(str(data["collected_at"]))
        rows = data.get("items")
        if not isinstance(rows, list):
            continue
        for item in rows:
            if not isinstance(item, dict) or item.get("stance") == OFFICIAL_STANCE:
                continue
            ts = _parse_ts(item.get("published_at") or item.get("first_seen_at"), tz)
            if ts is None or ts < cutoff or ts > now + dt.timedelta(hours=1):
                continue
            items.append(item)
    latest = None
    parsed = [(_parse_ts(c, tz), c) for c in collected]
    parsed = [p for p in parsed if p[0] is not None]
    if parsed:
        latest = max(parsed, key=lambda p: p[0])[1]
    return items, latest


def market_news_mood(items) -> dict:
    """只算標題。同一則標題同時含悲觀與樂觀詞時兩邊都不算。"""
    negative = positive = 0
    evidence: list = []
    for item in items:
        title = str(item.get("title", ""))
        neg, pos = _hit(title, PESSIMISTIC_KEYWORDS), _hit(title, OPTIMISTIC_KEYWORDS)
        if neg and pos:
            continue
        if neg:
            negative += 1
            if len(evidence) < EVIDENCE_LIMIT:
                evidence.append(_evidence(item))
        elif pos:
            positive += 1
    return {"negative": negative, "positive": positive,
            "score": (negative - positive) / max(1, negative + positive),
            "evidence": evidence}


def symbol_bad_news(items, symbol: str) -> list:
    out: list = []
    for item in items:
        if symbol in (item.get("related_tickers") or []) and _hit(
                str(item.get("title", "")), BAD_NEWS_KEYWORDS):
            out.append(_evidence(item))
            if len(out) >= EVIDENCE_LIMIT:
                break
    return out


@dataclass
class MarketMood:
    pessimistic: bool = False
    news_pessimistic: Optional[bool] = None
    price_pessimistic: Optional[bool] = None
    news_negative: int = 0
    news_positive: int = 0
    news_score: float = 0.0
    market_change_pct: Optional[float] = None
    reasons: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    computed_at: str = ""


def evaluate_mood(*, news: Optional[dict], news_age_minutes: Optional[float],
                  market_change_pct: Optional[float], settings) -> MarketMood:
    mood = MarketMood(market_change_pct=market_change_pct,
                      computed_at=dt.datetime.now().isoformat(timespec="seconds"))
    fresh = (news is not None and news_age_minutes is not None
             and news_age_minutes <= settings.smile_news_max_age_minutes)
    if fresh:
        mood.news_negative = int(news.get("negative", 0))
        mood.news_positive = int(news.get("positive", 0))
        mood.news_score = float(news.get("score", 0.0))
        mood.evidence = list(news.get("evidence") or [])
        mood.news_pessimistic = (mood.news_negative >= settings.smile_mood_news_min_negative
                                 and mood.news_score >= settings.smile_mood_news_score)
        mood.reasons.append(
            f"新聞悲觀則數 {mood.news_negative}、樂觀 {mood.news_positive}、分數 {mood.news_score:.2f}"
            f"（{'符合' if mood.news_pessimistic else '未達'}門檻）")
    else:
        mood.reasons.append("新聞資料過期或沒有，無法判斷")
    if market_change_pct is not None:
        mood.price_pessimistic = market_change_pct <= -settings.smile_mood_market_drop_pct
        mood.reasons.append(
            f"市場即時漲跌 {market_change_pct:+.2f}%"
            f"（{'符合' if mood.price_pessimistic else '未達'}跌 {settings.smile_mood_market_drop_pct}% 門檻）")
    else:
        mood.reasons.append("沒有市場即時漲跌幅，無法判斷")
    n, p = mood.news_pessimistic, mood.price_pessimistic
    mode = settings.smile_mood_require
    if mode == "both":
        mood.pessimistic = n is True and p is True
    elif mode == "any":
        mood.pessimistic = n is True or p is True
    elif mode == "news":
        mood.pessimistic = n is True
    else:
        mood.pessimistic = p is True
    return mood
