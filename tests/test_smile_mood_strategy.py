"""微笑策略盤中「時事＋即時股價」：情緒賣出與壞消息擋買。新聞檔一律寫在 tmp_path。"""
import datetime as dt
import json
from unittest.mock import patch

import pytest
from shioaji.constant import OrderState  # noqa: F401  (與 test_simulation_check 相同的 shioaji 載入)

from bot.broker import SjBroker
from bot.config import Settings
from bot.models import MarketTick
from bot.paper_broker import PaperApi
from bot.rss_archive import archive_path
from bot.simulation_check import _strategy
from bot.utils import now_tw

BASE = dict(_env_file=None, api_key="local", secret_key="local", run_mode="trade",
            simulation=True, strategy_type="smile_curve", symbols=["0050"],
            symbols_auto_merge=False, max_fund=5000, daily_fund_budget=0,
            use_odd_lot=True, smile_base_lot=10, odd_lot_max_shares=20,
            min_price=0, max_price=0, telegram_bot_token="", telegram_chat_id="",
            llm_gate_enabled=False, llm_sell_gate_enabled=False,
            llm_intraday_review_enabled=False, broker_fee_discount=0, broker_min_fee=0,
            smile_reference_prices={"0050": 100.0}, smile_mood_news_min_negative=2)


def setup(root, **overrides):
    settings = Settings(**{**BASE, **overrides})
    broker = SjBroker(settings, simulation_only=True)
    api = PaperApi(settings, broker._dispatch_order_callback, auto_fill=True)
    broker.api = api
    broker._contracts["0050"] = api.contract("0050")
    return _strategy(settings, broker, root), api


def tick(strategy, price):
    with patch("bot.strategy_smile.now_tw_time", return_value=dt.time(10)):
        strategy.on_tick(MarketTick(now_tw(), "0050", price, 1, 0, 100, "local_paper"))


def write_news(root, titles, *, collected_minutes_ago=5, related=("0050",)):
    now = now_tw()
    ts = (now - dt.timedelta(minutes=30)).isoformat(timespec="seconds")
    items = [{"title": t, "url": f"http://x/{i}", "desc": "", "published_at": ts, "feed": "f",
              "stance": "", "first_seen_at": ts, "related_tickers": list(related)}
             for i, t in enumerate(titles)]
    path = archive_path(root, now.date().isoformat())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "date": now.date().isoformat(),
        "collected_at": (now - dt.timedelta(minutes=collected_minutes_ago)).isoformat(timespec="seconds"),
        "items": items}, ensure_ascii=False), encoding="utf-8")


BEARISH = ["台股重挫", "美股暴跌 恐慌蔓延"]


def held(tmp_path, **overrides):
    """標準價 100，99 買進 10 股；之後價格 99.9 淨利為正、98.5 為淨虧。"""
    strategy, api = setup(tmp_path, **overrides)
    tick(strategy, 100)
    tick(strategy, 99)
    assert strategy.positions["0050"].quantity == 10 and len(api.trades) == 1
    return strategy, api


def mood_sell_calls(strategy, price):
    with patch.object(strategy, "_place_stop_sell", wraps=strategy._place_stop_sell) as spy:
        tick(strategy, price)
    return [c for c in spy.call_args_list if "smile_mood" in c.args]


def test_profit_and_pessimistic_market_sells_whole_position(tmp_path):
    write_news(tmp_path, BEARISH)
    strategy, api = held(tmp_path, smile_mood_sell_enabled=True, smile_mood_require="news")
    calls = mood_sell_calls(strategy, 99.9)
    assert len(calls) == 1 and calls[0].args[1] == 10
    assert len(api.trades) == 2 and not strategy.positions
    e = strategy.engines["0050"]
    assert not e.state.disabled and not e.state.cooldown_until and not e.open_lots
    row, = [json.loads(l) for l in
            (strategy._state_root / "data" / "smile_mood_log.jsonl").read_text("utf-8").splitlines()]
    assert row["action"] == "mood_sell" and row["symbol"] == "0050" and row["price"] == 99.9
    assert row["mood"]["pessimistic"] is True and row["mood"]["evidence"]


def test_price_mode_uses_market_change_from_prev_close(tmp_path):
    strategy, api = held(tmp_path, smile_mood_sell_enabled=True, smile_mood_require="price")
    strategy._prev_close["0050"] = 99.95  # 99.9 只跌 0.05%
    assert not mood_sell_calls(strategy, 99.9)
    strategy._prev_close["0050"] = 105.0  # 跌約 4.9%
    assert len(mood_sell_calls(strategy, 99.9)) == 1
    assert not strategy.positions


def test_losing_position_is_never_sold_by_mood(tmp_path):
    write_news(tmp_path, BEARISH)
    strategy, api = held(tmp_path, smile_mood_sell_enabled=True, smile_mood_require="news")
    assert not mood_sell_calls(strategy, 98.5)
    assert len(api.trades) == 1 and strategy.positions["0050"].quantity == 10


def test_profit_without_pessimism_does_not_sell(tmp_path):
    write_news(tmp_path, ["台股大漲創高", "美股飆漲", "指數反彈"])
    strategy, api = held(tmp_path, smile_mood_sell_enabled=True, smile_mood_require="news")
    assert not mood_sell_calls(strategy, 99.9)
    assert len(api.trades) == 1


def test_min_profit_pct_blocks_small_profit(tmp_path):
    write_news(tmp_path, BEARISH)
    strategy, api = held(tmp_path, smile_mood_sell_enabled=True, smile_mood_require="news",
                         smile_mood_min_profit_pct=5)
    assert not mood_sell_calls(strategy, 99.9)
    assert len(api.trades) == 1


def test_stale_news_with_require_both_does_not_sell(tmp_path):
    write_news(tmp_path, BEARISH, collected_minutes_ago=300)
    strategy, api = held(tmp_path, smile_mood_sell_enabled=True, smile_mood_require="both")
    strategy._prev_close["0050"] = 105.0  # 股價悲觀，但新聞過期＝無法判斷
    assert not mood_sell_calls(strategy, 99.9)
    assert len(api.trades) == 1


def test_bad_company_news_blocks_buy_and_logs_once_per_day(tmp_path):
    write_news(tmp_path, ["某公司董座遭搜索 涉掏空"])
    strategy, api = setup(tmp_path, smile_news_block_enabled=True)
    tick(strategy, 100)
    tick(strategy, 99)
    tick(strategy, 98.9)
    assert not api.trades and not strategy.positions
    lines = (strategy._state_root / "data" / "smile_mood_log.jsonl").read_text("utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["action"] == "news_block"
    assert json.loads(lines[0])["bad_news"][0]["title"].startswith("某公司")


def test_news_block_ignores_other_tickers_and_does_not_sell(tmp_path):
    write_news(tmp_path, ["某公司董座遭搜索 涉掏空"], related=("2330",))
    strategy, api = setup(tmp_path, smile_news_block_enabled=True)
    tick(strategy, 100)
    tick(strategy, 99)
    assert len(api.trades) == 1  # 別檔的壞消息不擋
    write_news(tmp_path, ["某公司董座遭搜索 涉掏空"])
    strategy._news_cache = None
    tick(strategy, 99.9)  # 已持倉有淨利：壞消息只擋買，不觸發賣出
    assert len(api.trades) == 1 and strategy.positions["0050"].quantity == 10


def test_disabled_features_never_read_news_and_keep_behaviour(tmp_path):
    write_news(tmp_path, BEARISH + ["某公司董座遭搜索 涉掏空"])
    with patch("bot.strategy_smile.load_recent_items",
               side_effect=AssertionError("不應讀新聞檔")) as loader:
        strategy, api = held(tmp_path)
        strategy._prev_close["0050"] = 105.0
        tick(strategy, 99.9)  # 未回到標準價：不賣
        assert len(api.trades) == 1
        tick(strategy, 100)  # 回到標準價且有淨利：既有停利
        assert len(api.trades) == 2 and not strategy.positions
        tick(strategy, 99)  # 再買進，未被壞消息擋
        assert len(api.trades) == 3
        loader.assert_not_called()
    assert not (strategy._state_root / "data" / "smile_mood_log.jsonl").exists()


def test_log_write_failure_does_not_break_on_tick(tmp_path):
    write_news(tmp_path, BEARISH)
    strategy, api = held(tmp_path, smile_mood_sell_enabled=True, smile_mood_require="news")
    # 讓紀錄檔路徑變成資料夾，寫入必然失敗。
    (strategy._state_root / "data" / "smile_mood_log.jsonl").mkdir(parents=True)
    tick(strategy, 99.9)
    assert len(api.trades) == 2


def test_market_change_prefers_0050_else_median(tmp_path):
    strategy, _ = setup(tmp_path, symbols=["0050", "2330", "2317"])
    assert strategy._market_change_pct() is None
    strategy._prev_close.update({"0050": 100.0, "2330": 100.0, "2317": 100.0})
    strategy._last_price.update({"0050": 98.0, "2330": 90.0, "2317": 101.0})
    assert strategy._market_change_pct() == pytest.approx(-2.0)
    strategy.settings = strategy.settings.model_copy(update={"symbols": ["2330", "2317"]})
    assert strategy._market_change_pct() == pytest.approx(-4.5)
