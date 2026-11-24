"""ETF 關注名單：從主動式與大型被動式 ETF 的持股，收斂出比較需要關注的股票。

    python -m bot.etf_focus            # 重算並寫 data/etf_focus/latest.md、latest.json
    python -m bot.etf_focus --print    # 另外把報告印出來

做法：
- 只看以台股為主的 ETF（台股權重合計 ≥ TW_MIN_WEIGHT_PCT）。
- 主動式與被動式分開計數。主動式的持股是經理人的判斷；被動式照指數規則選股，
  只當「有沒有被主流指數納入」的背景資訊，不和主動式的家數相加排序。
- 加減碼用股數比較，並扣掉整檔 ETF 的規模變化：ETF 被申購時所有持股的股數都會等比例
  增加，那不是加碼。規模變化取「兩期都持有的股票，股數比值的中位數」。
- 比較基準是至少 LOOKBACK_DAYS 天前的最近一期持股；沒有那麼舊的就用最舊的一期。

這是關注名單，不是買賣訊號：持股快照從 2026-09 才開始累積，沒有足夠的歷史可以驗證
「多數 ETF 持有或加碼的股票之後表現較好」。門檻都是暫定值，寫在檔案開頭。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import statistics
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from bot.active_etf import (ActiveEtf, HoldingsSnapshot, list_holdings_dates, load_holdings,
                            load_tracked_etfs)
from bot.runtime_state import atomic_json, atomic_text
from bot.utils import configure_utf8_stdio, now_tw

TW_MIN_WEIGHT_PCT = 50.0   # 台股權重合計低於這個比例的 ETF 不列入
LOOKBACK_DAYS = 28         # 加減碼的比較期間（日曆天）
CHANGE_BAND = 0.05         # 扣掉規模變化後，股數變動超過 ±5% 才算加碼或減碼
MIN_COMMON_FOR_SCALE = 5   # 兩期共同持股少於這個數目就不估規模變化（視為 1）
CORE_SHARE = 0.5           # 主動式有一半以上持有 → 共識核心
NET_SHARE = 0.2            # 主動式淨加碼（或淨減碼）家數達這個比例 → 主動加碼（減碼）
NET_MIN = 3                # 且至少這麼多家
_TW_STOCK = re.compile(r"^\d{4}$")

TAG_CORE, TAG_ADD, TAG_REDUCE = "共識核心", "主動加碼", "主動減碼"
TAG_INDEX_IN, TAG_INDEX_OUT = "被動納入", "被動剔除"
FOCUS_TAGS = (TAG_CORE, TAG_ADD, TAG_INDEX_IN)
CAUTION_TAGS = (TAG_REDUCE, TAG_INDEX_OUT)


def tw_holdings(snapshot: Optional[HoldingsSnapshot]) -> Dict[str, object]:
    if snapshot is None:
        return {}
    return {h.ticker.strip(): h for h in snapshot.holdings if _TW_STOCK.match(h.ticker.strip())}


def scale_factor(old: Dict[str, object], new: Dict[str, object]) -> float:
    """整檔 ETF 的規模變化倍數（申購贖回造成的等比例股數變動）。"""
    ratios = [new[t].shares / old[t].shares for t in old.keys() & new.keys()
              if old[t].shares > 0 and new[t].shares > 0]
    if len(ratios) < MIN_COMMON_FOR_SCALE:
        return 1.0
    return statistics.median(ratios)


def classify(old, new, scale: float) -> str:
    """單一 ETF 對單一股票的動作：new／exit／add／reduce／hold。"""
    if old is None:
        return "new"
    if new is None:
        return "exit"
    if old.shares <= 0 or new.shares <= 0:
        return "hold"  # 來源沒有股數，無法判斷
    ratio = new.shares / (old.shares * scale)
    if ratio > 1 + CHANGE_BAND:
        return "add"
    if ratio < 1 - CHANGE_BAND:
        return "reduce"
    return "hold"


def pick_base_date(dates: List[dt.date], latest: dt.date, lookback_days: int) -> Optional[dt.date]:
    older = [d for d in dates if d < latest]
    if not older:
        return None
    target = latest - dt.timedelta(days=lookback_days)
    far_enough = [d for d in older if d <= target]
    return max(far_enough) if far_enough else min(older)


def build_focus(root: Path, *, etfs: Optional[Iterable[ActiveEtf]] = None,
                lookback_days: int = LOOKBACK_DAYS) -> dict:
    root = Path(root)
    etfs = list(load_tracked_etfs(root) if etfs is None else etfs)
    used, skipped = [], []
    rows: Dict[str, dict] = {}

    def row(ticker: str, name: str) -> dict:
        item = rows.setdefault(ticker, {
            "ticker": ticker, "name": name, "active_holders": [], "passive_holders": [],
            "active_weight_sum": 0.0,
            "active": {"new": [], "add": [], "reduce": [], "exit": []},
            "passive": {"new": [], "exit": []}})
        if name and not item["name"]:
            item["name"] = name
        return item

    for etf in etfs:
        dates = list_holdings_dates(etf.symbol, root)
        if not dates:
            skipped.append({"symbol": etf.symbol, "reason": "沒有持股資料"})
            continue
        latest_snapshot = load_holdings(etf.symbol, dates[0], root)
        new = tw_holdings(latest_snapshot)
        tw_weight = sum(h.weight_pct for h in new.values())
        if tw_weight < TW_MIN_WEIGHT_PCT:
            skipped.append({"symbol": etf.symbol,
                            "reason": f"台股權重 {tw_weight:.0f}% 低於 {TW_MIN_WEIGHT_PCT:.0f}%"})
            continue
        kind = "passive" if getattr(etf, "kind", "active") == "passive" else "active"
        base_date = pick_base_date(dates, dates[0], lookback_days)
        old = tw_holdings(load_holdings(etf.symbol, base_date, root)) if base_date else {}
        scale = scale_factor(old, new) if base_date else 1.0
        used.append({"symbol": etf.symbol, "name": etf.name, "kind": kind,
                     "latest": dates[0].isoformat(),
                     "base": base_date.isoformat() if base_date else None,
                     "scale": round(scale, 4), "holdings": len(new)})
        for ticker, holding in new.items():
            item = row(ticker, holding.name)
            item[f"{kind}_holders"].append(etf.symbol)
            if kind == "active":
                item["active_weight_sum"] += holding.weight_pct
        if not base_date:
            continue
        for ticker in old.keys() | new.keys():
            action = classify(old.get(ticker), new.get(ticker), scale)
            if action == "hold" or (kind == "passive" and action in ("add", "reduce")):
                continue  # 被動式的股數調整只是跟著指數權重，不當訊號
            holding = new.get(ticker) or old.get(ticker)
            row(ticker, holding.name)[kind][action].append(etf.symbol)

    active_total = sum(1 for e in used if e["kind"] == "active")
    passive_total = len(used) - active_total
    net_threshold = max(NET_MIN, int(round(active_total * NET_SHARE)))
    result_rows = []
    for item in rows.values():
        active = item["active"]
        net = len(active["new"]) + len(active["add"]) - len(active["reduce"]) - len(active["exit"])
        tags = []
        if active_total and len(item["active_holders"]) >= active_total * CORE_SHARE:
            tags.append(TAG_CORE)
        if net >= net_threshold:
            tags.append(TAG_ADD)
        if net <= -net_threshold:
            tags.append(TAG_REDUCE)
        if item["passive"]["new"]:
            tags.append(TAG_INDEX_IN)
        if item["passive"]["exit"]:
            tags.append(TAG_INDEX_OUT)
        weight_sum = item.pop("active_weight_sum")
        result_rows.append({
            **item,
            "active_count": len(item["active_holders"]),
            "passive_count": len(item["passive_holders"]),
            # 對全部主動式 ETF 平均（沒持有的算 0），才能跨股票比較
            "active_avg_weight": round(weight_sum / active_total, 3) if active_total else 0.0,
            "active_net": net, "tags": tags})
    result_rows.sort(key=lambda r: (-r["active_count"], -r["active_net"], -r["passive_count"],
                                    r["ticker"]))
    return {
        "generated_at": now_tw().isoformat(timespec="seconds"),
        "as_of": max((e["latest"] for e in used), default=None),
        "lookback_days": lookback_days, "active_total": active_total,
        "passive_total": passive_total, "net_threshold": net_threshold,
        "etfs": used, "skipped": skipped, "stocks": result_rows,
        "focus": [r["ticker"] for r in result_rows if any(t in FOCUS_TAGS for t in r["tags"])],
        "caution": [r["ticker"] for r in result_rows if any(t in CAUTION_TAGS for t in r["tags"])],
    }


def _line(r: dict, active_total: int, passive_total: int) -> str:
    a = r["active"]
    moves = f"新 {len(a['new'])}／加 {len(a['add'])}／減 {len(a['reduce'])}／出 {len(a['exit'])}"
    return (f"| {r['ticker']} | {r['name']} | {r['active_count']}/{active_total} | "
            f"{r['active_avg_weight']:.2f}% | {moves} | {r['active_net']:+d} | "
            f"{r['passive_count']}/{passive_total} | {'、'.join(r['tags'])} |")


def render(result: dict) -> str:
    at, pt = result["active_total"], result["passive_total"]
    by_ticker = {r["ticker"]: r for r in result["stocks"]}
    header = ("| 代號 | 名稱 | 主動持有 | 主動平均權重 | 主動動作（家數） | 淨加碼 | 被動持有 | 標籤 |\n"
              "|---|---|---|---|---|---|---|---|")
    out = [
        "# ETF 關注名單", "",
        f"產生時間 {result['generated_at']}；持股最新到 {result['as_of']}；"
        f"比較約 {result['lookback_days']} 天前的持股。",
        f"納入以台股為主的主動式 {at} 檔、被動式 {pt} 檔；共 {len(result['stocks'])} 檔股票，"
        f"其中關注 {len(result['focus'])} 檔、留意減碼 {len(result['caution'])} 檔。", "",
        "這是關注名單，不是買賣訊號；持股歷史太短，還沒有證據顯示名單上的股票之後表現較好。", "",
        "標籤：",
        f"- {TAG_CORE}：一半以上的主動式 ETF 持有。",
        f"- {TAG_ADD}／{TAG_REDUCE}：主動式的淨加碼（新買進＋加碼−減碼−出清）家數達 "
        f"±{result['net_threshold']} 家。股數已扣掉整檔 ETF 的規模變化。",
        f"- {TAG_INDEX_IN}／{TAG_INDEX_OUT}：被動式 ETF 在比較期間新納入或剔除（指數調整）。", "",
        "## 關注", "", header]
    out += [_line(by_ticker[t], at, pt) for t in result["focus"]] or ["| （無） | | | | | | | |"]
    out += ["", "## 留意：主動式減碼或被動式剔除", "", header]
    out += [_line(by_ticker[t], at, pt) for t in result["caution"]] or ["| （無） | | | | | | | |"]
    out += ["", "## 納入的 ETF", "", "| 代號 | 名稱 | 類型 | 最新持股 | 比較基準 | 規模變化倍數 | 台股檔數 |",
            "|---|---|---|---|---|---|---|"]
    out += [f"| {e['symbol']} | {e['name']} | {'被動式' if e['kind'] == 'passive' else '主動式'} | "
            f"{e['latest']} | {e['base'] or '只有一期'} | {e['scale']:.3f} | {e['holdings']} |"
            for e in result["etfs"]]
    if result["skipped"]:
        out += ["", "未納入：" + "；".join(f"{s['symbol']}（{s['reason']}）"
                                        for s in result["skipped"])]
    return "\n".join(out) + "\n"


def update(root: Path) -> dict:
    """重算並寫檔。每天另存一份 JSON，之後才有歷史可以檢驗這份名單。"""
    root = Path(root)
    result = build_focus(root)
    folder = root / "data" / "etf_focus"
    folder.mkdir(parents=True, exist_ok=True)
    atomic_json(folder / "latest.json", result)
    atomic_text(folder / "latest.md", render(result))
    if result["as_of"]:
        atomic_json(folder / f"{result['as_of']}.json", result)
    return result


def main(argv=None) -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="ETF 關注名單（主動式＋大型被動式）")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--print", action="store_true", dest="show", help="印出報告")
    args = parser.parse_args(argv)
    result = update(args.root.resolve())
    if args.show:
        print(render(result))
    else:
        print(f"持股最新到 {result['as_of']}；主動式 {result['active_total']} 檔、被動式 "
              f"{result['passive_total']} 檔；關注 {len(result['focus'])} 檔、"
              f"留意 {len(result['caution'])} 檔 → data/etf_focus/latest.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
