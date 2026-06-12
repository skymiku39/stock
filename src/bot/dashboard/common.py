"""Dashboard shared helpers and constants."""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, List, Optional, Tuple

import pandas as pd
import streamlit as st

_PKG_ROOT = Path(__file__).resolve().parents[1]
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DASHBOARD_UI_BUILD = "movers-cache-v18"


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


def _altair_is_composite(chart: Any) -> bool:
    """判斷 Altair 圖是否為 layer/vconcat 等複合結構。

    Streamlit 1.42+ 在 use_container_width=True 時，複合圖常靜默空白。
    """
    try:
        spec = chart.to_dict() if hasattr(chart, "to_dict") else {}
    except Exception:
        return True
    return any(k in spec for k in ("layer", "vconcat", "hconcat", "concat"))


def _inline_vega_datasets(spec: dict) -> dict:
    """將 Altair 6 ``datasets`` 引用展開為 inline ``values``。"""
    import copy

    datasets = spec.get("datasets")
    if not datasets:
        return spec
    out = copy.deepcopy(spec)
    ds = out.pop("datasets", {}) or {}

    def _resolve(data_obj: Any) -> Any:
        if not isinstance(data_obj, dict):
            return data_obj
        name = data_obj.get("name")
        if name and name in ds:
            return {"values": ds[name]}
        return data_obj

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            if "data" in node:
                node["data"] = _resolve(node["data"])
            for val in node.values():
                _walk(val)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(out)
    return out


def _propagate_layer_data(spec: dict) -> dict:
    """將頂層 inline data 複製到缺少 data 的 layer（Streamlit 繼承有時失效）。"""
    top = spec.get("data")
    if not isinstance(top, dict) or "values" not in top:
        return spec
    for layer in spec.get("layer", []):
        if isinstance(layer, dict) and "data" not in layer:
            layer["data"] = top
    return spec


_VEGA_LITE_V5_SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json"


def _prepare_vega_spec_for_streamlit(spec: dict) -> dict:
    """內嵌 datasets、補齊 layer data、降級 schema 供 Streamlit 穩定渲染。"""
    out = _propagate_layer_data(_inline_vega_datasets(spec))
    schema = str(out.get("$schema", ""))
    if "v6" in schema or "v6." in schema:
        out["$schema"] = _VEGA_LITE_V5_SCHEMA
    return out


def _dashboard_debug_enabled() -> bool:
    import os as _os
    return _os.environ.get("DASHBOARD_DEBUG", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _debug_log_session(
    hypothesis_id: str,
    location: str,
    message: str,
    data: dict,
) -> None:
    # #region agent log
    try:
        import os as _os
        import time as _time

        if _os.environ.get("PYTEST_CURRENT_TEST"):
            return
        if not _dashboard_debug_enabled():
            return
        payload = {
            "sessionId": "b18956",
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": {**data, "ui_build": DASHBOARD_UI_BUILD},
            "timestamp": int(_time.time() * 1000),
        }
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        for log_path in (
            PROJECT_ROOT / "debug-b18956.log",
            PROJECT_ROOT / "log" / "debug-b18956.log",
        ):
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.open("a", encoding="utf-8").write(line)
            except Exception:
                pass
        try:
            import urllib.request

            urllib.request.urlopen(
                urllib.request.Request(
                    "http://127.0.0.1:7317/ingest/7f514bf2-c058-4294-88d9-bbf41df354c0",
                    data=line.encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "X-Debug-Session-Id": "b18956",
                    },
                    method="POST",
                ),
                timeout=0.5,
            )
        except Exception:
            pass
        try:
            from bot.utils import get_logger

            get_logger("dashboard-debug").info(
                "%s %s %s",
                hypothesis_id,
                message,
                json.dumps(data, ensure_ascii=False),
            )
        except Exception:
            pass
    except Exception:
        pass
    # #endregion


def _debug_log_candlestick(location: str, data: dict) -> None:
    _debug_log_session("H12", location, "candlestick svg render", data)


def write_dashboard_heartbeat(page: str, extra: Optional[dict] = None) -> None:
    """寫入執行心跳（供確認瀏覽器是否跑此專案程式）。"""
    # #region agent log
    try:
        import os as _os
        import time as _time

        if _os.environ.get("PYTEST_CURRENT_TEST"):
            return
        if not _dashboard_debug_enabled():
            return
        payload = {
            "ui_build": DASHBOARD_UI_BUILD,
            "page": page,
            "module": str(Path(__file__).resolve()),
            "timestamp": _time.time(),
            **(extra or {}),
        }
        hb = PROJECT_ROOT / "data" / "dashboard_heartbeat.json"
        hb.parent.mkdir(parents=True, exist_ok=True)
        hb.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        _debug_log_session("H11", "common.py:write_dashboard_heartbeat", "heartbeat", payload)
    except Exception:
        pass
    # #endregion


def _display_close_line_chart(
    bars: List[dict],
    *,
    height: int = 90,
    log_key: str = "",
) -> None:
    """原生收盤價折線（Streamlit 內建，欄位內穩定顯示）。"""
    if not bars:
        return
    plot = pd.DataFrame(bars)
    if "date" in plot.columns:
        plot["date"] = pd.to_datetime(plot["date"], errors="coerce")
        st.line_chart(plot, x="date", y="close", height=height)
    else:
        st.line_chart(plot[["close"]], height=height)
    _debug_log_session(
        "H25",
        "common.py:_display_close_line_chart",
        "native line chart",
        {"bars": len(bars), "key": log_key},
    )


def _display_ohlc_line_chart(
    df: pd.DataFrame,
    *,
    height: int = 200,
    log_key: str = "",
    ma_periods: Tuple[int, ...] = (5, 20, 60),
) -> None:
    """K 線工作台用：收盤 + 均線原生折線（保證可見）。"""
    if df is None or df.empty or "close" not in df.columns:
        return
    plot = df.copy()
    if "date" in plot.columns:
        plot["date"] = pd.to_datetime(plot["date"], errors="coerce")
    y_cols = ["close"]
    for p in ma_periods:
        col = f"ma{p}"
        if col in plot.columns:
            y_cols.append(col)
    if "date" in plot.columns:
        st.line_chart(plot, x="date", y=y_cols, height=height)
    else:
        st.line_chart(plot[y_cols], height=height)
    _debug_log_session(
        "H25",
        "common.py:_display_ohlc_line_chart",
        "ohlc native line chart",
        {"rows": len(plot), "series": y_cols, "key": log_key},
    )


def _build_candlestick_svg(
    bars: List[dict],
    *,
    width: int = 320,
    height: int = 110,
) -> str:
    """以 inline SVG 繪製蠟燭圖（無 Altair / Plotly 依賴）。"""
    if not bars:
        return ""
    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]
    pmin, pmax = min(lows), max(highs)
    if pmax <= pmin:
        pmax = pmin + 1.0
    n = len(bars)
    pad = 4
    plot_w = max(1, width - 2 * pad)
    plot_h = max(1, height - 2 * pad)
    slot = plot_w / max(n, 1)
    body_w = max(2.0, min(10.0, slot * 0.65))

    def y_px(price: float) -> float:
        return pad + plot_h * (1.0 - (price - pmin) / (pmax - pmin))

    parts = [
        f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">',
        '<rect width="100%" height="100%" fill="#fafafa"/>',
    ]
    for i, b in enumerate(bars):
        o, h, l, c = float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])
        cx = pad + slot * i + slot / 2.0
        color = "#d64545" if c >= o else "#1d9c5b"
        y_hi, y_lo = y_px(h), y_px(l)
        parts.append(
            f'<line x1="{cx:.1f}" y1="{y_hi:.1f}" x2="{cx:.1f}" y2="{y_lo:.1f}" '
            f'stroke="{color}" stroke-width="1"/>'
        )
        y_open, y_close = y_px(o), y_px(c)
        top = min(y_open, y_close)
        bh = max(abs(y_close - y_open), 1.0)
        parts.append(
            f'<rect x="{cx - body_w / 2:.1f}" y="{top:.1f}" width="{body_w:.1f}" '
            f'height="{bh:.1f}" fill="{color}"/>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _build_candlestick_image(
    bars: List[dict],
    *,
    width: int = 320,
    height: int = 110,
):
    """以 Pillow 繪製蠟燭圖 PNG（Streamlit ``st.image`` 穩定顯示）。"""
    from PIL import Image, ImageDraw

    if not bars:
        return None
    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]
    pmin, pmax = min(lows), max(highs)
    if pmax <= pmin:
        pmax = pmin + 1.0
    n = len(bars)
    pad = 4
    plot_w = max(1, width - 2 * pad)
    plot_h = max(1, height - 2 * pad)
    slot = plot_w / max(n, 1)
    body_w = max(2.0, min(10.0, slot * 0.65))

    def y_px(price: float) -> float:
        return pad + plot_h * (1.0 - (price - pmin) / (pmax - pmin))

    img = Image.new("RGB", (width, height), "#fafafa")
    draw = ImageDraw.Draw(img)
    for i, b in enumerate(bars):
        o, h, l, c = float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])
        cx = pad + slot * i + slot / 2.0
        color = "#d64545" if c >= o else "#1d9c5b"
        y_hi, y_lo = y_px(h), y_px(l)
        draw.line((cx, y_hi, cx, y_lo), fill=color, width=1)
        y_open, y_close = y_px(o), y_px(c)
        top = min(y_open, y_close)
        bh = max(abs(y_close - y_open), 1.0)
        draw.rectangle(
            (cx - body_w / 2.0, top, cx + body_w / 2.0, top + bh),
            fill=color,
        )
    return img


def _chart_cache_path(log_key: str, width: int, height: int) -> Path:
    import re

    safe = re.sub(r"[^\w.-]+", "_", log_key or "spark").strip("_") or "spark"
    cache_dir = PROJECT_ROOT / "data" / "chart_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{safe}_{width}x{height}.png"


def _display_candlestick_chart(
    bars: List[dict],
    df: Optional[pd.DataFrame] = None,
    *,
    width: int = 320,
    height: int = 110,
    log_key: str = "",
    show_ma: bool = False,
    ma_periods: Tuple[int, ...] = (5, 20, 60),
    include_volume: bool = False,
    show_bar_count: bool = False,
) -> None:
    """matplotlib 蠟燭圖 + ``st.pyplot``（OHLC 實體 K 線）。"""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    if not bars:
        return
    n = len(bars)
    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]
    pmin, pmax = min(lows), max(highs)
    if pmax <= pmin:
        pmax = pmin + 1.0
    pad = (pmax - pmin) * 0.04
    ylo, yhi = pmin - pad, pmax + pad

    fig_h = max(1.2, height / 72)
    fig_w = max(2.0, width / 72)
    if include_volume and df is not None and "volume" in df.columns:
        fig, (ax, ax_v) = plt.subplots(
            2, 1, figsize=(fig_w, fig_h * 1.25), dpi=96,
            gridspec_kw={"height_ratios": [3, 1]}, sharex=True,
        )
    else:
        fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=96)
        ax_v = None

    body_w = max(0.35, min(0.85, 2.4 / max(n, 1) * 20))
    for i, b in enumerate(bars):
        o, h, l, c = (
            float(b["open"]), float(b["high"]),
            float(b["low"]), float(b["close"]),
        )
        color = "#d64545" if c >= o else "#1d9c5b"
        ax.plot([i, i], [l, h], color=color, linewidth=0.9, solid_capstyle="round")
        bot = min(o, c)
        bh = max(abs(c - o), (yhi - ylo) * 0.003)
        ax.add_patch(
            Rectangle(
                (i - body_w / 2, bot), body_w, bh,
                facecolor=color, edgecolor=color, linewidth=0.4,
            ),
        )

    if show_ma and df is not None:
        plot = df.reset_index(drop=True) if "close" in df.columns else df
        ma_colors = {"ma5": "#ffa500", "ma20": "#1f77b4", "ma60": "#9467bd"}
        for p in ma_periods:
            col = f"ma{p}"
            if col in plot.columns:
                ax.plot(
                    range(len(plot)), plot[col].values,
                    color=ma_colors.get(col, "#888888"),
                    linewidth=1.1, alpha=0.9,
                )

    ax.set_xlim(-0.6, n - 0.4)
    ax.set_ylim(ylo, yhi)
    ax.set_facecolor("#fafafa")
    fig.patch.set_facecolor("#fafafa")
    ax.tick_params(left=False, labelleft=False, bottom=False, labelbottom=False)
    for spine in ax.spines.values():
        spine.set_visible(False)

    if ax_v is not None and df is not None:
        vols = df["volume"].astype(float).values
        vcolors = [
            "#d64545" if bars[i]["close"] >= bars[i]["open"] else "#1d9c5b"
            for i in range(n)
        ]
        ax_v.bar(range(n), vols, color=vcolors, width=body_w * 1.1)
        ax_v.tick_params(left=False, labelleft=False, bottom=False, labelbottom=False)
        for spine in ax_v.spines.values():
            spine.set_visible(False)

    fig.tight_layout(pad=0.15)
    _debug_log_candlestick(
        "common.py:_display_candlestick_chart",
        {
            "bars": n, "width": width, "height": height,
            "key": log_key, "renderer": "matplotlib_pyplot",
            "show_ma": show_ma, "include_volume": include_volume,
        },
    )
    st.pyplot(fig, clear_figure=True, use_container_width=True)
    if show_bar_count:
        st.caption(f"📊 {n} 根 K 棒（蠟燭圖）")


def _display_candlestick_svg(
    bars: List[dict],
    *,
    width: int = 320,
    height: int = 110,
    log_key: str = "",
    show_bar_count: bool = False,
) -> None:
    """向後相容別名 → matplotlib 蠟燭圖。"""
    _display_candlestick_chart(
        bars, width=width, height=height,
        log_key=log_key, show_bar_count=show_bar_count,
    )


def _ohlc_bars_from_df(df: pd.DataFrame) -> List[dict]:
    plot = df.copy()
    if "date" not in plot.columns and plot.index.name == "date":
        plot = plot.reset_index()
    out: List[dict] = []
    for _, row in plot.iterrows():
        close = float(row["close"])
        item = {
            "open": float(row.get("open", close)),
            "high": float(row.get("high", close)),
            "low": float(row.get("low", close)),
            "close": close,
        }
        if "date" in row.index:
            item["date"] = row["date"]
        out.append(item)
    return out


def _fallback_is_ohlc(df: Optional[pd.DataFrame]) -> bool:
    """K 線 fallback 資料（含收盤價與日期）。"""
    if df is None or df.empty:
        return False
    cols = set(df.columns)
    has_close = "close" in cols
    has_date = "date" in cols or df.index.name == "date"
    return has_close and has_date


def _display_ohlc_native(
    df: pd.DataFrame,
    *,
    width: int = 700,
    height: int = 300,
    include_volume: bool = False,
) -> None:
    """matplotlib 蠟燭圖 + 均線 + 可選成交量。"""
    plot = df.copy()
    if "date" not in plot.columns and plot.index.name == "date":
        plot = plot.reset_index()
    if "date" in plot.columns:
        plot["date"] = pd.to_datetime(plot["date"])
    _display_candlestick_chart(
        _ohlc_bars_from_df(plot),
        df=plot,
        width=width,
        height=height,
        log_key="ohlc",
        show_ma=True,
        include_volume=include_volume,
    )


def _display_macd_native(df: pd.DataFrame, *, height: int = 200) -> None:
    plot = df.copy()
    if "date" in plot.columns:
        plot["date"] = pd.to_datetime(plot["date"])
    cols = [c for c in ("macd", "macd_signal") if c in plot.columns]
    if cols:
        st.line_chart(plot, x="date", y=cols, height=height)
    if "macd_hist" in plot.columns:
        st.bar_chart(plot, x="date", y="macd_hist", height=max(80, height // 3))


def _display_rsi_native(df: pd.DataFrame, *, height: int = 160) -> None:
    plot = df.copy()
    if "date" in plot.columns:
        plot["date"] = pd.to_datetime(plot["date"])
    if "rsi" in plot.columns:
        st.line_chart(plot, x="date", y="rsi", height=height)


def _display_altair(
    chart: Any,
    *,
    width: Optional[int] = None,
    fallback_df: Optional[pd.DataFrame] = None,
    chart_key: Optional[str] = None,
    use_ohlc_native: bool = False,
    include_volume: bool = False,
    chart_height: int = 300,
) -> None:
    """安全渲染 Altair 圖；僅在 ``use_ohlc_native=True`` 時對 K 線走原生圖表。"""
    if chart is None:
        return
    try:
        if _altair_is_composite(chart):
            w = width or 700
            if use_ohlc_native and _fallback_is_ohlc(fallback_df):
                _debug_log_session(
                    "H20",
                    "common.py:_display_altair",
                    "altair render path",
                    {
                        "path": "ohlc_native",
                        "rows": len(fallback_df),  # type: ignore[arg-type]
                        "chart_key": chart_key or "",
                    },
                )
                _display_ohlc_native(
                    fallback_df,  # type: ignore[arg-type]
                    width=w,
                    height=chart_height,
                    include_volume=include_volume,
                )
                return
            _debug_log_session(
                "H20",
                "common.py:_display_altair",
                "altair render path",
                {"path": "vega_lite", "chart_key": chart_key or ""},
            )
            chart = chart.properties(width=w).configure_view(
                discreteWidth=w, continuousWidth=w,
            )
            raw_spec = chart.to_dict()
            spec = _prepare_vega_spec_for_streamlit(raw_spec)
            kwargs: dict = {"spec": spec, "width": w, "use_container_width": False}
            if chart_key:
                kwargs["key"] = chart_key
            st.vega_lite_chart(**kwargs)
        else:
            st.altair_chart(chart, use_container_width=True)
    except Exception as exc:
        if fallback_df is not None and not fallback_df.empty:
            plot_df = fallback_df.copy()
            idx_col = next(
                (c for c in ("date", "週別", "年月", "年季", "key") if c in plot_df.columns),
                None,
            )
            val_col = next(
                (c for c in (
                    "close", "累計張數", "營收 (千元)", "EPS", "季營收", "比例",
                    "YoY %", "毛利率 %",
                ) if c in plot_df.columns),
                None,
            )
            if val_col is None:
                num_cols = list(plot_df.select_dtypes(include="number").columns)
                val_col = num_cols[0] if num_cols else None
            if val_col:
                if idx_col:
                    plot_df = plot_df.set_index(idx_col)
                st.line_chart(plot_df[[val_col]])
                st.caption("圖表渲染失敗，已改顯示折線圖。")
                return
        st.caption(f"圖表渲染失敗：{exc}")


def _llm_auto_banner(text: str = "") -> None:
    """頁面有『可選自動 LLM』功能時，用這個 banner 提醒。"""
    msg = (
        "🤖 **此頁含可選自動 LLM 行為**：只有在對應開關啟用、且 `GEMINI_API_KEY` 已設定時，"
        "才會呼叫 Gemini 做法說情緒分析 (有 12 小時快取避免重複)。"
    )
    if text:
        msg += f"  \n{text}"
    st.warning(msg, icon="🤖")
