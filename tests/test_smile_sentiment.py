"""微笑時事／即時情緒：純函式測試（不連網、不碰專案 data/）。"""
import datetime as dt
import json

import pytest

from bot.config import Settings
from bot.rss_archive import archive_path
from bot.smile_sentiment import (evaluate_mood, load_recent_items, market_news_mood,
                                 symbol_bad_news)

TZ = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime(2026, 11, 18, 10, 0, tzinfo=TZ)


def item(title, hours_ago=1.0, **kw):
    ts = (NOW - dt.timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    base = {"title": title, "url": f"http://x/{title}", "desc": "", "published_at": ts,
            "feed": "f", "stance": "", "first_seen_at": ts, "related_tickers": []}
    base.update(kw)
    return base


def write_day(root, date, items, collected_at="2026-11-18T09:50:00+08:00"):
    path = archive_path(root, date)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"date": date, "collected_at": collected_at, "items": items},
                               ensure_ascii=False), encoding="utf-8")


def settings(**kw):
    return Settings(_env_file=None, **kw)


def test_load_recent_items_window_and_collected_at(tmp_path):
    write_day(tmp_path, "2026-11-18", [item("今日新", 1), item("太舊", 30)],
              collected_at="2026-11-18T09:50:00+08:00")
    write_day(tmp_path, "2026-11-17", [item("昨日仍在視窗", 20)],
              collected_at="2026-11-17T23:00:00+08:00")
    items, collected = load_recent_items(tmp_path, NOW, 24)
    assert sorted(i["title"] for i in items) == ["今日新", "昨日仍在視窗"]
    assert collected == "2026-11-18T09:50:00+08:00"


def test_load_recent_items_falls_back_to_first_seen_at(tmp_path):
    row = item("無發布時間", 2, published_at="")
    write_day(tmp_path, "2026-11-18", [row])
    items, _ = load_recent_items(tmp_path, NOW, 24)
    assert [i["title"] for i in items] == ["無發布時間"]


def test_load_recent_items_missing_and_corrupt(tmp_path):
    assert load_recent_items(tmp_path, NOW, 24) == ([], None)
    path = archive_path(tmp_path, "2026-11-18")
    path.parent.mkdir(parents=True)
    path.write_text("{壞掉", encoding="utf-8")
    assert load_recent_items(tmp_path, NOW, 24) == ([], None)
    path.write_text("[1, 2]", encoding="utf-8")
    assert load_recent_items(tmp_path, NOW, 24) == ([], None)


def test_load_recent_items_excludes_official_stance(tmp_path):
    write_day(tmp_path, "2026-11-18", [item("官媒", 1, stance="official"), item("一般", 1)])
    items, _ = load_recent_items(tmp_path, NOW, 24)
    assert [i["title"] for i in items] == ["一般"]


def test_market_news_mood_counts_titles_only_and_skips_mixed():
    items = [item("台股重挫 恐慌蔓延"), item("Stocks plunge in sell-off"),
             item("台股大漲創高"), item("盤勢中性", ), item("重挫後強彈"),  # 同時含兩種詞，不計
             {**item("無關標題"), "desc": "暴跌 重挫"}]  # 內文有詞但標題沒有，不計
    for i in range(7):
        items.append(item(f"外資賣壓沉重 {i}"))
    mood = market_news_mood(items)
    assert mood["negative"] == 9 and mood["positive"] == 1
    assert mood["score"] == pytest.approx((9 - 1) / 10)
    assert len(mood["evidence"]) == 5
    assert set(mood["evidence"][0]) == {"title", "url", "feed", "published_at"}


def test_market_news_mood_empty():
    assert market_news_mood([]) == {"negative": 0, "positive": 0, "score": 0.0, "evidence": []}


def test_symbol_bad_news_requires_ticker_and_keyword():
    items = [item("某公司董座遭搜索", related_tickers=["2317"]),
             item("某公司遭搜索", related_tickers=["2330"]),
             item("某公司營收創高", related_tickers=["2317"]),
             item("CEO indicted for fraud", related_tickers=["2317"])]
    bad = symbol_bad_news(items, "2317")
    assert [b["title"] for b in bad] == ["某公司董座遭搜索", "CEO indicted for fraud"]
    assert symbol_bad_news(items, "9999") == []
    many = [item(f"掏空案 {i}", related_tickers=["2317"]) for i in range(9)]
    assert len(symbol_bad_news(many, "2317")) == 5


NEWS_BAD = {"negative": 12, "positive": 1, "score": 0.8, "evidence": [{"title": "t"}]}
NEWS_OK = {"negative": 2, "positive": 5, "score": -0.4, "evidence": []}


@pytest.mark.parametrize("require,news,age,chg,expected", [
    ("both", NEWS_BAD, 10, -2.0, True),
    ("both", NEWS_BAD, 10, -0.5, False),
    ("both", NEWS_BAD, 10, None, False),          # 股價 None 視為不悲觀
    ("both", NEWS_BAD, 200, -2.0, False),         # 新聞過期
    ("both", None, None, -2.0, False),
    ("any", NEWS_BAD, 10, -0.5, True),
    ("any", NEWS_OK, 10, -2.0, True),
    ("any", NEWS_OK, 10, -0.5, False),
    ("any", NEWS_BAD, 200, None, False),
    ("news", NEWS_BAD, 10, 3.0, True),
    ("news", NEWS_BAD, 200, -5.0, False),
    ("price", NEWS_OK, None, -1.5, True),
    ("price", NEWS_BAD, 10, -1.0, False),
])
def test_evaluate_mood_modes(require, news, age, chg, expected):
    mood = evaluate_mood(news=news, news_age_minutes=age, market_change_pct=chg,
                         settings=settings(smile_mood_require=require))
    assert mood.pessimistic is expected
    assert mood.market_change_pct == chg
    assert mood.reasons


def test_evaluate_mood_none_flags_and_thresholds():
    s = settings()
    stale = evaluate_mood(news=NEWS_BAD, news_age_minutes=91, market_change_pct=None, settings=s)
    assert stale.news_pessimistic is None and stale.price_pessimistic is None
    low = evaluate_mood(news={"negative": 9, "positive": 0, "score": 1.0, "evidence": []},
                        news_age_minutes=5, market_change_pct=-1.4, settings=s)
    assert low.news_pessimistic is False and low.price_pessimistic is False
    fresh = evaluate_mood(news=NEWS_BAD, news_age_minutes=90, market_change_pct=-1.5, settings=s)
    assert fresh.pessimistic and fresh.evidence == NEWS_BAD["evidence"]


def test_new_settings_defaults_and_validation():
    s = settings()
    assert s.smile_mood_sell_enabled is False and s.smile_news_block_enabled is False
    assert s.smile_mood_require == "both"
    with pytest.raises(ValueError):
        settings(smile_mood_market_drop_pct=-1)
    with pytest.raises(ValueError):
        settings(smile_mood_require="bogus")
