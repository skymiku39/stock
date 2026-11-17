"""前瞻追蹤：把 #19 選定的兩組參數凍結，只用驗證段之後的新行情記帳。只讀資料庫、只跑回測。

為什麼需要：#19 的驗證段（2024-01-01～2026-10-31）已經用過一次，之後再看那段資料就不算驗證。
新的證據只能來自之後的行情，所以從 2026-11-01 起，用同一份名單、同一組成交假設與選定參數，
每個交易日重算一次、把結果存下來。參數寫死在這裡，改了就不是同一份追蹤。

    python -m bot.forward_tracker --root C:\\skymiku\\stock [--force]

結果在 data/forward_tracking/：latest.json、latest.md，以及每個資料日一份 <日期>.json。

限制：
- 兩種策略都從 FORWARD_START 以 50 萬的新帳戶開始，不接續驗證段結束時的部位。
- 初期樣本很少，報告會標示「樣本不足」；判定是否有證據仍用 docs/swing-strategy-design.md 四之 5。
- 還沒有隨機進場基準（樣本夠了再跑才有意義）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from bot.runtime_state import atomic_json, atomic_text, read_json
from bot.utils import now_tw

FORWARD_START = "2026-11-01"          # #19 驗證段結束的隔天
SWING_WARMUP_START = "2026-05-01"     # 只用來暖機波段指標，不產生訊號
# #19 依事先訂好的規則選定的參數（docs/reports/strategy-comparison.md）。
FROZEN = {
    "smile": {"smile_take_profit_pct": 0.0, "smile_symbol_stop_loss_pct": 15.0},
    "swing": {"breakout_days": 20, "trail_atr": 4.0},
}
LABELS = {"smile": "微笑方案", "swing": "波段原型"}
MIN_COVERAGE = 0.9                    # 名單中至少這個比例的標的有當日日 K，才算這一天可以記帳
OUTPUT_REL = "data/forward_tracking"


def _open_positions(summary) -> list:
    if hasattr(summary, "results"):       # 微笑方案
        return [dict(symbol=r.symbol, lots=r.open_lots, shares=getattr(r, "open_shares", None),
                     cost_twd=getattr(r, "open_cost_twd", None),
                     unrealized_pnl_twd=getattr(r, "unrealized_pnl_twd", None))
                for r in summary.results if r.open_lots > 0]
    out = []
    for p in summary.open_positions:
        row = p if isinstance(p, dict) else getattr(p, "__dict__", {})
        out.append({k: row.get(k) for k in ("symbol", "entry_date", "entry_price", "shares", "paid_twd", "stop")})
    return out


def _strategy_result(summary, params: dict) -> dict:
    from bot.strategy_comparison import describe
    described = describe(summary, year_from=FORWARD_START)
    curve = [p for p in summary.equity_curve if p["date"] >= FORWARD_START]
    return dict(params=params, metrics=described["metrics"], exit_reasons=described["exit_reasons"],
                rounds=[r.to_dict() for r in summary.rounds], open_positions=_open_positions(summary),
                equity=curve[-1]["equity"] if curve else summary.initial_cash,
                equity_curve=[dict(date=p["date"], equity=p["equity"]) for p in curve],
                data_ended=described["data_ended"], halted_date=described["halted_date"])


def run_forward(root: Path, *, end: str = "") -> dict:
    from bot.backtest_universe import load_universe
    from bot.stock_db import StockDB, default_db_path
    from bot.strategy_comparison import (EXECUTION, INITIAL_CASH, REFERENCE_ROUNDS, _smile_backtester,
                                         _swing_backtester)
    from bot.swing_backtest import load_histories

    root = Path(root)
    symbols = load_universe(root / "config" / "backtest_universe.json")
    end = end or now_tw().date().isoformat()
    db = StockDB.open(path=default_db_path(root))
    try:
        forward, forward_events = load_histories(db, symbols, start=FORWARD_START, end=end)
        warm, warm_events = load_histories(db, symbols, start=SWING_WARMUP_START, end=end)
        last_event = db.conn.execute("SELECT MAX(ex_date), MAX(retrieved_at) FROM ex_rights_events").fetchone()
    finally:
        db.close()
    dates = sorted({bar.date[:10] for bars in forward.values() for bar in bars})
    as_of = dates[-1] if dates else ""
    covered = sum(1 for bars in forward.values() if bars and bars[-1].date[:10] == as_of)
    result = dict(
        forward_start=FORWARD_START, as_of=as_of, trading_days=len(dates),
        generated_at=now_tw().isoformat(timespec="seconds"), initial_cash=INITIAL_CASH,
        execution=EXECUTION, symbols=len(symbols), symbols_at_as_of=covered,
        coverage_ok=bool(symbols) and covered / len(symbols) >= MIN_COVERAGE,
        ex_rights_last_event=last_event[0], ex_rights_retrieved_at=last_event[1],
        reference_rounds=REFERENCE_ROUNDS, strategies={})
    if not dates:
        return result
    smile = _smile_backtester(FROZEN["smile"]).run_histories(
        forward, start=FORWARD_START, end=end, ex_rights=forward_events)
    swing = _swing_backtester(FROZEN["swing"]).run_histories(
        warm, trade_start=FORWARD_START, ex_rights=warm_events)
    result["strategies"] = dict(smile=_strategy_result(smile, FROZEN["smile"]),
                                swing=_strategy_result(swing, FROZEN["swing"]))
    return result


def render(result: dict) -> str:
    lines = ["# 前瞻追蹤（凍結參數，只看驗證段之後的行情）", "",
             f"- 期間：{result['forward_start']}～{result['as_of'] or '尚無資料'}（{result['trading_days']} 個交易日）",
             f"- 名單：`config/backtest_universe.json` {result['symbols']} 檔，其中 {result['symbols_at_as_of']} 檔有最新一天的日 K"
             + ("" if result["coverage_ok"] else "（**不足，這一天的結果不完整**）"),
             f"- 起始資金：{result['initial_cash']:,} 元；成交假設與 #19 相同（{result['execution']}）",
             f"- 除權息資料最後一筆事件 {result['ex_rights_last_event']}，取得時間 {result['ex_rights_retrieved_at']}",
             f"- 產生時間：{result['generated_at']}", ""]
    for key, data in result["strategies"].items():
        m = data["metrics"]
        enough = m["round_count"] >= result["reference_rounds"]
        lines += [f"## {LABELS.get(key, key)}", "", f"參數：`{data['params']}`", "",
                  "| 項目 | 數值 |", "|---|---|",
                  f"| 權益 | {data['equity']:,.0f} 元（報酬 {m['return_pct']:.2f}%） |",
                  f"| 已平倉輪數 | {m['round_count']}（參考值 {result['reference_rounds']}"
                  f"{'' if enough else '；**樣本不足，不能當證據**'}） |",
                  f"| 每輪期望值 | {m['expectancy_pct']:.2f}% |",
                  f"| 勝率 | {m.get('win_rate', 0):.1f}% |",
                  f"| 最大回撤 | {m['max_drawdown_pct']:.2f}% |",
                  f"| 成交買進次數 | {m['filled_buys']} |",
                  f"| 未平倉部位 | {len(data['open_positions'])} 檔 |", ""]
        if data["open_positions"]:
            lines.append("未平倉：" + "、".join(str(p.get("symbol")) for p in data["open_positions"]))
            lines.append("")
        if data["rounds"]:
            lines += ["| 標的 | 進場 | 出場 | 損益（元） | 報酬 | 原因 |", "|---|---|---|---|---|---|"]
            lines += [f"| {r.get('symbol')} | {r.get('entry_date', '')} | {r.get('exit_date', '')} | "
                      f"{r.get('pnl_twd', 0):,.0f} | {r.get('return_pct', 0):.2f}% | {r.get('exit_reason', '')} |"
                      for r in data["rounds"]]
            lines.append("")
    if not result["strategies"]:
        lines.append("還沒有 FORWARD_START 之後的日 K。")
    lines += ["## 怎麼讀", "",
              "- 這是樣本外的實際走法，不是調參數的依據；看到結果後改參數，這段就不再是樣本外。",
              "- 輪數少的時候每輪期望值與勝率沒有意義，先看有沒有照規則進出場、有沒有異常。",
              "- 兩種策略都從期間起點開新帳戶，數字不能和 #19 驗證段的帳戶直接相加。", ""]
    return "\n".join(lines)


def update(root: Path, *, force: bool = False, end: str = "") -> dict:
    """資料日有前進（且名單涵蓋率足夠）才重算；回傳 latest.json 的內容。"""
    root = Path(root)
    out = root / OUTPUT_REL
    latest = read_json(out / "latest.json")
    result = run_forward(root, end=end)
    # 比較除了產生時間以外的全部內容：同一天的日 K 事後被修正（例如重抓、除權息事件補進來）
    # 也要更新，不能只看資料日與檔數。
    # 經過 JSON 來回再比，避免 tuple／list 之類的型別差異被當成內容改變。
    comparable = json.loads(json.dumps(result, ensure_ascii=False))
    unchanged = {k: v for k, v in latest.items() if k != "generated_at"} ==         {k: v for k, v in comparable.items() if k != "generated_at"}
    if not force and unchanged:
        return latest
    if not result["coverage_ok"] and not force:
        return latest or result
    atomic_json(out / "latest.json", result)
    if result["as_of"]:
        atomic_json(out / f"{result['as_of']}.json", result)
    atomic_text(out / "latest.md", render(result))
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    from bot.cli_encoding import configure_console
    configure_console()
    parser = argparse.ArgumentParser(description="凍結參數的前瞻追蹤（只讀資料庫）")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--force", action="store_true", help="資料日沒有前進或涵蓋率不足也重算")
    parser.add_argument("--end", default="", help="只算到這一天（預設今天）")
    args = parser.parse_args(argv)
    result = update(args.root.resolve(), force=args.force, end=args.end)
    print(render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
