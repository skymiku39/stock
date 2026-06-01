"""active_etf -- 主動式 ETF 清單、持股資料模型與快照。

設計重點：
1. 內建一份「目前已掛牌主動式 ETF」清單 (依 2025-2026 公開資訊)，
   使用者可在 data/active_etfs.json 自行覆蓋/擴充。
2. 持股資料以 CSV 為單一事實來源，路徑 data/etf_holdings/<symbol>/<YYYY-MM-DD>.csv
   欄位: ticker,name,weight_pct,shares,value
3. 透過 Shioaji 取得 ETF 自身的即時市價/淨值估計與折溢價；持股淨值需另行匯入。
4. 不假設一定能爬到投信網頁；保留 Hook (HoldingsFetcher) 由子類別擴充。
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, TYPE_CHECKING

from bot.cloud_file_cache import (
    mirror_file_to_cloud,
    restore_file_from_cloud,
    restore_tree_from_cloud,
)
from bot.utils import get_logger, mk_folder, now_tw

if TYPE_CHECKING:
    from bot.broker import SjBroker


# ----------------------------------------------------------------------
# 內建主動式 ETF 清單 (公開資訊整理；可被 data/active_etfs.json 覆寫)
# ----------------------------------------------------------------------

# 依 2025-2026 公開資訊與 TWSE「主動式 ETF」專區整理。
# holdings_url 採用 etfinfo.tw 的標準化端點 — 該站每日同步各投信公告的 PCF / 持股清單，
# 比直接打散在 12+ 投信官網 (URL 格式都不一樣) 更可靠且更易於 LLM 解析。
# 使用者可在「主動 ETF 追蹤」頁覆寫成各家投信的官方頁。
_ETFINFO_HOLDINGS = "https://www.etfinfo.tw/etf/{symbol}/holdings"

DEFAULT_ACTIVE_ETFS: List[Dict[str, str]] = [
    {"symbol": "00980A", "name": "主動野村臺灣優選", "issuer": "野村投信", "region": "台灣", "freq": "季配",
     "holdings_url": _ETFINFO_HOLDINGS.format(symbol="00980A")},
    {"symbol": "00981A", "name": "主動統一台股增長", "issuer": "統一投信", "region": "台灣", "freq": "季配",
     "holdings_url": _ETFINFO_HOLDINGS.format(symbol="00981A")},
    {"symbol": "00982A", "name": "主動群益台灣強棒", "issuer": "群益投信", "region": "台灣", "freq": "季配",
     "holdings_url": _ETFINFO_HOLDINGS.format(symbol="00982A")},
    {"symbol": "00983A", "name": "主動中信ARK創新", "issuer": "中國信託投信", "region": "美國", "freq": "年配",
     "holdings_url": _ETFINFO_HOLDINGS.format(symbol="00983A")},
    {"symbol": "00984A", "name": "主動安聯台灣高息", "issuer": "安聯投信", "region": "台灣", "freq": "季配",
     "holdings_url": _ETFINFO_HOLDINGS.format(symbol="00984A")},
    {"symbol": "00985A", "name": "主動野村台灣50", "issuer": "野村投信", "region": "台灣", "freq": "年配",
     "holdings_url": _ETFINFO_HOLDINGS.format(symbol="00985A")},
    {"symbol": "00986A", "name": "主動台新龍頭成長", "issuer": "台新投信", "region": "全球", "freq": "年配",
     "holdings_url": _ETFINFO_HOLDINGS.format(symbol="00986A")},
    {"symbol": "00987A", "name": "主動台新優勢成長", "issuer": "台新投信", "region": "台灣", "freq": "年配",
     "holdings_url": _ETFINFO_HOLDINGS.format(symbol="00987A")},
    {"symbol": "00988A", "name": "主動統一全球創新", "issuer": "統一投信", "region": "全球", "freq": "年配",
     "holdings_url": _ETFINFO_HOLDINGS.format(symbol="00988A")},
    {"symbol": "00989A", "name": "主動摩根極速領跑", "issuer": "摩根投信", "region": "美國", "freq": "-",
     "holdings_url": _ETFINFO_HOLDINGS.format(symbol="00989A")},
    {"symbol": "00990A", "name": "主動元大AI新經濟", "issuer": "元大投信", "region": "台灣", "freq": "-",
     "holdings_url": _ETFINFO_HOLDINGS.format(symbol="00990A")},
    {"symbol": "00991A", "name": "主動復華未來50", "issuer": "復華投信", "region": "台灣", "freq": "半年配",
     "holdings_url": _ETFINFO_HOLDINGS.format(symbol="00991A")},
    {"symbol": "00992A", "name": "主動群益科技創新", "issuer": "群益投信", "region": "台灣", "freq": "季配",
     "holdings_url": _ETFINFO_HOLDINGS.format(symbol="00992A")},
    {"symbol": "00993A", "name": "主動安聯台灣", "issuer": "安聯投信", "region": "台灣", "freq": "年配",
     "holdings_url": _ETFINFO_HOLDINGS.format(symbol="00993A")},
    {"symbol": "00994A", "name": "主動第一金台股優", "issuer": "第一金投信", "region": "台灣", "freq": "-",
     "holdings_url": _ETFINFO_HOLDINGS.format(symbol="00994A")},
    {"symbol": "00995A", "name": "主動中信台灣卓越", "issuer": "中國信託投信", "region": "台灣", "freq": "季配",
     "holdings_url": _ETFINFO_HOLDINGS.format(symbol="00995A")},
    {"symbol": "00996A", "name": "主動兆豐台灣豐收", "issuer": "兆豐投信", "region": "台灣", "freq": "季配",
     "holdings_url": _ETFINFO_HOLDINGS.format(symbol="00996A")},
    {"symbol": "00997A", "name": "主動群益美國增長", "issuer": "群益投信", "region": "美國", "freq": "季配",
     "holdings_url": _ETFINFO_HOLDINGS.format(symbol="00997A")},
    {"symbol": "00998A", "name": "主動復華金融股息", "issuer": "復華投信", "region": "全球", "freq": "季配",
     "holdings_url": "https://www.fhtrust.com.tw/ETF/etf_detail/ETF24"},
    {"symbol": "00400A", "name": "主動國泰動能高息", "issuer": "國泰投信", "region": "台灣", "freq": "月配",
     "holdings_url": _ETFINFO_HOLDINGS.format(symbol="00400A")},
    {"symbol": "00401A", "name": "主動摩根台灣鑫收", "issuer": "摩根投信", "region": "台灣", "freq": "月配",
     "holdings_url": _ETFINFO_HOLDINGS.format(symbol="00401A")},
    {"symbol": "00402A", "name": "主動安聯美國科技", "issuer": "安聯投信", "region": "美國", "freq": "-",
     "holdings_url": _ETFINFO_HOLDINGS.format(symbol="00402A")},
    {"symbol": "00403A", "name": "主動統一升級50", "issuer": "統一投信", "region": "台灣", "freq": "季配",
     "holdings_url": _ETFINFO_HOLDINGS.format(symbol="00403A")},
    {"symbol": "00404A", "name": "主動聯博全球非投", "issuer": "聯博投信", "region": "全球", "freq": "月配",
     "holdings_url": _ETFINFO_HOLDINGS.format(symbol="00404A")},
    {"symbol": "00405A", "name": "主動富邦台灣龍耀", "issuer": "富邦投信", "region": "台灣", "freq": "-",
     "holdings_url": _ETFINFO_HOLDINGS.format(symbol="00405A")},
    {"symbol": "00406A", "name": "主動中信台灣收益", "issuer": "中國信託投信", "region": "台灣", "freq": "月配",
     "holdings_url": _ETFINFO_HOLDINGS.format(symbol="00406A")},
    {"symbol": "00407A", "name": "主動凱基台灣", "issuer": "凱基投信", "region": "台灣", "freq": "不配息",
     "holdings_url": _ETFINFO_HOLDINGS.format(symbol="00407A")},
]


@dataclass
class ActiveEtf:
    """單一主動式 ETF 中繼資料。"""

    symbol: str
    name: str
    issuer: str
    region: str = "台灣"
    freq: str = "-"
    holdings_url: str = ""  # 投信官方持股頁面 URL，供自動抓取使用


@dataclass
class Holding:
    """ETF 內單一持股。"""

    ticker: str
    name: str = ""
    weight_pct: float = 0.0
    shares: float = 0.0
    value: float = 0.0


@dataclass
class HoldingsSnapshot:
    """某 ETF 在某日的完整持股快照。"""

    symbol: str
    date: dt.date
    holdings: List[Holding] = field(default_factory=list)

    def by_ticker(self) -> Dict[str, Holding]:
        return {h.ticker: h for h in self.holdings}

    def total_weight(self) -> float:
        return sum(h.weight_pct for h in self.holdings)


@dataclass
class EtfQuote:
    """ETF 在某時刻的市場報價 + 折溢價估計。"""

    symbol: str
    ts: dt.datetime
    market_price: float
    nav: Optional[float] = None
    pct_chg: float = 0.0
    volume: int = 0

    @property
    def premium_pct(self) -> Optional[float]:
        if self.nav and self.nav > 0:
            return 100.0 * (self.market_price - self.nav) / self.nav
        return None


# ----------------------------------------------------------------------
# 清單 IO
# ----------------------------------------------------------------------


def list_path(root: Optional[Path] = None) -> Path:
    base = root or Path.cwd()
    return base / "data" / "active_etfs.json"


def load_active_etfs(root: Optional[Path] = None) -> List[ActiveEtf]:
    """優先讀 data/active_etfs.json，找不到時用內建預設值。

    對舊版 JSON (沒有 holdings_url 欄位) 也能相容。
    """
    p = list_path(root)
    restore_file_from_cloud(p, root=root)
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            out: List[ActiveEtf] = []
            valid_fields = {f.name for f in dataclasses.fields(ActiveEtf)}
            for r in raw:
                clean = {k: v for k, v in r.items() if k in valid_fields}
                out.append(ActiveEtf(**clean))
            return out
        except Exception:
            pass
    return [ActiveEtf(**r) for r in DEFAULT_ACTIVE_ETFS]


def save_active_etfs(etfs: Iterable[ActiveEtf], root: Optional[Path] = None) -> Path:
    p = list_path(root)
    mk_folder(str(p.parent))
    data = [dataclasses.asdict(e) for e in etfs]
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    mirror_file_to_cloud(p, root=root)
    return p


# ----------------------------------------------------------------------
# 持股快照 IO  (CSV 為單一事實來源，方便手動編輯/版本控管)
# ----------------------------------------------------------------------


def holdings_dir(symbol: str, root: Optional[Path] = None) -> Path:
    base = root or Path.cwd()
    return base / "data" / "etf_holdings" / symbol


def holdings_path(symbol: str, date: dt.date, root: Optional[Path] = None) -> Path:
    return holdings_dir(symbol, root) / f"{date.isoformat()}.csv"


def list_holdings_dates(symbol: str, root: Optional[Path] = None) -> List[dt.date]:
    d = holdings_dir(symbol, root)
    restore_tree_from_cloud(d, root=root)
    if not d.exists():
        return []
    dates: List[dt.date] = []
    for f in d.glob("*.csv"):
        try:
            dates.append(dt.date.fromisoformat(f.stem))
        except ValueError:
            continue
    return sorted(dates, reverse=True)


def load_holdings(
    symbol: str,
    date: Optional[dt.date] = None,
    root: Optional[Path] = None,
) -> Optional[HoldingsSnapshot]:
    """讀指定日的持股，未指定日則讀最新一筆。"""
    if date is None:
        dates = list_holdings_dates(symbol, root)
        if not dates:
            return None
        date = dates[0]
    p = holdings_path(symbol, date, root)
    restore_file_from_cloud(p, root=root)
    if not p.exists():
        return None
    holdings: List[Holding] = []
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                holdings.append(Holding(
                    ticker=str(row.get("ticker", "")).strip(),
                    name=str(row.get("name", "")).strip(),
                    weight_pct=float(row.get("weight_pct", 0) or 0),
                    shares=float(row.get("shares", 0) or 0),
                    value=float(row.get("value", 0) or 0),
                ))
            except (ValueError, TypeError):
                continue
    return HoldingsSnapshot(symbol=symbol, date=date, holdings=holdings)


def save_holdings(snap: HoldingsSnapshot, root: Optional[Path] = None) -> Path:
    p = holdings_path(snap.symbol, snap.date, root)
    mk_folder(str(p.parent))
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["ticker", "name", "weight_pct", "shares", "value"],
        )
        writer.writeheader()
        for h in snap.holdings:
            writer.writerow(dataclasses.asdict(h))
    mirror_file_to_cloud(p, root=root)
    return p


# ----------------------------------------------------------------------
# Shioaji 快照
# ----------------------------------------------------------------------


def fetch_etf_quotes_via_shioaji(
    broker: "SjBroker",
    etfs: List[ActiveEtf],
    logger: Optional[logging.Logger] = None,
) -> Dict[str, EtfQuote]:
    """以 Shioaji snapshots API 取得 ETF 即時報價 + 前日收盤。

    Shioaji 公開 API 不直接給 NAV，因此 nav 留空，留待後續從投信網頁/PCF 補。
    """
    log = logger or get_logger("active-etf")
    quotes: Dict[str, EtfQuote] = {}
    if not etfs:
        return quotes
    try:
        if broker.api is None:
            log.warning("broker 未登入，跳過 ETF 快照")
            return quotes
        contracts = [broker.get_contract(e.symbol) for e in etfs]
        contracts = [c for c in contracts if c is not None]
        if not contracts:
            return quotes
        snaps = broker.api.snapshots(contracts)
        for snap in snaps:
            code = getattr(snap, "code", "")
            close = float(getattr(snap, "close", 0) or 0)
            change = float(getattr(snap, "change_price", 0) or 0)
            change_rate = float(getattr(snap, "change_rate", 0) or 0)
            volume = int(getattr(snap, "total_volume", 0) or 0)
            quotes[code] = EtfQuote(
                symbol=code,
                ts=now_tw(),
                market_price=close,
                nav=None,
                pct_chg=change_rate or (100 * change / (close - change) if close - change > 0 else 0.0),
                volume=volume,
            )
    except Exception:
        log.exception("Shioaji snapshots 失敗")
    return quotes


# ----------------------------------------------------------------------
# 工具：將快照轉為 pandas DataFrame (供儀表板)
# ----------------------------------------------------------------------


def snapshots_to_records(
    snapshots: Dict[str, HoldingsSnapshot],
    etf_meta: Dict[str, ActiveEtf],
) -> List[Dict[str, Any]]:
    """攤平多檔 ETF 持股為記錄列，方便交叉分析。"""
    rows: List[Dict[str, Any]] = []
    for sym, snap in snapshots.items():
        meta = etf_meta.get(sym)
        for h in snap.holdings:
            rows.append({
                "etf_symbol": sym,
                "etf_name": meta.name if meta else "",
                "etf_issuer": meta.issuer if meta else "",
                "date": snap.date.isoformat(),
                "ticker": h.ticker,
                "name": h.name,
                "weight_pct": h.weight_pct,
                "shares": h.shares,
                "value": h.value,
            })
    return rows


__all__ = [
    "ActiveEtf",
    "DEFAULT_ACTIVE_ETFS",
    "EtfQuote",
    "Holding",
    "HoldingsSnapshot",
    "fetch_etf_quotes_via_shioaji",
    "holdings_dir",
    "holdings_path",
    "list_active_etfs_path",
    "list_holdings_dates",
    "load_active_etfs",
    "load_holdings",
    "save_active_etfs",
    "save_holdings",
    "snapshots_to_records",
]


list_active_etfs_path = list_path
