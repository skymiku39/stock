"""共用管線輔助函式（macro 摘要、JSON 解析、ETF 共識、報告序列化）。"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from bot.cloud_file_cache import restore_file_from_cloud, restore_tree_from_cloud
from bot.evidence import make_evidence, persist_evidence_items
from bot.market_macro import macro_quality_status


def macro_summary_text(macro: Dict[str, Any]) -> str:
    bits: List[str] = []
    idx = macro.get("indices") or {}
    for sym, label in [
        ("^GSPC", "S&P"), ("^IXIC", "NASDAQ"),
        ("^SOX", "SOX"), ("^VIX", "VIX"),
        ("^TWII", "加權"),
    ]:
        q = idx.get(sym)
        if q:
            bits.append(f"{label} {q.get('pct_change', 0):+.2f}%")
    prem = macro.get("adr_premiums") or []
    if prem:
        for p in prem:
            bits.append(f"ADR {p.get('adr_symbol')} 溢價 {p.get('premium_pct', 0):+.2f}%")
    fb = macro.get("futures_basis")
    if isinstance(fb, dict) and fb.get("state") and fb.get("state") != "unknown":
        bits.append(
            f"台指期{fb.get('state')} {fb.get('basis', 0):+.0f}點"
            f"({fb.get('basis_pct', 0):+.2f}%, 近月OI {fb.get('open_interest', 0):,.0f})"
        )
    return "; ".join(bits) or "(無 macro)"


def parse_json_blob(text: str) -> Optional[Dict[str, Any]]:
    """robust JSON 解析 (處理 LLM 偶爾帶 ```json fence)。"""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:].strip()
    try:
        return json.loads(t)
    except Exception:
        l = t.find("{")
        r = t.rfind("}")
        if l >= 0 and r > l:
            try:
                return json.loads(t[l:r + 1])
            except Exception:
                return None
    return None


def consensus_tickers_today(root: Path) -> List[str]:
    """從最近一次 pipeline run 取共識 top tickers。"""
    base = root / "data" / "pipeline_runs"
    restore_tree_from_cloud(base, root=root)
    if not base.exists():
        return []
    runs = sorted([d for d in base.iterdir() if d.is_dir()], key=lambda p: p.name, reverse=True)
    if not runs:
        return []
    rj = runs[0] / "run.json"
    restore_file_from_cloud(rj, root=root)
    if not rj.exists():
        return []
    try:
        data = json.loads(rj.read_text(encoding="utf-8"))
        tickers: List[str] = []
        for item in (data.get("consensus_top") or []):
            t = str(item.get("ticker", "")).strip()
            if t and t.isdigit() and t not in tickers:
                tickers.append(t)
        return tickers[:20]
    except Exception:
        return []


def build_report_evidence(
    *,
    news_items: List[Any] | None = None,
    macro: Dict[str, Any] | None = None,
) -> List[Dict[str, Any]]:
    """建立分析報告可回放的輸入 evidence。

    只接受 collector 已明確附帶的 ``source_document_id``；此 helper 不會
    依 URL、標題或文字反查來源。沒有 ID 的快取／legacy 資料仍可保存為
    ``unresolved``，但不能被誤標成 exact。
    """
    out: List[Dict[str, Any]] = []
    for item in news_items or []:
        title = str(getattr(item, "title", "") or "").strip()
        summary = str(getattr(item, "summary", "") or "").strip()
        if not title and not summary:
            continue
        source = str(getattr(item, "source", "cnyes") or "cnyes")
        source_ref = str(
            getattr(item, "news_id", "") or getattr(item, "url", "") or title
        )
        out.append(make_evidence(
            "news",
            source=source,
            source_ref=source_ref,
            title=title,
            url=str(getattr(item, "url", "") or ""),
            published_at=str(getattr(item, "published_at", "") or ""),
            excerpt=summary or title,
            quality_status="ok" if summary or title else "partial",
            source_document_id=str(getattr(item, "source_document_id", "") or ""),
            metadata={
                "related_tickers": list(getattr(item, "related_tickers", []) or []),
                "keywords": list(getattr(item, "keywords", []) or []),
            },
        ))

    macro_data = macro or {}
    macro_excerpt = macro_summary_text(macro_data)
    if macro_excerpt != "(無 macro)":
        as_of = str(macro_data.get("asof_date") or "")
        out.append(make_evidence(
            "macro",
            source="market_macro",
            source_ref=f"macro:{as_of}" if as_of else "macro",
            title=f"Market macro snapshot {as_of}".strip(),
            observed_at=str(macro_data.get("fetched_at") or ""),
            as_of=as_of,
            retrieved_at=str(macro_data.get("fetched_at") or ""),
            excerpt=macro_excerpt,
            quality_status=macro_quality_status(macro_data),
            source_document_id=str(macro_data.get("source_document_id") or ""),
            metadata={
                "source_dates": dict(macro_data.get("source_dates") or {}),
                "source_date_status": str(macro_data.get("source_date_status") or "unknown"),
                "quality_status": macro_quality_status(macro_data),
            },
        ))

    unique: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in out:
        evidence_key = str(item.get("id") or "")
        if evidence_key and evidence_key not in seen:
            seen.add(evidence_key)
            unique.append(item)
    return unique


def persist_report_evidence(
    root: Path,
    items: List[Dict[str, Any]],
    *,
    logger: Any = None,
) -> List[Dict[str, Any]]:
    """將報告 evidence 寫入中央 registry，並驗證 exact raw 可回放性。"""
    if not items:
        return []
    from bot.source_registry import is_source_document_replayable
    from bot.stock_db import StockDB

    normalized: List[Dict[str, Any]] = []
    db = None
    try:
        db = StockDB.open(root=root)
        for raw in items:
            item = dict(raw)
            source_id = str(item.get("source_document_id") or "").strip()
            source = db.get_source_document(source_id) if source_id else None
            if source is None or not is_source_document_replayable(root, source):
                item["source_document_id"] = ""
                item["provenance_status"] = "unresolved"
                metadata = dict(item.get("metadata") or {})
                metadata["provenance_status"] = "unresolved"
                metadata["source_identity_check"] = "missing_or_not_replayable"
                item["metadata"] = metadata
            normalized.append(item)
        persist_evidence_items(db, normalized)
        return normalized
    except Exception as exc:
        if logger is not None:
            logger.warning("report evidence 保存失敗：%s", exc)
        return normalized or [dict(item) for item in items]
    finally:
        if db is not None:
            db.close()


def prepare_report_payload_for_storage(
    root: Path,
    data: Dict[str, Any],
    *,
    logger: Any = None,
) -> tuple[Dict[str, Any], List[Dict[str, Any]], str]:
    """在報告落 SQLite 前重跑 evidence provenance 閘門。

    報告 JSON 可能來自舊版快取；不能因為它已存在檔案中，就把
    ``news:<id>`` 或其他 legacy reference 當成 exact source document。這個
    helper 會保留標題／摘要，但只保留 collector 明確提供且能回放的
    ``source_document_id``，並在 payload 頂層留下 composite／unresolved 狀態。
    """
    payload = dict(data or {})
    raw_evidence = payload.get("evidence")
    if not isinstance(raw_evidence, list):
        source_sections = payload.get("source_sections")
        if isinstance(source_sections, dict):
            raw_evidence = source_sections.get("evidence")
    if not isinstance(raw_evidence, list):
        raw_evidence = []

    normalized = persist_report_evidence(root, raw_evidence, logger=logger)
    payload["evidence"] = normalized
    source_sections = payload.get("source_sections")
    if isinstance(source_sections, dict) and "evidence" in source_sections:
        source_sections = dict(source_sections)
        source_sections["evidence"] = normalized
        payload["source_sections"] = source_sections

    exact_items = [
        item for item in normalized
        if str(item.get("provenance_status") or "") in {"exact", "composite_exact"}
        and str(item.get("source_document_id") or "").strip()
    ]
    if normalized and len(exact_items) == len(normalized):
        provenance_status = "exact" if len(exact_items) == 1 else "composite_exact"
    else:
        provenance_status = "unresolved"
    payload["provenance_status"] = provenance_status

    quality_status = "ok" if not payload.get("errors") and provenance_status in {
        "exact", "composite_exact",
    } else "partial"
    return payload, normalized, quality_status


def repair_stored_report_provenance(
    root: Path,
    *,
    report_type: str = "",
    limit: int = 10000,
) -> Dict[str, int]:
    """明確的 opt-in 修復：重寫既有報告的 evidence provenance。

    這不是一般讀取路徑的一部分；只在管理者已建立備份後呼叫。它保留
    payload 中的標題／摘要與 evidence ID，但會把無法回放的來源降為
    ``unresolved``，不會依 URL 或標題猜補 source document。
    """
    from bot.stock_db import StockDB

    db = StockDB.open(root=Path(root))
    changed = 0
    skipped = 0
    try:
        rows = db.list_llm_daily_reports(
            report_type=report_type or None,
            limit=max(1, int(limit)),
        )
        for row in rows:
            try:
                raw_payload = json.loads(row.payload_json or "{}")
            except (TypeError, ValueError):
                skipped += 1
                continue
            if not isinstance(raw_payload, dict):
                skipped += 1
                continue
            payload, evidence, quality_status = prepare_report_payload_for_storage(
                Path(root), raw_payload,
            )
            next_payload_json = json.dumps(payload, ensure_ascii=False)
            next_evidence_json = json.dumps(evidence, ensure_ascii=False)
            if (
                row.payload_json == next_payload_json
                and row.evidence_json == next_evidence_json
                and row.quality_status == quality_status
            ):
                continue
            row.payload_json = next_payload_json
            row.evidence_json = next_evidence_json
            row.quality_status = quality_status
            db.upsert_llm_daily_report(row)
            changed += 1
    finally:
        db.close()
    return {"checked": len(rows) if "rows" in locals() else 0, "changed": changed, "skipped": skipped}


def intraday_report_to_dict(report: Any) -> Dict[str, Any]:
    """將 IntradayReport（或同欄位 dataclass）轉為 JSON 可序列化 dict。"""
    rankings = report.rankings
    if rankings and hasattr(rankings[0], "__dataclass_fields__"):
        rankings_out = [asdict(c) for c in rankings]
    else:
        rankings_out = list(rankings)
    return {
        "asof": report.asof,
        "market_tone": report.market_tone,
        "overall_brief": report.overall_brief,
        "themes": report.themes,
        "rankings": rankings_out,
        "macro_summary": report.macro_summary,
        "evidence": list(getattr(report, "evidence", []) or []),
        "brief_md": report.brief_md,
        "brief_prompt_id": report.brief_prompt_id,
        "brief_prompt_version": report.brief_prompt_version,
        "duration_sec": report.duration_sec,
        "errors": report.errors,
        "output_dir": report.output_dir,
    }
