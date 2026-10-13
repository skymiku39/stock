"""Raw-first materialization for full-market fundamental datasets.

The normal refresh queue materializes only requested tickers.  This module
provides the complementary batch path: take the latest *exact* raw source
documents already registered in ``SourceDB``, parse every row, and write the
canonical/research/evidence/claim chain without guessing a document by URL or
cache filename.

No network access happens here.  A separate fetcher must first capture the
raw response; this boundary makes the batch operation deterministic and
replayable.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from bot.fundamentals_fetcher import (
    DividendRecord,
    _decumulate_quarterlies,
    _parse_dividend,
    _parse_one_revenue,
    _parse_quarterly_raw,
    _parse_tpex_dividend_csv,
    _parse_valuation,
)
from bot.dividend_research import materialize_dividend_slice
from bot.monthly_revenue_research import (
    materialize_monthly_revenue_slice,
    year_month_from_parts,
)
from bot.quarterly_research import materialize_quarterly_slice
from bot.source_registry import resolve_source_document_path
from bot.stock_db import (
    DividendHistory,
    QuarterlyReport,
    SourceDocumentRow,
    StockDB,
    ValuationDaily,
)
from bot.utils import get_logger
from bot.valuation_research import materialize_valuation_slice


@dataclass
class BatchStats:
    """Counters for one or more deterministic materialization passes."""

    source_documents: int = 0
    rows_seen: int = 0
    attempted: int = 0
    ok: int = 0
    reused: int = 0
    stale_skipped: int = 0
    skipped: int = 0
    failed: int = 0
    errors: List[str] = field(default_factory=list)

    def absorb(self, status: str, *, error: str = "") -> None:
        state = str(status or "failed")
        if state in {"ok", "reused"}:
            setattr(self, state, int(getattr(self, state)) + 1)
        elif state == "stale_skipped":
            self.stale_skipped += 1
        elif state == "skipped":
            self.skipped += 1
        else:
            self.failed += 1
            if error and len(self.errors) < 20:
                self.errors.append(str(error)[:500])

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _latest_documents(db: StockDB, source_prefix: str) -> List[SourceDocumentRow]:
    """Return one newest manifest row per exact source name."""
    rows = db.conn.execute(
        """
        SELECT document_id FROM source_documents
        WHERE source LIKE ? AND kind = 'http_response'
        ORDER BY source, retrieved_at DESC, document_id DESC
        """,
        (f"{source_prefix}%",),
    ).fetchall()
    seen: Set[str] = set()
    out: List[SourceDocumentRow] = []
    for row in rows:
        document = db.get_source_document(str(row[0]))
        if document is None or document.source in seen:
            continue
        seen.add(document.source)
        out.append(document)
    return out


def _raw_rows(root: Path, source: SourceDocumentRow) -> List[Dict[str, Any]]:
    """Read and hash-check one exact raw archive document."""
    path = resolve_source_document_path(root, source)
    if not path.is_file():
        raise FileNotFoundError(source.content_path)
    raw = path.read_bytes()
    if source.content_hash:
        digest = hashlib.sha256(raw).hexdigest()
        if digest != source.content_hash:
            raise ValueError(
                f"raw hash mismatch for {source.document_id}: "
                f"expected {source.content_hash[:12]} got {digest[:12]}"
            )
    payload = json.loads(raw.decode("utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
        payload = payload["rows"]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        return [payload]
    raise ValueError(f"raw payload is not an object/list: {source.document_id}")


def _dividend_raw_rows(root: Path, source: SourceDocumentRow) -> List[Dict[str, Any]]:
    """Read one exact TWSE JSON or TPEx CSV dividend archive."""
    path = resolve_source_document_path(root, source)
    if not path.is_file():
        raise FileNotFoundError(source.content_path)
    raw = path.read_bytes()
    if source.content_hash:
        digest = hashlib.sha256(raw).hexdigest()
        if digest != source.content_hash:
            raise ValueError(
                f"raw hash mismatch for {source.document_id}: "
                f"expected {source.content_hash[:12]} got {digest[:12]}"
            )
    if source.source == "dividend_tpex" or path.suffix.lower() == ".csv":
        return _parse_tpex_dividend_csv(raw.decode("utf-8-sig"))
    payload = json.loads(raw.decode("utf-8-sig"))
    if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
        payload = payload["rows"]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        return [payload]
    raise ValueError(f"raw dividend payload is not an object/list: {source.document_id}")


def _materialize_dividend(
    root: Path,
    db: StockDB,
    *,
    limit: int = 0,
    logger: logging.Logger,
) -> BatchStats:
    """Materialize the newest exact full-market dividend archives.

    The public endpoints contain multiple rows for one symbol/year (for
    example quarterly distributions).  They are aggregated by that key while
    retaining the exact source document that supplied the rows.  If a key
    unexpectedly spans multiple source documents, it is skipped instead of
    guessing a representative provenance ID.
    """
    stats = BatchStats()
    grouped: Dict[Tuple[str, int], DividendRecord] = {}
    source_ids: Dict[Tuple[str, int], Set[str]] = {}
    for source in _latest_documents(db, "dividend_"):
        stats.source_documents += 1
        try:
            rows = _dividend_raw_rows(root, source)
        except Exception as exc:
            stats.absorb("failed", error=f"{source.document_id}: {exc}")
            continue
        stats.rows_seen += len(rows)
        for raw in rows:
            symbol = str(
                raw.get("公司代號") or raw.get("股票代號") or raw.get("Code") or ""
            ).strip()
            if not symbol:
                stats.skipped += 1
                continue
            enriched = dict(raw)
            enriched.update({
                "_source_document_id": source.document_id,
                "_source_url": source.url,
                # as_of is the source data period.  retrieved_at is only the
                # local capture timestamp and must never be promoted to
                # canonical source_date when the source omitted its period.
                "_source_date": source.as_of,
                "_retrieved_at": source.retrieved_at,
                "_quality_status": source.quality_status,
            })
            parsed = _parse_dividend(enriched, symbol)
            if parsed is None or parsed.year <= 0:
                stats.skipped += 1
                continue
            key = (parsed.ticker, parsed.year)
            source_ids.setdefault(key, set()).add(source.document_id)
            current = grouped.get(key)
            if current is None:
                grouped[key] = parsed
                continue
            current.cash_dividend += parsed.cash_dividend
            current.stock_dividend += parsed.stock_dividend
            current.ex_dividend_date = current.ex_dividend_date or parsed.ex_dividend_date
            current.ex_right_date = current.ex_right_date or parsed.ex_right_date
            current.fill_date = current.fill_date or parsed.fill_date
            if current.payout_ratio is None:
                current.payout_ratio = parsed.payout_ratio

    for key in sorted(grouped):
        if limit > 0 and stats.attempted >= limit:
            break
        source_documents = source_ids.get(key, set())
        item = grouped[key]
        if len(source_documents) != 1 or not item.source_document_id:
            stats.skipped += 1
            stats.errors.append(
                f"{key[0]}:{key[1]} spans multiple or missing exact source documents"
            )
            continue
        item.cash_dividend = round(item.cash_dividend, 4)
        item.stock_dividend = round(item.stock_dividend, 4)
        canonical = DividendHistory(
            symbol=item.ticker,
            year=item.year,
            cash_dividend=item.cash_dividend,
            stock_dividend=item.stock_dividend,
            ex_dividend_date=item.ex_dividend_date,
            ex_right_date=item.ex_right_date,
            fill_date=item.fill_date,
            fill_days=item.fill_days,
            payout_ratio=item.payout_ratio,
            source_url=item.source_url,
            source_date=item.source_date,
            retrieved_at=item.retrieved_at,
            quality_status=item.quality_status,
            source_record_id=item.source_document_id,
        )
        stats.attempted += 1
        db.upsert_dividend_history(canonical)
        result = materialize_dividend_slice(root=root, dividend=canonical, db=db)
        stats.absorb(result.status, error=result.error)
    logger.info("full-market dividend materialize: %s", stats.to_dict())
    return stats


def _normalize_valuation_row(raw: Dict[str, Any], source: SourceDocumentRow) -> Dict[str, Any]:
    """Map TPEx's native fields to the common valuation parser contract."""
    if source.source == "valuation_tpex":
        return {
            "Code": raw.get("SecuritiesCompanyCode"),
            "Name": raw.get("CompanyName"),
            "PEratio": raw.get("PriceEarningRatio"),
            "PBratio": raw.get("PriceBookRatio"),
            "DividendYield": raw.get("YieldRatio"),
            "Date": raw.get("Date") or raw.get("資料日期") or "",
            "_source_url": source.url,
            "_retrieved_at": source.retrieved_at,
            "_quality_status": source.quality_status,
            "_source_document_id": source.document_id,
        }
    row = dict(raw)
    row.update({
        "_source_url": source.url,
        "_retrieved_at": source.retrieved_at,
        "_quality_status": source.quality_status,
        "_source_document_id": source.document_id,
    })
    return row


def _materialize_monthly_revenue(
    root: Path,
    db: StockDB,
    *,
    limit: int = 0,
    logger: logging.Logger,
) -> BatchStats:
    stats = BatchStats()
    seen: Set[Tuple[str, str]] = set()
    for source in _latest_documents(db, "monthly_revenue_"):
        stats.source_documents += 1
        try:
            rows = _raw_rows(root, source)
        except Exception as exc:
            stats.absorb("failed", error=f"{source.document_id}: {exc}")
            continue
        stats.rows_seen += len(rows)
        for raw in rows:
            parsed = _parse_one_revenue(raw)
            if parsed is None or parsed.year <= 0 or parsed.month <= 0:
                stats.skipped += 1
                continue
            key = (parsed.ticker, year_month_from_parts(parsed.year, parsed.month))
            if key in seen:
                stats.skipped += 1
                continue
            seen.add(key)
            if limit > 0 and stats.attempted >= limit:
                return stats
            stats.attempted += 1
            result = materialize_monthly_revenue_slice(
                root=root,
                source_document_id=source.document_id,
                symbol=parsed.ticker,
                year_month=key[1],
                revenue=parsed.revenue,
                yoy_pct=parsed.yoy,
                mom_pct=parsed.mom,
                cumulative=parsed.cum_revenue,
                cum_yoy_pct=parsed.cum_yoy,
                source_url=source.url,
                published_at=source.published_at or source.as_of or key[1],
                retrieved_at=source.retrieved_at,
                quality_status=source.quality_status,
                db=db,
            )
            stats.absorb(result.status, error=result.error)
    logger.info("full-market monthly revenue materialize: %s", stats.to_dict())
    return stats


def _materialize_valuation(
    root: Path,
    db: StockDB,
    *,
    limit: int = 0,
    logger: logging.Logger,
) -> BatchStats:
    stats = BatchStats()
    seen: Set[Tuple[str, str]] = set()
    for source in _latest_documents(db, "valuation_"):
        stats.source_documents += 1
        try:
            rows = _raw_rows(root, source)
        except Exception as exc:
            stats.absorb("failed", error=f"{source.document_id}: {exc}")
            continue
        stats.rows_seen += len(rows)
        for raw in rows:
            parsed = _parse_valuation(_normalize_valuation_row(raw, source))
            if parsed is None or not parsed.ticker or not parsed.date:
                stats.skipped += 1
                continue
            key = (parsed.ticker, parsed.date)
            if key in seen:
                stats.skipped += 1
                continue
            seen.add(key)
            if limit > 0 and stats.attempted >= limit:
                return stats
            stats.attempted += 1
            canonical = ValuationDaily(
                symbol=parsed.ticker,
                as_of=parsed.date,
                pe_ratio=parsed.pe_ratio,
                pb_ratio=parsed.pb_ratio,
                dividend_yield=parsed.dividend_yield,
                dividend_year=parsed.dividend_year,
                source_url=parsed.source_url,
                source_date=parsed.source_date,
                retrieved_at=parsed.retrieved_at,
                quality_status=parsed.quality_status,
                source_record_id=parsed.source_document_id,
            )
            db.upsert_valuation(canonical)
            result = materialize_valuation_slice(root=root, valuation=canonical, db=db)
            stats.absorb(result.status, error=result.error)
    logger.info("full-market valuation materialize: %s", stats.to_dict())
    return stats


def _materialize_quarterly(
    root: Path,
    db: StockDB,
    *,
    limit: int = 0,
    logger: logging.Logger,
) -> BatchStats:
    stats = BatchStats()
    seen: Set[Tuple[str, str]] = set()
    for source in _latest_documents(db, "quarterly_financials_"):
        stats.source_documents += 1
        try:
            rows = _raw_rows(root, source)
        except Exception as exc:
            stats.absorb("failed", error=f"{source.document_id}: {exc}")
            continue
        stats.rows_seen += len(rows)
        for raw in rows:
            symbol = str(raw.get("公司代號") or raw.get("SecuritiesCompanyCode") or "").strip()
            if not symbol:
                stats.skipped += 1
                continue
            enriched = dict(raw)
            enriched.update({
                "_source_document_id": source.document_id,
                "_source_url": source.url,
                "_retrieved_at": source.retrieved_at,
                "_quality_status": source.quality_status,
            })
            parsed = _parse_quarterly_raw(enriched, symbol)
            if parsed is None:
                stats.skipped += 1
                continue
            quarterlies = _decumulate_quarterlies(symbol, [parsed])
            if not quarterlies:
                stats.skipped += 1
                continue
            item = quarterlies[0]
            period = f"{item.year}Q{item.quarter}"
            key = (symbol, period)
            if key in seen:
                stats.skipped += 1
                continue
            seen.add(key)
            if limit > 0 and stats.attempted >= limit:
                return stats
            stats.attempted += 1
            report = QuarterlyReport(
                symbol=item.ticker,
                period=period,
                eps=item.eps,
                revenue=item.revenue,
                gross_margin=item.gross_margin,
                op_margin=item.operating_margin,
                net_margin=item.net_margin,
                note=item.note,
                source_url=item.source_url,
                source_date=item.source_date,
                retrieved_at=item.retrieved_at,
                quality_status=item.quality_status,
                source_record_id=item.source_document_id,
            )
            db.upsert_quarterly_report(report)
            result = materialize_quarterly_slice(root=root, report=report, db=db)
            stats.absorb(result.status, error=result.error)
    logger.info("full-market quarterly materialize: %s", stats.to_dict())
    return stats


def materialize_all_fundamentals(
    root: Path,
    *,
    limit_per_domain: int = 0,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, Any]:
    """Materialize latest exact raw rows for all four fundamental domains.

    ``limit_per_domain`` is intended for smoke tests; zero means all parsed
    rows.  The returned counters are safe to persist in a pipeline manifest.
    """
    project_root = Path(root).resolve()
    log = logger or get_logger("fundamentals-research-batch", log_dir=project_root / "log")
    db = StockDB.open(root=project_root)
    try:
        domains = {
            "monthly_revenue": _materialize_monthly_revenue(
                project_root, db, limit=limit_per_domain, logger=log,
            ).to_dict(),
            "valuation": _materialize_valuation(
                project_root, db, limit=limit_per_domain, logger=log,
            ).to_dict(),
            "quarterly": _materialize_quarterly(
                project_root, db, limit=limit_per_domain, logger=log,
            ).to_dict(),
            "dividend_history": _materialize_dividend(
                project_root, db, limit=limit_per_domain, logger=log,
            ).to_dict(),
        }
        result: Dict[str, Any] = dict(domains)
        result["failed"] = sum(int(value.get("failed", 0)) for value in domains.values())
        result["attempted"] = sum(int(value.get("attempted", 0)) for value in domains.values())
        return result
    finally:
        db.close()


__all__ = ["BatchStats", "materialize_all_fundamentals"]
