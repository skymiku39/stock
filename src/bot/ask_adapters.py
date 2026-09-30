"""Ask 本地 EvidencePack adapters（只讀，不打外網）。

將 ``ticker_view.build_snapshot``、本地 macro／ETF 共識，以及
``data/news`` 日快取轉成 ``make_evidence`` 項目，供 ``AskEngine`` 引用。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from bot.evidence import make_evidence as _make_evidence
from bot.market_macro import macro_quality_status
from bot.source_registry import is_source_document_replayable
from bot.utils import get_logger


def make_evidence(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """cache adapter 不猜來源；已有 exact document ID 時保留其 provenance。"""
    has_exact_document = str(kwargs.get("source_document_id") or "").startswith("source-doc:")
    if not has_exact_document:
        kwargs.setdefault("provenance_status", "cache_only")
    metadata = dict(kwargs.get("metadata") or {})
    if not has_exact_document:
        metadata.setdefault("cache_only", True)
        metadata.setdefault("provenance_status", "cache_only")
    kwargs["metadata"] = metadata
    return _make_evidence(*args, **kwargs)


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


def _replayable_source_document_ids(
    root: Path, candidates: Any,
) -> List[str]:
    """保留仍能由本地 raw archive 回放的 exact source IDs。"""
    if not isinstance(candidates, (list, tuple, set)):
        candidates = [candidates]
    try:
        from bot.stock_db import StockDB

        db = StockDB.open(root=root)
        try:
            out: List[str] = []
            for candidate in candidates:
                source_id = str(candidate or "").strip()
                if not source_id.startswith("source-doc:"):
                    continue
                source = db.get_source_document(source_id)
                if is_source_document_replayable(root, source):
                    out.append(source_id)
            return list(dict.fromkeys(out))
        finally:
            db.close()
    except Exception:
        return []


def _etf_holdings_evidence_context(
    ticker: str, root: Path,
) -> tuple[List[str], Dict[str, str]]:
    """回傳 (replayable source IDs, holdings_dates)。

    ``holdings_dates`` 一律來自本地 CSV 快照日期，不臆造來源 ID。
    只有當「每一個持有該 ticker 的最新 ETF 快照」都能對到可回放的 exact
    ``etf_holdings_*`` document 時，才回傳 source IDs（供 composite_exact）。
    """
    try:
        from bot.active_etf import list_holdings_dates, load_active_etfs, load_holdings
        from bot.source_registry import SourceRegistry

        holdings_dates: Dict[str, str] = {}
        candidates: List[tuple[str, str]] = []
        for etf in load_active_etfs(root):
            dates = list_holdings_dates(etf.symbol, root)
            if not dates:
                continue
            snapshot = load_holdings(etf.symbol, dates[0], root)
            if snapshot and ticker in snapshot.by_ticker():
                date_text = dates[0].isoformat()
                holdings_dates[etf.symbol] = date_text
                candidates.append((etf.symbol, date_text))
        if not candidates:
            return [], holdings_dates
        registry = SourceRegistry(root)
        try:
            wanted = {f"{symbol}:{date}" for symbol, date in candidates}
            rows = registry.db.list_source_documents(limit=5000)
            matching = list(dict.fromkeys(
                row.document_id for row in rows
                if row.source.startswith("etf_holdings_")
                and row.source_ref in wanted
                and is_source_document_replayable(root, row)
            ))
            replayed_refs = {
                row.source_ref for row in rows
                if row.source.startswith("etf_holdings_")
                and row.source_ref in wanted
                and is_source_document_replayable(root, row)
            }
            source_ids = matching if replayed_refs == wanted else []
            return source_ids, holdings_dates
        finally:
            registry.close()
    except Exception:
        return [], {}


def _etf_source_document_ids(ticker: str, root: Path) -> List[str]:
    """找出明確包含 ticker 的 ETF holdings raw documents。

    ETF 共識是多份持股快照的衍生結果，因此只回傳 exact manifest IDs，
    不把 CSV 路徑或 holdings URL 當成 source_document_id。
    """
    source_ids, _holdings_dates = _etf_holdings_evidence_context(ticker, root)
    return source_ids


def _fundamental_source_document_ids(
    ticker: str,
    root: Path,
    *,
    revenue_period: str,
    valuation_date: str,
) -> List[str]:
    """回傳可證明支撐基本面總摘要的 exact source IDs。

    總摘要的數值來自快取 snapshot，但它只在摘要的營收期間／估值日能和
    canonical SQLite rows 對上，且兩列都能回到現存 source_documents 時，
    才升級為 composite_exact。這裡不依 URL、檔名或相似內容猜來源。
    """
    if not revenue_period or not valuation_date:
        return []
    try:
        from bot.stock_db import StockDB

        db = StockDB.open(root=root)
        try:
            revenue = next(
                (
                    row for row in db.get_monthly_revenue(ticker, revenue_period)
                    if row.quality_status in {"ok", "partial"}
                    and str(row.year_month) == revenue_period
                    and str(row.source_record_id or "").startswith("source-doc:")
                    and (
                        (document := db.get_source_document(str(row.source_record_id)))
                        is not None
                    )
                    and is_source_document_replayable(root, document)
                ),
                None,
            )
            valuation = next(
                (
                    row for row in db.get_valuation(ticker, valuation_date)
                    if row.quality_status in {"ok", "partial"}
                    and str(row.as_of) == valuation_date
                    and str(row.source_record_id or "").startswith("source-doc:")
                    and (
                        (document := db.get_source_document(str(row.source_record_id)))
                        is not None
                    )
                    and is_source_document_replayable(root, document)
                ),
                None,
            )
            if revenue is None or valuation is None:
                return []
            return [
                str(revenue.source_record_id),
                str(valuation.source_record_id),
            ]
        finally:
            db.close()
    except Exception:
        return []


def _price_source_document_ids(
    ticker: str,
    root: Path,
    *,
    last_date: str,
    limit: int,
) -> List[str]:
    """確認價格／技術摘要所用 K 線是否全部可回放。"""
    try:
        from bot.stock_db import StockDB

        db = StockDB.open(root=root)
        try:
            rows = db.get_price_history(
                ticker, limit=max(1, int(limit)), ascending=False,
            )
            if last_date:
                rows = [row for row in rows if str(row.date) <= str(last_date)]
            if not rows:
                return []
            source_ids: List[str] = []
            for row in rows:
                source_id = str(row.source_record_id or "")
                if (
                    row.quality_status not in {"ok", "partial"}
                    # yfinance normalized raw is replayable, but it is not an
                    # official TWSE/TPEx response.  Do not let its manifest
                    # ID upgrade a technical/ticker summary to composite_exact.
                    or str(row.source or "").strip().lower() == "yfinance"
                    or not source_id.startswith("source-doc:")
                    or not is_source_document_replayable(
                        root, db.get_source_document(source_id)
                    )
                ):
                    return []
                source_ids.append(source_id)
            return list(dict.fromkeys(source_ids))
        finally:
            db.close()
    except Exception:
        return []


def _local_taifex_evidence(root: Path, *, limit: int = 1) -> List[Dict[str, Any]]:
    """回傳已完成 exact TAIFEX research slice 的最新證據。

    只接受 evidence registry 已保存且仍指向 source document 的資料；不依
    source URL、標題或日期相似度補造 provenance。
    """
    try:
        from bot.stock_db import StockDB

        db = StockDB.open(root=root)
        try:
            rows = db.search_evidence("", kind="taifex_futures", limit=max(1, int(limit)))
            out: List[Dict[str, Any]] = []
            for row in rows:
                source_id = str(row.source_document_id or "")
                if not source_id.startswith("source-doc:"):
                    continue
                if not is_source_document_replayable(
                    root, db.get_source_document(source_id)
                ):
                    continue
                try:
                    metadata = json.loads(row.metadata_json or "{}")
                except (TypeError, ValueError):
                    metadata = {}
                if not isinstance(metadata, dict):
                    metadata = {}
                metadata["provenance_status"] = "exact"
                out.append(make_evidence(
                    "taifex_futures",
                    source=row.source,
                    source_ref=row.source_ref,
                    title=row.title,
                    url=row.url,
                    observed_at=row.observed_at,
                    as_of=row.as_of,
                    published_at=row.published_at,
                    retrieved_at=row.retrieved_at,
                    valid_from=row.valid_from,
                    valid_to=row.valid_to,
                    excerpt=row.excerpt,
                    quality_status=row.quality_status,
                    source_document_id=source_id,
                    provenance_status="exact",
                    metadata=metadata,
                ))
            return out
        finally:
            db.close()
    except Exception:
        return []


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
        "technical", "chips", "etf", "taifex", "macro", "ticker_snapshot", "fundamental",
        "news",
    })
    # 沒有明確資源時仍可提供輕量 snapshot 摘要（價格）
    if not wanted and not needs:
        wanted = {"ticker_snapshot"}
    if not wanted and needs.isdisjoint({
        "technical", "chips", "etf", "taifex", "macro", "ticker_snapshot", "fundamental",
        "news", "web_search",
    }):
        return []

    log = get_logger("ask-adapters")
    items: List[Dict[str, Any]] = []
    # TAIFEX evidence 有自己的 exact registry 路徑；只問 TAIFEX 時不必
    # 另外載入 ticker snapshot／macro 快取。若同時要求 macro，仍會進入
    # snapshot 分支並保留兩種證據。
    snap_needs = wanted - {"news", "taifex"}

    if "taifex" in needs:
        items.extend(_local_taifex_evidence(Path(root), limit=1))

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
                    allow_live_sources=False,
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
                    snapshot_source_ids = _price_source_document_ids(
                        ticker, Path(root), last_date=last_date, limit=1,
                    )
                    items.append(make_evidence(
                        "ticker_snapshot",
                        source="ticker_view",
                        source_ref=f"{ticker}:snap:{last_date or fetched}",
                        title=f"{ticker} 本地快照",
                        observed_at=last_date or fetched,
                        as_of=last_date,
                        retrieved_at=fetched,
                        excerpt=(
                            f"{ticker} {snap.name or ''} 現價 {snap.price:g}，"
                            f"漲跌 {snap.pct_change:+.2f}%"
                        ).strip(),
                        quality_status="ok",
                        provenance_status=(
                            "composite_exact" if snapshot_source_ids else "cache_only"
                        ),
                        metadata={
                            "cache_only": not bool(snapshot_source_ids),
                            "source_document_ids": snapshot_source_ids,
                            "component": "latest_price_bar",
                        },
                    ))

            if "technical" in needs and snap.technicals and snap.technicals.has_data and tech_to_dict:
                tech = tech_to_dict(snap.technicals)
                last_date = _observed(tech.get("last_date"), fetched)
                technical_source_ids = _price_source_document_ids(
                    ticker, Path(root), last_date=last_date, limit=150,
                )
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
                    as_of=last_date,
                    retrieved_at=fetched,
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
                    provenance_status=(
                        "composite_exact" if technical_source_ids else "cache_only"
                    ),
                    metadata={
                        "cache_only": not bool(technical_source_ids),
                        "source_document_ids": technical_source_ids,
                        "component": "price_history_derived_indicators",
                    },
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
                    raw_chip_source_ids = list(chips.get("source_document_ids") or [])
                    chip_source_ids = _replayable_source_document_ids(
                        Path(root), raw_chip_source_ids
                    )
                    if raw_chip_source_ids and len(chip_source_ids) != len(
                        set(str(value).strip() for value in raw_chip_source_ids if str(value).strip())
                    ):
                        chip_source_ids = []
                    items.append(make_evidence(
                        "chips",
                        source="chips_cache",
                        source_ref=f"{ticker}:chips:{chips.get('days', 5)}",
                        title=f"{ticker} 近 {chips.get('days', 5)} 日籌碼",
                        observed_at=_observed(chips.get("end_date"), fetched),
                        as_of=_observed(chips.get("end_date") or chips.get("date"), ""),
                        retrieved_at=fetched,
                        excerpt=(
                            f"外資淨買超 {float(chips.get('foreign_net', 0) or 0):,.0f}，"
                            f"投信 {float(chips.get('investment_trust_net', 0) or 0):,.0f}，"
                            f"自營商 {float(chips.get('dealer_net', 0) or 0):,.0f}；"
                            f"融資變動 {float(chips.get('margin_buy_change_pct', 0) or 0):+.2f}%，"
                            f"借券變動 {float(chips.get('short_borrow_change_pct', 0) or 0):+.2f}%"
                        ),
                        quality_status="ok",
                        provenance_status=(
                            "composite_exact"
                            if chip_source_ids else "cache_only"
                        ),
                        metadata={
                            "cache_only": not bool(chip_source_ids),
                            "source_document_ids": chip_source_ids,
                            "temporal_status": str(
                                chips.get("source_date_status") or "unknown"
                            ),
                        },
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
                    etf_source_ids, holdings_dates = _etf_holdings_evidence_context(
                        ticker, Path(root),
                    )
                    as_of_dates = sorted({str(value) for value in holdings_dates.values() if value})
                    holdings_as_of = as_of_dates[-1] if as_of_dates else ""
                    temporal_status = (
                        "aligned"
                        if len(as_of_dates) <= 1
                        else "mixed"
                    )
                    items.append(make_evidence(
                        "etf",
                        source="etf_consensus_local",
                        source_ref=f"{ticker}:etf_consensus",
                        title=f"{ticker} 主動 ETF 共識",
                        observed_at=_observed(holdings_as_of, fetched),
                        as_of=holdings_as_of,
                        retrieved_at=fetched,
                        excerpt="；".join(parts),
                        quality_status="ok",
                        provenance_status=(
                            "composite_exact" if etf_source_ids else "cache_only"
                        ),
                        metadata={
                            "cache_only": not bool(etf_source_ids),
                            "source_document_ids": etf_source_ids,
                            "holdings_dates": dict(holdings_dates),
                            "temporal_status": temporal_status,
                        },
                    ))

            if "macro" in needs and snap.macro_snapshot:
                macro = snap.macro_snapshot
                from bot.macro_research import macro_evidence_excerpt

                macro_source_ids = _replayable_source_document_ids(
                    Path(root), macro.get("source_document_id") or ""
                )
                items.append(make_evidence(
                    "macro",
                    source="macro_cache",
                    source_ref=f"macro:{macro.get('asof_date') or fetched}",
                    title="跨市場 macro 快取",
                    observed_at=_observed(macro.get("asof_date"), fetched),
                    as_of=_observed(macro.get("asof_date"), ""),
                    retrieved_at=fetched,
                    excerpt=macro_evidence_excerpt(macro),
                    quality_status=macro_quality_status(macro),
                    source_document_id=(macro_source_ids[0] if macro_source_ids else ""),
                    metadata={
                        "pipeline": "market_macro_snapshot_slice",
                        "temporal_status": str(
                            macro.get("source_date_status") or "unknown"
                        ),
                        "source_dates": dict(macro.get("source_dates") or {}),
                        "quality_status": macro_quality_status(macro),
                    },
                ))

            if "fundamental" in needs and snap.fundamentals and snap.fundamentals.has_data and fund_to_dict:
                fund = fund_to_dict(snap.fundamentals)
                derived = fund.get("derived") or {}
                val = fund.get("valuation") or {}
                rev = derived.get("latest_revenue") or {}
                rev_period = ""
                if rev.get("year") and rev.get("month"):
                    rev_period = f"{int(rev['year']):04d}-{int(rev['month']):02d}"
                val_date = str(val.get("date") or "")
                observed = rev_period or val_date or _observed(fund.get("fetched_at"), fetched)
                cache_dir = Path(root) / "data" / "fundamentals" / ticker
                cache_paths = [
                    f"data/fundamentals/{ticker}/{name}"
                    for name in (
                        "monthly_revenue.json", "valuation_latest.json",
                        "dividends.json", "quarterlies_raw.json", "snapshot.json",
                    )
                    if (cache_dir / name).exists()
                ]
                for name in (
                    "monthly_revenue_all.json", "valuation_all.json",
                    "dividends_all.json", "quarterly_financials_all.json",
                ):
                    if (Path(root) / "data" / "fundamentals" / name).exists():
                        cache_paths.append(f"data/fundamentals/{name}")
                fundamental_source_ids = _fundamental_source_document_ids(
                    ticker,
                    Path(root),
                    revenue_period=rev_period,
                    valuation_date=val_date,
                )
                items.append(make_evidence(
                    "fundamental",
                    source="fundamentals_cache",
                    source_ref=f"{ticker}:fund:{observed or fund.get('fetched_at') or fetched}",
                    title=f"{ticker} 基本面快取",
                    observed_at=observed,
                    as_of=observed,
                    retrieved_at=_observed(fund.get("fetched_at"), fetched),
                    excerpt=(
                        f"估值日期={val_date or '未知'} PER={val.get('pe_ratio')} "
                        f"PBR={val.get('pb_ratio')} 殖利率={val.get('dividend_yield')}；"
                        f"最新月營收期間={rev_period or '未知'} "
                        f"金額={rev.get('revenue')} 千元，"
                        f"YoY={rev.get('yoy')}% MoM={rev.get('mom')}%，"
                        f"累計營收={rev.get('cum_revenue')} 千元，"
                        f"累計YoY={rev.get('cum_yoy')}%，"
                        f"YoY連增月數={derived.get('revenue_yoy_streak')}"
                    ),
                    quality_status="ok",
                    provenance_status=(
                        "composite_exact" if fundamental_source_ids else "cache_only"
                    ),
                    metadata={
                        "cache_only": not bool(fundamental_source_ids),
                        "cache_paths": cache_paths,
                        "source_document_ids": fundamental_source_ids,
                        "components": ["monthly_revenue", "valuation_daily"],
                        "source_dates": {
                            "revenue_period": rev_period,
                            "valuation_date": val_date,
                        },
                    },
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
    db = None
    try:
        from bot.stock_db import StockDB

        db = StockDB.open(root=root)
    except Exception:
        db = None
    try:
        for n in news:
            hit = ticker in (n.related_tickers or [])
            blob = f"{n.title or ''} {n.summary or ''}"
            if not hit and ticker in blob:
                hit = True
            if not hit:
                continue

            existing = None
            source_document_id = (
                _replayable_source_document_ids(
                    root, getattr(n, "source_document_id", "") or ""
                )
                or [""]
            )[0]
            if db is not None and source_document_id:
                for row in db.list_evidence(source=n.source or "cnyes", limit=500):
                    if (
                        row.kind == "news"
                        and row.source_document_id == source_document_id
                        and row.source_ref == (n.news_id or n.url or n.title)
                    ):
                        existing = row
                        break
            if existing is not None:
                try:
                    metadata = json.loads(existing.metadata_json or "{}")
                except (TypeError, ValueError):
                    metadata = {}
                if not isinstance(metadata, dict):
                    metadata = {}
                out.append({
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
                    "provenance_status": str(
                        metadata.get("provenance_status") or "exact"
                    ),
                    "metadata": metadata,
                })
            else:
                out.append(make_evidence(
                    "news",
                    source=n.source or "cnyes_cache",
                    source_ref=n.news_id or n.url or n.title,
                    title=n.title or f"{ticker} 新聞",
                    url=n.url or "",
                    observed_at=n.published_at or "",
                    published_at=n.published_at or "",
                    excerpt=((n.summary or n.title) or "")[:2000],
                    quality_status="ok",
                    source_document_id=source_document_id,
                ))
            if len(out) >= limit:
                break
    finally:
        if db is not None:
            db.close()
    return out


__all__ = [
    "collect_snapshot_resource_evidence",
]
