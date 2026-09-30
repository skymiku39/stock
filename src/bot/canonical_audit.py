"""Read-only semantic checks for canonical SQLite facts.

The source audit verifies raw manifests and relational provenance.  This module
checks the values that are actually consumed by analysis, without rewriting
them.  A bad row remains in the database for replay, but the audit marks it so
callers cannot mistake persistence for correctness.
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from bot.data_quality import validate_ohlcv
from bot.source_audit import AuditFinding, DatasetAudit, _parse_date, audit_rows


CANONICAL_TABLES = (
    "stock_info",
    "etf_meta",
    "price_history",
    "intraday_bars",
    "monthly_revenue",
    "valuation_daily",
    "dividend_history",
    "quarterly_report",
)

_PERIOD_RE = re.compile(r"^(?:19|20)\d{2}-(?:0[1-9]|1[0-2])$")
_QUARTER_RE = re.compile(r"^(?:19|20)\d{2}Q[1-4]$")

# A source document being present is not enough: it must belong to the same
# data domain as the canonical row.  Keep this as prefixes because TWSE/TPEx
# endpoints have several source-specific report names (especially quarterly
# financial tables).
_SOURCE_DOMAIN_PREFIXES = {
    "stock_info": ("twse_company_info", "tpex_company_info", "company_"),
    "price_history": (
        "twse_stock_day", "tpex_stock_day", "yfinance_stock_day", "price_history",
    ),
    "intraday_bars": ("shioaji_", "intraday_"),
    "monthly_revenue": ("monthly_revenue_",),
    "valuation_daily": ("valuation_",),
    "dividend_history": ("dividend_",),
    "quarterly_report": ("quarterly_financials_",),
}


@dataclass
class CanonicalTableAudit:
    table: str
    rows: int
    quality_status: str
    findings: List[AuditFinding] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.quality_status in {"ok", "partial"}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "table": self.table,
            "rows": self.rows,
            "quality_status": self.quality_status,
            "findings": [finding.to_dict() for finding in self.findings],
        }


def _key(table: str, row: Mapping[str, Any]) -> str:
    fields = {
        "stock_info": ("symbol",),
        "etf_meta": ("symbol",),
        "price_history": ("symbol", "date"),
        "intraday_bars": ("symbol", "ts", "interval"),
        "monthly_revenue": ("symbol", "year_month"),
        "valuation_daily": ("symbol", "as_of"),
        "dividend_history": ("symbol", "year"),
        "quarterly_report": ("symbol", "period"),
    }.get(table, ("symbol",))
    return ":".join(str(row.get(field) or "") for field in fields)


def _finding(
    check: str,
    severity: str,
    message: str,
    *,
    index: int,
    table: str,
    row: Mapping[str, Any],
) -> AuditFinding:
    return AuditFinding(
        check=check,
        severity=severity,
        message=message,
        details={
            "table": table,
            "row_index": index,
            "row_key": _key(table, row),
        },
    )


def _audit_temporal_fields(
    table: str,
    row: Mapping[str, Any],
    *,
    index: int,
) -> List[AuditFinding]:
    """Check provenance timestamps without inventing missing dates."""
    findings: List[AuditFinding] = []
    for field_name in ("retrieved_at", "updated_at"):
        value = str(row.get(field_name) or "").strip()
        if not value:
            continue
        try:
            dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            findings.append(_finding(
                "timestamp_format", "error",
                f"時間格式無法解析：{field_name}={value}",
                index=index, table=table, row=row,
            ))

    source_date = str(row.get("source_date") or "").strip()
    if source_date:
        # Historical price batches commonly use the month of the download
        # (YYYY-MM), while quarterly sources may use YYYYQn. Both are valid
        # source periods even when the canonical row itself has a daily date.
        valid = (
            _PERIOD_RE.fullmatch(source_date) is not None
            or _QUARTER_RE.fullmatch(source_date.upper()) is not None
            or _parse_date(source_date) is not None
        )
        if not valid:
            findings.append(_finding(
                "source_date_format", "error",
                f"來源日期格式無法解析：{source_date}",
                index=index, table=table, row=row,
            ))
    return findings


def _audit_source_domain(
    table: str,
    row: Mapping[str, Any],
    *,
    index: int,
    source_documents: Optional[Mapping[str, str]],
) -> List[AuditFinding]:
    """Reject a real but cross-domain provenance link.

    Missing IDs and unknown IDs remain separate provenance/orphan findings in
    ``stock-data-audit``.  This check only handles the stronger case where the
    ID resolves to a known source with the wrong domain.
    """
    if not source_documents:
        return []
    source_id = str(row.get("source_record_id") or "").strip()
    source = str(source_documents.get(source_id) or "").strip()
    if not source:
        return []
    prefixes = _SOURCE_DOMAIN_PREFIXES.get(table, ())
    if not prefixes or any(source.startswith(prefix) for prefix in prefixes):
        return []
    return [_finding(
        "provenance_domain",
        "error",
        f"{table} 的 source_record_id 連到不相符來源域：{source}",
        index=index,
        table=table,
        row={**dict(row), "source": source, "source_record_id": source_id},
    )]


def _base_audit(table: str, rows: List[Dict[str, Any]]) -> DatasetAudit:
    if table == "stock_info":
        return audit_rows(
            rows, source=table, required_fields=("symbol", "name", "market"),
            key_fields=("symbol",), numeric_fields=("capital", "shares_outstanding"),
            numeric_ranges={"capital": (0.0, None), "shares_outstanding": (0.0, None)},
        )
    if table == "etf_meta":
        return audit_rows(
            rows, source=table, required_fields=("symbol", "name"),
            key_fields=("symbol",),
        )
    if table == "price_history":
        return audit_rows(
            rows, source=table,
            required_fields=("symbol", "date", "open", "high", "low", "close", "volume"),
            key_fields=("symbol", "date"), date_fields=("date",),
            numeric_fields=("open", "high", "low", "close", "volume"),
            numeric_ranges={
                "open": (0.0, None), "high": (0.0, None), "low": (0.0, None),
                "close": (0.0, None), "volume": (0.0, None),
            },
        )
    if table == "intraday_bars":
        return audit_rows(
            rows, source=table,
            required_fields=("symbol", "ts", "interval", "open", "high", "low", "close", "volume"),
            key_fields=("symbol", "ts", "interval"), date_fields=("ts",),
            numeric_fields=("open", "high", "low", "close", "volume", "amount"),
            numeric_ranges={
                "open": (0.0, None), "high": (0.0, None), "low": (0.0, None),
                "close": (0.0, None), "volume": (0.0, None), "amount": (0.0, None),
            },
        )
    if table == "monthly_revenue":
        return audit_rows(
            rows, source=table, required_fields=("symbol", "year_month"),
            key_fields=("symbol", "year_month"),
            numeric_fields=("revenue", "yoy_pct", "mom_pct", "cumulative", "cum_yoy_pct"),
            # 公開市場月營收偶爾會因退貨、重編或更正而出現負值；它是
            # source fact，不應在 canonical audit 被誤判成結構性損壞。
            # 數值可解析性仍由 numeric_fields 檢查。
        )
    if table == "valuation_daily":
        return audit_rows(
            rows, source=table, required_fields=("symbol", "as_of"),
            key_fields=("symbol", "as_of"), date_fields=("as_of",),
            numeric_fields=("pe_ratio", "pb_ratio", "dividend_yield"),
            numeric_ranges={"pb_ratio": (0.0, None), "dividend_yield": (0.0, None)},
        )
    if table == "dividend_history":
        return audit_rows(
            rows, source=table, required_fields=("symbol", "year"),
            key_fields=("symbol", "year"),
            numeric_fields=("year", "cash_dividend", "stock_dividend", "fill_days", "payout_ratio"),
            numeric_ranges={"year": (1900.0, float(dt.date.today().year + 1)),
                            "cash_dividend": (0.0, None), "stock_dividend": (0.0, None),
                            "fill_days": (0.0, None), "payout_ratio": (0.0, None)},
        )
    if table == "quarterly_report":
        return audit_rows(
            rows, source=table, required_fields=("symbol", "period"),
            key_fields=("symbol", "period"),
            numeric_fields=("eps", "revenue", "gross_margin", "op_margin", "net_margin"),
            # 財報比率不是固定在 [-100, 100] 的 bounded metric：小營收、
            # 一次性損失／收益或金融業列報方式都可能產生超出該區間的
            # exact source value。這裡只做格式與期間檢查，不臆判經濟合理性。
        )
    return DatasetAudit(source=table, rows=len(rows), quality_status="ok")


def audit_canonical_rows(
    table: str,
    rows: Iterable[Mapping[str, Any]],
    *,
    source_documents: Optional[Mapping[str, str]] = None,
) -> CanonicalTableAudit:
    """Audit one canonical table without changing any row."""
    materialized = [dict(row) for row in rows if isinstance(row, Mapping)]
    # Canonical tables are filled by independent pipelines.  An untouched
    # optional table means "not collected yet", not a malformed dataset.
    if not materialized:
        return CanonicalTableAudit(table, 0, "empty", [])
    base = _base_audit(table, materialized)
    findings = list(base.findings)

    for index, row in enumerate(materialized):
        findings.extend(_audit_temporal_fields(table, row, index=index))
        findings.extend(_audit_source_domain(
            table, row, index=index, source_documents=source_documents,
        ))
        if table == "price_history":
            errors = validate_ohlcv(
                open_price=row.get("open"), high=row.get("high"),
                low=row.get("low"), close=row.get("close"),
                volume=row.get("volume"),
            )
            for error in errors:
                findings.append(_finding(
                    "ohlcv_invariant", "error", error,
                    index=index, table=table, row=row,
                ))
        elif table == "intraday_bars":
            errors = validate_ohlcv(
                open_price=row.get("open"), high=row.get("high"),
                low=row.get("low"), close=row.get("close"),
                volume=row.get("volume"), amount=row.get("amount"),
            )
            for error in errors:
                findings.append(_finding(
                    "ohlcv_invariant", "error", error,
                    index=index, table=table, row=row,
                ))
        elif table == "monthly_revenue":
            period = str(row.get("year_month") or "").strip()
            if not _PERIOD_RE.fullmatch(period):
                findings.append(_finding(
                    "period_format", "error", f"月份期間格式無效：{period}",
                    index=index, table=table, row=row,
                ))
        elif table == "quarterly_report":
            period = str(row.get("period") or "").strip().upper()
            if not _QUARTER_RE.fullmatch(period):
                findings.append(_finding(
                    "period_format", "error", f"季度期間格式無效：{period}",
                    index=index, table=table, row=row,
                ))
        elif table == "dividend_history":
            for field_name in ("ex_dividend_date", "ex_right_date", "fill_date"):
                value = str(row.get(field_name) or "").strip()
                if value and not _date_is_parseable(value):
                    findings.append(_finding(
                        "date_format", "error", f"日期格式無法解析：{field_name}={value}",
                        index=index, table=table, row=row,
                    ))

    if any(item.severity == "error" for item in findings):
        status = "invalid"
    elif any(item.severity == "warning" for item in findings):
        status = "partial"
    else:
        status = "ok"
    return CanonicalTableAudit(table, len(materialized), status, findings)


def _date_is_parseable(value: str) -> bool:
    text = value.strip()
    try:
        dt.date.fromisoformat(text[:10].replace("/", "-"))
        return True
    except ValueError:
        parts = re.split(r"[-/.]", text)
        if len(parts) == 3 and all(part.isdigit() for part in parts):
            year, month, day = (int(part) for part in parts)
            if len(parts[0]) == 3 and year < 1911:
                year += 1911
            try:
                dt.date(year, month, day)
                return True
            except ValueError:
                return False
        return False


def audit_canonical_database(path: Path) -> Dict[str, Any]:
    """Read a SQLite database in read-only mode and audit supported tables."""
    target = Path(path).resolve()
    if not target.is_file():
        return {"exists": False, "path": str(target), "tables": {}, "rows_checked": 0}
    conn = sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        existing = {
            str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        reports: Dict[str, Dict[str, Any]] = {}
        rows_checked = 0
        source_documents: Dict[str, str] = {}
        if "source_documents" in existing:
            source_documents = {
                str(row["document_id"]): str(row["source"] or "")
                for row in conn.execute(
                    "SELECT document_id, source FROM source_documents"
                ).fetchall()
            }
        for table in CANONICAL_TABLES:
            if table not in existing:
                continue
            rows = [dict(row) for row in conn.execute(f'SELECT * FROM "{table}"')]
            report = audit_canonical_rows(
                table, rows, source_documents=source_documents,
            )
            reports[table] = report.to_dict()
            rows_checked += report.rows
        invalid = sum(report["quality_status"] == "invalid" for report in reports.values())
        partial = sum(report["quality_status"] == "partial" for report in reports.values())
        empty = sum(report["quality_status"] == "empty" for report in reports.values())
        return {
            "exists": True,
            "path": str(target),
            "tables": reports,
            "rows_checked": rows_checked,
            "invalid_tables": invalid,
            "partial_tables": partial,
            "empty_tables": empty,
        }
    finally:
        conn.close()


__all__ = [
    "CANONICAL_TABLES",
    "CanonicalTableAudit",
    "audit_canonical_database",
    "audit_canonical_rows",
]
