"""market_macro -- 美股 / 加權指 / VIX / ADR 溢價的自動抓取與快取。

設計
====
* 使用 `yfinance` (免費且最穩定的免登入 API) 抓所有指數與美股
* 抓回來後計算每檔 ADR 對應台股的「公允台股價」與「溢價百分比」
* 結果以日為單位快取於 `data/macro/YYYY-MM-DD.json`，重複呼叫不會打爆 API
* 不依賴 yfinance 也能 import (lazy)；安裝不到時退化為「無資料」狀態

使用
====
```python
from bot.market_macro import fetch_macro_snapshot
snap = fetch_macro_snapshot(root=PROJECT_ROOT)   # 用 cache
snap = fetch_macro_snapshot(force_refresh=True)  # 強制重抓
```
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import warnings
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bot.utils import get_logger, mk_folder, now_tw

warnings.filterwarnings("ignore", category=FutureWarning)


# ----------------------------------------------------------------------
# 配置：要追蹤的標的
# ----------------------------------------------------------------------

# 美股 / 全球指數
DEFAULT_INDICES: List[Tuple[str, str, str]] = [
    # (symbol, name, group)
    ("^GSPC", "S&P 500", "us_index"),
    ("^IXIC", "NASDAQ Composite", "us_index"),
    ("^DJI", "道瓊工業", "us_index"),
    ("^SOX", "費城半導體", "us_index"),
    ("^VIX", "VIX 恐慌指數", "us_index"),
    ("^TWII", "加權指數", "tw_index"),
]

# 重要美股 / ADR
# (symbol, name, role, parent_tw_ticker)
DEFAULT_STOCKS: List[Tuple[str, str, str, str]] = [
    ("NVDA", "NVIDIA", "AI/GPU 龍頭", ""),
    ("AMD", "AMD", "AI/CPU/GPU", ""),
    ("AAPL", "Apple", "消費電子龍頭", ""),
    ("MSFT", "Microsoft", "雲端/AI", ""),
    ("GOOGL", "Alphabet", "雲端/AI", ""),
    ("META", "Meta", "AI 資本支出", ""),
    ("AMZN", "Amazon", "雲端/電商", ""),
    ("TSLA", "Tesla", "電動車", ""),
    ("ASML", "ASML", "半導體設備", ""),
    ("AVGO", "Broadcom", "ASIC/網通", ""),
    ("MU", "Micron", "記憶體", ""),
    # ADR
    ("TSM", "台積電 ADR", "晶圓代工", "2330"),
    ("UMC", "聯電 ADR", "晶圓代工", "2303"),
    ("ASX", "日月光 ADR", "封測", "3711"),
]

# ADR 兌母股比例：1 張 ADR 對應幾股母股 (台股 1 張 = 1000 股)
ADR_RATIOS: Dict[str, float] = {
    "TSM": 5.0,   # 1 ADR = 5 shares of 2330
    "UMC": 5.0,
    "ASX": 2.0,
}


# ----------------------------------------------------------------------
# 資料模型
# ----------------------------------------------------------------------


@dataclass
class IndexQuote:
    symbol: str
    name: str
    group: str
    price: float = 0.0
    prev_close: float = 0.0
    pct_change: float = 0.0
    volume: float = 0.0
    fetched_at: str = ""
    source: str = "yfinance"


@dataclass
class StockQuote:
    symbol: str
    name: str
    role: str = ""
    price: float = 0.0
    prev_close: float = 0.0
    pct_change: float = 0.0
    volume: float = 0.0
    market_cap: float = 0.0
    parent_tw_ticker: str = ""
    fetched_at: str = ""


@dataclass
class AdrPremium:
    tw_ticker: str
    tw_name: str
    adr_symbol: str
    adr_price_usd: float
    tw_price_twd: float
    fx_usdtwd: float
    adr_ratio: float
    fair_tw_price: float
    premium_pct: float


@dataclass
class MacroSnapshot:
    fetched_at: str
    asof_date: str            # 多半是抓取當日 (美股結算可能落後一日)
    indices: Dict[str, IndexQuote] = field(default_factory=dict)
    stocks: Dict[str, StockQuote] = field(default_factory=dict)
    adr_premiums: List[AdrPremium] = field(default_factory=list)
    usdtwd: float = 0.0
    notes: List[str] = field(default_factory=list)
    cached: bool = False

    # ---- 便利方法 ----
    def index(self, sym: str) -> Optional[IndexQuote]:
        return self.indices.get(sym)

    def stock(self, sym: str) -> Optional[StockQuote]:
        return self.stocks.get(sym)


# ----------------------------------------------------------------------
# yfinance 包裝
# ----------------------------------------------------------------------


def _import_yf():
    try:
        import yfinance as yf  # type: ignore
        return yf
    except Exception:
        return None


def _safe_quote(yf_module: Any, symbol: str, logger: logging.Logger) -> Optional[Dict[str, Any]]:
    try:
        t = yf_module.Ticker(symbol)
        hist = t.history(period="5d", auto_adjust=False)
        if hist is None or hist.empty:
            return None
        close = float(hist["Close"].iloc[-1])
        prev = float(hist["Close"].iloc[-2]) if len(hist) > 1 else close
        volume = float(hist["Volume"].iloc[-1]) if "Volume" in hist.columns else 0.0
        return {
            "price": close,
            "prev_close": prev,
            "pct_change": ((close - prev) / prev * 100.0) if prev else 0.0,
            "volume": volume,
        }
    except Exception:
        logger.exception("yfinance 抓 %s 失敗", symbol)
        return None


def _fetch_fx_usdtwd(yf_module: Any, logger: logging.Logger) -> float:
    """yfinance 的 TWD=X 是 USD/TWD (即 1 USD 換多少 TWD)。"""
    if yf_module is None:
        return 31.5  # 合理 fallback
    q = _safe_quote(yf_module, "TWD=X", logger)
    if q and q["price"] > 0:
        return q["price"]
    return 31.5


# ----------------------------------------------------------------------
# 取台股收盤 (用於 ADR 溢價計算)
# ----------------------------------------------------------------------


def _fetch_tw_close(ticker: str, logger: logging.Logger) -> Optional[float]:
    """先試 yfinance (例：2330.TW)；失敗時試 TWSE OpenAPI。"""
    yf = _import_yf()
    if yf is not None:
        for suffix in (".TW", ".TWO"):
            q = _safe_quote(yf, f"{ticker}{suffix}", logger)
            if q and q["price"] > 0:
                return q["price"]
    # TODO: 補 TWSE OpenAPI fallback
    return None


# ----------------------------------------------------------------------
# 快取
# ----------------------------------------------------------------------


def _cache_path(date: dt.date, root: Optional[Path]) -> Path:
    return (root or Path.cwd()) / "data" / "macro" / f"macro_{date.isoformat()}.json"


def _load_cache(date: dt.date, root: Optional[Path]) -> Optional[MacroSnapshot]:
    p = _cache_path(date, root)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        snap = MacroSnapshot(
            fetched_at=d.get("fetched_at", ""),
            asof_date=d.get("asof_date", date.isoformat()),
            usdtwd=float(d.get("usdtwd", 0)),
            notes=list(d.get("notes", []) or []),
            cached=True,
        )
        for sym, info in (d.get("indices") or {}).items():
            snap.indices[sym] = IndexQuote(**info)
        for sym, info in (d.get("stocks") or {}).items():
            snap.stocks[sym] = StockQuote(**info)
        for p_info in (d.get("adr_premiums") or []):
            snap.adr_premiums.append(AdrPremium(**p_info))
        return snap
    except Exception:
        return None


def _save_cache(snap: MacroSnapshot, root: Optional[Path]) -> Path:
    asof = dt.date.fromisoformat(snap.asof_date) if snap.asof_date else now_tw().date()
    p = _cache_path(asof, root)
    mk_folder(str(p.parent))
    payload = {
        "fetched_at": snap.fetched_at,
        "asof_date": snap.asof_date,
        "usdtwd": snap.usdtwd,
        "notes": snap.notes,
        "indices": {k: asdict(v) for k, v in snap.indices.items()},
        "stocks": {k: asdict(v) for k, v in snap.stocks.items()},
        "adr_premiums": [asdict(x) for x in snap.adr_premiums],
    }
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------


def fetch_macro_snapshot(
    *,
    root: Optional[Path] = None,
    force_refresh: bool = False,
    use_cache: bool = True,
    logger: Optional[logging.Logger] = None,
) -> MacroSnapshot:
    """抓 (或回傳快取的) 美股 + 加權 + ADR 溢價總覽。"""
    log = logger or get_logger("macro")
    today = now_tw().date()

    if use_cache and not force_refresh:
        cached = _load_cache(today, root)
        if cached is not None:
            log.info("macro 命中快取 %s", today)
            return cached

    yf = _import_yf()
    notes: List[str] = []
    if yf is None:
        notes.append("yfinance 未安裝，回傳空資料")
        return MacroSnapshot(
            fetched_at=now_tw().isoformat(timespec="seconds"),
            asof_date=today.isoformat(),
            notes=notes,
        )

    snap = MacroSnapshot(
        fetched_at=now_tw().isoformat(timespec="seconds"),
        asof_date=today.isoformat(),
    )

    # ---- 指數 ----
    for sym, name, group in DEFAULT_INDICES:
        q = _safe_quote(yf, sym, log)
        if q is None:
            notes.append(f"index {sym} 抓不到")
            continue
        snap.indices[sym] = IndexQuote(
            symbol=sym, name=name, group=group,
            price=q["price"], prev_close=q["prev_close"],
            pct_change=q["pct_change"], volume=q["volume"],
            fetched_at=snap.fetched_at,
        )

    # ---- 個股 / ADR ----
    for sym, name, role, parent in DEFAULT_STOCKS:
        q = _safe_quote(yf, sym, log)
        if q is None:
            notes.append(f"stock {sym} 抓不到")
            continue
        snap.stocks[sym] = StockQuote(
            symbol=sym, name=name, role=role,
            price=q["price"], prev_close=q["prev_close"],
            pct_change=q["pct_change"], volume=q["volume"],
            parent_tw_ticker=parent,
            fetched_at=snap.fetched_at,
        )

    # ---- 匯率 ----
    snap.usdtwd = _fetch_fx_usdtwd(yf, log)

    # ---- ADR 溢價 ----
    for adr_sym, ratio in ADR_RATIOS.items():
        sq = snap.stocks.get(adr_sym)
        if sq is None or not sq.parent_tw_ticker:
            continue
        tw_price = _fetch_tw_close(sq.parent_tw_ticker, log)
        if not tw_price or tw_price <= 0:
            continue
        fair_tw = sq.price * snap.usdtwd / ratio if ratio else 0
        if fair_tw <= 0:
            continue
        prem_pct = (fair_tw - tw_price) / tw_price * 100.0
        snap.adr_premiums.append(AdrPremium(
            tw_ticker=sq.parent_tw_ticker,
            tw_name=_tw_name_for(sq.parent_tw_ticker, adr_sym),
            adr_symbol=adr_sym,
            adr_price_usd=sq.price,
            tw_price_twd=tw_price,
            fx_usdtwd=snap.usdtwd,
            adr_ratio=ratio,
            fair_tw_price=round(fair_tw, 2),
            premium_pct=round(prem_pct, 2),
        ))

    snap.notes = notes
    try:
        _save_cache(snap, root)
    except Exception:
        log.exception("macro cache 寫入失敗")

    log.info(
        "macro 抓取完成: %d 指數, %d 個股, %d ADR 溢價",
        len(snap.indices), len(snap.stocks), len(snap.adr_premiums),
    )
    return snap


def _tw_name_for(ticker: str, adr: str) -> str:
    fallback = {"2330": "台積電", "2303": "聯電", "3711": "日月光"}
    return fallback.get(ticker, adr)


# ----------------------------------------------------------------------
# 序列化 (給 dashboard 用)
# ----------------------------------------------------------------------


def macro_to_dict(s: MacroSnapshot) -> Dict[str, Any]:
    return {
        "fetched_at": s.fetched_at,
        "asof_date": s.asof_date,
        "usdtwd": s.usdtwd,
        "notes": list(s.notes),
        "indices": {k: asdict(v) for k, v in s.indices.items()},
        "stocks": {k: asdict(v) for k, v in s.stocks.items()},
        "adr_premiums": [asdict(p) for p in s.adr_premiums],
        "cached": s.cached,
    }


def load_supply_chain(root: Optional[Path] = None) -> Dict[str, Any]:
    """讀取 data/supply_chain.json (美股 → 台股供應鏈對照)。"""
    p = (root or Path.cwd()) / "data" / "supply_chain.json"
    if not p.exists():
        return {"us_stocks": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"us_stocks": {}}


def save_supply_chain(data: Dict[str, Any], root: Optional[Path] = None) -> Path:
    p = (root or Path.cwd()) / "data" / "supply_chain.json"
    mk_folder(str(p.parent))
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def related_us_stocks_for_tw(
    tw_ticker: str,
    supply_chain: Optional[Dict[str, Any]] = None,
    root: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """反查：某檔台股對應到哪些美股客戶/供應鏈夥伴。"""
    sc = supply_chain or load_supply_chain(root)
    out: List[Dict[str, Any]] = []
    for us_sym, info in (sc.get("us_stocks") or {}).items():
        for entry in info.get("tw_supply_chain", []) or []:
            if entry.get("tw_ticker") == tw_ticker:
                out.append({
                    "us_ticker": us_sym,
                    "us_name": info.get("name", us_sym),
                    "us_category": info.get("category", ""),
                    "role": entry.get("role", ""),
                    "weight": float(entry.get("weight", 0.5)),
                })
                break
    out.sort(key=lambda x: x["weight"], reverse=True)
    return out


__all__ = [
    "ADR_RATIOS",
    "AdrPremium",
    "DEFAULT_INDICES",
    "DEFAULT_STOCKS",
    "IndexQuote",
    "MacroSnapshot",
    "StockQuote",
    "fetch_macro_snapshot",
    "load_supply_chain",
    "macro_to_dict",
    "related_us_stocks_for_tw",
    "save_supply_chain",
]
