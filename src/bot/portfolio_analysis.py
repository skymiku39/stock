"""Portfolio analysis data pack helpers.

The LLM should analyze evidence, not just a position table.  This module builds
a compact, auditable bundle from broker positions plus the local ticker
snapshots gathered by the dashboard.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Dict, Iterable, List, Mapping, Optional


QUALITY_FACTORS = [
    ("price", "價格"),
    ("fundamentals", "基本面"),
    ("technicals", "技術面"),
    ("chips", "三大法人"),
    ("distribution", "TDCC"),
    ("etf", "ETF共識"),
    ("llm", "既有LLM"),
]


def build_portfolio_analysis_bundle(
    *,
    rows: Iterable[Mapping[str, Any]],
    snapshots: Mapping[str, Mapping[str, Any]],
    broker_meta: Optional[Mapping[str, Any]] = None,
    warnings: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Build a JSON-serializable evidence bundle for portfolio LLM analysis."""
    clean_rows = [_jsonable(dict(row)) for row in rows]
    by_symbol = {str(row.get("代號") or row.get("symbol") or ""): row for row in clean_rows}
    holdings: List[Dict[str, Any]] = []
    quality_rows: List[Dict[str, Any]] = []

    for symbol, row in by_symbol.items():
        snap = dict(snapshots.get(symbol, {}) or {})
        quality = data_quality(symbol, row, snap)
        quality_rows.append(quality)
        holdings.append({
            "symbol": symbol,
            "name": row.get("名稱", "") or snap.get("name", ""),
            "industry": row.get("產業", ""),
            "broker": {
                "qty_lots": row.get("券商張數", 0),
                "avg_price": row.get("券商均價", 0),
                "cost_twd": row.get("券商成本", 0),
                "last_price": row.get("最新價", 0),
                "market_value_twd": row.get("券商市值", 0),
                "unrealized_pnl_twd": row.get("未實現損益", 0),
                "unrealized_pnl_pct": row.get("損益%", 0),
                "weight_pct": row.get("權重%", 0),
                "position_type": row.get("庫存類別", ""),
            },
            "ownership": {
                "label": row.get("本工具標記", ""),
                "bot_qty_lots": row.get("本工具張數", 0),
                "manual_or_external_qty_lots": row.get("手動/外部張數", 0),
                "bot_avg_cost": row.get("本工具均價", 0),
                "detail": row.get("標記說明", ""),
            },
            "evidence": compact_snapshot(snap),
            "data_quality": quality,
        })

    totals = portfolio_totals(clean_rows)
    quality_summary = summarize_quality(quality_rows)
    return {
        "asof": _now_iso(),
        "broker": _jsonable(dict(broker_meta or {})),
        "totals": totals,
        "industry_exposure": industry_exposure(clean_rows),
        "risk_flags_from_rules": warnings or [],
        "data_quality_summary": quality_summary,
        "data_quality": quality_rows,
        "holdings": holdings,
    }


def portfolio_totals(rows: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    items = list(rows)
    cost = sum(_num(r.get("券商成本")) for r in items)
    market = sum(_num(r.get("券商市值")) for r in items)
    pnl = sum(_num(r.get("未實現損益")) for r in items)
    ret = pnl / cost * 100.0 if cost > 0 else 0.0
    bot_count = sum(1 for r in items if _num(r.get("本工具張數")) > 0)
    top3 = sum(sorted((_num(r.get("權重%")) for r in items), reverse=True)[:3])
    return {
        "holding_count": len(items),
        "broker_cost_twd": round(cost, 0),
        "broker_market_value_twd": round(market, 0),
        "unrealized_pnl_twd": round(pnl, 0),
        "unrealized_pnl_pct": round(ret, 2),
        "bot_marked_count": bot_count,
        "top3_weight_pct": round(top3, 2),
    }


def industry_exposure(rows: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    totals: Dict[str, float] = {}
    for row in rows:
        industry = str(row.get("產業") or "未分類")
        totals[industry] = totals.get(industry, 0.0) + _num(row.get("券商市值"))
    total = sum(totals.values())
    out = []
    for industry, value in sorted(totals.items(), key=lambda item: item[1], reverse=True):
        out.append({
            "industry": industry,
            "market_value_twd": round(value, 0),
            "weight_pct": round(value / total * 100.0, 2) if total > 0 else 0.0,
        })
    return out


def compact_snapshot(snap: Mapping[str, Any]) -> Dict[str, Any]:
    fundamentals = snap.get("fundamentals") or {}
    technicals = snap.get("technicals") or {}
    llm = snap.get("llm_analysis") or {}
    logic = snap.get("logic_check") or {}
    consensus = snap.get("consensus") or {}
    distribution = snap.get("distribution") or {}
    macro = snap.get("macro_snapshot") or {}
    return _jsonable({
        "name": snap.get("name", ""),
        "price": snap.get("price", 0),
        "pct_change": snap.get("pct_change", 0),
        "volume": snap.get("volume", 0),
        "chip_summary": snap.get("chip_summary"),
        "fundamentals": _pick(fundamentals, [
            "has_data", "name", "latest_monthly_revenue", "latest_yoy_pct",
            "latest_eps", "valuation", "dividend_summary",
        ]),
        "technicals": _pick(technicals, [
            "has_data", "last_close", "pct_change_1d", "pct_change_5d",
            "pct_change_20d", "volume_ratio", "ma20", "ma60", "rsi14",
            "macd_hist",
        ]),
        "distribution": _pick(distribution, [
            "week_date", "large_holder_pct", "whale_holder_pct",
            "retail_holder_pct",
        ]),
        "distribution_label": snap.get("distribution_label", ""),
        "distribution_detail": snap.get("distribution_detail", ""),
        "etf_consensus": _pick(consensus, ["etf_count", "avg_weight", "max_weight", "rank"]),
        "held_by_etfs": snap.get("held_by_etfs", [])[:8],
        "llm_analysis": _pick(llm, [
            "summary", "sentiment", "sentiment_score", "confidence",
            "growth_drivers", "risks", "catalyst_outlook",
            "evidence_sources", "fetched_at",
        ]),
        "logic_check": _pick(logic, ["verdict", "reasoning", "confidence", "suggestion"]),
        "macro": _pick(macro, ["cached", "asof_date", "usdtwd", "notes"]),
        "source_files": snap.get("source_files", [])[:12],
    })


def data_quality(
    symbol: str,
    row: Mapping[str, Any],
    snap: Mapping[str, Any],
) -> Dict[str, Any]:
    fundamentals = snap.get("fundamentals") or {}
    technicals = snap.get("technicals") or {}
    flags = {
        "price": _num(row.get("最新價")) > 0 or _num(snap.get("price")) > 0,
        "fundamentals": bool(fundamentals and fundamentals.get("has_data")),
        "technicals": bool(technicals and technicals.get("has_data")),
        "chips": bool(snap.get("chip_summary")),
        "distribution": bool(snap.get("distribution")),
        "etf": bool((snap.get("consensus") or {}).get("etf_count") or snap.get("held_by_etfs")),
        "llm": bool(snap.get("llm_analysis")),
    }
    available = sum(1 for v in flags.values() if v)
    score = available / len(flags) if flags else 0.0
    missing = [label for key, label in QUALITY_FACTORS if not flags.get(key)]
    return {
        "symbol": symbol,
        "name": row.get("名稱", "") or snap.get("name", ""),
        "weight_pct": row.get("權重%", 0),
        "coverage_score": round(score, 2),
        "available_count": available,
        "total_count": len(flags),
        "missing": missing,
        **flags,
    }


def summarize_quality(rows: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    items = list(rows)
    if not items:
        return {"average_coverage_score": 0.0, "lowest_coverage": []}
    avg = sum(_num(r.get("coverage_score")) for r in items) / len(items)
    missing_counts: Dict[str, int] = {}
    for row in items:
        for missing in row.get("missing", []) or []:
            missing_counts[str(missing)] = missing_counts.get(str(missing), 0) + 1
    lowest = sorted(
        (
            {
                "symbol": r.get("symbol", ""),
                "name": r.get("name", ""),
                "coverage_score": r.get("coverage_score", 0),
                "missing": r.get("missing", []),
            }
            for r in items
        ),
        key=lambda r: _num(r.get("coverage_score")),
    )[:5]
    return {
        "average_coverage_score": round(avg, 2),
        "missing_counts": missing_counts,
        "lowest_coverage": lowest,
    }


def bundle_to_json(bundle: Mapping[str, Any], *, max_chars: int = 24000) -> str:
    text = json.dumps(_jsonable(dict(bundle)), ensure_ascii=False, indent=2)
    if len(text) > max_chars:
        return text[:max_chars] + "\n...(truncated)..."
    return text


def _pick(data: Any, keys: List[str]) -> Dict[str, Any]:
    if not isinstance(data, Mapping):
        return {}
    return {key: data.get(key) for key in keys if key in data}


def _num(value: Any) -> float:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _now_iso() -> str:
    tz = dt.timezone(dt.timedelta(hours=8))
    return dt.datetime.now(tz).isoformat(timespec="seconds")


__all__ = [
    "QUALITY_FACTORS",
    "build_portfolio_analysis_bundle",
    "bundle_to_json",
    "compact_snapshot",
    "data_quality",
    "industry_exposure",
    "portfolio_totals",
    "summarize_quality",
]
