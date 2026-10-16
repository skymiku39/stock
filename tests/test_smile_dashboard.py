from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from bot.smile_dashboard_service import BASELINE, RunRequest, run_comparison, save_run
from bot.stock_db import PriceBar


@pytest.fixture
def root(tmp_path):
    baseline = tmp_path / BASELINE
    baseline.mkdir(parents=True)
    histories, documents = {}, {}
    for sym in ("2303","2330"):
        bars = [PriceBar(sym,f"2026-01-0{i+1}",open=o,close=c,volume=100000,source_record_id=sym)
                for i,(o,c) in enumerate([(100,100),(100,98),(99,102),(103,103)])]
        histories[sym] = [asdict(b) for b in bars]
        raw = json.dumps(dict(title=f"115年01月 {sym} 測試",fields=["日期","成交股數"],
                              data=[[f"115/01/0{i+1}","100,000,000"] for i in range(4)])).encode()
        path = tmp_path / f"{sym}.json"
        path.write_bytes(raw)
        documents[sym] = dict(path=path.name,sha256=hashlib.sha256(raw).hexdigest())
    data = json.dumps(histories).encode()
    (baseline/"inputs.json").write_bytes(data)
    (baseline/"comparison.json").write_text(json.dumps(dict(input_sha256=hashlib.sha256(data).hexdigest(),documents=documents)))
    return tmp_path


def request():
    return RunRequest(("2303","2330"),"2026-01-01","2026-01-04")


def test_scenarios_use_same_capital_and_percent_units(root):
    result = run_comparison(root,request())
    assert len(result["results"]) == 3
    for r in result["results"].values():
        assert r["initial_cash"] == 500000
    current = result["results"]["目前設定"]
    assert current["execution"]["slippage_bps"] == 10
    assert current["execution"]["participation"] == .001
    assert current["net_pnl_twd"] < result["results"]["理想成交"]["net_pnl_twd"]


def test_reject_invalid_range_symbols_and_input_tampering(root):
    for r in [replace(request(),symbols=()),replace(request(),start="2025-12-31"),
              replace(request(),participation_pct=0),replace(request(),capital=-1)]:
        with pytest.raises(ValueError): run_comparison(root,r)
    with (root / BASELINE / "inputs.json").open("ab") as f: f.write(b" ")
    with pytest.raises(ValueError,match="hash mismatch"):
        run_comparison(root,request())


def test_persistent_result_can_be_reloaded_and_never_overwrites(root):
    result = run_comparison(root,replace(request(),compare=False))
    first,second = save_run(root,result),save_run(root,result)
    assert first != second
    saved = json.loads(first.read_text(encoding="utf-8"))
    assert saved["results"] == result["results"]
    assert len(saved["results"]) == 1


def test_page_runs_changes_capital_and_loads_saved_run(root,monkeypatch):
    monkeypatch.setenv("STOCK_SMILE_ROOT",str(root))
    page = Path(__file__).resolve().parents[1]/"src/bot/smile_dashboard.py"
    app = AppTest.from_file(str(page),default_timeout=20).run()
    assert not app.exception
    app.button[0].click().run()
    assert not app.exception
    assert len(app.metric) == 4
    first_file = app.session_state["result_file"]
    assert app.session_state["result"]["request"]["capital"] == 500000
    app.number_input(key="capital").set_value(100000).run()
    # Editing controls does not relabel existing results as a fresh run.
    assert app.session_state["result"]["request"]["capital"] == 500000
    next(b for b in app.button if b.label=="執行回測").click().run()
    assert not app.exception
    assert app.session_state["result"]["request"]["capital"] == 100000
    assert app.session_state["result_file"] != first_file
    reopened = AppTest.from_file(str(page),default_timeout=20).run()
    next(b for b in reopened.button if b.label=="載入紀錄").click().run()
    assert not reopened.exception
    assert len(reopened.metric) == 4


def test_page_fails_closed_for_missing_snapshot(tmp_path,monkeypatch):
    monkeypatch.setenv("STOCK_SMILE_ROOT",str(tmp_path))
    page=Path(__file__).resolve().parents[1]/"src/bot/smile_dashboard.py"
    app=AppTest.from_file(str(page)).run()
    assert not app.exception
    assert app.error
    assert not app.button
