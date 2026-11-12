"""回測標的選取：用過去某一年的全市場成交金額排名，避免用「現在」的熱門股偷看未來。

流程：逐日抓 TWSE 與 TPEx 的全市場每日行情（原始 JSON 存檔以便重現），
把每檔股票的日成交金額加總，除以該年交易日數得到日均成交金額，
排除 2330 與非普通股（ETF、權證、受益證券、存託憑證）後取前 N 檔。

已下市的股票在今天的資料來源抓不到，這個存活者偏誤無法完全消除；
排名用的是當年實際成交資料，所以當年成交很大、後來才下市者只要原始資料還在就會被計入，
但選定後若日 K 抓不到，會在回補報告列為缺口，不會被替換。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import requests

TWSE_URL = (
    "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
    "?date={ymd}&type=ALLBUT0999&response=json"
)
TPEX_URL = (
    "https://www.tpex.org.tw/www/zh-tw/afterTrading/otc"
    "?date={y}/{m:02d}/{d:02d}&type=EW&response=json"
)
HEADERS = {"User-Agent": "Mozilla/5.0 (stock-lab research; contact: local)"}
DEFAULT_EXCLUDED = ("2330",)
UNIVERSE_FILE = Path("config") / "backtest_universe.json"

_COMMON_STOCK = re.compile(r"^[1-9]\d{3}$")


def is_common_stock(code: str) -> bool:
    """四位數字且不以 0 開頭（排除 ETF 00xx、權證、受益證券）；91xx 是存託憑證。"""
    return bool(_COMMON_STOCK.match(code)) and not code.startswith("91")


def _num(text: object) -> float:
    s = str(text).replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_twse_daily(payload: dict) -> Dict[str, Tuple[str, float]]:
    """回傳 {代號: (名稱, 成交金額)}；假日或無資料回空 dict。"""
    for table in payload.get("tables") or []:
        fields = [str(f).strip() for f in table.get("fields") or []]
        if "證券代號" in fields and "成交金額" in fields:
            i_code, i_name, i_amt = (
                fields.index("證券代號"), fields.index("證券名稱"), fields.index("成交金額"),
            )
            return {
                str(r[i_code]).strip(): (str(r[i_name]).strip(), _num(r[i_amt]))
                for r in table.get("data") or []
            }
    return {}


def parse_tpex_daily(payload: dict) -> Dict[str, Tuple[str, float]]:
    for table in payload.get("tables") or []:
        fields = [str(f).replace(" ", "").strip() for f in table.get("fields") or []]
        if "代號" in fields and "成交金額(元)" in fields:
            i_code, i_name, i_amt = (
                fields.index("代號"), fields.index("名稱"), fields.index("成交金額(元)"),
            )
            return {
                str(r[i_code]).strip(): (str(r[i_name]).strip(), _num(r[i_amt]))
                for r in table.get("data") or []
            }
    return {}


def fetch_json(
    url: str,
    *,
    session: Optional[requests.Session] = None,
    retries: int = 3,
    backoff: float = 5.0,
) -> dict:
    """GET JSON，timeout／連線錯誤／429／5xx／非 JSON 才重試；用完仍失敗就丟出例外。"""
    sess = session or requests.Session()
    last: Exception = RuntimeError("no attempt")
    for attempt in range(retries):
        try:
            resp = sess.get(url, headers=HEADERS, timeout=30)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.HTTPError(f"HTTP {resp.status_code}")
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            last = exc
            if attempt + 1 < retries:
                time.sleep(backoff * (attempt + 1))
    raise last


def weekdays(year: int) -> List[dt.date]:
    day, out = dt.date(year, 1, 1), []
    while day.year == year:
        if day.weekday() < 5:
            out.append(day)
        day += dt.timedelta(days=1)
    return out


def load_or_fetch_day(
    market: str,
    day: dt.date,
    cache_dir: Path,
    *,
    session: Optional[requests.Session] = None,
    delay: float = 3.0,
    fetcher: Callable[..., dict] = fetch_json,
) -> dict:
    """原始 JSON 存在 cache_dir/{market}-{date}.json；已存在就不重抓。"""
    path = cache_dir / f"{market}-{day.isoformat()}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    url = (
        TWSE_URL.format(ymd=day.strftime("%Y%m%d"))
        if market == "twse"
        else TPEX_URL.format(y=day.year, m=day.month, d=day.day)
    )
    payload = fetcher(url, session=session)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    if delay > 0:
        time.sleep(delay)
    return payload


def collect_turnover(
    year: int,
    cache_dir: Path,
    *,
    session: Optional[requests.Session] = None,
    delay: float = 3.0,
    fetcher: Callable[..., dict] = fetch_json,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> Tuple[Dict[str, float], Dict[str, str], int]:
    """回傳 (代號→全年成交金額合計, 代號→名稱, 交易日數)。交易日以 TWSE 當日有資料為準。"""
    totals: Dict[str, float] = defaultdict(float)
    names: Dict[str, str] = {}
    trading_days = 0
    days = weekdays(year)
    for i, day in enumerate(days, start=1):
        twse = parse_twse_daily(
            load_or_fetch_day("twse", day, cache_dir, session=session, delay=delay, fetcher=fetcher)
        )
        if not twse:
            if progress:
                progress(i, len(days), f"{day} 休市")
            continue
        trading_days += 1
        tpex = parse_tpex_daily(
            load_or_fetch_day("tpex", day, cache_dir, session=session, delay=delay, fetcher=fetcher)
        )
        for table in (twse, tpex):
            for code, (name, amount) in table.items():
                totals[code] += amount
                names[code] = name
        if progress:
            progress(i, len(days), f"{day} 交易日 {trading_days}")
    return dict(totals), names, trading_days


def select_universe(
    totals: Dict[str, float],
    names: Dict[str, str],
    trading_days: int,
    *,
    count: int = 60,
    excluded: Iterable[str] = DEFAULT_EXCLUDED,
) -> List[dict]:
    """日均成交金額由大到小，同額以代號排序，確保可重現。"""
    skip = set(excluded)
    ranked = sorted(
        ((code, amt) for code, amt in totals.items() if is_common_stock(code) and code not in skip),
        key=lambda item: (-item[1], item[0]),
    )
    days = max(1, trading_days)
    return [
        {
            "rank": i,
            "symbol": code,
            "name": names.get(code, ""),
            "avg_daily_turnover_twd": round(amt / days),
        }
        for i, (code, amt) in enumerate(ranked[:count], start=1)
    ]


def build_universe_file(
    year: int,
    cache_dir: Path,
    out_path: Path,
    *,
    count: int = 60,
    excluded: Sequence[str] = DEFAULT_EXCLUDED,
    session: Optional[requests.Session] = None,
    delay: float = 3.0,
    fetcher: Callable[..., dict] = fetch_json,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> dict:
    totals, names, trading_days = collect_turnover(
        year, cache_dir, session=session, delay=delay, fetcher=fetcher, progress=progress,
    )
    doc = {
        "method": (
            f"{year} 年 TWSE＋TPEx 全市場每日行情，依日均成交金額（全年合計 ÷ 交易日數）"
            f"排名，排除 {', '.join(excluded)} 與非普通股（ETF、權證、受益證券、存託憑證）；選定後不更換。"
        ),
        "ranking_year": year,
        "count": count,
        "excluded": list(excluded),
        "trading_days": trading_days,
        "sources": [TWSE_URL, TPEX_URL],
        "raw_cache_dir": str(cache_dir),
        "caveat": "已下市股票在現行來源可能抓不到日 K，存活者偏誤無法完全消除。",
        "symbols": select_universe(totals, names, trading_days, count=count, excluded=excluded),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return doc


def load_universe(path: Path = UNIVERSE_FILE) -> List[str]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    return [row["symbol"] for row in doc["symbols"]]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="用過去一年成交金額選回測標的")
    parser.add_argument("--year", type=int, default=2020)
    parser.add_argument("--count", type=int, default=60)
    parser.add_argument("--delay", type=float, default=3.0, help="每次請求間隔秒數")
    parser.add_argument("--cache-dir", default="data/cache/universe")
    parser.add_argument("--out", default=str(UNIVERSE_FILE))
    args = parser.parse_args(argv)

    def show(i: int, n: int, label: str) -> None:
        if i % 20 == 0 or i == n:
            print(f"[{i}/{n}] {label}", flush=True)

    doc = build_universe_file(
        args.year, Path(args.cache_dir), Path(args.out),
        count=args.count, delay=args.delay, progress=show,
    )
    print(f"已寫入 {args.out}：{len(doc['symbols'])} 檔，交易日 {doc['trading_days']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
