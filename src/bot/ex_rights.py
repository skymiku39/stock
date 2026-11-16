"""除權息資料：從 TWSE／TPEx 取得除權息日、前收盤價、參考價與股利，存入 ex_rights_events。

price_history 是未還原價格。除權息日的跳空不是真實下跌，回測要用這張表調整標準價與停損價、
並把現金股利計入。本模組只負責取得與保存；回測套用留給 #18。

欄位語意：
- prev_close／reference_price：交易所公布的除權息前收盤價與除權息參考價，
  調整係數 = reference_price ÷ prev_close。
- value_total：權值＋息值（元／股）。
- cash_dividend：TPEx 直接提供；TWSE 只有「權值＋息值」，僅在 kind 為「息」時等於現金股利，
  「權息」時無法拆分，存 NULL。
- stock_per_1000：每仟股無償配股數（僅 TPEx 提供）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

import requests

from bot.backtest_universe import fetch_json, load_universe

TWSE_URL = (
    "https://www.twse.com.tw/exchangeReport/TWT49U"
    "?response=json&startDate={start}&endDate={end}"
)
TPEX_URL = (
    "https://www.tpex.org.tw/www/zh-tw/bulletin/exDailyQ"
    "?startDate={start}&endDate={end}&response=json"
)

_ROC_DATE = re.compile(r"(\d{2,3})\D+(\d{1,2})\D+(\d{1,2})")


def roc_to_iso(text: str) -> str:
    """'109年01月30日' 或 '109/01/02' → '2020-01-30'；無法解析回空字串。"""
    m = _ROC_DATE.search(str(text))
    if not m:
        return ""
    y, mo, d = (int(g) for g in m.groups())
    try:
        return dt.date(y + 1911, mo, d).isoformat()
    except ValueError:
        return ""


def _f(text: object) -> Optional[float]:
    s = str(text).replace(",", "").strip()
    if not s or s in {"--", "-", "N/A"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _kind(text: str) -> str:
    t = str(text)
    if "權" in t and "息" in t:
        return "權息"
    if "權" in t:
        return "權"
    if "息" in t:
        return "息"
    return t.strip()


def _index(fields: Sequence[str], *names: str) -> int:
    clean = [str(f).replace(" ", "").strip() for f in fields]
    for name in names:
        if name in clean:
            return clean.index(name)
    return -1


def parse_twse(payload: dict, *, url: str = "", retrieved_at: str = "") -> List[dict]:
    fields = payload.get("fields") or []
    i_date, i_code = _index(fields, "資料日期"), _index(fields, "股票代號")
    i_prev, i_ref = _index(fields, "除權息前收盤價"), _index(fields, "除權息參考價")
    i_val, i_kind = _index(fields, "權值+息值"), _index(fields, "權/息")
    if min(i_date, i_code, i_prev, i_ref, i_val, i_kind) < 0:
        raise ValueError("TWSE 除權息欄位與預期不符")
    rows = []
    for r in payload.get("data") or []:
        kind = _kind(r[i_kind])
        value = _f(r[i_val])
        rows.append({
            "symbol": str(r[i_code]).strip(),
            "ex_date": roc_to_iso(r[i_date]),
            "kind": kind,
            "prev_close": _f(r[i_prev]),
            "reference_price": _f(r[i_ref]),
            "value_total": value,
            "cash_dividend": value if kind == "息" else (0.0 if kind == "權" else None),
            "stock_per_1000": None,
            "market": "twse",
            "source_url": url,
            "retrieved_at": retrieved_at,
        })
    return [r for r in rows if r["ex_date"] and r["symbol"]]


def parse_tpex(payload: dict, *, url: str = "", retrieved_at: str = "") -> List[dict]:
    tables = payload.get("tables") or []
    if not tables:
        return []
    table = tables[0]
    fields = table.get("fields") or []
    i_date, i_code = _index(fields, "除權息日期"), _index(fields, "代號")
    i_prev, i_ref = _index(fields, "除權息前收盤價"), _index(fields, "除權息參考價")
    i_val, i_kind = _index(fields, "權值+息值"), _index(fields, "權/息")
    i_cash, i_stock = _index(fields, "現金股利"), _index(fields, "每仟股無償配股")
    if min(i_date, i_code, i_prev, i_ref, i_val, i_kind, i_cash, i_stock) < 0:
        raise ValueError("TPEx 除權息欄位與預期不符")
    rows = []
    for r in table.get("data") or []:
        rows.append({
            "symbol": str(r[i_code]).strip(),
            "ex_date": roc_to_iso(r[i_date]),
            "kind": _kind(r[i_kind]),
            "prev_close": _f(r[i_prev]),
            "reference_price": _f(r[i_ref]),
            "value_total": _f(r[i_val]),
            "cash_dividend": _f(r[i_cash]),
            "stock_per_1000": _f(r[i_stock]),
            "market": "tpex",
            "source_url": url,
            "retrieved_at": retrieved_at,
        })
    return [r for r in rows if r["ex_date"] and r["symbol"]]


def fetch_year(
    year: int,
    *,
    session: Optional[requests.Session] = None,
    fetcher=fetch_json,
    now: Optional[str] = None,
) -> List[dict]:
    stamp = now or dt.datetime.now().isoformat(timespec="seconds")
    twse_url = TWSE_URL.format(start=f"{year}0101", end=f"{year}1231")
    tpex_url = TPEX_URL.format(start=f"{year}/01/01", end=f"{year}/12/31")
    rows = parse_twse(fetcher(twse_url, session=session), url=twse_url, retrieved_at=stamp)
    rows += parse_tpex(fetcher(tpex_url, session=session), url=tpex_url, retrieved_at=stamp)
    return rows


_COLUMNS = (
    "symbol", "ex_date", "kind", "prev_close", "reference_price", "value_total",
    "cash_dividend", "stock_per_1000", "market", "source_url", "retrieved_at",
)


def save_events(db, rows: Iterable[dict]) -> int:
    data = [tuple(r[c] for c in _COLUMNS) for r in rows]
    if not data:
        return 0
    sql = (
        f"INSERT OR REPLACE INTO ex_rights_events ({', '.join(_COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in _COLUMNS)})"
    )
    with db.transaction(immediate=True) as c:
        c.executemany(sql, data)
    return len(data)


def load_events(db, symbol: str, *, start: str = "", end: str = "") -> List[dict]:
    sql = "SELECT * FROM ex_rights_events WHERE symbol = ?"
    args: list = [symbol]
    if start:
        sql += " AND ex_date >= ?"
        args.append(start)
    if end:
        sql += " AND ex_date <= ?"
        args.append(end)
    cur = db.conn.execute(sql + " ORDER BY ex_date", args)
    return [dict(row) for row in cur.fetchall()]


def verify_against_price_history(db, symbol: str) -> List[dict]:
    """交叉檢查：交易所公布的「除權息前收盤價」應等於 price_history 除權息日前一個交易日的收盤價。

    回傳每筆事件的比對結果；沒有前一日日 K 的事件標 status=no_price。
    """
    results = []
    for ev in load_events(db, symbol):
        cur = db.conn.execute(
            "SELECT date, close FROM price_history WHERE symbol = ? AND date < ? "
            "ORDER BY date DESC LIMIT 1",
            (symbol, ev["ex_date"]),
        )
        row = cur.fetchone()
        if row is None or ev["prev_close"] is None:
            results.append({**ev, "status": "no_price", "ok": False})
            continue
        ok = abs(float(row["close"]) - float(ev["prev_close"])) < 0.005
        results.append({**ev, "prev_date": row["date"], "db_close": float(row["close"]),
                        "status": "ok" if ok else "mismatch", "ok": ok})
    return results


def main(argv: Optional[Sequence[str]] = None) -> int:
    from bot.stock_db import StockDB, default_db_path

    parser = argparse.ArgumentParser(description="取得除權息資料並存入 ex_rights_events")
    parser.add_argument("--from-year", type=int, default=2020)
    parser.add_argument("--to-year", type=int, default=dt.date.today().year)
    parser.add_argument("--universe", default="config/backtest_universe.json",
                        help="只保存這份清單內的標的；空字串表示全部")
    parser.add_argument("--db", default="", help="資料庫路徑，預設為正式資料庫")
    args = parser.parse_args(argv)

    symbols = set(load_universe(Path(args.universe))) if args.universe else None
    db = StockDB.open(path=Path(args.db) if args.db else default_db_path(Path.cwd()))
    try:
        for year in range(args.from_year, args.to_year + 1):
            rows = fetch_year(year)
            if symbols is not None:
                rows = [r for r in rows if r["symbol"] in symbols]
            print(f"{year}: 儲存 {save_events(db, rows)} 筆", flush=True)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
