"""Reproduce legacy vs next-open results from hash-verified local TWSE rows.

Run with uv run python scripts/compare_smile_execution.py --output <directory>.
Reads SQLite in read-only mode; never fetches or modifies canonical market data.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from bot.config import Settings
from bot.smile_execution import SmileExecution
from bot.smile_curve import SmileCurveEngine
from bot.smile_curve_backtest import SmileCurveBacktester
from bot.stock_db import PriceBar
from bot.trade_cost import net_pnl_twd


def verified_histories(root, symbols):
    conn = sqlite3.connect((root / "data/stock.db").as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    histories, rejected, documents = {}, [], {}
    try:
        conn.execute("BEGIN")  # consistent read snapshot across all symbols
        for symbol in symbols:
            histories[symbol] = []
            rows = conn.execute("SELECT * FROM price_history WHERE symbol=? ORDER BY date", (symbol,))
            for row in rows:
                try:
                    sid = row["source_record_id"]
                    if row["quality_status"] != "ok" or row["source"] != "twse_stock_day":
                        raise ValueError("unsupported_or_unverified_row")
                    if sid not in documents:
                        doc = conn.execute("SELECT * FROM source_documents WHERE document_id=?", (sid,)).fetchone()
                        if not doc or doc["quality_status"] != "ok" or doc["source"] != "twse_stock_day":
                            raise ValueError("missing_verified_document")
                        path = (root / doc["content_path"]).resolve()
                        if not path.is_relative_to(root):
                            raise ValueError("raw_path_outside_root")
                        raw = path.read_bytes()
                        if hashlib.sha256(raw).hexdigest() != doc["content_hash"]:
                            raise ValueError("raw_hash_mismatch")
                        payload = json.loads(raw)
                        if payload.get("stat") != "OK":
                            raise ValueError("invalid_raw_response")
                        documents[sid] = (dict(doc), payload)
                    doc, payload = documents[sid]
                    if doc["source_ref"] != f"{symbol}:{row['date'][:7]}":
                        raise ValueError("source_identity_mismatch")
                    if (symbol not in str(payload.get("title", "")).split()
                            or str(payload.get("date", ""))[:6] != row["date"][:7].replace("-", "")):
                        raise ValueError("raw_identity_mismatch")
                    fields = payload["fields"]
                    y,m,d = row["date"][:10].split("-")
                    roc = f"{int(y)-1911}/{m}/{d}"
                    matches = [dict(zip(fields, item)) for item in payload["data"]
                               if item[0] == roc]
                    if len(matches) != 1:
                        raise ValueError("raw_date_not_unique")
                    raw_row = matches[0]
                    for key, name in (("open","開盤價"),("high","最高價"),("low","最低價"),("close","收盤價")):
                        value = float(str(raw_row[name]).replace(",", ""))
                        if not math.isfinite(value) or value <= 0 or value != row[key]:
                            raise ValueError("price_not_exact_or_invalid")
                    if not row["low"] <= min(row["open"],row["close"]) <= max(row["open"],row["close"]) <= row["high"]:
                        raise ValueError("inconsistent_ohlc")
                    if "**" in str(raw_row.get("註記", "")):
                        raise ValueError("unhandled_corporate_action")
                    histories[symbol].append(PriceBar(**{k:row[k] for k in PriceBar.__dataclass_fields__}))
                except (ValueError, KeyError, OSError, TypeError) as exc:
                    rejected.append(dict(symbol=symbol,date=row["date"],reason=str(exc)))
    finally:
        conn.close()
    return histories, rejected, {sid:dict(path=d[0]["content_path"],sha256=d[0]["content_hash"])
                                  for sid,d in documents.items()}


def legacy(histories, settings, mode):
    results = []
    curve = []
    for sym, bars in histories.items():
        e = SmileCurveEngine(sym, settings)
        peak = float(settings.effective_fund_cap())
        max_dd = 0.0
        for b in bars:
            if mode == "ohlc":
                e.on_bar_ohlc(b.date,b.open,b.high,b.low,b.close)
            else:
                e.on_bar(b.date,b.close)
            unrealized = sum(net_pnl_twd(l.entry_price,b.close,l.quantity,l.unit,
                                        settings=settings,sell_tax_rate=e.sell_tax) for l in e.open_lots)
            equity = settings.effective_fund_cap()+e.realized_pnl_twd+unrealized
            peak = max(peak,equity)
            max_dd = max(max_dd,100*(peak-equity)/peak)
            curve.append(dict(symbol=sym,date=b.date,equity=equity))
        results.append(dict(symbol=sym,realized_pnl_twd=e.realized_pnl_twd,
                            unrealized_pnl_twd=unrealized if bars else 0,
                            net_pnl_twd=e.realized_pnl_twd+(unrealized if bars else 0),
                            max_drawdown_pct=max_dd, trade_count=len(e.round_trips),
                            open_lots=len(e.open_lots)))
    return dict(mode=mode,capital_per_symbol=settings.effective_fund_cap(),
                total_capital=settings.effective_fund_cap()*len(histories),
                results=results,equity_curve=curve,
                warning="Legacy independent accounts; not a shared-capital portfolio; biased fills")


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root",type=Path,default=Path.cwd())
    p.add_argument("--output",type=Path,required=True)
    args=p.parse_args()
    root=args.root.resolve()
    histories,rejected,documents=verified_histories(root,["2303","2330"])
    if rejected or any(not rows for rows in histories.values()):
        args.output.mkdir(parents=True,exist_ok=True)
        (args.output/"rejected.json").write_text(json.dumps(rejected,ensure_ascii=False,indent=2),encoding="utf-8")
        raise SystemExit("Input rejected/missing; do not silently bridge excluded daily bars")
    common_start=max(rows[0].date for rows in histories.values())
    common_end=min(rows[-1].date for rows in histories.values())
    common={s:[b for b in rows if common_start<=b.date<=common_end] for s,rows in histories.items()}
    cases=[]
    for label,hist,fund in [("common_500k",common,500000),("common_3m",common,3000000),
                            ("2330_long_500k",{"2330":histories["2330"]},500000)]:
        settings=Settings(_env_file=None,max_fund=fund,daily_fund_budget=0,
                          smile_buy_tiers="1:1,3:2,5:3,8:4",smile_base_lot=1,
                          smile_reference_prices={},use_odd_lot=False,max_lot_per_symbol=2,
                          broker_fee_discount=0.28,broker_min_fee=1,smile_regular_tax_rate=0.003)
        cases.append(dict(label=label,parameters=dict(fund=fund,tiers=settings.smile_buy_tiers,
                          base_lot=1,max_lot_per_symbol=2,fee_discount=0.28,min_fee=1,tax=0.003,
                          reference="first_close_then_reset",odd_lot=False),
                          coverage={s:dict(start=b[0].date,end=b[-1].date,count=len(b)) for s,b in hist.items()},
                          legacy_close=legacy(hist,settings,"close"),legacy_ohlc=legacy(hist,settings,"ohlc"),
                          next_open_independent={s:SmileCurveBacktester(settings, execution=SmileExecution.ideal()).run_histories({s:b}).to_dict()
                                                 for s,b in hist.items()},
                          next_open_shared=SmileCurveBacktester(settings, execution=SmileExecution.ideal()).run_histories(hist).to_dict()))
    inputs={s:[asdict(b) for b in rows] for s,rows in histories.items()}
    encoded=json.dumps(inputs,ensure_ascii=False,sort_keys=True).encode("utf-8")
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/"inputs.json").write_bytes(encoded)
    report=dict(input_sha256=hashlib.sha256(encoded).hexdigest(),documents=documents,
                rejected=rejected,cases=cases,
                limitations=["Local archived TWSE responses verified by hash and exact OHLC; not live exchange revalidation",
                             "Daily open executable price assumed; no slippage, volume, limit-lock or auction fill model",
                             "Cash dividends/adjustments not modeled; price-only backtest, not total return",
                             "Daily close net liquidation drawdown; intraday drawdown can be worse",
                             "Fixed symbol priority; no optimized symbol/parameter selection"])
    (args.output/"comparison.json").write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
    for case in cases:
        r=case["next_open_shared"]
        print(case["label"],json.dumps({k:r[k] for k in ("realized_pnl_twd","unrealized_pnl_twd","net_pnl_twd","return_pct","max_drawdown_pct","trade_count")},ensure_ascii=False))


if __name__ == "__main__":
    main()
