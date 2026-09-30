"""!ask 的來源 Adapter 契約與條件式即時來源。

Adapter 只回傳 Evidence packet，不直接把模型輸出寫進長期記憶。外部來源
由 AskEngine 以明確政策啟用，所有失敗都變成可追蹤的 ``ResourceResult``，
不會被誤解成「查無資料」。
"""

from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import asdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Protocol
from urllib.parse import urlparse

from bot.evidence import make_evidence
from bot.query_plan import QueryPlan, build_search_query
from bot.source_registry import is_source_document_replayable
from bot.utils import now_tw


def _replayable_source_document_id(root: Path, candidate: Any) -> str:
    """只回傳 manifest 與本地 raw archive 都可回放的來源 ID。"""
    source_id = str(candidate or "").strip()
    if not source_id.startswith("source-doc:"):
        return ""
    try:
        from bot.stock_db import StockDB

        db = StockDB.open(root=root)
        try:
            source = db.get_source_document(source_id)
            return source_id if is_source_document_replayable(root, source) else ""
        finally:
            db.close()
    except Exception:
        return ""


# 專業知識不接受任意搜尋摘要。這份 allowlist 是第一階段的安全邊界；
# 新來源應先加入並經測試，不以相似度或標題猜測權威性。
_PROFESSIONAL_AUTHORITY_DOMAINS = (
    "gov.tw",
    "fsc.gov.tw",
    "twse.com.tw",
    "tpex.org.tw",
    "taifex.com.tw",
    "sec.gov",
    "sec.gov.tw",
    "arxiv.org",
    "ssrn.com",
    "pubmed.ncbi.nlm.nih.gov",
    "nih.gov",
    "nature.com",
    "sciencedirect.com",
    "nber.org",
    "imf.org",
    "worldbank.org",
    "oecd.org",
    "bis.org",
)


def _authority_domain(url: str) -> str:
    host = (urlparse(str(url or "")).hostname or "").lower().strip(".")
    for domain in _PROFESSIONAL_AUTHORITY_DOMAINS:
        if host == domain or host.endswith("." + domain):
            return domain
    return ""


@dataclass(frozen=True)
class ResourceResult:
    resource: str
    evidence: List[dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    status: str = "ok"       # ok / partial / failed / skipped / timeout
    elapsed_ms: int = 0
    live: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


class ResourceAdapter(Protocol):
    resource_name: str

    def collect(self, plan: QueryPlan) -> ResourceResult:
        """以 QueryPlan 收集證據；不得直接產生未驗證答案。"""


class LocalEvidenceAdapter:
    """SQLite evidence registry adapter；不發出網路請求。"""

    resource_name = "historical_evidence"

    def __init__(self, db: Any) -> None:
        self.db = db

    def collect(self, plan: QueryPlan) -> ResourceResult:
        rows = []
        for term in [plan.normalized_query, *plan.keywords]:
            if not str(term).strip():
                continue
            rows.extend(self.db.search_evidence(str(term), limit=plan.max_sources))
            if len(rows) >= plan.max_sources:
                break
        seen: set[str] = set()
        evidence: List[dict[str, Any]] = []
        for row in rows:
            item = asdict(row)
            item["id"] = item.pop("evidence_id")
            try:
                item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            except (TypeError, ValueError):
                item["metadata"] = {}
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            evidence.append(item)
        return ResourceResult(
            self.resource_name,
            evidence=evidence[:plan.max_sources],
            status="ok" if evidence else "partial",
            warnings=[] if evidence else ["local evidence 沒有命中；不等於查無此事"],
        )


class LocalMemoryAdapter:
    """短／長記憶 adapter；記憶未帶 evidence 時只產生明確的 memory 證據。"""

    resource_name = "memory"

    def __init__(self, db: Any) -> None:
        self.db = db

    def collect(self, plan: QueryPlan) -> ResourceResult:
        from bot.evidence import make_evidence

        evidence: List[dict[str, Any]] = []
        for term in [plan.normalized_query, *plan.keywords]:
            if not str(term).strip():
                continue
            rows = self.db.search_memory_rows(str(term), memory_layers=("short", "long"), limit=plan.max_sources)
            for memory in rows:
                linked = self.db.list_memory_evidence(memory.memory_id, limit=plan.max_sources)
                if linked:
                    evidence.extend(asdict(row) | {"id": row.evidence_id} for row in linked)
                else:
                    evidence.append(make_evidence(
                        "memory", source=memory.source or "memory_store",
                        source_ref=memory.source_record_id or memory.memory_id,
                        title=memory.summary or memory.subject or memory.memory_id,
                        observed_at=memory.observed_at or memory.updated_at,
                        excerpt=memory.content or memory.summary,
                        quality_status=memory.quality_status,
                    ))
            if len(evidence) >= plan.max_sources:
                break
        unique: Dict[str, dict[str, Any]] = {}
        for item in evidence:
            identifier = str(item.get("id") or item.get("evidence_id") or "")
            if identifier:
                item["id"] = identifier
                unique.setdefault(identifier, item)
        return ResourceResult(
            self.resource_name,
            evidence=list(unique.values())[:plan.max_sources],
            status="ok" if unique else "partial",
        )


class CanonicalSnapshotAdapter:
    """現有 technical/fundamental/chips/ETF/macro cache-only adapter。"""

    resource_name = "canonical_snapshot"

    def __init__(self, *, root: Path) -> None:
        self.root = Path(root)

    def collect(self, plan: QueryPlan) -> ResourceResult:
        if not plan.ticker:
            return ResourceResult(self.resource_name, status="skipped", warnings=["canonical snapshot 需要 ticker"])
        try:
            from bot.ask_adapters import collect_snapshot_resource_evidence

            evidence = collect_snapshot_resource_evidence(
                plan.ticker, root=self.root, resource_needs=list(plan.resource_needs),
            )
            return ResourceResult(
                self.resource_name, evidence=evidence,
                status="ok" if evidence else "partial",
                warnings=[] if evidence else ["canonical snapshot cache 沒有資料"],
            )
        except Exception as exc:
            return ResourceResult(
                self.resource_name,
                status="failed",
                warnings=[f"canonical snapshot 失敗：{type(exc).__name__}: {exc}"],
            )


class LiveTickerAdapter:
    """以 TWSE MIS 公開延遲報價補齊即時 ticker evidence。

    這是明確 ``--live``／``STOCK_ASK_LIVE=1`` 才會執行的唯讀 adapter。
    MIS 可能延遲，證據仍保留 quote date/time，交由 Ask 的 freshness policy
    決定能否進入回答；沒有 exact raw manifest 時不猜 source_document_id。
    """

    resource_name = "ticker_snapshot"

    def __init__(self, *, root: Path, timeout: float = 8.0) -> None:
        self.root = Path(root)
        try:
            self.timeout = max(0.25, float(timeout))
        except (TypeError, ValueError):
            self.timeout = 8.0

    def collect(self, plan: QueryPlan) -> ResourceResult:
        started = time.monotonic()
        if not plan.ticker:
            return ResourceResult(
                self.resource_name,
                warnings=["TWSE MIS 即時報價需要 ticker"],
                status="skipped",
                live=True,
            )
        try:
            from bot.market_source import TwsePublicMarketSource

            source = TwsePublicMarketSource(
                [plan.ticker],
                poll_seconds=0,
                root=self.root,
                request_timeout=self.timeout,
            )
            quotes = source.get_quotes()
            quote = quotes.get(plan.ticker)
            if not isinstance(quote, dict) or quote.get("price") is None:
                return ResourceResult(
                    self.resource_name,
                    warnings=[
                        "TWSE MIS 沒有可用成交／委買委賣價格；不等於查無此事",
                    ],
                    status="partial",
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    live=True,
                )

            fetched_at = str(quote.get("fetched_at") or now_tw().isoformat(timespec="seconds"))
            quote_date = str(quote.get("quote_date") or "")
            quote_time = str(quote.get("quote_time") or "")
            observed_at = (
                f"{quote_date}T{quote_time}+08:00"
                if quote_date and quote_time
                else fetched_at
            )
            source_document_id = _replayable_source_document_id(
                self.root, quote.get("source_document_id")
            )
            price = float(quote["price"])
            prev_close = float(quote.get("prev_close") or 0.0)
            pct = quote.get("pct_chg")
            pct_text = "未知" if pct is None else f"{float(pct):+.2f}%"
            volume = int(quote.get("volume") or 0)
            evidence = make_evidence(
                self.resource_name,
                source="twse_mis",
                source_ref=(
                    source_document_id
                    or f"twse-mis:{plan.ticker}:{quote_date}:{quote_time}"
                ),
                title=f"{plan.ticker} TWSE MIS 延遲報價",
                url=TwsePublicMarketSource.MIS_API_URL,
                observed_at=observed_at,
                as_of=quote_date,
                retrieved_at=fetched_at,
                excerpt=(
                    f"{plan.ticker} {quote.get('name') or ''} 現價 {price:g}，"
                    f"前收 {prev_close:g}，漲跌 {pct_text}，成交量 {volume:,}；"
                    f"報價時間 {quote_date} {quote_time}，價格依據 {quote.get('price_basis') or 'unknown'}"
                ).strip(),
                quality_status="ok" if prev_close > 0 else "partial",
                source_document_id=(
                    source_document_id
                    if source_document_id.startswith("source-doc:") else ""
                ),
                provenance_status=(
                    "exact" if source_document_id.startswith("source-doc:")
                    else "unresolved"
                ),
                metadata={
                    "delayed_quote": True,
                    "exchange": quote.get("exchange") or "",
                    "price_basis": quote.get("price_basis") or "",
                    "source_document_id": source_document_id,
                },
            )
            return ResourceResult(
                self.resource_name,
                evidence=[evidence],
                warnings=["TWSE MIS 為公開延遲報價，仍以 quote date/time 判斷新鮮度"],
                status="ok",
                elapsed_ms=int((time.monotonic() - started) * 1000),
                live=True,
            )
        except Exception as exc:
            return ResourceResult(
                self.resource_name,
                warnings=[f"TWSE MIS ticker 來源失敗：{type(exc).__name__}: {exc}"],
                status="failed",
                elapsed_ms=int((time.monotonic() - started) * 1000),
                live=True,
            )


class WebSearchAdapter:
    """只有 plan 明確要求 news/web_search 時才呼叫的網路 Adapter。"""

    def __init__(self, *, root: Path, resource_name: str, timeout: int = 8) -> None:
        if resource_name not in {"news", "web_search"}:
            raise ValueError("WebSearchAdapter 只支援 news 或 web_search")
        self.root = Path(root)
        self.resource_name = resource_name
        self.timeout = max(1, int(timeout))

    def collect(self, plan: QueryPlan) -> ResourceResult:
        started = time.monotonic()
        retrieved_at = now_tw().isoformat(timespec="seconds")
        query = build_search_query(plan, resource=self.resource_name)
        try:
            from bot.web_search import search_news, search_web

            rows = (
                search_news(query, max_results=plan.max_sources, timeout=self.timeout, root=self.root)
                if self.resource_name == "news"
                else search_web(query, max_results=plan.max_sources, timeout=self.timeout, root=self.root)
            )
            evidence = []
            for row in rows:
                source_document_id = _replayable_source_document_id(
                    self.root, getattr(row, "source_document_id", "")
                )
                evidence.append(make_evidence(
                    self.resource_name,
                    source=getattr(row, "source", "web_search") or "web_search",
                    source_ref=getattr(row, "url", "") or getattr(row, "title", ""),
                    title=getattr(row, "title", ""),
                    url=getattr(row, "url", ""),
                    published_at=getattr(row, "published_at", "") or "",
                    observed_at=getattr(row, "published_at", "") or retrieved_at,
                    retrieved_at=retrieved_at,
                    excerpt=getattr(row, "snippet", "") or "",
                    quality_status="partial" if getattr(row, "snippet", "") else "unknown",
                    source_document_id=source_document_id,
                ))
            status = "ok" if evidence else "partial"
            warning = [] if evidence else [f"{self.resource_name} 沒有回傳搜尋結果；不等於查無此事"]
            return ResourceResult(
                resource=self.resource_name,
                evidence=evidence,
                warnings=warning,
                status=status,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                live=True,
                metadata={"search_query": query},
            )
        except Exception as exc:
            return ResourceResult(
                resource=self.resource_name,
                warnings=[f"{self.resource_name} 來源失敗：{type(exc).__name__}: {exc}"],
                status="failed",
                elapsed_ms=int((time.monotonic() - started) * 1000),
                live=True,
                metadata={"search_query": query},
            )


class ProfessionalKnowledgeAdapter:
    """只從核准權威網域建立專業知識 evidence。

    一般搜尋結果即使有摘要也不會進入這個 resource；只有 allowlist 網域的
    結果會嘗試抓取原文。抓不到原文時仍可保留 ``partial`` 搜尋摘要，但會在
    metadata／warning 明確標示，讓回答驗證與人工審核知道它不是完整專家文件。
    """

    resource_name = "professional_knowledge"

    def __init__(self, *, root: Path, timeout: float = 8.0) -> None:
        self.root = Path(root)
        try:
            self.timeout = max(1.0, float(timeout))
        except (TypeError, ValueError):
            self.timeout = 8.0

    def collect(self, plan: QueryPlan) -> ResourceResult:
        started = time.monotonic()
        query = build_search_query(plan, resource=self.resource_name)
        try:
            from bot.web_search import fetch_page_text, search_web

            rows = search_web(
                query,
                max_results=min(plan.max_sources * 2, 20),
                timeout=max(1, int(min(8.0, self.timeout))),
                root=self.root,
            )
            candidates = [
                row for row in rows
                if _authority_domain(getattr(row, "url", ""))
            ]
            if not candidates:
                return ResourceResult(
                    self.resource_name,
                    warnings=[
                        "未找到核准官方／監管／學術網域；一般搜尋結果不列為專業證據",
                    ],
                    status="partial",
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    live=True,
                    metadata={"search_query": query},
                )

            evidence: List[dict[str, Any]] = []
            warnings: List[str] = []
            for row in candidates[:plan.max_sources]:
                remaining = self.timeout - (time.monotonic() - started)
                if remaining < 1.0:
                    warnings.append("專業來源已達時間預算；不等於查無此事")
                    break
                url = str(getattr(row, "url", "") or "")
                domain = _authority_domain(url)
                page = fetch_page_text(
                    url,
                    max_chars=3000,
                    timeout=max(1, int(min(8.0, remaining))),
                    root=self.root,
                )
                excerpt = str(getattr(page, "text", "") or "").strip()
                mode = "page_text"
                if not excerpt:
                    excerpt = str(getattr(row, "snippet", "") or "").strip()
                    mode = "search_snippet"
                    if excerpt:
                        warnings.append(f"{domain} 原文未取得，僅保留搜尋摘要")
                if not excerpt:
                    continue
                source_document_id = _replayable_source_document_id(
                    self.root,
                    getattr(page, "source_document_id", "")
                    or getattr(row, "source_document_id", "")
                    or ""
                )
                evidence.append(make_evidence(
                    self.resource_name,
                    source=f"professional:{domain}",
                    source_ref=source_document_id or url,
                    title=str(getattr(page, "title", "") or getattr(row, "title", "") or url),
                    url=url,
                    observed_at=str(
                        getattr(page, "fetched_at", "")
                        or now_tw().isoformat(timespec="seconds")
                    ),
                    retrieved_at=now_tw().isoformat(timespec="seconds"),
                    excerpt=excerpt[:3000],
                    quality_status="ok" if mode == "page_text" else "partial",
                    source_document_id=(
                        source_document_id
                        if source_document_id.startswith("source-doc:") else ""
                    ),
                    provenance_status=(
                        "exact" if source_document_id.startswith("source-doc:")
                        else "unresolved"
                    ),
                    metadata={
                        "authority_domain": domain,
                        "authority_status": "allowlisted",
                        "evidence_mode": mode,
                        "requested_resource": self.resource_name,
                    },
                ))
            return ResourceResult(
                self.resource_name,
                evidence=evidence[:plan.max_sources],
                warnings=warnings,
                status="ok" if evidence else "partial",
                elapsed_ms=int((time.monotonic() - started) * 1000),
                live=True,
                metadata={"search_query": query},
            )
        except Exception as exc:
            return ResourceResult(
                self.resource_name,
                warnings=[f"專業知識來源失敗：{type(exc).__name__}: {exc}"],
                status="failed",
                elapsed_ms=int((time.monotonic() - started) * 1000),
                live=True,
                metadata={"search_query": query},
            )


class UnavailableAdapter:
    """明確表示尚未接入的來源，避免把空結果當成成功。"""

    def __init__(self, resource_name: str, reason: str = "adapter 尚未接入") -> None:
        self.resource_name = resource_name
        self.reason = reason

    def collect(self, plan: QueryPlan) -> ResourceResult:
        return ResourceResult(
            resource=self.resource_name,
            warnings=[f"{self.resource_name}: {self.reason}"],
            status="skipped",
        )


class MopsAdapter:
    """MOPS/TWSE 重大訊息來源；raw response 由 mops_scraper 保存。"""

    resource_name = "mops"

    def __init__(
        self,
        *,
        root: Path,
        max_details: int = 3,
        timeout: float = 8.0,
    ) -> None:
        self.root = Path(root)
        self.max_details = max(0, int(max_details))
        try:
            self.timeout = max(0.25, float(timeout))
        except (TypeError, ValueError):
            self.timeout = 8.0

    def collect(self, plan: QueryPlan) -> ResourceResult:
        started = time.monotonic()
        if not plan.ticker:
            return ResourceResult("mops", warnings=["MOPS 需要 ticker"], status="skipped")
        try:
            from bot.mops_scraper import fetch_material_info, ingest_material_detail
            from bot.stock_db import StockDB

            deadline = started + self.timeout
            warnings: List[str] = []
            rows = fetch_material_info(
                plan.ticker,
                root=self.root,
                timeout=min(5.0, self.timeout),
                deadline=deadline,
            )
            if time.monotonic() >= deadline:
                warnings.append(
                    f"MOPS 列表已達 {self.timeout:g}s 時間預算；不等於查無此事"
                )
            retrieved_at = now_tw().isoformat(timespec="seconds")
            evidence: List[dict[str, Any]] = []
            seen_evidence_ids: set[str] = set()
            seen_source_docs: set[str] = set()
            db = StockDB.open(root=self.root)
            try:
                for row in rows[:plan.max_sources]:
                    if time.monotonic() >= deadline:
                        warnings.append(
                            f"MOPS 詳情已達 {self.timeout:g}s 時間預算；不等於查無此事"
                        )
                        break
                    excerpt = str(getattr(row, "subject", "") or "").strip()
                    detail_url = str(getattr(row, "detail_url", "") or "")
                    source_document_id = ""
                    research_document_id = ""
                    provenance_status = "unresolved"
                    research_evidence: dict[str, Any] | None = None
                    if detail_url and len(evidence) < self.max_details:
                        detail = ingest_material_detail(
                            detail_url,
                            root=self.root,
                            ticker=plan.ticker,
                            max_chars=2000,
                            materialize_research=True,
                            timeout=max(
                                0.25,
                                min(5.0, deadline - time.monotonic()),
                            ),
                        )
                        if detail.text:
                            excerpt = (
                                f"{excerpt}；{detail.text}" if excerpt else detail.text
                            )
                        source_document_id = detail.source_document_id
                        research_document_id = detail.research_document_id
                        if detail.research_status == "failed":
                            warnings.append(
                                f"mops research slice 失敗: "
                                f"{detail.research_error or detail_url}"
                            )
                        source_document = db.get_source_document(source_document_id)
                        if source_document_id and is_source_document_replayable(
                            self.root, source_document
                        ):
                            provenance_status = "exact"
                        elif source_document_id:
                            warnings.append(
                                f"MOPS source document 無法回放 raw archive："
                                f"{source_document_id}"
                            )
                            source_document_id = ""
                        if (
                            detail.research_status in {"ok", "reused"}
                            and detail.evidence_id
                        ):
                            # 優先使用 research slice 已登錄的 exact evidence，
                            # 避免 adapter 再造一筆重複 news evidence。
                            # 僅在目前來源仍可回放、exact／composite 仍成立時重用；
                            # 不可在清除非可回放 ID 後，把 DB 舊列預設成 exact。
                            if detail.evidence_id in seen_evidence_ids:
                                continue
                            if (
                                source_document_id
                                and source_document_id in seen_source_docs
                            ):
                                continue
                            if (
                                provenance_status in {"exact", "composite_exact"}
                                and source_document_id
                            ):
                                existing = db.get_evidence(detail.evidence_id)
                                if existing is not None:
                                    try:
                                        meta = json.loads(
                                            existing.metadata_json or "{}"
                                        )
                                    except (TypeError, ValueError):
                                        meta = {}
                                    if not isinstance(meta, dict):
                                        meta = {}
                                    packet_provenance = str(
                                        meta.get("provenance_status")
                                        or provenance_status
                                    ).strip().lower()
                                    if packet_provenance not in {
                                        "exact",
                                        "composite_exact",
                                    }:
                                        packet_provenance = provenance_status
                                    reuse_source_id = str(
                                        existing.source_document_id or ""
                                    ).strip()
                                    if reuse_source_id != source_document_id:
                                        reuse_source_id = source_document_id
                                    research_evidence = {
                                        "id": existing.evidence_id,
                                        "kind": existing.kind,
                                        "source": existing.source,
                                        "source_ref": existing.source_ref,
                                        "title": existing.title,
                                        "url": existing.url,
                                        "observed_at": existing.observed_at,
                                        "as_of": existing.as_of,
                                        "published_at": existing.published_at,
                                        "retrieved_at": existing.retrieved_at,
                                        "excerpt": existing.excerpt,
                                        "quality_status": existing.quality_status,
                                        "source_document_id": reuse_source_id,
                                        "provenance_status": packet_provenance,
                                        "metadata": meta,
                                    }
                    if research_evidence is not None:
                        evidence.append(research_evidence)
                        seen_evidence_ids.add(str(research_evidence["id"]))
                        if source_document_id:
                            seen_source_docs.add(source_document_id)
                        continue

                    packet = make_evidence(
                        "news",
                        source="mops",
                        source_ref=detail_url or (
                            f"mops:{plan.ticker}:{getattr(row, 'date', '')}"
                        ),
                        title=f"MOPS {plan.ticker} {excerpt[:120]}",
                        url=detail_url,
                        observed_at=str(getattr(row, "date", "") or retrieved_at),
                        published_at=str(getattr(row, "date", "") or ""),
                        retrieved_at=retrieved_at,
                        excerpt=excerpt,
                        quality_status="ok" if excerpt else "partial",
                        source_document_id=source_document_id,
                        provenance_status=provenance_status,
                        metadata={
                            "pipeline": (
                                "mops_official_vertical_slice"
                                if source_document_id else ""
                            ),
                            "research_document_id": research_document_id,
                        },
                    )
                    if packet["id"] in seen_evidence_ids:
                        continue
                    if source_document_id and source_document_id in seen_source_docs:
                        continue
                    evidence.append(packet)
                    seen_evidence_ids.add(packet["id"])
                    if source_document_id:
                        seen_source_docs.add(source_document_id)
            finally:
                db.close()
            if not evidence and not warnings:
                warnings.append("MOPS 沒有回傳資料；不等於查無此事")
            return ResourceResult(
                "mops",
                evidence=evidence,
                warnings=warnings,
                status="ok" if evidence else "partial",
                elapsed_ms=int((time.monotonic() - started) * 1000),
                live=True,
            )
        except Exception as exc:
            return ResourceResult(
                "mops", warnings=[f"MOPS 來源失敗：{type(exc).__name__}: {exc}"],
                status="failed", elapsed_ms=int((time.monotonic() - started) * 1000), live=True,
            )


class MonthlyRevenueAdapter:
    """本地 canonical 月營收垂直切片；只在 QueryPlan 要求 fundamental 時使用。

    優先重用 research slice 已登錄的 exact evidence，不重複建立同一來源證據。
    不下單、不打外網；缺少可回放 exact source_document_id 時標 unresolved，
    不臆造來源，也不把 legacy／cache 列升格為 exact。
    """

    resource_name = "fundamental"

    def __init__(self, *, root: Path) -> None:
        self.root = Path(root)

    def collect(self, plan: QueryPlan) -> ResourceResult:
        started = time.monotonic()
        if "fundamental" not in (plan.resource_needs or []):
            return ResourceResult(
                self.resource_name,
                status="skipped",
                warnings=["QueryPlan 未要求 fundamental"],
            )
        if not plan.ticker:
            return ResourceResult(
                self.resource_name,
                status="skipped",
                warnings=["月營收需要 ticker"],
            )
        try:
            from bot.monthly_revenue_research import PIPELINE, SOURCE_NAME
            from bot.stock_db import StockDB

            db = StockDB.open(root=self.root)
            try:
                revenues = [
                    row for row in db.get_monthly_revenue(plan.ticker)
                    if row.quality_status in {"ok", "partial"}
                ]
                if not revenues:
                    return ResourceResult(
                        self.resource_name,
                        status="partial",
                        warnings=["本地 monthly_revenue 無可用資料；不等於查無此事"],
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                    )
                revenue = revenues[0]  # year_month DESC
                source_document_id = str(revenue.source_record_id or "").strip()
                warnings: List[str] = []
                # 月營收契約：缺 exact／不可回放 → unresolved（非 exact）；
                # metadata.cache_only 僅標示無 raw 對照，不升格 provenance。
                provenance = "unresolved"
                existing = None
                if source_document_id:
                    source_document = db.get_source_document(source_document_id)
                    source_quality = str(
                        getattr(source_document, "quality_status", "") or "ok"
                    )
                    quality_ok = source_quality in {"ok", "partial"}
                    replayable = (
                        source_document is not None
                        and quality_ok
                        and is_source_document_replayable(
                            self.root, source_document
                        )
                    )
                    if not replayable:
                        if source_document is None:
                            warnings.append(
                                "monthly_revenue source_record_id 不存在於 manifest；"
                                "標示 unresolved，不臆造來源"
                            )
                        elif not quality_ok:
                            warnings.append(
                                "monthly_revenue source_document quality_status="
                                f"{source_quality} 不可用；降為 unresolved"
                            )
                        else:
                            warnings.append(
                                "monthly_revenue source_record_id 無法回放 raw archive："
                                f"{source_document_id}"
                            )
                        provenance = "unresolved"
                        source_document_id = ""
                    else:
                        provenance = "exact"
                        # 重用既有 evidence（同 source_document_id + pipeline）
                        for row in db.list_evidence(source=SOURCE_NAME, limit=50):
                            if row.source_document_id != source_document_id:
                                continue
                            try:
                                meta = json.loads(row.metadata_json or "{}")
                            except (TypeError, ValueError):
                                meta = {}
                            if not isinstance(meta, dict):
                                meta = {}
                            if meta.get("pipeline") != PIPELINE:
                                continue
                            if meta.get("year_month") and meta.get("year_month") != revenue.year_month:
                                continue
                            existing = row
                            break
                else:
                    warnings.append(
                        "monthly_revenue 缺少 source_document_id；"
                        "標示 unresolved，不臆造來源"
                    )

                if existing is not None:
                    try:
                        meta = json.loads(existing.metadata_json or "{}")
                    except (TypeError, ValueError):
                        meta = {}
                    if not isinstance(meta, dict):
                        meta = {}
                    # 既有 exact evidence 仍須對應目前可回放來源；不可殘留升格。
                    packet_provenance = str(
                        meta.get("provenance_status") or provenance
                    )
                    if provenance != "exact":
                        packet_provenance = "unresolved"
                    elif packet_provenance not in {"exact", "composite_exact"}:
                        packet_provenance = "exact"
                    packet = {
                        "id": existing.evidence_id,
                        "kind": existing.kind,
                        "source": existing.source,
                        "source_ref": existing.source_ref,
                        "title": existing.title,
                        "url": existing.url,
                        "observed_at": existing.observed_at,
                        "as_of": existing.as_of,
                        "published_at": existing.published_at,
                        "retrieved_at": existing.retrieved_at,
                        "excerpt": existing.excerpt,
                        "quality_status": existing.quality_status,
                        "source_document_id": existing.source_document_id,
                        "provenance_status": packet_provenance,
                        "metadata": meta,
                    }
                    return ResourceResult(
                        self.resource_name,
                        evidence=[packet],
                        warnings=warnings,
                        status="ok",
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                        live=False,
                    )

                excerpt = (
                    f"{revenue.symbol} {revenue.year_month} 月營收 "
                    f"{revenue.revenue:g}（新台幣千元），"
                    f"YoY {revenue.yoy_pct:+g}%、MoM {revenue.mom_pct:+g}%"
                )
                packet = make_evidence(
                    "monthly_revenue",
                    source=SOURCE_NAME if source_document_id else "monthly_revenue",
                    source_ref=(
                        source_document_id
                        or f"{revenue.symbol}:{revenue.year_month}"
                    ),
                    title=f"{revenue.symbol} 月營收 {revenue.year_month}",
                    url=revenue.source_url,
                    observed_at=revenue.source_date or revenue.year_month,
                    as_of=revenue.year_month,
                    published_at=revenue.source_date or "",
                    retrieved_at=revenue.retrieved_at or now_tw().isoformat(timespec="seconds"),
                    excerpt=excerpt,
                    quality_status=revenue.quality_status,
                    source_document_id=source_document_id,
                    provenance_status=provenance,
                    metadata={
                        "pipeline": PIPELINE if source_document_id else "",
                        "symbol": revenue.symbol,
                        "year_month": revenue.year_month,
                        "revenue_unit": "TWD_thousand",
                        # cache_only 僅表示無 exact raw；provenance 維持 unresolved。
                        "cache_only": not bool(source_document_id),
                    },
                )
                return ResourceResult(
                    self.resource_name,
                    evidence=[packet],
                    warnings=warnings,
                    status="ok",
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    live=False,
                )
            finally:
                db.close()
        except Exception as exc:
            return ResourceResult(
                self.resource_name,
                warnings=[f"月營收 adapter 失敗：{type(exc).__name__}: {exc}"],
                status="failed",
                elapsed_ms=int((time.monotonic() - started) * 1000),
                live=False,
            )


__all__ = [
    "CanonicalSnapshotAdapter", "LocalEvidenceAdapter", "LocalMemoryAdapter",
    "LiveTickerAdapter",
    "MonthlyRevenueAdapter", "MopsAdapter", "ProfessionalKnowledgeAdapter",
    "ResourceAdapter", "ResourceResult",
    "UnavailableAdapter", "WebSearchAdapter",
]
