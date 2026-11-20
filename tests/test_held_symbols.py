"""持股名單：手動與自動記錄取聯集、只增不減（不連網、不碰專案 data/）。"""
import json
from types import SimpleNamespace

from bot import active_etf, daily_collection, portfolio, smile_watch
from bot.held_symbols import load_held_symbols, load_seen, record_held, seen_path


def write_manual(root, payload):
    (root / "config").mkdir(exist_ok=True)
    (root / "config" / "held_symbols.json").write_text(
        payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")


def test_union_of_manual_and_seen(tmp_path):
    assert load_held_symbols(tmp_path) == []
    write_manual(tmp_path, {"symbols": ["1514", " 2330 ", "NVDA US", ""]})
    assert record_held(tmp_path, ["3037", "1514"]) == ["3037", "1514"]
    assert load_held_symbols(tmp_path) == ["1514", "2330", "3037"]


def test_record_only_adds_and_keeps_first_seen(tmp_path):
    record_held(tmp_path, ["1514"])
    first = load_seen(tmp_path)["1514"]
    assert record_held(tmp_path, ["1514", "1514", "bad", "2603"]) == ["2603"]
    # 之後查不到（賣掉）也不會被移除
    assert record_held(tmp_path, []) == []
    seen = load_seen(tmp_path)
    assert set(seen) == {"1514", "2603"} and seen["1514"] == first


def test_broken_files_do_not_break_the_other_source(tmp_path):
    write_manual(tmp_path, "{broken")
    record_held(tmp_path, ["1514"])
    assert load_held_symbols(tmp_path) == ["1514"]
    seen_path(tmp_path).write_text("[1, 2]", encoding="utf-8")
    write_manual(tmp_path, {"symbols": ["2330"]})
    assert load_held_symbols(tmp_path) == ["2330"]


def test_extended_price_symbols_include_held(tmp_path, monkeypatch):
    monkeypatch.setattr(active_etf, "load_active_etfs", lambda root: [])
    write_manual(tmp_path, {"symbols": ["1514"]})
    record_held(tmp_path, ["9999"])
    assert daily_collection.extended_price_symbols(tmp_path) == ["1514", "9999"]


def test_smile_watch_records_every_broker_position(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    positions = [SimpleNamespace(symbol="1514", qty=5, avg_price=82.12),
                 SimpleNamespace(symbol="2603", qty=0.5, avg_price=200.0),  # 零股、不在盯盤名單
                 SimpleNamespace(symbol="2330", qty=0, avg_price=0.0)]
    monkeypatch.setattr(portfolio, "fetch_shioaji_positions",
                        lambda settings: SimpleNamespace(error="", positions=positions))
    settings = SimpleNamespace(model_copy=lambda update: SimpleNamespace(**update))
    found = smile_watch.fetch_real_holdings(settings, ["1514"])
    assert found == {"1514": smile_watch.Holding("1514", 5, 82.12)}
    assert load_held_symbols(tmp_path) == ["1514", "2603"]
