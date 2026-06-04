"""Dashboard shared helpers and constants."""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import List, Optional

import pandas as pd
import streamlit as st

_PKG_ROOT = Path(__file__).resolve().parents[1]
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

PROJECT_ROOT = Path(__file__).resolve().parents[3]


# ======================================================================
# 共用工具
# ======================================================================


def _project_path(*parts: str) -> Path:
    return PROJECT_ROOT.joinpath(*parts)


def _safe_read_csv(path: Path) -> Optional[pd.DataFrame]:
    try:
        return pd.read_csv(path, encoding="utf-8-sig")
    except Exception:
        try:
            return pd.read_csv(path, encoding="utf-8")
        except Exception as e:
            st.error(f"無法讀取 {path.name}: {e}")
            return None


def _list_files(folder: Path, pattern: str) -> List[Path]:
    if not folder.exists():
        return []
    return sorted(folder.glob(pattern), reverse=True)


def _human_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f} 秒"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.1f} 分"
    hours = minutes / 60
    return f"{hours:.1f} 時"


def _safe_json_field(path: Path, field: str) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        value = data.get(field, "")
        return str(value or "")
    except Exception:
        return ""


def _local_file_label(path: Path) -> str:
    if not path.exists():
        return "無"
    try:
        stamp = dt.datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        return f"有 ({stamp})"
    except Exception:
        return "有"


def _ticker_local_data_summary(ticker: str) -> str:
    """Return a compact local-cache summary without fetching external sources."""
    parts: List[str] = []
    try:
        from bot.technicals import get_kline_coverage

        cov = get_kline_coverage(ticker, root=PROJECT_ROOT)
        if cov:
            parts.append(
                f"K線 {cov.get('rows', 0)} 筆 "
                f"({cov.get('earliest', '-')}~{cov.get('latest', '-')})"
            )
        else:
            parts.append("K線無本地資料")
    except Exception:
        parts.append("K線檢查失敗")

    fund_dir = _project_path("data", "fundamentals", ticker)
    fund_found = [
        label
        for label, filename in (
            ("月營收", "monthly_revenue.json"),
            ("估值", "valuation_latest.json"),
            ("股利", "dividends.json"),
            ("季報", "quarterlies_raw.json"),
        )
        if (fund_dir / filename).exists()
    ]
    parts.append("基本面 " + (" / ".join(fund_found) if fund_found else "無本地資料"))

    dist_path = _project_path("data", "chip_distribution", ticker, "history.json")
    try:
        dist_rows = json.loads(dist_path.read_text(encoding="utf-8")) if dist_path.exists() else []
        if dist_rows:
            latest = dist_rows[-1].get("week_date", "-")
            parts.append(f"TDCC {len(dist_rows)} 週 (最新 {latest})")
        else:
            parts.append("TDCC 無本地資料")
    except Exception:
        parts.append("TDCC 檢查失敗")

    llm_path = _project_path("data", "auto_llm", f"{ticker}.json")
    llm_ts = _safe_json_field(llm_path, "fetched_at")
    parts.append(f"LLM {'有 ' + llm_ts if llm_ts else _local_file_label(llm_path)}")

    macro_path = _project_path("data", "macro", f"macro_{dt.date.today().isoformat()}.json")
    parts.append(f"今日 macro {_local_file_label(macro_path)}")
    return "｜".join(parts)


def _badge(text: str, color: str = "gray") -> str:
    palette = {
        "green": "#1d9c5b",
        "red": "#d64545",
        "yellow": "#d49a17",
        "blue": "#2563eb",
        "gray": "#6b7280",
        "purple": "#7c3aed",
    }
    bg = palette.get(color, palette["gray"])
    return (
        f"<span style='background:{bg};color:#fff;padding:2px 10px;"
        f"border-radius:10px;font-size:12px;'>{text}</span>"
    )


# ----------------------------------------------------------------------
# LLM 消耗提示 (全域共用)
# ----------------------------------------------------------------------
# 規則：凡是點下去後 *會直接呼叫 Gemini API* 的按鈕，label 前加 🤖
# 並把以下常數帶到 help= 內，讓使用者懸停就看得到。
# 對可選的自動 LLM 操作 (例如「分析」/「計算評分」勾選自動 LLM)，
# 額外用 _llm_auto_banner() 在頁面頂端顯示一個明顯的提示框。

LLM_TAG = "🤖"
LLM_LABEL = "LLM"

LLM_HINT_DIRECT = (
    "🤖 此操作會直接呼叫 Gemini API，消耗 token 與 API 額度。"
    "可在『LLM 呼叫紀錄』頁追蹤每筆呼叫的 input/output/延遲/tokens。"
)

LLM_HINT_AUTO = (
    "🤖 自動 LLM：若本功能的自動 LLM 開關已開、且已設 GEMINI_API_KEY，"
    "會呼叫 Gemini (用 MOPS 重大訊息 + 鉅亨新聞做法說情緒分析)，每檔 12 小時內快取。"
    "未設 API Key 時會 graceful skip，不會收費。"
)


def _llm_button_label(label: str) -> str:
    """為 button label 加上 🤖 LLM 前綴 (若尚未含 LLM 字眼)。"""
    if LLM_TAG in label or "LLM" in label or "Gemini" in label:
        return label
    return f"{LLM_TAG} {label}"


def _llm_caption(text: str = "") -> None:
    """頁面/區塊頂部統一的『此處會呼叫 LLM』提示橫條。"""
    msg = "🤖 **LLM 消耗提示**：以下操作會呼叫 Gemini API、消耗 token (請先確認 `GEMINI_API_KEY` 已設定)。"
    if text:
        msg += f"  \n{text}"
    st.info(msg, icon="🤖")


def _llm_auto_banner(text: str = "") -> None:
    """頁面有『可選自動 LLM』功能時，用這個 banner 提醒。"""
    msg = (
        "🤖 **此頁含可選自動 LLM 行為**：只有在對應開關啟用、且 `GEMINI_API_KEY` 已設定時，"
        "才會呼叫 Gemini 做法說情緒分析 (有 12 小時快取避免重複)。"
    )
    if text:
        msg += f"  \n{text}"
    st.warning(msg, icon="🤖")
