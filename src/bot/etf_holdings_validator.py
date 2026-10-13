"""ETF 持股的來源專用 domain validator。

驗證結構化持股快照的 schema、重複代號、權重非負、可選的權重加總警告，
以及 ``as_of``／``retrieved_at`` 語意區隔。回傳結構化 errors／warnings 與
``quality_status``，供 research slice 寫入 evidence；不臆造
``source_document_id``，也不把 cache-only／unknown 來源自動升格為 ok。
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Union

from bot.data_quality import (
    QUALITY_INVALID,
    QUALITY_OK,
    QUALITY_PARTIAL,
    combine_quality_status,
)
from bot.source_audit import AuditFinding


_DATE_RE = re.compile(r"^(?:19|20)\d{2}-\d{2}-\d{2}$")
_WEIGHT_SUM_TARGET = 100.0
_WEIGHT_SUM_TOLERANCE = 5.0


HoldingLike = Union[Mapping[str, Any], Any]


@dataclass
class EtfHoldingsValidationResult:
    """ETF 持股驗證結果；可直接對應既有 quality_status 契約。"""

    quality_status: str = QUALITY_OK
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    findings: List[AuditFinding] = field(default_factory=list)
    holding_count: int = 0
    weight_sum: float = 0.0
    as_of: str = ""
    retrieved_at: str = ""
    duplicate_tickers: List[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.quality_status in {QUALITY_OK, QUALITY_PARTIAL}

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["findings"] = [finding.to_dict() for finding in self.findings]
        return payload


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _holding_get(holding: HoldingLike, key: str, default: Any = None) -> Any:
    if isinstance(holding, Mapping):
        return holding.get(key, default)
    return getattr(holding, key, default)


def _parse_weight(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _snapshot_fields(
    snapshot: Any,
) -> tuple[str, str, Sequence[HoldingLike]]:
    if isinstance(snapshot, Mapping):
        symbol = _text(snapshot.get("symbol"))
        as_of = _text(snapshot.get("as_of") or snapshot.get("date"))
        holdings = snapshot.get("holdings") or []
        if not isinstance(holdings, Sequence) or isinstance(holdings, (str, bytes)):
            holdings = []
        return symbol, as_of, list(holdings)
    symbol = _text(getattr(snapshot, "symbol", ""))
    raw_date = getattr(snapshot, "date", None)
    if isinstance(raw_date, dt.date):
        as_of = raw_date.isoformat()
    else:
        as_of = _text(raw_date or getattr(snapshot, "as_of", ""))
    holdings = getattr(snapshot, "holdings", None) or []
    if not isinstance(holdings, Sequence) or isinstance(holdings, (str, bytes)):
        holdings = []
    return symbol, as_of, list(holdings)


def validate_etf_holdings(
    snapshot: Any,
    *,
    as_of: str = "",
    retrieved_at: str = "",
    source_quality_status: str = "",
    weight_sum_tolerance: float = _WEIGHT_SUM_TOLERANCE,
) -> EtfHoldingsValidationResult:
    """驗證 ETF 持股快照；失敗不拋例外，回傳結構化結果。"""
    result = EtfHoldingsValidationResult(
        as_of=_text(as_of),
        retrieved_at=_text(retrieved_at),
    )
    findings: List[AuditFinding] = []
    symbol, snapshot_as_of, holdings = _snapshot_fields(snapshot)
    if not result.as_of:
        result.as_of = snapshot_as_of

    if not symbol:
        findings.append(AuditFinding(
            "schema", "error", "缺少 ETF symbol",
        ))
    if not isinstance(holdings, list):
        findings.append(AuditFinding(
            "schema", "error", "holdings 必須是 list",
        ))
        holdings = []

    if not holdings:
        findings.append(AuditFinding(
            "schema", "error", "holdings 為空",
        ))

    seen: Dict[str, int] = {}
    duplicates: List[str] = []
    weight_sum = 0.0
    for index, holding in enumerate(holdings):
        if not isinstance(holding, (Mapping,)) and not hasattr(holding, "ticker"):
            findings.append(AuditFinding(
                "schema", "error",
                f"第 {index + 1} 筆持股不是物件",
                {"row_index": index},
            ))
            continue
        ticker = _text(_holding_get(holding, "ticker"))
        name = _text(_holding_get(holding, "name"))
        if not ticker and not name:
            findings.append(AuditFinding(
                "schema", "error",
                f"第 {index + 1} 筆缺少 ticker／name",
                {"row_index": index},
            ))
        weight = _parse_weight(_holding_get(holding, "weight_pct", 0))
        if weight is None:
            findings.append(AuditFinding(
                "schema", "error",
                f"第 {index + 1} 筆 weight_pct 非數值",
                {"row_index": index},
            ))
            continue
        if weight < 0:
            findings.append(AuditFinding(
                "weight_negative", "error",
                f"第 {index + 1} 筆權重為負：{weight}",
                {"row_index": index, "ticker": ticker, "weight_pct": weight},
            ))
        weight_sum += weight
        if ticker:
            key = ticker.casefold()
            if key in seen:
                duplicates.append(ticker)
                findings.append(AuditFinding(
                    "duplicate_ticker", "error",
                    f"重複持股代號：{ticker}",
                    {
                        "ticker": ticker,
                        "first_row_index": seen[key],
                        "row_index": index,
                    },
                ))
            else:
                seen[key] = index

    result.holding_count = len(holdings)
    result.weight_sum = weight_sum
    result.duplicate_tickers = sorted(set(duplicates))

    if (
        holdings
        and abs(weight_sum - _WEIGHT_SUM_TARGET) > float(weight_sum_tolerance)
    ):
        findings.append(AuditFinding(
            "weight_sum", "warning",
            (
                f"權重加總 {weight_sum:.4g}% 偏離 "
                f"{_WEIGHT_SUM_TARGET:g}%±{weight_sum_tolerance:g}"
            ),
            {"weight_sum": weight_sum, "target": _WEIGHT_SUM_TARGET},
        ))

    # as_of = 資料所屬日；retrieved_at = 抓取時間。兩者不可互相冒充。
    as_of_value = result.as_of
    retrieved = result.retrieved_at
    if as_of_value:
        if "T" in as_of_value or " " in as_of_value.strip():
            findings.append(AuditFinding(
                "as_of_retrieved_at", "error",
                "as_of 不可使用抓取時間戳；請與 retrieved_at 區分",
                {"as_of": as_of_value, "retrieved_at": retrieved},
            ))
        elif not _DATE_RE.match(as_of_value):
            findings.append(AuditFinding(
                "as_of_format", "warning",
                f"as_of 無法辨識為 YYYY-MM-DD：{as_of_value}",
                {"as_of": as_of_value},
            ))
    if not as_of_value and retrieved:
        findings.append(AuditFinding(
            "as_of_retrieved_at", "warning",
            "缺少 as_of；retrieved_at 只能辨識抓取版本，不可冒充資料期間",
            {"retrieved_at": retrieved},
        ))
    if as_of_value and retrieved and as_of_value == retrieved:
        findings.append(AuditFinding(
            "as_of_retrieved_at", "error",
            "as_of 與 retrieved_at 相同字串，語意未區分",
            {"as_of": as_of_value, "retrieved_at": retrieved},
        ))

    errors = [f.message for f in findings if f.severity == "error"]
    warnings = [f.message for f in findings if f.severity == "warning"]
    result.findings = findings
    result.errors = errors
    result.warnings = warnings
    if errors:
        result.quality_status = QUALITY_INVALID
    elif warnings:
        result.quality_status = QUALITY_PARTIAL
    else:
        result.quality_status = QUALITY_OK

    if source_quality_status:
        result.quality_status = combine_quality_status(
            source_quality_status, result.quality_status,
        )
        # 合併後若來源把品質拉低，保留驗證找出的 errors／warnings 不變。
    return result


def validate_etf_holdings_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    symbol: str,
    as_of: str = "",
    retrieved_at: str = "",
    source_quality_status: str = "",
) -> EtfHoldingsValidationResult:
    """以 list-of-dict holdings 驗證；供 fixture／非 dataclass 路徑使用。"""
    return validate_etf_holdings(
        {"symbol": symbol, "as_of": as_of, "holdings": list(rows)},
        as_of=as_of,
        retrieved_at=retrieved_at,
        source_quality_status=source_quality_status,
    )


__all__ = [
    "EtfHoldingsValidationResult",
    "validate_etf_holdings",
    "validate_etf_holdings_rows",
]


