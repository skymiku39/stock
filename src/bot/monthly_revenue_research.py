"""TWSE 月營收（單一基本面來源）的 research／evidence／claim 垂直切片。

資料流（方案 C）：

``raw archive → source_documents → monthly_revenue (canonical SQLite)
→ research_documents/chunks → evidence_items → llm_runs → claims``

此模組只處理月營收事實欄位（營收、YoY、MoM、資料年月、單位）。
不做看多／看空推論、不下單、不處理新聞／macro／籌碼／ETF／季報。
``source_document_id`` 必須是呼叫端已知的 exact ID，不得依 URL／標題猜測。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from bot.evidence import make_evidence, persist_evidence_items
from bot.fundamentals_fetcher import MonthlyRevenue as ParsedMonthlyRevenue
from bot.fundamentals_fetcher import _parse_one_revenue
from bot.source_registry import (
    SourceRegistry,
    is_source_document_replayable,
    resolve_source_document_path,
)
from bot.stock_db import (
    ClaimEvidenceRow,
    ClaimRow,
    LlmRunEvidenceRow,
    LlmRunRow,
    MonthlyRevenue,
    StockDB,
)
from bot.utils import now_tw

_WHITESPACE_RE = re.compile(r"\s+")

INGEST_RUN_STATUS = "source_ingested"
INGEST_TASK_TYPE = "monthly_revenue_ingest"
PIPELINE = "monthly_revenue_vertical_slice"
REVENUE_UNIT = "TWD_thousand"
REVENUE_UNIT_LABEL = "新台幣千元"
SOURCE_NAME = "monthly_revenue_twse"
SLICE_STATUSES = {"ok", "reused", "failed", "empty", "stale_skipped"}

FIELD_DEFS: Dict[str, str] = {
    "symbol": "股票代號",
    "year_month": "資料年月（ISO YYYY-MM；as_of＝所屬期間）",
    "revenue": f"當月營收（單位：{REVENUE_UNIT_LABEL}）",
    "yoy_pct": "營收年增率 %",
    "mom_pct": "營收月增率 %",
    "cumulative": f"當月累計營收（單位：{REVENUE_UNIT_LABEL}）",
    "cum_yoy_pct": "累計營收年增率 %",
    "retrieved_at": "本機抓取時間",
    "published_at": "來源發布／資料日（若 raw 有提供）",
}


@dataclass
class MonthlyRevenueSlice:
    """一次月營收垂直切片的可追蹤結果。"""

    source_document_id: str
    symbol: str = ""
    year_month: str = ""
    research_document_id: str = ""
    evidence_id: str = ""
    claim_ids: List[str] = field(default_factory=list)
    run_id: str = ""
    canonical_json: str = ""
    chunk_ids: List[str] = field(default_factory=list)
    provenance_status: str = "exact"
    # ok／reused／failed／empty／stale_skipped
    status: str = "ok"
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def normalize_claim_whitespace(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", str(value or "")).strip()


def claim_snippet_supported(snippet: str, canonical_text: str) -> bool:
    needle = normalize_claim_whitespace(snippet)
    haystack = normalize_claim_whitespace(canonical_text)
    return bool(needle) and needle in haystack


def _format_number(value: float) -> str:
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return text or "0"


def year_month_from_parts(year: int, month: int) -> str:
    return f"{int(year):04d}-{int(month):02d}"


def build_canonical_payload(
    *,
    symbol: str,
    year_month: str,
    revenue: float,
    yoy_pct: float,
    mom_pct: float,
    cumulative: float = 0.0,
    cum_yoy_pct: float = 0.0,
    source_document_id: str,
    retrieved_at: str = "",
    published_at: str = "",
    source_url: str = "",
    quality_status: str = "ok",
) -> Dict[str, Any]:
    return {
        "symbol": str(symbol),
        "year_month": str(year_month),
        "as_of": str(year_month),
        "revenue": float(revenue),
        "revenue_unit": REVENUE_UNIT,
        "revenue_unit_label": REVENUE_UNIT_LABEL,
        "yoy_pct": float(yoy_pct),
        "mom_pct": float(mom_pct),
        "cumulative": float(cumulative),
        "cum_yoy_pct": float(cum_yoy_pct),
        "source_document_id": str(source_document_id),
        "retrieved_at": str(retrieved_at or ""),
        "published_at": str(published_at or ""),
        "source_url": str(source_url or ""),
        "quality_status": str(quality_status or "ok"),
        "field_defs": dict(FIELD_DEFS),
        "pipeline": PIPELINE,
    }


def canonical_text_from_payload(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)


def _extractive_claims(payload: Mapping[str, Any]) -> List[Dict[str, str]]:
    """只產生可由原始欄位直接核對的事實 claim。"""
    symbol = str(payload.get("symbol") or "")
    year_month = str(payload.get("year_month") or "")
    revenue = _format_number(float(payload.get("revenue") or 0))
    yoy = _format_number(float(payload.get("yoy_pct") or 0))
    mom = _format_number(float(payload.get("mom_pct") or 0))
    unit = str(payload.get("revenue_unit_label") or REVENUE_UNIT_LABEL)

    def _json_fragment(key: str, value: Any) -> str:
        return f"{json.dumps(key, ensure_ascii=False)}: {json.dumps(value, ensure_ascii=False)}"

    return [
        {
            "claim_type": "source_fact",
            "text": (
                f"{symbol} {year_month} 月營收 {revenue}"
                f"（單位：{unit}）"
            ),
            "snippet": _json_fragment("revenue", float(payload.get("revenue") or 0)),
        },
        {
            "claim_type": "source_fact",
            "text": f"{symbol} {year_month} 營收年增率 YoY {yoy}%",
            "snippet": _json_fragment("yoy_pct", float(payload.get("yoy_pct") or 0)),
        },
        {
            "claim_type": "source_fact",
            "text": f"{symbol} {year_month} 營收月增率 MoM {mom}%",
            "snippet": _json_fragment("mom_pct", float(payload.get("mom_pct") or 0)),
        },
        {
            "claim_type": "source_fact",
            "text": f"{symbol} 月營收資料年月 as_of={year_month}",
            "snippet": _json_fragment("year_month", year_month),
        },
        {
            "claim_type": "source_fact",
            "text": f"月營收單位為{unit}",
            "snippet": _json_fragment("revenue_unit_label", unit),
        },
    ]


def _parse_iso_ts(value: str) -> str:
    """回傳可比較的時間字串；空值視為最舊。"""
    return str(value or "").strip()


def should_skip_stale_overwrite(
    existing: Optional[MonthlyRevenue],
    *,
    incoming_retrieved_at: str,
) -> bool:
    """同一 (symbol, year_month) 若既有 retrieved_at 較新，拒絕用較舊資料覆寫。"""
    if existing is None:
        return False
    old_ts = _parse_iso_ts(existing.retrieved_at)
    new_ts = _parse_iso_ts(incoming_retrieved_at)
    if not old_ts or not new_ts:
        return False
    return old_ts > new_ts


def _source_quality_usable(quality_status: str) -> bool:
    return (quality_status or "ok") in {"ok", "partial"}


def source_document_usable_for_slice(
    *,
    root: Path,
    source: Any,
    source_document_id: str,
) -> tuple[bool, str]:
    """切片前確認 exact 來源仍可安全使用（品質＋raw 可回放）。

    不依 URL／標題另找文件；hash 不符或品質不可用時拒絕升格為 exact 切片。
    """
    if source is None:
        return False, f"unknown source_document_id: {source_document_id}"
    if not _source_quality_usable(str(getattr(source, "quality_status", "") or "ok")):
        return False, (
            f"source_document quality_status={source.quality_status} 不可切片"
        )
    if not is_source_document_replayable(root, source):
        path = resolve_source_document_path(root, source)
        if not path.is_file():
            return False, f"raw archive missing: {source.content_path}"
        if source.content_hash:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != source.content_hash:
                return False, (
                    f"raw hash mismatch: expected {source.content_hash[:12]}…"
                    f" got {digest[:12]}…"
                )
        return False, f"raw archive 無法回放: {source_document_id}"
    return True, ""


def load_raw_rows_from_source_document(
    *,
    root: Path,
    source_document_id: str,
    db: StockDB,
) -> tuple[Optional[Any], List[Dict[str, Any]], str]:
    """由 exact source_document_id 讀取 raw archive 並解析成 rows。

    回傳 ``(source_row, rows, error)``。不得依 URL／標題另找文件。
    """
    source = db.get_source_document(source_document_id)
    usable, error = source_document_usable_for_slice(
        root=root,
        source=source,
        source_document_id=source_document_id,
    )
    if not usable:
        return source, [], error
    path = resolve_source_document_path(root, source)
    raw_bytes = path.read_bytes()
    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return source, [], f"raw JSON 解析失敗: {exc}"
    rows: List[Dict[str, Any]] = []
    if isinstance(payload, list):
        rows = [r for r in payload if isinstance(r, dict)]
    elif isinstance(payload, dict):
        nested = payload.get("rows")
        if isinstance(nested, list):
            rows = [r for r in nested if isinstance(r, dict)]
        else:
            rows = [payload]
    else:
        return source, [], "raw payload 不是 JSON array/object"
    return source, rows, ""


def find_ticker_revenue(
    rows: List[Dict[str, Any]], ticker: str,
) -> Optional[ParsedMonthlyRevenue]:
    target = str(ticker or "").strip()
    if not target:
        return None
    for raw in rows:
        try:
            parsed = _parse_one_revenue(raw)
        except Exception:
            continue
        if parsed and parsed.ticker == target and parsed.year > 0 and parsed.month > 0:
            return parsed
    return None


def materialize_monthly_revenue_slice(
    *,
    root: Path,
    source_document_id: str,
    symbol: str,
    year_month: str,
    revenue: float,
    yoy_pct: float,
    mom_pct: float,
    cumulative: float = 0.0,
    cum_yoy_pct: float = 0.0,
    source_url: str = "",
    published_at: str = "",
    retrieved_at: str = "",
    quality_status: str = "ok",
    db: Optional[StockDB] = None,
    allow_stale_overwrite: bool = False,
) -> MonthlyRevenueSlice:
    """由已知 raw ``source_document_id`` 寫入 canonical 並建立 research／evidence／claim。"""
    project_root = Path(root)
    owns_db = db is None
    database = db or StockDB.open(root=project_root)
    try:
        source = database.get_source_document(source_document_id)
        usable, usability_error = source_document_usable_for_slice(
            root=project_root,
            source=source,
            source_document_id=source_document_id,
        )
        if not usable:
            return MonthlyRevenueSlice(
                source_document_id=source_document_id,
                symbol=symbol,
                year_month=year_month,
                status="failed",
                provenance_status="unresolved",
                error=usability_error,
            )
        sym = str(symbol or "").strip()
        ym = str(year_month or "").strip()
        if not sym or not ym:
            return MonthlyRevenueSlice(
                source_document_id=source_document_id,
                status="failed",
                error="symbol／year_month 不可為空",
            )

        retrieved = (
            str(retrieved_at or "").strip()
            or source.retrieved_at
            or now_tw().isoformat(timespec="seconds")
        )
        # published_at 僅在上游／呼叫端提供真實發布時間時填入；
        # 不可用 year_month／as_of 冒充來源發布時間。
        published = str(published_at or source.published_at or "").strip()
        existing_rows = database.get_monthly_revenue(sym, ym)
        existing = existing_rows[0] if existing_rows else None
        if (
            not allow_stale_overwrite
            and should_skip_stale_overwrite(existing, incoming_retrieved_at=retrieved)
        ):
            return MonthlyRevenueSlice(
                source_document_id=source_document_id,
                symbol=sym,
                year_month=ym,
                status="stale_skipped",
                provenance_status="exact",
                error=(
                    f"既有 retrieved_at={existing.retrieved_at} 較新，"
                    f"拒絕以 {retrieved} 覆寫"
                ),
            )

        payload = build_canonical_payload(
            symbol=sym,
            year_month=ym,
            revenue=float(revenue),
            yoy_pct=float(yoy_pct),
            mom_pct=float(mom_pct),
            cumulative=float(cumulative),
            cum_yoy_pct=float(cum_yoy_pct),
            source_document_id=source_document_id,
            retrieved_at=retrieved,
            published_at=published,
            source_url=source_url or source.url,
            quality_status=quality_status or source.quality_status or "ok",
        )
        text = canonical_text_from_payload(payload)

        research_document_id = "research-doc:" + hashlib.sha256(
            f"{source_document_id}|{text}".encode("utf-8")
        ).hexdigest()[:24]
        run_id = "rev-slice:" + hashlib.sha256(
            f"{source_document_id}|{research_document_id}".encode("utf-8")
        ).hexdigest()[:24]

        claim_seed = _extractive_claims(payload)
        claim_ids = [f"{run_id}:claim:{i}" for i in range(len(claim_seed))]
        evidence_probe = make_evidence(
            "monthly_revenue",
            source=SOURCE_NAME,
            source_ref=f"{sym}:{ym}",
            title=f"{sym} 月營收 {ym}",
            url=payload["source_url"],
            observed_at=ym,
            as_of=ym,
            published_at=published,
            retrieved_at=retrieved,
            excerpt=text[:2000],
            quality_status=str(payload["quality_status"]),
            source_document_id=source_document_id,
            provenance_status="exact",
            metadata={
                "research_document_id": research_document_id,
                "pipeline": PIPELINE,
                "symbol": sym,
                "year_month": ym,
                "revenue_unit": REVENUE_UNIT,
            },
        )
        evidence_id = str(evidence_probe["id"])
        existing_research = database.get_research_document(research_document_id)
        if (
            existing_research is not None
            and database.get_llm_run(run_id) is not None
            and database.get_evidence(evidence_id) is not None
            and all(database.get_claim(cid) is not None for cid in claim_ids)
        ):
            chunks = database.list_research_chunks(research_document_id)
            return MonthlyRevenueSlice(
                source_document_id=source_document_id,
                symbol=sym,
                year_month=ym,
                research_document_id=research_document_id,
                evidence_id=evidence_id,
                claim_ids=claim_ids,
                run_id=run_id,
                canonical_json=text,
                chunk_ids=[c.chunk_id for c in chunks],
                provenance_status="exact",
                status="reused",
            )

        database.upsert_monthly_revenue(MonthlyRevenue(
            symbol=sym,
            year_month=ym,
            revenue=float(revenue),
            yoy_pct=float(yoy_pct),
            mom_pct=float(mom_pct),
            cumulative=float(cumulative),
            cum_yoy_pct=float(cum_yoy_pct),
            note=f"unit={REVENUE_UNIT};pipeline={PIPELINE}",
            source_url=str(payload["source_url"]),
            source_date=published,
            retrieved_at=retrieved,
            quality_status=str(payload["quality_status"]),
            source_record_id=source_document_id,
        ))

        registry = SourceRegistry(project_root, db=database)
        research = registry.materialize_research_document(
            source_document_id,
            text,
            document_type="monthly_revenue",
            symbol=sym,
            title=f"{sym} 月營收 {ym}",
            published_at=published,
            metadata={
                "source": SOURCE_NAME,
                "pipeline": PIPELINE,
                "year_month": ym,
                "as_of": ym,
                "retrieved_at": retrieved,
                "revenue_unit": REVENUE_UNIT,
                "field_defs": FIELD_DEFS,
            },
        )
        research_document_id = research.document_id
        run_id = "rev-slice:" + hashlib.sha256(
            f"{source_document_id}|{research_document_id}".encode("utf-8")
        ).hexdigest()[:24]
        claim_ids = [f"{run_id}:claim:{i}" for i in range(len(claim_seed))]

        chunks = database.list_research_chunks(research.document_id)
        chunk_ids = [c.chunk_id for c in chunks]
        evidence_packet = make_evidence(
            "monthly_revenue",
            source=SOURCE_NAME,
            source_ref=f"{sym}:{ym}",
            title=f"{sym} 月營收 {ym}",
            url=payload["source_url"],
            observed_at=ym,
            as_of=ym,
            published_at=published,
            retrieved_at=retrieved,
            excerpt=text[:2000],
            quality_status=str(payload["quality_status"]),
            source_document_id=source_document_id,
            provenance_status="exact",
            metadata={
                "research_document_id": research.document_id,
                "pipeline": PIPELINE,
                "symbol": sym,
                "year_month": ym,
                "revenue_unit": REVENUE_UNIT,
                "field_defs": FIELD_DEFS,
            },
        )
        evidence_id = str(evidence_packet["id"])
        persist_evidence_items(database, [evidence_packet])

        now = now_tw().isoformat(timespec="seconds")
        database.upsert_llm_run(LlmRunRow(
            run_id=run_id,
            task_type=INGEST_TASK_TYPE,
            model="",
            prompt_id=PIPELINE,
            prompt_version="1.0",
            started_at=now,
            finished_at=now,
            status=INGEST_RUN_STATUS,
            input_hash=hashlib.sha256(
                f"{source_document_id}|{sym}|{ym}".encode("utf-8")
            ).hexdigest(),
            output_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            trace_id=source_document_id,
            metadata_json=json.dumps(
                {
                    "pipeline": PIPELINE,
                    "source_document_id": source_document_id,
                    "research_document_id": research.document_id,
                    "symbol": sym,
                    "year_month": ym,
                    "run_kind": "ingest",
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        ))
        database.link_llm_run_evidence(LlmRunEvidenceRow(
            run_id=run_id,
            evidence_id=evidence_id,
            ordinal=0,
            role="supporting",
        ))

        written_claims: List[str] = []
        for idx, claim_spec in enumerate(claim_seed):
            claim_id = f"{run_id}:claim:{idx}"
            ok = claim_snippet_supported(claim_spec["snippet"], text)
            claim_status = "verified" if ok else "unverified"
            database.upsert_claim(ClaimRow(
                claim_id=claim_id,
                run_id=run_id,
                text=claim_spec["text"],
                claim_type=claim_spec["claim_type"],
                confidence=1.0 if claim_status == "verified" else 0.0,
                status=claim_status,
                uncertainty="" if claim_status == "verified" else "欄位無法在 canonical JSON 核對",
                created_at=now,
                updated_at=now,
            ))
            database.link_claim_evidence(ClaimEvidenceRow(
                claim_id=claim_id,
                evidence_id=evidence_id,
                ordinal=idx,
            ))
            written_claims.append(claim_id)

        return MonthlyRevenueSlice(
            source_document_id=source_document_id,
            symbol=sym,
            year_month=ym,
            research_document_id=research.document_id,
            evidence_id=evidence_id,
            claim_ids=written_claims,
            run_id=run_id,
            canonical_json=text,
            chunk_ids=chunk_ids,
            provenance_status="exact",
            status="ok",
        )
    except Exception as exc:
        return MonthlyRevenueSlice(
            source_document_id=str(source_document_id or ""),
            symbol=str(symbol or ""),
            year_month=str(year_month or ""),
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        if owns_db:
            database.close()


def ingest_monthly_revenue_from_source_document(
    *,
    root: Path,
    source_document_id: str,
    ticker: str,
    db: Optional[StockDB] = None,
    allow_stale_overwrite: bool = False,
) -> MonthlyRevenueSlice:
    """由 exact source_document 重播 raw → canonical → research 切片。"""
    project_root = Path(root)
    owns_db = db is None
    database = db or StockDB.open(root=project_root)
    try:
        source, rows, error = load_raw_rows_from_source_document(
            root=project_root,
            source_document_id=source_document_id,
            db=database,
        )
        if error:
            return MonthlyRevenueSlice(
                source_document_id=source_document_id,
                symbol=str(ticker or ""),
                status="failed",
                provenance_status="unresolved",
                error=error,
            )
        if not rows:
            return MonthlyRevenueSlice(
                source_document_id=source_document_id,
                symbol=str(ticker or ""),
                status="empty",
                provenance_status="exact",
                error="raw 無列資料；不等於查無此事",
            )
        matched = find_ticker_revenue(rows, ticker)
        if matched is None:
            return MonthlyRevenueSlice(
                source_document_id=source_document_id,
                symbol=str(ticker or ""),
                status="empty",
                provenance_status="exact",
                error=f"raw 中找不到 ticker={ticker} 月營收列；不等於來源失敗",
            )
        ym = year_month_from_parts(matched.year, matched.month)
        return materialize_monthly_revenue_slice(
            root=project_root,
            source_document_id=source_document_id,
            symbol=matched.ticker,
            year_month=ym,
            revenue=matched.revenue,
            yoy_pct=matched.yoy,
            mom_pct=matched.mom,
            cumulative=matched.cum_revenue,
            cum_yoy_pct=matched.cum_yoy,
            source_url=(source.url if source else ""),
            published_at=(source.published_at if source else ""),
            retrieved_at=(source.retrieved_at if source else ""),
            quality_status=(source.quality_status if source else "ok"),
            db=database,
            allow_stale_overwrite=allow_stale_overwrite,
        )
    finally:
        if owns_db:
            database.close()


__all__ = [
    "FIELD_DEFS",
    "INGEST_RUN_STATUS",
    "INGEST_TASK_TYPE",
    "PIPELINE",
    "REVENUE_UNIT",
    "REVENUE_UNIT_LABEL",
    "SOURCE_NAME",
    "MonthlyRevenueSlice",
    "build_canonical_payload",
    "canonical_text_from_payload",
    "claim_snippet_supported",
    "find_ticker_revenue",
    "ingest_monthly_revenue_from_source_document",
    "load_raw_rows_from_source_document",
    "materialize_monthly_revenue_slice",
    "normalize_claim_whitespace",
    "should_skip_stale_overwrite",
    "source_document_usable_for_slice",
    "year_month_from_parts",
]
