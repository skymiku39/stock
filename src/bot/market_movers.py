"""market_movers -- 全市場即時漲跌幅掃描 (TWSE MIS 分批查詢)。

設計
====
* 代號清單來自 ``market_meta.load_market_map`` (每日快取)
* 依已知上市/上櫃只查單一 exchange，避免 MIS 雙查
* 分批請求 (預設 45 檔/批)，合併後排序取 Top N
* 純函式、無 Streamlit 依賴；儀表板用 ``dashboard.cache_helpers`` 加 TTL
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import requests

from bot.market_meta import is_etf, load_market_map
from bot.utils import get_logger, now_tw

MIS_INDEX_URL = "https://mis.twse.com.tw/stock/index.jsp"
MIS_API_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"

Direction = Literal["gainers", "losers", "abs"]


@dataclass
class MoverRow:
    ticker: str
    name: str = ""
    price: float | None = None
    prev_close: float = 0.0
    pct_chg: float | None = None
    volume: int = 0
    exchange: str = ""
    quote_time: str = ""
    quote_date: str = ""
    price_basis: str = ""


@dataclass
class MarketMoversResult:
    asof: str
    direction: str
    limit: int
    scanned: int = 0
    quoted: int = 0
    batch_count: int = 0
    duration_sec: float = 0.0
    rows: list[MoverRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            **{k: v for k, v in asdict(self).items() if k != "rows"},
            "rows": [asdict(r) for r in self.rows],
        }


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept": "application/json",
    })
    return s


def _parse_float(value: Any) -> float | None:
    try:
        text = str(value or "").replace(",", "").strip()
        if not text or text == "-":
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def _parse_int(value: Any) -> int:
    try:
        text = str(value or "").replace(",", "").strip()
        if not text or text == "-":
            return 0
        return int(float(text))
    except (TypeError, ValueError):
        return 0


def _first_book_price(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text or text == "-":
        return None
    return _parse_float(text.split("_", 1)[0])


def _resolve_price(item: dict[str, Any]) -> tuple[float | None, str]:
    z_str = item.get("z", "-")
    pz_str = item.get("pz", "-")
    last_trade = _parse_float(z_str)
    previous_trade = _parse_float(pz_str)
    best_bid = _first_book_price(item.get("b"))
    best_ask = _first_book_price(item.get("a"))
    if last_trade is not None:
        return last_trade, "last_trade"
    if previous_trade is not None:
        return previous_trade, "previous_trade"
    if best_bid is not None and best_ask is not None:
        return round((best_bid + best_ask) / 2, 4), "bid_ask_mid"
    if best_bid is not None:
        return best_bid, "best_bid"
    if best_ask is not None:
        return best_ask, "best_ask"
    return None, ""


def _ex_ch_for_batch(
    tickers: list[str],
    market_map: dict[str, str],
) -> str:
    parts: list[str] = []
    for ticker in tickers:
        market = market_map.get(ticker, "twse")
        ex = "tse" if market == "twse" else "otc"
        parts.append(f"{ex}_{ticker}.tw")
    return "|".join(parts)


def _parse_mis_items(items: list[dict]) -> dict[str, MoverRow]:
    out: dict[str, MoverRow] = {}
    for item in items:
        code = str(item.get("c") or "").strip()
        if not code:
            continue
        price, basis = _resolve_price(item)
        prev_close = _parse_float(item.get("y")) or 0.0
        pct_chg: float | None = None
        if price is not None and prev_close > 0:
            pct_chg = round(100 * (price - prev_close) / prev_close, 2)
        raw_date = str(item.get("d") or "")
        quote_date = ""
        if len(raw_date) == 8 and raw_date.isdigit():
            quote_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
        out[code] = MoverRow(
            ticker=code,
            name=str(item.get("n") or ""),
            price=price,
            prev_close=prev_close,
            pct_chg=pct_chg,
            volume=_parse_int(item.get("v")),
            exchange=str(item.get("ex") or ""),
            quote_time=str(item.get("t") or ""),
            quote_date=quote_date,
            price_basis=basis,
        )
    return out


def list_scan_tickers(
    *,
    root: Path | None = None,
    exclude_etf: bool = True,
    markets: list[str] | None = None,
) -> list[str]:
    """回傳可掃描的代號清單 (上市+上櫃一般股)。"""
    market_map = load_market_map(root=root)
    allowed = set(markets or ["twse", "tpex"])
    tickers: list[str] = []
    for code, market in market_map.items():
        if market not in allowed:
            continue
        if not code.isdigit() or len(code) != 4:
            continue
        if exclude_etf and is_etf(code):
            continue
        tickers.append(code)
    return sorted(set(tickers))


def fetch_market_movers(
    *,
    root: Path | None = None,
    limit: int = 50,
    direction: Direction = "gainers",
    exclude_etf: bool = True,
    batch_size: int = 45,
    batch_pause_sec: float = 0.15,
    session: requests.Session | None = None,
    logger: logging.Logger | None = None,
) -> MarketMoversResult:
    """掃描全市場 MIS 報價並回傳漲跌幅排行。"""
    log = logger or get_logger("market-movers")
    t0 = time.time()
    result = MarketMoversResult(
        asof=now_tw().isoformat(timespec="seconds"),
        direction=direction,
        limit=max(1, limit),
    )
    market_map = load_market_map(root=root)
    tickers = list_scan_tickers(root=root, exclude_etf=exclude_etf)
    result.scanned = len(tickers)
    if not tickers:
        result.errors.append("無可掃描代號 (market_map 為空)")
        result.duration_sec = round(time.time() - t0, 2)
        return result

    sess = session or _session()
    try:
        sess.get(MIS_INDEX_URL, timeout=10)
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"MIS session 初始化失敗: {exc}")
        result.duration_sec = round(time.time() - t0, 2)
        return result

    merged: dict[str, MoverRow] = {}
    batches = [
        tickers[i: i + batch_size]
        for i in range(0, len(tickers), batch_size)
    ]
    result.batch_count = len(batches)

    for idx, batch in enumerate(batches):
        ex_ch = _ex_ch_for_batch(batch, market_map)
        try:
            resp = sess.get(
                MIS_API_URL,
                params={"ex_ch": ex_ch, "json": "1", "delay": "0"},
                timeout=20,
            )
            data = resp.json()
            merged.update(_parse_mis_items(data.get("msgArray") or []))
        except Exception as exc:  # noqa: BLE001
            msg = f"批次 {idx + 1}/{len(batches)} 失敗: {exc}"
            log.warning(msg)
            result.errors.append(msg)
        if idx + 1 < len(batches) and batch_pause_sec > 0:
            time.sleep(batch_pause_sec)

    quoted = [r for r in merged.values() if r.pct_chg is not None]
    result.quoted = len(quoted)

    reverse = direction != "losers"
    if direction == "abs":
        quoted.sort(key=lambda r: abs(r.pct_chg or 0.0), reverse=True)
    else:
        quoted.sort(key=lambda r: r.pct_chg or 0.0, reverse=reverse)

    result.rows = quoted[: result.limit]
    result.duration_sec = round(time.time() - t0, 2)
    log.info(
        "market movers %s: scanned=%d quoted=%d batches=%d -> top %d (%.1fs)",
        direction,
        result.scanned,
        result.quoted,
        result.batch_count,
        len(result.rows),
        result.duration_sec,
    )
    return result


__all__ = [
    "Direction",
    "MarketMoversResult",
    "MoverRow",
    "fetch_market_movers",
    "list_scan_tickers",
]
