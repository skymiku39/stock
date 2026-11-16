"""strategy_comparison_report -- 把 strategy_comparison 的結果寫成 Markdown 報告。純函式，不讀資料。"""

from __future__ import annotations

from typing import Optional

LABELS = {"smile": "微笑方案（改造後）", "swing": "波段原型"}
AXIS_LABELS = {"smile_take_profit_pct": "停利 %", "smile_symbol_stop_loss_pct": "單檔停損 %",
               "breakout_days": "突破天數", "trail_atr": "移動停利 ATR 倍數"}
REASONS = {"take_profit": "停利", "stop_loss": "停損", "trailing_stop": "移動停利",
           "expiry": "到期", "data_end": "資料中斷"}


def _num(value, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "無"
    return f"{value:,.{digits}f}{suffix}"


def _params(params: dict) -> str:
    return "、".join(f"{AXIS_LABELS.get(k, k)} {v:g}" for k, v in params.items())


def _table(header: list, rows: list) -> list:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return lines


def _metric_row(label: str, m: dict) -> list:
    return [label, m["round_count"], _num(m["win_rate"], 1, "%"), _num(m["expectancy_pct"], 2, "%"),
            f"{_num(m['hold_days_mean'], 0)}／{_num(m['hold_days_median'], 0)}／{m['hold_days_max']}",
            _num(m["annualized_return_pct"], 2, "%"), _num(m["max_drawdown_pct"], 1, "%"),
            _num(m["avg_capital_utilization_pct"], 0, "%"), _num(m["net_pnl_twd"], 0)]


METRIC_HEADER = ["", "輪數", "勝率", "每輪期望值", "持有天數 平均／中位／最長", "年化報酬", "最大回撤",
                 "平均資金使用率", "淨損益（元）"]


def _years(values: dict) -> str:
    return "、".join(f"{year} {_num(v, 0)}" for year, v in sorted(values.items())) or "無"


def _strategy_section(key: str, s: dict, validated: bool) -> list:
    lines = [f"### {LABELS[key]}", ""]
    rows = []
    for combo in s["combos"]:
        m = combo["train"]
        losing_years = [y for y, v in sorted(combo["train_yearly_pnl_twd"].items()) if v < 0]
        rows.append([("**選定** " if combo["chosen"] else "") + _params(combo["params"]),
                     m["round_count"], _num(m["win_rate"], 1, "%"), _num(m["expectancy_pct"], 2, "%"),
                     _num(m["hold_days_mean"], 0), _num(m["annualized_return_pct"], 2, "%"),
                     _num(m["max_drawdown_pct"], 1, "%"), _years(combo["train_yearly_pnl_twd"]),
                     "、".join(losing_years) or "無"])
    lines += ["參數決定段的全部組合：", ""]
    lines += _table(["組合", "輪數", "勝率", "每輪期望值", "平均持有天數", "年化報酬", "最大回撤",
                     "逐年淨損益（元）", "虧損年份"], rows)
    chosen = s["chosen"]
    lines += ["", f"選定：{_params(chosen['params'])}。"
              + ("它與所有相鄰組合在參數決定段的每輪期望值都為正。" if chosen["neighbours_all_positive"]
                 else "**沒有任何組合能讓自己與所有相鄰組合的期望值都為正**，這裡取期望值最高的一組，"
                      "依規則應視為碰巧，結論要打折。"), ""]
    if not validated:
        return lines
    v, verdict = s["validation"], s["verdict"]
    lines += ["驗證段（只跑一次）：", ""]
    lines += _table(METRIC_HEADER, [_metric_row("驗證段", v["metrics"])])
    reasons = "、".join(f"{REASONS.get(k, k)} {n}" for k, n in sorted(v["exit_reasons"].items())) or "無"
    lines += ["", f"- 出場原因：{reasons}。期末未平倉 {v['open_positions']} 檔。"
              + (f" 浮虧護欄在 {v['halted_date']} 觸發，之後不再買進。" if v.get("halted_date") else ""),
              f"- 逐年淨損益（元，含未實現）：{_years(v['yearly_pnl_twd'])}。",
              f"- 虧損的輪次：{v['losing_round_count']} 輪，合計 {_num(v['losing_rounds_pnl_twd'], 0)} 元。", ""]
    lines += ["通過標準逐項判定：", ""]
    rows = []
    for item in verdict["core"]:
        value = item["value"]
        if isinstance(value, dict) and "random_p95" in value:
            shown = f"策略 {_num(value['strategy'], 2, '%')}，隨機第 95 百分位 {_num(value['random_p95'], 2, '%')}"
        elif isinstance(value, dict):
            shown = _years(value)
        else:
            shown = _num(value, 2, "%")
        rows.append(["核心", item["name"], shown, "通過" if item["passed"] else "**未通過**"])
    for item in verdict["reference"]:
        rows.append(["參考", item["name"], _num(item["value"], 0 if isinstance(item["value"], int) else 1),
                     "符合" if item["met"] else item["note"]])
    lines += _table(["類別", "條件", "實際", "判定"], rows)
    dist = s["random_entry"]["distribution"]
    cal = s["random_entry"]["calibration"]
    lines += ["", "隨機進場基準（同樣的出場規則與部位大小，只把進場日改成隨機）：", ""]
    rows = []
    for name, label, digits, suffix in (("expectancy_pct", "每輪期望值", 2, "%"), ("round_count", "輪數", 0, ""),
                                        ("win_rate", "勝率", 1, "%"),
                                        ("annualized_return_pct", "年化報酬", 2, "%"),
                                        ("max_drawdown_pct", "最大回撤", 1, "%"),
                                        ("filled_buys", "成交買進次數", 0, "")):
        d = dist.get(name)
        if d:
            rows.append([label, _num(v["metrics"].get(name), digits, suffix), _num(d["mean"], digits, suffix),
                         _num(d["p5"], digits, suffix), _num(d["p50"], digits, suffix),
                         _num(d["p95"], digits, suffix)])
    lines += _table(["指標", "策略", "隨機平均", "第 5 百分位", "中位數", "第 95 百分位"], rows)
    lines += ["", f"進場次數校準：目標 {cal['target']} 次，試跑平均 {_num(cal['pilot_mean'], 1)} 次，"
              + ("在 10% 容許範圍內。" if cal["within_tolerance"] else "**未達 10% 容許範圍**，比較時要留意。"), ""]
    lines += ["最差的五檔（依已平倉損益）：", ""]
    lines += _table(["代號", "輪數", "損益（元）"],
                    [[r["symbol"], r["rounds"], _num(r["pnl_twd"], 0)] for r in v["worst_symbols"]])
    lines += ["", "虧損最大的輪次：", ""]
    lines += _table(["代號", "進場", "出場", "持有天數", "原因", "損益（元）", "報酬率"],
                    [[r["symbol"], r["entry_date"], r["exit_date"], r["hold_days"],
                      REASONS.get(r["exit_reason"], r["exit_reason"]), _num(r["pnl_twd"], 0),
                      _num(r["return_pct"], 1, "%")] for r in v["worst_rounds"]])
    lines.append("")
    return lines


def _conclusion(result: dict) -> list:
    lines = []
    for key in ("smile", "swing"):
        s = result["strategies"][key]
        verdict = s["verdict"]
        failed = [c["name"] for c in verdict["core"] if not c["passed"]]
        notes = [r["note"] for r in verdict["reference"] if not r["met"]]
        if verdict["has_evidence"]:
            text = f"**{LABELS[key]}：核心條件全部通過，有證據。**"
        else:
            text = f"**{LABELS[key]}：沒有證據。** 未通過：{'；'.join(failed)}。"
        if not s["chosen"]["neighbours_all_positive"]:
            notes.append("選定參數的相鄰組合未全數為正")
        if notes:
            text += f" 另外：{'；'.join(notes)}。"
        lines.append(f"- {text}")
    return lines


def render(result: dict) -> str:
    validated = result["validated"]
    strategies = result["strategies"]
    lines = ["# 微笑方案與波段原型的比較報告", "",
             f"對應 issue #19。標的 {len(result['symbols'])} 檔（其中 {result['symbols_with_data']} 檔有日 K），"
             f"參數決定段 {result['train']['start']}～{result['train']['end']}，"
             f"驗證段 {result['validation']['start']}～{result['validation']['end']}。"
             f"起始資金 {result['initial_cash']:,} 元，零股，兩種策略共用同一個現金帳戶與成交假設。", ""]
    if not validated:
        lines += ["> 這份輸出只跑了參數決定段，驗證段尚未執行。", ""]
    else:
        lines += ["## 結論", ""] + _conclusion(result) + [
            "", "核心條件未通過就是沒有證據，不調參數重跑。驗證段只跑了這一次；"
            "之後若改參數再看這段資料，這段就不能再當驗證用。", ""]
        lines += ["## 驗證段並列", ""]
        lines += _table(METRIC_HEADER, [
            _metric_row(LABELS[key], strategies[key]["validation"]["metrics"]) for key in ("smile", "swing")])
        rows = []
        for key in ("smile", "swing"):
            d = strategies[key]["random_entry"]["distribution"].get("expectancy_pct")
            rows.append([f"{LABELS[key]}的隨機進場", _num(d["mean"], 2, "%") if d else "無",
                         _num(d["p5"], 2, "%") if d else "無", _num(d["p95"], 2, "%") if d else "無"])
        lines += ["", "隨機進場基準的每輪期望值（各自沿用該策略的出場規則）：", ""]
        lines += _table(["", "平均", "第 5 百分位", "第 95 百分位"], rows)
        lines += ["", "現金基準的報酬為 0。不使用買進持有，也不使用 2330。", ""]
    lines += ["## 做法", "",
              "- 標的：`config/backtest_universe.json`，以 2020 年全年日均成交金額排名取 60 檔，排除 2330 與非普通股，選定後不換。",
              "- 成交假設：收盤產生訊號，次日開盤成交；每邊滑價 10 bps；單筆不超過前一日成交量的 0.1%；"
              "漲停不買、跌停不賣；手續費 0.1425% 打 2.8 折（最低 1 元）、證交稅 0.3%。",
              "- 除權息：配發在除權息日入帳，成本、標準價與停損價同步調整。",
              "- 資料中斷的標的：最後一根日 K 的下一個交易日以最後收盤價強制平倉。",
              f"- 微笑方案固定值：階梯 `{result['smile_fixed']['smile_buy_tiers']}`、不做最深階每日加碼、"
              f"持有期限 {result['smile_fixed']['smile_max_hold_days']} 天、認賠後冷卻 "
              f"{result['smile_fixed']['smile_reentry_cooldown_days']} 天、每階金額為權益的 "
              f"{result['smile_fixed']['smile_unit_equity_pct']}% × 倍數、單檔上限為權益的 "
              f"{result['smile_fixed']['smile_max_symbol_fund_pct']:.0f}%、全部持倉浮虧達 "
              f"{result['smile_fixed']['smile_max_unrealized_loss_pct']:.0f}% 即停止買進。比較停利與單檔停損。",
              f"- 波段原型固定值：均線 {result['swing_defaults']['fast_ma']}／{result['swing_defaults']['slow_ma']} 日、"
              f"ATR {result['swing_defaults']['atr_days']} 日、初始停損 {result['swing_defaults']['initial_stop_atr']:g} 倍 ATR、"
              f"持有期限 {result['swing_defaults']['max_hold_days']} 天、每筆承擔權益 "
              f"{result['swing_defaults']['risk_pct']:g}% 的風險、單檔上限 {result['swing_defaults']['max_position_pct']:.0f}%、"
              f"最多 {result['swing_defaults']['max_positions']} 檔。比較突破天數與移動停利倍數。",
              "- 選定規則（事先訂好）：參數決定段中，自己與所有相鄰組合的每輪期望值都為正的組合裡，取期望值最高者。",
              f"- 隨機進場重複 {result['random_runs']} 次、重抽樣 {result['bootstrap_runs']:,} 次，亂數種子 {result['seed']}。",
              "- 通過標準見 `docs/swing-strategy-design.md` 四之 5。", ""]
    lines += ["## 各策略明細", ""]
    for key in ("smile", "swing"):
        lines += _strategy_section(key, strategies[key], validated)
    lines += ["## 限制", ""]
    ended = strategies["smile"].get("validation", {}).get("data_ended", []) if validated else []
    missing = result["symbols_without_data"]
    lines += [
        "- 存活者偏誤：名單用 2020 年的成交金額選，但已下市而抓不到資料的股票不在名單內，這個偏誤無法完全消除。"
        + (f"名單中 {'、'.join(missing)} 在兩段期間都沒有日 K。" if missing else ""),
        "- 資料中斷：2448（最後一根日 K 2020-12-23）與 2456（2021-12-28）都在參數決定段中斷，"
        "回測以最後收盤價強制平倉；兩檔都是合併下市，這個處理大致合理，對財務問題下市的股票會偏樂觀。"
        + (f"驗證段另有資料中斷：{'、'.join(d['symbol'] for d in ended)}。" if ended else "驗證段沒有資料中斷的標的。"),
        "- 參數決定段的資料從 2020-01-02 開始，波段原型前約 60 個交易日用來暖機指標，所以它在這一段比微笑方案晚約三個月開始交易。"
        "驗證段則用 2023 下半年的資料暖機，兩者同時開始。",
        "- 股利實際晚數週入帳、股利稅與二代健保補充保費都沒有計入；配股一律視為以參考價折現的現金。",
        "- 漲跌停用前一日收盤價推算，不是交易所公布的參考價；成交量上限用前一日成交量，無法反映當日實際流動性。",
        "- 微笑方案的盤中策略與這裡的日 K 回測規則一致，但盤中是即時成交，回測一律次日開盤成交，數字不能直接互換。",
        "- 波段原型只有回測，沒有盤中下單實作。",
        "- 兩種策略各比較 9 組參數並從中選一組，參數決定段的數字因此偏樂觀；能當證據的只有驗證段。",
        "", "## 重現", "", "```bash",
        "uv run --offline --no-sync python -m bot.strategy_comparison --output docs/reports/strategy-comparison.md --json work/strategy-comparison/result.json",
        "```", "",
        "同一份資料庫與同一個指令會得到相同的結果。資料庫的日 K 之後若有增補或修正，數字會跟著變。", ""]
    return "\n".join(lines)
