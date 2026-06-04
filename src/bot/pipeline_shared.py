"""共用管線輔助函式（macro 摘要、JSON 解析、ETF 共識、報告序列化）。"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from bot.cloud_file_cache import restore_file_from_cloud, restore_tree_from_cloud


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
        "brief_md": report.brief_md,
        "brief_prompt_id": report.brief_prompt_id,
        "brief_prompt_version": report.brief_prompt_version,
        "duration_sec": report.duration_sec,
        "errors": report.errors,
        "output_dir": report.output_dir,
    }
