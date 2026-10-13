"""chips_fetcher -- 自動從 TWSE 公開 API 抓籌碼面資料。

涵蓋：
* 三大法人買賣超 (T86 / 上市)
* 借券賣出餘額 (TWT93U)
* 融資融券餘額 (MI_MARGN)
* 鉅額交易 (BFT41U)

所有資料以日為單位快取於 data/chips/<date>/<endpoint>.json，
重複呼叫不會打爆 TWSE，缺檔才會 HTTP fetch。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from bot.cloud_file_cache import mirror_file_to_cloud, restore_file_from_cloud
from bot.source_registry import SourceRegistry
from bot.utils import get_logger, mk_folder, now_tw


# ----------------------------------------------------------------------
# Endpoint 集中表
# ----------------------------------------------------------------------

# 採用較穩定的 rwd 路徑；若 TWSE 端點異動，可在這裡集中替換。
ENDPOINTS = {
    # 三大法人 (上市) — 每檔個股的外資/投信/自營商買賣超
    "qfii_daily": (
        "https://www.twse.com.tw/rwd/zh/fund/T86"
        "?date={date_compact}&selectType=ALLBUT0999&response=json"
    ),
    # 借券賣出餘額
    "borrow_balance": (
        "https://www.twse.com.tw/rwd/zh/marginTrading/TWT93U"
        "?date={date_compact}&response=json"
    ),
    # 融資融券餘額
    "margin_balance": (
        "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
        "?date={date_compact}&selectType=ALL&response=json"
    ),
    # 鉅額交易
    "block_trade": (
        "https://www.twse.com.tw/rwd/zh/block/BFIAUU"
        "?date={date_compact}&selectType=S&response=json"
    ),
}

# TWSE 偶爾會保留另一個公開報表路徑；主端點回 HTML/404 時才嘗試，
# 避免端點改版造成整個籌碼流程靜默失效。
ENDPOINT_FALLBACKS = {
    "borrow_balance": (
        "https://www.twse.com.tw/exchangeReport/TWT93U"
        "?date={date_compact}&response=json",
    ),
}

# 上櫃 (TPEx) 端點 — 使用 YYYY/MM/DD 日期格式，回傳 {tables:[{fields,data}]}
ENDPOINTS_TPEX = {
    # 三大法人買賣明細 (sect=EW 為一般股票，含 ETF)
    "tpex_insti": (
        "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade"
        "?type=Daily&sect=EW&date={date_slash}&response=json"
    ),
    # 融資融券餘額
    "tpex_margin": (
        "https://www.tpex.org.tw/www/zh-tw/margin/balance"
        "?date={date_slash}&response=json"
    ),
}


# ----------------------------------------------------------------------
# 資料模型
# ----------------------------------------------------------------------


@dataclass
class ChipDailyRow:
    """個股單日籌碼摘要。"""

    ticker: str
    name: str = ""
    foreign_net: float = 0.0          # 外資 + 陸資 淨買賣超 (張)
    investment_trust_net: float = 0.0
    dealer_net: float = 0.0
    margin_balance: float = 0.0       # 融資餘額 (張)
    short_balance: float = 0.0        # 融券餘額 (張)
    borrow_balance: float = 0.0       # 借券賣出餘額 (張)


@dataclass
class ChipSummary:
    """個股近 N 日的籌碼面累計摘要 (供 logic_check 用)。"""

    ticker: str
    days: int = 5
    foreign_net: float = 0.0
    investment_trust_net: float = 0.0
    dealer_net: float = 0.0
    margin_buy_change_pct: float = 0.0
    short_borrow_change_pct: float = 0.0   # 借券賣出餘額變動率 %
    short_sell_change_pct: float = 0.0     # 融券餘額變動率 %
    block_trade_net: float = 0.0
    rows: List[ChipDailyRow] = field(default_factory=list)
    # 摘要可能由多個 endpoint／多個交易日組成，不能壓成單一來源 ID。
    end_date: str = ""
    source_document_ids: List[str] = field(default_factory=list)
    source_date_status: str = "unknown"


# ----------------------------------------------------------------------
# 快取 IO
# ----------------------------------------------------------------------


def _cache_path(name: str, date: dt.date, root: Optional[Path] = None) -> Path:
    base = root or Path.cwd()
    return base / "data" / "chips" / date.isoformat() / f"{name}.json"


_CACHE_ENDPOINT_NAMES = (
    "qfii_daily", "margin_balance", "borrow_balance", "block_trade",
    "tpex_insti", "tpex_margin",
)

# 同一個研究執行可能會為多個 ticker 反覆請求同一日期的全市場端點。
# 這裡只保存 process 內的結果（包含明確失敗的 None），不把失敗回應寫成
# 磁碟快取；因此不會把上游錯誤誤當成有效資料，也能避免重複打 API。
_SESSION_ENDPOINT_CACHE_ATTR = "_streamsuite_chip_endpoint_cache"

# ``build_chip_summary`` 會為不同 ticker 建立不同 requests.Session；若上游在
# 同一 process／日期回傳無 schema 或 redirect，僅使用 session cache 仍會重試
# 數百次。失敗只在 process 記憶體中短暫抑制，不寫入磁碟，也不會把失敗當成
# 有效資料。TTL 到期後新的呼叫仍可重新驗證上游是否恢復。
_PROCESS_FAILURE_CACHE_TTL_SECONDS = 300.0
_PROCESS_FAILURE_CACHE_MAX_ENTRIES = 1024
_PROCESS_FAILURE_CACHE: Dict[tuple, float] = {}


def _session_endpoint_cache(session: Any) -> Dict[tuple, Optional[Dict[str, Any]]]:
    """取得單一 session 的短生命週期 endpoint cache。"""
    cache = getattr(session, _SESSION_ENDPOINT_CACHE_ATTR, None)
    if not isinstance(cache, dict):
        cache = {}
        try:
            setattr(session, _SESSION_ENDPOINT_CACHE_ATTR, cache)
        except Exception:
            # 極少數不可設定屬性的測試／第三方 session 仍可正常抓取，
            # 只是退回原本的每次請求行為。
            return {}
    return cache


def _process_failure_cache_active(cache_key: tuple) -> bool:
    failed_at = _PROCESS_FAILURE_CACHE.get(cache_key)
    if failed_at is None:
        return False
    if time.monotonic() - failed_at < _PROCESS_FAILURE_CACHE_TTL_SECONDS:
        return True
    _PROCESS_FAILURE_CACHE.pop(cache_key, None)
    return False


def _remember_process_failure(cache_key: tuple) -> None:
    """記錄失敗並清理過期／過量項目，避免長駐 scheduler 無限成長。"""
    now = time.monotonic()
    expired = [
        key for key, failed_at in _PROCESS_FAILURE_CACHE.items()
        if now - failed_at >= _PROCESS_FAILURE_CACHE_TTL_SECONDS
    ]
    for key in expired:
        _PROCESS_FAILURE_CACHE.pop(key, None)
    if len(_PROCESS_FAILURE_CACHE) >= _PROCESS_FAILURE_CACHE_MAX_ENTRIES:
        oldest = min(_PROCESS_FAILURE_CACHE, key=_PROCESS_FAILURE_CACHE.get)
        _PROCESS_FAILURE_CACHE.pop(oldest, None)
    _PROCESS_FAILURE_CACHE[cache_key] = now


def latest_cached_chip_date(
    requested: dt.date,
    *,
    root: Optional[Path] = None,
) -> Optional[dt.date]:
    """回傳不晚於 requested 的最近籌碼快取日；只讀檔案系統。"""
    base = (root or Path.cwd()) / "data" / "chips"
    if not base.exists():
        return None
    candidates: List[dt.date] = []
    for child in base.iterdir():
        if not child.is_dir():
            continue
        try:
            day = dt.date.fromisoformat(child.name)
        except ValueError:
            continue
        if day > requested:
            continue
        if any((child / f"{name}.json").is_file() for name in _CACHE_ENDPOINT_NAMES):
            candidates.append(day)
    return max(candidates) if candidates else None


def cached_chip_source_document_ids(
    dates: List[dt.date],
    *,
    root: Optional[Path] = None,
) -> List[str]:
    """以 exact source/ref 對應已保存的籌碼 raw manifest。"""
    if not dates:
        return []
    registry = SourceRegistry(root or Path.cwd())
    try:
        date_text = {day.isoformat() for day in dates}
        source_names = {
            *(f"chips_twse_{name}" for name in _CACHE_ENDPOINT_NAMES[:4]),
            *(f"chips_tpex_{name}" for name in _CACHE_ENDPOINT_NAMES[4:]),
        }
        rows = registry.db.list_source_documents(limit=5000)
        ids = [
            row.document_id
            for row in rows
            if row.source in source_names
            and str(row.source_ref).rsplit(":", 1)[-1] in date_text
        ]
        return list(dict.fromkeys(ids))
    finally:
        registry.close()


def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-TW,zh;q=0.9",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.tpex.org.tw/",
    })
    return s


def _capture_chip_response(
    *,
    source: str,
    content: bytes,
    url: str,
    date: dt.date,
    name: str,
    root: Optional[Path],
    logger: logging.Logger,
) -> None:
    """保存籌碼 API 原始回應；provenance 失敗不阻斷既有快取流程。"""
    payload = content or b""
    if not payload:
        return
    try:
        SourceRegistry(root or Path.cwd()).capture_document(
            source,
            payload,
            source_url=url,
            source_ref=f"{name}:{date.isoformat()}",
            title=f"{name} {date.isoformat()}",
            kind="api_response",
            suffix="json",
            mime_type="application/json",
            as_of=date.isoformat(),
            metadata={"endpoint": name},
        )
    except Exception:
        logger.debug("%s raw provenance 寫入失敗", name, exc_info=True)


def _fetch_endpoint(
    name: str,
    date: dt.date,
    *,
    session: Optional[requests.Session] = None,
    root: Optional[Path] = None,
    use_cache: bool = True,
    cache_only: bool = False,
    logger: Optional[logging.Logger] = None,
) -> Optional[Dict[str, Any]]:
    log = logger or get_logger("chips")
    effective_date = date
    if cache_only and not any(
        _cache_path(name, date, root).is_file() for name in _CACHE_ENDPOINT_NAMES
    ):
        effective_date = latest_cached_chip_date(date, root=root) or date
    date = effective_date
    sess = session or _new_session()
    memory_cache = _session_endpoint_cache(sess)
    cache_key = (
        name,
        date.isoformat(),
        str((root or Path.cwd()).resolve()),
        bool(use_cache),
        bool(cache_only),
    )
    path = _cache_path(name, date, root)
    if use_cache:
        restore_file_from_cloud(path, root=root)
    if use_cache and path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                memory_cache[cache_key] = data
                return data
        except Exception:
            pass
    if cache_key in memory_cache:
        return memory_cache[cache_key]
    if _process_failure_cache_active(cache_key):
        memory_cache[cache_key] = None
        return None
    if cache_only:
        memory_cache[cache_key] = None
        return None
    urls = [ENDPOINTS[name]] + list(ENDPOINT_FALLBACKS.get(name, ()))
    for url_template in urls:
        url = url_template.format(date_compact=date.strftime("%Y%m%d"))
        try:
            resp = sess.get(url, timeout=20)
            if resp.status_code != 200:
                log.warning("%s HTTP %d (%s)", name, resp.status_code, url)
                continue
            try:
                data = resp.json()
            except Exception:
                log.warning("%s 回應不是有效 JSON (%s)", name, url)
                continue
            if not isinstance(data, dict):
                log.warning("%s JSON 根節點不是 object (%s)", name, url)
                continue
            fields = data.get("fields") or []
            rows = data.get("data") or []
            tables = data.get("tables") or []
            if not ((isinstance(fields, list) and isinstance(rows, list) and fields and rows) or tables):
                log.warning("%s 回應缺少 fields/data/tables，拒絕快取", name)
                continue
            _capture_chip_response(
                source=f"chips_twse_{name}",
                content=bytes(getattr(resp, "content", b"") or b""),
                url=url,
                date=date,
                name=name,
                root=root,
                logger=log,
            )
            mk_folder(str(path.parent))
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            mirror_file_to_cloud(path, root=root)
            memory_cache[cache_key] = data
            time.sleep(0.5)  # 對 TWSE 友善
            return data
        except Exception:
            log.exception("%s 抓取失敗 (%s)", name, url)
    _remember_process_failure(cache_key)
    memory_cache[cache_key] = None
    return None


# ----------------------------------------------------------------------
# 解析：T86 三大法人
# ----------------------------------------------------------------------


def _parse_t86(data: Dict[str, Any]) -> Dict[str, ChipDailyRow]:
    out: Dict[str, ChipDailyRow] = {}
    fields = data.get("fields") or []
    rows = data.get("data") or []
    if not fields or not rows:
        return out

    def col_idx(*keywords: str) -> int:
        for i, f in enumerate(fields):
            for kw in keywords:
                if kw in f:
                    return i
        return -1

    idx_code = col_idx("證券代號")
    idx_name = col_idx("證券名稱")
    idx_foreign = col_idx("外資及陸資買賣超股數", "外陸資買賣超股數", "外資買賣超股數")
    idx_trust = col_idx("投信買賣超股數")
    idx_dealer = col_idx("自營商買賣超股數")

    for row in rows:
        if idx_code < 0 or idx_code >= len(row):
            continue
        ticker = str(row[idx_code]).strip()
        if not ticker:
            continue
        try:
            foreign = _to_float(row[idx_foreign]) if idx_foreign >= 0 else 0.0
            trust = _to_float(row[idx_trust]) if idx_trust >= 0 else 0.0
            dealer = _to_float(row[idx_dealer]) if idx_dealer >= 0 else 0.0
        except Exception:
            continue
        out[ticker] = ChipDailyRow(
            ticker=ticker,
            name=str(row[idx_name]).strip() if 0 <= idx_name < len(row) else "",
            foreign_net=foreign / 1000.0,  # 換成「張」
            investment_trust_net=trust / 1000.0,
            dealer_net=dealer / 1000.0,
        )
    return out


def _parse_margin(data: Dict[str, Any]) -> Dict[str, tuple[float, float]]:
    """回傳 {ticker: (margin_balance, short_balance)} (張)。"""
    out: Dict[str, tuple[float, float]] = {}
    fields = data.get("fields") or []
    rows = data.get("data") or []
    if not fields or not rows:
        # MI_MARGN 回傳結構為 {tables: [{fields, data}]}
        tables = data.get("tables") or []
        for t in tables:
            f2 = t.get("fields") or []
            r2 = t.get("data") or []
            if "融資餘額" in "".join(f2):
                fields = f2
                rows = r2
                break
    if not fields or not rows:
        return out

    def idx(*kw: str) -> int:
        for i, f in enumerate(fields):
            for k in kw:
                if k in f:
                    return i
        return -1

    i_code = idx("股票代號", "證券代號")
    i_margin = idx("融資今日餘額", "融資餘額")
    i_short = idx("融券今日餘額", "融券餘額")
    if i_code < 0:
        return out
    for row in rows:
        if i_code >= len(row):
            continue
        ticker = str(row[i_code]).strip()
        if not ticker:
            continue
        m = _to_float(row[i_margin]) if i_margin >= 0 else 0.0
        s = _to_float(row[i_short]) if i_short >= 0 else 0.0
        out[ticker] = (m, s)
    return out


def _parse_borrow(data: Dict[str, Any]) -> Dict[str, float]:
    """回傳 {ticker: 借券賣出餘額(張)}。"""
    out: Dict[str, float] = {}
    fields = [str(field) for field in (data.get("fields") or [])]
    rows = data.get("data") or []
    if not fields or not rows:
        return out

    def idx(*kw: str) -> int:
        for i, f in enumerate(fields):
            for k in kw:
                if k in f:
                    return i
        return -1

    i_code = idx("股票代號", "證券代號", "代號")
    # TWT93U 目前有兩組「前日餘額/當日餘額」：前半是借券賣出，
    # 後半才是借券賣出餘額。優先選明確欄名，否則取最後一個餘額欄，
    # 避免把前日餘額誤當成今日餘額。
    explicit_balance = [
        index for index, field in enumerate(fields)
        if field in {"當日借券賣出餘額", "今日借券賣出餘額", "借券賣出餘額"}
    ]
    balance_candidates = [index for index, field in enumerate(fields) if "餘額" in field]
    i_balance = (explicit_balance[-1] if explicit_balance else
                 balance_candidates[-1] if balance_candidates else -1)
    if i_code < 0 or i_balance < 0:
        return out
    for row in rows:
        if i_code >= len(row):
            continue
        ticker = str(row[i_code]).strip()
        if not ticker:
            continue
        out[ticker] = _to_float(row[i_balance]) / 1000.0
    return out


def _parse_block(data: Dict[str, Any]) -> Dict[str, float]:
    """回傳 {ticker: 鉅額交易淨額(張)}。TWSE 沒有明確的買賣方向，先用成交量做累計。"""
    out: Dict[str, float] = {}
    rows = data.get("data") or []
    fields = data.get("fields") or []
    if not rows or not fields:
        return out

    def idx(*kw: str) -> int:
        for i, f in enumerate(fields):
            for k in kw:
                if k in f:
                    return i
        return -1

    i_code = idx("證券代號", "代號")
    i_vol = idx("成交股數", "股數")
    if i_code < 0 or i_vol < 0:
        return out
    for row in rows:
        if i_code >= len(row):
            continue
        ticker = str(row[i_code]).strip()
        if not ticker:
            continue
        vol = _to_float(row[i_vol]) / 1000.0
        out[ticker] = out.get(ticker, 0.0) + vol
    return out


# ----------------------------------------------------------------------
# 上櫃 (TPEx) 籌碼
# ----------------------------------------------------------------------


def _fetch_tpex_chip(
    name: str,
    date: dt.date,
    *,
    session: Optional[requests.Session] = None,
    root: Optional[Path] = None,
    use_cache: bool = True,
    cache_only: bool = False,
    logger: Optional[logging.Logger] = None,
) -> Optional[Dict[str, Any]]:
    """抓上櫃 TPEx 籌碼端點 (name in ENDPOINTS_TPEX)，回傳 tables[0]。"""
    log = logger or get_logger("chips")
    effective_date = date
    if cache_only and not any(
        _cache_path(name, date, root).is_file() for name in _CACHE_ENDPOINT_NAMES
    ):
        effective_date = latest_cached_chip_date(date, root=root) or date
    date = effective_date
    sess = session or _new_session()
    path = _cache_path(name, date, root)
    if use_cache:
        restore_file_from_cloud(path, root=root)
    if use_cache and path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    if cache_only:
        return None
    url = ENDPOINTS_TPEX[name].format(date_slash=date.strftime("%Y/%m/%d"))
    try:
        resp = sess.get(url, timeout=20)
        if resp.status_code != 200:
            log.warning("%s HTTP %d (%s)", name, resp.status_code, url)
            return None
        try:
            data = resp.json()
        except Exception:
            return None
        tables = data.get("tables") if isinstance(data, dict) else None
        if not tables:
            return None
        table = tables[0] or {}
        if not isinstance(table, dict) or not isinstance(table.get("fields"), list) or not isinstance(table.get("data"), list):
            log.warning("%s TPEx table schema 異常，拒絕快取", name)
            return None
        _capture_chip_response(
            source=f"chips_tpex_{name}",
            content=bytes(getattr(resp, "content", b"") or b""),
            url=url,
            date=date,
            name=name,
            root=root,
            logger=log,
        )
        mk_folder(str(path.parent))
        path.write_text(json.dumps(table, ensure_ascii=False), encoding="utf-8")
        mirror_file_to_cloud(path, root=root)
        time.sleep(0.5)
        return table
    except Exception:
        log.exception("%s 抓取失敗 (%s)", name, url)
        return None


def _parse_tpex_insti(table: Dict[str, Any]) -> Dict[str, ChipDailyRow]:
    """上櫃三大法人；依欄名找欄位，避免固定 index 漂移後靜默誤讀。"""
    out: Dict[str, ChipDailyRow] = {}
    fields = [str(x) for x in (table.get("fields") or [])]
    rows = table.get("data") or []

    def find_index(*keywords: str) -> int:
        for index, field in enumerate(fields):
            if any(keyword in field for keyword in keywords):
                return index
        return -1

    idx_code = find_index("代號", "代碼")
    idx_name = find_index("名稱")
    idx_foreign = find_index("外資及陸資", "外陸資", "外資合計")
    idx_trust = find_index("投信")
    idx_dealer = find_index("自營商合計", "自營商")
    if min(idx_code, idx_foreign, idx_trust, idx_dealer) < 0:
        return out
    for row in rows:
        if not isinstance(row, list) or max(idx_code, idx_foreign, idx_trust, idx_dealer) >= len(row):
            continue
        ticker = str(row[idx_code]).strip()
        if not ticker:
            continue
        out[ticker] = ChipDailyRow(
            ticker=ticker,
            name=str(row[idx_name]).strip() if 0 <= idx_name < len(row) else "",
            foreign_net=_to_float(row[idx_foreign]) / 1000.0,
            investment_trust_net=_to_float(row[idx_trust]) / 1000.0,
            dealer_net=_to_float(row[idx_dealer]) / 1000.0,
        )
    return out


def _parse_tpex_margin(table: Dict[str, Any]) -> Dict[str, tuple[float, float]]:
    """上櫃融資融券；依欄名找欄位，回傳 {ticker:(margin,short)}。"""
    out: Dict[str, tuple[float, float]] = {}
    fields = [str(x) for x in (table.get("fields") or [])]
    rows = table.get("data") or []
    def find_index(*keywords: str) -> int:
        for index, field in enumerate(fields):
            if any(keyword in field for keyword in keywords):
                return index
        return -1
    idx_code = find_index("代號", "代碼")
    idx_margin = find_index("融資餘額")
    idx_short = find_index("融券餘額")
    if min(idx_code, idx_margin, idx_short) < 0:
        return out
    for row in rows:
        if not isinstance(row, list) or max(idx_code, idx_margin, idx_short) >= len(row):
            continue
        ticker = str(row[idx_code]).strip()
        if not ticker:
            continue
        out[ticker] = (_to_float(row[idx_margin]), _to_float(row[idx_short]))
    return out


# ----------------------------------------------------------------------
# 公開 API
# ----------------------------------------------------------------------


def fetch_daily_chips(
    date: dt.date,
    *,
    session: Optional[requests.Session] = None,
    root: Optional[Path] = None,
    use_cache: bool = True,
    cache_only: bool = False,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, ChipDailyRow]:
    """抓某一日全市場個股的籌碼匯總。

    ``cache_only=True`` 用於問答／展示的唯讀熱路徑：會嘗試還原與讀取
    已存在的本地快取，但缺檔時直接回傳空結果，不啟動 HTTP。
    """
    log = logger or get_logger("chips")
    sess = session or _new_session()

    t86 = _fetch_endpoint(
        "qfii_daily", date, session=sess, root=root,
        use_cache=use_cache, cache_only=cache_only, logger=log,
    )
    margin = _fetch_endpoint(
        "margin_balance", date, session=sess, root=root,
        use_cache=use_cache, cache_only=cache_only, logger=log,
    )
    borrow = _fetch_endpoint(
        "borrow_balance", date, session=sess, root=root,
        use_cache=use_cache, cache_only=cache_only, logger=log,
    )

    rows = _parse_t86(t86) if t86 else {}
    margin_map = _parse_margin(margin) if margin else {}
    borrow_map = _parse_borrow(borrow) if borrow else {}

    for ticker, (m, s) in margin_map.items():
        row = rows.setdefault(ticker, ChipDailyRow(ticker=ticker))
        row.margin_balance = m
        row.short_balance = s
    for ticker, b in borrow_map.items():
        row = rows.setdefault(ticker, ChipDailyRow(ticker=ticker))
        row.borrow_balance = b

    # ---- 上櫃 (TPEx) 合併 (代號不與上市重疊) ----
    tpex_insti = _fetch_tpex_chip(
        "tpex_insti", date, session=sess, root=root,
        use_cache=use_cache, cache_only=cache_only, logger=log,
    )
    tpex_margin = _fetch_tpex_chip(
        "tpex_margin", date, session=sess, root=root,
        use_cache=use_cache, cache_only=cache_only, logger=log,
    )
    if tpex_insti:
        for ticker, irow in _parse_tpex_insti(tpex_insti).items():
            row = rows.setdefault(ticker, ChipDailyRow(ticker=ticker))
            row.name = row.name or irow.name
            row.foreign_net = irow.foreign_net
            row.investment_trust_net = irow.investment_trust_net
            row.dealer_net = irow.dealer_net
    if tpex_margin:
        for ticker, (m, s) in _parse_tpex_margin(tpex_margin).items():
            row = rows.setdefault(ticker, ChipDailyRow(ticker=ticker))
            row.margin_balance = m
            row.short_balance = s

    return rows


def _recent_trading_days(end_date: dt.date, days: int) -> List[dt.date]:
    """回傳含 end_date 在內的最近 N 個「非週末」日期 (不檢驗國定假日，TWSE 缺檔時 fetch 會自動跳過)。"""
    out: List[dt.date] = []
    cur = end_date
    while len(out) < days:
        if cur.weekday() < 5:  # 一~五
            out.append(cur)
        cur -= dt.timedelta(days=1)
    return out


def build_chip_summary(
    ticker: str,
    *,
    end_date: Optional[dt.date] = None,
    days: int = 5,
    root: Optional[Path] = None,
    session: Optional[requests.Session] = None,
    logger: Optional[logging.Logger] = None,
) -> ChipSummary:
    """個股近 N 日籌碼摘要。"""
    end = end_date or now_tw().date()
    dates = _recent_trading_days(end, days)
    sess = session or _new_session()

    rows: List[ChipDailyRow] = []
    observed_dates: List[dt.date] = []
    foreign_total = 0.0
    trust_total = 0.0
    dealer_total = 0.0
    block_total = 0.0

    first_margin: Optional[float] = None
    last_margin: Optional[float] = None
    first_borrow: Optional[float] = None
    last_borrow: Optional[float] = None
    first_short: Optional[float] = None
    last_short: Optional[float] = None

    for d in reversed(dates):
        daily = fetch_daily_chips(
            d, session=sess, root=root, logger=logger,
        )
        row = daily.get(ticker)
        if row:
            rows.append(row)
            observed_dates.append(d)
            foreign_total += row.foreign_net
            trust_total += row.investment_trust_net
            dealer_total += row.dealer_net
            if first_margin is None:
                first_margin = row.margin_balance
                first_borrow = row.borrow_balance
                first_short = row.short_balance
            last_margin = row.margin_balance
            last_borrow = row.borrow_balance
            last_short = row.short_balance
        block = _fetch_endpoint("block_trade", d, session=sess, root=root, logger=logger)
        if block:
            bmap = _parse_block(block)
            block_total += bmap.get(ticker, 0.0)

    def _pct_change(first: Optional[float], last: Optional[float]) -> float:
        if first and first > 0 and last is not None:
            return 100.0 * (last - first) / first
        return 0.0

    return ChipSummary(
        ticker=ticker,
        days=days,
        foreign_net=foreign_total,
        investment_trust_net=trust_total,
        dealer_net=dealer_total,
        margin_buy_change_pct=_pct_change(first_margin, last_margin),
        short_borrow_change_pct=_pct_change(first_borrow, last_borrow),
        short_sell_change_pct=_pct_change(first_short, last_short),
        block_trade_net=block_total,
        rows=rows,
        end_date=max(observed_dates).isoformat() if observed_dates else "",
        source_document_ids=cached_chip_source_document_ids(
            observed_dates, root=root,
        ),
        source_date_status=(
            "aligned" if len({day.isoformat() for day in observed_dates}) <= 1
            else ("mixed" if observed_dates else "unknown")
        ),
    )


def summary_to_chips_context(s: ChipSummary):
    """轉成 llm_analyzer.ChipsContext。"""
    from bot.llm_analyzer import ChipsContext
    return ChipsContext(
        foreign_net=s.foreign_net,
        investment_trust_net=s.investment_trust_net,
        dealer_net=s.dealer_net,
        margin_buy_change_pct=s.margin_buy_change_pct,
        short_borrow_change_pct=s.short_borrow_change_pct,
        block_trade_net=s.block_trade_net,
        notes=f"基於最近 {s.days} 交易日資料 (共 {len(s.rows)} 筆)",
    )


def summary_to_dict(s: ChipSummary) -> Dict[str, Any]:
    return {
        "ticker": s.ticker,
        "days": s.days,
        "foreign_net": s.foreign_net,
        "investment_trust_net": s.investment_trust_net,
        "dealer_net": s.dealer_net,
        "margin_buy_change_pct": s.margin_buy_change_pct,
        "short_borrow_change_pct": s.short_borrow_change_pct,
        "short_sell_change_pct": s.short_sell_change_pct,
        "block_trade_net": s.block_trade_net,
        "end_date": s.end_date,
        "source_document_ids": list(s.source_document_ids),
        "source_date_status": s.source_date_status,
        "rows": [asdict(r) for r in s.rows],
    }


# ----------------------------------------------------------------------
# 輔助
# ----------------------------------------------------------------------


def _to_float(x: Any) -> float:
    if x is None:
        return 0.0
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).replace(",", "").replace("--", "0").strip()
    if not s or s in ("-", "--"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


__all__ = [
    "ChipDailyRow",
    "ChipSummary",
    "build_chip_summary",
    "cached_chip_source_document_ids",
    "fetch_daily_chips",
    "latest_cached_chip_date",
    "summary_to_chips_context",
    "summary_to_dict",
]
