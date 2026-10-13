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
import csv
import hashlib
import io
import json
import logging
import warnings
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from bot.cloud_file_cache import mirror_file_to_cloud, restore_file_from_cloud
from bot.source_registry import SourceRegistry, resolve_source_document_path
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
    source_date: str = ""


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
    source_date: str = ""


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
    # ADR / FX / Taiwan spot may come from different feeds and dates.  Keep
    # the provenance on the calculated value instead of presenting it as a
    # single same-time quote.
    adr_source_date: str = ""
    tw_source: str = ""
    tw_source_date: str = ""
    fx_source_date: str = ""
    temporal_status: str = "unknown"


@dataclass
class FuturesBasis:
    """台指期 (TX) 近月與現貨 (加權指) 的價差 — 作為「期貨領先指標」之一。

    正價差 (futures > spot) 代表市場情緒偏樂觀/過熱；
    逆價差 (futures < spot) 代表偏悲觀/保守 (6-8 月除權息旺季會自然偏逆價差)。
    """
    contract: str = "TX"
    contract_month: str = ""     # 近月，例 "202606"
    futures_price: float = 0.0   # 近月期貨收盤/結算
    spot_price: float = 0.0      # 加權指數現貨
    basis: float = 0.0           # 期貨 - 現貨 (點數)
    basis_pct: float = 0.0       # 價差 / 現貨 %
    open_interest: float = 0.0   # 近月未平倉量
    state: str = ""              # 正價差 | 逆價差 | 平水 | unknown
    asof_date: str = ""
    source: str = "taifex"
    source_document_id: str = ""
    quality_status: str = "ok"
    cached: bool = False
    note: str = ""


@dataclass
class MacroSnapshot:
    fetched_at: str
    asof_date: str            # 多半是抓取當日 (美股結算可能落後一日)
    indices: Dict[str, IndexQuote] = field(default_factory=dict)
    stocks: Dict[str, StockQuote] = field(default_factory=dict)
    adr_premiums: List[AdrPremium] = field(default_factory=list)
    usdtwd: Optional[float] = None
    notes: List[str] = field(default_factory=list)
    futures_basis: Optional[FuturesBasis] = None
    cached: bool = False
    # 每一筆外部行情的實際資料日期；asof_date 是快取/抓取日期，不能取代它。
    source_dates: Dict[str, str] = field(default_factory=dict)
    source_date_status: str = "unknown"  # aligned / mixed / unknown
    source_document_id: str = ""

    # ---- 便利方法 ----
    def index(self, sym: str) -> Optional[IndexQuote]:
        return self.indices.get(sym)

    def stock(self, sym: str) -> Optional[StockQuote]:
        return self.stocks.get(sym)


def classify_source_dates(source_dates: Dict[str, str]) -> str:
    """判斷一組來源是否具有同一個可確認的觀測日。"""
    values = {
        str(value or "").strip()[:10]
        for value in (source_dates or {}).values()
        if str(value or "").strip()
    }
    if not values:
        return "unknown"
    return "aligned" if len(values) == 1 else "mixed"


def macro_quality_status(snapshot: Mapping[str, Any]) -> str:
    """將 macro 的時間與來源降級狀態轉成 evidence quality。

    ``source_date_status``、來源備註與台指期快取狀態都會影響「能否直接
    比較」；它們不能只存在報告 warning，而讓 evidence 看起來仍是完整的
    ``ok``。這個判定故意保守，並不把 ``partial`` 當成沒有資料。
    """
    temporal = str(snapshot.get("source_date_status") or "unknown").strip().lower()
    futures = snapshot.get("futures_basis")
    futures_quality = "ok"
    futures_cached = False
    if isinstance(futures, Mapping):
        futures_quality = str(futures.get("quality_status") or "ok").strip().lower()
        futures_cached = bool(futures.get("cached"))
    if temporal != "aligned":
        return "partial"
    if snapshot.get("notes"):
        return "partial"
    if futures_quality != "ok" or futures_cached:
        return "partial"
    return "ok"


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
        source_date = ""
        try:
            last_index = hist.index[-1]
            date_value = getattr(last_index, "date", None)
            date_value = date_value() if callable(date_value) else date_value
            if isinstance(date_value, dt.datetime):
                date_value = date_value.date()
            if isinstance(date_value, dt.date):
                source_date = date_value.isoformat()
            else:
                candidate = str(date_value or last_index).strip()[:10]
                dt.date.fromisoformat(candidate)
                source_date = candidate
        except Exception:
            source_date = ""
        return {
            "price": close,
            "prev_close": prev,
            "pct_change": ((close - prev) / prev * 100.0) if prev else 0.0,
            "volume": volume,
            "source_date": source_date,
        }
    except Exception:
        logger.exception("yfinance 抓 %s 失敗", symbol)
        return None


def _fetch_fx_usdtwd(yf_module: Any, logger: logging.Logger) -> Optional[float]:
    """yfinance 的 TWD=X 是 USD/TWD (即 1 USD 換多少 TWD)。"""
    price, _ = _fetch_fx_usdtwd_with_meta(yf_module, logger)
    return price


def _fetch_fx_usdtwd_with_meta(
    yf_module: Any,
    logger: logging.Logger,
) -> Tuple[Optional[float], str]:
    """回傳 USD/TWD 與 yfinance 實際觀測日。"""
    if yf_module is None:
        return None, ""
    q = _safe_quote(yf_module, "TWD=X", logger)
    if q and q["price"] > 0:
        return q["price"], str(q.get("source_date") or "")
    return None, ""


# ----------------------------------------------------------------------
# 取台股收盤 (用於 ADR 溢價計算)
# ----------------------------------------------------------------------


# TWSE OpenAPI：全市場個股當日收盤均價 (含收盤價)。免登入、穩定。
URL_TWSE_STOCK_DAY_AVG = (
    "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_AVG_ALL"
)

# 程序內快取：避免每檔 ADR 都重打一次全市場列表。
_TW_CLOSE_CACHE: Dict[str, float] = {}
_TW_CLOSE_CACHE_LOADED = False


def _load_twse_close_map(logger: logging.Logger) -> Dict[str, float]:
    """抓 TWSE 全市場收盤價對照表 {ticker: close}，程序內只抓一次。"""
    global _TW_CLOSE_CACHE_LOADED
    if _TW_CLOSE_CACHE_LOADED:
        return _TW_CLOSE_CACHE
    loaded_ok = False
    try:
        import requests  # 延遲 import，維持模組輕量

        resp = requests.get(
            URL_TWSE_STOCK_DAY_AVG,
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        )
        if resp.status_code == 200:
            for row in resp.json() or []:
                code = str(row.get("Code") or "").strip()
                close_raw = str(row.get("ClosingPrice") or "").replace(",", "").strip()
                if not code or not close_raw or close_raw in ("-", "--"):
                    continue
                try:
                    _TW_CLOSE_CACHE[code] = float(close_raw)
                except ValueError:
                    continue
            loaded_ok = bool(_TW_CLOSE_CACHE)
        else:
            logger.warning("TWSE 收盤價 OpenAPI HTTP %d", resp.status_code)
    except Exception:
        logger.exception("TWSE 收盤價 OpenAPI 抓取失敗")
    # 空回應或錯誤回應不可視為成功，下一次呼叫仍應有機會重試。
    if loaded_ok:
        _TW_CLOSE_CACHE_LOADED = True
    return _TW_CLOSE_CACHE


def _fetch_tw_close(ticker: str, logger: logging.Logger) -> Optional[float]:
    """先試 yfinance (例：2330.TW)；失敗時改用 TWSE OpenAPI 收盤價。"""
    price, _, _ = _fetch_tw_close_with_meta(ticker, logger)
    return price


def _fetch_tw_close_with_meta(
    ticker: str,
    logger: logging.Logger,
) -> Tuple[Optional[float], str, str]:
    """回傳台股現貨、觀測日與來源，供 ADR 溢價追溯。"""
    yf = _import_yf()
    if yf is not None:
        for suffix in (".TW", ".TWO"):
            q = _safe_quote(yf, f"{ticker}{suffix}", logger)
            if q and q["price"] > 0:
                return (
                    q["price"],
                    str(q.get("source_date") or ""),
                    f"yfinance:{ticker}{suffix}",
                )
    # TWSE OpenAPI fallback (免登入，yfinance 被限流時仍可取得當日收盤)
    close_map = _load_twse_close_map(logger)
    close = close_map.get(ticker)
    if close and close > 0:
        # STOCK_DAY_AVG_ALL does not expose a reliable per-row observation
        # date in every response; leave it blank rather than claiming that
        # fetch time is the market observation date.
        return close, "", "twse:STOCK_DAY_AVG_ALL"
    return None, "", ""


# ----------------------------------------------------------------------
# 台指期 (TX) 近月價差 — 期貨領先指標
# ----------------------------------------------------------------------

# 期交所 OpenAPI：期貨每日行情 (免登入、穩定 JSON)。
URL_TAIFEX_DAILY_FUT = "https://openapi.taifex.com.tw/v1/DailyMarketReportFut"


def _to_float_loose(x: Any) -> float:
    try:
        s = str(x).replace(",", "").strip()
        if not s or s in ("-", "--"):
            return 0.0
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def _taifex_rows(response: Any) -> List[Dict[str, Any]]:
    """讀取 TAIFEX 新舊格式：JSON 或 UTF-8 CSV。"""
    try:
        rows = response.json()
    except Exception:
        raw = getattr(response, "content", b"")
        if isinstance(raw, bytes):
            text = raw.decode("utf-8-sig", errors="replace")
        else:
            text = str(getattr(response, "text", raw) or "")
        reader = csv.DictReader(io.StringIO(text))
        rows = [dict(row) for row in reader if row]
    return _normalize_taifex_rows(rows)


def _normalize_taifex_rows(rows: Any) -> List[Dict[str, Any]]:
    if not isinstance(rows, list):
        return []

    # TAIFEX 的 CSV 欄位是中文，JSON 版本則使用英文欄位；統一成
    # 下游既有的英文模型，避免供應商換格式時整條來源失效。
    normalized: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        normalized.append({
            "Contract": row.get("Contract") or row.get("契約代號"),
            "ContractMonth(Week)": (
                row.get("ContractMonth(Week)")
                or row.get("ContractMonth")
                or row.get("到期月份(週別)")
            ),
            "Last": row.get("Last") or row.get("最後成交價"),
            "SettlementPrice": row.get("SettlementPrice") or row.get("結算價"),
            "OpenInterest": row.get("OpenInterest") or row.get("未沖銷契約數"),
            "Date": row.get("Date") or row.get("日期"),
        })
    return normalized


def _taifex_rows_from_bytes(raw: bytes) -> List[Dict[str, Any]]:
    """解析已保存的 exact raw archive，不依賴目前 TAIFEX 網站可用性。"""
    try:
        rows = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        text = raw.decode("utf-8-sig", errors="replace")
        rows = [dict(row) for row in csv.DictReader(io.StringIO(text)) if row]
    return _normalize_taifex_rows(rows)


def _front_tx_row(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """取 TX 月合約近月列，排除週合約與其他商品。"""
    tx_rows = []
    for row in rows:
        if str(row.get("Contract") or "").strip().upper() != "TX":
            continue
        month = str(row.get("ContractMonth(Week)") or row.get("ContractMonth") or "").strip()
        if len(month) == 6 and month.isdigit():
            tx_rows.append(row)
    if not tx_rows:
        return None
    tx_rows.sort(
        key=lambda row: str(
            row.get("ContractMonth(Week)") or row.get("ContractMonth") or ""
        )
    )
    return tx_rows[0]


def _build_futures_basis(
    front: Dict[str, Any],
    spot_price: float,
    *,
    source_document_id: str = "",
    quality_status: str = "ok",
    cached: bool = False,
    note: str = "",
) -> Optional[FuturesBasis]:
    month = str(front.get("ContractMonth(Week)") or front.get("ContractMonth") or "").strip()
    futures_price = _to_float_loose(front.get("Last"))
    if futures_price <= 0:
        futures_price = _to_float_loose(front.get("SettlementPrice"))
    if futures_price <= 0:
        return None
    fb = FuturesBasis(
        contract="TX",
        contract_month=month,
        futures_price=futures_price,
        spot_price=round(spot_price, 2),
        open_interest=_to_float_loose(front.get("OpenInterest")),
        asof_date=str(front.get("Date") or ""),
        source_document_id=source_document_id,
        quality_status=quality_status,
        cached=cached,
        note=note,
    )
    if spot_price and spot_price > 0:
        fb.basis = round(futures_price - spot_price, 2)
        fb.basis_pct = round(fb.basis / spot_price * 100.0, 3)
        if fb.basis > 1:
            fb.state = "正價差"
        elif fb.basis < -1:
            fb.state = "逆價差"
        else:
            fb.state = "平水"
    else:
        fb.state = "unknown"
    return fb


def _materialize_taifex_basis_research(
    fb: FuturesBasis,
    *,
    root: Optional[Path],
    logger: logging.Logger,
) -> None:
    if root is None or not fb.source_document_id:
        return
    try:
        from bot.taifex_research import materialize_taifex_slice

        research_result = materialize_taifex_slice(
            root=Path(root),
            source_document_id=fb.source_document_id,
            contract=fb.contract,
            contract_month=fb.contract_month,
            futures_price=fb.futures_price,
            spot_price=fb.spot_price,
            basis=fb.basis,
            basis_pct=fb.basis_pct,
            open_interest=fb.open_interest,
            state=fb.state,
            asof_date=fb.asof_date,
        )
        if research_result.status == "failed":
            logger.warning("TAIFEX research slice 失敗: %s", research_result.error)
    except Exception:
        logger.exception("TAIFEX 研究切片寫入失敗")


def _load_cached_tx_futures_basis(
    spot_price: float,
    *,
    root: Optional[Path],
    logger: logging.Logger,
) -> Optional[FuturesBasis]:
    """live TAIFEX 失敗時，從最近可驗證 raw document 回放。"""
    if root is None:
        return None
    try:
        project_root = Path(root).resolve()
        with SourceRegistry(project_root) as registry:
            documents = registry.db.list_source_documents(
                source="taifex_daily_fut", kind="http_response", limit=100,
            )
            for source in documents:
                if source.quality_status not in {"ok", "partial"}:
                    continue
                path = resolve_source_document_path(project_root, source)
                if not path.is_file():
                    continue
                raw = path.read_bytes()
                if source.content_hash and hashlib.sha256(raw).hexdigest() != source.content_hash:
                    logger.warning("跳過 hash 不符的 TAIFEX cache: %s", source.document_id)
                    continue
                front = _front_tx_row(_taifex_rows_from_bytes(raw))
                if front is None:
                    continue
                fb = _build_futures_basis(
                    front,
                    spot_price,
                    source_document_id=source.document_id,
                    quality_status="partial",
                    cached=True,
                    note="TAIFEX live endpoint unavailable; using exact local raw archive",
                )
                if fb is not None:
                    _materialize_taifex_basis_research(fb, root=project_root, logger=logger)
                    return fb
    except Exception:
        logger.exception("TAIFEX exact cache fallback 失敗")
    return None


def fetch_tx_futures_basis(
    spot_price: float,
    *,
    logger: Optional[logging.Logger] = None,
    root: Optional[Path] = None,
) -> Optional[FuturesBasis]:
    """抓台指期 (TX) 近月行情並計算與現貨 (加權指) 的正逆價差。

    Args:
        spot_price: 加權指數現貨點數 (通常取自 macro snapshot 的 ^TWII)。

    回傳 ``None`` 表示期交所抓取失敗或無有效近月資料。
    """
    log = logger or get_logger("macro")
    try:
        import requests

        resp = requests.get(
            URL_TAIFEX_DAILY_FUT,
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        )
        if resp.status_code != 200:
            log.warning("TAIFEX 期貨行情 HTTP %d", resp.status_code)
            return _load_cached_tx_futures_basis(spot_price, root=root, logger=log)
        rows = _taifex_rows(resp)
    except Exception:
        log.exception("TAIFEX 期貨行情抓取失敗")
        return _load_cached_tx_futures_basis(spot_price, root=root, logger=log)
    if not isinstance(rows, list):
        return _load_cached_tx_futures_basis(spot_price, root=root, logger=log)

    # 只取大台指 TX 的「月合約」(ContractMonth(Week) 為 6 位數，排除週合約 W)
    tx_rows: List[Dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        if str(r.get("Contract") or "").strip().upper() != "TX":
            continue
        cm = str(r.get("ContractMonth(Week)") or r.get("ContractMonth") or "").strip()
        if len(cm) == 6 and cm.isdigit():
            tx_rows.append(r)
    if not tx_rows:
        return _load_cached_tx_futures_basis(spot_price, root=root, logger=log)

    # 近月 = 最小 (最近) 的合約月
    tx_rows.sort(key=lambda r: str(r.get("ContractMonth(Week)") or ""))
    front = tx_rows[0]
    fb = _build_futures_basis(front, spot_price)
    if fb is None:
        return _load_cached_tx_futures_basis(spot_price, root=root, logger=log)
    fut_price = fb.futures_price

    if root is not None:
        try:
            raw_content = getattr(resp, "content", b"")
            if not isinstance(raw_content, bytes):
                raw_content = str(getattr(resp, "text", raw_content) or "").encode("utf-8")
            raw_is_json = "json" in str(resp.headers.get("content-type", "")).lower()
            suffix = "json" if raw_is_json else "csv"
            mime_type = "application/json" if raw_is_json else "text/csv; charset=utf-8"
            with SourceRegistry(root) as registry:
                document = registry.capture_document(
                    "taifex_daily_fut",
                    raw_content,
                    source_url=URL_TAIFEX_DAILY_FUT,
                    source_ref=f"taifex:{fb.asof_date}:{fb.contract_month}",
                    title=f"TAIFEX TX daily futures {fb.asof_date}",
                    kind="http_response",
                    suffix=suffix,
                    mime_type=mime_type,
                    as_of=fb.asof_date,
                    metadata={"format": "json" if raw_is_json else "csv"},
                )
                fb.source_document_id = document.document_id
        except Exception:
            log.exception("TAIFEX 原始回應 provenance 保存失敗")
    if spot_price and spot_price > 0:
        fb.basis = round(fut_price - spot_price, 2)
        fb.basis_pct = round(fb.basis / spot_price * 100.0, 3)
        if fb.basis > 1:
            fb.state = "正價差"
        elif fb.basis < -1:
            fb.state = "逆價差"
        else:
            fb.state = "平水"
    else:
        fb.state = "unknown"
    _materialize_taifex_basis_research(fb, root=root, logger=log)
    return fb


# ----------------------------------------------------------------------
# 快取
# ----------------------------------------------------------------------


def _cache_path(date: dt.date, root: Optional[Path]) -> Path:
    return (root or Path.cwd()) / "data" / "macro" / f"macro_{date.isoformat()}.json"


def _load_cache(date: dt.date, root: Optional[Path]) -> Optional[MacroSnapshot]:
    p = _cache_path(date, root)
    restore_file_from_cloud(p, root=root)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        snap = MacroSnapshot(
            fetched_at=d.get("fetched_at", ""),
            asof_date=d.get("asof_date", date.isoformat()),
            usdtwd=(float(d["usdtwd"]) if d.get("usdtwd") is not None else None),
            notes=list(d.get("notes", []) or []),
            cached=True,
            source_dates=dict(d.get("source_dates") or {}),
            source_date_status=str(d.get("source_date_status") or "unknown"),
            source_document_id=str(d.get("source_document_id") or ""),
        )
        for sym, info in (d.get("indices") or {}).items():
            snap.indices[sym] = IndexQuote(**info)
        for sym, info in (d.get("stocks") or {}).items():
            snap.stocks[sym] = StockQuote(**info)
        for p_info in (d.get("adr_premiums") or []):
            snap.adr_premiums.append(AdrPremium(**p_info))
        fb = d.get("futures_basis")
        if isinstance(fb, dict):
            try:
                snap.futures_basis = FuturesBasis(**fb)
            except TypeError:
                snap.futures_basis = None
        # 舊快取與可重播的 raw archive 刻意保持相同內容；若快取 JSON
        # 沒有內嵌 provenance ID，從 manifest 以 exact source_ref 找回，
        # 不用 URL、標題或內容相似度猜測來源。
        if not snap.source_document_id:
            try:
                registry = SourceRegistry(root or Path.cwd())
                try:
                    source_ref = f"macro:{snap.asof_date or date.isoformat()}"
                    candidates = [
                        row for row in registry.db.list_source_documents(
                            source="market_macro", kind="snapshot", limit=200
                        )
                        if row.source_ref == source_ref
                    ]
                    if candidates:
                        snap.source_document_id = candidates[0].document_id
                finally:
                    registry.close()
            except Exception:
                get_logger("macro").debug("macro cache provenance 解析失敗", exc_info=True)
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
        "futures_basis": asdict(snap.futures_basis) if snap.futures_basis else None,
        "source_dates": dict(snap.source_dates),
        "source_date_status": snap.source_date_status,
        # raw/cache payload 不內嵌 manifest identity；載入時以 exact
        # source_ref 回查，避免 raw archive 與 derived cache 分叉。
        "source_document_id": "",
    }
    payload_text = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        with SourceRegistry(root or Path.cwd()) as registry:
            document = registry.capture_document(
                "market_macro",
                payload_text.encode("utf-8"),
                source_url="yfinance://macro",
                source_ref=f"macro:{snap.asof_date or asof.isoformat()}",
                title=f"Market macro snapshot {snap.asof_date or asof.isoformat()}",
                kind="snapshot",
                suffix="json",
                mime_type="application/json",
                as_of=snap.asof_date or asof.isoformat(),
                retrieved_at=snap.fetched_at,
                quality_status=macro_quality_status(payload),
                metadata={
                    "source_dates": snap.source_dates,
                    "source_date_status": snap.source_date_status,
                    "quality_status": macro_quality_status(payload),
                },
            )
            snap.source_document_id = document.document_id
            try:
                from bot.macro_research import materialize_macro_snapshot_slice

                research_result = materialize_macro_snapshot_slice(
                    root=root or Path.cwd(),
                    source_document_id=document.document_id,
                    snapshot=payload,
                    db=registry.db,
                )
                if research_result.status == "failed":
                    get_logger("macro").warning(
                        "macro research slice 失敗: %s", research_result.error,
                    )
            except Exception:
                get_logger("macro").exception("macro research slice 寫入失敗")
    except Exception:
        get_logger("macro").debug("macro raw provenance 寫入失敗", exc_info=True)
    # write_bytes avoids Windows newline translation; the cache must be the
    # exact byte replay of the immutable raw archive.
    p.write_bytes(payload_text.encode("utf-8"))
    mirror_file_to_cloud(p, root=root)
    return p


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------


def fetch_macro_snapshot(
    *,
    root: Optional[Path] = None,
    force_refresh: bool = False,
    use_cache: bool = True,
    cache_only: bool = False,
    logger: Optional[logging.Logger] = None,
) -> MacroSnapshot:
    """抓 (或回傳快取的) 美股 + 加權 + ADR 溢價總覽。

    Args:
        cache_only: True 時只讀本地/雲端鏡像快取；快取不存在就回空 snapshot，
            不呼叫 yfinance 或外部 OpenAPI。Dashboard 的快速 local-first 路徑使用。
    """
    log = logger or get_logger("macro")
    today = now_tw().date()

    if use_cache and not force_refresh:
        cached = _load_cache(today, root)
        if cached is not None:
            log.info("macro 命中快取 %s", today)
            return cached
        if cache_only:
            log.info("macro cache-only miss %s", today)
            return MacroSnapshot(
                fetched_at=now_tw().isoformat(timespec="seconds"),
                asof_date=today.isoformat(),
                notes=["本地 macro 快取不存在；本次略過外部抓取"],
            )

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
            source_date=q.get("source_date", ""),
        )
        if q.get("source_date"):
            snap.source_dates[sym] = q["source_date"]

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
            source_date=q.get("source_date", ""),
        )
        if q.get("source_date"):
            snap.source_dates[sym] = q["source_date"]

    # ---- 匯率 ----
    fx_source_date = ""
    snap.usdtwd, fx_source_date = _fetch_fx_usdtwd_with_meta(yf, log)
    if fx_source_date:
        snap.source_dates["USDTWD"] = fx_source_date
    if snap.usdtwd is None:
        notes.append("USDTWD unavailable；不使用估計匯率")

    # ---- ADR 溢價 ----
    for adr_sym, ratio in ADR_RATIOS.items():
        sq = snap.stocks.get(adr_sym)
        if sq is None or not sq.parent_tw_ticker:
            continue
        tw_price, tw_source_date, tw_source = _fetch_tw_close_with_meta(
            sq.parent_tw_ticker, log,
        )
        if not tw_price or tw_price <= 0:
            continue
        fair_tw = sq.price * snap.usdtwd / ratio if ratio and snap.usdtwd else 0
        if fair_tw <= 0:
            continue
        prem_pct = (fair_tw - tw_price) / tw_price * 100.0
        adr_source_date = str(sq.source_date or "")
        source_date_map = {
            f"{adr_sym}:adr": adr_source_date,
            f"{adr_sym}:tw": tw_source_date,
            f"{adr_sym}:fx": fx_source_date,
        }
        temporal_status = classify_source_dates(source_date_map)
        snap.source_dates.update(source_date_map)
        observed_dates = {
            label: value for label, value in source_date_map.items() if value
        }
        if len(set(observed_dates.values())) > 1:
            notes.append(
                f"ADR {adr_sym} 來源日期未對齊: "
                + ", ".join(f"{k}={v}" for k, v in observed_dates.items())
            )
        if not tw_source_date:
            notes.append(
                f"ADR {adr_sym} 台股現貨來源 {tw_source} 未提供可確認的觀測日"
            )
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
            adr_source_date=adr_source_date,
            tw_source=tw_source,
            tw_source_date=tw_source_date,
            fx_source_date=fx_source_date,
            temporal_status=temporal_status,
        ))

    # ---- 台指期近月價差 (期貨領先指標) ----
    try:
        twii = snap.indices.get("^TWII")
        spot = twii.price if twii else 0.0
        fb = fetch_tx_futures_basis(spot, logger=log, root=root)
        if fb is not None:
            snap.futures_basis = fb
            if fb.cached:
                notes.append(
                    f"TAIFEX live unavailable；使用 exact raw cache ({fb.asof_date})，"
                    "已標記 partial，不視為即時行情"
                )
        else:
            notes.append("台指期價差抓取失敗")
    except Exception:
        log.exception("台指期價差計算失敗")

    snap.source_date_status = classify_source_dates(snap.source_dates)
    if snap.source_date_status == "mixed":
        notes.append("macro 來源日期混合；請依 source_dates 判斷是否可直接比較")
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
        "futures_basis": asdict(s.futures_basis) if s.futures_basis else None,
        "cached": s.cached,
        "source_dates": dict(s.source_dates),
        "source_date_status": s.source_date_status,
        "source_document_id": s.source_document_id,
    }


def load_supply_chain(root: Optional[Path] = None) -> Dict[str, Any]:
    """讀取 data/supply_chain.json (美股 → 台股供應鏈對照)。"""
    p = (root or Path.cwd()) / "data" / "supply_chain.json"
    restore_file_from_cloud(p, root=root)
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
    mirror_file_to_cloud(p, root=root)
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
    "FuturesBasis",
    "IndexQuote",
    "MacroSnapshot",
    "StockQuote",
    "fetch_macro_snapshot",
    "fetch_tx_futures_basis",
    "load_supply_chain",
    "macro_to_dict",
    "related_us_stocks_for_tw",
    "save_supply_chain",
]
