"""validate_sources -- 資料源「只讀」健康度 smoke test。

定位
====
不改動正式 ``data/``、不下單、不寫雲端：所有抓取都導向暫存資料夾，
逐一檢查每個外部資料源並輸出 JSON / Markdown 摘要。

三層驗證
========
* ``layer="link"``   -- 連結有填、格式正確
* ``layer="http"``   -- HTTP 能抓到有效內容 (status 200 + 非空/非阻擋頁)
* ``layer="parse"``  -- 解析後能被程式流程使用 (rows > 0 / 可建立 record)

安全
====
* 只顯示憑證「是否存在」(present=true/false)，**絕不印出金鑰內容**。
* Shioaji / MIS / 雲端 live ping 預設關閉，需以旗標明確開啟。

用法
====
```bash
uv run stock-validate                       # 預設全部公開資料源 (read-only)
uv run stock-validate --json out.json       # 另存 JSON
uv run stock-validate --md out.md           # 另存 Markdown
uv run stock-validate --shioaji             # 額外嘗試 Shioaji 登入/快照 (需憑證)
uv run stock-validate --mis-seconds 30      # MIS 即時 tick 連續輪詢秒數
```
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

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
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "layer": self.layer,
            "ok": self.ok,
            "rows": self.rows,
            "sample_date": self.sample_date,
            "error": self.error,
            "note": self.note,
            "needs_credentials": self.needs_credentials,
            "details": self.details,
        }


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
        if not r.ok:
            r.error = "回傳 0 筆"
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
        if not r.ok:
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
        if not r.ok:
            r.error = "回傳 0 筆"
    except Exception as e:  # noqa: BLE001
        r.error = f"{type(e).__name__}: {e}"
    return r


# 來源頁出現「持股明細表」的最少權重百分比 token 數 (近似判斷，真正解析由 LLM 完成)。
# 空頁/免責頁通常只有少數小數；真正持股頁 (10+ 檔，各帶權重%) 會出現多個 "xx.xx"。
_MIN_WEIGHT_TOKENS = 8


def check_etf_urls(root: Path) -> list[CheckResult]:
    """逐檔 ETF：HTTP 200 (layer=http) + 來源頁是否含持股明細 (layer=parse 的非 LLM 近似)。

    layer=parse 僅近似 (數權重百分比 token)；真正的持股 CSV 由 LLM 抓取流程產生。
    """
    from bot.active_etf import load_active_etfs
    from bot.etf_holdings_fetcher import _html_to_text  # type: ignore

    out: list[CheckResult] = []
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


def check_mops_conference() -> CheckResult:
    from bot.mops_scraper import fetch_conference_schedule
    r = CheckResult(name="MOPS/法說會行事曆 (mopsov ajax_t100sb02_1)", layer="parse")
    today = dt.date.today()
    roc = today.year - 1911
    try:
        # 試本月與上月，至少一個月份 > 0
        best = 0
        for m in (today.month, max(1, today.month - 1)):
            entries = fetch_conference_schedule(roc, m)
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


def check_mops_material() -> CheckResult:
    """重大訊息：OpenAPI t187ap04_L 全市場當日筆數 + 不可為安全性阻擋。"""
    from bot.mops_scraper import URL_MATERIAL_OPENAPI
    r = CheckResult(name="MOPS/重大訊息 (OpenAPI t187ap04_L)", layer="parse")
    try:
        resp = _session().get(URL_MATERIAL_OPENAPI, timeout=20)
        if resp.status_code != 200:
            r.error = f"HTTP {resp.status_code}"
            return r
        data = resp.json()
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
        if s and s.rows:
            r.sample_date = str(getattr(s.rows[-1], "date", "") or "")
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


def check_web_search() -> CheckResult:
    from bot.web_search import search_web
    r = CheckResult(name="網路搜尋 (DuckDuckGo)", layer="parse")
    try:
        results = search_web("台積電 法說會", max_results=5)
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
    from bot.market_macro import fetch_macro_snapshot, fetch_tx_futures_basis
    r = CheckResult(name="期貨領先指標/台指期正逆價差 (TAIFEX)", layer="parse")
    try:
        spot = 0.0
        try:
            snap = fetch_macro_snapshot(root=root)
            twii = snap.indices.get("^TWII")
            spot = twii.price if twii else 0.0
        except Exception:  # noqa: BLE001
            spot = 0.0
        fb = fetch_tx_futures_basis(spot)
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
        }
        if spot <= 0:
            r.note = "現貨點數缺 (yfinance 未取得加權)，僅驗證期貨可抓、未算價差"
    except Exception as e:  # noqa: BLE001
        r.error = f"{type(e).__name__}: {e}"
    return r


# ----------------------------------------------------------------------
# 需憑證 / 即時的檢查
# ----------------------------------------------------------------------


def check_shioaji(attempt_login: bool) -> list[CheckResult]:
    """Shioaji 分段驗證：憑證存在 / 登入 / contract / snapshot。

    預設只回報「憑證是否存在」(不印金鑰)。加 --shioaji 才會嘗試登入。
    """
    from bot.config import Settings
    s = Settings()
    creds = CheckResult(name="Shioaji/憑證存在性", layer="link", needs_credentials=True)
    creds.details = {
        "api_key_present": bool(s.api_key),
        "secret_key_present": bool(s.secret_key),
        "ca_path_present": bool(s.ca_path),
        "simulation": bool(s.simulation),
    }
    creds.ok = bool(s.api_key and s.secret_key)
    if not creds.ok:
        creds.error = "缺 API_KEY / SECRET_KEY"
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
            error="無憑證，略過登入",
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


def check_mis(seconds: int) -> CheckResult:
    """TWSE MIS 即時報價：前收價 + 連續輪詢取 tick。"""
    from bot.market_source import TwsePublicMarketSource
    r = CheckResult(name="TWSE MIS/即時報價 (連續 poll)", layer="parse")
    symbols = ["2330", "2317"]
    try:
        src = TwsePublicMarketSource(symbols, poll_seconds=2)
        prev = src.get_prev_close(symbols)
        r.details["prev_close"] = prev
        deadline = time.time() + max(2, seconds)
        tick_total = 0
        polls = 0
        while time.time() < deadline:
            ticks = src.poll()
            tick_total += len(ticks)
            polls += 1
            if tick_total > 0:
                break
            time.sleep(2)
        r.rows = tick_total
        r.details["polls"] = polls
        if tick_total > 0:
            r.ok = True
            r.note = f"{polls} 次輪詢取得 {tick_total} 筆 tick"
        elif prev:
            r.ok = True
            r.note = "非盤中：取得前收價但無即時 tick (盤中限制，已明確標示)"
        else:
            r.error = "無前收價亦無 tick"
    except Exception as e:  # noqa: BLE001
        r.error = f"{type(e).__name__}: {e}"
    return r


def check_cloud(live: bool) -> CheckResult:
    """雲端 (Google Sheets/Drive)：預設只看設定存在性 (mock)，--cloud 才 live ping。"""
    from bot.config import Settings
    s = Settings()
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
        cfg = load_config_from_env()
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
    root: Path | None = None,
    attempt_shioaji: bool = False,
    mis_seconds: int = 0,
    cloud_live: bool = False,
) -> list[CheckResult]:
    """跑全部 read-only 檢查 (寫入導向暫存 root)。"""
    tmp = Path(tempfile.mkdtemp(prefix="validate_sources_"))
    root = root or tmp
    results: list[CheckResult] = []

    public_checks: list[Callable[[], CheckResult]] = [
        lambda: check_monthly_revenue(root),
        lambda: check_valuation(root),
        lambda: check_dividends(root),
        lambda: check_quarterly(root),
        lambda: check_mops_conference(),
        lambda: check_global_events(root),
        lambda: check_mops_material(),
        lambda: check_chips(root),
        lambda: check_distribution(root),
        lambda: check_news(root),
        lambda: check_web_search(),
        lambda: check_macro(root),
        lambda: check_futures_basis(root),
    ]
    for fn in public_checks:
        try:
            results.append(fn())
        except Exception as e:  # noqa: BLE001
            results.append(CheckResult(name=getattr(fn, "__name__", "unknown"), error=f"{type(e).__name__}: {e}"))

    results.extend(check_etf_urls(root))
    results.extend(check_shioaji(attempt_shioaji))
    if mis_seconds and mis_seconds > 0:
        results.append(check_mis(mis_seconds))
    else:
        results.append(CheckResult(
            name="TWSE MIS/即時報價 (連續 poll)", layer="parse",
            note="未啟用 (加 --mis-seconds N 才會連續輪詢)",
        ))
    results.append(check_cloud(cloud_live))
    return results


def summarize(results: list[CheckResult]) -> dict[str, Any]:
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


def to_markdown(summary: dict[str, Any]) -> str:
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="資料源只讀健康度 smoke test")
    parser.add_argument("--json", dest="json_path", default="", help="另存 JSON 報告路徑")
    parser.add_argument("--md", dest="md_path", default="", help="另存 Markdown 報告路徑")
    parser.add_argument("--shioaji", action="store_true", help="嘗試 Shioaji 登入+快照 (需憑證)")
    parser.add_argument("--mis-seconds", type=int, default=0, help="MIS 即時 tick 連續輪詢秒數")
    parser.add_argument("--cloud", action="store_true", help="對雲端做 live ping (預設只檢查設定)")
    args = parser.parse_args(argv)

    # Windows 主控台多為 cp950，輸出 emoji/中文易炸 → 盡量切到 utf-8、不行就 replace
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    results = run_all(
        attempt_shioaji=args.shioaji,
        mis_seconds=args.mis_seconds,
        cloud_live=args.cloud,
    )
    summary = summarize(results)
    md = to_markdown(summary)

    # 先寫檔 (utf-8)，再印主控台，避免主控台編碼問題影響檔案輸出
    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    if args.md_path:
        Path(args.md_path).write_text(md, encoding="utf-8")

    try:
        print(md)
    except Exception:  # noqa: BLE001
        print(md.encode("ascii", "replace").decode("ascii"))
    if args.json_path:
        print(f"\nJSON 已寫入 {args.json_path}")
    if args.md_path:
        print(f"Markdown 已寫入 {args.md_path}")

    # 有「真正失敗 (非警告)」時回傳非 0，方便 CI
    return 0 if summary["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
