"""ETF 關注名單：規模變化校正、主動與被動分開計數、標籤（不連網、不碰專案 data/）。"""
import datetime as dt
import json

from bot import etf_focus
from bot.active_etf import (ActiveEtf, Holding, HoldingsSnapshot, load_passive_etfs,
                            load_tracked_etfs, save_holdings)

OLD, NEW = dt.date(2026, 10, 20), dt.date(2026, 11, 19)
FILLER = [f"90{i:02d}" for i in range(6)]  # 讓規模變化估得出來的共同持股


def etf(symbol, kind="active"):
    return ActiveEtf(symbol=symbol, name=symbol, issuer="", kind=kind)


def save(root, symbol, date, shares, weight=12.0):
    """shares：{代號: 股數}。"""
    save_holdings(HoldingsSnapshot(symbol, date, [
        Holding(t, f"股{t}", weight, s, 0.0) for t, s in shares.items()]), root)


def filler(scale=1.0):
    return {t: 1000 * scale for t in FILLER}


def by_ticker(result):
    return {r["ticker"]: r for r in result["stocks"]}


def test_scale_factor_is_median_ratio_of_common_holdings():
    old = {t: Holding(t, "", 1, 1000) for t in FILLER}
    new = {t: Holding(t, "", 1, 1200) for t in FILLER}
    new[FILLER[0]] = Holding(FILLER[0], "", 1, 5000)  # 單一檔大幅加碼不影響中位數
    assert etf_focus.scale_factor(old, new) == 1.2
    assert etf_focus.scale_factor({}, new) == 1.0


def test_inflow_is_not_counted_as_adding(tmp_path):
    """ETF 規模變 1.5 倍時，等比例增加的股數不算加碼；超過規模變化的才算。"""
    save(tmp_path, "A1", OLD, {**filler(), "2454": 1000, "2303": 1000, "3037": 1000})
    save(tmp_path, "A1", NEW, {**filler(1.5), "2454": 1500, "2303": 1000, "3037": 2000})
    rows = by_ticker(etf_focus.build_focus(tmp_path, etfs=[etf("A1")]))
    assert rows["2454"]["active"] == {"new": [], "add": [], "reduce": [], "exit": []}
    assert rows["2303"]["active"]["reduce"] == ["A1"]  # 股數沒變＝相對減碼
    assert rows["3037"]["active"]["add"] == ["A1"]


def test_new_and_exit_and_tags(tmp_path):
    actives = [etf(f"A{i}") for i in range(4)]
    for e in actives:
        save(tmp_path, e.symbol, OLD, {**filler(), "2360": 1000})
        save(tmp_path, e.symbol, NEW, {**filler(), "3661": 1000})
    result = etf_focus.build_focus(tmp_path, etfs=actives)
    rows = by_ticker(result)
    assert result["active_total"] == 4 and result["net_threshold"] == 3
    assert rows["3661"]["active"]["new"] == ["A0", "A1", "A2", "A3"]
    assert rows["3661"]["tags"] == [etf_focus.TAG_CORE, etf_focus.TAG_ADD]
    assert rows["2360"]["active_count"] == 0 and rows["2360"]["active_net"] == -4
    assert rows["2360"]["tags"] == [etf_focus.TAG_REDUCE]
    assert "3661" in result["focus"] and "2360" in result["caution"]
    assert "2360" not in result["focus"]


def test_passive_counted_separately_and_only_index_changes_flagged(tmp_path):
    save(tmp_path, "A1", OLD, {**filler(), "2330": 1000})
    save(tmp_path, "A1", NEW, {**filler(), "2330": 1000})
    save(tmp_path, "P1", OLD, {**filler(), "2330": 1000, "1101": 1000})
    save(tmp_path, "P1", NEW, {**filler(), "2330": 3000, "2882": 1000})
    result = etf_focus.build_focus(tmp_path, etfs=[etf("A1"), etf("P1", "passive")])
    rows = by_ticker(result)
    assert (result["active_total"], result["passive_total"]) == (1, 1)
    assert rows["2330"]["active_count"] == 1 and rows["2330"]["passive_count"] == 1
    # 被動式的股數增加只是跟著指數權重，不當成加碼
    assert rows["2330"]["active"]["add"] == [] and rows["2330"]["active_net"] == 0
    assert rows["2882"]["passive"]["new"] == ["P1"]
    assert rows["2882"]["tags"] == [etf_focus.TAG_INDEX_IN]
    assert rows["1101"]["tags"] == [etf_focus.TAG_INDEX_OUT]


def test_overseas_etf_and_missing_data_are_skipped(tmp_path):
    save(tmp_path, "US1", NEW, {"NVDA US": 1000, "2330": 10}, weight=40.0)
    result = etf_focus.build_focus(tmp_path, etfs=[etf("US1"), etf("NONE")])
    assert result["etfs"] == [] and result["stocks"] == []
    assert {s["symbol"] for s in result["skipped"]} == {"US1", "NONE"}


def test_single_snapshot_has_holders_but_no_changes(tmp_path):
    save(tmp_path, "A1", NEW, {**filler(), "2330": 1000})
    result = etf_focus.build_focus(tmp_path, etfs=[etf("A1")])
    assert result["etfs"][0]["base"] is None
    assert by_ticker(result)["2330"]["active"] == {"new": [], "add": [], "reduce": [], "exit": []}


def test_pick_base_date_prefers_snapshot_at_least_lookback_old():
    dates = [NEW, dt.date(2026, 11, 10), OLD, dt.date(2026, 9, 18)]
    assert etf_focus.pick_base_date(dates, NEW, 28) == OLD
    assert etf_focus.pick_base_date([NEW, dt.date(2026, 11, 10)], NEW, 28) == dt.date(2026, 11, 10)
    assert etf_focus.pick_base_date([NEW], NEW, 28) is None


def test_update_writes_report_files(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "passive_etfs.json").write_text(
        json.dumps({"etfs": [{"symbol": "0050", "name": "元大台灣50"}]}), encoding="utf-8")
    save(tmp_path, "0050", NEW, {**filler(), "2330": 1000})
    result = etf_focus.update(tmp_path)
    folder = tmp_path / "data" / "etf_focus"
    assert result["passive_total"] == 1 and result["as_of"] == NEW.isoformat()
    assert json.loads((folder / "latest.json").read_text(encoding="utf-8"))["as_of"] == NEW.isoformat()
    assert (folder / f"{NEW.isoformat()}.json").exists()
    assert "# ETF 關注名單" in (folder / "latest.md").read_text(encoding="utf-8")


def test_passive_list_loading(tmp_path):
    assert load_passive_etfs(tmp_path) == []
    (tmp_path / "config").mkdir()
    path = tmp_path / "config" / "passive_etfs.json"
    path.write_text("{broken", encoding="utf-8")
    assert load_passive_etfs(tmp_path) == []
    path.write_text(json.dumps({"etfs": [{"symbol": "0050", "name": "元大台灣50"},
                                         {"symbol": "00981A"}, {"name": "沒有代號"}]}),
                    encoding="utf-8")
    passive = load_passive_etfs(tmp_path)
    assert [(e.symbol, e.kind) for e in passive] == [("0050", "passive"), ("00981A", "passive")]
    assert passive[0].holdings_url == "https://www.etfinfo.tw/etf/0050/holdings"
    tracked = load_tracked_etfs(tmp_path)
    # 代號重複時以主動式名單為準，不會出現兩次
    assert [e.kind for e in tracked if e.symbol == "00981A"] == ["active"]
    assert tracked[-1].symbol == "0050"
