"""賣出改扣最近買進的：用 2026-11-23 亞力 1514 的實際數字。"""
import json
from types import SimpleNamespace

import pytest

from bot import portfolio, smile_watch
from bot.lot_view import Lot, Sale, rematch_latest_first

REMAINING = [Lot("2026-11-03", 1, 88425), Lot("2026-11-06", 1, 79813),
             Lot("2026-11-19", 1, 75807), Lot("2026-11-20", 1, 67996)]
# 11-23 以 68.9 賣一張，券商扣掉 08-04 買的那張（成本 98,540）、帳面實現 -29,944
SALE = Sale("2026-11-23", 1, -29944 + 98540, (Lot("2026-08-04", 1, 98540),))


def test_latest_buy_is_sold_and_old_lot_stays():
    view = rematch_latest_first(REMAINING, [SALE], "2026-11-23")
    assert [lot.date for lot in view.lots] == ["2026-08-04", "2026-11-03", "2026-11-06", "2026-11-19"]
    assert view.qty == 4 and view.cost == 98540 + 88425 + 79813 + 75807
    assert round(view.cost / 4000, 2) == 85.65  # 券商帳面是 78.01
    assert view.sales[0].pnl == 68596 - 67996 and view.sales[0].broker_pnl == -29944
    # 兩種認定只是已實現與未實現的分法不同，總和一樣
    value = 4 * 69000
    assert view.realized_pnl + value - view.cost == pytest.approx(
        view.broker_realized_pnl + value - sum(lot.cost for lot in REMAINING))


def test_sales_before_since_keep_broker_matching():
    view = rematch_latest_first(REMAINING, [SALE], "2026-11-24")
    assert view.lots == REMAINING and view.sales == []


def test_sale_only_takes_buys_up_to_its_date_and_splits_lots():
    remaining = [Lot("2026-11-10", 2, 200000), Lot("2026-11-21", 1, 70000)]
    sales = [Sale("2026-11-12", 1, 95000, (Lot("2026-08-01", 1, 120000),)),
             Sale("2026-11-22", 2, 150000, (Lot("2026-08-02", 2, 260000),))]
    view = rematch_latest_first(remaining, sales, "2026-11-01")
    # 11-12 賣的扣 11-10 的一半（不能扣之後才買的 11-21）；11-22 賣的扣 11-21 與 11-10 剩下的
    assert [(s.date, s.cost) for s in view.sales] == [("2026-11-12", 100000), ("2026-11-22", 170000)]
    assert [(lot.date, lot.qty) for lot in view.lots] == [("2026-08-01", 1), ("2026-08-02", 2)]


def test_sale_larger_than_available_buys_is_an_error():
    with pytest.raises(ValueError):
        rematch_latest_first([], [Sale("2026-11-23", 2, 1.0, (Lot("2026-08-04", 1, 1.0),))], "2026-11-01")


def _snapshot(history, qty=4, error=""):
    return SimpleNamespace(error="", asof="2026-11-23T12:00:00+08:00", share_positions=[],
                           positions=[SimpleNamespace(symbol="1514", qty=qty, avg_price=78.01,
                                                      last_price=69.0, pnl=-37261.0)],
                           lot_history=history, lot_history_error=error)


def test_watch_uses_recomputed_average_and_writes_detail(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    asked = {}

    def fake(settings, lots_since=None):
        asked.update(lots_since or {})
        return _snapshot({"1514": {"remaining": REMAINING, "sales": [SALE]}})

    monkeypatch.setattr(portfolio, "fetch_shioaji_positions", fake)
    settings = SimpleNamespace(model_copy=lambda update: SimpleNamespace(**update))
    found = smile_watch.fetch_real_holdings(settings, ["1514"], {"1514": "2026-11-23"})
    assert asked == {"1514": "2026-11-23"}
    assert found == {"1514": smile_watch.Holding("1514", 4, 85.65, 78.01)}
    detail = json.loads((tmp_path / "data/smile_watch/lots-1514.json").read_text(encoding="utf-8"))
    assert detail["avg_price"] == 85.65 and detail["realized_pnl_twd"] == 600
    assert detail["broker_realized_pnl_twd"] == -29944

    from bot.tray import status as tray_status
    note = tray_status.read_cost_note(tmp_path, "1514", 4, 85.65)
    assert "78.01" in note and "+600" in note and "-29,944" in note
    assert tray_status.read_cost_note(tmp_path, "1514", 5, 85.65) == ""  # 張數對不上就不顯示
    assert tray_status.read_cost_note(tmp_path, "1514", 4, 78.01) == ""  # 狀態檔還是帳面均價

    # 對不起來時沿用券商帳面均價，並清掉舊的明細檔
    monkeypatch.setattr(portfolio, "fetch_shioaji_positions",
                        lambda settings, lots_since=None: _snapshot({}, error="boom"))
    found = smile_watch.fetch_real_holdings(settings, ["1514"], {"1514": "2026-11-23"})
    assert found == {"1514": smile_watch.Holding("1514", 4, 78.01)}
    assert not (tmp_path / "data/smile_watch/lots-1514.json").exists()


def test_targets_accept_sell_latest_first_since(tmp_path):
    path = tmp_path / "smile_watch.json"
    path.write_text(json.dumps({"targets": [
        {"symbol": "1514", "reference": 105, "sell_latest_first_since": "2026-11-23"}]}), encoding="utf-8")
    assert smile_watch.load_targets(path)[0].latest_first_since == "2026-11-23"
    path.write_text(json.dumps({"targets": [
        {"symbol": "1514", "reference": 105, "sell_latest_first_since": "last week"}]}), encoding="utf-8")
    with pytest.raises(ValueError):
        smile_watch.load_targets(path)
