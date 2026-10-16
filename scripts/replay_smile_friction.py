"""Replay the exact previous input snapshot with execution-friction scenarios."""
import argparse
from collections import Counter
import json
from pathlib import Path

from bot.config import Settings
from bot.smile_curve_backtest import SmileCurveBacktester
from bot.smile_execution import SmileExecution


from bot.smile_snapshot import load_snapshot


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root",type=Path,default=Path.cwd())
    p.add_argument("--baseline",type=Path,default=Path("data/reports/smile_execution_20261016"))
    p.add_argument("--output",type=Path,required=True)
    args = p.parse_args()
    root = args.root.resolve()
    baseline = args.baseline if args.baseline.is_absolute() else root/args.baseline
    old,histories = load_snapshot(root,baseline)
    scenarios = {"ideal":SmileExecution.ideal(),
                 "slippage_only":SmileExecution(10,1,False,True,False),
                 "base":SmileExecution(10,.001,True,True),
                 "stress":SmileExecution(30,.0001,True,True)}
    cases = []
    for case in old["cases"]:
        params = case["parameters"]
        settings = Settings(_env_file=None,max_fund=params["fund"],daily_fund_budget=0,
                            smile_buy_tiers=params["tiers"],smile_base_lot=params["base_lot"],
                            max_lot_per_symbol=params["max_lot_per_symbol"],use_odd_lot=params["odd_lot"],
                            smile_reference_prices={},broker_fee_discount=params["fee_discount"],
                            broker_min_fee=params["min_fee"],smile_regular_tax_rate=params["tax"])
        selected = {s:[b for b in histories[s] if coverage["start"]<=b.date<=coverage["end"]]
                    for s,coverage in case["coverage"].items()}
        results = {}
        for name,execution in scenarios.items():
            summary = SmileCurveBacktester(settings,execution=execution).run_histories(selected)
            result = summary.to_dict()
            result["cancel_reasons"] = dict(Counter(o.get("reason", "unknown") for o in summary.orders if o["status"]=="cancelled"))
            results[name] = result
            print(case["label"],name, f"net={summary.net_pnl_twd:.2f} return={summary.return_pct:.2f}% MDD={summary.max_drawdown_pct:.2f}%", result["cancel_reasons"])
        for metric in ("net_pnl_twd","max_drawdown_pct","realized_pnl_twd","unrealized_pnl_twd"):
            if abs(results["ideal"][metric]-case["next_open_shared"][metric]) > 1e-7:
                raise ValueError(f"baseline regression: {case['label']} {metric}")
        cases.append(dict(label=case["label"],parameters=params,coverage=case["coverage"],results=results))
    args.output.mkdir(parents=True,exist_ok=True)
    payload = dict(input_sha256=old["input_sha256"],baseline=str(baseline),cases=cases,
                   assumptions=["Ordinary TW cash equities only; prior close is a proxy for official limit reference",
                                "Participation uses prior session total shares, not opening-auction depth",
                                "No same-day OHLC extremes or volume used to determine opening fills",
                                "Buy partial fills consume tier; sell whole entry lot or cancel; next close may resignal",
                                "Unrealized PnL includes adverse exit slippage and fees; not guaranteed liquidation",
                                "No dividends/corporate-action adjustment; daily-close drawdown, not intraday"])
    (args.output/"comparison.json").write_text(json.dumps(payload,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")


if __name__ == "__main__":
    main()
