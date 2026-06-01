"""etf_holdings_fetcher -- 自動抓取主動式 ETF 官方持股頁面，並用 Gemini 抽取 JSON。

流程：
1. 依 ActiveEtf.holdings_url 抓取網頁 HTML / PDF
2. 簡單 normalize 成純文字 (BeautifulSoup → text，PDF → pypdf)
3. 呼叫 `extract_etf_holdings` prompt 取得結構化 JSON 陣列
4. 轉成 HoldingsSnapshot 並 save_holdings()

設計：
* 不在 import 時強制 BS4/lxml/pypdf
* 即使 LLM 無回應仍會保留原始文字到 data/etf_holdings_raw/，便於追蹤失敗原因
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import requests

from bot.active_etf import (
    ActiveEtf,
    Holding,
    HoldingsSnapshot,
    load_active_etfs,
    save_holdings,
)
from bot.cloud_file_cache import mirror_file_to_cloud
from bot.llm_analyzer import GeminiClient, extract_json, gemini_call
from bot.utils import get_logger, mk_folder, now_tw

try:
    from bs4 import BeautifulSoup  # type: ignore
    _HAS_BS4 = True
except Exception:
    _HAS_BS4 = False

try:
    from pypdf import PdfReader  # type: ignore
    _HAS_PYPDF = True
except Exception:
    _HAS_PYPDF = False


# ----------------------------------------------------------------------
# 結果模型
# ----------------------------------------------------------------------


@dataclass
class FetchResult:
    etf: ActiveEtf
    success: bool = False
    snapshot_date: Optional[dt.date] = None
    holdings_count: int = 0
    saved_path: Optional[Path] = None
    raw_text_path: Optional[Path] = None
    error: str = ""
    prompt_id: str = ""
    prompt_version: str = ""
    elapsed_ms: int = 0
    llm_metadata: Dict = field(default_factory=dict)


# ----------------------------------------------------------------------
# HTTP 工具
# ----------------------------------------------------------------------


def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    })
    return s


def _html_to_text(html: str) -> str:
    if _HAS_BS4:
        try:
            soup = BeautifulSoup(html, "lxml" if _has_lxml() else "html.parser")
            for s in soup(["script", "style", "noscript"]):
                s.decompose()
            text = soup.get_text("\n", strip=True)
            return re.sub(r"\n{3,}", "\n\n", text)
        except Exception:
            pass
    return re.sub(r"<[^>]+>", "", html)


def _pdf_to_text(content: bytes, tmp_path: Path) -> str:
    if not _HAS_PYPDF:
        return ""
    try:
        tmp_path.write_bytes(content)
        reader = PdfReader(str(tmp_path))
        parts: List[str] = []
        for p in reader.pages[:60]:
            try:
                parts.append(p.extract_text() or "")
            except Exception:
                continue
        return "\n\n".join(parts)
    except Exception:
        return ""


def _has_lxml() -> bool:
    try:
        import lxml  # noqa: F401
        return True
    except Exception:
        return False


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------


def fetch_and_save_holdings(
    etf: ActiveEtf,
    client: GeminiClient,
    *,
    snapshot_date: Optional[dt.date] = None,
    root: Optional[Path] = None,
    session: Optional[requests.Session] = None,
    logger: Optional[logging.Logger] = None,
) -> FetchResult:
    """自動抓 + LLM 解析 + 存檔 一條龍。"""
    log = logger or get_logger("etf-fetcher")
    sess = session or _new_session()
    result = FetchResult(etf=etf, snapshot_date=snapshot_date or now_tw().date())

    if not etf.holdings_url:
        result.error = "no_holdings_url"
        log.info("[%s] 未設定 holdings_url，跳過", etf.symbol)
        return result

    # ---- 1. 下載原始內容 ----
    try:
        resp = sess.get(etf.holdings_url, timeout=30)
        resp.raise_for_status()
        content_type = (resp.headers.get("content-type") or "").lower()
        is_pdf = (
            "pdf" in content_type or etf.holdings_url.lower().endswith(".pdf")
        )
        raw_text = ""
        if is_pdf:
            raw_dir = (root or Path.cwd()) / "data" / "etf_holdings_raw" / etf.symbol
            mk_folder(str(raw_dir))
            tmp = raw_dir / f"{result.snapshot_date.isoformat()}.pdf"
            raw_text = _pdf_to_text(resp.content, tmp)
            mirror_file_to_cloud(tmp, root=root)
            result.raw_text_path = tmp
        else:
            try:
                resp.encoding = resp.apparent_encoding or "utf-8"
            except Exception:
                resp.encoding = "utf-8"
            raw_text = _html_to_text(resp.text)
            raw_dir = (root or Path.cwd()) / "data" / "etf_holdings_raw" / etf.symbol
            mk_folder(str(raw_dir))
            tmp = raw_dir / f"{result.snapshot_date.isoformat()}.txt"
            tmp.write_text(raw_text, encoding="utf-8")
            mirror_file_to_cloud(tmp, root=root)
            result.raw_text_path = tmp

        if not raw_text or len(raw_text.strip()) < 50:
            result.error = "raw_text_too_short"
            log.warning("[%s] 原始內容過短 (%d 字)", etf.symbol, len(raw_text))
            return result
    except Exception as e:
        result.error = f"fetch_failed: {e}"
        log.exception("[%s] 下載失敗", etf.symbol)
        return result

    # ---- 2. 呼叫 Gemini 抽 JSON ----
    if not client.enabled:
        result.error = "llm_disabled"
        log.warning("[%s] LLM 未啟用，原始內容已存於 %s", etf.symbol, result.raw_text_path)
        return result

    raw_text_for_llm = raw_text[:60000]
    raw_json, info = gemini_call(
        "extract_etf_holdings",
        client=client,
        metadata={"etf_symbol": etf.symbol, "task": "extract_etf_holdings"},
        etf_symbol=etf.symbol,
        etf_name=etf.name,
        source_text=raw_text_for_llm,
    )
    result.prompt_id = info.get("prompt_id", "")
    result.prompt_version = info.get("prompt_version", "")
    result.elapsed_ms = info.get("latency_ms", 0)
    result.llm_metadata = info

    if raw_json is None:
        result.error = "llm_no_response"
        return result

    data = extract_json(raw_json)
    holdings: List[Holding] = []
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            ticker = str(item.get("ticker") or "").strip()
            if not ticker or not re.match(r"^\w{4,6}\.?\w*$", ticker):
                continue
            try:
                holdings.append(Holding(
                    ticker=ticker,
                    name=str(item.get("name") or ""),
                    weight_pct=float(item.get("weight_pct") or 0),
                    shares=float(item.get("shares") or 0),
                    value=float(item.get("value") or 0),
                ))
            except (ValueError, TypeError):
                continue

    if not holdings:
        result.error = "no_holdings_parsed"
        log.warning("[%s] LLM 抽不出持股 (raw output 開頭: %s)", etf.symbol, (raw_json or "")[:200])
        return result

    # ---- 3. 存檔 ----
    snap = HoldingsSnapshot(
        symbol=etf.symbol,
        date=result.snapshot_date,
        holdings=holdings,
    )
    saved = save_holdings(snap, root)
    result.success = True
    result.holdings_count = len(holdings)
    result.saved_path = saved
    log.info(
        "[%s] 自動更新 %d 檔持股 → %s", etf.symbol, len(holdings), saved.name,
    )
    return result


def fetch_all_active_etfs(
    client: GeminiClient,
    *,
    snapshot_date: Optional[dt.date] = None,
    root: Optional[Path] = None,
    only_symbols: Optional[List[str]] = None,
    logger: Optional[logging.Logger] = None,
) -> List[FetchResult]:
    """跑一輪：抓所有有 holdings_url 的主動式 ETF。"""
    log = logger or get_logger("etf-fetcher")
    sess = _new_session()
    etfs = load_active_etfs(root)
    if only_symbols:
        wanted = set(only_symbols)
        etfs = [e for e in etfs if e.symbol in wanted]
    results: List[FetchResult] = []
    for e in etfs:
        if not e.holdings_url:
            log.debug("[%s] 略過 (無 holdings_url)", e.symbol)
            continue
        try:
            r = fetch_and_save_holdings(
                e, client, snapshot_date=snapshot_date,
                root=root, session=sess, logger=log,
            )
            results.append(r)
        except Exception:
            log.exception("[%s] 例外", e.symbol)
            results.append(FetchResult(etf=e, error="exception"))
    log.info(
        "ETF 持股自動抓取完成：%d 成功 / %d 失敗",
        sum(1 for r in results if r.success),
        sum(1 for r in results if not r.success),
    )
    return results


__all__ = [
    "FetchResult",
    "fetch_all_active_etfs",
    "fetch_and_save_holdings",
]
