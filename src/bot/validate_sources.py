"""validate_sources -- 資料源健康度 smoke test（``stock-validate``）。

定位
====
逐一檢查每個外部資料源並輸出 JSON / Markdown 摘要。資料 root 語意：

* 省略 ``--root``：使用暫存 data root，結束後自動清理；不碰正式 ``data/``。
* 明確 ``--root PATH``：將 ``PATH`` 傳給收集器，可能寫入 cache / raw / provenance。
* 設定來源：省略 ``--root`` 時讀 CWD；明確 ``--root PATH`` 時讀 ``PATH``。

三層驗證
========
* ``layer="link"``   -- 連結有填、格式正確
* ``layer="http"``   -- HTTP 能抓到有效內容 (status 200 + 非空/非阻擋頁)
* ``layer="parse"``  -- 解析後能被程式流程使用 (rows > 0 / 可建立 record)

安全
====
* 不下單；只顯示憑證「是否存在」(present=true/false)，**絕不印出金鑰內容**。
* Shioaji / MIS / 雲端 live ping 預設關閉，需以旗標明確開啟；雲端僅在要求時 ping。

用法
====
```bash
uv run stock-validate                       # 預設全部公開資料源 (暫存 root)
uv run stock-validate --json out.json       # 另存 JSON
uv run stock-validate --md out.md           # 另存 Markdown
uv run stock-validate --shioaji             # 額外嘗試 Shioaji 登入/快照 (需憑證)
uv run stock-validate --mis-seconds 30      # MIS 即時 tick 連續輪詢秒數
uv run stock-validate --cloud               # 對雲端做 live ping
```
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import sys
import tempfile
import time
from dataclasses import asdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests

from bot.cli_encoding import configure_console

from bot.source_audit import DatasetAudit, audit_rows
from bot.utils import log_root, now_tw


# ----------------------------------------------------------------------
# 結果模型
# ----------------------------------------------------------------------


@dataclass
class CheckResult:
    name: str
    layer: str = "http"            # link | http | parse
    ok: bool = False
    rows: int = 0
    sample_date: str = ""
    error: str = ""
    note: str = ""
    needs_credentials: bool = False
    quality_status: str = "unknown"
    findings: List[Dict[str, Any]] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "layer": self.layer,
            "ok": self.ok,
            "rows": self.rows,
            "sample_date": self.sample_date,
            "error": self.error,
            "note": self.note,
            "needs_credentials": self.needs_credentials,
            "quality_status": self.quality_status,
            "findings": self.findings,
            "details": self.details,
        }


def _row_dicts(rows: Any) -> List[Dict[str, Any]]:
    """把 dataclass / dict 混合結果轉成來源稽核可讀的 dict。"""
    out: List[Dict[str, Any]] = []
    for row in rows or []:
        if isinstance(row, dict):
            out.append(dict(row))
        elif hasattr(row, "__dataclass_fields__"):
            out.append(asdict(row))
        elif hasattr(row, "__dict__"):
            out.append(dict(vars(row)))
    return out


def _apply_audit(result: CheckResult, audit: DatasetAudit) -> None:
    """將語意品質與 smoke test 結果分開保存，避免 HTTP 200 被誤當成可靠。"""
    result.quality_status = audit.quality_status
    result.findings = [finding.to_dict() for finding in audit.findings]
    result.details["quality"] = audit.to_dict()
    if audit.quality_status == "invalid":
        result.ok = False
        if not result.error:
            result.error = "語意品質稽核失敗"
    elif audit.quality_status == "partial" and not result.note:
        result.note = "資料可解析但有品質警告，請查看 findings"


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0 Safari/537.36",
        "Accept-Language": "zh-TW,zh;q=0.9",
    })
    return s


# ----------------------------------------------------------------------
# 公開資料源檢查
# ----------------------------------------------------------------------


def check_monthly_revenue(root: Path) -> CheckResult:
    from bot.fundamentals_fetcher import fetch_monthly_revenue_all
    r = CheckResult(name="基本面/月營收 (TWSE t187ap05_L + TPEx)", layer="parse")
    try:
        rows = fetch_monthly_revenue_all(root=root, use_cache=False)
        r.rows = len(rows)
        r.ok = r.rows > 0
        _apply_audit(r, audit_rows(
            _row_dicts(rows),
            source="monthly_revenue",
            required_fields=(
                ("公司代號", "公司代碼", "證券代號", "stock_code"),
                ("資料年月", "年月", "datatime"),
            ),
            key_fields=(
                ("公司代號", "公司代碼", "證券代號", "stock_code"),
                ("資料年月", "年月", "datatime"),
            ),
        ))
        if not r.ok and not r.error:
            r.error = "回傳 0 筆"
    except Exception as e:  # noqa: BLE001
        r.error = f"{type(e).__name__}: {e}"
    return r


def check_company_info(root: Optional[Path] = None) -> CheckResult:
    """驗證上市與上櫃公司名冊的 HTTP、欄位與市場覆蓋。"""
    from bot.company_info import _build_company_map

    r = CheckResult(name="公司名冊 (TWSE + TPEx)", layer="parse")
    try:
        company_map = _build_company_map(root=root)
        rows = _row_dicts(company_map.values())
        r.rows = len(rows)
        market_counts: Dict[str, int] = {}
        for row in rows:
            market = str(row.get("market") or "unknown")
            market_counts[market] = market_counts.get(market, 0) + 1
        r.details["market_counts"] = market_counts
        r.ok = bool(rows) and all(market_counts.get(market, 0) > 0 for market in ("TWSE", "TPEX"))
        _apply_audit(r, audit_rows(
            rows,
            source="company_info",
            required_fields=("symbol", "name", "market"),
            key_fields=("symbol",),
        ))
        if not r.ok and not r.error:
            r.error = f"公司名冊市場覆蓋不足: {market_counts}"
    except Exception as e:  # noqa: BLE001
        r.error = f"{type(e).__name__}: {e}"
    return r


def check_daily_kline(root: Optional[Path] = None) -> CheckResult:
    """驗證 TWSE/TPEx 單月日 K 解析與 OHLCV 基本一致性。"""
    from bot.technicals import fetch_monthly_kline

    r = CheckResult(name="歷史日K (TWSE + TPEx)", layer="parse")
    previous_month = now_tw().date().replace(day=1) - dt.timedelta(days=1)
    samples = (
        ("TWSE", "2330", "twse"),
        ("TPEx", "6488", "tpex"),
    )
    audits: Dict[str, DatasetAudit] = {}
    try:
        for market_name, ticker, market in samples:
            rows = fetch_monthly_kline(
                ticker,
                previous_month.year,
                previous_month.month,
                market=market,
            )
            audit = audit_rows(
                rows,
                source=f"daily_kline_{market}",
                required_fields=("date", "open", "high", "low", "close", "volume"),
                key_fields=("date",),
                date_fields=("date",),
                numeric_fields=("open", "high", "low", "close", "volume"),
                numeric_ranges={"open": (0.0, None), "high": (0.0, None),
                                "low": (0.0, None), "close": (0.0, None),
                                "volume": (0.0, None)},
            )
            audits[market_name] = audit
            r.details[market_name] = {
                "ticker": ticker,
                "rows": len(rows),
                "quality": audit.to_dict(),
            }

        r.rows = sum(audit.rows for audit in audits.values())
        r.ok = bool(audits) and all(audit.rows > 0 and audit.usable for audit in audits.values())
        if audits:
            if any(audit.quality_status == "invalid" for audit in audits.values()):
                r.quality_status = "invalid"
                r.findings = [
                    finding.to_dict()
                    for audit in audits.values()
                    for finding in audit.findings
                ]
                r.error = "日 K 語意品質稽核失敗"
            elif any(audit.quality_status == "partial" for audit in audits.values()):
                r.quality_status = "partial"
                r.note = "日 K 可解析但有品質警告，請查看 details"
            else:
                r.quality_status = "ok"
        if not r.ok and not r.error:
            r.error = "TWSE 或 TPEx 上月日 K 無資料"
    except Exception as e:  # noqa: BLE001
        r.error = f"{type(e).__name__}: {e}"
    return r


def check_valuation(root: Path) -> CheckResult:
    from bot.fundamentals_fetcher import fetch_valuation_all
    r = CheckResult(name="基本面/估值 PER-PBR-殖利率 (BWIBBU_ALL + TPEx)", layer="parse")
    try:
        rows = fetch_valuation_all(root=root, use_cache=False)
        r.rows = len(rows)
        r.ok = r.rows > 0
        # A single provider outlier must not make an otherwise complete
        # market-wide dataset unusable.  Keep it visible as partial quality,
        # while individual ticker parsing rejects the value defensively.
        usable_rows = []
        outliers = []
        for index, row in enumerate(rows):
            try:
                raw_value = row.get("DividendYield") or row.get("殖利率(%)")
                value = float(str(raw_value).replace(",", "").strip()) if raw_value not in (None, "") else 0.0
            except (TypeError, ValueError):
                value = 0.0
            if value < -100.0 or value > 100.0:
                outliers.append({
                    "row_index": index,
                    "ticker": str(row.get("Code") or row.get("證券代號") or ""),
                    "dividend_yield": value,
                })
            else:
                usable_rows.append(row)
        r.details["invalid_outlier_rows"] = outliers
        _apply_audit(r, audit_rows(
            _row_dicts(usable_rows),
            source="valuation",
            required_fields=(("Code", "證券代號", "SecuritiesCompanyCode"),),
            key_fields=(("Code", "證券代號", "SecuritiesCompanyCode"),),
            date_fields=(("Date", "資料日期", "日期"),),
            numeric_fields=(
                ("PEratio", "PER", "本益比"),
                ("PBratio", "PBR", "股價淨值比"),
                ("DividendYield", "殖利率", "YieldRatio"),
            ),
            numeric_ranges={"DividendYield": (-100.0, 100.0)},
        ))
        if outliers and r.quality_status != "invalid":
            r.quality_status = "partial"
            r.note = f"排除 {len(outliers)} 筆殖利率異常值；原始資料已保留供追蹤"
            r.ok = bool(usable_rows)
        if not r.ok and not r.error:
            r.error = "回傳 0 筆"
    except Exception as e:  # noqa: BLE001
        r.error = f"{type(e).__name__}: {e}"
    return r


def check_dividends(root: Path) -> CheckResult:
    """股利：全市場筆數 + 2330 能建立 record。"""
    from bot.fundamentals_fetcher import fetch_dividend_all, fetch_dividends
    r = CheckResult(name="基本面/股利分派 (TWSE t187ap45_L + TPEx t187ap45_O)", layer="parse")
    try:
        rows = fetch_dividend_all(root=root, use_cache=False)
        r.rows = len(rows)
        recs = fetch_dividends("2330", root=root)
        r.details["2330_records"] = len(recs)
        if recs:
            latest = recs[-1]
            r.sample_date = str(latest.year)
            r.details["2330_latest"] = {
                "year": latest.year, "cash": latest.cash_dividend, "stock": latest.stock_dividend,
            }
        r.ok = r.rows > 0 and len(recs) > 0
        if not r.ok:
            r.error = f"rows={r.rows}, 2330_records={len(recs)}"
    except Exception as e:  # noqa: BLE001
        r.error = f"{type(e).__name__}: {e}"
    return r


def check_quarterly(root: Path) -> CheckResult:
    from bot.fundamentals_fetcher import fetch_quarterly_financials_all
    r = CheckResult(name="基本面/季報 EPS-三率 (t187ap06 各業別)", layer="parse")
    try:
        rows = fetch_quarterly_financials_all(root=root, use_cache=False)
        r.rows = len(rows)
        r.ok = r.rows > 0
        _apply_audit(r, audit_rows(
            _row_dicts(rows),
            source="quarterly_financials",
            required_fields=(
                ("公司代號", "SecuritiesCompanyCode"),
                ("年度", "Year"),
                ("季別", "Season"),
            ),
            key_fields=(
                ("公司代號", "SecuritiesCompanyCode"),
                ("年度", "Year"),
                ("季別", "Season"),
            ),
        ))
        if not r.ok and not r.error:
            r.error = "回傳 0 筆"
    except Exception as e:  # noqa: BLE001
        r.error = f"{type(e).__name__}: {e}"
    return r


# 來源頁出現「持股明細表」的最少權重百分比 token 數 (近似判斷，真正解析由 LLM 完成)。
# 空頁/免責頁通常只有少數小數；真正持股頁 (10+ 檔，各帶權重%) 會出現多個 "xx.xx"。
_MIN_WEIGHT_TOKENS = 8


def check_etf_urls(root: Path) -> List[CheckResult]:
    """逐檔 ETF：HTTP 200 (layer=http) + 來源頁是否含持股明細 (layer=parse 的非 LLM 近似)。

    layer=parse 僅近似 (數權重百分比 token)；真正的持股 CSV 由 LLM 抓取流程產生。
    """
    from bot.active_etf import load_active_etfs
    from bot.etf_holdings_fetcher import _html_to_text  # type: ignore

    out: List[CheckResult] = []
    sess = _session()
    etfs = load_active_etfs(root)
    for e in etfs:
        r = CheckResult(name=f"ETF持股/{e.symbol} {e.name}", layer="http")
        if not e.holdings_url:
            r.error = "no_holdings_url"
            out.append(r)
            continue
        r.details["url"] = e.holdings_url
        try:
            resp = sess.get(e.holdings_url, timeout=25)
            r.details["status"] = resp.status_code
            if resp.status_code != 200:
                r.error = f"HTTP {resp.status_code}"
                out.append(r)
                continue
            try:
                resp.encoding = resp.apparent_encoding or "utf-8"
            except Exception:  # noqa: BLE001
                resp.encoding = "utf-8"
            text = _html_to_text(resp.text)
            weight_tokens = len(re.findall(r"\b\d{1,2}\.\d{1,2}\b", text))
            r.details["weight_tokens"] = weight_tokens
            has_holdings = weight_tokens >= _MIN_WEIGHT_TOKENS
            r.layer = "parse"
            r.rows = weight_tokens
            r.ok = has_holdings
            if not has_holdings:
                r.note = (
                    "HTTP 200 但來源頁尚無持股明細 (常見於新掛牌、第三方尚未收錄；"
                    "LLM 抓取會回 no_holdings_parsed，屬已知 fallback)"
                )
        except Exception as ex:  # noqa: BLE001
            r.error = f"{type(ex).__name__}: {ex}"
        out.append(r)
    return out


def check_mops_conference(root: Optional[Path] = None) -> CheckResult:
    from bot.mops_scraper import fetch_conference_schedule
    r = CheckResult(name="MOPS/法說會行事曆 (mopsov ajax_t100sb02_1)", layer="parse")
    today = dt.date.today()
    roc = today.year - 1911
    try:
        # 試本月與上月，至少一個月份 > 0
        best = 0
        for m in (today.month, max(1, today.month - 1)):
            entries = fetch_conference_schedule(roc, m, root=root)
            best = max(best, len(entries))
            if entries:
                r.sample_date = f"{roc}/{m:02d}"
                break
        r.rows = best
        r.ok = best > 0
        if not r.ok:
            r.error = "近兩個月皆 0 筆 (可能被安全性阻擋或無資料)"
    except Exception as e:  # noqa: BLE001
        r.error = f"{type(e).__name__}: {e}"
    return r


def check_global_events(root: Path) -> CheckResult:
    r = CheckResult(name="全球科技事件行事曆", layer="parse")
    try:
        from bot.global_event_calendar import load_global_events, update_global_events
        update_global_events(root=root, include_network=False)
        events = load_global_events(root)
        r.rows = len(events)
        r.ok = r.rows > 0
        if events:
            r.sample_date = events[0].date.isoformat()
        r.note = "種子 + 快取 (未打外網)" if r.ok else "無全球科技事件"
    except Exception as e:  # noqa: BLE001
        r.error = f"{type(e).__name__}: {e}"
    return r


def check_mops_material(root: Optional[Path] = None) -> CheckResult:
    """重大訊息：OpenAPI t187ap04_L 全市場當日筆數 + 不可為安全性阻擋。"""
    from bot.mops_scraper import URL_MATERIAL_OPENAPI
    r = CheckResult(name="MOPS/重大訊息 (OpenAPI t187ap04_L)", layer="parse")
    try:
        resp = _session().get(URL_MATERIAL_OPENAPI, timeout=20)
        if resp.status_code != 200:
            r.error = f"HTTP {resp.status_code}"
            return r
        data = resp.json()
        # Keep this smoke-test response in the same isolated root as the
        # other collectors when one is supplied.  This is intentionally
        # best-effort: the health check must still work if provenance capture
        # is unavailable.
        if root is not None and getattr(resp, "content", b""):
            try:
                from bot.source_registry import SourceRegistry

                SourceRegistry(Path(root)).capture_document(
                    "mops_material_openapi",
                    resp.content,
                    source_url=URL_MATERIAL_OPENAPI,
                    source_ref=dt.date.today().isoformat(),
                    title="MOPS 重大訊息 OpenAPI",
                    kind="http_response",
                    suffix="json",
                    mime_type="application/json",
                )
            except Exception:
                # Provenance is an enhancement to this smoke test, not a
                # reason to turn a working public endpoint into a failure.
                pass
        r.rows = len(data) if isinstance(data, list) else 0
        # OpenAPI「每日」資料量在盤後/假日可能為 0；視為 ok 但標註
        r.ok = isinstance(data, list)
        if r.rows == 0:
            r.note = "OpenAPI 每日重大訊息今日 0 筆 (盤後/假日正常；個股歷史走 mopsov fallback)"
    except Exception as e:  # noqa: BLE001
        r.error = f"{type(e).__name__}: {e}"
    return r


def check_chips(root: Path) -> CheckResult:
    from bot.chips_fetcher import build_chip_summary
    r = CheckResult(name="籌碼面/三大法人 (TWSE)", layer="parse")
    try:
        s = build_chip_summary("2330", days=5, root=root)
        r.rows = len(s.rows) if s and s.rows else 0
        r.ok = r.rows > 0
        r.details["expected_days"] = 5
        r.details["observed_days"] = r.rows
        if s and s.rows:
            r.sample_date = str(getattr(s.rows[-1], "date", "") or "")
            if r.rows < 5:
                r.note = f"近 5 個工作日取得 {r.rows} 日；最新日可能尚未發布或為休市日"
        if not r.ok:
            r.error = "近 5 日 0 筆 (盤後資料未更新或非交易日)"
    except Exception as e:  # noqa: BLE001
        r.error = f"{type(e).__name__}: {e}"
    return r


def check_distribution(root: Path) -> CheckResult:
    from bot.chip_distribution import build_distribution_snapshot
    r = CheckResult(name="集保/股權分散 (TDCC)", layer="parse")
    try:
        snap = build_distribution_snapshot("2330", root=root)
        ok = snap is not None and bool(getattr(snap, "levels", None) or getattr(snap, "rows", None))
        r.ok = bool(ok)
        r.rows = 1 if ok else 0
        if snap is not None:
            r.sample_date = str(getattr(snap, "date", "") or getattr(snap, "data_date", "") or "")
        if not r.ok:
            r.error = "無分散表資料"
    except Exception as e:  # noqa: BLE001
        r.error = f"{type(e).__name__}: {e}"
    return r


def check_news(root: Path) -> CheckResult:
    from bot.news_fetcher import fetch_today_news
    r = CheckResult(name="新聞 (鉅亨網)", layer="parse")
    try:
        items = fetch_today_news(limit=30, force_refresh=True, root=root)
        r.rows = len(items)
        r.ok = r.rows > 0
        if not r.ok:
            r.error = "0 條新聞"
    except Exception as e:  # noqa: BLE001
        r.error = f"{type(e).__name__}: {e}"
    return r


def check_web_search(root: Optional[Path] = None) -> CheckResult:
    from bot.web_search import search_web
    r = CheckResult(name="網路搜尋 (DuckDuckGo)", layer="parse")
    try:
        results = search_web("台積電 法說會", max_results=5, root=root)
        r.rows = len(results)
        r.ok = r.rows > 0
        if not r.ok:
            r.error = "0 筆搜尋結果"
    except Exception as e:  # noqa: BLE001
        r.error = f"{type(e).__name__}: {e}"
    return r


def check_macro(root: Path) -> CheckResult:
    from bot.market_macro import fetch_macro_snapshot
    r = CheckResult(name="總經 (yfinance: 美股/SOX/VIX/加權 + ADR 溢價)", layer="parse")
    try:
        snap = fetch_macro_snapshot(root=root, force_refresh=True)
        r.rows = len(snap.indices)
        r.details["adr_premiums"] = len(snap.adr_premiums)
        r.details["stocks"] = len(snap.stocks)
        r.sample_date = snap.asof_date
        r.ok = r.rows > 0
        if not r.ok:
            r.error = "無指數資料 (yfinance 被限流?)"
    except Exception as e:  # noqa: BLE001
        r.error = f"{type(e).__name__}: {e}"
    return r


def check_futures_basis(root: Path) -> CheckResult:
    """期貨領先指標：台指期近月 + 正逆價差 (TAIFEX)。"""
    from bot.market_macro import fetch_tx_futures_basis, fetch_macro_snapshot
    r = CheckResult(name="期貨領先指標/台指期正逆價差 (TAIFEX)", layer="parse")
    try:
        spot = 0.0
        try:
            snap = fetch_macro_snapshot(root=root)
            twii = snap.indices.get("^TWII")
            spot = twii.price if twii else 0.0
        except Exception:  # noqa: BLE001
            spot = 0.0
        fb = fetch_tx_futures_basis(spot, root=root)
        if fb is None:
            r.error = "TAIFEX 期貨行情抓取失敗"
            return r
        r.ok = fb.futures_price > 0
        r.rows = 1
        r.sample_date = fb.asof_date
        r.details = {
            "contract_month": fb.contract_month,
            "futures_price": fb.futures_price,
            "spot_price": fb.spot_price,
            "basis": fb.basis,
            "basis_pct": fb.basis_pct,
            "state": fb.state,
            "open_interest": fb.open_interest,
            "source_document_id": fb.source_document_id,
            "cached": fb.cached,
            "quality_status": fb.quality_status,
        }
        if fb.cached:
            # 能由 exact raw archive 回放代表流程可用，但不是 live 成功；
            # 讓摘要顯示 warning 而非把 stale cache 標成 OK。
            r.ok = False
            r.note = (
                f"TAIFEX live endpoint unavailable；已回放 exact raw cache ({fb.asof_date})，"
                "結果為 partial/stale，不代表即時行情"
            )
            r.quality_status = "partial"
        if spot <= 0:
            r.note = (r.note + "；" if r.note else "") + "現貨點數缺 (yfinance 未取得加權)，未算價差"
    except Exception as e:  # noqa: BLE001
        r.error = f"{type(e).__name__}: {e}"
    return r


# ----------------------------------------------------------------------
# 需憑證 / 即時的檢查
# ----------------------------------------------------------------------


def check_shioaji(
    attempt_login: bool,
    *,
    config_root: Optional[Path] = None,
) -> List[CheckResult]:
    """Shioaji 分段驗證：憑證存在 / 登入 / contract / snapshot。

    預設只回報「憑證是否存在」(不印金鑰)。加 --shioaji 才會嘗試登入。
    """
    from bot.config import Settings
    env_file = str((config_root or Path.cwd()).resolve() / ".env")
    s = Settings(_env_file=env_file)
    creds = CheckResult(name="Shioaji/憑證存在性", layer="link", needs_credentials=True)
    creds.details = {
        "api_key_present": bool(s.api_key),
        "secret_key_present": bool(s.secret_key),
        "ca_path_present": bool(s.ca_path),
        "simulation": bool(s.simulation),
    }
    creds.ok = bool(s.api_key and s.secret_key)
    if not creds.ok:
        creds.note = "缺 API_KEY / SECRET_KEY（需要券商憑證；公開資料檢查不受影響）"
    out = [creds]

    if not attempt_login:
        out.append(CheckResult(
            name="Shioaji/登入+快照", layer="http", needs_credentials=True,
            note="未啟用 (加 --shioaji 才會嘗試登入)",
        ))
        return out

    if not creds.ok:
        out.append(CheckResult(
            name="Shioaji/登入+快照", layer="http", needs_credentials=True,
            note="無憑證，略過登入（需要券商憑證）",
        ))
        return out

    login_r = CheckResult(name="Shioaji/登入", layer="http", needs_credentials=True)
    contract_r = CheckResult(name="Shioaji/contract(2330)", layer="http", needs_credentials=True)
    snap_r = CheckResult(name="Shioaji/snapshot(2330)", layer="parse", needs_credentials=True)
    broker = None
    try:
        from bot.broker import SjBroker
        broker = SjBroker(s)
        ok = broker.login()
        login_r.ok = bool(ok) and broker.api is not None
        if not login_r.ok:
            login_r.error = "登入失敗"
        else:
            contract = broker.get_contract("2330")
            contract_r.ok = contract is not None
            if not contract_r.ok:
                contract_r.error = "找不到 2330 合約"
            else:
                try:
                    snaps = broker.api.snapshots([contract])
                    snap_r.rows = len(snaps) if snaps else 0
                    snap_r.ok = snap_r.rows > 0
                    if snap_r.rows == 0:
                        snap_r.error = "snapshot 回 0 筆 (simulation 環境常無快照；以明確錯誤回報，不視為資料可用)"
                except Exception as e:  # noqa: BLE001
                    snap_r.error = f"{type(e).__name__}: {e}"
    except Exception as e:  # noqa: BLE001
        login_r.error = f"{type(e).__name__}: {e}"
    finally:
        if broker is not None:
            try:
                broker.logout()
            except Exception:  # noqa: BLE001
                pass
    out.extend([login_r, contract_r, snap_r])
    return out


# TWSE 常規交易時段（官網 MIS 即時資訊時段 09:00–13:30）。不以星期幾單獨斷言開市。
_MIS_SESSION_START = dt.time(9, 0)
_MIS_SESSION_END = dt.time(13, 30)
# MIS 盤中為即時資訊；新鮮 tick 須相對 now 在此秒數內。
_MIS_MAX_FRESH_AGE = dt.timedelta(seconds=60)


def _mis_time_of_day(value: dt.datetime) -> dt.time:
    """回傳不含 tz 的 time-of-day，供盤中窗比較。"""
    if value.tzinfo is not None:
        return value.timetz().replace(tzinfo=None)
    return value.time()


def assess_mis_quote_freshness(
    quote_dt: Optional[dt.datetime],
    *,
    now: Optional[dt.datetime] = None,
    max_age: dt.timedelta = _MIS_MAX_FRESH_AGE,
    session_start: dt.time = _MIS_SESSION_START,
    session_end: dt.time = _MIS_SESSION_END,
) -> Dict[str, Any]:
    """依來源報價日／時判斷是否像「新鮮盤中 tick」。

    規則（可辯護、不依賴「今天是星期幾 ⇒ 一定開市」）：
    1. 必須有可解析的來源 ``d``/``t`` 時間戳。
    2. ``now`` 本身須落在常規交易時段（09:00–13:30）。
    3. 報價日期須與台灣 ``now`` 的當日相同。
    4. 相對 ``now`` 的年齡 ≤ ``max_age``（預設 60 秒）。

    因此盤後查詢（例如 now=13:35、戳記 13:30）會因 ``now`` 已離開時段而
    ``fresh=False``，避免把收盤殘留列當成即時 tick。
    """
    clock = now or now_tw()
    if clock.tzinfo is not None and quote_dt is not None and quote_dt.tzinfo is None:
        quote_dt = quote_dt.replace(tzinfo=clock.tzinfo)
    elif clock.tzinfo is None and quote_dt is not None and quote_dt.tzinfo is not None:
        clock = clock.replace(tzinfo=quote_dt.tzinfo)

    now_tod = _mis_time_of_day(clock)
    now_in_session = session_start <= now_tod <= session_end
    payload: Dict[str, Any] = {
        "quote_at": quote_dt.isoformat(timespec="seconds") if quote_dt else "",
        "asof": clock.isoformat(timespec="seconds"),
        "age_seconds": None,
        "session_stamp": False,
        "now_in_session": now_in_session,
        "fresh": False,
        "reason": "missing_quote_timestamp",
        "max_age_seconds": int(max_age.total_seconds()),
        "session_window": f"{session_start.strftime('%H:%M')}-{session_end.strftime('%H:%M')}",
    }
    if quote_dt is None:
        return payload

    age = clock - quote_dt
    age_seconds = age.total_seconds()
    payload["age_seconds"] = int(age_seconds)
    stamp_tod = _mis_time_of_day(quote_dt)
    session_stamp = session_start <= stamp_tod <= session_end
    payload["session_stamp"] = session_stamp

    if not now_in_session:
        payload["reason"] = "outside_session_now"
        return payload
    if quote_dt.date() != clock.date():
        payload["reason"] = "quote_date_mismatch"
        return payload
    if age_seconds < 0:
        # 來源時鐘略快時仍允許在 max_age 內視為新鮮。
        if abs(age_seconds) <= max_age.total_seconds() and session_stamp:
            payload["fresh"] = True
            payload["reason"] = "fresh_in_session"
        else:
            payload["reason"] = "quote_in_future"
        return payload
    if not session_stamp:
        payload["reason"] = "outside_session_stamp"
        return payload
    if age > max_age:
        payload["reason"] = "stale_age"
        return payload
    payload["fresh"] = True
    payload["reason"] = "fresh_in_session"
    return payload


def _quote_datetime_from_payload(quote: Dict[str, Any]) -> Optional[dt.datetime]:
    """Prefer raw MIS ``d``/``t``; fall back to normalized quote_date/quote_time.

    Parses ``d``/``t`` locally (same ``%Y%m%d %H:%M:%S`` + Taiwan tz as market
    source) so tests that replace ``TwsePublicMarketSource`` do not turn the
    parser into a MagicMock.
    """
    raw = quote.get("raw")
    if isinstance(raw, dict):
        raw_date = str(raw.get("d") or "").strip()
        raw_time = str(raw.get("t") or "").strip()
        if len(raw_date) == 8 and raw_date.isdigit() and raw_time:
            try:
                parsed = dt.datetime.strptime(
                    f"{raw_date} {raw_time}", "%Y%m%d %H:%M:%S",
                )
                return parsed.replace(tzinfo=now_tw().tzinfo)
            except ValueError:
                pass
    quote_date = str(quote.get("quote_date") or "").strip()
    quote_time = str(quote.get("quote_time") or "").strip()
    if not quote_date or not quote_time:
        return None
    try:
        parsed = dt.datetime.strptime(
            f"{quote_date} {quote_time}", "%Y-%m-%d %H:%M:%S",
        )
    except ValueError:
        return None
    return parsed.replace(tzinfo=now_tw().tzinfo)


def check_mis(
    seconds: int,
    *,
    now: Optional[dt.datetime] = None,
) -> CheckResult:
    """TWSE MIS 即時報價：前收價 + 以來源時間戳判斷新鮮盤中 tick。

    不把第一次 poll 的 volume baseline（相對 ``-1``）當成 live tick；
    必須通過 :func:`assess_mis_quote_freshness`。
    """
    from bot.market_source import TwsePublicMarketSource

    r = CheckResult(name="TWSE MIS/即時報價 (連續 poll)", layer="parse")
    symbols = ["2330", "2317"]
    r.details["freshness_rule"] = {
        "session_window": (
            f"{_MIS_SESSION_START.strftime('%H:%M')}-"
            f"{_MIS_SESSION_END.strftime('%H:%M')}"
        ),
        "max_age_seconds": int(_MIS_MAX_FRESH_AGE.total_seconds()),
        "note": (
            "now 須在常規時段；來源 d/t 須為台灣當日且年齡 ≤60 秒；"
            "不以星期幾單獨斷言開市"
        ),
    }
    try:
        src = TwsePublicMarketSource(symbols, poll_seconds=2)
        prev = src.get_prev_close(symbols)
        r.details["prev_close"] = prev
        deadline = time.time() + max(2, seconds)
        fresh_total = 0
        polls = 0
        latest_quotes: List[Dict[str, Any]] = []
        while time.time() < deadline:
            clock = now if now is not None else now_tw()
            quotes = src.get_quotes()
            polls += 1
            snapshot: List[Dict[str, Any]] = []
            poll_fresh = 0
            for code in symbols:
                quote = quotes.get(code) if isinstance(quotes, dict) else None
                if not isinstance(quote, dict):
                    snapshot.append({
                        "symbol": code,
                        "fresh": False,
                        "reason": "missing_quote",
                        "quote_at": "",
                        "age_seconds": None,
                        "session_stamp": False,
                    })
                    continue
                quote_dt = _quote_datetime_from_payload(quote)
                assessment = assess_mis_quote_freshness(quote_dt, now=clock)
                row = {
                    "symbol": code,
                    "price": quote.get("price"),
                    "volume": quote.get("volume"),
                    "quote_date": quote.get("quote_date") or "",
                    "quote_time": quote.get("quote_time") or "",
                    **assessment,
                }
                snapshot.append(row)
                if assessment.get("fresh"):
                    poll_fresh += 1
            latest_quotes = snapshot
            fresh_total = poll_fresh
            if fresh_total > 0:
                break
            time.sleep(2)
        r.rows = fresh_total
        r.details["polls"] = polls
        r.details["quotes"] = latest_quotes
        r.details["fresh_tick_count"] = fresh_total
        if fresh_total > 0:
            r.ok = True
            r.note = f"{polls} 次輪詢取得 {fresh_total} 筆新鮮盤中 tick"
            r.sample_date = next(
                (str(q.get("quote_date") or "") for q in latest_quotes if q.get("fresh")),
                "",
            )
        elif prev:
            r.ok = True
            r.note = (
                "非盤中／無新鮮 tick：取得前收價但無即時新鮮報價 "
                "(盤後殘留列不計入 live tick，已明確標示)"
            )
        else:
            r.error = "無前收價亦無新鮮 tick"
    except Exception as e:  # noqa: BLE001
        r.error = f"{type(e).__name__}: {e}"
    return r


def check_cloud(
    live: bool,
    *,
    config_root: Optional[Path] = None,
) -> CheckResult:
    """雲端 (Google Sheets/Drive)：預設只看設定存在性 (mock)，--cloud 才 live ping。"""
    from bot.config import Settings
    env_file = str((config_root or Path.cwd()).resolve() / ".env")
    s = Settings(_env_file=env_file)
    r = CheckResult(name="雲端/Google Sheets-Drive", layer="link", needs_credentials=True)
    r.details = {
        "google_sheet_id_present": bool(s.google_sheet_id),
        "google_sa_json_present": bool(s.google_sa_json_path) and Path(s.google_sa_json_path).exists()
        if s.google_sa_json_path else False,
        "google_cache_dir_present": bool(s.google_cache_dir),
    }
    if not live:
        r.ok = True
        r.note = "僅檢查設定存在性 (加 --cloud 才 live ping)"
        return r
    try:
        from bot.cloud_sync import GoogleSheetSync, load_config_from_env
        cfg = load_config_from_env(config_root)
        if not cfg.enabled:
            r.error = "雲端未設定 (GOOGLE_SHEET_ID / GOOGLE_SA_JSON_PATH)"
            return r
        title = GoogleSheetSync(cfg).ping()
        r.ok = bool(title)
        r.note = f"live ping 成功 (sheet: {title})" if r.ok else "ping 無回應"
    except Exception as e:  # noqa: BLE001
        r.error = f"{type(e).__name__}: {e}"
    return r


# ----------------------------------------------------------------------
# 主流程 / 報表
# ----------------------------------------------------------------------


def run_all(
    *,
    root: Optional[Path] = None,
    attempt_shioaji: bool = False,
    mis_seconds: int = 0,
    cloud_live: bool = False,
) -> List[CheckResult]:
    """跑全部 read-only 檢查；未指定 root 時使用並自動清理暫存 root。"""
    config_root = (root or Path.cwd()).resolve()
    temp_root = None
    if root is None:
        temp_root = Path(tempfile.mkdtemp(prefix="validate_sources_"))
        root = temp_root
    try:
        return _run_all_checks(
            root=Path(root),
            config_root=config_root,
            attempt_shioaji=attempt_shioaji,
            mis_seconds=mis_seconds,
            cloud_live=cloud_live,
        )
    finally:
        if temp_root is not None:
            shutil.rmtree(temp_root, ignore_errors=True)


def _run_all_checks(
    *,
    root: Path,
    config_root: Path,
    attempt_shioaji: bool,
    mis_seconds: int,
    cloud_live: bool,
) -> List[CheckResult]:
    results: List[CheckResult] = []

    public_checks: List[Callable[[], CheckResult]] = [
        lambda: check_company_info(root),
        lambda: check_daily_kline(root),
        lambda: check_monthly_revenue(root),
        lambda: check_valuation(root),
        lambda: check_dividends(root),
        lambda: check_quarterly(root),
        lambda: check_mops_conference(root),
        lambda: check_global_events(root),
        lambda: check_mops_material(root),
        lambda: check_chips(root),
        lambda: check_distribution(root),
        lambda: check_news(root),
        lambda: check_web_search(root),
        lambda: check_macro(root),
        lambda: check_futures_basis(root),
    ]
    for fn in public_checks:
        try:
            results.append(fn())
        except Exception as e:  # noqa: BLE001
            results.append(CheckResult(name=getattr(fn, "__name__", "unknown"), error=f"{type(e).__name__}: {e}"))

    results.extend(check_etf_urls(root))
    results.extend(check_shioaji(attempt_shioaji, config_root=config_root))
    if mis_seconds and mis_seconds > 0:
        results.append(check_mis(mis_seconds))
    else:
        results.append(CheckResult(
            name="TWSE MIS/即時報價 (連續 poll)", layer="parse",
            note="未啟用 (加 --mis-seconds N 才會連續輪詢)",
        ))
    results.append(check_cloud(cloud_live, config_root=config_root))
    return results


def summarize(results: List[CheckResult]) -> Dict[str, Any]:
    ok = sum(1 for r in results if r.ok)
    fail = sum(1 for r in results if not r.ok and not r.note)
    warn = sum(1 for r in results if not r.ok and r.note)
    return {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "total": len(results),
        "ok": ok,
        "warn": warn,
        "fail": fail,
        "results": [r.to_dict() for r in results],
    }


def to_markdown(summary: Dict[str, Any]) -> str:
    lines = [
        "# 資料源驗證報告",
        "",
        f"產生時間：{summary['generated_at']}",
        "",
        f"**總計 {summary['total']}｜✅ OK {summary['ok']}｜⚠️ 警告 {summary['warn']}｜❌ 失敗 {summary['fail']}**",
        "",
        "| 狀態 | 資料源 | 層級 | 筆數 | 樣本日 | 備註 / 錯誤 |",
        "|:----:|--------|:----:|-----:|--------|------------|",
    ]
    for r in summary["results"]:
        if r["ok"]:
            icon = "✅"
        elif r["note"]:
            icon = "⚠️"
        else:
            icon = "❌"
        msg = r["error"] or r["note"] or ""
        lines.append(
            f"| {icon} | {r['name']} | {r['layer']} | {r['rows']} | {r['sample_date']} | {msg} |"
        )
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    configure_console()
    parser = argparse.ArgumentParser(description="資料源健康度 smoke test；預設使用暫存 data root，明確指定 --root 時可能寫入選定目錄樹")
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help=(
            "專案資料／設定根目錄（未指定時資料檢查用暫存目錄；"
            "明確指定時該 root 會傳給收集器，可能寫入 cache/raw/provenance；"
            "設定檔仍：未指定用 CWD，有指定用該 root）"
        ),
    )
    parser.add_argument("--json", dest="json_path", default="", help="另存 JSON 報告路徑")
    parser.add_argument("--md", dest="md_path", default="", help="另存 Markdown 報告路徑")
    parser.add_argument("--shioaji", action="store_true", help="嘗試 Shioaji 登入+快照 (需憑證)")
    parser.add_argument("--mis-seconds", type=int, default=0, help="MIS 即時 tick 連續輪詢秒數")
    parser.add_argument("--cloud", action="store_true", help="對雲端做 live ping (預設只檢查設定)")
    args = parser.parse_args(argv)

    project_root = args.root.resolve() if args.root else Path.cwd().resolve()

    with log_root(project_root):
        results = run_all(
            root=args.root,
            attempt_shioaji=args.shioaji,
            mis_seconds=args.mis_seconds,
            cloud_live=args.cloud,
        )
    summary = summarize(results)
    md = to_markdown(summary)

    # 先寫檔 (utf-8)，再印主控台，避免主控台編碼問題影響檔案輸出
    if args.json_path:
        json_path = Path(args.json_path)
        if not json_path.is_absolute():
            json_path = project_root / json_path
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    if args.md_path:
        md_path = Path(args.md_path)
        if not md_path.is_absolute():
            md_path = project_root / md_path
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(md, encoding="utf-8")

    try:
        print(md)
    except Exception:  # noqa: BLE001
        print(md.encode("ascii", "replace").decode("ascii"))
    if args.json_path:
        print(f"\nJSON 已寫入 {json_path}")
    if args.md_path:
        print(f"Markdown 已寫入 {md_path}")

    # 有「真正失敗 (非警告)」時回傳非 0，方便 CI
    return 0 if summary["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
