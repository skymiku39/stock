import datetime as dt
import json

from bot import forward_tracker as forward
from bot.stock_db import PriceBar, StockDB

TZ = dt.timezone(dt.timedelta(hours=8))


def _weekdays(start, end):
    day = start
    while day <= end:
        if day.weekday() < 5:
            yield day
        day += dt.timedelta(days=1)


def _project(tmp_path, symbols, last_day, *, lagging=()):
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config/backtest_universe.json").write_text(
        json.dumps({"symbols": [{"symbol": s} for s in symbols]}), encoding="utf-8")
    db = StockDB.open(root=tmp_path)
    try:
        bars = []
        for index, symbol in enumerate(symbols):
            end = last_day - dt.timedelta(days=3) if symbol in lagging else last_day
            for n, day in enumerate(_weekdays(dt.date(2026, 5, 4), end)):
                price = 50 + index + n * 0.1
                bars.append(PriceBar(symbol=symbol, date=day.isoformat(), open=price, high=price + 1,
                                     low=price - 1, close=price + 0.5, volume=5_000_000))
        db.bulk_upsert_price_bars(bars)
    finally:
        db.close()
    return tmp_path


def test_forward_run_uses_frozen_params_and_only_forward_dates(tmp_path, monkeypatch):
    monkeypatch.setattr(forward, "now_tw", lambda: dt.datetime(2026, 11, 17, 16, tzinfo=TZ))
    root = _project(tmp_path, ["1101", "1102", "1103"], dt.date(2026, 11, 16))
    result = forward.run_forward(root)
    assert result["as_of"] == "2026-11-16" and result["trading_days"] == 11 and result["coverage_ok"]
    assert result["strategies"]["smile"]["params"] == forward.FROZEN["smile"]
    assert result["strategies"]["swing"]["params"] == {"breakout_days": 20, "trail_atr": 4.0}
    assert result["strategies"]["c2"]["params"] == {"max_hold_days": 90, "trail_atr": 4.0}
    assert result["strategies"]["crash"]["params"] == {"first_drop_pct": 20.0, "max_hold_days": 90}
    assert result["strategies"]["crash_aggr"]["params"] == {"first_drop_pct": 10.0, "weighting": "all_in"}
    for data in result["strategies"].values():
        assert all(point["date"] >= forward.FORWARD_START for point in data["equity_curve"])
        assert all(r["entry_date"] >= forward.FORWARD_START for r in data["rounds"])
    text = forward.render(result)
    assert "樣本不足" in text and "2026-11-01～2026-11-16" in text


def test_update_writes_once_per_data_day_and_waits_for_coverage(tmp_path, monkeypatch):
    monkeypatch.setattr(forward, "now_tw", lambda: dt.datetime(2026, 11, 17, 16, tzinfo=TZ))
    out = tmp_path / forward.OUTPUT_REL
    # 三檔裡兩檔還沒有最新一天：涵蓋率不足，不寫檔。
    root = _project(tmp_path, ["1101", "1102", "1103"], dt.date(2026, 11, 16), lagging=("1102", "1103"))
    assert not forward.update(root)["coverage_ok"] and not (out / "latest.json").exists()
    _project(tmp_path, ["1101", "1102", "1103"], dt.date(2026, 11, 16))
    first = forward.update(root)
    assert first["coverage_ok"] and (out / "2026-11-16.json").exists() and (out / "latest.md").exists()
    # 資料日沒有前進：沿用既有結果，不重寫。
    stamp = (out / "latest.json").stat().st_mtime_ns
    monkeypatch.setattr(forward, "now_tw", lambda: dt.datetime(2026, 11, 17, 18, tzinfo=TZ))
    assert forward.update(root)["generated_at"] == first["generated_at"]
    assert (out / "latest.json").stat().st_mtime_ns == stamp
    assert forward.update(root, force=True)["generated_at"] != first["generated_at"]


def test_update_rewrites_when_same_day_prices_are_corrected(tmp_path, monkeypatch):
    monkeypatch.setattr(forward, "now_tw", lambda: dt.datetime(2026, 11, 17, 16, tzinfo=TZ))
    root = _project(tmp_path, ["1101", "1102"], dt.date(2026, 11, 16))
    first = forward.update(root)
    # 同一個資料日、同樣的檔數，但 11-16 的日 K 事後被修正：結果不同就要重寫。
    db = StockDB.open(root=tmp_path)
    try:
        db.bulk_upsert_price_bars([PriceBar(symbol="1101", date="2026-11-16", open=20, high=21,
                                            low=19, close=20, volume=5_000_000)])
    finally:
        db.close()
    monkeypatch.setattr(forward, "now_tw", lambda: dt.datetime(2026, 11, 17, 18, tzinfo=TZ))
    second = forward.update(root)
    assert second["as_of"] == first["as_of"]
    assert second["generated_at"] != first["generated_at"]
