"""實際持股的微笑盯盤：純函式與提醒去重（不連網、不碰專案 data/）。"""
import json

import pytest

from bot.config import Settings
from bot.smile_watch import (Holding, SmileWatcher, build_engine, evaluate, format_alert,
                             parse_holding)

TS = "2026-11-19T10:00:00+08:00"


def settings(**kw):
    base = dict(smile_symbol_stop_loss_pct=25.0, smile_take_profit_pct=0.0,
                max_lot_per_symbol=2, broker_fee_discount=0.28)
    base.update(kw)
    return Settings(_env_file=None).model_copy(update=base)


def engine(lots=4, avg=85.65, reference=105.0, **kw):
    return build_engine(Holding("1514", lots, avg), reference, settings(**kw))


def test_parse_holding():
    assert parse_holding("1514", "4@85.65") == Holding("1514", 4, 85.65)
    for bad in ("4", "x@1", "0@85", "4@0"):
        with pytest.raises(ValueError):
            parse_holding("1514", bad)


def test_loss_position_has_no_signal_and_reports_levels():
    st = evaluate(engine(), 75.9, TS)
    assert st.signals == []
    assert st.pnl_twd < 0 and st.pnl_pct == pytest.approx(-11.7, abs=0.2)
    assert st.breakeven_price == pytest.approx(85.98, abs=0.05)
    assert st.stop_price == pytest.approx(64.49, abs=0.05)
    assert st.drop_pct == pytest.approx(27.71, abs=0.01)
    assert st.tiers_crossed == 4 and st.add_room_lots == 0


def test_breakeven_and_stop_prices_are_where_the_engine_flips():
    e = engine()
    st = evaluate(e, 80.0, TS)
    assert e.unrealized_pnl(st.breakeven_price + 0.05) > 0 > e.unrealized_pnl(st.breakeven_price - 0.05)
    assert e.stop_loss_breached(st.stop_price - 0.05)
    assert not e.stop_loss_breached(st.stop_price + 0.05)


def test_signals_by_price():
    e = engine()
    assert evaluate(e, 66.0, TS).signals == ["stop_near"]
    assert evaluate(e, 64.0, TS).signals == ["stop_loss"]
    assert evaluate(e, 90.0, TS).signals == ["breakeven"]  # 有淨利但還沒回到標準價
    assert evaluate(e, 105.0, TS).signals == ["take_profit"]


def test_take_profit_pct_and_disabled_stop():
    assert evaluate(engine(smile_take_profit_pct=5.0), 91.0, TS).signals == ["take_profit"]
    st = evaluate(engine(smile_symbol_stop_loss_pct=0.0), 50.0, TS)
    assert st.stop_price is None and st.signals == []


def test_add_buy_only_with_room_under_symbol_cap():
    assert "add_buy" in evaluate(engine(lots=1, avg=100.0), 96.0, TS).signals
    assert "add_buy" not in evaluate(engine(lots=1, avg=100.0), 104.5, TS).signals  # 未達第一階
    assert "add_buy" not in evaluate(engine(lots=2, avg=100.0), 96.0, TS).signals  # 已達上限


def test_watcher_alerts_once_per_day_and_survives_restart(tmp_path):
    sent = []
    e = engine()
    watcher = SmileWatcher(tmp_path, sent.append)
    assert watcher.handle(evaluate(e, 66.0, TS)) == ["stop_near"]
    assert watcher.handle(evaluate(e, 65.5, TS)) == []
    assert watcher.handle(evaluate(e, 64.0, TS)) == ["stop_loss"]
    assert len(sent) == 2 and "不會下單" in sent[0]
    restarted = SmileWatcher(tmp_path, sent.append)
    assert restarted.handle(evaluate(e, 64.0, TS)) == []
    assert restarted.handle(evaluate(e, 64.0, "2026-11-20T09:05:00+08:00")) == ["stop_loss"]
    rows = [json.loads(x) for x in (tmp_path / "alerts.jsonl").read_text("utf-8").splitlines()]
    assert [r["signal"] for r in rows] == ["stop_near", "stop_loss", "stop_loss"]
    assert json.loads((tmp_path / "status-1514.json").read_text("utf-8"))["price"] == 64.0


def test_format_alert_mentions_levels():
    text = format_alert(evaluate(engine(), 66.0, TS), "stop_near")
    assert "接近單檔停損" in text and "64.48" in text and "105.00" in text


def test_usable_price_accepts_trade_or_mid_of_today_only():
    from bot.smile_watch import _usable_price
    today = "2026-11-19"
    quote = {"price": 76.5, "price_basis": "bid_ask_mid", "quote_date": today}
    assert _usable_price(quote, today) == 76.5
    assert _usable_price({**quote, "price_basis": "last_trade"}, today) == 76.5
    assert _usable_price({**quote, "price_basis": "best_bid"}, today) is None  # 只有單邊掛單
    assert _usable_price({**quote, "quote_date": "2026-11-18"}, today) is None
    assert _usable_price({**quote, "price": None}, today) is None
    assert _usable_price(None, today) is None


def test_load_targets(tmp_path):
    from bot.smile_watch import WatchTarget, load_targets
    path = tmp_path / "smile_watch.json"
    assert load_targets(path) == []
    path.write_text(json.dumps({"targets": [
        {"symbol": "1514", "reference": 105},
        {"symbol": "2412", "reference": 130.5, "stop_loss_pct": 15, "holding": "2@120"}]}),
        encoding="utf-8")
    assert load_targets(path) == [
        WatchTarget("1514", 105.0),
        WatchTarget("2412", 130.5, 15.0, Holding("2412", 2, 120.0))]
    for bad in ('{"targets": [{"symbol": "1514"}]}', "not json",
                '{"targets": [{"symbol": "1514", "reference": 0}]}',
                '{"targets": [{"symbol": "1", "reference": 5}, {"symbol": "1", "reference": 6}]}'):
        path.write_text(bad, encoding="utf-8")
        with pytest.raises(ValueError):
            load_targets(path)


def test_repo_watch_list_is_valid():
    from pathlib import Path

    from bot.smile_watch import load_targets
    targets = load_targets(Path(__file__).resolve().parents[1] / "config" / "smile_watch.json")
    assert all(t.reference > 0 for t in targets)


def test_status_file_and_alert_record_source(tmp_path):
    eng = engine()
    watcher = SmileWatcher(tmp_path, None)
    status = evaluate(eng, 100.0, TS)
    assert status.price_source == "primary"
    assert "報價來源" not in format_alert(status, "breakeven")  # 主來源：提醒文字不變
    status.price_source, status.price_note = "fugle", "鎖漲停、最後成交價 100、時間 11:01:16"
    watcher.handle(status)
    saved = json.loads((tmp_path / "status-1514.json").read_text(encoding="utf-8"))
    assert saved["price_source"] == "fugle" and "鎖漲停" in saved["price_note"]
    assert "報價來源：fugle" in format_alert(status, "breakeven")
