"""Ask 本地 EvidencePack adapters（只讀，不打外網）。

將 ``ticker_view.build_snapshot``、本地 macro／ETF 共識，以及
``data/news`` 日快取轉成 ``make_evidence`` 項目，供 ``AskEngine`` 引用。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from bot.evidence import make_evidence
from bot.utils import get_logger


def _observed(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    text = str(value or "").strip()
    return text or fallback


def collect_snapshot_resource_evidence(
    ticker: str,
    *,
    root: Path,
    resource_needs: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """依 QueryPlan 資源需求，從本地 snapshot 組 evidence。

    強制 ``auto_fill_missing=False`` / ``auto_llm=False`` / ``macro_cache_only=True``，
    避免在 ``!ask`` 路徑偷偷觸發外部 API 或 Gemini。
    """
    needs: Set[str] = set(resource_needs or [])
    wanted = needs.intersection({
        "technical", "chips", "etf", "macro", "ticker_snapshot", "fundamental",
        "news",
    })
    # 沒有明確資源時仍可提供輕量 snapshot 摘要（價格）
    if not wanted and not needs:
        wanted = {"ticker_snapshot"}
    if not wanted and needs.isdisjoint({
        "technical", "chips", "etf", "macro", "ticker_snapshot", "fundamental",
        "news", "web_search",
    }):
        return []

    log = get_logger("ask-adapters")
    items: List[Dict[str, Any]] = []
    snap_needs = wanted - {"news"}

    if snap_needs or (not needs and "ticker_snapshot" in wanted):
        try:
            from bot.ticker_view import build_snapshot
            from bot.technicals import snapshot_to_dict as tech_to_dict
            from bot.fundamentals_fetcher import snapshot_to_dict as fund_to_dict
        except Exception:
            log.exception("[%s] 無法 import snapshot 模組", ticker)
            tech_to_dict = None  # type: ignore
            fund_to_dict = None  # type: ignore
            snap = None
        else:
            try:
                snap = build_snapshot(
                    ticker,
                    Path(root),
                    auto_fill_missing=False,
                    auto_llm=False,
                    refresh_chips=False,
                    refresh_fundamentals=False,
                    refresh_technicals=False,
                    refresh_distribution=False,
                    refresh_macro=False,
                    macro_cache_only=True,
                )
            except Exception:
                log.exception("[%s] build_snapshot 失敗", ticker)
                snap = None

        if snap is not None:
            fetched = _observed(getattr(snap, "fetched_at", ""), "")

            if "ticker_snapshot" in wanted or "technical" in needs:
                if snap.price > 0 or (snap.technicals and snap.technicals.has_data):
                    last_date = ""
                    if snap.technicals and snap.technicals.has_data:
                        last_date = _observed(
                            getattr(snap.technicals, "last_date", ""), fetched,
                        )
                    items.append(make_evidence(
                        "ticker_snapshot",
                        source="ticker_view",
                        source_ref=f"{ticker}:snap:{last_date or fetched}",
                        title=f"{ticker} 本地快照",
                        observed_at=last_date or fetched,
                        excerpt=(
                            f"{ticker} {snap.name or ''} 現價 {snap.price:g}，"
                            f"漲跌 {snap.pct_change:+.2f}%"
                        ).strip(),
                        quality_status="ok",
                    ))

            if "technical" in needs and snap.technicals and snap.technicals.has_data and tech_to_dict:
                tech = tech_to_dict(snap.technicals)
                last_date = _observed(tech.get("last_date"), fetched)
                signals = tech.get("signals") or []
                signal_txt = "；".join(
                    f"{s.get('label', '')}:{s.get('detail', '')}"
                    for s in signals[:4]
                    if isinstance(s, dict)
                )
                items.append(make_evidence(
                    "technical",
                    source="technicals_cache",
                    source_ref=f"{ticker}:tech:{last_date}",
                    title=f"{ticker} 技術面 {last_date}",
                    observed_at=last_date,
                    excerpt=(
                        f"收 {tech.get('last_close')}，1日 {tech.get('pct_change_1d')}%，"
                        f"5日 {tech.get('pct_change_5d')}%，20日 {tech.get('pct_change_20d')}%，"
                        f"60日 {tech.get('pct_change_60d')}%；"
                        f"RSI14={tech.get('rsi14')} MACD={tech.get('macd')} "
                        f"MA5={tech.get('ma5')} MA20={tech.get('ma20')} MA60={tech.get('ma60')}；"
                        f"technical_score={tech.get('technical_score')}。"
                        f"{signal_txt}"
                    )[:2000],
                    quality_status="ok",
                ))

            if "chips" in needs and snap.chip_summary:
                chips = snap.chip_summary
                rows = chips.get("rows") or []
                has_net = any(
                    abs(float(chips.get(k, 0) or 0)) > 0
                    for k in (
                        "foreign_net", "investment_trust_net", "dealer_net",
                        "block_trade_net",
                    )
                )
                if rows or has_net:
                    items.append(make_evidence(
                        "chips",
                        source="chips_cache",
                        source_ref=f"{ticker}:chips:{chips.get('days', 5)}",
                        title=f"{ticker} 近 {chips.get('days', 5)} 日籌碼",
                        observed_at=fetched,
                        excerpt=(
                            f"外資淨買超 {float(chips.get('foreign_net', 0) or 0):,.0f}，"
                            f"投信 {float(chips.get('investment_trust_net', 0) or 0):,.0f}，"
                            f"自營商 {float(chips.get('dealer_net', 0) or 0):,.0f}；"
                            f"融資變動 {float(chips.get('margin_buy_change_pct', 0) or 0):+.2f}%，"
                            f"借券變動 {float(chips.get('short_borrow_change_pct', 0) or 0):+.2f}%"
                        ),
                        quality_status="ok",
                    ))

            if "etf" in needs:
                parts: List[str] = []
                if snap.consensus:
                    c = snap.consensus
                    parts.append(
                        f"被 {c.get('etf_count', 0)} 檔主動 ETF 持有，"
                        f"合計權重≈{float(c.get('total_weight', 0) or 0):.2f}%"
                    )
                if snap.new_build_signal:
                    nb = snap.new_build_signal
                    parts.append(
                        f"新建倉訊號：{nb.get('etf_count', '?')} 檔 — {nb.get('note', '')}"
                    )
                if snap.add_signal:
                    ad = snap.add_signal
                    parts.append(
                        f"加碼訊號：{ad.get('etf_count', '?')} 檔 — {ad.get('note', '')}"
                    )
                if snap.held_by_etfs:
                    held = ", ".join(
                        f"{h.get('etf_symbol', '')}"
                        f"({float(h.get('weight_pct', 0) or 0):.2f}%)"
                        for h in snap.held_by_etfs[:6]
                    )
                    parts.append(f"持有清單：{held}")
                if parts:
                    items.append(make_evidence(
                        "etf",
                        source="etf_consensus_local",
                        source_ref=f"{ticker}:etf_consensus",
                        title=f"{ticker} 主動 ETF 共識",
                        observed_at=fetched,
                        excerpt="；".join(parts),
                        quality_status="ok",
                    ))

            if "macro" in needs and snap.macro_snapshot:
                macro = snap.macro_snapshot
                keys = [
                    "asof_date", "usdtwd", "sox", "nasdaq", "spx", "vix",
                    "taiex", "tx_basis", "notes",
                ]
                bits = []
                for k in keys:
                    if k in macro and macro[k] not in (None, "", [], {}):
                        bits.append(f"{k}={macro[k]}")
                if not bits:
                    bits.append(json.dumps(macro, ensure_ascii=False)[:800])
                items.append(make_evidence(
                    "macro",
                    source="macro_cache",
                    source_ref=f"macro:{macro.get('asof_date') or fetched}",
                    title="跨市場 macro 快取",
                    observed_at=_observed(macro.get("asof_date"), fetched),
                    excerpt="；".join(str(b) for b in bits)[:2000],
                    quality_status="ok",
                ))

            if "fundamental" in needs and snap.fundamentals and snap.fundamentals.has_data and fund_to_dict:
                fund = fund_to_dict(snap.fundamentals)
                derived = fund.get("derived") or {}
                val = fund.get("valuation") or {}
                rev = derived.get("latest_revenue") or {}
                items.append(make_evidence(
                    "fundamental",
                    source="fundamentals_cache",
                    source_ref=f"{ticker}:fund:{fund.get('fetched_at') or fetched}",
                    title=f"{ticker} 基本面快取",
                    observed_at=_observed(fund.get("fetched_at"), fetched),
                    excerpt=(
                        f"PER={val.get('pe_ratio')} PBR={val.get('pb_ratio')} "
                        f"殖利率={val.get('dividend_yield')}；"
                        f"最新月營收 YoY={rev.get('yoy')} MoM={rev.get('mom')}；"
                        f"YoY連增月數={derived.get('revenue_yoy_streak')}"
                    ),
                    quality_status="ok",
                ))

    if "news" in needs:
        items.extend(_collect_cached_news_evidence(ticker, root=Path(root)))

    return items


def _collect_cached_news_evidence(
    ticker: str,
    *,
    root: Path,
    limit: int = 8,
) -> List[Dict[str, Any]]:
    """只讀 data/news 日快取，不打外網。"""
    try:
        from bot.news_fetcher import load_cached_news_only
    except Exception:
        return []
    try:
        news = load_cached_news_only(root=root, limit=200)
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for n in news:
        hit = ticker in (n.related_tickers or [])
        blob = f"{n.title or ''} {n.summary or ''}"
        if not hit and ticker in blob:
            hit = True
        if not hit:
            continue
        out.append(make_evidence(
            "news",
            source=n.source or "cnyes_cache",
            source_ref=n.news_id or n.url or n.title,
            title=n.title or f"{ticker} 新聞",
            url=n.url or "",
            observed_at=n.published_at or "",
            excerpt=((n.summary or n.title) or "")[:2000],
            quality_status="ok",
        ))
        if len(out) >= limit:
            break
    return out


__all__ = [
    "collect_snapshot_resource_evidence",
]
