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
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

from bot.active_etf import (
    ActiveEtf,
    Holding,
    HoldingsSnapshot,
    load_active_etfs,
    save_holdings,
)
from bot.cloud_file_cache import mirror_file_to_cloud
from bot.source_registry import SourceRegistry
from bot.fund_holdings_parser import (
    is_moneydj_fund_holdings_url,
    parse_moneydj_fund_holdings_html,
)
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
    source_document_id: str = ""
    research_status: str = ""
    research_document_id: str = ""
    research_evidence_id: str = ""
    research_error: str = ""
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


# 第三方/發行商頁面在「新掛牌尚未公開持股」時常見的提示字樣。
_NO_DATA_MARKERS = (
    "查無資料",
    "查無持股",
    "尚無資料",
    "暫無資料",
    "無持股資料",
    "no data",
    "no holdings",
    "data not available",
)

_DERIVATIVE_ASSET_TYPES = {
    "derivative", "future", "futures", "option", "swap",
}


def _is_non_exposure_etf_record(record: Dict[str, object]) -> bool:
    """判斷 ETF payload 中沒有權重的現金／結算餘額列。

    這些列仍保留在 raw source document，可供日後擴充資產配置模型；目前
    ``Holding`` 是持股曝險模型，不能把「權重未知」猜成 0%。只有來源明確
    使用常見現金／應收／應付款代號時才安全排除，未知 null 權重仍拒絕解析。
    """
    code = str(record.get("code") or "").strip().upper()
    name = str(record.get("name") or "").strip().upper()
    asset_type = str(record.get("assetType") or "").strip().lower()
    if asset_type in {
        "cash", "cash_equivalent", "receivable", "payable",
        *_DERIVATIVE_ASSET_TYPES,
    }:
        return True
    if code.startswith(("C_", "DR_", "PFUR_")):
        return True
    return any(marker in name for marker in ("CASH", "RECEIVABLE", "PAYABLE"))


def _parse_etfinfo_holdings_html(
    html: str,
    *,
    symbol: str = "",
) -> Optional[Tuple[Optional[dt.date], List[Holding]]]:
    """確定性解析 ETF 資訊網的公開持股表。

    這是刻意狹窄的 parser：只有找到明確的持股表欄位、股票代號／名稱、
    權重與股數時才回傳結果；頁面改版或資料不完整就回傳 ``None``，讓呼叫端
    保留原始內容並回退 Gemini，而不是產生看似完整的錯誤快照。
    """
    if not _HAS_BS4 or not html:
        return None
    try:
        soup = BeautifulSoup(html, "lxml" if _has_lxml() else "html.parser")
    except Exception:
        return None

    # etfinfo.tw SSR 會把完整持股放在 Nuxt payload，HTML table 只呈現第 1 頁。
    # 優先讀 payload，避免把 top-15 誤當成完整快照。
    payload_result = _parse_etfinfo_nuxt_payload(html, symbol=symbol)
    if payload_result is not None:
        return payload_result

    # 沒有 payload 時只接受沒有 pagination 的完整 table；有分頁就拒絕，
    # 讓呼叫端回退 LLM，不產生不完整 canonical snapshot。
    if re.search(r"\b\d+\s*/\s*\d+\b", BeautifulSoup(html, "html.parser").get_text(" ", strip=True)):
        return None

    table = None
    weight_index: Optional[int] = None
    shares_index: Optional[int] = None
    for candidate in soup.find_all("table"):
        header = candidate.find("tr")
        if header is None:
            continue
        headers = header.find_all("th", recursive=False)
        if not headers:
            continue
        labels = [h.get_text(" ", strip=True) for h in headers]
        for index, label in enumerate(labels):
            if "權重" in label and "股數" not in label:
                weight_index = index
            if "股數" in label and "權重" not in label:
                shares_index = index
        if weight_index is not None and shares_index is not None:
            table = candidate
            break
        weight_index = shares_index = None

    if table is None or weight_index is None or shares_index is None:
        return None

    holdings: List[Holding] = []
    for row in table.find_all("tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) <= max(weight_index, shares_index):
            continue
        code_node = row.select_one(".stock-code-link")
        name_node = row.select_one(".stock-name-sub")
        ticker = code_node.get_text(" ", strip=True) if code_node else ""
        name = name_node.get_text(" ", strip=True) if name_node else ""
        if not ticker or not name:
            continue

        weight_match = re.search(r"[-+]?\d+(?:\.\d+)?\s*%", cells[weight_index].get_text(" ", strip=True))
        if not weight_match:
            continue
        try:
            weight_pct = float(weight_match.group(0).replace("%", "").strip())
        except ValueError:
            continue
        if weight_pct <= 0:
            continue

        shares_text = cells[shares_index].get_text(" ", strip=True)
        shares_match = re.search(r"[-+]?\d[\d,]*(?:\.\d+)?", shares_text)
        try:
            shares = float(shares_match.group(0).replace(",", "")) if shares_match else 0.0
        except ValueError:
            shares = 0.0
        holdings.append(Holding(
            ticker=ticker,
            name=name,
            weight_pct=weight_pct,
            shares=shares,
        ))

    if not holdings:
        return None

    as_of: Optional[dt.date] = None
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    date_match = re.search(r"(20\d{2}-\d{2}-\d{2})", title)
    if date_match:
        try:
            as_of = dt.date.fromisoformat(date_match.group(1))
        except ValueError:
            as_of = None
    return as_of, holdings


def _parse_etfinfo_nuxt_payload(
    html: str,
    *,
    symbol: str,
) -> Optional[Tuple[Optional[dt.date], List[Holding]]]:
    """從 etfinfo.tw 的 SSR payload 取完整 holdings 陣列。

    Nuxt payload 的物件值以陣列索引互相引用；此 resolver 只處理這個來源
    已觀察到的 JSON／ShallowReactive 結構，遇到改版或循環就回傳 ``None``。
    """
    if not symbol:
        return None
    try:
        soup = BeautifulSoup(html, "lxml" if _has_lxml() else "html.parser")
        node = soup.find("script", {"type": "application/json"})
        if node is None or not node.string:
            return None
        payload = json.loads(node.string)
        if not isinstance(payload, list) or len(payload) < 5 or not isinstance(payload[1], dict):
            return None
    except Exception:
        return None

    memo: Dict[int, object] = {}
    visiting: set[int] = set()

    def resolve_ref(value: object) -> object:
        if isinstance(value, int) and not isinstance(value, bool):
            if value < 0 or value >= len(payload) or value in visiting:
                raise ValueError("invalid Nuxt payload reference")
            if value in memo:
                return memo[value]
            visiting.add(value)
            resolved = resolve_value(payload[value])
            visiting.remove(value)
            memo[value] = resolved
            return resolved
        return value

    def resolve_value(value: object) -> object:
        if isinstance(value, dict):
            return {str(k): resolve_ref(v) for k, v in value.items()}
        if isinstance(value, list):
            if value and isinstance(value[0], str) and value[0] in {
                "ShallowReactive", "Reactive", "ShallowRef",
            }:
                if len(value) != 2:
                    raise ValueError("unsupported Nuxt wrapper")
                return resolve_ref(value[1])
            return [resolve_ref(v) for v in value]
        return value

    try:
        root = resolve_ref(1)
        data = root.get("data") if isinstance(root, dict) else None
        detail = data.get(f"etf-detail-base-{symbol}") if isinstance(data, dict) else None
        holdings_block = detail.get("holdings") if isinstance(detail, dict) else None
        records = holdings_block.get("holdings") if isinstance(holdings_block, dict) else None
        if not isinstance(records, list) or not records:
            return None
    except Exception:
        return None

    holdings: List[Holding] = []
    for record in records:
        if not isinstance(record, dict):
            return None
        # HoldingsSnapshot is the stock-exposure model.  ETF 資訊網也會在
        # 完整 payload 中揭露選擇權／期貨避險列，這些列可能有負權重；
        # raw source 仍完整保存，但不能混入股票共識或被當成負數股票持股。
        asset_type = str(record.get("assetType") or "").strip().lower()
        if asset_type in _DERIVATIVE_ASSET_TYPES:
            continue
        ticker = str(record.get("code") or "").strip()
        name = str(record.get("name") or "").strip()
        raw_weight = record.get("weight")
        if raw_weight is None:
            if _is_non_exposure_etf_record(record):
                continue
            return None
        try:
            weight = float(raw_weight)
            shares = float(record.get("shares") or 0)
        except (TypeError, ValueError):
            return None
        # 0% 仍是來源明確揭露的持股（例如暫時只保留最小單位），
        # 不應因為排序／顯示習慣而把它誤判成解析失敗。
        if not ticker or not name or weight < 0:
            return None
        holdings.append(Holding(
            ticker=ticker,
            name=name,
            weight_pct=weight,
            shares=shares,
        ))
    if not holdings:
        return None

    as_of: Optional[dt.date] = None
    snapshot_date = holdings_block.get("snapshotDate") if isinstance(holdings_block, dict) else None
    if isinstance(snapshot_date, str):
        try:
            as_of = dt.date.fromisoformat(snapshot_date[:10])
        except ValueError:
            as_of = None
    return as_of, holdings


def _looks_like_no_data(text: str) -> bool:
    """判斷頁面是否屬於「新掛牌尚無公開持股」而非真正抓取失敗。"""
    low = text.lower()
    return any(m.lower() in low for m in _NO_DATA_MARKERS)


def _looks_like_incomplete_moneydj_etf_html(html: str, url: str) -> bool:
    """辨識 MoneyDJ ETF 頁面的前十大摘要，避免誤存成完整持股。

    MoneyDJ 的 ``basic0007`` 頁面只公開前十大持股；即使 LLM 能讀出這些列，
    也不能把 top-N 摘要當成完整 portfolio。原始 HTML 已由 SourceRegistry 保存，
    待取得官方完整清單後再建立 canonical snapshot。
    """
    if not html or "moneydj.com/etf/" not in (url or "").lower():
        return False
    if "basic0007" not in (url or "").lower():
        return False
    if not _HAS_BS4:
        return False
    try:
        soup = BeautifulSoup(html, "lxml" if _has_lxml() else "html.parser")
    except Exception:
        return False
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        header = " ".join(rows[0].get_text(" ", strip=True).split())
        if not all(marker in header for marker in ("個股名稱", "投資比例", "持有股數")):
            continue
        data_rows = [
            row for row in rows[1:]
            if len(row.find_all(["td", "th"], recursive=False)) >= 3
        ]
        return 0 < len(data_rows) <= 10
    return False


def _materialize_research(
    result: FetchResult,
    snapshot: HoldingsSnapshot,
    *,
    root: Optional[Path],
    logger: logging.Logger,
) -> None:
    """把已成功保存的 canonical holdings 接到 research/evidence/claim 層。

    研究切片失敗不回滾已保存的 canonical CSV；兩者狀態分開記錄，避免
    「資料已保存」被錯誤報成「整條來源鏈都成功」。
    """
    if not result.source_document_id:
        result.research_status = "failed"
        result.research_error = "ETF raw archive 沒有 exact source_document_id"
        return
    try:
        from bot.etf_holdings_research import materialize_etf_holdings_slice

        sliced = materialize_etf_holdings_slice(
            root=Path(root or Path.cwd()),
            snapshot=snapshot,
            source_document_id=result.source_document_id,
        )
        result.research_status = sliced.status
        result.research_document_id = sliced.research_document_id
        result.research_evidence_id = sliced.evidence_id
        result.research_error = sliced.error
        if sliced.status not in {"ok", "reused"}:
            logger.warning(
                "[%s] ETF research slice %s: %s",
                result.etf.symbol, sliced.status, sliced.error,
            )
    except Exception as exc:  # noqa: BLE001
        result.research_status = "failed"
        result.research_error = f"{type(exc).__name__}: {exc}"
        logger.exception("[%s] ETF research slice 例外", result.etf.symbol)


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
        raw_html = ""
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
            raw_html = resp.text
            raw_text = _html_to_text(raw_html)
            raw_dir = (root or Path.cwd()) / "data" / "etf_holdings_raw" / etf.symbol
            mk_folder(str(raw_dir))
            tmp = raw_dir / f"{result.snapshot_date.isoformat()}.txt"
            tmp.write_text(raw_text, encoding="utf-8")
            mirror_file_to_cloud(tmp, root=root)
            result.raw_text_path = tmp

        # 保留下載到的原始 HTML/PDF；後續文字化與 LLM 抽取都只能算 derived。
        try:
            with SourceRegistry(root or Path.cwd()) as registry:
                document = registry.capture_document(
                    f"etf_holdings_{etf.symbol}",
                    bytes(getattr(resp, "content", b"") or b""),
                    source_url=etf.holdings_url,
                    source_ref=f"{etf.symbol}:{result.snapshot_date.isoformat()}",
                    title=f"{etf.symbol} holdings {result.snapshot_date.isoformat()}",
                    kind="http_response",
                    suffix="pdf" if is_pdf else "html",
                    mime_type=content_type or ("application/pdf" if is_pdf else "text/html"),
                    as_of=result.snapshot_date.isoformat(),
                    metadata={"etf_name": etf.name, "content_type": content_type},
                )
                result.source_document_id = document.document_id
        except Exception as exc:
            result.error = f"raw_registration_failed: {exc}"
            result.research_status = "failed"
            result.research_error = result.error
            log.exception("[%s] ETF raw provenance 寫入失敗", etf.symbol)
            return result

        if not raw_text or len(raw_text.strip()) < 50:
            result.error = "raw_text_too_short"
            result.research_status = "skipped"
            result.research_error = result.error
            log.warning("[%s] 原始內容過短 (%d 字)", etf.symbol, len(raw_text))
            return result

        # 新掛牌 ETF：發行商/第三方頁面常顯示「查無資料」。
        # 直接標記為 no_data_yet，省去無謂的 LLM 呼叫，待日後資料出現自動補齊。
        if _looks_like_no_data(raw_text):
            result.error = "no_data_yet"
            result.research_status = "skipped"
            result.research_error = result.error
            log.info(
                "[%s] 來源顯示查無持股 (新掛牌尚未公開)，標記 no_data_yet 待後續自動補齊",
                etf.symbol,
            )
            return result
    except Exception as e:
        result.error = f"fetch_failed: {e}"
        log.exception("[%s] 下載失敗", etf.symbol)
        return result

    # ---- 1b. 共同基金持股：MoneyDJ 確定性解析（免 LLM）----
    if raw_html and is_moneydj_fund_holdings_url(etf.holdings_url):
        parsed = parse_moneydj_fund_holdings_html(
            raw_html, root=root, logger=log,
        )
        if parsed.holdings:
            if parsed.as_of:
                result.snapshot_date = parsed.as_of
            snap = HoldingsSnapshot(
                symbol=etf.symbol,
                date=result.snapshot_date,
                holdings=parsed.holdings,
            )
            saved = save_holdings(snap, root)
            result.success = True
            result.holdings_count = len(parsed.holdings)
            result.saved_path = saved
            result.prompt_id = "fund_holdings_parser"
            result.prompt_version = "deterministic"
            _materialize_research(result, snap, root=root, logger=log)
            log.info(
                "[%s] 共同基金持股確定性解析 %d 檔 → %s (as_of=%s)",
                etf.symbol, len(parsed.holdings), saved.name, result.snapshot_date,
            )
            return result
        log.info("[%s] MoneyDJ 基金頁解析無持股，改走 LLM fallback", etf.symbol)

    # ---- 1c. ETF 資訊網公開持股表：確定性解析（免 LLM）----
    if raw_html and "etfinfo.tw" in etf.holdings_url.lower():
        parsed = _parse_etfinfo_holdings_html(raw_html, symbol=etf.symbol)
        if parsed is not None:
            as_of, holdings = parsed
            if as_of is not None:
                result.snapshot_date = as_of
            snap = HoldingsSnapshot(
                symbol=etf.symbol,
                date=result.snapshot_date,
                holdings=holdings,
            )
            saved = save_holdings(snap, root)
            result.success = True
            result.holdings_count = len(holdings)
            result.saved_path = saved
            result.prompt_id = "etfinfo_holdings_parser"
            result.prompt_version = "deterministic"
            _materialize_research(result, snap, root=root, logger=log)
            log.info(
                "[%s] ETF資訊網持股確定性解析 %d 檔 → %s (as_of=%s)",
                etf.symbol, len(holdings), saved.name, result.snapshot_date,
            )
            return result
        log.info("[%s] ETF資訊網頁面未符合持股表契約，改走 LLM fallback", etf.symbol)

    # ---- 1d. MoneyDJ ETF 頁面只有 top-10，禁止把摘要誤當完整快照 ----
    if _looks_like_incomplete_moneydj_etf_html(raw_html, etf.holdings_url):
        result.error = "source_incomplete_top_n"
        result.research_status = "skipped"
        result.research_error = (
            "MoneyDJ ETF 頁面只提供前十大持股；保留 raw，等待完整來源"
        )
        log.warning(
            "[%s] MoneyDJ ETF 只有前十大持股，拒絕 LLM 假補全；原始內容已存於 %s",
            etf.symbol, result.raw_text_path,
        )
        return result

    # ---- 2. 呼叫 Gemini 抽 JSON ----
    if not client.enabled:
        result.error = "llm_disabled"
        result.research_status = "skipped"
        result.research_error = "canonical holdings 尚未建立：llm_disabled"
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
        result.research_status = "skipped"
        result.research_error = "canonical holdings 尚未建立：llm_no_response"
        return result

    data = extract_json(raw_json)
    holdings: List[Holding] = []
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            ticker = str(item.get("ticker") or "").strip()
            name = str(item.get("name") or "").strip()
            try:
                weight = float(item.get("weight_pct") or 0)
            except (ValueError, TypeError):
                weight = 0.0
            # 台股 4-6 位數 / 美股代號等「乾淨代號」直接接受；
            # 海外個股可能無代號 → 只要有公司名與權重即保留 (ticker 退化為公司名)。
            clean_ticker = bool(ticker) and bool(re.match(r"^[A-Za-z0-9.\-]{1,12}$", ticker))
            if not clean_ticker:
                if not name or weight <= 0:
                    continue
                ticker = (ticker or name)[:40]
            try:
                holdings.append(Holding(
                    ticker=ticker,
                    name=name,
                    weight_pct=weight,
                    shares=float(item.get("shares") or 0),
                    value=float(item.get("value") or 0),
                ))
            except (ValueError, TypeError):
                continue

    if not holdings:
        result.error = "no_holdings_parsed"
        result.research_status = "skipped"
        result.research_error = "canonical holdings 尚未建立：no_holdings_parsed"
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
    _materialize_research(result, snap, root=root, logger=log)
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
    n_ok = sum(1 for r in results if r.success)
    n_pending = sum(1 for r in results if r.error == "no_data_yet")
    n_fail = sum(1 for r in results if not r.success and r.error != "no_data_yet")
    log.info(
        "ETF 持股自動抓取完成：%d 成功 / %d 新掛牌待補(no_data_yet) / %d 失敗",
        n_ok, n_pending, n_fail,
    )
    if n_pending:
        pend = ", ".join(r.etf.symbol for r in results if r.error == "no_data_yet")
        log.info("新掛牌尚無公開持股，待後續排程自動補齊：%s", pend)
    return results


__all__ = [
    "FetchResult",
    "fetch_all_active_etfs",
    "fetch_and_save_holdings",
]
