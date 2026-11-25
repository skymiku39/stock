"""night_futures -- 台指期（TX）夜盤行情（TAIFEX MIS getQuoteList，MarketType=1）。

為什麼另外抓：期交所 OpenAPI 的 DailyMarketReportFut 是收盤後才更新的日報，夜盤那一列
記在「夜盤結束後那個交易日」，所以早上 08:00 時還拿不到昨晚（15:00～次日 05:00）的夜盤。
MIS 的盤後端點在夜盤收盤後到下一場夜盤開始前，都還查得到上一場夜盤的最後狀態。

日期慣例（2026-11-25 實測）：
- MIS 的 ``CDate`` 是夜盤「開始」那天（11-24 15:00～11-25 05:00 的夜盤記為 20261124）。
- ``CRefPrice`` 是前一個日盤的結算價，所以漲跌幅＝夜盤相對前一日盤結算的變動，
  可當作下一個日盤開盤的方向參考（不是開盤價預測）。
- ``TXF-P`` 列的 ``CRefPrice`` 是加權指數現貨前一日收盤。
- ``Status`` 為 ``TC`` 時已收盤；其他值的意義沒有官方文件，原樣保存。

輸出：data/night_futures/<夜盤開始日>.json 與 latest.json；原始回應進 SourceRegistry
（source ``taifex_mis_night``）。只讀公開行情，不登入、不下單。

用法：python -m bot.night_futures --root . [--json]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from bot.hot_stock_futures import TAIFEX_MIS_API_URL, _parse_float, _parse_int, _session
from bot.runtime_state import atomic_json
from bot.utils import configure_utf8_stdio, get_logger, now_tw

SOURCE_NAME = "taifex_mis_night"
NIGHT_REQUEST: Dict[str, str] = {
    "MarketType": "1",  # 1 = 盤後（夜盤），0 = 一般（日盤）
    "SymbolType": "F",
    "KindID": "1",
    "CID": "TXF",
    "ExpireMonth": "",
    "SortColumn": "",
    "AscDesc": "A",
}
# 月份代碼 A～L 對應 1～12 月，後面一位數是年份個位數；夜盤合約以 -M 結尾。
_CONTRACT_RE = re.compile(r"^TXF([A-L])(\d)-M$")


def _contract_month(symbol_id: str, session_year: int) -> Optional[str]:
    """TXFL6-M -> '202612'。年份只有個位數，取不早於夜盤年份前一年的最近年份。"""
    m = _CONTRACT_RE.match(symbol_id)
    if not m:
        return None
    month = ord(m.group(1)) - ord("A") + 1
    digit = int(m.group(2))
    year = session_year - session_year % 10 + digit
    if year < session_year - 1:
        year += 10
    return f"{year:04d}{month:02d}"


def _session_date(raw: str) -> str:
    text = str(raw or "").strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return ""


def _quote_time(raw: str) -> str:
    text = str(raw or "").strip()
    if len(text) == 6 and text.isdigit():
        return f"{text[:2]}:{text[2:4]}:{text[4:]}"
    return text


def next_trading_weekday(session_date: str) -> str:
    """夜盤開始日之後的第一個平日（未排除國定假日）。"""
    try:
        d = dt.date.fromisoformat(session_date)
    except ValueError:
        return ""
    d += dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d.isoformat()


def parse_night_quotes(payload: Dict[str, Any]) -> Dict[str, Any]:
    """把 MIS 回應轉成夜盤紀錄；找不到近月合約時 status 為 no_data。"""
    rt = payload.get("RtData") or {}
    quotes = [q for q in (rt.get("QuoteList") or []) if isinstance(q, dict)]
    spot_close = None
    contracts: List[Dict[str, Any]] = []
    for q in quotes:
        sid = str(q.get("SymbolID") or "").strip()
        if sid == "TXF-P":
            spot_close = _parse_float(q.get("CRefPrice"))
            continue
        session_date = _session_date(q.get("CDate"))
        if not session_date:
            continue
        month = _contract_month(sid, int(session_date[:4]))
        if month is None:
            continue
        last = _parse_float(q.get("CLastPrice"))
        ref = _parse_float(q.get("CRefPrice"))
        pct = _parse_float(q.get("CDiffRate"))
        if pct is None and last is not None and ref:
            pct = round(100 * (last - ref) / ref, 2)
        contracts.append({
            "symbol_id": sid,
            "contract_month": month,
            "session_date": session_date,
            "quote_time": _quote_time(q.get("CTime")),
            "status": str(q.get("Status") or "").strip(),
            "last": last,
            "ref_settlement": ref,
            "change": _parse_float(q.get("CDiff")),
            "pct_change": pct,
            "open": _parse_float(q.get("COpenPrice")),
            "high": _parse_float(q.get("CHighPrice")),
            "low": _parse_float(q.get("CLowPrice")),
            "volume": _parse_int(q.get("CTotalVolume")),
        })
    contracts.sort(key=lambda r: r["contract_month"])
    traded = [c for c in contracts if c["last"] is not None]
    out: Dict[str, Any] = {
        "status": "no_data",
        "rt_code": str(payload.get("RtCode") or ""),
        "spot_close": spot_close,
        "front": None,
        "contracts": contracts,
    }
    if not traded:
        return out
    front = dict(traded[0])
    front["closed"] = front["status"] == "TC"
    front["for_trading_day"] = next_trading_weekday(front["session_date"])
    if spot_close and front["last"] is not None:
        front["vs_spot_close_pct"] = round(100 * (front["last"] - spot_close) / spot_close, 2)
    else:
        front["vs_spot_close_pct"] = None
    out["status"] = "ok"
    out["front"] = front
    return out


def fetch_night_futures(
    *,
    root: Optional[Path] = None,
    session: Any = None,
    now: Optional[dt.datetime] = None,
    logger: Optional[logging.Logger] = None,
    save: bool = True,
) -> Dict[str, Any]:
    """抓台指期夜盤並（root 有值且 save 時）存檔；失敗時回 status=error，不丟例外。"""
    log = logger or get_logger("night-futures")
    now = now or now_tw()
    record: Dict[str, Any] = {
        "fetched_at": now.isoformat(timespec="seconds"),
        "source": SOURCE_NAME,
        "source_url": TAIFEX_MIS_API_URL,
        "source_document_id": "",
        "error": "",
    }
    sess = session or _session()
    try:
        resp = sess.post(TAIFEX_MIS_API_URL, json=dict(NIGHT_REQUEST), timeout=25)
        content = getattr(resp, "content", b"") or b""
        if root is not None and content:
            try:
                from bot.source_registry import SourceRegistry

                status_code = int(getattr(resp, "status_code", 200))
                with SourceRegistry(Path(root)) as registry:
                    doc = registry.capture_document(
                        SOURCE_NAME,
                        content,
                        source_url=TAIFEX_MIS_API_URL,
                        source_ref=f"night:{record['fetched_at']}",
                        title="TAIFEX 台指期夜盤行情",
                        kind="http_response",
                        suffix="json",
                        mime_type="application/json",
                        quality_status="ok" if 200 <= status_code < 300 else "invalid",
                        metadata={"request": NIGHT_REQUEST},
                    )
                    record["source_document_id"] = doc.document_id
            except Exception:  # noqa: BLE001 - 原始檔保存失敗不影響行情本身
                log.warning("夜盤原始回應保存失敗", exc_info=True)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        record.update(status="error", error=f"{type(exc).__name__}: {exc}",
                      spot_close=None, front=None, contracts=[])
        log.warning("台指期夜盤抓取失敗：%s", record["error"])
        return record
    record.update(parse_night_quotes(payload))
    if record["rt_code"] not in ("0", ""):
        record["error"] = f"RtCode={record['rt_code']} {payload.get('RtMsg', '')}".strip()
    if save and root is not None and record["status"] == "ok":
        out_dir = Path(root) / "data" / "night_futures"
        atomic_json(out_dir / f"{record['front']['session_date']}.json", record)
        atomic_json(out_dir / "latest.json", record)
    return record


def brief_section(
    loader: Callable[[], Dict[str, Any]],
    *,
    expected_session_date: str,
) -> tuple:
    """給美股隔夜重點用：回傳 (section, warnings)。夜盤失敗不能讓整份重點失敗。"""
    warnings: List[str] = []
    try:
        rec = loader()
    except Exception as exc:  # noqa: BLE001
        rec = {"status": "error", "error": f"{type(exc).__name__}: {exc}", "front": None}
    front = rec.get("front") if isinstance(rec, dict) else None
    section = {
        "status": rec.get("status", "error") if isinstance(rec, dict) else "error",
        "error": rec.get("error", "") if isinstance(rec, dict) else "",
        "fetched_at": rec.get("fetched_at", "") if isinstance(rec, dict) else "",
        "spot_close": rec.get("spot_close") if isinstance(rec, dict) else None,
        "front": front,
    }
    if section["status"] != "ok" or not front:
        warnings.append(f"台指期夜盤取不到：{section['error'] or section['status']}")
        return section, warnings
    if front.get("session_date") != expected_session_date:
        warnings.append(
            f"台指期夜盤的日期是 {front.get('session_date')}，不是預期的 {expected_session_date}，"
            "可能是休市或資料還沒更新")
    if not front.get("closed"):
        warnings.append(f"台指期夜盤狀態為「{front.get('status') or '空白'}」，可能還沒收盤")
    return section, warnings


def main(argv: Optional[List[str]] = None) -> int:
    configure_utf8_stdio()
    p = argparse.ArgumentParser(description="抓台指期夜盤（TAIFEX MIS）並存檔")
    p.add_argument("--root", default=".")
    p.add_argument("--json", action="store_true", help="輸出完整紀錄")
    p.add_argument("--no-save", action="store_true")
    args = p.parse_args(argv)
    root = Path(args.root)
    rec = fetch_night_futures(root=root, save=not args.no_save,
                              logger=get_logger("night-futures", log_dir=root / "log"))
    if args.json:
        print(json.dumps(rec, ensure_ascii=False, indent=2))
    else:
        f = rec.get("front") or {}
        print(json.dumps({
            "status": rec.get("status"), "error": rec.get("error"),
            "session_date": f.get("session_date"), "for_trading_day": f.get("for_trading_day"),
            "contract": f.get("symbol_id"), "last": f.get("last"),
            "pct_change": f.get("pct_change"), "low": f.get("low"), "high": f.get("high"),
            "quote_time": f.get("quote_time"), "closed": f.get("closed"),
            "vs_spot_close_pct": f.get("vs_spot_close_pct"),
        }, ensure_ascii=False))
    return 0 if rec.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
