"""日 K 交叉核對：用交易所的「全市場單日行情」核對資料庫裡同一天的逐檔日 K。只讀資料庫。

為什麼需要：price_history 是逐檔向 STOCK_DAY／tradingStock 抓的；全市場日報（TWSE MI_INDEX、
TPEx 上櫃日報）是另一個端點。兩邊的開高低收應該完全相同，不同就代表抓取、解析或寫入有問題。
這符合專案「資料驗收要核對數值、日期、來源」的標準，而且一天只多兩個請求。

    python -m bot.price_crosscheck --root C:\\skymiku\\stock --date 2026-11-16

成交量：資料庫存「張」，全市場日報是「股」。上市應完全相同。上櫃兩個端點的統計範圍不同：
2026-11 實測約三成標的逐檔端點比全市場日報多 3～25%（方向一致，推測含盤後、零股或鉅額交易，
未證實），所以上櫃容許 3%，超過的列為提醒，不算價格錯誤。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional, Sequence

from bot.backtest_universe import TPEX_URL, TWSE_URL, fetch_json
from bot.runtime_state import atomic_json
from bot.utils import now_tw

CACHE_REL = "data/cache/market_daily"
OUTPUT_REL = "data/price_crosscheck"
PRICE_TOLERANCE = 0.005
TPEX_VOLUME_TOLERANCE = 0.03
MIN_QUOTES = 300          # 少於這個數量視為尚未公布或回應異常，不快取、不核對
MAX_LISTED = 50


def _num(value: object) -> Optional[float]:
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


def _parse(payload: dict, market: str, code: str, names: Sequence[str]) -> Dict[str, dict]:
    for table in payload.get("tables") or []:
        fields = [str(f).replace(" ", "").strip() for f in table.get("fields") or []]
        if code in fields and all(n in fields for n in names):
            idx = [fields.index(n) for n in names]
            i_code = fields.index(code)
            out = {}
            for row in table.get("data") or []:
                o, h, l, c, shares = (_num(row[i]) for i in idx)
                out[str(row[i_code]).strip()] = dict(market=market, open=o, high=h, low=l, close=c,
                                                     volume=None if shares is None else shares / 1000)
            return out
    return {}


def parse_twse_quotes(payload: dict) -> Dict[str, dict]:
    return _parse(payload, "twse", "證券代號", ("開盤價", "最高價", "最低價", "收盤價", "成交股數"))


def parse_tpex_quotes(payload: dict) -> Dict[str, dict]:
    return _parse(payload, "tpex", "代號", ("開盤", "最高", "最低", "收盤", "成交股數"))


def load_market_quotes(root: Path, day: dt.date, *, fetcher: Callable[..., dict] = fetch_json) -> Dict[str, dict]:
    """兩個市場的單日行情；原始回應只有在內容完整時才存檔（避免把「尚未公布」的空回應當成結果）。"""
    cache = Path(root) / CACHE_REL
    quotes: Dict[str, dict] = {}
    for market, url, parser in (
        ("twse", TWSE_URL.format(ymd=day.strftime("%Y%m%d")), parse_twse_quotes),
        ("tpex", TPEX_URL.format(y=day.year, m=day.month, d=day.day), parse_tpex_quotes),
    ):
        path = cache / f"{market}-{day.isoformat()}.json"
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
        else:
            payload = fetcher(url)
        parsed = parser(payload)
        if len(parsed) < MIN_QUOTES:
            raise ValueError(f"{market} 全市場行情只有 {len(parsed)} 檔，可能尚未公布")
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        quotes.update(parsed)
    return quotes


def crosscheck(root: Path, date: str, *, expected: Iterable[str] = (),
               fetcher: Callable[..., dict] = fetch_json) -> dict:
    """核對 date 當天資料庫所有日 K；expected 是「應該要有」的代號，用來列出資料庫缺的。"""
    root = Path(root)
    quotes = load_market_quotes(root, dt.date.fromisoformat(date), fetcher=fetcher)
    con = sqlite3.connect(f"file:{(root / 'data/stock.db').as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        rows = con.execute("SELECT symbol, open, high, low, close, volume, source FROM price_history "
                           "WHERE date = ? ORDER BY symbol", (date,)).fetchall()
    finally:
        con.close()
    mismatches, volume_notes, unknown, matched = [], [], [], 0
    for symbol, o, h, l, c, volume, source in rows:
        quote = quotes.get(symbol)
        if quote is None:
            unknown.append(symbol)
            continue
        ours = dict(open=o, high=h, low=l, close=c)
        wrong = {k: dict(db=ours[k], market=quote[k]) for k in ours
                 if quote[k] is not None and (ours[k] is None or abs(float(ours[k]) - quote[k]) > PRICE_TOLERANCE)}
        if wrong:
            mismatches.append(dict(symbol=symbol, source=source, fields=wrong))
            continue
        matched += 1
        if quote["volume"] is not None and volume is not None:
            allowed = max(0.001, abs(quote["volume"]) * (TPEX_VOLUME_TOLERANCE if quote["market"] == "tpex" else 0))
            if abs(float(volume) - quote["volume"]) > allowed + 1e-6:
                volume_notes.append(dict(symbol=symbol, source=source, db=volume, market=quote["volume"]))
    have = {row[0] for row in rows}
    missing = sorted(s for s in expected if s in quotes and s not in have)
    return dict(
        date=date, checked_at=now_tw().isoformat(timespec="seconds"),
        status="fail" if mismatches else "warn" if (missing or unknown) else "ok",
        market_quotes=len(quotes), db_rows=len(rows), matched=matched,
        price_mismatch_count=len(mismatches), price_mismatches=mismatches[:MAX_LISTED],
        volume_note_count=len(volume_notes), volume_notes=volume_notes[:MAX_LISTED],
        not_in_market_file=unknown[:MAX_LISTED],
        missing_in_db_count=len(missing), missing_in_db=missing[:MAX_LISTED])


def run(root: Path, date: str, *, expected: Iterable[str] = (), fetcher: Callable[..., dict] = fetch_json) -> dict:
    result = crosscheck(root, date, expected=expected, fetcher=fetcher)
    atomic_json(Path(root) / OUTPUT_REL / f"{date}.json", result)
    return result


def describe(result: dict) -> str:
    text = (f"{result['date']}：資料庫 {result['db_rows']} 檔中 {result['matched']} 檔的開高低收與全市場日報一致，"
            f"價格不符 {result['price_mismatch_count']} 檔，成交量差異 {result['volume_note_count']} 檔，"
            f"名單內尚未入庫 {result['missing_in_db_count']} 檔")
    if result["price_mismatches"]:
        text += "；價格不符：" + "、".join(m["symbol"] for m in result["price_mismatches"][:10])
    if result["not_in_market_file"]:
        text += "；全市場日報沒有這些代號：" + "、".join(result["not_in_market_file"][:10])
    return text


def main(argv: Optional[Sequence[str]] = None) -> int:
    from bot.cli_encoding import configure_console
    configure_console()
    parser = argparse.ArgumentParser(description="用全市場單日行情核對資料庫的日 K（唯讀）")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--date", required=True, help="要核對的交易日，YYYY-MM-DD")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        from bot.daily_collection import extended_price_symbols
        expected = extended_price_symbols(root)
    except Exception:
        expected = []
    result = run(root, args.date, expected=expected)
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else describe(result))
    return {"ok": 0, "warn": 1, "fail": 2}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
