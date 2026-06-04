"""Stock Bot Dashboard -- 一覽所有功能、編組態、跑模式、看報表的 Streamlit 介面。

啟動方式 (任一即可):
    uv run stock-dashboard
    uv run streamlit run src/bot/dashboard.py
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# 確保被 `streamlit run src/bot/dashboard.py` 啟動時也能 import bot.*
_PKG_ROOT = Path(__file__).resolve().parents[1]
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from bot.env_io import (  # noqa: E402
    ENV_FIELDS,
    EnvField,
    env_path,
    grouped_fields,
    load_env,
    save_env,
    validate,
)
from bot.process_runner import get_runner, get_scheduler_runner, tail_file  # noqa: E402
from bot.active_etf import (  # noqa: E402
    ActiveEtf,
    DEFAULT_ACTIVE_ETFS,
    Holding,
    HoldingsSnapshot,
    list_active_etfs_path,
    list_holdings_dates,
    load_active_etfs,
    load_holdings,
    save_active_etfs,
    save_holdings,
)
from bot.etf_consensus import (  # noqa: E402
    build_consensus,
    consensus_additions,
    consensus_new_builds,
    diff_snapshots,
)
from bot.prompt_registry import get_registry  # noqa: E402
from bot.llm_log import get_call_logger  # noqa: E402
from bot.scoring import (  # noqa: E402
    STRATEGY_RULES,
    TIMEFRAMES,
    TIMEFRAME_LABELS,
    WEIGHTS,
    compute_scorecard,
    scorecard_to_row,
)
from bot.ticker_view import build_snapshot, snapshot_to_dict  # noqa: E402
from bot.portfolio import (  # noqa: E402
    LOT_SIZE,
    BrokerPosition,
    PortfolioPosition,
    classify_bot_ownership,
    fetch_broker_positions,
    load_bot_portfolio,
)
from bot.portfolio_analysis import (  # noqa: E402
    build_portfolio_analysis_bundle,
    bundle_to_json,
)
from bot import watchlist as wl  # noqa: E402
from bot.stock_db import (  # noqa: E402
    ALL_TABLES,
    SYNCABLE_TABLES,
    StockDB,
    StockInfo,
    WatchlistRow,
    default_db_path,
    get_db,
    reset_db_singleton,
)
from bot.company_info import lookup_company_info  # noqa: E402
from bot.cloud_sync import (  # noqa: E402
    CloudConfig,
    CloudSyncDependencyError,
    GoogleSheetSync,
    TableSyncResult,
    load_config_from_env,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


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


# ======================================================================
# 頁面: 功能總覽
# ======================================================================


FEATURE_GRID = [
    ("三種執行模式", "trade 自動交易 / watch 看盤 / report 公開資料分析"),
    ("Shioaji 原生整合", "Decorator 回呼、Queue 解耦、避免阻塞"),
    ("盤前收盤價載入", "透過 snapshots / TWSE 取得精確前日收盤"),
    ("多檔資金追蹤", "即時追蹤已用資金，避免超額下單"),
    ("移動停利", "追蹤持倉最高價，自高點回撤 N% 觸發出場"),
    ("部位管理", "即時持倉均價、數量，成交回報自動更新"),
    ("委託單追蹤", "防止重複下單，輪詢狀態自動清理"),
    ("斷線重連", "指數退避重試，自動恢復行情訂閱"),
    ("收盤全出場", "EXIT_TIME 到後自動市價清倉"),
    ("交易紀錄匯出", "trade 模式收盤後匯出 trades_*.csv"),
    ("訊號與報表匯出", "watch/report 模式匯出 signals/report CSV"),
    ("Telegram 通知", "買賣/停損停利/收盤摘要推播"),
    ("模擬模式", "SIMULATION=true 走 Shioaji 模擬環境"),
    ("主動 ETF 追蹤", "28+ 檔主動式 ETF 持股快照、權重、共識計算"),
    ("ETF 共識跟單", "跨多檔 ETF 偵測新建倉/共識加碼，自動納入監控池"),
    ("MOPS 法說會爬蟲", "抓取法人說明會行事曆與個股重大訊息"),
    ("Gemini LLM 分析", "免費 API 解析法說會、提取情緒與成長驅動因子"),
    ("言行反查", "用籌碼面驗證管理階層是否言行一致 (出貨/吸籌)"),
    ("Prompt 版本化管理", "所有 LLM prompt 集中於 prompts/*.yaml，可在 UI 直接編輯"),
    ("LLM 呼叫全紀錄", "每次 LLM 呼叫的 input/output/延遲/tokens 寫入 JSONL 供審計"),
    ("ETF 持股自動抓取", "依 URL 配置 HTTP 下載 + Gemini 自動抽取持股 JSON"),
    ("籌碼面自動拉取", "TWSE OpenAPI 自動抓三大法人/借券/融資/鉅額交易"),
    ("一鍵研究管線", "ETF×籌碼×法說×LLM×每日簡報全部串連執行"),
    ("評分量表系統", "六個 factor × 四時間框架 (當沖/短/中/長) 加權，自動產出建議"),
    ("個股總覽 Watchlist", "表格式列出多檔股票，依時間框分數排序篩選"),
    ("目前持股分析", "用本地成交紀錄彙總 FIFO 成本、市值、損益與曝險權重"),
    ("個股深入分析", "單檔股票六分頁：分析、原資料、分析數據、購買策略、現況、歷史"),
]

MODES = [
    {
        "name": "trade",
        "title": "trade — 自動交易",
        "color": "green",
        "desc": "完整 Shioaji 登入 + CA，策略觸發時真正送單 (SIMULATION=true 走模擬環境)。",
        "needs": ["API Key", "Secret Key", "電子憑證 (實單)"],
        "outputs": ["data/trades_YYYY-MM-DD.csv"],
    },
    {
        "name": "watch",
        "title": "watch — 看盤模式",
        "color": "blue",
        "desc": "Shioaji 即時行情 + 不啟用 CA 不下單，記錄 would-buy / would-sell 訊號。",
        "needs": ["API Key", "Secret Key"],
        "outputs": ["data/reports/signals_*.csv", "data/reports/report_*.csv"],
    },
    {
        "name": "report",
        "title": "report — 報表分析",
        "color": "yellow",
        "desc": "不登入券商，輪詢 TWSE 公開延遲報價 (≥ 20 分鐘) 做策略分析。",
        "needs": ["(不需要 API Key)"],
        "outputs": ["data/reports/signals_*.csv", "data/reports/report_*.csv"],
    },
]


def page_overview() -> None:
    st.title("Stock Bot 功能總覽")
    st.caption("台股當沖自動交易機器人 · Shioaji + TWSE 公開資料")

    env_values = load_env()
    runner = get_runner(PROJECT_ROOT)
    current = runner.current()
    running = runner.is_running()

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("執行模式", env_values.get("RUN_MODE", "trade"))
    col2.metric("行情來源", env_values.get("MARKET_SOURCE", "") or "(自動)")
    col3.metric(
        "監控股票",
        len([s for s in env_values.get("SYMBOLS", "").split(",") if s.strip()]) or 0,
    )
    sim = env_values.get("SIMULATION", "true").lower() == "true"
    col4.markdown(
        f"**模擬模式**<br>{_badge('SIMULATION', 'yellow' if sim else 'red')} "
        f"{_badge('ON', 'green') if sim else _badge('OFF', 'red')}",
        unsafe_allow_html=True,
    )

    st.markdown("---")

    s1, s2, s3 = st.columns([1, 1, 2])
    with s1:
        if running:
            st.markdown(_badge("● Bot 執行中", "green"), unsafe_allow_html=True)
        else:
            st.markdown(_badge("○ Bot 未執行", "gray"), unsafe_allow_html=True)
    with s2:
        if current:
            elapsed = (current.ended_at or time.time()) - current.started_at
            st.caption(
                f"最近一次: {current.run_mode} (PID {current.pid}, {_human_duration(elapsed)})"
            )
        else:
            st.caption("尚未從儀表板啟動過")
    with s3:
        st.caption(f"專案位置: `{PROJECT_ROOT}`")

    st.markdown("### 三種執行模式")
    cols = st.columns(3)
    for col, m in zip(cols, MODES):
        with col:
            st.markdown(f"#### {m['title']}")
            st.markdown(_badge(m["name"].upper(), m["color"]), unsafe_allow_html=True)
            st.write(m["desc"])
            st.caption("需要：" + " / ".join(m["needs"]))
            st.caption("輸出：")
            for o in m["outputs"]:
                st.code(o, language="text")

    st.markdown("### 核心功能")
    grid_cols = st.columns(3)
    for i, (title, desc) in enumerate(FEATURE_GRID):
        with grid_cols[i % 3]:
            st.markdown(f"**{title}**")
            st.caption(desc)

    st.markdown("### 快速入口")
    st.caption("整體工作流程：(1) 自動化管線一鍵抓料 → (2) 個股總覽看 watchlist 評分 → (3) 點個股深入分析")

    q1, q2, q3, q4 = st.columns(4)
    if q1.button("📊 K 線看板", use_container_width=True, type="primary"):
        st.session_state.page = "K 線看板"
        st.rerun()
    if q2.button("🔬 個股深入分析", use_container_width=True):
        st.session_state.page = "個股深入分析"
        st.rerun()
    if q3.button("⚙ 自動化管線", use_container_width=True):
        st.session_state.page = "自動化管線"
        st.rerun()
    if q4.button("🔧 組態設定", use_container_width=True):
        st.session_state.page = "組態設定"
        st.rerun()

    q5, q6, q7, q8 = st.columns(4)
    if q5.button("跟單訊號", use_container_width=True):
        st.session_state.page = "跟單訊號"
        st.rerun()
    if q6.button("主動 ETF 追蹤 🤖", use_container_width=True, help="此頁的「立即抓取所有」會呼叫 Gemini。"):
        st.session_state.page = "主動 ETF 追蹤"
        st.rerun()
    if q7.button("🤖 LLM 法說分析", use_container_width=True, help="此頁所有「用 Gemini 分析 / 執行反查」按鈕都會呼叫 Gemini。"):
        st.session_state.page = "LLM 法說分析"
        st.rerun()
    if q8.button("啟動 / 監控", use_container_width=True):
        st.session_state.page = "啟動 / 監控"
        st.rerun()

    q9, _, _, _ = st.columns(4)
    if q9.button("目前持股分析", use_container_width=True):
        st.session_state.page = "目前持股分析"
        st.rerun()

    st.markdown("---")
    with st.container(border=True):
        st.markdown("### 🤖 哪些操作會呼叫 LLM (消耗 Gemini API 額度)？")
        st.markdown(
            "| 頁面 | 操作 | LLM 呼叫類型 |\n"
            "|------|------|--------------|\n"
            "| 🤖 LLM 法說分析 | 用 Gemini 分析 / 執行反查 | **直接** (1-2 次/按) |\n"
            "| 🤖 自動化管線 | 立即執行管線 (LLM toggle 開) | **直接** (數次 ~ 數十次) |\n"
            "| 🤖 主動 ETF 追蹤 | 立即抓取所有 (有 URL 的) | **直接** (每檔 ETF 1 次) |\n"
            "| 🤖 今日當沖戰情室 | 重抓新聞 + 重跑 / 用快取重跑 | **直接** (至少 2 次) |\n"
            "| 🤖 美股 / 跨市場 | 呼叫 Gemini 產出簡報 | **直接** (1 次) |\n"
            "| 🤖 個股深入分析 | 分析 / 強制重抓全部資料 | **自動** (該檔缺 LLM 法說時) |\n"
            "| 🤖 個股總覽 | 計算評分 | **自動** (對每檔缺 LLM 法說的個股) |\n"
            "| Prompt 管理 / LLM 呼叫紀錄 | (檢視/編輯) | ❌ 不呼叫 |\n"
            "| 其他頁面 (組態/資料庫/風控/報表/...) | — | ❌ 不呼叫 |\n"
        )
        st.caption(
            "ℹ 自動觸發的 LLM 呼叫有 12 小時快取 (`data/auto_llm/<ticker>.json`)，"
            "且未設 `GEMINI_API_KEY` 時整段 graceful skip，不會偷扣額度。"
            "所有 LLM 呼叫的 input/output/延遲/tokens 都會記在『LLM 呼叫紀錄』頁。"
        )


# ======================================================================
# 頁面: 組態設定
# ======================================================================


def _render_field(f: EnvField, value: str) -> str:
    """根據欄位型別渲染對應的 widget，回傳新值的字串。"""
    key = f"env_{f.key}"
    help_text = f.help or None

    if f.kind == "select":
        opts = f.options
        idx = opts.index(value) if value in opts else 0
        new = st.selectbox(f.label, opts, index=idx, key=key, help=help_text)
        return new

    if f.kind == "bool":
        cur = value.lower() in ("true", "1", "yes")
        new = st.toggle(f.label, value=cur, key=key, help=help_text)
        return "true" if new else "false"

    if f.kind == "time":
        try:
            hh, mm = value.split(":") if value else ("09", "30")
            t_init = dt.time(int(hh), int(mm))
        except Exception:
            t_init = dt.time(9, 30)
        new_t = st.time_input(f.label, value=t_init, key=key, help=help_text)
        return new_t.strftime("%H:%M")

    if f.kind == "password":
        return st.text_input(
            f.label, value=value, type="password", key=key, help=help_text,
        )

    if f.kind == "int":
        try:
            v_int = int(value) if value else int(f.default or 0)
        except ValueError:
            v_int = int(f.default or 0)
        new_i = st.number_input(
            f.label, value=v_int, step=1, key=key, help=help_text,
        )
        return str(int(new_i))

    if f.kind == "float":
        try:
            v_f = float(value) if value else float(f.default or 0)
        except ValueError:
            v_f = float(f.default or 0)
        new_f = st.number_input(
            f.label, value=v_f, step=0.1, format="%.2f", key=key, help=help_text,
        )
        return f"{new_f}"

    if f.kind == "symbols":
        new = st.text_input(
            f.label, value=value, key=key,
            help=(help_text or "") + " (例: 2330,0050,2881)",
            placeholder="2330,0050",
        )
        cleaned = ",".join(s.strip() for s in new.split(",") if s.strip())
        return cleaned

    return st.text_input(f.label, value=value, key=key, help=help_text)


def page_config() -> None:
    st.title("組態設定")
    st.caption(f".env 路徑: `{env_path()}`")

    env_values = load_env()

    if not env_values:
        st.warning("尚未找到 .env，將使用各欄位預設值。儲存後會自動建立。")

    groups = grouped_fields()
    new_values: Dict[str, str] = dict(env_values)

    for section, fields_in_section in groups.items():
        with st.expander(section, expanded=section in ("執行模式", "策略", "風控")):
            cols = st.columns(2)
            for i, f in enumerate(fields_in_section):
                with cols[i % 2]:
                    cur = env_values.get(f.key, str(f.default))
                    new_values[f.key] = _render_field(f, cur)

    st.markdown("---")
    c1, c2, c3 = st.columns([1, 1, 2])
    save_clicked = c1.button("儲存到 .env", type="primary", use_container_width=True)
    reload_clicked = c2.button("放棄變更 / 重新載入", use_container_width=True)

    if reload_clicked:
        st.rerun()

    if save_clicked:
        errors = validate(new_values)
        if errors:
            st.error("發現以下錯誤，請修正後再儲存：")
            for e in errors:
                st.write(f"- {e}")
        else:
            p = save_env(new_values)
            st.success(f"已儲存到 {p}（同時備份為 .env.bak）")
            st.toast("組態已更新", icon="✅")

    with st.expander("檢視目前 .env 原文 (已遮罩敏感欄位)"):
        from bot.env_io import masked
        st.code(
            "\n".join(f"{k}={v}" for k, v in masked(load_env()).items()) or "(空)",
            language="ini",
        )


# ======================================================================
# 頁面: 啟動 / 監控
# ======================================================================


def page_runner() -> None:
    st.title("啟動 / 監控")
    runner = get_runner(PROJECT_ROOT)

    env_values = load_env()
    default_mode = env_values.get("RUN_MODE", "trade")

    # 啟動前提醒：建議先跑 preflight
    if env_values.get("RUN_MODE") == "trade" and env_values.get("SIMULATION", "true").lower() in ("false", "0"):
        with st.container(border=True):
            cols = st.columns([5, 1])
            with cols[0]:
                st.warning(
                    "⚠️ 即將以 **trade + 真實環境** 模式啟動。建議先到「交易可行性檢查」"
                    "驗證 API Token 權限、CA 憑證、簽署狀態。"
                )
            with cols[1]:
                if st.button("🩺 去檢查", use_container_width=True):
                    st.session_state.page = "交易可行性檢查"
                    st.rerun()

    col1, col2 = st.columns([1, 2])
    with col1:
        st.subheader("啟動模式")
        mode = st.radio(
            "選擇本次執行模式",
            ["trade", "watch", "report"],
            index=["trade", "watch", "report"].index(default_mode)
            if default_mode in ("trade", "watch", "report") else 0,
            help="會覆蓋 .env 的 RUN_MODE (僅本次執行)",
        )
        symbols_override = st.text_input(
            "本次監控股票 (留空使用 .env)",
            value="",
            placeholder="2330,0050",
        )
        running = runner.is_running()
        start_clicked = st.button(
            "啟動 Bot", type="primary", disabled=running, use_container_width=True,
        )
        stop_clicked = st.button(
            "停止 Bot", disabled=not running, use_container_width=True,
        )

    with col2:
        st.subheader("執行狀態")
        cur = runner.current()
        if running and cur:
            elapsed = time.time() - cur.started_at
            st.markdown(
                f"{_badge('執行中', 'green')} &nbsp; PID {cur.pid} &nbsp; "
                f"模式 `{cur.run_mode}` &nbsp; 持續 {_human_duration(elapsed)}",
                unsafe_allow_html=True,
            )
            st.caption(f"Log: `{cur.log_path}`")
        elif cur:
            duration = (cur.ended_at or cur.started_at) - cur.started_at
            rc_badge = _badge(
                f"exit={cur.return_code}",
                "green" if cur.return_code == 0 else "red",
            )
            st.markdown(
                f"{_badge('已結束', 'gray')} &nbsp; {rc_badge} &nbsp; "
                f"模式 `{cur.run_mode}` &nbsp; 總長 {_human_duration(duration)}",
                unsafe_allow_html=True,
            )
            st.caption(f"Log: `{cur.log_path}`")
        else:
            st.info("尚未啟動過任何 Bot。設定好 .env 後按「啟動 Bot」即可。")

    if start_clicked:
        extra: Dict[str, str] = {}
        if symbols_override.strip():
            extra["SYMBOLS"] = ",".join(
                s.strip() for s in symbols_override.split(",") if s.strip()
            )
        record = runner.start(run_mode=mode, extra_env=extra)
        st.success(f"已啟動 (PID {record.pid})，模式: {record.run_mode}")
        time.sleep(0.5)
        st.rerun()

    if stop_clicked:
        ok = runner.stop()
        if ok:
            st.success("已送出停止訊號")
        else:
            st.warning("強制終止 (timeout)")
        time.sleep(0.5)
        st.rerun()

    st.markdown("---")
    st.subheader("即時 Log (最後 200 行)")
    cur = runner.current()
    if cur:
        auto = st.checkbox("自動刷新 (每 3 秒)", value=running)
        st.code(tail_file(cur.log_path, lines=200) or "(尚無輸出)", language="log")
        if auto and running:
            time.sleep(3)
            st.rerun()
    else:
        st.caption("尚未產生任何執行 log。")


# ======================================================================
# 頁面: 風控中心
# ======================================================================


def page_risk_center() -> None:
    st.title("風控中心 (Risk Center)")
    st.caption("看一眼今天的資金/部位/虧損狀態，必要時拉下「緊急 Kill Switch」立刻關掉所有新進場。")

    from bot.config import Settings as S  # noqa: E402
    from bot.risk_guard import KILL_SWITCH_FILENAME, RiskGuard  # noqa: E402

    settings = S()
    guard = RiskGuard(settings=settings, project_root=PROJECT_ROOT)
    snap = guard.snapshot()

    # ============ Kill Switch =============
    with st.container(border=True):
        cols = st.columns([3, 1, 1])
        with cols[0]:
            if snap["kill_switch"]:
                st.error(
                    "🔴 **Kill Switch 已啟動** — 所有新進場單會被拒絕。"
                    f" 觸發檔案: `data/{KILL_SWITCH_FILENAME}`"
                )
            else:
                st.success("🟢 Kill Switch 未啟動 — 正常運作中")
        with cols[1]:
            if not snap["kill_switch"]:
                if st.button("🔴 緊急拉閘", type="primary", use_container_width=True):
                    guard.engage_kill_switch("dashboard_manual")
                    st.rerun()
        with cols[2]:
            if snap["kill_switch"]:
                if st.button("🟢 解除拉閘", use_container_width=True):
                    guard.release_kill_switch()
                    st.rerun()

    # ============ 即時概況 =============
    st.markdown("### 📊 今日即時概況")
    fund_used = float(snap["fund_used"])
    fund_cap = float(snap["fund_cap"])
    fund_remaining = float(snap["fund_remaining"])
    fund_pct = (fund_used / fund_cap * 100) if fund_cap > 0 else 0

    m1, m2, m3, m4 = st.columns(4)
    m1.metric(
        "已用資金", f"{fund_used:,.0f}",
        f"{fund_pct:.1f}% of {fund_cap:,.0f}",
        delta_color="off",
    )
    m2.metric(
        "剩餘可用", f"{fund_remaining:,.0f}",
        f"{100-fund_pct:.1f}%",
        delta_color="off",
    )
    m3.metric(
        "在倉檔數", f"{snap['open_positions']}",
        f"上限 {snap['max_open_positions'] or '不限'}",
        delta_color="off",
    )
    m4.metric(
        "今日進場次數", f"{snap['today_orders']}",
        f"上限 {snap['daily_max_orders'] or '不限'}",
        delta_color="off",
    )

    # 資金使用進度條
    st.progress(min(fund_pct / 100, 1.0), text=f"資金使用 {fund_pct:.1f}%")

    # ============ 損益 =============
    realized = float(snap["realized_pnl_twd"])
    unrealized = float(snap["unrealized_pnl_twd"])
    loss_cap = float(snap["daily_loss_cap"])
    pnl_color = "green" if realized >= 0 else "red"

    st.markdown("### 💰 損益狀態")
    p1, p2, p3 = st.columns(3)
    p1.metric("已實現損益", f"{realized:+,.0f}", delta=None)
    p2.metric("未實現損益", f"{unrealized:+,.0f}", delta=None)
    if loss_cap > 0:
        # 距離熔斷剩多少
        gap = loss_cap + realized  # realized 是負的時候 gap 才會變小
        if gap > 0:
            p3.metric("距熔斷剩餘空間", f"{gap:,.0f}", f"門檻 -{loss_cap:,.0f}")
        else:
            p3.metric("熔斷狀態", "🚨 已觸發", f"虧損 {realized:,.0f}")
    else:
        p3.metric("熔斷設定", "未啟用", "DAILY_MAX_LOSS_* 都是 0")

    # ============ 當前風控設定 =============
    with st.expander("🛡 當前風控設定 (12 道閘門)", expanded=False):
        rules_data = [
            ["A. 總資金上限", f"{settings.max_fund:,} TWD", "MAX_FUND"],
            ["A. 單筆委託上限", f"{settings.per_order_max_cost_twd:,} TWD" if settings.per_order_max_cost_twd > 0 else "(不限)", "PER_ORDER_MAX_COST_TWD"],
            ["B. 單檔最大張數", f"{settings.max_lot_per_symbol}", "MAX_LOT_PER_SYMBOL"],
            ["B. 同時最多在倉", f"{settings.max_open_positions} 檔" if settings.max_open_positions > 0 else "(不限)", "MAX_OPEN_POSITIONS"],
            ["B. 黑名單", ", ".join(snap["blacklist"]) or "(無)", "BLACKLIST_SYMBOLS"],
            ["C. 每日虧損 (元)", f"-{settings.daily_max_loss_twd:,} TWD" if settings.daily_max_loss_twd > 0 else "(不限)", "DAILY_MAX_LOSS_TWD"],
            ["C. 每日虧損 (%)", f"-{settings.daily_max_loss_pct}% × max_fund" if settings.daily_max_loss_pct > 0 else "(不限)", "DAILY_MAX_LOSS_PCT"],
            ["D. 當日進場次數", f"≤ {settings.daily_max_orders}" if settings.daily_max_orders > 0 else "(不限)", "DAILY_MAX_ORDERS"],
            ["D. 單檔進場次數", f"≤ {settings.per_symbol_daily_max_orders}" if settings.per_symbol_daily_max_orders > 0 else "(不限)", "PER_SYMBOL_DAILY_MAX_ORDERS"],
            ["D. 平倉後冷卻", f"{settings.reentry_cooldown_seconds}s" if settings.reentry_cooldown_seconds > 0 else "(立即可再進)", "REENTRY_COOLDOWN_SECONDS"],
            ["E. 進場最大漲幅", f"≤ {settings.max_pct_chg_on_entry}%" if settings.max_pct_chg_on_entry > 0 else "(不限)", "MAX_PCT_CHG_ON_ENTRY"],
            ["E. 價格區間", f"{settings.min_price:.0f}~{settings.max_price:.0f}" if (settings.min_price or settings.max_price) else "(不限)", "MIN_PRICE / MAX_PRICE"],
            ["F. 停損/停利", f"{settings.stop_loss_pct}% / +{settings.take_profit_pct}% / 移動 {settings.trailing_stop_pct}%", "STOP_LOSS_PCT / TAKE_PROFIT_PCT / TRAILING_STOP_PCT"],
        ]
        df_rules = pd.DataFrame(rules_data, columns=["閘門", "當前值", "環境變數"])
        st.dataframe(df_rules, hide_index=True, use_container_width=True)
        st.caption("💡 要調整這些值，去「組態設定」頁找對應的 ENV，存檔即可。重啟 bot 才會生效。")

    # ============ 各檔今日下單統計 =============
    sym_orders = snap.get("today_orders_per_symbol", {})
    if sym_orders:
        st.markdown("### 📋 各檔今日進場次數")
        df_sym = pd.DataFrame(
            [(s, n) for s, n in sym_orders.items()],
            columns=["代號", "次數"],
        ).sort_values("次數", ascending=False)
        st.dataframe(df_sym, hide_index=True, use_container_width=True)

    # ============ 被擋下來的進場 =============
    blocked = snap.get("blocked_recent", [])
    st.markdown(f"### ⛔ 最近被擋下的進場 ({len(blocked)} / 最多保留 20 筆)")
    if blocked:
        df_blocked = pd.DataFrame(blocked)
        st.dataframe(df_blocked, hide_index=True, use_container_width=True)
    else:
        st.info("今日尚無進場被風控擋下。")

    # ============ 手動 CLI 提示 =============
    with st.expander("🛠 也可以用 CLI 控制 Kill Switch", expanded=False):
        st.code(
            "# 立刻拉閘 (Bot 下次嘗試進場時會立刻被拒)\n"
            f"echo manual > {PROJECT_ROOT / 'data' / KILL_SWITCH_FILENAME}\n\n"
            "# 解除拉閘\n"
            f"del {PROJECT_ROOT / 'data' / KILL_SWITCH_FILENAME}\n",
            language="powershell",
        )
        st.caption(
            "Kill Switch 只擋「新進場」，不擋「平倉」"
            "（停損/停利/收盤強平永遠會執行，因為平倉本身就是降低風險）。"
        )


# ======================================================================
# 頁面: 交易可行性檢查
# ======================================================================


_PREFLIGHT_ICON = {"ok": "✅", "warn": "⚠️", "fail": "❌", "info": "ℹ️"}
_PREFLIGHT_COLOR = {"ok": "green", "warn": "orange", "fail": "red", "info": "gray"}
_PREFLIGHT_SECTIONS = [
    ("section_env", "1. 環境變數"),
    ("section_ca", "2. 電子憑證 (CA)"),
    ("section_login", "3. Shioaji 連線測試"),
    ("section_account", "4. 帳戶權限"),
    ("section_risk", "5. 風控設定"),
    ("section_time", "6. 時間窗口"),
]


def page_preflight() -> None:
    st.title("交易可行性檢查 (Preflight)")
    st.caption(
        "在啟動 Bot 之前，先確認：API 金鑰 / 電子憑證 / 帳戶權限 / 模式設定 / "
        "停損停利 / 是否在盤中，全部到位才能真實下單。"
    )

    with st.expander("這份檢查在驗證什麼？", expanded=False):
        st.markdown(
            """
            **本系統只能買賣台股**（透過永豐 Shioaji API）。美股相關資料只用於分析，無法下單。

            真實下單的硬性條件 (全部要 ✅ 才能下單)：

            1. `API_KEY` / `SECRET_KEY` 已設定 — 在永豐 e leader 申請 Shioaji
            2. **API 金鑰具備「下單」權限** (不只是 Data) — 在 e leader 勾選後重新產生
            3. 線上協議已簽署 (stock_account.signed = True)
            4. 電子憑證 `.pfx` 檔存在、密碼正確、未過期
            5. 設定 `RUN_MODE=trade` (watch / report 不會下單)
            6. 設定 `SIMULATION=false` (true 為模擬環境)
            7. 現在處於台股盤中 09:00–13:30，且為交易日
            8. 風控設定 (MAX_FUND / MAX_LOT_PER_SYMBOL) 合理

            **目前不支援**：盤後零股交易 (15:00 盤、13:30 之後)、興櫃股票、海外證券。
            """
        )

    col_btn, col_opt = st.columns([1, 2])
    with col_btn:
        run_btn = st.button(
            "🩺 立刻檢查",
            type="primary",
            use_container_width=True,
        )
    with col_opt:
        do_login = st.checkbox(
            "包含實際 Shioaji 連線測試 (約需 5-10 秒)",
            value=True,
            help="關掉的話只檢查 .env 設定，不會打永豐 API",
        )

    if not run_btn and "preflight_report" not in st.session_state:
        st.info("按上方「🩺 立刻檢查」開始體檢。")
        return

    if run_btn:
        from bot.config import Settings as S  # noqa: E402
        from bot.preflight import report_to_dict, run_preflight  # noqa: E402

        with st.spinner("檢查中… (連線 Shioaji 約需數秒)"):
            try:
                report = run_preflight(S(), do_real_login=do_login)
                st.session_state["preflight_report"] = report_to_dict(report)
            except Exception as e:
                st.error(f"檢查時發生未預期錯誤: {e}")
                return

    data = st.session_state.get("preflight_report")
    if not data:
        return

    # ===== 結論 =====
    if data["can_trade_now"]:
        st.success(f"🟢 **可以下真實單** ｜ {data['summary']}")
    elif data["can_simulate"]:
        st.warning(f"🟡 **暫不可下真實單** ｜ {data['summary']}")
    else:
        st.error(f"🔴 **設定有阻擋** ｜ {data['summary']}")
    st.caption(f"檢查時間: {data['fetched_at']}")

    # ===== 摘要計數 =====
    sections = data["sections"]
    all_items = [c for items in sections.values() for c in items]
    ok = sum(1 for c in all_items if c["status"] == "ok")
    warn = sum(1 for c in all_items if c["status"] == "warn")
    fail = sum(1 for c in all_items if c["status"] == "fail")
    info = sum(1 for c in all_items if c["status"] == "info")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("✅ 通過", ok)
    m2.metric("⚠️ 警告", warn)
    m3.metric("❌ 阻擋", fail)
    m4.metric("ℹ️ 資訊", info)

    # ===== 阻擋清單 (有 fail 時最上方提醒) =====
    if fail > 0:
        st.markdown("##### ❌ 必須先解決")
        for c in all_items:
            if c["status"] != "fail":
                continue
            with st.container(border=True):
                st.markdown(f"**{c['name']}**")
                st.write(c["detail"])
                if c.get("suggestion"):
                    st.info(f"💡 {c['suggestion']}")

    # ===== 分區明細 =====
    st.markdown("---")
    st.subheader("分區明細")
    section_keys_dash = [
        ("env", "1. 環境變數"),
        ("ca", "2. 電子憑證 (CA)"),
        ("login", "3. Shioaji 連線測試"),
        ("account", "4. 帳戶權限"),
        ("risk", "5. 風控設定"),
        ("time", "6. 時間窗口"),
    ]
    for key, title in section_keys_dash:
        items = sections.get(key, [])
        if not items:
            continue
        with st.expander(f"{title} ({len(items)} 項)", expanded=(fail > 0 and key in ("env", "login", "account"))):
            for c in items:
                icon = _PREFLIGHT_ICON.get(c["status"], " ")
                cols = st.columns([3, 5])
                with cols[0]:
                    st.markdown(f"{icon} **{c['name']}**")
                with cols[1]:
                    if c["detail"]:
                        st.write(c["detail"])
                    if c.get("suggestion"):
                        st.caption(f"💡 {c['suggestion']}")

    # ===== 原始 JSON =====
    with st.expander("檢視原始 JSON", expanded=False):
        st.json(data)


# ======================================================================
# 頁面: 報表分析
# ======================================================================


def _render_csv_chart(df: pd.DataFrame, ts_col: str, value_col: str, group_col: Optional[str] = None) -> None:
    try:
        import altair as alt
        chart_df = df.copy()
        chart_df[ts_col] = pd.to_datetime(chart_df[ts_col], errors="coerce")
        if group_col and group_col in chart_df.columns:
            base = alt.Chart(chart_df).mark_line(point=True).encode(
                x=alt.X(f"{ts_col}:T", title="時間"),
                y=alt.Y(f"{value_col}:Q", title=value_col),
                color=alt.Color(f"{group_col}:N", title=group_col),
                tooltip=list(chart_df.columns),
            )
        else:
            base = alt.Chart(chart_df).mark_line(point=True).encode(
                x=alt.X(f"{ts_col}:T", title="時間"),
                y=alt.Y(f"{value_col}:Q", title=value_col),
                tooltip=list(chart_df.columns),
            )
        st.altair_chart(base, use_container_width=True)
    except Exception as e:
        st.caption(f"繪圖失敗: {e}")


def page_reports() -> None:
    st.title("報表分析")
    report_dir = _project_path("data", "reports")
    st.caption(f"報表目錄: `{report_dir}`")

    tab_report, tab_signal = st.tabs(["分析報表 report_*.csv", "訊號明細 signals_*.csv"])

    with tab_report:
        files = _list_files(report_dir, "report_*.csv")
        if not files:
            st.info("尚無分析報表。請先在 watch / report 模式跑過一次。")
        else:
            chosen = st.selectbox("選擇報表", [f.name for f in files], key="report_pick")
            f = report_dir / chosen
            df = _safe_read_csv(f)
            if df is not None:
                st.dataframe(df, use_container_width=True)
                c1, c2 = st.columns(2)
                with c1:
                    if "pnl_pct" in df.columns and "symbol" in df.columns:
                        st.bar_chart(df.set_index("symbol")["pnl_pct"])
                with c2:
                    if {"buy_signals", "sell_signals", "symbol"}.issubset(df.columns):
                        st.bar_chart(
                            df.set_index("symbol")[["buy_signals", "sell_signals"]]
                        )
                with f.open("rb") as fp:
                    st.download_button(
                        "下載 CSV", data=fp.read(), file_name=chosen, mime="text/csv",
                    )

    with tab_signal:
        files = _list_files(report_dir, "signals_*.csv")
        if not files:
            st.info("尚無訊號明細。")
        else:
            chosen = st.selectbox("選擇訊號檔", [f.name for f in files], key="signal_pick")
            f = report_dir / chosen
            df = _safe_read_csv(f)
            if df is not None and not df.empty:
                col1, col2, col3 = st.columns(3)
                with col1:
                    syms = sorted(df["symbol"].astype(str).unique().tolist()) if "symbol" in df.columns else []
                    pick_sym = st.multiselect("商品篩選", syms, default=syms)
                with col2:
                    actions = sorted(df["action"].unique().tolist()) if "action" in df.columns else []
                    pick_act = st.multiselect("動作", actions, default=actions)
                with col3:
                    reasons = sorted(df["reason"].unique().tolist()) if "reason" in df.columns else []
                    pick_rs = st.multiselect("原因", reasons, default=reasons)

                view = df
                if "symbol" in view.columns and pick_sym:
                    view = view[view["symbol"].astype(str).isin(pick_sym)]
                if "action" in view.columns and pick_act:
                    view = view[view["action"].isin(pick_act)]
                if "reason" in view.columns and pick_rs:
                    view = view[view["reason"].isin(pick_rs)]

                m1, m2, m3, m4 = st.columns(4)
                m1.metric("總訊號", len(view))
                if "action" in view.columns:
                    m2.metric("would-buy", int((view["action"] == "would-buy").sum()))
                    m3.metric("would-sell", int((view["action"] == "would-sell").sum()))
                if "pnl_pct" in view.columns and len(view):
                    m4.metric("平均虛擬損益(%)", f"{view['pnl_pct'].mean():.2f}")

                st.dataframe(view, use_container_width=True)

                if {"ts", "price", "symbol"}.issubset(view.columns):
                    st.markdown("#### 訊號價格走勢")
                    _render_csv_chart(view, "ts", "price", "symbol")

                with f.open("rb") as fp:
                    st.download_button(
                        "下載 CSV", data=fp.read(), file_name=chosen, mime="text/csv",
                    )


# ======================================================================
# 頁面: 交易紀錄
# ======================================================================


def page_trades() -> None:
    st.title("交易紀錄")
    data_dir = _project_path("data")
    st.caption(f"交易檔目錄: `{data_dir}`")
    files = _list_files(data_dir, "trades_*.csv")
    if not files:
        st.info("尚無交易紀錄。需在 trade 模式跑過一次才會匯出。")
        return

    chosen = st.selectbox("選擇交易紀錄", [f.name for f in files])
    f = data_dir / chosen
    df = _safe_read_csv(f)
    if df is None or df.empty:
        st.warning("檔案為空。")
        return

    if "amount" not in df.columns and {"price", "quantity"}.issubset(df.columns):
        df["amount"] = df["price"] * df["quantity"] * 1000

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("成交筆數", len(df))
    if "action" in df.columns:
        buys = df[df["action"] == "Buy"]
        sells = df[df["action"] == "Sell"]
        m2.metric("買進金額", f"{buys['amount'].sum():,.0f}" if "amount" in buys.columns else "-")
        m3.metric("賣出金額", f"{sells['amount'].sum():,.0f}" if "amount" in sells.columns else "-")
        if "amount" in df.columns:
            net = float(sells["amount"].sum() - buys["amount"].sum())
            m4.metric("淨額", f"{net:,.0f}")

    st.dataframe(df, use_container_width=True)

    if {"datetime", "amount", "action"}.issubset(df.columns):
        st.markdown("#### 成交金額走勢")
        df_plot = df.copy()
        df_plot["datetime"] = pd.to_datetime(df_plot["datetime"], errors="coerce")
        st.line_chart(df_plot.set_index("datetime")["amount"])

    with f.open("rb") as fp:
        st.download_button("下載 CSV", data=fp.read(), file_name=chosen, mime="text/csv")


# ======================================================================
# 頁面: 日誌檢視
# ======================================================================


def page_logs() -> None:
    st.title("日誌檢視")
    log_dir = _project_path("log")
    st.caption(f"日誌目錄: `{log_dir}`")

    if not log_dir.exists():
        st.info("尚無 log 目錄，跑過任何模式後就會出現。")
        return

    files = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        st.info("尚無 log 檔。")
        return

    name_to_path = {f.name: f for f in files}
    chosen = st.selectbox("選擇 log 檔", list(name_to_path.keys()))
    f = name_to_path[chosen]
    n = st.slider("顯示最後 N 行", 50, 2000, 300, step=50)
    auto = st.checkbox("自動刷新 (每 5 秒)")

    st.caption(f"檔案: `{f}` · 大小 {f.stat().st_size:,} bytes")
    st.code(tail_file(f, lines=n) or "(空)", language="log")

    if auto:
        time.sleep(5)
        st.rerun()


# ======================================================================
# 頁面: 通知測試
# ======================================================================


def page_notifier() -> None:
    st.title("Telegram 通知測試")

    env_values = load_env()
    token = env_values.get("TELEGRAM_BOT_TOKEN", "")
    chat = env_values.get("TELEGRAM_CHAT_ID", "")

    c1, c2 = st.columns(2)
    c1.markdown(
        f"Bot Token: {_badge('已設定', 'green') if token else _badge('未設定', 'gray')}",
        unsafe_allow_html=True,
    )
    c2.markdown(
        f"Chat ID: {_badge('已設定', 'green') if chat else _badge('未設定', 'gray')}",
        unsafe_allow_html=True,
    )

    if not (token and chat):
        st.warning("請先在「組態設定」頁填入 TELEGRAM_BOT_TOKEN 與 TELEGRAM_CHAT_ID。")

    st.markdown("---")
    msg = st.text_area(
        "測試訊息",
        value="📈 <b>Stock Bot Dashboard</b>\n這是一則來自儀表板的測試訊息。",
        height=120,
    )
    if st.button("發送測試", disabled=not (token and chat), type="primary"):
        from bot.notifier import TelegramNotifier

        notifier = TelegramNotifier(bot_token=token, chat_id=chat)
        notifier.send(msg)
        st.success("已送出 (Telegram 為背景非同步發送，請至手機確認)")


# ======================================================================
# 頁面: 策略與文件
# ======================================================================


def page_docs() -> None:
    st.title("策略與文件")

    src_files = [
        ("README.md", _project_path("README.md")),
        ("docs/setup.md", _project_path("docs", "setup.md")),
        ("docs/architecture.md", _project_path("docs", "architecture.md")),
        ("docs/operations.md", _project_path("docs", "operations.md")),
        ("docs/profit-plan.md", _project_path("docs", "profit-plan.md")),
        ("docs/trading-rules.md", _project_path("docs", "trading-rules.md")),
    ]
    src_files = [(n, p) for n, p in src_files if p.exists()]

    tab_doc, tab_strategy, tab_modules = st.tabs(["文件", "策略原始碼", "模組總覽"])

    with tab_doc:
        if not src_files:
            st.info("沒有找到文件。")
        else:
            pick = st.selectbox("選擇文件", [n for n, _ in src_files])
            p = dict(src_files)[pick]
            st.markdown(p.read_text(encoding="utf-8"))

    with tab_strategy:
        sp = _project_path("src", "bot", "strategy.py")
        if sp.exists():
            st.caption(f"`{sp.relative_to(PROJECT_ROOT)}`")
            st.code(sp.read_text(encoding="utf-8"), language="python")
        else:
            st.warning("找不到 strategy.py")

    with tab_modules:
        modules = [
            ("main.py", "程式進入點 (多模式分流 + 策略切換)"),
            ("config.py", "組態管理 (pydantic-settings + .env)"),
            ("broker.py", "Shioaji 連線管理 (登入/行情/下單/斷線重連)"),
            ("strategy.py", "策略引擎 (BaseStrategy + MyStrategy)"),
            ("strategy_etf_follow.py", "主動 ETF 共識跟單策略"),
            ("models.py", "資料模型 (PositionInfo, MarketTick, SignalEvent)"),
            ("market_source.py", "TWSE 公開延遲行情來源"),
            ("signal_recorder.py", "訊號記錄與報表匯出 (watch/report)"),
            ("recorder.py", "交易紀錄收集與 CSV 匯出 (trade)"),
            ("notifier.py", "Telegram 推播通知"),
            ("utils.py", "工具函數 (Logger, 時間)"),
            ("active_etf.py", "主動式 ETF 清單與持股資料模型"),
            ("etf_consensus.py", "共識持股 / 加碼 / 抬轎候選計算"),
            ("mops_scraper.py", "MOPS 法說會 / 重大訊息爬蟲"),
            ("llm_analyzer.py", "Gemini LLM 法說會語意解析 + 邏輯反查"),
            ("prompt_registry.py", "Prompt YAML 載入 / 渲染 / 儲存"),
            ("llm_log.py", "LLM 呼叫 JSONL 紀錄"),
            ("etf_holdings_fetcher.py", "ETF 持股自動抓取 (HTTP + LLM 抽取)"),
            ("chips_fetcher.py", "TWSE 籌碼面自動拉取"),
            ("data_pipeline.py", "自動化研究管線編排器"),
            ("auto_research.py", "stock-auto-research CLI 進入點"),
            ("env_io.py", "儀表板的 .env 讀寫工具"),
            ("process_runner.py", "儀表板的 bot 子行程管理"),
            ("dashboard.py", "本儀表板"),
        ]
        st.dataframe(
            pd.DataFrame(modules, columns=["檔案", "說明"]),
            hide_index=True,
            use_container_width=True,
        )


# ======================================================================
# 頁面: 主動 ETF 追蹤
# ======================================================================


def page_etf_tracker() -> None:
    st.title("主動式 ETF 追蹤")
    st.caption("依 2025-2026 公開資訊整理的主動式 ETF 清單與持股快照")

    etfs = load_active_etfs(PROJECT_ROOT)
    st.markdown(f"### 共 **{len(etfs)}** 檔主動式 ETF")

    df_meta = pd.DataFrame([
        {
            "代號": e.symbol, "名稱": e.name, "投信": e.issuer,
            "地區": e.region, "配息": e.freq,
            "持股 URL": e.holdings_url or "—",
        } for e in etfs
    ])

    holdings_meta = []
    for e in etfs:
        dates = list_holdings_dates(e.symbol, PROJECT_ROOT)
        df_meta_count = len(dates)
        holdings_meta.append({
            "代號": e.symbol,
            "快照數": df_meta_count,
            "最新快照": dates[0].isoformat() if dates else "—",
        })
    meta_df = pd.merge(
        df_meta, pd.DataFrame(holdings_meta), on="代號", how="left",
    )

    m1, m2, m3 = st.columns(3)
    m1.metric("已建立快照的 ETF", int((meta_df["快照數"] > 0).sum()))
    m2.metric("總快照數", int(meta_df["快照數"].fillna(0).sum()))
    m3.metric("最新更新日", meta_df["最新快照"].max() or "—")

    st.dataframe(meta_df, hide_index=True, use_container_width=True)

    st.markdown("---")
    st.markdown("### ETF 持股快照管理")
    col_sel, col_dt = st.columns([1, 1])
    sym_options = [f"{e.symbol} {e.name}" for e in etfs]
    sel = col_sel.selectbox("選擇 ETF", sym_options, key="etf_pick")
    sym = sel.split(" ")[0]
    dates = list_holdings_dates(sym, PROJECT_ROOT)
    if dates:
        date_pick = col_dt.selectbox(
            "選擇快照日期",
            [d.isoformat() for d in dates],
            key="etf_date_pick",
        )
        snap = load_holdings(sym, dt.date.fromisoformat(date_pick), PROJECT_ROOT)
    else:
        col_dt.info("尚未匯入此 ETF 的持股快照")
        snap = None

    if snap and snap.holdings:
        st.markdown(f"#### {sym} {date_pick} 持股 ({len(snap.holdings)} 檔)")
        holding_df = pd.DataFrame([
            {"代號": h.ticker, "名稱": h.name, "權重(%)": h.weight_pct,
             "張數": h.shares, "市值": h.value}
            for h in snap.holdings
        ])
        st.dataframe(holding_df, hide_index=True, use_container_width=True)
        if "權重(%)" in holding_df.columns and len(holding_df):
            top10 = holding_df.nlargest(10, "權重(%)")
            st.markdown("#### 前 10 大持股權重")
            st.bar_chart(top10.set_index("代號")["權重(%)"])
    else:
        st.info("此 ETF 尚無對應日期的持股檔。可在下方匯入 / 編輯。")

    with st.expander("自動從投信網頁抓持股 (🤖 使用 Gemini 抽取)", expanded=False):
        st.caption(
            "於下方填入每一檔 ETF 的官方持股頁 URL (或留空關閉)，"
            "按「🤖 立即抓取所有」會：HTTP 下載 → 轉純文字 → **呼叫 Gemini "
            "`extract_etf_holdings` prompt (消耗 LLM)** → 存為當日 CSV。"
        )
        edited_rows = []
        for e in etfs:
            cols = st.columns([1, 2, 4])
            cols[0].write(f"**{e.symbol}**")
            cols[1].write(e.name)
            url = cols[2].text_input(
                "URL", value=e.holdings_url, key=f"url_{e.symbol}",
                label_visibility="collapsed",
                placeholder="https://投信網站/該 ETF 持股頁",
            )
            edited_rows.append((e, url))

        cu, cf = st.columns(2)
        if cu.button("儲存 URL 設定", key="save_urls"):
            new_list = []
            for e, url in edited_rows:
                new_list.append(ActiveEtf(
                    symbol=e.symbol, name=e.name, issuer=e.issuer,
                    region=e.region, freq=e.freq, holdings_url=url.strip(),
                ))
            p = save_active_etfs(new_list, PROJECT_ROOT)
            st.success(f"已儲存到 {p}")
            st.rerun()

        if cf.button(
            "🤖 立即抓取所有 (有 URL 的)",
            type="primary",
            key="fetch_all",
            help=(
                "對每檔有 URL 的 ETF 逐一呼叫 Gemini `extract_etf_holdings` "
                "從 HTML 抽出結構化持股 JSON。每檔約 1 次 LLM call。"
                "\n\n" + LLM_HINT_DIRECT
            ),
        ):
            env_values = load_env()
            api_key = env_values.get("GEMINI_API_KEY", "")
            model = env_values.get("GEMINI_MODEL", "gemini-2.5-flash")
            if not api_key:
                st.error("請先在「組態設定」填入 GEMINI_API_KEY")
            else:
                from bot.etf_holdings_fetcher import fetch_all_active_etfs
                from bot.llm_analyzer import GeminiClient

                client = GeminiClient(api_key=api_key, model=model)
                status = st.status("抓取 ETF 持股", expanded=True)
                status.write(f"準備處理 {len(etfs_with_url)} 檔有 URL 的 ETF。")
                status.write("每檔會下載來源頁面，並呼叫 Gemini 抽取結構化持股。")
                try:
                    results = fetch_all_active_etfs(
                        client, root=PROJECT_ROOT,
                    )
                    status.update(
                        label=f"ETF 持股抓取完成：{len(results)} 檔",
                        state="complete",
                        expanded=False,
                    )
                except Exception as exc:
                    status.update(label="ETF 持股抓取失敗", state="error", expanded=True)
                    raise exc
                rows = [{
                    "代號": r.etf.symbol,
                    "名稱": r.etf.name,
                    "成功": r.success,
                    "持股數": r.holdings_count,
                    "錯誤": r.error,
                    "存檔": str(r.saved_path) if r.saved_path else "",
                } for r in results]
                st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    with st.expander("匯入 / 編輯持股 CSV", expanded=False):
        st.caption(
            "CSV 欄位: `ticker,name,weight_pct,shares,value`，"
            "存於 `data/etf_holdings/<symbol>/<YYYY-MM-DD>.csv`"
        )
        new_date = st.date_input("快照日期", value=dt.date.today())
        uploaded = st.file_uploader("上傳 CSV", type=["csv"], key="etf_upload")
        if uploaded is not None and st.button("儲存到快照", key="etf_save_csv"):
            try:
                df_in = pd.read_csv(uploaded, encoding="utf-8-sig")
            except Exception:
                uploaded.seek(0)
                df_in = pd.read_csv(uploaded, encoding="utf-8")
            holdings: List[Holding] = []
            for _, row in df_in.iterrows():
                holdings.append(Holding(
                    ticker=str(row.get("ticker", "")).strip(),
                    name=str(row.get("name", "")),
                    weight_pct=float(row.get("weight_pct", 0) or 0),
                    shares=float(row.get("shares", 0) or 0),
                    value=float(row.get("value", 0) or 0),
                ))
            snap_new = HoldingsSnapshot(symbol=sym, date=new_date, holdings=holdings)
            p = save_holdings(snap_new, PROJECT_ROOT)
            st.success(f"已儲存 {len(holdings)} 筆到 `{p}`")

        st.markdown("##### 手動快速建立 (粘貼 CSV 文字)")
        sample = "ticker,name,weight_pct,shares,value\n2330,台積電,22.1,500,5000000\n"
        text = st.text_area(
            "貼上 CSV 內容", value="", height=150, placeholder=sample, key="etf_paste",
        )
        if text and st.button("從文字儲存", key="etf_save_text"):
            import io
            df_in = pd.read_csv(io.StringIO(text))
            holdings = [
                Holding(
                    ticker=str(r.get("ticker", "")).strip(),
                    name=str(r.get("name", "")),
                    weight_pct=float(r.get("weight_pct", 0) or 0),
                    shares=float(r.get("shares", 0) or 0),
                    value=float(r.get("value", 0) or 0),
                )
                for _, r in df_in.iterrows()
            ]
            snap_new = HoldingsSnapshot(symbol=sym, date=new_date, holdings=holdings)
            p = save_holdings(snap_new, PROJECT_ROOT)
            st.success(f"已儲存 {len(holdings)} 筆到 `{p}`")


# ======================================================================
# 頁面: 跟單訊號
# ======================================================================


def _load_all_latest_snapshots() -> Dict[str, "HoldingsSnapshot"]:
    etfs = load_active_etfs(PROJECT_ROOT)
    out: Dict[str, HoldingsSnapshot] = {}
    for e in etfs:
        dates = list_holdings_dates(e.symbol, PROJECT_ROOT)
        if not dates:
            continue
        snap = load_holdings(e.symbol, dates[0], PROJECT_ROOT)
        if snap:
            out[e.symbol] = snap
    return out


def _load_prev_snapshots() -> Dict[str, "HoldingsSnapshot"]:
    etfs = load_active_etfs(PROJECT_ROOT)
    out: Dict[str, HoldingsSnapshot] = {}
    for e in etfs:
        dates = list_holdings_dates(e.symbol, PROJECT_ROOT)
        if len(dates) < 2:
            continue
        snap = load_holdings(e.symbol, dates[1], PROJECT_ROOT)
        if snap:
            out[e.symbol] = snap
    return out


def page_follow_signals() -> None:
    st.title("ETF 共識跟單訊號")
    st.caption("跨多檔主動式 ETF 自動偵測共識持股、加碼、新建倉")

    etfs = load_active_etfs(PROJECT_ROOT)
    etf_meta = {e.symbol: e for e in etfs}
    latest = _load_all_latest_snapshots()
    prev = _load_prev_snapshots()

    if not latest:
        st.warning(
            "尚未有任何 ETF 持股快照。請先到「主動 ETF 追蹤」頁匯入至少一檔 ETF 的持股 CSV。"
        )
        return

    col1, col2, col3 = st.columns(3)
    min_consensus = col1.number_input("共識持股 ETF 數門檻", 1, 10, 2)
    min_new = col2.number_input("新建倉 ETF 數門檻", 1, 10, 2)
    min_add = col3.number_input("共識加碼 ETF 數門檻", 1, 10, 3)

    tab_consensus, tab_new, tab_add, tab_changes = st.tabs([
        "共識持股 (橫向)", "共識新建倉", "共識加碼", "持股變動明細",
    ])

    with tab_consensus:
        consensus = build_consensus(latest, etf_meta, min_etf_count=int(min_consensus))
        st.metric("符合門檻的個股數", len(consensus))
        if consensus:
            rows = []
            for c in consensus[:200]:
                rows.append({
                    "個股": c.ticker,
                    "名稱": c.name,
                    "ETF 數": c.etf_count,
                    "合計權重(%)": round(c.total_weight, 2),
                    "平均權重(%)": round(c.avg_weight, 2),
                    "持有 ETF": ", ".join(w.etf_symbol for w in c.held_by),
                })
            df = pd.DataFrame(rows)
            st.dataframe(df, hide_index=True, use_container_width=True)
            top20 = df.head(20)
            st.markdown("#### Top 20 共識持股 (ETF 數)")
            st.bar_chart(top20.set_index("個股")["ETF 數"])
        else:
            st.info("沒有達到門檻的共識持股。")

    with tab_new:
        if not prev:
            st.info("需要至少兩個快照日期才能比較新建倉。")
        else:
            from bot.etf_consensus import diff_snapshots
            changes = []
            for sym, snap_after in latest.items():
                snap_before = prev.get(sym)
                if snap_before:
                    changes.extend(diff_snapshots(snap_before, snap_after))
            news = consensus_new_builds(changes, min_etfs=int(min_new))
            st.metric("共識新建倉訊號", len(news))
            if news:
                rows = [{
                    "個股": s.ticker, "名稱": s.name,
                    "同步建倉 ETF 數": s.etf_count,
                    "合計權重": round(s.total_weight_delta, 2),
                    "建倉 ETF": ", ".join(s.related_etfs),
                    "說明": s.note,
                } for s in news]
                st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
            else:
                st.info("沒有達到門檻的新建倉訊號。")

    with tab_add:
        if not prev:
            st.info("需要至少兩個快照日期才能比較加碼。")
        else:
            from bot.etf_consensus import diff_snapshots
            changes = []
            for sym, snap_after in latest.items():
                snap_before = prev.get(sym)
                if snap_before:
                    changes.extend(diff_snapshots(snap_before, snap_after))
            adds = consensus_additions(changes, min_etfs=int(min_add))
            st.metric("共識加碼訊號", len(adds))
            if adds:
                rows = [{
                    "個股": s.ticker, "名稱": s.name,
                    "同步加碼 ETF 數": s.etf_count,
                    "權重增幅(%)": round(s.total_weight_delta, 2),
                    "加碼 ETF": ", ".join(s.related_etfs),
                } for s in adds]
                st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
            else:
                st.info("沒有達到門檻的加碼訊號。")

    with tab_changes:
        if not prev:
            st.info("需要至少兩個快照日期才能比較持股變動。")
        else:
            from bot.etf_consensus import diff_snapshots
            sel_etf = st.selectbox(
                "選擇 ETF",
                [f"{s} {etf_meta[s].name}" for s in sorted(latest.keys() & prev.keys())],
                key="changes_etf",
            )
            sym = sel_etf.split(" ")[0]
            changes = diff_snapshots(prev[sym], latest[sym])
            if changes:
                rows = [{
                    "個股": c.ticker, "名稱": c.name,
                    "變動類型": c.change_type,
                    "前權重": round(c.weight_pct_before, 2),
                    "新權重": round(c.weight_pct_after, 2),
                    "Δ權重": round(c.weight_delta, 2),
                    "Δ張數": round(c.shares_delta, 0),
                } for c in changes]
                st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
            else:
                st.info("無持股變動。")


# ======================================================================
# 頁面: LLM 法說會分析
# ======================================================================


def _render_auto_research_tab(api_key: str, model: str) -> None:
    """LLM 法說分析頁 → 「🤖 自動研究」分頁的內容。

    使用者只需要輸入個股代號 → 一鍵自動跑完
    (行事曆 + MOPS 重訊 + 鉅亨新聞 + 網頁搜尋 + LLM 結構化分析 + 籌碼面言行反查)。
    """
    st.caption(
        "全自動：行事曆 + MOPS 重大訊息 + 鉅亨新聞 + 上網搜尋 → Gemini 研究 → 籌碼面言行反查"
    )

    from bot.conference_calendar import upcoming_conferences

    upcoming = upcoming_conferences(days=14, root=PROJECT_ROOT)
    if upcoming:
        upcoming_tickers_list = list({e.ticker for e in upcoming if e.ticker})
        st.info(
            f"📅 未來 14 天有 {len(upcoming)} 場法說會 (涵蓋 {len(upcoming_tickers_list)} 檔)，"
            f"代號：{', '.join(upcoming_tickers_list[:20])}"
            + (" ..." if len(upcoming_tickers_list) > 20 else "")
        )
    else:
        st.caption("未來 14 天行事曆快取為空 (可能 MOPS 還沒公告，或行事曆需要重抓)。")

    cc1, cc2, cc3 = st.columns([2, 1, 1])
    tickers_input = cc1.text_input(
        "個股代號 (逗號分隔)",
        value="2330",
        key="auto_research_tickers",
        help="可一次輸入多檔，例如 2330,2317,3231。留白將跑 watchlist 全部。",
    )
    force = cc2.checkbox("強制重跑", value=False, key="auto_research_force",
                        help="略過 12 小時快取，重新打 LLM。")
    include_upcoming = cc3.checkbox(
        "+ 未來 14 天法說股", value=False, key="auto_research_upcoming",
        help="自動加入未來 14 天有法說會的所有個股。",
    )

    btn_run = st.button(
        "🤖 開始自動研究", type="primary",
        disabled=not api_key, key="auto_research_run",
    )
    if btn_run:
        tickers: List[str] = []
        for raw in (tickers_input or "").split(","):
            t = raw.strip()
            if t and t not in tickers:
                tickers.append(t)
        if not tickers:
            try:
                items = wl.load(PROJECT_ROOT).items
                tickers = [i.ticker for i in items if i.ticker]
            except Exception:
                tickers = []
        if include_upcoming:
            for e in upcoming:
                if e.ticker and e.ticker not in tickers:
                    tickers.append(e.ticker)
        if not tickers:
            st.error("沒有 ticker 可分析；請輸入代號或先建立 watchlist。")
            return

        from bot.auto_llm import auto_research_ticker
        from bot.llm_analyzer import GeminiClient

        client = GeminiClient(api_key=api_key, model=model)
        results: List[Dict] = []
        progress = st.progress(
            0.0,
            text=(
                f"準備自動研究 {len(tickers)} 檔：先檢查 12 小時快取，"
                "必要時抓 MOPS / 新聞 / 網頁資料並呼叫 Gemini"
            ),
        )
        for i, ticker in enumerate(tickers, 1):
            progress.progress(
                (i - 1) / max(len(tickers), 1),
                text=(
                    f"[{i}/{len(tickers)}] {ticker}: 檢查快取 → 籌碼摘要 → "
                    "MOPS/新聞/網頁素材 → Gemini 分析"
                ),
            )
            name_hint = ""
            try:
                for it in wl.load(PROJECT_ROOT).items:
                    if it.ticker == ticker:
                        name_hint = it.name
                        break
            except Exception:
                pass
            try:
                res = auto_research_ticker(
                    ticker,
                    root=PROJECT_ROOT,
                    name_hint=name_hint,
                    force_refresh=force,
                    refresh_calendar=False,
                )
            except Exception as e:
                results.append({"ticker": ticker, "error": str(e)})
                continue
            if res is None:
                results.append({"ticker": ticker, "error": "無結果 (素材不足或 LLM 停用)"})
                continue
            row = {
                "ticker": ticker,
                "name": name_hint,
                "sentiment": res.get("sentiment"),
                "score": res.get("sentiment_score"),
                "confidence": res.get("confidence"),
                "catalyst_outlook": res.get("catalyst_outlook", ""),
                "logic_verdict": (res.get("logic_check") or {}).get("verdict", ""),
                "logic_suggestion": (res.get("logic_check") or {}).get("suggestion", ""),
                "_raw": res,
            }
            results.append(row)
        progress.progress(1.0, text=f"完成 {len(results)} 檔")
        st.session_state["auto_research_results"] = results
        st.success(f"完成：{len(results)} 檔已自動研究")

    # ------ 自動載入/還原快取結果 ------
    current_tickers: List[str] = []
    for raw in (tickers_input or "").split(","):
        t = raw.strip()
        if t and t not in current_tickers:
            current_tickers.append(t)
    if not current_tickers:
        try:
            items = wl.load(PROJECT_ROOT).items
            current_tickers = [i.ticker for i in items if i.ticker]
        except Exception:
            current_tickers = []

    results = st.session_state.get("auto_research_results")
    results_tickers = [r["ticker"] for r in results] if results is not None else []
    if results is None or set(results_tickers) != set(current_tickers):
        from bot.auto_llm import load_cached_auto_analysis
        results = []
        for ticker in current_tickers:
            cached = load_cached_auto_analysis(ticker, PROJECT_ROOT, max_age_hours=999999)
            if cached:
                name_hint = cached.get("name", "")
                row = {
                    "ticker": ticker,
                    "name": name_hint,
                    "sentiment": cached.get("sentiment"),
                    "score": cached.get("sentiment_score"),
                    "confidence": cached.get("confidence"),
                    "catalyst_outlook": cached.get("catalyst_outlook", ""),
                    "logic_verdict": (cached.get("logic_check") or {}).get("verdict", ""),
                    "logic_suggestion": (cached.get("logic_check") or {}).get("suggestion", ""),
                    "_raw": cached,
                }
                results.append(row)
        st.session_state["auto_research_results"] = results

    if results:
        st.markdown("### 研究結果")
        df = pd.DataFrame([
            {k: v for k, v in r.items() if not k.startswith("_") and k != "error"}
            | ({"error": r["error"]} if r.get("error") else {})
            for r in results
        ])
        st.dataframe(df, hide_index=True, use_container_width=True)

        for r in results:
            if r.get("error"):
                st.warning(f"{r['ticker']}: {r['error']}")
                continue
            raw = r.get("_raw") or {}
            with st.expander(
                f"🔎 {r['ticker']} {raw.get('name','')} — "
                f"{raw.get('sentiment','?')} "
                f"({raw.get('sentiment_score', 0):+.2f}, "
                f"信心 {raw.get('confidence', 0):.0%})",
                expanded=False,
            ):
                st.markdown(f"**📝 摘要**：{raw.get('summary','(無)')}")
                if raw.get("catalyst_outlook"):
                    st.markdown(f"**🚀 催化劑展望**：{raw['catalyst_outlook']}")
                if raw.get("news_sentiment"):
                    st.markdown(f"**📰 新聞基調**：{raw['news_sentiment']}")

                gc1, gc2 = st.columns(2)
                with gc1:
                    st.markdown("**成長驅動因子**")
                    for d in raw.get("growth_drivers", []) or []:
                        st.write(f"- {d}")
                with gc2:
                    st.markdown("**風險**")
                    for x in raw.get("risks", []) or []:
                        st.write(f"- {x}")

                st.markdown("**關鍵指標**")
                st.json(raw.get("key_metrics", {}) or {})

                st.markdown(
                    f"**Capex 信號:** {raw.get('capex_signal','-')}"
                    f"&nbsp;|&nbsp; **毛利率展望:** {raw.get('margin_outlook','-')}"
                )

                logic = raw.get("logic_check") or {}
                if logic:
                    color = {
                        "consistent": "green",
                        "suspicious_distribution": "red",
                        "suspicious_accumulation": "yellow",
                        "inconclusive": "gray",
                    }.get(logic.get("verdict", ""), "gray")
                    st.markdown(
                        f"**💡 言行反查**：{_badge(logic.get('verdict','-'), color)}"
                        f" → 建議 **{logic.get('suggestion','-').upper()}**"
                        f" (信心 {float(logic.get('confidence', 0)):.0%})",
                        unsafe_allow_html=True,
                    )
                    if logic.get("reasoning"):
                        st.caption(logic["reasoning"])

                sources = raw.get("evidence_sources") or []
                if sources:
                    st.markdown("**🔗 主要資料來源**")
                    for s in sources:
                        st.caption(f"- {s}")

                meta = raw.get("source_materials") or {}
                if meta:
                    st.caption(
                        f"素材：MOPS {meta.get('mops_count', 0)} 條、"
                        f"鉅亨 {meta.get('news_count', 0)} 條、"
                        f"網頁搜尋 {meta.get('web_search_results', 0)} 筆、"
                        f"抓 {meta.get('web_pages_fetched', 0)} 篇原文、"
                        f"行事曆未來 {meta.get('upcoming_conferences', 0)}+"
                        f"過去 {meta.get('past_conferences', 0)} 場 "
                        f"｜ prompt={raw.get('prompt_id','')} v{raw.get('prompt_version','')}"
                        f"｜ 更新於 {raw.get('fetched_at','')}"
                    )


def page_llm_analysis() -> None:
    st.title("LLM 法說會分析 / 邏輯反查")
    _llm_caption(
        "全自動：抓 MOPS 法說會行事曆 + 上網搜尋 (DuckDuckGo + Google News) + 鉅亨新聞 "
        "→ Gemini 結構化研究 → 籌碼面言行反查。"
        "本頁進入時會自動更新行事曆快取 (一日一次)。"
    )

    env_values = load_env()
    api_key = env_values.get("GEMINI_API_KEY", "")
    model = env_values.get("GEMINI_MODEL", "gemini-2.5-flash")

    try:
        from bot.conference_calendar import (
            ensure_calendar_fresh,
            last_refresh_at,
        )
        ensure_calendar_fresh(root=PROJECT_ROOT)
        cal_last = last_refresh_at(PROJECT_ROOT)
    except Exception:
        cal_last = ""

    c1, c2, c3 = st.columns(3)
    c1.markdown(
        f"Gemini API Key: {_badge('已設定', 'green') if api_key else _badge('未設定', 'gray')}",
        unsafe_allow_html=True,
    )
    c2.markdown(f"模型: `{model}`")
    c3.markdown(f"行事曆快取: `{cal_last or '尚未更新'}`")

    if not api_key:
        st.warning(
            "請先到「組態設定」頁填入 `GEMINI_API_KEY`。"
            "可在 https://aistudio.google.com 免費取得。"
        )

    tab_auto, tab_paste, tab_mops, tab_logic = st.tabs([
        "🤖 自動研究 (推薦)",
        "貼文字分析",
        "MOPS 行事曆 / 重大訊息",
        "言行反查",
    ])

    with tab_auto:
        _render_auto_research_tab(api_key, model)

    with tab_paste:
        # ------ 自動載入貼文字分析結果 ------
        cached_paste = None
        if "last_llm_analysis" not in st.session_state:
            cached_paste = _load_paste_analysis()
            if cached_paste:
                st.session_state["last_llm_analysis"] = cached_paste["analysis"]
                st.session_state["llm_ticker"] = cached_paste["analysis"].ticker
                st.session_state["llm_text"] = cached_paste["text"]

        ticker = st.text_input("股票代號", value="2330", key="llm_ticker")
        text = st.text_area(
            "貼上法說會逐字稿 / 簡報文字",
            height=300,
            placeholder="例：本季營收 850 億美元，年增 35%，下季毛利率指引 56-58% ...",
            key="llm_text",
        )
        run = st.button(
            "🤖 用 Gemini 分析",
            type="primary",
            disabled=not (api_key and text),
            help=LLM_HINT_DIRECT,
        )
        if run:
            from bot.llm_analyzer import GeminiClient, analyze_presentation
            client = GeminiClient(api_key=api_key, model=model)
            with st.spinner("Gemini 正在解析貼上的法說內容，並輸出結構化 JSON..."):
                analysis = analyze_presentation(text, ticker=ticker, client=client)
            st.session_state["last_llm_analysis"] = analysis
            _save_paste_analysis(analysis, text)

        analysis = st.session_state.get("last_llm_analysis")
        if analysis:
            c1, c2, c3 = st.columns(3)
            c1.metric("情緒", analysis.sentiment)
            c2.metric("情緒分數", f"{analysis.sentiment_score:+.2f}")
            c3.metric("置信度", f"{analysis.confidence:.0%}")

            st.markdown("### 摘要")
            st.write(analysis.summary or "(無)")

            cc1, cc2 = st.columns(2)
            with cc1:
                st.markdown("**成長驅動因子**")
                for d in analysis.growth_drivers:
                    st.write(f"- {d}")
            with cc2:
                st.markdown("**風險**")
                for r in analysis.risks:
                    st.write(f"- {r}")

            st.markdown("**關鍵指標**")
            st.json(analysis.key_metrics)

            st.markdown(
                f"**Capex 信號:** {analysis.capex_signal} "
                f"&nbsp;|&nbsp; **毛利率展望:** {analysis.margin_outlook}"
            )

            with st.expander("Gemini 原始回應"):
                st.code(analysis.raw_response or "(無)", language="json")

    with tab_mops:
        st.caption(
            "MOPS 法說會行事曆 (自動快取一日一次 + 個股重大訊息)。"
            "「自動研究」分頁會直接吃這些快取，不需要手動觸發。"
        )
        sub_tab_conf, sub_tab_mat = st.tabs(["法說會行事曆 (自動快取)", "個股重大訊息"])

        with sub_tab_conf:
            from bot.conference_calendar import (
                load_calendar,
                recent_conferences,
                update_calendar,
                upcoming_conferences,
            )
            cc1, cc2, cc3 = st.columns([1, 1, 2])
            days_up = cc1.number_input("未來 N 天", 1, 60, 14, key="cal_up_days")
            days_back = cc2.number_input("過去 N 天", 1, 90, 14, key="cal_back_days")
            with cc3:
                if st.button("🔄 立即重抓 (上月/本月/下月/+2)", key="cal_refresh"):
                    with st.spinner("自 MOPS 抓最新行事曆…"):
                        summary = update_calendar(root=PROJECT_ROOT)
                    st.success(f"完成: {summary}")
                    st.rerun()

            up = upcoming_conferences(days=int(days_up), root=PROJECT_ROOT)
            back = recent_conferences(days=int(days_back), root=PROJECT_ROOT)
            all_items = load_calendar(root=PROJECT_ROOT)

            st.markdown(f"#### 🔮 未來 {int(days_up)} 天 ({len(up)} 場)")
            if up:
                df_up = pd.DataFrame([
                    {"日期": e.date.isoformat(), "時間": e.time,
                     "代號": e.ticker, "公司": e.company, "備註": e.note}
                    for e in up
                ])
                st.dataframe(df_up, hide_index=True, use_container_width=True)
            else:
                st.info("未來窗口內目前沒有法說會。")

            st.markdown(f"#### 🕘 過去 {int(days_back)} 天 ({len(back)} 場)")
            if back:
                df_back = pd.DataFrame([
                    {"日期": e.date.isoformat(), "時間": e.time,
                     "代號": e.ticker, "公司": e.company, "備註": e.note}
                    for e in back
                ])
                st.dataframe(df_back, hide_index=True, use_container_width=True)
            else:
                st.info("過去窗口內沒有法說會紀錄。")

            with st.expander(f"完整快取共 {len(all_items)} 場 (含 ±2 個月)"):
                if all_items:
                    df_all = pd.DataFrame([
                        {"日期": e.date.isoformat(), "時間": e.time,
                         "代號": e.ticker, "公司": e.company, "備註": e.note}
                        for e in all_items
                    ])
                    st.dataframe(df_all, hide_index=True, use_container_width=True)
                else:
                    st.info("尚無任何快取。點上方「立即重抓」即可。")

        with sub_tab_mat:
            mt = st.text_input("股票代號", value="2330", key="mops_mat_ticker")
            if st.button("抓取重大訊息", key="mops_mat_fetch"):
                from bot.mops_scraper import fetch_material_info
                with st.spinner(f"自 MOPS 抓取 {mt} 的重大訊息..."):
                    materials = fetch_material_info(mt)
                if materials:
                    df = pd.DataFrame([
                        {"日期": m.date.isoformat(), "時間": m.time,
                         "代號": m.ticker, "主旨": m.subject}
                        for m in materials
                    ])
                    st.dataframe(df, hide_index=True, use_container_width=True)
                else:
                    st.info("查無資料。")

    with tab_logic:
        st.caption("用『法說會語意』+『籌碼面』交叉檢驗管理階層是否言行一致")
        last_analysis = st.session_state.get("last_llm_analysis")
        if last_analysis is None:
            st.info("請先在『貼文字分析』頁完成一場法說會分析。")
        else:
            # 確保 session state 中的反查結果是針對當前分析的 ticker
            if "last_logic_check_result" in st.session_state:
                if st.session_state["last_logic_check_result"].ticker != last_analysis.ticker:
                    st.session_state.pop("last_logic_check_result", None)
                    st.session_state.pop("auto_chips_summary", None)

            cached_logic = None
            if "last_logic_check_result" not in st.session_state:
                cached_logic = _load_logic_check(last_analysis.ticker)
                if cached_logic:
                    st.session_state["last_logic_check_result"] = cached_logic["result"]

            st.write(
                f"基於最近一次分析: **{last_analysis.ticker}** "
                f"(sentiment={last_analysis.sentiment} {last_analysis.sentiment_score:+.2f})"
            )

            auto_fetch = st.button(
                f"自動抓 {last_analysis.ticker} 籌碼面 (近 5 日 TWSE)",
                key="auto_chips",
            )
            if auto_fetch:
                from bot.chips_fetcher import build_chip_summary
                with st.spinner("自 TWSE 抓近 5 日籌碼，並整理法人/融資融券摘要..."):
                    s = build_chip_summary(
                        last_analysis.ticker, days=5, root=PROJECT_ROOT,
                    )
                st.session_state["auto_chips_summary"] = s
                st.success(
                    f"完成。外資 {s.foreign_net:+.0f} 張、投信 {s.investment_trust_net:+.0f}、"
                    f"借券變動 {s.short_borrow_change_pct:+.1f}%"
                )

            auto_s = st.session_state.get("auto_chips_summary")
            chips_source = auto_s
            if chips_source is None and cached_logic:
                chips_source = cached_logic["chips"]

            cc1, cc2 = st.columns(2)
            with cc1:
                foreign = st.number_input(
                    "外資近期淨買超 (張)",
                    value=float(chips_source.foreign_net) if chips_source else 0.0,
                    step=100.0,
                )
                trust = st.number_input(
                    "投信淨買超 (張)",
                    value=float(chips_source.investment_trust_net) if chips_source else 0.0,
                    step=100.0,
                )
                dealer = st.number_input(
                    "自營商淨買超 (張)",
                    value=float(chips_source.dealer_net) if chips_source else 0.0,
                    step=100.0,
                )
            with cc2:
                margin = st.number_input(
                    "融資餘額變動 (%)",
                    value=float(chips_source.margin_buy_change_pct) if chips_source else 0.0,
                    step=1.0,
                )
                short_b = st.number_input(
                    "借券賣出餘額變動 (%)",
                    value=float(chips_source.short_borrow_change_pct) if chips_source else 0.0,
                    step=1.0,
                )
                block = st.number_input(
                    "鉅額交易淨額 (張)",
                    value=float(chips_source.block_trade_net) if chips_source else 0.0,
                    step=100.0,
                )
            note = st.text_input("備註", "")
            if st.button(
                "🤖 執行反查",
                type="primary",
                help=(
                    "對比『法說會語意分數』與『籌碼面方向』找言行不一致 (出貨/吸籌)。"
                    "有設 GEMINI_API_KEY 會走 Gemini；沒設則退回純規則式 (不消耗 LLM)。"
                    "\n\n" + LLM_HINT_DIRECT
                ),
            ):
                from bot.llm_analyzer import ChipsContext, GeminiClient, logic_check
                chips = ChipsContext(
                    foreign_net=foreign,
                    investment_trust_net=trust,
                    dealer_net=dealer,
                    margin_buy_change_pct=margin,
                    short_borrow_change_pct=short_b,
                    block_trade_net=block,
                    notes=note,
                )
                client = GeminiClient(api_key=api_key, model=model) if api_key else None
                with st.spinner("用籌碼摘要反查 LLM 結論一致性..."):
                    result = logic_check(last_analysis, chips, client)
                st.session_state["last_logic_check_result"] = result
                _save_logic_check(last_analysis.ticker, chips, result)

            result = st.session_state.get("last_logic_check_result")
            if result:
                verdict_color = {
                    "consistent": "green",
                    "suspicious_distribution": "red",
                    "suspicious_accumulation": "yellow",
                    "inconclusive": "gray",
                }.get(result.verdict, "gray")
                st.markdown(
                    f"### 判定: {_badge(result.verdict, verdict_color)}",
                    unsafe_allow_html=True,
                )
                cc1, cc2 = st.columns(2)
                cc1.metric("置信度", f"{result.confidence:.0%}")
                cc2.metric("建議", result.suggestion.upper())
                st.write(result.reasoning)


# ======================================================================
# 頁面: 自動化管線
# ======================================================================


def page_pipeline() -> None:
    st.title("自動化研究管線")
    st.caption(
        "一鍵完成：ETF 持股自動抓取 → 共識計算 → 籌碼面拉取 → 法說 LLM 解析 → "
        "言行反查 → 每日簡報"
    )
    _llm_caption(
        "下方 toggle 中 **`LLM 法說分析`** 與 **`產出每日簡報`** 打開時，"
        "「🤖 立即執行管線」會直接呼叫 Gemini。可在 toggle 全關情況下只跑 ETF/籌碼，"
        "完全不消耗 LLM。"
    )

    env_values = load_env()
    api_key = env_values.get("GEMINI_API_KEY", "")
    model = env_values.get("GEMINI_MODEL", "gemini-2.5-flash")

    c1, c2, c3 = st.columns(3)
    c1.markdown(
        f"Gemini API Key: {_badge('已設定', 'green') if api_key else _badge('未設定', 'red')}",
        unsafe_allow_html=True,
    )
    c2.markdown(f"模型: `{model}`")
    etfs_with_url = [e for e in load_active_etfs(PROJECT_ROOT) if e.holdings_url]
    c3.metric("有 URL 的 ETF", f"{len(etfs_with_url)}")

    st.markdown("### 管線設定")
    a, b, c, d = st.columns(4)
    fetch_etf = a.toggle(
        "抓 ETF 持股",
        value=env_values.get("PIPELINE_FETCH_ETF", "true").lower() == "true",
    )
    fetch_chips = b.toggle(
        "抓籌碼面",
        value=env_values.get("PIPELINE_FETCH_CHIPS", "true").lower() == "true",
    )
    run_llm = c.toggle("LLM 法說分析", value=bool(api_key))
    gen_brief = d.toggle(
        "產出每日簡報",
        value=env_values.get("PIPELINE_GENERATE_BRIEF", "true").lower() == "true",
    )

    e1, e2, e3 = st.columns(3)
    days = e1.number_input(
        "籌碼面回顧天數",
        2, 20,
        int(env_values.get("PIPELINE_CHIP_LOOKBACK_DAYS", "5") or 5),
    )
    min_cons = e2.number_input(
        "共識焦點門檻 (ETF 數)",
        1, 10,
        int(env_values.get("PIPELINE_MIN_CONSENSUS", "2") or 2),
    )
    extra_focus = e3.text_input(
        "額外焦點 (逗號分隔)", value="", placeholder="2330,2382",
    )

    st.markdown("##### 附加法說會輸入 (可選)")
    pres_ticker = st.text_input(
        "ticker", value="", placeholder="2330", key="pipe_pres_ticker",
    )
    pres_text = st.text_area(
        "法說會逐字稿 / 簡報內容",
        height=180,
        placeholder="可貼一份重要法說會的內容；留空則跳過此步驟",
        key="pipe_pres_text",
    )

    will_call_llm = (bool(run_llm) or bool(gen_brief)) and bool(api_key)
    btn_label = (
        "🤖 立即執行管線 (含 LLM 呼叫)"
        if will_call_llm
        else "立即執行管線 (本次不呼叫 LLM)"
    )
    run_now = st.button(
        btn_label,
        type="primary",
        use_container_width=True,
        help=(
            LLM_HINT_DIRECT
            if will_call_llm
            else "目前 LLM 相關 toggle 都已關閉，本次不會呼叫 Gemini。"
        ),
    )
    if run_now:
        from bot.data_pipeline import (
            PipelineConfig,
            PresentationInput,
            run_full_pipeline,
        )

        focus = [s.strip() for s in extra_focus.split(",") if s.strip()]
        presentations: List[PresentationInput] = []
        if pres_ticker.strip() and pres_text.strip():
            presentations.append(PresentationInput(
                ticker=pres_ticker.strip(),
                text=pres_text,
                source="dashboard",
                label="manual_paste",
            ))

        config = PipelineConfig(
            project_root=PROJECT_ROOT,
            gemini_api_key=api_key,
            gemini_model=model,
            focus_tickers=focus,
            chip_lookback_days=int(days),
            fetch_etf_holdings=bool(fetch_etf),
            fetch_chips=bool(fetch_chips),
            run_llm_analysis=bool(run_llm) and bool(api_key),
            generate_brief=bool(gen_brief) and bool(api_key),
            min_consensus_for_focus=int(min_cons),
            presentation_inputs=presentations,
        )

        planned_steps = []
        if fetch_etf:
            planned_steps.append("抓 ETF 持股並更新本地快取")
        if fetch_chips:
            planned_steps.append(f"抓籌碼面近 {int(days)} 日")
        planned_steps.append("計算 ETF 共識與焦點股")
        if config.run_llm_analysis:
            planned_steps.append("呼叫 Gemini 做法說分析")
        if config.generate_brief:
            planned_steps.append("呼叫 Gemini 產出每日簡報")
        if presentations:
            planned_steps.append(f"處理手動貼上的法說輸入 {len(presentations)} 筆")

        status = st.status("研究管線執行中", expanded=True)
        status.write("本次步驟：" + " → ".join(planned_steps))
        status.write("後端正在依序執行；完成後會寫入 `data/pipeline_runs/`。")
        try:
            run = run_full_pipeline(config)
            status.update(
                label=f"研究管線完成：run_id={run.run_id}",
                state="complete",
                expanded=False,
            )
        except Exception as exc:
            status.update(label="研究管線執行失敗", state="error", expanded=True)
            raise exc
        st.success(
            f"完成! run_id={run.run_id} 耗時 {run.duration_sec:.1f}s "
            f"焦點 {len(run.focus_tickers)} 檔，錯誤 {len(run.errors)} 筆"
        )
        st.session_state["last_pipeline_run_id"] = run.run_id

    st.markdown("---")
    st.markdown("### 歷史執行紀錄")
    from bot.data_pipeline import list_pipeline_runs, load_pipeline_run
    history = list_pipeline_runs(PROJECT_ROOT)
    if not history:
        st.info("尚無執行紀錄。設定好參數後按「立即執行管線」即可。")
        return

    df = pd.DataFrame(history)
    if "run_id" in df.columns:
        st.dataframe(df, hide_index=True, use_container_width=True)
    run_ids = [h["run_id"] for h in history]
    default_id = st.session_state.get("last_pipeline_run_id", run_ids[0])
    idx = run_ids.index(default_id) if default_id in run_ids else 0
    pick = st.selectbox("檢視 run", run_ids, index=idx, key="pipe_pick")

    run = load_pipeline_run(PROJECT_ROOT, pick)
    if run is None:
        st.warning("找不到 run 內容")
        return

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("焦點個股", len(run.focus_tickers))
    m2.metric("法說會分析", len(run.presentation_analyses))
    m3.metric("ETF 抓取成功", run.etf_fetch_summary.get("success", 0))
    m4.metric("錯誤", len(run.errors))

    t_brief, t_etf, t_cons, t_pres, t_logic, t_chip, t_err = st.tabs([
        "每日簡報", "ETF 抓取", "共識/訊號", "法說分析", "言行反查",
        "籌碼面", "錯誤",
    ])

    with t_brief:
        if run.daily_brief_md:
            st.caption(
                f"prompt: {run.daily_brief_prompt_id} v{run.daily_brief_prompt_version}"
            )
            st.markdown(run.daily_brief_md)
            st.download_button(
                "下載 daily_brief.md",
                data=run.daily_brief_md.encode("utf-8"),
                file_name=f"daily_brief_{pick}.md",
            )
        else:
            st.info("此次未產出簡報 (可能 LLM 未啟用或產生失敗)")

    with t_etf:
        details = run.etf_fetch_summary.get("details", [])
        if details:
            st.dataframe(pd.DataFrame(details), hide_index=True, use_container_width=True)
        else:
            st.info("(略過或無資料)")

    with t_cons:
        st.markdown("#### 共識持股 Top 20")
        if run.consensus_top:
            st.dataframe(pd.DataFrame(run.consensus_top[:20]), hide_index=True, use_container_width=True)
        st.markdown("#### 共識新建倉")
        if run.new_build_signals:
            st.dataframe(pd.DataFrame(run.new_build_signals), hide_index=True, use_container_width=True)
        st.markdown("#### 共識加碼")
        if run.add_signals:
            st.dataframe(pd.DataFrame(run.add_signals), hide_index=True, use_container_width=True)

    with t_pres:
        if run.presentation_analyses:
            for a in run.presentation_analyses:
                with st.expander(f"{a.get('ticker','-')} — {a.get('label','')}"):
                    st.write(f"sentiment: **{a.get('sentiment')}** ({a.get('sentiment_score'):+.2f}), confidence: {a.get('confidence'):.0%}")
                    st.write(a.get("summary", ""))
                    st.json({k: a[k] for k in ("growth_drivers","risks","key_metrics","capex_signal","margin_outlook") if k in a})
        else:
            st.info("(無法說會分析)")

    with t_logic:
        if run.logic_checks:
            st.dataframe(pd.DataFrame(run.logic_checks), hide_index=True, use_container_width=True)
        else:
            st.info("(無)")

    with t_chip:
        if run.chip_summaries:
            cols = ["ticker", "days", "foreign_net", "investment_trust_net",
                    "dealer_net", "margin_buy_change_pct", "short_borrow_change_pct",
                    "block_trade_net"]
            rows = [{k: c.get(k) for k in cols} for c in run.chip_summaries]
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        else:
            st.info("(略)")

    with t_err:
        if run.errors:
            for e in run.errors:
                st.error(e)
        else:
            st.success("無錯誤")


# ======================================================================
# 頁面: Prompt 管理
# ======================================================================


def page_prompts() -> None:
    st.title("Prompt 管理")
    st.caption(
        "所有 LLM 呼叫使用的 Prompt 都集中在 `prompts/*.yaml` 中。"
        "於此修改後立即生效，並會同步寫入 Git 控管的檔案。"
    )
    st.success(
        "✅ **此頁不會呼叫 LLM** — 編輯、儲存、Render 預覽都是本機操作，"
        "不會消耗 Gemini API 額度。",
        icon="✅",
    )

    reg = get_registry(PROJECT_ROOT / "prompts")
    ids = reg.list_ids()

    c1, c2 = st.columns([3, 1])
    c1.metric("Prompt 數", len(ids))
    if c2.button("重新載入"):
        reg.reload()
        st.toast("已重新載入 Prompt", icon="✅")
        st.rerun()

    if not ids:
        st.warning(
            f"prompts 目錄找不到任何 YAML：{PROJECT_ROOT / 'prompts'}"
        )
        return

    # 概覽表
    rows = []
    for pid in ids:
        p = reg.get(pid)
        if p is None:
            continue
        rows.append({
            "id": p.id,
            "version": p.version,
            "model": p.model_hint,
            "max_tokens": p.max_output_tokens,
            "輸入欄位": ", ".join(i.name for i in p.inputs),
            "說明": p.description,
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    st.markdown("---")
    pid = st.selectbox("選擇 Prompt", ids, key="prompt_pick")
    p = reg.get(pid)
    if p is None:
        return

    st.markdown(f"### {p.id} (v{p.version})")
    st.caption(p.description)

    yaml_text = ""
    if p.path and p.path.exists():
        yaml_text = p.path.read_text(encoding="utf-8")
    else:
        yaml_text = p.to_yaml_text()

    edited = st.text_area(
        "YAML 原文 (修改後按下方儲存即可生效)",
        value=yaml_text,
        height=500,
        key=f"prompt_yaml_{pid}",
    )

    csave, ctest = st.columns(2)
    if csave.button("儲存", type="primary", key=f"save_{pid}"):
        try:
            path = reg.save_yaml(pid, edited)
            st.success(f"已儲存到 {path}，並已重新載入")
        except Exception as e:
            st.error(f"儲存失敗: {e}")

    if ctest.button("Render 預覽", key=f"render_{pid}"):
        try:
            sample_vars = {}
            for inp in p.inputs:
                sample_vars[inp.name] = f"<{inp.name}>"
            preview = p.render(**sample_vars)
            st.code(preview, language="text")
        except Exception as e:
            st.error(f"render 失敗: {e}")


# ======================================================================
# 頁面: LLM 呼叫紀錄
# ======================================================================


def page_llm_log() -> None:
    st.title("LLM 呼叫紀錄")
    st.caption(
        "所有透過 `gemini_call()` 與 `analyze_presentation()` / `logic_check()` 等 "
        "API 發出的 LLM 請求都會落地於 `log/llm_calls/*.jsonl`。"
    )
    st.success(
        "✅ **此頁不會呼叫 LLM** — 只讀取歷史 JSONL 紀錄。"
        "可在這裡審計 / 估算每日 token 消耗。",
        icon="✅",
    )

    log = get_call_logger()
    dates = log.list_dates()
    if not dates:
        st.info("尚無 LLM 呼叫紀錄。執行管線或法說分析後即會出現。")
        return

    c1, c2, c3 = st.columns([1, 1, 2])
    date_pick = c1.selectbox("日期", [d.isoformat() for d in dates], key="llm_date")
    limit = c2.number_input("最多顯示筆數", 10, 5000, 200, step=10)
    flt_id = c3.text_input("過濾 prompt_id (留空全部)", value="", key="llm_filter")

    records = log.read(dt.date.fromisoformat(date_pick), limit=int(limit))
    if flt_id:
        records = [r for r in records if flt_id in r.prompt_id]

    if not records:
        st.info("此日無紀錄")
        return

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("總筆數", len(records))
    m2.metric("成功", sum(1 for r in records if r.success))
    m3.metric("失敗", sum(1 for r in records if not r.success))
    avg_latency = sum(r.latency_ms for r in records) / max(1, len(records))
    m4.metric("平均延遲", f"{avg_latency:.0f} ms")

    rows = []
    for r in records:
        rows.append({
            "時間": r.ts,
            "prompt_id": r.prompt_id,
            "版本": r.prompt_version,
            "模型": r.model,
            "延遲(ms)": r.latency_ms,
            "tokens_in": r.tokens_in,
            "tokens_out": r.tokens_out,
            "成功": r.success,
            "error": r.error[:80] if r.error else "",
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    st.markdown("---")
    st.markdown("### 檢視單筆")
    idx_options = [
        f"{i}: {r.ts} {r.prompt_id} v{r.prompt_version}"
        for i, r in enumerate(records)
    ]
    pick = st.selectbox("選擇一筆", idx_options, key="llm_call_pick")
    idx = int(pick.split(":")[0])
    r = records[idx]

    tabs = st.tabs(["Output", "Input (rendered prompt)", "Metadata"])
    with tabs[0]:
        st.code(r.output or "(空)", language="json" if r.prompt_id != "(rule_based)" else "text")
    with tabs[1]:
        st.code(r.input, language="text")
    with tabs[2]:
        st.json({
            "ts": r.ts,
            "prompt_id": r.prompt_id,
            "prompt_version": r.prompt_version,
            "model": r.model,
            "latency_ms": r.latency_ms,
            "tokens_in": r.tokens_in,
            "tokens_out": r.tokens_out,
            "success": r.success,
            "error": r.error,
            "metadata": r.metadata,
        })


# ======================================================================
# 頁面: 個股總覽 (Watchlist)
# ======================================================================


def _action_badge(action: str) -> str:
    palette = {
        "STRONG_BUY": ("強烈買進", "green"),
        "BUY": ("買進", "green"),
        "HOLD": ("觀望", "gray"),
        "REDUCE": ("減碼", "orange"),
        "SELL": ("賣出", "red"),
    }
    label, color = palette.get(action, (action, "gray"))
    return _badge(label, color)


def _build_scorecards(
    tickers: List[str],
    *,
    name_map: Optional[Dict[str, str]] = None,
    refresh_chips: bool = False,
    refresh_fundamentals: bool = False,
    refresh_technicals: bool = False,
    refresh_distribution: bool = False,
    auto_fill_missing: bool = False,
    auto_llm: bool = False,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> List:
    """對一群 ticker 組 snapshot 後跑 scoring。"""
    out = []
    nm = name_map or {}
    from bot.fundamentals_fetcher import snapshot_to_dict as _fund_dict
    from bot.technicals import snapshot_to_dict as _tech_dict
    total = len(tickers)
    for idx, t in enumerate(tickers, 1):
        if on_progress:
            on_progress(idx, total, t)
        snap = build_snapshot(
            t, PROJECT_ROOT,
            refresh_chips=refresh_chips,
            refresh_fundamentals=refresh_fundamentals,
            refresh_technicals=refresh_technicals,
            refresh_distribution=refresh_distribution,
            auto_fill_missing=auto_fill_missing,
            auto_llm=auto_llm,
            macro_cache_only=not auto_fill_missing,
            name_hint=nm.get(t, ""),
        )
        card = compute_scorecard(
            ticker=snap.ticker,
            name=snap.name,
            price=snap.price,
            pct_change=snap.pct_change,
            volume=snap.volume,
            llm_analysis=snap.llm_analysis,
            logic_result=snap.logic_check,
            consensus=snap.consensus,
            new_build_signal=snap.new_build_signal,
            add_signal=snap.add_signal,
            chip_summary=snap.chip_summary,
            fundamental=_fund_dict(snap.fundamentals) if snap.fundamentals else None,
            technical_snapshot=_tech_dict(snap.technicals) if snap.technicals else None,
            distribution_label=snap.distribution_label,
            distribution_detail=snap.distribution_detail,
            distribution_score=snap.distribution_score,
            has_distribution=bool(snap.distribution_trend and snap.distribution_trend.weeks),
            macro_snapshot=snap.macro_snapshot,
            related_us_stocks=snap.us_related,
            adr_premium=snap.adr_premium,
            fetched_at=snap.fetched_at,
        )
        out.append((snap, card))
    return out


def page_watchlist() -> None:
    st.title("個股總覽 (Watchlist)")
    st.caption(
        "把你關注的股票放這裡。每一檔會用四個時間框架 (當沖/短/中/長) "
        "對 LLM 法說、籌碼、ETF 共識、技術面、風險做加權評分。"
    )
    _llm_auto_banner(
        "「🤖 計算評分」預設採 local-first：先讀 SQLite/CSV/JSON 本地資料並快速算分。"
        "只有勾選「缺資料自動補抓」或各刷新開關時才會打外部資料源；"
        "只有勾選「自動 LLM」時才會為缺分析的個股呼叫 Gemini。"
    )

    wlist = wl.load(PROJECT_ROOT)
    existing = {it.ticker for it in wlist.items}

    # ------ 工具列：增 / 刪 / 來源同步 ------
    with st.expander("管理清單 (增刪 / 來源同步)", expanded=not wlist.items):
        c1, c2, c3 = st.columns([2, 2, 1])
        new_ticker = c1.text_input("代號", value="", placeholder="2330", key="wl_new_t")
        new_name = c2.text_input("名稱 (可空)", value="", key="wl_new_n")
        new_tag = c3.text_input("標籤", value="", placeholder="AI", key="wl_new_tag")
        if c3.button("加入", use_container_width=True, key="wl_add"):
            if new_ticker.strip():
                wl.add(
                    new_ticker.strip(), name=new_name.strip(),
                    tags=[new_tag.strip()] if new_tag.strip() else [],
                    root=PROJECT_ROOT,
                )
                st.toast(f"已加入 {new_ticker}", icon="✅")
                st.rerun()

        st.markdown("##### 一鍵從現有資料合併")
        s1, s2 = st.columns(2)
        if s1.button("加入所有 ETF 共識焦點 (≥2 檔 ETF)", use_container_width=True):
            wl.merge_etf_focus(min_consensus=2, root=PROJECT_ROOT)
            st.toast("ETF 共識焦點已合併", icon="✅")
            st.rerun()
        if s2.button("加入最近一次 Pipeline 焦點", use_container_width=True):
            wl.merge_pipeline_focus(root=PROJECT_ROOT)
            st.toast("Pipeline 焦點已合併", icon="✅")
            st.rerun()

        if wlist.items:
            st.markdown("##### 清單編輯 / 移除")
            del_t = st.selectbox(
                "選擇要移除的 ticker",
                [f"{i.ticker} {i.name}" for i in wlist.items],
                key="wl_del",
            )
            if st.button("移除", key="wl_del_btn"):
                wl.remove(del_t.split()[0], root=PROJECT_ROOT)
                st.rerun()

    # ------ 評分量表規則 ------
    with st.expander("評分規則速覽 (四時間框架 × 8 個 factor)", expanded=False):
        rows = []
        factor_labels = {
            "fundamental": "基本面",
            "technical": "技術面",
            "chips": "三大法人",
            "distribution": "大戶結構",
            "etf_consensus": "ETF 共識",
            "llm_sentiment": "法說語意",
            "logic": "言行一致性",
            "risk": "風險警示",
        }
        for tf, weights in WEIGHTS.items():
            row = {"時間框架": TIMEFRAME_LABELS[tf]}
            for fk, label in factor_labels.items():
                row[label] = f"{weights.get(fk, 0):.0%}" if fk in weights else "—"
            rows.append(row)
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

        st.markdown("**建議行動門檻** — `≥78 強烈買進 / ≥62 買進 / ≥45 觀望 / ≥30 減碼 / <30 賣出`")
        strat_rows = []
        for tf, r in STRATEGY_RULES.items():
            strat_rows.append({
                "時間框架": TIMEFRAME_LABELS[tf],
                "停損": f"{r['stop_pct']:+.1f}%",
                "停利": f"{r['target_pct']:+.1f}%",
                "盈虧比": f"{abs(r['target_pct']/r['stop_pct']):.2f}",
                "持有期": r["horizon"],
                "部位": r["size_hint"],
                "進場邏輯": r["entry_logic"],
            })
        st.dataframe(pd.DataFrame(strat_rows), hide_index=True, use_container_width=True)

    if not wlist.items:
        st.info("Watchlist 是空的。請從上方加入 ticker，或一鍵合併 ETF 共識焦點。")
        return

    # ------ 評分 (含資料抓取選項) ------
    st.caption(
        "SOP：預設先用本地資料快速呈現；需要更新時，再勾選下方刷新或補抓開關。"
    )
    cs1, cs2, cs3, cs4, cs5, cs6, cs7 = st.columns([1, 1, 1, 1, 1, 1, 1])
    refresh_chips = cs1.toggle(
        "現抓 5 日籌碼", value=False, key="wl_refresh",
        help="勾選會對每檔逐一呼叫 TWSE OpenAPI (約每檔 1-3 秒)。",
    )
    refresh_tech = cs2.toggle("抓日K + 指標", value=False, key="wl_refresh_t")
    refresh_fund = cs3.toggle("更新基本面", value=False, key="wl_refresh_f")
    refresh_dist = cs4.toggle("更新 TDCC", value=False, key="wl_refresh_d")
    auto_fill_missing = cs5.toggle(
        "缺資料補抓",
        value=False,
        key="wl_auto_fill",
        help="本地沒有資料時才補抓外部來源。關閉時缺資料會被標示，不阻塞整個 watchlist。",
    )
    auto_llm = cs6.toggle(
        "自動 LLM",
        value=False,
        key="wl_auto_llm",
        help="缺少 LLM 法說分析時呼叫 Gemini。關閉時只使用既有 pipeline/auto_llm 快取。",
    )
    if cs7.button(
        "🤖 計算評分",
        type="primary",
        use_container_width=True,
        key="wl_calc",
        help=(
            "對 watchlist 每檔重新組 snapshot + 加權算分。預設只讀本地資料；"
            "依勾選狀態決定是否補抓外部資料或呼叫 Gemini。"
        ),
    ):
        st.session_state.pop("watchlist_cards", None)

    cards = st.session_state.get("watchlist_cards")
    if cards is None:
        status = st.status("準備計算 watchlist 評分", expanded=True)
        status.write(f"檢查清單：{len(wlist.items)} 檔，優先讀取本地 SQLite / CSV / JSON。")
        if any((refresh_chips, refresh_tech, refresh_fund, refresh_dist)):
            status.write("已勾選刷新項目：部分 ticker 會呼叫外部資料源。")
        elif auto_fill_missing:
            status.write("缺資料補抓已開啟：本地沒有資料時才呼叫外部資料源。")
        else:
            status.write("快速模式：不補抓外部資料，缺資料會標示並降低資料覆蓋。")
        if auto_llm:
            status.write("自動 LLM 已開啟：缺少分析快取時可能呼叫 Gemini。")
        else:
            status.write("自動 LLM 關閉：只使用既有 LLM / pipeline 快取。")
        progress = st.progress(0.0, text="尚未開始")

        def _progress(idx: int, total: int, ticker: str) -> None:
            progress.progress(
                (idx - 1) / max(total, 1),
                text=f"[{idx}/{total}] {ticker}: 讀取本地資料、套用刷新設定並計算評分",
            )

        try:
            cards = _build_scorecards(
                [i.ticker for i in wlist.items],
                name_map={i.ticker: i.name for i in wlist.items},
                refresh_chips=refresh_chips,
                refresh_technicals=refresh_tech,
                refresh_fundamentals=refresh_fund,
                refresh_distribution=refresh_dist,
                auto_fill_missing=auto_fill_missing,
                auto_llm=auto_llm,
                on_progress=_progress,
            )
            st.session_state["watchlist_cards"] = cards
            progress.progress(1.0, text=f"完成 {len(cards)} 檔評分")
            status.update(label=f"watchlist 評分完成：{len(cards)} 檔", state="complete", expanded=False)
        except Exception as exc:
            status.update(label="watchlist 評分失敗", state="error", expanded=True)
            raise exc

    # ------ 主表 ------
    rows = []
    for snap, card in cards:
        row = scorecard_to_row(card)
        row["持倉"] = f"{snap.position_qty:.0f} 張" if snap.position_qty else "—"
        row["最近分析"] = snap.pipeline_run_id or "—"
        row["共識ETF"] = (snap.consensus or {}).get("etf_count", 0)
        rows.append(row)
    df = pd.DataFrame(rows)

    # 篩選 / 排序
    cf1, cf2, cf3 = st.columns(3)
    tf_pick = cf1.selectbox(
        "排序依據",
        ["當沖分", "短期分", "中期分", "長期分"],
        index=2,
    )
    only_buy = cf2.toggle("只顯示『買進 / 強烈買進』", value=False)
    name_filter = cf3.text_input("搜尋 (代號/名稱)", value="")

    if tf_pick in df.columns:
        df = df.sort_values(tf_pick, ascending=False, na_position="last")
    if only_buy:
        df = df[df["建議"].isin(["STRONG_BUY", "BUY"])]
    if name_filter.strip():
        kw = name_filter.strip()
        df = df[df["代號"].str.contains(kw) | df["名稱"].fillna("").str.contains(kw)]

    st.dataframe(
        df, hide_index=True, use_container_width=True,
        column_config={
            "當沖分": st.column_config.ProgressColumn(
                "當沖分", min_value=0, max_value=100, format="%.1f",
            ),
            "短期分": st.column_config.ProgressColumn(
                "短期分", min_value=0, max_value=100, format="%.1f",
            ),
            "中期分": st.column_config.ProgressColumn(
                "中期分", min_value=0, max_value=100, format="%.1f",
            ),
            "長期分": st.column_config.ProgressColumn(
                "長期分", min_value=0, max_value=100, format="%.1f",
            ),
        },
    )

    # 下載 + 跳轉到深入分析
    csv_bytes = df.to_csv(index=False).encode("utf-8-sig")
    cd1, cd2 = st.columns([2, 3])
    cd1.download_button(
        "下載 CSV", csv_bytes,
        file_name=f"watchlist_scores_{dt.date.today().isoformat()}.csv",
    )
    picks = [r["代號"] for r in rows]
    target = cd2.selectbox("跳到個股深入分析", picks, key="wl_jump")
    if cd2.button("查看", use_container_width=True):
        st.session_state["detail_ticker"] = target
        st.session_state["page"] = "個股深入分析"
        st.rerun()


# ======================================================================
# 頁面: 目前持股分析
# ======================================================================


def _latest_local_close(db: StockDB, symbol: str) -> Tuple[float, str]:
    try:
        bars = db.get_price_history(symbol, limit=1, ascending=True)
    except Exception:
        return 0.0, ""
    if not bars:
        return 0.0, ""
    return float(bars[-1].close or 0.0), str(bars[-1].date or "")


def _stock_label(db: StockDB, symbol: str) -> Tuple[str, str]:
    try:
        info = db.get_stock_info(symbol)
    except Exception:
        info = None

    # 自動補：DB 沒這檔、或名稱/產業為空時，從 TWSE/TPEx 公司基本資料補 (每日快取)。
    if info is None or not info.industry or not (info.short_name or info.name):
        try:
            fetched = lookup_company_info(symbol, root=PROJECT_ROOT)
        except Exception:
            fetched = None
        if fetched is not None:
            if info is None:
                info = fetched
            else:
                info.name = info.name or fetched.name
                info.short_name = info.short_name or fetched.short_name
                info.industry = info.industry or fetched.industry
                info.market = info.market or fetched.market
                info.listed_date = info.listed_date or fetched.listed_date
            try:
                db.upsert_stock_info(info)
            except Exception:
                pass

    if info is None:
        # 公司清單查不到 (多為 ETF/基金) → 至少標記，避免一律「未分類」。
        try:
            from bot.market_meta import is_etf
            if is_etf(symbol):
                return "", "ETF / 基金"
        except Exception:
            pass
        return "", ""

    industry = info.industry
    if not industry:
        try:
            from bot.market_meta import is_etf
            if is_etf(symbol):
                industry = "ETF / 基金"
        except Exception:
            pass
    return info.short_name or info.name, industry


def _portfolio_rows(
    broker_positions: List[BrokerPosition],
    bot_positions: Dict[str, PortfolioPosition],
    db: StockDB,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for bp in sorted(broker_positions, key=lambda p: p.symbol):
        symbol = bp.symbol
        bot_pos = bot_positions.get(symbol)
        ownership = classify_bot_ownership(bp.qty, bot_pos)
        name, industry = _stock_label(db, symbol)
        local_close, local_date = _latest_local_close(db, symbol)
        last_price = bp.last_price or local_close
        price_source = "券商" if bp.last_price > 0 else ("本地K線" if local_close > 0 else "")
        cost = float(bp.cost_basis)
        market_value = bp.market_value if bp.market_value > 0 else (
            last_price * bp.qty * LOT_SIZE if last_price > 0 else 0.0
        )
        pnl = float(bp.pnl)
        if pnl == 0 and market_value > 0 and cost > 0:
            pnl = market_value - cost
        pnl_pct = pnl / cost * 100.0 if cost > 0 else 0.0
        bot_cost = float(bot_pos.cost_basis) if bot_pos else 0.0
        rows.append({
            "代號": symbol,
            "名稱": name,
            "產業": industry or "未分類",
            "券商張數": bp.qty,
            "券商均價": bp.avg_price,
            "券商成本": cost,
            "最新價": last_price,
            "最新價來源": price_source or "缺價",
            "券商市值": market_value,
            "未實現損益": pnl,
            "損益%": pnl_pct,
            "本工具標記": ownership.label,
            "本工具張數": ownership.bot_qty,
            "手動/外部張數": ownership.manual_qty,
            "本工具均價": bot_pos.avg_cost if bot_pos else 0.0,
            "本工具成本估算": bot_cost,
            "權重%": 0.0,
            "券商方向": bp.direction,
            "庫存類別": bp.cond,
            "昨餘張數": bp.yd_qty,
            "本工具最後成交": bot_pos.last_trade_ts if bot_pos else "",
            "本工具成交筆數": bot_pos.trade_count if bot_pos else 0,
            "資料狀態": (
                "OK" if bp.last_price > 0
                else ("券商缺價，使用本地收盤" if local_close > 0 else "缺最新價")
            ),
            "資料日期": local_date,
            "標記說明": ownership.detail,
        })
    total_market = sum(float(r["券商市值"]) for r in rows)
    if total_market > 0:
        for row in rows:
            row["權重%"] = float(row["券商市值"]) / total_market * 100.0
    return rows


def _portfolio_warnings(df: pd.DataFrame) -> List[str]:
    warnings: List[str] = []
    if df.empty:
        return warnings

    missing = int((df["資料狀態"] != "OK").sum())
    if missing:
        warnings.append(f"{missing} 檔券商庫存缺少即時/最新價；其中可用本地 K 線者已暫用本地收盤估值。")

    if "權重%" in df.columns and float(df["權重%"].max()) >= 40:
        top = df.sort_values("權重%", ascending=False).iloc[0]
        warnings.append(f"{top['代號']} 權重 {top['權重%']:.1f}%，單檔曝險偏集中。")

    if len(df) >= 3:
        top3 = float(df.sort_values("權重%", ascending=False).head(3)["權重%"].sum())
        if top3 >= 75:
            warnings.append(f"前三大持股合計 {top3:.1f}%，投組集中度偏高。")

    losers = df[(df["最新價"] > 0) & (df["損益%"] <= -5)]
    if not losers.empty:
        names = "、".join(losers.sort_values("損益%").head(3)["代號"].astype(str).tolist())
        warnings.append(f"{names} 未實現損益低於 -5%，建議回到個股深入分析檢查停損與基本面。")

    partial = df[df["本工具標記"] == "部分本工具"]
    if not partial.empty:
        names = "、".join(partial.head(5)["代號"].astype(str).tolist())
        warnings.append(f"{names} 同時包含本工具與手動/外部庫存，標記為估算值。")

    return warnings


def _collect_portfolio_analysis_bundle(
    *,
    df: pd.DataFrame,
    broker_snapshot: Any,
    warnings: List[str],
    max_holdings: int,
    auto_fill_missing: bool,
    refresh_chips: bool,
    refresh_fundamentals: bool,
    refresh_technicals: bool,
    refresh_distribution: bool,
    auto_ticker_llm: bool,
) -> Dict[str, Any]:
    ranked = df.sort_values("權重%", ascending=False).head(max_holdings)
    rows = ranked.to_dict("records")
    snapshots: Dict[str, Dict[str, Any]] = {}

    status = st.status("蒐集目前持股分析資料包", expanded=True)
    status.write(
        f"準備蒐集 {len(rows)} 檔：券商庫存、本工具標記、個股快照、"
        "基本面、技術面、籌碼、TDCC、ETF 共識與既有 LLM。"
    )
    if auto_fill_missing:
        status.write("缺資料補抓已開啟：本地缺資料時會呼叫外部公開資料源。")
    else:
        status.write("local-first：優先使用 SQLite/CSV/JSON 快取，不主動補抓缺漏資料。")
    if auto_ticker_llm:
        status.write("個股 LLM 素材補強已開啟：缺少個股研究快取時，可能逐檔呼叫 Gemini。")

    progress = st.progress(0.0, text="尚未開始")
    try:
        for idx, row in enumerate(rows, 1):
            symbol = str(row.get("代號", "")).strip()
            progress.progress(
                (idx - 1) / max(len(rows), 1),
                text=f"[{idx}/{len(rows)}] {symbol}: 蒐集個股資料包",
            )
            snap = build_snapshot(
                symbol,
                PROJECT_ROOT,
                name_hint=str(row.get("名稱", "") or ""),
                refresh_chips=refresh_chips,
                refresh_fundamentals=refresh_fundamentals,
                refresh_technicals=refresh_technicals,
                refresh_distribution=refresh_distribution,
                auto_fill_missing=auto_fill_missing,
                auto_llm=auto_ticker_llm,
                macro_cache_only=not auto_fill_missing,
            )
            snapshots[symbol] = snapshot_to_dict(snap)
        progress.progress(1.0, text=f"完成 {len(rows)} 檔資料蒐集")
        bundle = build_portfolio_analysis_bundle(
            rows=rows,
            snapshots=snapshots,
            broker_meta={
                "broker": getattr(broker_snapshot, "broker", ""),
                "account": getattr(broker_snapshot, "account", ""),
                "asof": getattr(broker_snapshot, "asof", ""),
                "simulation": getattr(broker_snapshot, "simulation", False),
            },
            warnings=warnings,
        )
        _save_portfolio_bundle(bundle)
        status.update(label=f"資料包完成：{len(bundle.get('holdings', []))} 檔", state="complete", expanded=False)
        return bundle
    except Exception:
        status.update(label="資料包蒐集失敗", state="error", expanded=True)
        raise


def _save_portfolio_bundle(bundle: Dict[str, Any]) -> Path:
    out_dir = _project_path("data", "portfolio_analysis")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"portfolio_bundle_{stamp}.json"
    path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "latest_bundle.json").write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def _save_portfolio_llm_result(markdown: str, info: Dict[str, Any]) -> Path:
    out_dir = _project_path("data", "portfolio_analysis")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"portfolio_analysis_{stamp}.md"
    header = (
        f"<!-- prompt={info.get('prompt_id', '')} "
        f"version={info.get('prompt_version', '')} "
        f"model={info.get('model', '')} -->\n\n"
    )
    path.write_text(header + markdown, encoding="utf-8")
    (out_dir / "latest_analysis.md").write_text(header + markdown, encoding="utf-8")
    return path


# ------ 持股快照磁碟快取 ------

_BROKER_SNAPSHOT_FILENAME = "latest_broker_snapshot.json"


def _save_broker_snapshot(snap) -> Path:
    """將券商庫存快照序列化為 JSON，存到 data/portfolio_analysis/。"""
    from dataclasses import asdict

    out_dir = _project_path("data", "portfolio_analysis")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / _BROKER_SNAPSHOT_FILENAME
    payload = asdict(snap)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _load_broker_snapshot():
    """從磁碟讀取上次儲存的券商庫存快照，回傳 BrokerPositionsSnapshot 或 None。"""
    from bot.portfolio import BrokerPosition, BrokerPositionsSnapshot

    path = _project_path("data", "portfolio_analysis", _BROKER_SNAPSHOT_FILENAME)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        positions = [
            BrokerPosition(**p) for p in data.get("positions", [])
        ]
        return BrokerPositionsSnapshot(
            broker=data.get("broker", ""),
            account=data.get("account", ""),
            asof=data.get("asof", ""),
            simulation=data.get("simulation", False),
            positions=positions,
            error=data.get("error", ""),
        )
    except Exception:
        return None


def _load_portfolio_bundle_from_disk() -> Optional[Dict[str, Any]]:
    """從磁碟讀取上次儲存的 portfolio analysis bundle。"""
    path = _project_path("data", "portfolio_analysis", "latest_bundle.json")
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_portfolio_llm_from_disk() -> Optional[Dict[str, Any]]:
    """從磁碟讀取上次儲存的 LLM 投組分析結果。"""
    path = _project_path("data", "portfolio_analysis", "latest_analysis.md")
    if not path.exists():
        return None
    try:
        markdown = path.read_text(encoding="utf-8")
        # 解析 header 中的 prompt/version/model 資訊
        info: Dict[str, str] = {}
        if markdown.startswith("<!--"):
            end = markdown.find("-->")
            if end > 0:
                header_text = markdown[4:end].strip()
                for part in header_text.split():
                    if "=" in part:
                        k, v = part.split("=", 1)
                        info[k.strip()] = v.strip()
                markdown = markdown[end + 3:].strip()
        return {
            "markdown": markdown,
            "info": info,
            "path": str(path),
        }
    except Exception:
        return None


# ------ 貼文字分析、言行反查、最後個股快取 ------

def _save_paste_analysis(analysis, text: str) -> None:
    """將貼文字分析結果與對應的逐字稿文字存入 data/cache_paste_analysis.json 中。"""
    from dataclasses import asdict
    path = _project_path("data", "cache_paste_analysis.json")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "analysis": asdict(analysis),
            "text": text,
            "saved_at": dt.datetime.now().isoformat()
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _load_paste_analysis() -> Optional[Dict[str, Any]]:
    """從 data/cache_paste_analysis.json 載入貼文字分析結果與文字。"""
    from bot.llm_analyzer import PresentationAnalysis
    path = _project_path("data", "cache_paste_analysis.json")
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        a_data = payload.get("analysis")
        if a_data:
            analysis = PresentationAnalysis(**a_data)
            return {
                "analysis": analysis,
                "text": payload.get("text", "")
            }
    except Exception:
        pass
    return None


def _save_logic_check(ticker: str, chips, result) -> None:
    """將籌碼面與言行一致性檢查的結果存入 data/cache_logic_check.json 中。"""
    from dataclasses import asdict
    path = _project_path("data", "cache_logic_check.json")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ticker": ticker,
            "chips": asdict(chips),
            "result": asdict(result),
            "saved_at": dt.datetime.now().isoformat()
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _load_logic_check(expected_ticker: str) -> Optional[Dict[str, Any]]:
    """從 data/cache_logic_check.json 載入特定股票代號的籌碼與反查結果。"""
    from bot.llm_analyzer import ChipsContext, LogicCheckResult
    path = _project_path("data", "cache_logic_check.json")
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("ticker") == expected_ticker:
            c_data = payload.get("chips")
            r_data = payload.get("result")
            if c_data and r_data:
                return {
                    "chips": ChipsContext(**c_data),
                    "result": LogicCheckResult(**r_data)
                }
    except Exception:
        pass
    return None


def _save_last_viewed_ticker(ticker: str) -> None:
    """儲存最後瀏覽的個股代號至 data/last_viewed_ticker.txt。"""
    path = _project_path("data", "last_viewed_ticker.txt")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(ticker.strip(), encoding="utf-8")
    except Exception:
        pass


def _load_last_viewed_ticker() -> str:
    """從 data/last_viewed_ticker.txt 讀取最後瀏覽的個股代號。"""
    path = _project_path("data", "last_viewed_ticker.txt")
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _render_portfolio_analysis_section(
    *,
    df: pd.DataFrame,
    broker_snapshot: Any,
    warnings: List[str],
    env_values: Dict[str, str],
) -> None:
    st.markdown("### 資料蒐集與 LLM 投組分析")
    st.caption(
        "先把券商庫存與每檔持股證據整理成資料包；確認資料覆蓋率後，再用 Gemini 做整體投組分析。"
    )

    with st.container(border=True):
        c1, c2, c3, c4, c5, c6 = st.columns([1, 1, 1, 1, 1, 1])
        max_holdings = int(c1.number_input(
            "分析檔數",
            min_value=1,
            max_value=max(1, len(df)),
            value=min(12, max(1, len(df))),
            step=1,
            key="portfolio_bundle_limit",
        ))
        auto_fill_missing = c2.toggle(
            "缺資料補抓",
            value=False,
            key="portfolio_bundle_auto_fill",
            help="本地缺資料時才呼叫外部公開資料源；會比 local-first 慢。",
        )
        refresh_tech = c3.toggle("更新技術面", value=False, key="portfolio_bundle_tech")
        refresh_fund = c4.toggle("更新基本面", value=False, key="portfolio_bundle_fund")
        refresh_chips = c5.toggle("更新籌碼", value=False, key="portfolio_bundle_chips")
        refresh_dist = c6.toggle("更新TDCC", value=False, key="portfolio_bundle_dist")

        l1, l2 = st.columns([1, 2])
        auto_ticker_llm = l1.toggle(
            "補個股 LLM 素材",
            value=False,
            key="portfolio_bundle_ticker_llm",
            help="缺少個股研究快取時可能逐檔呼叫 Gemini，適合正式分析前補資料。",
        )
        l2.caption(
            "資料包會寫入 `data/portfolio_analysis/latest_bundle.json`，"
            "LLM 結果會寫入 `data/portfolio_analysis/latest_analysis.md`。"
        )

        if st.button(
            "蒐集 / 更新分析資料包",
            type="primary",
            use_container_width=True,
            key="portfolio_collect_bundle",
            disabled=df.empty,
        ):
            bundle = _collect_portfolio_analysis_bundle(
                df=df,
                broker_snapshot=broker_snapshot,
                warnings=warnings,
                max_holdings=max_holdings,
                auto_fill_missing=auto_fill_missing,
                refresh_chips=refresh_chips,
                refresh_fundamentals=refresh_fund,
                refresh_technicals=refresh_tech,
                refresh_distribution=refresh_dist,
                auto_ticker_llm=auto_ticker_llm,
            )
            st.session_state["portfolio_analysis_bundle"] = bundle
            st.session_state.pop("portfolio_analysis_result", None)

    bundle = st.session_state.get("portfolio_analysis_bundle")
    if bundle:
        quality = pd.DataFrame(bundle.get("data_quality", []))
        qsum = bundle.get("data_quality_summary", {})
        q1, q2, q3 = st.columns(3)
        q1.metric("資料覆蓋率", f"{float(qsum.get('average_coverage_score', 0)) * 100:.0f}%")
        q2.metric("資料包持股", f"{len(bundle.get('holdings', []))} 檔")
        q3.metric("缺口類型", f"{len(qsum.get('missing_counts', {}))} 種")
        if not quality.empty:
            st.dataframe(
                quality,
                hide_index=True,
                use_container_width=True,
                column_config={
                    "coverage_score": st.column_config.ProgressColumn(
                        "覆蓋率", min_value=0, max_value=1, format="%.0%",
                    ),
                    "weight_pct": st.column_config.NumberColumn("權重%", format="%.1f%%"),
                },
            )
        st.download_button(
            "下載 LLM 資料包 JSON",
            data=bundle_to_json(bundle, max_chars=200_000).encode("utf-8"),
            file_name=f"portfolio_bundle_{dt.date.today().isoformat()}.json",
            mime="application/json",
        )

        api_key = env_values.get("GEMINI_API_KEY", "")
        model = env_values.get("GEMINI_MODEL", "gemini-2.5-flash") or "gemini-2.5-flash"
        if not api_key:
            st.warning("尚未設定 `GEMINI_API_KEY`，可以先蒐集資料包，但無法產生 LLM 投組分析。")
        if st.button(
            _llm_button_label("分析目前持股"),
            use_container_width=True,
            disabled=not api_key,
            key="portfolio_llm_analyze",
            help=LLM_HINT_DIRECT,
        ):
            from bot.llm_analyzer import GeminiClient, gemini_call

            client = GeminiClient(api_key=api_key, model=model)
            with st.spinner("Gemini 正在讀取資料包並分析投組..."):
                raw, info = gemini_call(
                    "portfolio_analysis",
                    client=client,
                    registry=get_registry(PROJECT_ROOT / "prompts"),
                    metadata={
                        "task": "portfolio_analysis",
                        "symbols": [h.get("symbol") for h in bundle.get("holdings", [])],
                    },
                    today=dt.date.today().isoformat(),
                    broker_asof=str((bundle.get("broker") or {}).get("asof", "")),
                    portfolio_json=bundle_to_json(bundle),
                )
            markdown = raw or "(LLM 未產出內容)"
            result_path = _save_portfolio_llm_result(markdown, info)
            st.session_state["portfolio_analysis_result"] = {
                "markdown": markdown,
                "info": info,
                "path": str(result_path),
            }

    result = st.session_state.get("portfolio_analysis_result")
    if result:
        info = result.get("info", {})
        st.markdown("#### LLM 投組分析")
        st.caption(
            f"prompt: `{info.get('prompt_id', '')}` v{info.get('prompt_version', '')} · "
            f"model: `{info.get('model', '')}` · saved: `{result.get('path', '')}`"
        )
        st.markdown(result.get("markdown", ""))


def page_portfolio() -> None:
    st.title("目前持股分析")
    st.caption(
        "以券商 `list_positions` 回傳的目前股票庫存為主；"
        "`data/trades_*.csv` 只用來標記哪些庫存可由本工具成交紀錄對上。"
    )

    trade_files = _list_files(_project_path("data"), "trades_*.csv")
    all_trades, bot_positions = load_bot_portfolio(PROJECT_ROOT)
    db = _open_db_for_page()

    env_values = load_env()
    backend = env_values.get("BROKER_BACKEND", "shioaji") or "shioaji"
    simulation = env_values.get("SIMULATION", "true").lower() in ("1", "true", "yes", "on")
    s1, s2, s3, s4 = st.columns([1, 1, 1, 2])
    s1.metric("券商後端", backend)
    s2.metric("環境", "模擬" if simulation else "正式")
    s3.metric("本工具未出清", f"{len(bot_positions)} 檔")
    s4.caption(
        f"本工具成交檔：{len(trade_files)} 個；"
        "標記依 FIFO 推估，若有手動下單會顯示部分或非本工具。"
    )

    if simulation:
        st.warning("目前 `.env` 的 `SIMULATION=true`，券商庫存會是模擬環境資料；若要讀正式券商帳戶，請切成 `SIMULATION=false`。")

    c_refresh, c_clear = st.columns([1, 1])
    if c_refresh.button("刷新券商庫存", type="primary", use_container_width=True, key="portfolio_fetch_broker"):
        from bot.config import Settings as S

        status = st.status("讀取券商庫存", expanded=True)
        status.write("載入 .env 設定並登入券商 API。")
        status.write("呼叫 Shioaji `list_positions(stock_account, unit=Common)`；不送出任何委託。")
        try:
            settings = S(_env_file=str(env_path()))  # type: ignore[call-arg]
            snap = fetch_broker_positions(settings)
            st.session_state["portfolio_broker_snapshot"] = snap
            st.session_state.pop("portfolio_from_cache", None)
            if snap.error:
                status.update(label="券商庫存讀取失敗", state="error", expanded=True)
            else:
                _save_broker_snapshot(snap)
                status.update(label=f"券商庫存讀取完成：{len(snap.positions)} 檔", state="complete", expanded=False)
        except Exception as exc:
            status.update(label="券商庫存讀取失敗", state="error", expanded=True)
            raise exc
    if c_clear.button("清除快照", use_container_width=True, key="portfolio_clear_broker"):
        st.session_state.pop("portfolio_broker_snapshot", None)
        st.rerun()

    snap = st.session_state.get("portfolio_broker_snapshot")
    if snap is None:
        # 嘗試從磁碟快取還原上次的券商庫存快照
        cached_snap = _load_broker_snapshot()
        if cached_snap is not None and not cached_snap.error:
            snap = cached_snap
            st.session_state["portfolio_broker_snapshot"] = snap
            st.session_state["portfolio_from_cache"] = True
            # 同時嘗試還原分析資料包與 LLM 結果
            if "portfolio_analysis_bundle" not in st.session_state:
                cached_bundle = _load_portfolio_bundle_from_disk()
                if cached_bundle:
                    st.session_state["portfolio_analysis_bundle"] = cached_bundle
            if "portfolio_analysis_result" not in st.session_state:
                cached_llm = _load_portfolio_llm_from_disk()
                if cached_llm:
                    st.session_state["portfolio_analysis_result"] = cached_llm
        else:
            st.info("請先按「刷新券商庫存」。此頁會以券商目前庫存為主，本工具成交紀錄只用來做來源標記。")
            if all_trades:
                st.markdown("#### 最近本工具成交")
            else:
                st.info("目前也沒有可解析的本工具成交紀錄。")
            _render_recent_portfolio_trades(all_trades)
            return

    if getattr(snap, "error", ""):
        st.error(f"讀取券商庫存失敗：{snap.error}")
        _render_recent_portfolio_trades(all_trades)
        return

    if st.session_state.get("portfolio_from_cache"):
        st.info(
            f"💡 目前呈現的是歷史快取庫存（快照時間: {snap.asof}）。"
            "如需最新資料，請按上方「刷新券商庫存」。"
        )
    st.caption(
        f"券商快照：{snap.broker} / 帳號 {snap.account or '—'} / "
        f"時間 {snap.asof} / {'模擬環境' if snap.simulation else '正式環境'}"
    )

    rows = _portfolio_rows(snap.positions, bot_positions, db)

    if rows:
        df = pd.DataFrame(rows).sort_values("券商市值", ascending=False)
    else:
        df = pd.DataFrame(columns=[
            "代號", "名稱", "產業", "券商張數", "券商均價", "券商成本", "最新價",
            "券商市值", "未實現損益", "損益%", "本工具標記", "本工具張數",
            "手動/外部張數", "本工具均價", "本工具成本估算", "權重%", "資料狀態",
        ])

    priced = df[df["最新價"] > 0] if not df.empty else df
    total_cost = float(df["券商成本"].sum()) if not df.empty else 0.0
    priced_cost = float(priced["券商成本"].sum()) if not priced.empty else 0.0
    total_market = float(priced["券商市值"].sum()) if not priced.empty else 0.0
    unrealized = float(priced["未實現損益"].sum()) if not priced.empty else 0.0
    ret_pct = unrealized / priced_cost * 100.0 if priced_cost > 0 else 0.0
    bot_marked = int(df["本工具張數"].gt(0).sum()) if not df.empty else 0

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("券商持股檔數", f"{len(df):,}")
    m2.metric("券商成本", f"{total_cost:,.0f}")
    m3.metric("券商市值", f"{total_market:,.0f}")
    m4.metric("未實現損益", f"{unrealized:+,.0f}")
    m5.metric("本工具標記", f"{bot_marked:,} 檔", f"{ret_pct:+.2f}%")

    if df.empty:
        st.info("券商目前沒有回傳股票庫存；下方仍可檢視最近本工具成交。")
    else:
        c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
        keyword = c1.text_input("搜尋代號 / 名稱 / 產業", value="", key="portfolio_kw")
        only_loss = c2.toggle("只看虧損", value=False, key="portfolio_loss")
        only_bot = c3.toggle("只看本工具", value=False, key="portfolio_only_bot")
        hide_missing = c4.toggle("隱藏缺價", value=False, key="portfolio_hide_missing")

        view = df.copy()
        if keyword.strip():
            kw = keyword.strip()
            view = view[
                view["代號"].astype(str).str.contains(kw, case=False)
                | view["名稱"].fillna("").astype(str).str.contains(kw, case=False)
                | view["產業"].fillna("").astype(str).str.contains(kw, case=False)
            ]
        if only_loss:
            view = view[(view["最新價"] > 0) & (view["未實現損益"] < 0)]
        if only_bot:
            view = view[view["本工具張數"] > 0]
        if hide_missing:
            view = view[view["資料狀態"] == "OK"]

        st.markdown("#### 券商庫存明細")
        st.dataframe(
            view,
            hide_index=True,
            use_container_width=True,
            column_config={
                "券商張數": st.column_config.NumberColumn(format="%.2f"),
                "券商均價": st.column_config.NumberColumn(format="%.2f"),
                "券商成本": st.column_config.NumberColumn(format="%.0f"),
                "最新價": st.column_config.NumberColumn(format="%.2f"),
                "券商市值": st.column_config.NumberColumn(format="%.0f"),
                "未實現損益": st.column_config.NumberColumn(format="%+.0f"),
                "損益%": st.column_config.NumberColumn(format="%+.2f%%"),
                "本工具張數": st.column_config.NumberColumn(format="%.2f"),
                "手動/外部張數": st.column_config.NumberColumn(format="%.2f"),
                "本工具均價": st.column_config.NumberColumn(format="%.2f"),
                "本工具成本估算": st.column_config.NumberColumn(format="%.0f"),
                "權重%": st.column_config.ProgressColumn(
                    "權重%", min_value=0, max_value=100, format="%.1f%%",
                ),
            },
        )

        csv_bytes = view.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "下載目前持股 CSV",
            csv_bytes,
            file_name=f"broker_portfolio_{dt.date.today().isoformat()}.csv",
        )

        if not view.empty:
            chart_col1, chart_col2 = st.columns(2)
            with chart_col1:
                st.markdown("#### 持股權重")
                st.bar_chart(view.set_index("代號")["權重%"])
            with chart_col2:
                st.markdown("#### 損益貢獻")
                st.bar_chart(view.set_index("代號")["未實現損益"])

        if not priced.empty:
            industry = (
                priced.groupby("產業", as_index=False)["券商市值"].sum()
                .sort_values("券商市值", ascending=False)
            )
            if float(industry["券商市值"].sum()) > 0:
                industry["權重%"] = industry["券商市值"] / float(industry["券商市值"].sum()) * 100.0
                st.markdown("#### 產業曝險")
                st.dataframe(
                    industry,
                    hide_index=True,
                    use_container_width=True,
                    column_config={
                        "券商市值": st.column_config.NumberColumn(format="%.0f"),
                        "權重%": st.column_config.ProgressColumn(
                            "權重%", min_value=0, max_value=100, format="%.1f%%",
                        ),
                    },
                )

        warnings = _portfolio_warnings(df)
        if warnings:
            with st.expander("風險與資料品質提醒", expanded=True):
                for item in warnings:
                    st.warning(item)

        _render_portfolio_analysis_section(
            df=df,
            broker_snapshot=snap,
            warnings=warnings,
            env_values=env_values,
        )

        picks = df["代號"].astype(str).tolist()
        jump_col1, jump_col2 = st.columns([3, 1])
        target = jump_col1.selectbox("跳到個股深入分析", picks, key="portfolio_jump")
        if jump_col2.button("查看", use_container_width=True, key="portfolio_jump_btn"):
            st.session_state["detail_ticker"] = target
            st.session_state["page"] = "個股深入分析"
            st.rerun()

    st.markdown("#### 最近本工具成交")
    _render_recent_portfolio_trades(all_trades)


def _render_recent_portfolio_trades(all_trades) -> None:
    recent = all_trades[-30:]
    if recent:
        trade_rows = [{
            "時間": t.ts,
            "代號": t.symbol,
            "方向": "買進" if t.side == "buy" else "賣出",
            "張數": t.qty,
            "成交價": t.price,
            "金額": t.amount,
            "來源": f"{t.source_file}:{t.source_row}",
            "備註": t.note,
        } for t in reversed(recent)]
        st.dataframe(
            pd.DataFrame(trade_rows),
            hide_index=True,
            use_container_width=True,
            column_config={
                "張數": st.column_config.NumberColumn(format="%.2f"),
                "成交價": st.column_config.NumberColumn(format="%.2f"),
                "金額": st.column_config.NumberColumn(format="%.0f"),
            },
        )
    else:
        st.info("沒有可解析的本工具成交列。")


# ======================================================================
# 頁面: 個股深入分析
# ======================================================================


def _tf_card(tf_score) -> None:
    """渲染單一時間框架的評分卡。"""
    color = {"green": "#16a34a", "gray": "#6b7280", "orange": "#ea580c", "red": "#dc2626"}[
        tf_score.color
    ]
    st.markdown(
        f"""<div style="border:1px solid {color}33; background:{color}0d;
            border-radius:8px; padding:10px 14px; margin-bottom:6px;">
            <div style="display:flex; justify-content:space-between; align-items:center;">
                <div style="font-weight:600; font-size:16px;">{tf_score.label}</div>
                <div style="font-size:22px; font-weight:700; color:{color}">
                    {tf_score.total:.1f}
                </div>
            </div>
            <div style="font-size:13px; color:{color}; font-weight:600;">
                {tf_score.action_label} · 資料覆蓋 {tf_score.confidence:.0%}
            </div>
            </div>""",
        unsafe_allow_html=True,
    )

    # factor breakdown
    for f in tf_score.factors:
        bar = "█" * int(f.score / 5) + "░" * (20 - int(f.score / 5))
        # 缺資料的因子已從加權中剔除，明確標示且淡化顏色
        if f.available:
            avail = ""
            row_color = color
            bar_color = color
        else:
            avail = " · 缺資料·不計入加權"
            row_color = "#9ca3af"
            bar_color = "#d1d5db"
        st.markdown(
            f"<div style='font-family:monospace; font-size:11.5px; "
            f"color:{row_color}; line-height:1.4;'>"
            f"{f.label} (w={f.weight:.0%}{avail}): "
            f"<span style='color:{bar_color}'>{bar}</span> {f.score:.0f}"
            f"</div>",
            unsafe_allow_html=True,
        )
        if f.detail:
            st.caption(f"　└ {f.detail}")

    if tf_score.strategy and tf_score.strategy.get("applicable"):
        s = tf_score.strategy
        st.markdown(
            f"**策略** | 進 `{s['entry']}` · 停損 `{s['stop']}` ({s['stop_pct']:+.1f}%) "
            f"· 停利 `{s['target']}` ({s['target_pct']:+.1f}%) · 盈虧比 `{s['rrr']:.2f}` · "
            f"部位 `{s['size_hint']}`"
        )
        st.caption(f"持有期: {s['horizon']} ｜ {s['entry_logic']}")
    elif tf_score.strategy:
        st.caption(f"策略: {tf_score.strategy.get('note', '評分不足，不建議進場')}")

    for n in tf_score.notes:
        st.caption(n)


def page_ticker_detail() -> None:
    st.title("個股深入分析")
    _llm_auto_banner(
        "「🤖 分析」預設採 local-first：先讀本地 DB/CSV/JSON 快取。"
        "要補外部資料請勾「缺資料補抓」或指定刷新項目；"
        "要補 LLM 分析請勾「自動 LLM」。"
    )

    wlist = wl.load(PROJECT_ROOT)
    options: List[str] = sorted({i.ticker for i in wlist.items})

    # 來自 watchlist 跳轉
    default_t = st.session_state.get("detail_ticker")
    if not default_t:
        default_t = _load_last_viewed_ticker()
        if default_t:
            st.session_state["detail_ticker"] = default_t
            
    free_t = st.text_input(
        "Ticker", value=default_t or "",
        placeholder="輸入代號 (例如 2330)，或從 watchlist 選一檔",
    )
    if options:
        from_wl = st.selectbox("或從 Watchlist 選", [""] + options, key="detail_pick_wl")
        if from_wl and from_wl != default_t:
            free_t = from_wl
            st.session_state["detail_ticker"] = from_wl

    cgo, ccall, cauto, cllm, crefresh, ctech, cfund, cdist, cadd = st.columns(
        [1.0, 1.1, 0.9, 0.9, 0.9, 0.9, 0.9, 0.8, 1.0],
    )
    if ccall.button(
        "🤖 強制重抓全部資料",
        use_container_width=True,
        key="detail_fetch_all",
        help=(
            "把資料刷新、缺資料補抓與自動 LLM 都打開。"
            "適合你明確要更新全部來源時使用；會比 local-first 慢。"
            "\n\n" + LLM_HINT_AUTO
        ),
    ):
        for k in (
            "detail_rc", "detail_rt", "detail_rf", "detail_rd",
            "detail_auto_fill", "detail_auto_llm",
        ):
            st.session_state[k] = True
        st.session_state.pop("detail_cache", None)
        st.session_state.pop("detail_cache_key", None)
    auto_fill_missing = cauto.toggle(
        "缺資料補抓",
        value=False,
        key="detail_auto_fill",
        help="本地資料缺漏時才呼叫外部資料源補齊。關閉時只讀本地與已勾選的刷新項目。",
    )
    auto_llm = cllm.toggle(
        "自動 LLM",
        value=False,
        key="detail_auto_llm",
        help="缺少 LLM 法說分析時呼叫 Gemini；關閉時只使用既有 LLM 快取或 pipeline 分析。",
    )
    refresh_chips = crefresh.toggle("即時抓籌碼", value=False, key="detail_rc")
    refresh_technicals = ctech.toggle("抓日K + 指標", value=False, key="detail_rt")
    refresh_fundamentals = cfund.toggle("更新基本面", value=False, key="detail_rf")
    refresh_distribution = cdist.toggle("更新 TDCC", value=False, key="detail_rd")
    if cgo.button(
        "🤖 分析",
        type="primary",
        use_container_width=True,
        key="detail_go",
        help=(
            "組裝該股全部 3D 資料 (基本面/技術面/籌碼/集保/ETF/macro)。"
            "預設先用本地快取；依勾選狀態決定是否補抓外部資料或呼叫 Gemini。"
        ),
    ):
        st.session_state["detail_ticker"] = free_t.strip()
        st.session_state.pop("detail_cache", None)
        st.session_state.pop("detail_cache_key", None)

    if cadd.button("加入 Watchlist", use_container_width=True, key="detail_add"):
        if free_t.strip():
            wl.add(free_t.strip(), root=PROJECT_ROOT)
            st.toast(f"已加入 {free_t} 到 Watchlist", icon="✅")

    ticker = st.session_state.get("detail_ticker", free_t.strip())
    if not ticker:
        st.info("請輸入或選擇一檔股票")
        return

    st.caption(f"本地資料檢查：{_ticker_local_data_summary(ticker)}")

    cache_key = (
        f"detail_{ticker}_{refresh_chips}_{refresh_technicals}_"
        f"{refresh_fundamentals}_{refresh_distribution}_"
        f"{auto_fill_missing}_{auto_llm}"
    )
    if (
        st.session_state.get("detail_cache_key") != cache_key
        or "detail_cache" not in st.session_state
    ):
        status = st.status(f"組裝 {ticker} 的完整資料", expanded=True)
        status.write("先檢查本地 DB / CSV / JSON 快取。")
        if any((refresh_chips, refresh_technicals, refresh_fundamentals, refresh_distribution)):
            status.write("已勾選刷新項目：會呼叫對應外部資料源。")
        elif auto_fill_missing:
            status.write("缺資料補抓已開啟：本地缺資料時才補抓外部來源。")
        else:
            status.write("快速模式：不補抓外部資料，缺資料會在下方提示。")
        status.write("開始組裝 snapshot 並計算四時間框架評分。")
        try:
            from bot.fundamentals_fetcher import snapshot_to_dict as _fund_dict
            from bot.technicals import snapshot_to_dict as _tech_dict
            snap = build_snapshot(
                ticker, PROJECT_ROOT,
                refresh_chips=refresh_chips,
                refresh_fundamentals=refresh_fundamentals,
                refresh_technicals=refresh_technicals,
                refresh_distribution=refresh_distribution,
                auto_fill_missing=auto_fill_missing,
                auto_llm=auto_llm,
                macro_cache_only=not auto_fill_missing,
            )
            card = compute_scorecard(
                ticker=snap.ticker, name=snap.name,
                price=snap.price, pct_change=snap.pct_change, volume=snap.volume,
                llm_analysis=snap.llm_analysis,
                logic_result=snap.logic_check,
                consensus=snap.consensus,
                new_build_signal=snap.new_build_signal,
                add_signal=snap.add_signal,
                chip_summary=snap.chip_summary,
                fundamental=_fund_dict(snap.fundamentals) if snap.fundamentals else None,
                technical_snapshot=_tech_dict(snap.technicals) if snap.technicals else None,
                distribution_label=snap.distribution_label,
                distribution_detail=snap.distribution_detail,
                distribution_score=snap.distribution_score,
                has_distribution=bool(snap.distribution_trend and snap.distribution_trend.weeks),
                macro_snapshot=snap.macro_snapshot,
                related_us_stocks=snap.us_related,
                adr_premium=snap.adr_premium,
                fetched_at=snap.fetched_at,
            )
            st.session_state["detail_cache"] = (snap, card)
            st.session_state["detail_cache_key"] = cache_key
            status.update(label=f"{ticker} 資料組裝完成", state="complete", expanded=False)
        except Exception as exc:
            status.update(label=f"{ticker} 資料組裝失敗", state="error", expanded=True)
            raise exc

    snap, card = st.session_state["detail_cache"]
    _save_last_viewed_ticker(ticker)

    # ---- 資料完整度提示 ----
    # local-first 模式下缺資料是正常狀態；引導使用者只刷新需要的資料源。
    missing: List[str] = []
    try:
        from bot.market_meta import detect_market, is_etf, market_label
        _mkt = detect_market(snap.ticker, root=PROJECT_ROOT)
        _is_etf = is_etf(snap.ticker)
        _mkt_label = market_label(_mkt)
    except Exception:
        _mkt, _is_etf, _mkt_label = "unknown", False, "未知市場"

    if snap.price <= 0 and not (snap.technicals and snap.technicals.has_data):
        missing.append(
            f"**現價／日K** — 抓不到 {snap.ticker} 的日K"
            f"（已支援上市 TWSE／上櫃 TPEx），請手動勾選 `抓日K + 指標` 重試"
        )
    if not snap.chip_summary:
        missing.append("**籌碼面** — 三大法人尚未公布或抓取失敗，請勾選 `即時抓籌碼` 重試")
    if _is_etf:
        # ETF 本來就沒有月營收 / 本益比 / 法說會，不應提示「抓不到、請重試」
        pass
    elif not snap.fundamentals or not snap.fundamentals.revenues:
        missing.append(
            f"**基本面** — 月營收/估值資料抓不到（{_mkt_label}），請勾選 `更新基本面` 重試"
        )
    if not snap.distribution_trend or not snap.distribution_trend.weeks:
        missing.append("**大戶結構 TDCC** — TDCC 端點暫時不可用，請勾選 `更新 TDCC` 重試")
    if snap.consensus is None and not snap.held_by_etfs:
        missing.append(
            "**主動 ETF 共識** — 請到 `主動 ETF 追蹤` 抓 ETF 持股，"
            "或在 `自動化管線` 一鍵跑完整流程"
        )
    if not snap.llm_analysis:
        from bot.env_io import load_env as _le
        if not auto_llm:
            missing.append(
                "**LLM 法說分析** — 本次自動 LLM 關閉；若要補分析，請勾選 `自動 LLM` "
                "或到 `LLM 法說分析` 頁手動貼逐字稿"
            )
        elif not _le().get("GEMINI_API_KEY"):
            missing.append(
                "**LLM 法說分析** — 請到 `組態設定` 填入 `GEMINI_API_KEY`，"
                "再勾選 `自動 LLM`；若有完整法說逐字稿可到 `LLM 法說分析` 頁手動貼"
            )
        else:
            missing.append(
                "**LLM 法說分析** — 自動分析未產生結果，可能是素材不足；"
                "可在 `LLM 法說分析` 頁貼逐字稿補強"
            )
    if missing:
        st.warning(
            "目前缺以下資料；可只勾選需要的刷新或補抓項目後重跑：\n\n- "
            + "\n- ".join(missing)
        )

    # ---- 頂部 KPI ----
    st.markdown(f"## {snap.ticker} {snap.name}")
    st.caption(
        f"快照時間 {snap.fetched_at} · 來源 pipeline_run "
        f"{snap.pipeline_run_id or '—'}"
    )

    k1, k2, k3, k4, k5, k6 = st.columns(6)
    k1.metric("現價", f"{snap.price:.2f}" if snap.price else "—")
    k2.metric("漲跌%", f"{snap.pct_change:+.2f}%" if snap.pct_change else "—")
    if snap.chip_summary:
        k3.metric("外資累計 (張)", f"{snap.chip_summary.get('foreign_net', 0):+.0f}")
        k4.metric("投信累計", f"{snap.chip_summary.get('investment_trust_net', 0):+.0f}")
        k5.metric(
            "借券變動",
            f"{snap.chip_summary.get('short_borrow_change_pct', 0):+.1f}%",
        )
    else:
        k3.metric("外資累計", "—")
        k4.metric("投信累計", "—")
        k5.metric("借券變動", "—")
    k6.metric(
        "ETF 共識",
        f"{(snap.consensus or {}).get('etf_count', 0)} 檔",
    )

    if snap.summary_notes if False else card.summary_notes:
        for n in card.summary_notes:
            st.warning(n)

    # ---- 四張時間框評分卡 ----
    st.markdown("### 四時間框架評分")
    tcols = st.columns(4)
    for i, tf in enumerate(TIMEFRAMES):
        with tcols[i]:
            _tf_card(card.timeframes[tf])

    # ---- 完整分頁 (3D 視角 + 操作) ----
    tabs = st.tabs([
        "📝 分析", "💰 基本面", "📈 技術面",
        "🏦 籌碼面", "🎁 股利政策", "🗓️ 季報 Q1-Q4",
        "🌐 美股連動",
        "📂 原始資料", "🎯 購買策略", "💼 目前狀況", "📜 歷史狀況",
    ])

    with tabs[0]:
        # ETF 沒有法說會：標題改為「新聞/情緒解讀」避免誤導
        if _is_etf:
            st.markdown("#### LLM 新聞 / 情緒解讀")
            st.caption("註：ETF 無法人說明會，本段為基於新聞與市場資訊的情緒彙整，非法說逐字稿。")
        else:
            st.markdown("#### LLM 法說會解讀")
        if snap.llm_analysis:
            a = snap.llm_analysis
            st.markdown(
                f"**情緒**: `{a.get('sentiment','-')}` ({a.get('sentiment_score', 0):+.2f}) "
                f" · **信心**: {float(a.get('confidence', 0)):.0%}"
                f" · prompt: `{a.get('prompt_id','')}` v{a.get('prompt_version','')}"
            )
            st.write(a.get("summary", ""))
            cA, cB = st.columns(2)
            with cA:
                st.markdown("**成長驅動**")
                for d in a.get("growth_drivers", []) or []:
                    st.markdown(f"- {d}")
            with cB:
                st.markdown("**風險點**")
                for d in a.get("risks", []) or []:
                    st.markdown(f"- {d}")
            st.markdown(
                f"**Capex 信號**: {a.get('capex_signal','-')} ｜ "
                f"**毛利率展望**: {a.get('margin_outlook','-')}"
            )
            if a.get("key_metrics"):
                st.json(a["key_metrics"])
        else:
            st.info("尚無 LLM 法說分析。請在「LLM 法說分析」頁或「自動化管線」中執行。")

        st.markdown("#### 言行反查")
        if snap.logic_check:
            lc = snap.logic_check
            st.markdown(
                f"**verdict**: `{lc.get('verdict')}` ｜ "
                f"**建議**: `{lc.get('suggestion')}` ｜ "
                f"信心 {float(lc.get('confidence', 0)):.0%}"
            )
            st.write(lc.get("reasoning", ""))
        else:
            st.info("尚未做言行反查 (需要 LLM 法說 + 籌碼資料)")

    with tabs[6]:
        st.markdown("#### 美股連動 (供應鏈夥伴 + ADR 溢價)")
        macro = snap.macro_snapshot or {}
        related = snap.us_related or []
        adr = snap.adr_premium

        if not macro:
            st.warning("尚未抓 macro 資料。請執行 `stock-macro-update` 或在「美股 / 跨市場」頁按重新抓取。")
        else:
            indices = macro.get("indices", {})
            cs = st.columns(5)
            for i, sym in enumerate(["^GSPC", "^IXIC", "^SOX", "^TWII", "^VIX"]):
                q = indices.get(sym)
                if q:
                    cs[i].metric(
                        q.get("name", sym),
                        f"{q.get('price', 0):.2f}",
                        f"{q.get('pct_change', 0):+.2f}%",
                    )
            st.caption(f"asof: {macro.get('asof_date', '-')} ｜ USDTWD={macro.get('usdtwd', 0):.3f}")

        st.markdown("##### 對應美股客戶 / 供應鏈夥伴")
        if related:
            stocks = macro.get("stocks", {}) if macro else {}
            rows = []
            for r in related:
                us = r.get("us_ticker", "")
                q = stocks.get(us) or {}
                rows.append({
                    "美股代號": us,
                    "美股名稱": r.get("us_name", us),
                    "類別": r.get("us_category", ""),
                    "供應鏈角色": r.get("role", ""),
                    "權重": r.get("weight", 0),
                    "當夜表現": (
                        f"{q.get('pct_change', 0):+.2f}%" if q else "—"
                    ),
                    "收盤價(USD)": q.get("price", "—") if q else "—",
                })
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
            avg_pct = (
                sum(float((macro.get("stocks", {}).get(r["美股代號"]) or {}).get("pct_change", 0))
                    * float(r["權重"]) for r in rows if r["當夜表現"] != "—")
                / max(1.0, sum(float(r["權重"]) for r in rows if r["當夜表現"] != "—"))
            )
            st.metric("加權平均連動 %", f"{avg_pct:+.2f}%")
        else:
            st.info(
                f"`{snap.ticker}` 在 supply_chain.json 找不到對應美股客戶。\n"
                "可以到「美股 / 跨市場」頁編輯對照表。"
            )

        if adr:
            st.markdown("##### 本股有 ADR — 溢價分析")
            cols = st.columns(4)
            cols[0].metric(f"{adr.get('adr_symbol')} (USD)", f"{adr.get('adr_price_usd', 0):.2f}")
            cols[1].metric("公允台股價", f"{adr.get('fair_tw_price', 0):.2f}")
            cols[2].metric("實際台股價", f"{adr.get('tw_price_twd', 0):.2f}")
            prem = adr.get("premium_pct", 0)
            cols[3].metric(
                "溢價/折價", f"{prem:+.2f}%",
                delta_color="normal" if abs(prem) < 1 else ("inverse" if prem < 0 else "off"),
            )
            if prem > 1.5:
                st.success("ADR 溢價 — 隔日台股早盤偏多訊號")
            elif prem < -1.5:
                st.error("ADR 折價 — 隔日台股早盤偏空訊號")
            else:
                st.info("ADR 與母股接近平價，無顯著訊號")

    with tabs[7]:
        st.markdown("#### 原始參考資料 / 來源檔")
        st.caption("此股票出現過的所有原始下載檔；點檔名可在系統檔案總管定位")
        if snap.source_files:
            df_src = pd.DataFrame(snap.source_files)
            st.dataframe(df_src, hide_index=True, use_container_width=True)
        else:
            st.info("尚無關聯原始檔。")

        st.markdown("#### 持有此股的主動式 ETF")
        if snap.held_by_etfs:
            st.dataframe(
                pd.DataFrame(snap.held_by_etfs),
                hide_index=True, use_container_width=True,
            )
        else:
            st.info("目前無主動 ETF 持有 (或尚未抓取 ETF 持股)")

        if snap.pipeline_run_dir:
            st.markdown(f"**Pipeline run 目錄**: `{snap.pipeline_run_dir}`")

    with tabs[3]:
        st.markdown("#### 三大法人 / 借券 / 融資 (近 N 日累計)")
        if snap.chip_summary:
            base = {k: v for k, v in snap.chip_summary.items() if k != "rows"}
            cols = st.columns(4)
            cols[0].metric("外資 (張)", f"{base.get('foreign_net', 0):+,.0f}")
            cols[1].metric("投信 (張)", f"{base.get('investment_trust_net', 0):+,.0f}")
            cols[2].metric("自營商 (張)", f"{base.get('dealer_net', 0):+,.0f}")
            cols[3].metric(
                "鉅額交易 (張)", f"{base.get('block_trade_net', 0):+,.0f}",
            )
            with st.expander("詳細數據 (JSON)", expanded=False):
                st.json(base)

            rows = snap.chip_summary.get("rows") or []
            if rows:
                # ---- 法人累計買賣超 + 股價疊加圖 ----
                df_c = pd.DataFrame(rows)
                if {"date", "foreign_net", "investment_trust_net"}.issubset(df_c.columns):
                    df_c = df_c.sort_values("date")
                    df_c["外資累計"] = df_c["foreign_net"].cumsum()
                    df_c["投信累計"] = df_c["investment_trust_net"].cumsum()
                    if "dealer_net" in df_c.columns:
                        df_c["自營累計"] = df_c["dealer_net"].cumsum()

                    st.markdown("**📈 法人累計買賣超走勢 (近 N 日)**")
                    try:
                        import altair as alt
                        keep_cols = ["date", "外資累計", "投信累計"]
                        if "自營累計" in df_c.columns:
                            keep_cols.append("自營累計")
                        long = df_c[keep_cols].melt(
                            id_vars=["date"], var_name="法人", value_name="累計張數",
                        )
                        chart = alt.Chart(long).mark_line(
                            point=alt.OverlayMarkDef(size=60),
                        ).encode(
                            x=alt.X("date:T", title="日期"),
                            y=alt.Y(
                                "累計張數:Q", title="累計買賣超 (張)",
                                scale=alt.Scale(zero=True),
                            ),
                            color=alt.Color(
                                "法人:N",
                                scale=alt.Scale(
                                    domain=["外資累計", "投信累計", "自營累計"],
                                    range=["#1f77b4", "#ff7f0e", "#2ca02c"],
                                ),
                            ),
                            tooltip=["date", "法人", "累計張數"],
                        ).properties(height=260)
                        zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(
                            color="gray", strokeDash=[4, 4],
                        ).encode(y="y:Q")
                        st.altair_chart(zero + chart, use_container_width=True)
                    except Exception:
                        st.line_chart(df_c.set_index("date")[
                            [c for c in ["外資累計", "投信累計", "自營累計"] if c in df_c.columns]
                        ])

                st.markdown("**逐日明細**")
                st.dataframe(df_c, hide_index=True, use_container_width=True)
        else:
            st.info("尚無籌碼資料 — 點上方『即時抓籌碼』可線上抓取")

        st.markdown("#### 大戶 vs 散戶 (集保戶股權分散)")
        if snap.distribution_trend and snap.distribution_trend.weeks:
            dist = snap.distribution
            tcols = st.columns(4)
            if dist:
                tcols[0].metric("大戶持股 %", f"{dist.large_holder_pct:.2f}%")
                tcols[1].metric("超大戶 %", f"{dist.whale_holder_pct:.2f}%")
                tcols[2].metric("散戶 %", f"{dist.retail_holder_pct:.2f}%")
                tcols[3].metric("大戶結構分數", f"{snap.distribution_score:.1f}")
            if snap.distribution_label:
                color_map = {
                    "accumulation": "✅", "distribution": "⚠️",
                    "neutral": "•", "no_data": "•",
                }
                st.info(f"{color_map.get(snap.distribution_label, '•')} {snap.distribution_detail}")
            df_dist = pd.DataFrame([
                {
                    "週別": w.week_date,
                    "大戶 %": w.large_holder_pct,
                    "超大戶 %": w.whale_holder_pct,
                    "散戶 %": w.retail_holder_pct,
                } for w in snap.distribution_trend.weeks
            ])
            try:
                import altair as alt  # noqa: F401
                long = df_dist.melt(id_vars=["週別"], var_name="類別", value_name="比例")
                chart = alt.Chart(long).mark_line(point=True).encode(
                    x=alt.X("週別:T", title="日期"),
                    y=alt.Y("比例:Q", title="持股比例 %"),
                    color=alt.Color("類別:N"),
                ).properties(height=260)
                st.altair_chart(chart, use_container_width=True)
            except Exception:
                st.dataframe(df_dist, hide_index=True, use_container_width=True)
        else:
            st.info("尚無 TDCC 集保戶資料 — 點上方『更新 TDCC』可抓取最新一期")

        st.markdown("#### ETF 共識與訊號")
        if snap.consensus:
            st.json(snap.consensus)
        if snap.new_build_signal:
            st.success(f"共識新建倉訊號: {snap.new_build_signal}")
        if snap.add_signal:
            st.success(f"共識加碼訊號: {snap.add_signal}")

    with tabs[1]:
        _render_fundamentals_tab(snap)

    with tabs[2]:
        _render_technicals_tab(snap)

    with tabs[4]:
        _render_dividends_tab(snap)

    with tabs[5]:
        _render_quarterly_tab(snap)

    with tabs[8]:
        st.markdown("#### 四時間框架的具體進場/停損/停利")
        rows = []
        for tf in TIMEFRAMES:
            tfs = card.timeframes[tf]
            s = tfs.strategy
            rows.append({
                "時間框架": tfs.label,
                "建議": tfs.action_label,
                "分數": f"{tfs.total:.1f}",
                "進場": s.get("entry", "—"),
                "停損": s.get("stop", "—"),
                "停利": s.get("target", "—"),
                "盈虧比": s.get("rrr", "—"),
                "部位": s.get("size_hint", "—"),
                "持有": s.get("horizon", "—"),
                "可進場": "是" if s.get("applicable") else "否",
            })
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

        st.markdown("#### 進場邏輯說明")
        for tf in TIMEFRAMES:
            tfs = card.timeframes[tf]
            applicable = tfs.strategy.get("applicable")
            tag = "✓" if applicable else "—"
            st.markdown(
                f"- **{tag} {tfs.label}** ({tfs.action_label}, {tfs.total:.1f} 分): "
                f"{tfs.strategy.get('entry_logic', tfs.strategy.get('note','-'))}"
            )

    with tabs[9]:
        st.markdown("#### 持倉狀況")
        if snap.position_qty > 0:
            c1, c2, c3 = st.columns(3)
            c1.metric("持倉張數", f"{snap.position_qty:.0f}")
            c2.metric("平均成本", f"{snap.position_avg_cost:.2f}")
            if snap.unrealized_pl is not None:
                c3.metric("未實現損益", f"{snap.unrealized_pl:+,.0f}")
        else:
            st.info("目前無持倉 (依 data/trades_*.csv FIFO 估算)")

        st.markdown("#### 此股的交易紀錄")
        if snap.trades:
            df = pd.DataFrame([dataclasses_asdict(t) for t in snap.trades])
            st.dataframe(df, hide_index=True, use_container_width=True)
        else:
            st.info("無交易紀錄")

    with tabs[10]:
        st.markdown("#### 歷次 Pipeline 對此股的分析")
        if snap.history:
            df = pd.DataFrame([{
                "run_id": h.run_id,
                "時間": h.started_at,
                "LLM 情緒": h.llm_sentiment or "—",
                "情緒分": f"{h.llm_score:+.2f}" if h.llm_score is not None else "—",
                "言行反查": h.logic_verdict or "—",
                "外資累計": f"{h.foreign_net:+.0f}" if h.foreign_net is not None else "—",
                "備註": h.note,
            } for h in snap.history])
            st.dataframe(df, hide_index=True, use_container_width=True)
        else:
            st.info("尚無歷史紀錄。後續每次跑 pipeline 都會累積。")


def dataclasses_asdict(obj):
    """避免引入太多 import，包一層。"""
    import dataclasses as dc
    return dc.asdict(obj)


# ======================================================================
# 個股 360 度分析 — 基本面 / 技術面 / 配股配息 / 季報 tabs
# ======================================================================


def _render_fundamentals_tab(snap) -> None:
    """基本面：估值量表 + 月營收走勢 + 三率走勢。"""
    f = snap.fundamentals
    if not f or not f.has_data:
        st.info(
            "尚無基本面資料。請在上方勾選『更新基本面』讓系統呼叫 TWSE OpenAPI，"
            "或將季度 EPS/三率 JSON 放到 `data/fundamentals_manual/<ticker>.json`。"
        )
        return

    cols = st.columns(5)
    val = f.valuation
    cols[0].metric("本益比 PER", f"{val.pe_ratio:.2f}" if val and val.pe_ratio else "—")
    cols[1].metric("股價淨值比 PBR", f"{val.pb_ratio:.2f}" if val and val.pb_ratio else "—")
    cols[2].metric(
        "現金殖利率", f"{val.dividend_yield:.2f}%" if val and val.dividend_yield else "—",
    )
    streak = f.revenue_yoy_streak()
    cols[3].metric("月營收 YoY 連續", f"{streak} 個月" if streak else "—")
    last_rev = f.latest_revenue()
    cols[4].metric(
        "最近月營收 YoY",
        f"{last_rev.yoy:+.1f}%" if last_rev and last_rev.yoy else "—",
    )

    st.markdown("#### 每月營收走勢 (YoY / MoM)")
    if f.revenues:
        df_rev = pd.DataFrame([
            {
                "年月": f"{r.year}/{r.month:02d}",
                "營收 (千元)": r.revenue,
                "YoY %": r.yoy,
                "MoM %": r.mom,
                "累計 YoY %": r.cum_yoy,
            } for r in sorted(f.revenues, key=lambda x: (x.year, x.month))
        ])
        try:
            import altair as alt  # noqa: F401
            base = alt.Chart(df_rev).encode(x=alt.X("年月:N", title="年月"))
            bar = base.mark_bar(opacity=0.4, color="#4c78a8").encode(
                y=alt.Y("營收 (千元):Q", title="營收"),
                tooltip=["年月", "營收 (千元)", "YoY %", "MoM %"],
            )
            yoy_line = base.mark_line(color="#e45756", point=True).encode(
                y=alt.Y("YoY %:Q", title="YoY %"),
            )
            chart = alt.layer(bar, yoy_line).resolve_scale(y="independent").properties(height=320)
            st.altair_chart(chart, use_container_width=True)
        except Exception:
            pass
        st.dataframe(df_rev, hide_index=True, use_container_width=True)
    else:
        st.info("無月營收歷史 — 重新更新基本面或手動補資料。")

    st.markdown("#### 三率走勢 (毛利率 / 營業利益率 / 淨利率)")
    if f.quarterlies:
        df_q = pd.DataFrame([
            {
                "年季": f"{q.year}Q{q.quarter}",
                "毛利率 %": q.gross_margin,
                "營業利益率 %": q.operating_margin,
                "淨利率 %": q.net_margin,
                "EPS": q.eps,
            } for q in sorted(f.quarterlies, key=lambda x: (x.year, x.quarter))
        ])
        try:
            import altair as alt  # noqa: F401
            melted = df_q.melt(
                id_vars=["年季"],
                value_vars=["毛利率 %", "營業利益率 %", "淨利率 %"],
                var_name="指標", value_name="%",
            )
            chart = alt.Chart(melted).mark_line(point=True).encode(
                x=alt.X("年季:N", title="年季"),
                y=alt.Y("%:Q", title="比率 (%)"),
                color="指標:N",
            ).properties(height=300)
            st.altair_chart(chart, use_container_width=True)
        except Exception:
            pass
        st.dataframe(df_q, hide_index=True, use_container_width=True)
    else:
        st.info(
            "無季度三率資料。建議手動建立 "
            "`data/fundamentals_manual/<ticker>.json` 並填入 quarterlies "
            "（欄位：year / quarter / eps / gross_margin / operating_margin / net_margin / roe）。"
        )

    if val:
        st.caption(
            f"估值資料來源：TWSE OpenAPI BWIBBU_ALL · 殖利率所屬年度 = `{val.dividend_year or '-'}`"
        )


def _render_technicals_tab(snap) -> None:
    """技術面：KPI + 訊號 + 完整 K 線工作台 (時間範圍 / Y 軸 / 面板選擇 / 往前抓)。"""
    t = snap.technicals
    if not t or not t.has_data:
        st.info(
            "尚無日 K 資料。請在上方勾選『抓日K + 指標』，系統會打 TWSE STOCK_DAY "
            "抓近 6 個月日 K 線 (含成交量) 並計算技術指標。可在下方圖表用「往更早抓取」延伸到 5/10 年前。"
        )
        return

    # ---- KPI ----
    cols = st.columns(6)
    cols[0].metric("收盤", f"{t.last_close:.2f}" if t.last_close else "—")
    cols[1].metric("1 日 %", f"{t.pct_change_1d:+.2f}%")
    cols[2].metric("5 日 %", f"{t.pct_change_5d:+.2f}%")
    cols[3].metric("20 日 %", f"{t.pct_change_20d:+.2f}%")
    cols[4].metric("60 日 %", f"{t.pct_change_60d:+.2f}%")
    cols[5].metric("技術面分", f"{t.technical_score:.1f}")

    # ---- 訊號 + 指標值 ----
    sigcols = st.columns(2)
    sigcols[0].markdown("**訊號摘要**")
    bullish = [s for s in t.signals if s.get("bullish") is True]
    bearish = [s for s in t.signals if s.get("bullish") is False]
    neutral = [s for s in t.signals if s.get("bullish") is None]
    for s in bullish:
        sigcols[0].success(f"✅ {s['label']}：{s['detail']}")
    for s in bearish:
        sigcols[0].error(f"⚠️ {s['label']}：{s['detail']}")
    for s in neutral:
        sigcols[0].info(f"• {s['label']}：{s['detail']}")

    with sigcols[1]:
        st.markdown("**指標數值**")
        st.dataframe(
            pd.DataFrame([
                {"指標": "RSI(14)", "值": t.rsi14},
                {"指標": "K", "值": t.k},
                {"指標": "D", "值": t.d},
                {"指標": "MACD", "值": t.macd},
                {"指標": "Signal", "值": t.macd_signal},
                {"指標": "MACD Hist", "值": t.macd_hist},
                {"指標": "MA5", "值": t.ma5},
                {"指標": "MA20", "值": t.ma20},
                {"指標": "MA60", "值": t.ma60},
                {"指標": "MA120", "值": t.ma120},
            ]),
            hide_index=True, use_container_width=True,
        )

    # ---- 完整 K 線工作台 (含 K 線型態判讀) ----
    st.markdown("### K 線工作台")
    try:
        from bot.technicals import (
            build_technical_snapshot,
            load_kline_from_db,
        )
        # 先試 SQLite (含所有歷史)，沒有再退回 CSV 透過 build_technical_snapshot
        df = load_kline_from_db(snap.ticker, root=PROJECT_ROOT)
        if df is None or df.empty:
            _, df = build_technical_snapshot(
                snap.ticker, root=PROJECT_ROOT, refresh=False,
            )
    except Exception:
        df = None

    if df is None or df.empty:
        st.info("無 K 線 DataFrame；請先勾選『抓日K + 指標』後再分析一次。")
        return

    _render_kline_workspace(
        snap.ticker, df,
        key_prefix=f"tech_{snap.ticker}",
        default_window="近 6 個月",
        default_panels=("kline", "volume", "macd", "rsi"),
        show_fetch_more=True,
    )


def _render_dividends_tab(snap) -> None:
    """股利政策：歷年配息穩定度 + 盈餘分配率。"""
    f = snap.fundamentals
    if not f or not f.dividends:
        st.info(
            "尚無股利紀錄。請勾選『更新基本面』讓系統呼叫 TWSE OpenAPI；"
            "舊年度需要逐年累積。"
        )
        return
    df_d = pd.DataFrame([
        {
            "年度": d.year,
            "現金股利": d.cash_dividend,
            "股票股利": d.stock_dividend,
            "合計": round(d.cash_dividend + d.stock_dividend, 2),
            "除息日": d.ex_dividend_date,
            "除權日": d.ex_right_date,
            "填息日": d.fill_date,
            "填息天數": d.fill_days,
            "盈餘分配率 %": d.payout_ratio,
        } for d in sorted(f.dividends, key=lambda x: x.year)
    ])

    cols = st.columns(3)
    cols[0].metric(
        "平均盈餘分配率 (近 5 年)",
        f"{f.avg_payout_ratio() or 0:.1f}%" if f.avg_payout_ratio() else "—",
    )
    last_cash = df_d["現金股利"].iloc[-1] if not df_d.empty else 0
    cols[1].metric("最新一年現金股利", f"{last_cash:.2f}")
    if not df_d.empty:
        cols[2].metric(
            "近 5 年配息穩定度 (std/mean)",
            _stability(df_d["現金股利"].tail(5).tolist()),
        )

    try:
        import altair as alt  # noqa: F401
        long = df_d.melt(
            id_vars=["年度"],
            value_vars=["現金股利", "股票股利"],
            var_name="類別", value_name="元",
        )
        chart = alt.Chart(long).mark_bar().encode(
            x=alt.X("年度:O", title="年度"),
            y=alt.Y("元:Q", title="每股股利"),
            color="類別:N",
        ).properties(height=280, title="歷年股利")
        st.altair_chart(chart, use_container_width=True)
    except Exception:
        pass
    st.dataframe(df_d, hide_index=True, use_container_width=True)

    st.caption(
        "盈餘分配率 = 現金 + 股票股利 / EPS；需要 quarterlies 完整才會計算。"
    )


def _stability(values: List[float]) -> str:
    arr = [v for v in values if v is not None]
    if len(arr) < 2:
        return "—"
    mean = sum(arr) / len(arr)
    if mean == 0:
        return "—"
    var = sum((v - mean) ** 2 for v in arr) / len(arr)
    std = var ** 0.5
    cv = std / mean
    if cv < 0.1:
        tag = "✅ 穩定"
    elif cv < 0.3:
        tag = "🟡 中等"
    else:
        tag = "🔴 波動大"
    return f"{cv:.2%} {tag}"


def _render_quarterly_tab(snap) -> None:
    """季報 Q1～Q4 框架 + 滾動 EPS。"""
    qv = snap.quarterly_view or {}
    focus = qv.get("current_focus") or {}
    rolling = qv.get("rolling_eps") or []
    qrev = qv.get("quarterly_revenue") or []

    if focus:
        st.markdown(f"### 目前市場焦點：{focus.get('year','-')} {focus.get('deadline_label','')}")
        st.info(focus.get("summary", ""))
        cA, cB = st.columns(2)
        with cA:
            st.markdown("**關鍵問題清單**")
            for q in focus.get("key_questions", []):
                st.markdown(f"- {q}")
        with cB:
            st.markdown("**重點觀察**")
            for w in focus.get("what_to_watch", []):
                st.markdown(f"- {w}")
        st.caption(
            f"公佈期限：{focus.get('deadline_string','-')}；"
            f"焦點季度 = Q{focus.get('quarter','-')}"
        )

    st.markdown("#### 滾動式累計 EPS (Q1/H1/9M/FY 進度)")
    if rolling:
        df_eps = pd.DataFrame(rolling)
        df_eps_disp = df_eps.rename(columns={
            "label": "節點", "year": "年度", "eps_sum": "累計 EPS",
            "prev_year_eps_sum": "去年同期", "diff_pct": "與去年同期 %", "note": "備註",
        })
        st.dataframe(
            df_eps_disp[["年度", "節點", "累計 EPS", "去年同期", "與去年同期 %", "備註"]],
            hide_index=True, use_container_width=True,
        )
        try:
            import altair as alt  # noqa: F401
            df_eps["key"] = df_eps["year"].astype(str) + " " + df_eps["label"]
            long = df_eps.melt(
                id_vars=["key"], value_vars=["eps_sum", "prev_year_eps_sum"],
                var_name="序列", value_name="EPS",
            )
            chart = alt.Chart(long).mark_bar().encode(
                x=alt.X("key:N", title="年度 / 節點", sort=None),
                y=alt.Y("EPS:Q"),
                color="序列:N",
                xOffset="序列:N",
            ).properties(height=260, title="滾動 EPS vs 去年同期")
            st.altair_chart(chart, use_container_width=True)
        except Exception:
            pass
    else:
        st.info(
            "尚無季度 EPS 資料。請手動建立 `data/fundamentals_manual/<ticker>.json`：\n\n"
            "```json\n"
            "{\n  \"ticker\": \"2330\",\n  \"quarterlies\": [\n"
            "    {\"year\": 2025, \"quarter\": 3, \"eps\": 14.71, "
            "\"gross_margin\": 59.1, \"operating_margin\": 48.5, "
            "\"net_margin\": 41.0, \"roe\": 8.2}\n"
            "  ]\n}\n```"
        )

    st.markdown("#### 季度營收聚合 (Q1=1-3 / Q2=4-6 / Q3=7-9 / Q4=10-12)")
    if qrev:
        df_qr = pd.DataFrame([
            {
                "年季": f"{r['year']}Q{r['quarter']}",
                "月份": ",".join(map(str, r.get("months", []))),
                "季營收": r.get("revenue", 0),
                "去年同期": r.get("revenue_last_year", 0),
                "YoY %": r.get("yoy", 0),
            } for r in qrev
        ])
        st.dataframe(df_qr, hide_index=True, use_container_width=True)
        try:
            import altair as alt  # noqa: F401
            chart = alt.Chart(df_qr).mark_bar(color="#4c78a8").encode(
                x=alt.X("年季:N", sort=None),
                y=alt.Y("季營收:Q"),
                tooltip=["年季", "季營收", "YoY %"],
            ).properties(height=240, title="季度營收")
            st.altair_chart(chart, use_container_width=True)
        except Exception:
            pass
    else:
        st.info("無月營收歷史，無法聚合季度營收。")


# ======================================================================
# K 線繪圖共用 panel builders (個股深入分析 + K 線看板共用)
# ======================================================================


# 可顯示的面板列表 (順序即堆疊預設順序)
KLINE_PANELS = [
    ("kline", "📈 K 線 + 均線"),
    ("volume", "📊 成交量"),
    ("macd", "MACD"),
    ("rsi", "RSI"),
    ("kd", "KD"),
    ("boll", "布林通道"),
]


# 時間範圍預設 (label, 約略交易日數)
KLINE_WINDOWS = [
    ("近 1 個月", 22),
    ("近 3 個月", 66),
    ("近 6 個月", 130),
    ("近 1 年", 250),
    ("近 2 年", 500),
    ("近 3 年", 750),
    ("近 5 年", 1250),
    ("近 10 年", 2500),
    ("全部", None),
    ("自訂日期", -1),
]


def _auto_candle_width(row_count: Optional[int]) -> int:
    """依目前顯示根數給一個容易閱讀的 K 棒寬度。"""
    if not row_count or row_count <= 0:
        return 4
    if row_count <= 25:
        return 12
    if row_count <= 45:
        return 10
    if row_count <= 80:
        return 8
    if row_count <= 140:
        return 6
    if row_count <= 260:
        return 4
    if row_count <= 520:
        return 3
    if row_count <= 1000:
        return 2
    return 1


def _candle_color_col(df: pd.DataFrame) -> pd.DataFrame:
    """加上 color 欄位 (台股慣例：紅漲綠跌)。"""
    out = df.copy()
    out["color"] = (out["close"] >= out["open"]).map({
        True: "#d64545", False: "#1d9c5b",
    })
    return out


def _kline_panel(
    df: pd.DataFrame,
    *,
    ma_periods: Tuple[int, ...] = (5, 20, 60),
    bollinger: bool = False,
    y_min: Optional[float] = None,
    y_max: Optional[float] = None,
    height: int = 320,
    candle_width: int = 4,
    title: str = "",
    x_axis: bool = True,
):
    """K 線 + 均線 + (可選) 布林通道 panel。"""
    import altair as alt
    if df.empty:
        return None
    candle_width = max(1, int(candle_width))
    d = _candle_color_col(df)
    d["date"] = pd.to_datetime(d["date"])
    for p in ma_periods:
        col = f"ma{p}"
        if col not in d.columns:
            d[col] = d["close"].rolling(p, min_periods=1).mean()

    y_scale = alt.Scale(zero=False)
    if y_min is not None and y_max is not None and y_max > y_min:
        y_scale = alt.Scale(domain=[y_min, y_max], zero=False, clamp=True)

    x_enc = alt.X(
        "date:T",
        title="日期" if x_axis else None,
        axis=alt.Axis() if x_axis else None,
    )
    base = alt.Chart(d).encode(x=x_enc)
    tooltip_fields = [
        alt.Tooltip("date:T", title="日期"),
        alt.Tooltip("open:Q", title="開", format=".2f"),
        alt.Tooltip("high:Q", title="高", format=".2f"),
        alt.Tooltip("low:Q", title="低", format=".2f"),
        alt.Tooltip("close:Q", title="收", format=".2f"),
        alt.Tooltip("volume:Q", title="量(張)", format=",.0f"),
    ]
    for p in ma_periods:
        col = f"ma{p}"
        if col in d.columns:
            tooltip_fields.append(alt.Tooltip(f"{col}:Q", title=f"MA{p}", format=".2f"))
    if bollinger and {"boll_upper", "boll_lower", "boll_mid"}.issubset(d.columns):
        tooltip_fields.extend([
            alt.Tooltip("boll_upper:Q", title="布林上", format=".2f"),
            alt.Tooltip("boll_mid:Q", title="布林中", format=".2f"),
            alt.Tooltip("boll_lower:Q", title="布林下", format=".2f"),
        ])

    hover_cols = ["low", "high", *[f"ma{p}" for p in ma_periods]]
    if bollinger:
        hover_cols.extend(["boll_upper", "boll_lower", "boll_mid"])
    hover_cols = [col for col in hover_cols if col in d.columns]
    hover_low = y_min if y_min is not None and y_max is not None and y_max > y_min else float(d[hover_cols].min().min())
    hover_high = y_max if y_min is not None and y_max is not None and y_max > y_min else float(d[hover_cols].max().max())
    d["_hover_low"] = hover_low
    d["_hover_high"] = hover_high

    rule = base.mark_rule().encode(
        y=alt.Y("low:Q", scale=y_scale, title="價"),
        y2="high:Q",
        color=alt.Color("color:N", scale=None, legend=None),
        tooltip=tooltip_fields,
    )
    bar = base.mark_bar(size=candle_width).encode(
        y="open:Q", y2="close:Q",
        color=alt.Color("color:N", scale=None, legend=None),
    )
    ma_colors = {
        "ma5": "#ffa500", "ma10": "#2ca02c", "ma20": "#1f77b4",
        "ma60": "#9467bd", "ma120": "#8c564b", "ma240": "#7f7f7f",
    }
    ma_layers = []
    for p in ma_periods:
        col = f"ma{p}"
        ma_layers.append(
            base.mark_line(
                color=ma_colors.get(col, "#888"),
                strokeWidth=1.4,
            ).encode(y=f"{col}:Q"),
        )

    boll_layers = []
    if bollinger and {"boll_upper", "boll_lower", "boll_mid"}.issubset(d.columns):
        boll_band = base.mark_area(opacity=0.08, color="#3478b6").encode(
            y="boll_lower:Q", y2="boll_upper:Q",
        )
        boll_mid = base.mark_line(strokeDash=[4, 3], color="#3478b6").encode(
            y="boll_mid:Q",
        )
        boll_layers = [boll_band, boll_mid]

    # 透明整高 bar 讓滑鼠只要在價格面板的 Y 範圍內，就能看到該日細節。
    hover = base.mark_bar(
        color="#000000",
        opacity=0.001,
        size=max(10, candle_width + 8),
    ).encode(
        y=alt.Y("_hover_low:Q", scale=y_scale, title="價"),
        y2="_hover_high:Q",
        tooltip=tooltip_fields,
    )

    layers = [*boll_layers, rule, bar, *ma_layers, hover]
    return alt.layer(*layers).properties(height=height, title=title or "")


def _volume_panel(df: pd.DataFrame, *, height: int = 110, title: str = "成交量"):
    import altair as alt
    if df.empty:
        return None
    d = _candle_color_col(df)
    d["date"] = pd.to_datetime(d["date"])
    return alt.Chart(d).mark_bar(opacity=0.6).encode(
        x=alt.X("date:T", title=""),
        y=alt.Y("volume:Q", title="成交張"),
        color=alt.Color("color:N", scale=None, legend=None),
        tooltip=[
            alt.Tooltip("date:T", title="日期"),
            alt.Tooltip("volume:Q", title="量(張)"),
        ],
    ).properties(height=height, title=title)


def _macd_panel(df: pd.DataFrame, *, height: int = 140, title: str = "MACD"):
    import altair as alt
    if df.empty or "macd" not in df.columns:
        return None
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"])
    line = alt.Chart(d).transform_fold(
        ["macd", "macd_signal"], as_=["指標", "值"],
    ).mark_line().encode(
        x=alt.X("date:T", title=""),
        y=alt.Y("值:Q", title="MACD"),
        color="指標:N",
    )
    hist = alt.Chart(d).mark_bar(opacity=0.5).encode(
        x="date:T",
        y="macd_hist:Q",
        color=alt.condition(
            "datum.macd_hist >= 0",
            alt.value("#d64545"), alt.value("#1d9c5b"),
        ),
    )
    return alt.layer(hist, line).properties(height=height, title=title)


def _rsi_panel(df: pd.DataFrame, *, height: int = 140, title: str = "RSI(14)"):
    import altair as alt
    if df.empty or "rsi14" not in df.columns:
        return None
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"])
    line = alt.Chart(d).mark_line(color="#e377c2").encode(
        x=alt.X("date:T", title=""),
        y=alt.Y("rsi14:Q", title="RSI14", scale=alt.Scale(domain=[0, 100])),
    )
    overbought = alt.Chart(pd.DataFrame({"y": [70]})).mark_rule(
        strokeDash=[3, 3], color="#d64545",
    ).encode(y="y:Q")
    oversold = alt.Chart(pd.DataFrame({"y": [30]})).mark_rule(
        strokeDash=[3, 3], color="#1d9c5b",
    ).encode(y="y:Q")
    return alt.layer(line, overbought, oversold).properties(height=height, title=title)


def _kd_panel(df: pd.DataFrame, *, height: int = 140, title: str = "KD"):
    import altair as alt
    if df.empty or "k" not in df.columns or "d" not in df.columns:
        return None
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"])
    return alt.Chart(d).transform_fold(
        ["k", "d"], as_=["指標", "值"],
    ).mark_line().encode(
        x=alt.X("date:T", title=""),
        y=alt.Y("值:Q", title="KD", scale=alt.Scale(domain=[0, 100])),
        color="指標:N",
    ).properties(height=height, title=title)


def _boll_panel(df: pd.DataFrame, *, height: int = 220, title: str = "布林通道"):
    """獨立顯示布林（與 K 線 panel 的 bollinger 重複時可關掉它）。"""
    import altair as alt
    if df.empty or "boll_upper" not in df.columns:
        return None
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"])
    band = alt.Chart(d).mark_area(opacity=0.15, color="#3478b6").encode(
        x="date:T", y="boll_lower:Q", y2="boll_upper:Q",
    )
    close = alt.Chart(d).mark_line(color="#222").encode(
        x=alt.X("date:T", title=""),
        y=alt.Y("close:Q", title="價", scale=alt.Scale(zero=False)),
    )
    mid = alt.Chart(d).mark_line(strokeDash=[4, 3], color="#3478b6").encode(
        x="date:T", y="boll_mid:Q",
    )
    return alt.layer(band, mid, close).properties(height=height, title=title)


def _build_df_with_indicators(
    df: pd.DataFrame,
    *,
    ma_periods: Tuple[int, ...] = (5, 10, 20, 60, 120),
) -> pd.DataFrame:
    """確保 df 含技術指標欄位 (給 panel 用)。"""
    from bot.technicals import compute_indicators
    if df is None or df.empty:
        return pd.DataFrame()
    if "macd" in df.columns and "rsi14" in df.columns:
        return df.copy()
    try:
        return compute_indicators(df, ma_periods=list(ma_periods))
    except Exception:
        return df.copy()


def _slice_df_by_window(
    df: pd.DataFrame,
    window_label: str,
    *,
    custom_start: Optional[dt.date] = None,
    custom_end: Optional[dt.date] = None,
) -> pd.DataFrame:
    """依照「近 N 月/年 / 自訂日期」剪 df。"""
    if df is None or df.empty:
        return df
    sorted_df = df.sort_values("date").reset_index(drop=True)
    if window_label == "全部":
        return sorted_df
    if window_label == "自訂日期":
        d = sorted_df.copy()
        d["_d"] = pd.to_datetime(d["date"]).dt.date
        if custom_start:
            d = d[d["_d"] >= custom_start]
        if custom_end:
            d = d[d["_d"] <= custom_end]
        return d.drop(columns=["_d"])
    # 對應 KLINE_WINDOWS 的 N 個交易日
    for lbl, days in KLINE_WINDOWS:
        if lbl == window_label and isinstance(days, int) and days > 0:
            return sorted_df.tail(days).reset_index(drop=True)
    return sorted_df


def _render_candle_pattern_section(df: pd.DataFrame, *, key_prefix: str) -> None:
    """K 線型態判讀區塊：最新一根型態 + 近 10 根型態表。

    被「個股深入分析 - 技術面」「K 線看板 - 單檔展開」共用 (直接吃 df，不依賴 snapshot)。
    """
    try:
        from bot.candle_patterns import classify_latest, classify_recent
        latest = classify_latest(df)
        recent = classify_recent(df, n=10)
    except Exception:
        latest, recent = None, []

    if latest is None:
        return

    bias = latest.bias
    badge = (
        "🔴 偏多" if bias is True
        else "🟢 偏空" if bias is False
        else "⚪ 中性 / 取決於位置"
    )
    box = st.success if bias is True else st.error if bias is False else st.info
    box(
        f"**最新 K 線型態：{latest.name}**（{latest.category}） · {badge}"
        f" ｜ 實體 {latest.body_pct * 100:.0f}% / 上影 {latest.upper_pct * 100:.0f}%"
        f" / 下影 {latest.lower_pct * 100:.0f}%\n\n"
        f"{latest.meaning}"
    )

    if recent:
        with st.expander("🕯 近 10 根 K 線型態判讀", expanded=False):
            st.caption(
                "依「實體大小 + 上下影線比例」分類；偏多 🔴 / 偏空 🟢 / 中性 ⚪。"
                "型態僅供參考，需搭配位置與其他指標判讀。"
            )
            prows = []
            for r in recent:
                b = r.get("bias")
                prows.append({
                    "日期": r.get("date", ""),
                    "型態": r.get("name", ""),
                    "類別": r.get("category", ""),
                    "方向": "🔴偏多" if b is True else "🟢偏空" if b is False else "⚪中性",
                    "反轉訊號": "✔" if r.get("reversal") else "",
                    "實體%": round(r.get("body_pct", 0) * 100, 1),
                    "上影%": round(r.get("upper_pct", 0) * 100, 1),
                    "下影%": round(r.get("lower_pct", 0) * 100, 1),
                    "市場訊號": r.get("meaning", ""),
                })
            st.dataframe(
                pd.DataFrame(prows), hide_index=True, use_container_width=True,
            )


def _render_kline_workspace(
    ticker: str,
    df: pd.DataFrame,
    *,
    key_prefix: str,
    default_window: str = "近 6 個月",
    default_panels: Tuple[str, ...] = ("kline", "volume", "macd", "rsi"),
    show_fetch_more: bool = True,
) -> None:
    """K 線完整工作台：時間範圍 / Y 軸 / 面板選擇 / 堆疊或分頁 + 往前批量抓。

    被「個股深入分析 - 技術面」與「K 線看板 - 單檔展開」共用。
    """
    from bot.technicals import (
        extend_kline_backward,
        get_kline_coverage,
    )

    if df is None or df.empty:
        st.info("無 K 線資料 — 請先抓取。")
        return

    df = _build_df_with_indicators(df).sort_values("date").reset_index(drop=True)
    df["_d"] = pd.to_datetime(df["date"]).dt.date
    earliest = df["_d"].min()
    latest = df["_d"].max()

    # ===== K 線型態判讀 (最新一根 + 近 10 根) =====
    _render_candle_pattern_section(df.drop(columns=["_d"]), key_prefix=key_prefix)

    # ===== 控制列 =====
    c1, c2, c3, c4 = st.columns([1.4, 1.4, 1.4, 1.4])
    window_label = c1.selectbox(
        "時間範圍",
        [w[0] for w in KLINE_WINDOWS],
        index=[w[0] for w in KLINE_WINDOWS].index(default_window)
        if default_window in [w[0] for w in KLINE_WINDOWS] else 2,
        key=f"{key_prefix}_window",
    )

    custom_start = c2.date_input(
        "自訂起日",
        value=max(earliest, (latest - dt.timedelta(days=365))),
        min_value=earliest, max_value=latest,
        key=f"{key_prefix}_start",
        disabled=(window_label != "自訂日期"),
    )
    custom_end = c3.date_input(
        "自訂迄日",
        value=latest, min_value=earliest, max_value=latest,
        key=f"{key_prefix}_end",
        disabled=(window_label != "自訂日期"),
    )
    layout_mode = c4.radio(
        "排列",
        ["堆疊", "分頁", "並排兩欄"],
        horizontal=True, key=f"{key_prefix}_layout",
    )

    df_slice = _slice_df_by_window(
        df.drop(columns=["_d"]),
        window_label,
        custom_start=custom_start if window_label == "自訂日期" else None,
        custom_end=custom_end if window_label == "自訂日期" else None,
    )
    if df_slice is None or df_slice.empty:
        st.warning("這個範圍內沒有任何 K 線資料。請選更寬的時間範圍。")
        return

    # ===== 面板 + Y 軸 + K 棒寬度 =====
    p1, p2, p3 = st.columns([3, 1.8, 1.6])
    panel_keys = p1.multiselect(
        "顯示面板",
        options=[k for k, _ in KLINE_PANELS],
        default=list(default_panels),
        format_func=lambda k: dict(KLINE_PANELS).get(k, k),
        key=f"{key_prefix}_panels",
    )
    y_mode = p2.radio(
        "Y 軸範圍 (K 線)", ["自動", "手動"], horizontal=True,
        key=f"{key_prefix}_ymode",
    )
    candle_width = p3.slider(
        "K 棒寬度",
        min_value=1,
        max_value=14,
        value=_auto_candle_width(len(df_slice)),
        step=1,
        key=f"{key_prefix}_candle_width_{window_label}",
        help="預設會依目前時間範圍調整；長週期可調細，短週期可調寬。",
    )

    auto_low = float(df_slice["low"].min())
    auto_high = float(df_slice["high"].max())
    y_min: Optional[float] = None
    y_max: Optional[float] = None
    if y_mode == "手動":
        ya, yb = st.columns(2)
        y_min = ya.number_input(
            "Y 下限", value=round(auto_low * 0.95, 2),
            min_value=0.0,
            key=f"{key_prefix}_ymin",
        )
        y_max = yb.number_input(
            "Y 上限", value=round(auto_high * 1.05, 2),
            min_value=0.0,
            key=f"{key_prefix}_ymax",
        )

    bollinger_overlay = "boll" in panel_keys

    # ===== 摘要 =====
    cov_cols = st.columns(4)
    cov_cols[0].metric("資料筆數", f"{len(df)} 根")
    cov_cols[1].metric("最早日期", str(earliest))
    cov_cols[2].metric("最新日期", str(latest))
    cov_cols[3].metric("此區段筆數", f"{len(df_slice)} 根")

    # ===== 往前抓 =====
    if show_fetch_more:
        with st.expander("⏪ 往更早抓取 K 線 (分批，可隨時停止)", expanded=False):
            f1, f2, f3 = st.columns([1.5, 1.5, 2])
            extend_years = f1.selectbox(
                "再往前", [1, 2, 3, 5, 10], index=2,
                format_func=lambda y: f"{y} 年",
                key=f"{key_prefix}_extend_years",
            )
            delay = f2.slider(
                "每月請求間隔 (秒)", 0.2, 2.0, 0.5, 0.1,
                key=f"{key_prefix}_delay",
                help="TWSE 對連續請求較敏感；推薦 0.4~0.8 秒。",
            )
            do_extend = f3.button(
                f"⏪ 開始抓取 {extend_years} 年更早資料",
                use_container_width=True,
                key=f"{key_prefix}_extend_btn",
            )
            if do_extend:
                _run_extend_backward(
                    ticker, extend_years, delay,
                    key_prefix=key_prefix,
                )

    # ===== 繪圖 =====
    panels: List[tuple[str, str, Any]] = []
    for key in panel_keys:
        chart = _make_panel_chart(
            key, df_slice,
            y_min=y_min, y_max=y_max,
            candle_width=candle_width,
            bollinger=bollinger_overlay and key == "kline",
            title=dict(KLINE_PANELS).get(key, key),
        )
        if chart is not None:
            panels.append((key, dict(KLINE_PANELS).get(key, key), chart))

    if not panels:
        st.info("請至少選擇一個面板。")
        return

    if layout_mode == "堆疊":
        for _, _, chart in panels:
            st.altair_chart(chart, use_container_width=True)
    elif layout_mode == "分頁":
        labels = [label for _, label, _ in panels]
        tabs_obj = st.tabs(labels)
        for i, (_, _, chart) in enumerate(panels):
            with tabs_obj[i]:
                st.altair_chart(chart, use_container_width=True)
    else:  # 並排兩欄
        for i in range(0, len(panels), 2):
            cols = st.columns(2)
            for j in range(2):
                if i + j < len(panels):
                    _, _, chart = panels[i + j]
                    with cols[j]:
                        st.altair_chart(chart, use_container_width=True)


def _make_panel_chart(
    key: str,
    df: pd.DataFrame,
    *,
    y_min: Optional[float] = None,
    y_max: Optional[float] = None,
    candle_width: int = 4,
    bollinger: bool = False,
    title: str = "",
):
    if key == "kline":
        return _kline_panel(
            df, y_min=y_min, y_max=y_max,
            candle_width=candle_width,
            bollinger=bollinger, title=title,
        )
    if key == "volume":
        return _volume_panel(df, title=title or "成交量")
    if key == "macd":
        return _macd_panel(df, title=title or "MACD")
    if key == "rsi":
        return _rsi_panel(df, title=title or "RSI(14)")
    if key == "kd":
        return _kd_panel(df, title=title or "KD")
    if key == "boll":
        return _boll_panel(df, title=title or "布林通道")
    return None


def _run_extend_backward(
    ticker: str,
    years: int,
    delay: float,
    *,
    key_prefix: str,
) -> None:
    """執行往前抓 N 年；顯示即時進度條。"""
    from bot.technicals import extend_kline_backward
    progress = st.progress(0, text="準備中…")
    counter = {"fetched_rows": 0, "months_done": 0}

    def cb(idx: int, total: int, label: str, fetched: int) -> None:
        counter["fetched_rows"] += fetched
        counter["months_done"] += 1
        ratio = idx / max(total, 1)
        progress.progress(
            min(ratio, 1.0),
            text=(
                f"[{idx}/{total}] 抓取 {ticker} {label} "
                f"-- 累計新增 {counter['fetched_rows']} 根 K 棒"
            ),
        )

    try:
        df_new = extend_kline_backward(
            ticker, years_back=years, root=PROJECT_ROOT,
            request_delay_sec=delay,
            on_progress=cb,
        )
    except Exception as e:
        st.error(f"抓取失敗：{e}")
        return
    finally:
        progress.empty()

    if df_new is None or df_new.empty:
        st.warning("沒有抓到新的資料 (可能已經有快取或超出 TWSE 可查詢區段)。")
    else:
        st.success(
            f"完成：累計新增 {counter['fetched_rows']} 根 K 棒、"
            f"快取現含 {len(df_new)} 根。請重整頁面或重新選擇時間範圍。",
        )


# ======================================================================
# 頁面: K 線看板 (Stock Board) -- 多檔股票一覽 + mini K 線
# ======================================================================


def _board_rows_summary(symbols: List[str], db: StockDB) -> pd.DataFrame:
    """組多檔股票的「最新報價 / 漲跌 / 5日 20日 % / 量比」快報。"""
    rows: List[Dict[str, object]] = []
    for sym in symbols:
        bars = db.get_price_history(sym, limit=60, ascending=True)
        if not bars:
            rows.append({
                "symbol": sym,
                "last_date": "—",
                "close": None,
                "chg_1d_%": None,
                "chg_5d_%": None,
                "chg_20d_%": None,
                "K線型態": "",
                "volume": None,
                "vol_ratio": None,
                "high_60d": None,
                "low_60d": None,
                "rows": 0,
            })
            continue
        last = bars[-1]
        prev = bars[-2] if len(bars) >= 2 else last
        d5 = bars[-6] if len(bars) >= 6 else bars[0]
        d20 = bars[-21] if len(bars) >= 21 else bars[0]

        def _pct(a: float, b: float) -> Optional[float]:
            if not b:
                return None
            return round(100.0 * (a - b) / b, 2)

        vol_recent = [b.volume for b in bars[-20:] if b.volume > 0]
        vol_ma = sum(vol_recent) / len(vol_recent) if vol_recent else 0.0

        # 最新一根 K 線型態
        pattern_label = ""
        try:
            from bot.candle_patterns import classify_candle
            bodies = [abs(b.close - b.open) for b in bars[-20:] if abs(b.close - b.open) > 0]
            avg_body = sum(bodies) / len(bodies) if bodies else None
            cp = classify_candle(
                open=last.open, high=last.high, low=last.low, close=last.close,
                avg_body=avg_body,
            )
            mark = "🔴" if cp.bias is True else "🟢" if cp.bias is False else "⚪"
            pattern_label = f"{mark} {cp.name}"
        except Exception:
            pattern_label = ""

        rows.append({
            "symbol": sym,
            "last_date": last.date,
            "close": round(last.close, 2),
            "chg_1d_%": _pct(last.close, prev.close),
            "chg_5d_%": _pct(last.close, d5.close),
            "chg_20d_%": _pct(last.close, d20.close),
            "K線型態": pattern_label,
            "volume": round(last.volume, 0),
            "vol_ratio": round(last.volume / vol_ma, 2) if vol_ma else None,
            "high_60d": round(max(b.high for b in bars), 2),
            "low_60d": round(min(b.low for b in bars if b.low > 0), 2)
                       if any(b.low > 0 for b in bars) else None,
            "rows": len(bars),
        })
    return pd.DataFrame(rows)


def _spark_chart(bars, *, height: int = 90, candle_width: int = 3):
    """單一個股的迷你 K 線縮圖 (用蠟燭 + 收盤線)。"""
    import altair as alt

    df = pd.DataFrame([{
        "date": b.date, "open": b.open, "high": b.high,
        "low": b.low, "close": b.close, "volume": b.volume,
    } for b in bars])
    if df.empty:
        return None
    candle_width = max(1, int(candle_width))
    df["date"] = pd.to_datetime(df["date"])
    df["color"] = (df["close"] >= df["open"]).map({True: "#d64545", False: "#1d9c5b"})
    df["_hover_low"] = float(df["low"].min())
    df["_hover_high"] = float(df["high"].max())
    tooltip_fields = [
        alt.Tooltip("date:T", title="日期"),
        alt.Tooltip("open:Q", title="開", format=".2f"),
        alt.Tooltip("high:Q", title="高", format=".2f"),
        alt.Tooltip("low:Q", title="低", format=".2f"),
        alt.Tooltip("close:Q", title="收", format=".2f"),
        alt.Tooltip("volume:Q", title="量(張)", format=",.0f"),
    ]

    base = alt.Chart(df).encode(
        x=alt.X("date:T", axis=None),
    )
    rule = base.mark_rule().encode(
        y=alt.Y("low:Q", axis=None, scale=alt.Scale(zero=False)),
        y2="high:Q",
        color=alt.Color("color:N", scale=None, legend=None),
        tooltip=tooltip_fields,
    )
    bar = base.mark_bar(size=candle_width).encode(
        y="open:Q", y2="close:Q",
        color=alt.Color("color:N", scale=None, legend=None),
    )
    hover = base.mark_bar(
        color="#000000",
        opacity=0.001,
        size=max(8, candle_width + 6),
    ).encode(
        y=alt.Y("_hover_low:Q", axis=None, scale=alt.Scale(zero=False)),
        y2="_hover_high:Q",
        tooltip=tooltip_fields,
    )
    return alt.layer(rule, bar, hover).properties(height=height)


def _full_kline_chart(bars, *, ma_periods=(5, 20, 60), title: str = ""):
    """完整 K + MA + 成交量子圖。"""
    import altair as alt

    df = pd.DataFrame([{
        "date": b.date, "open": b.open, "high": b.high,
        "low": b.low, "close": b.close, "volume": b.volume,
    } for b in bars])
    if df.empty:
        return None
    df["date"] = pd.to_datetime(df["date"])
    for p in ma_periods:
        df[f"ma{p}"] = df["close"].rolling(p, min_periods=1).mean()
    df["color"] = (df["close"] >= df["open"]).map({True: "#d64545", False: "#1d9c5b"})

    base = alt.Chart(df).encode(x=alt.X("date:T", title=""))
    rule = base.mark_rule().encode(
        y=alt.Y("low:Q", scale=alt.Scale(zero=False), title="價"),
        y2="high:Q",
        color=alt.Color("color:N", scale=None, legend=None),
    )
    bar = base.mark_bar(size=4).encode(
        y="open:Q", y2="close:Q",
        color=alt.Color("color:N", scale=None, legend=None),
    )
    ma_colors = {"ma5": "#ffa500", "ma20": "#1f77b4", "ma60": "#9467bd"}
    ma_layers = []
    for p in ma_periods:
        col = f"ma{p}"
        if col in df.columns:
            ma_layers.append(
                base.mark_line(color=ma_colors.get(col, "#888")).encode(
                    y=f"{col}:Q",
                ),
            )
    price = alt.layer(rule, bar, *ma_layers).properties(
        height=260, title=title or "",
    )
    vol = alt.Chart(df).mark_bar(opacity=0.5).encode(
        x=alt.X("date:T", title="日期"),
        y=alt.Y("volume:Q", title="成交張數"),
        color=alt.Color("color:N", scale=None, legend=None),
    ).properties(height=90)
    return price, vol


def _board_y_controls(key: str) -> Tuple[Optional[float], Optional[float]]:
    """Y 軸範圍 (Optional min, max)；放到 K 線 panel 用。"""
    enabled = st.toggle("手動 Y 軸範圍", value=False, key=f"{key}_y_enable")
    if not enabled:
        return None, None
    yc1, yc2 = st.columns(2)
    y_min = yc1.number_input("Y 下限", value=0.0, min_value=0.0, key=f"{key}_ymin")
    y_max = yc2.number_input("Y 上限", value=0.0, min_value=0.0, key=f"{key}_ymax")
    if y_max <= y_min:
        return None, None
    return float(y_min), float(y_max)


def _bars_to_df(bars) -> pd.DataFrame:
    if not bars:
        return pd.DataFrame()
    return pd.DataFrame([{
        "date": b.date, "open": b.open, "high": b.high,
        "low": b.low, "close": b.close, "volume": b.volume,
    } for b in bars])


def _render_grid_view(
    sorted_syms: List[str],
    df_view: pd.DataFrame,
    db,
    *,
    n_days: Optional[int],
    cols_per_row: int,
    candle_width: int,
) -> None:
    range_label = "全部" if n_days is None else f"近 {n_days} 日"
    st.markdown(f"### K 線縮圖 ({range_label})")
    cols = st.columns(cols_per_row)
    for i, sym in enumerate(sorted_syms):
        bars = db.get_price_history(sym, limit=n_days, ascending=True)
        with cols[i % cols_per_row]:
            row = df_view.loc[df_view["symbol"] == sym].iloc[0]
            chg = row.get("chg_1d_%")
            chg_str = f"{chg:+.2f}%" if isinstance(chg, (int, float)) else "—"
            color = "green" if (isinstance(chg, (int, float)) and chg > 0) else (
                "red" if isinstance(chg, (int, float)) and chg < 0 else "gray"
            )
            st.markdown(
                f"**`{sym}`** · 收 {row['close'] if row['close'] is not None else '—'} "
                f"· {_badge(chg_str, color)}",
                unsafe_allow_html=True,
            )
            patt = row.get("K線型態", "")
            if patt:
                st.caption(f"型態：{patt}")
            if bars:
                chart = _spark_chart(bars, height=110, candle_width=candle_width)
                if chart is not None:
                    st.altair_chart(chart, use_container_width=True)
            else:
                st.caption("無 K 線資料 - 點上方『一鍵更新』")


def _render_compare_view(
    sorted_syms: List[str],
    df_view: pd.DataFrame,
    db,
    *,
    window_label: str,
    cols_per_row: int,
    candle_width: int,
) -> None:
    """並排大圖：選 2~4 檔，每檔顯示完整 K 線 + 量。"""
    st.markdown("### 並排大圖比較 (含 MA20/60)")
    multi_pick = st.multiselect(
        "選擇要比較的個股 (建議 2~4 檔)",
        options=sorted_syms,
        default=sorted_syms[: min(3, len(sorted_syms))],
        key="board_compare_pick",
    )
    if not multi_pick:
        st.info("請至少選擇一檔股票。")
        return

    y_min, y_max = _board_y_controls("board_compare")
    cols_per_row = max(1, min(cols_per_row, 2, len(multi_pick)))
    rows = [multi_pick[i:i + cols_per_row] for i in range(0, len(multi_pick), cols_per_row)]
    for row in rows:
        cols = st.columns(len(row))
        for sym, col in zip(row, cols):
            with col:
                bars = db.get_price_history(sym, ascending=True)
                if not bars:
                    st.warning(f"{sym}: 無 K 線資料。")
                    continue
                prow = df_view.loc[df_view["symbol"] == sym]
                patt = prow.iloc[0].get("K線型態", "") if not prow.empty else ""
                if patt:
                    st.caption(f"最新 K 線型態：{patt}")
                df_full = _bars_to_df(bars)
                df_sliced = _slice_df_by_window(df_full, window_label)
                df_ind = _build_df_with_indicators(df_sliced, ma_periods=(5, 20, 60))
                chart = _kline_panel(
                    df_ind, ma_periods=(5, 20, 60),
                    y_min=y_min, y_max=y_max,
                    candle_width=candle_width,
                    height=300, title=f"{sym}",
                )
                vol = _volume_panel(df_ind, height=80)
                if chart is not None:
                    st.altair_chart(chart, use_container_width=True)
                if vol is not None:
                    st.altair_chart(vol, use_container_width=True)


def _render_focus_view(
    sorted_syms: List[str],
    db,
) -> None:
    """單檔專注：把完整 K 線工作台 (時間範圍/Y軸/面板/抓取) 顯示在這一檔上。"""
    st.markdown("### 單檔專注 (完整 K 線工作台)")
    pick = st.selectbox(
        "選一檔深入看", options=sorted_syms, index=0 if sorted_syms else None,
        key="board_focus_pick",
    )
    if not pick:
        return
    bars = db.get_price_history(pick, ascending=True)
    if not bars:
        st.warning(f"{pick} 尚無 K 線資料。請點上方『一鍵更新』或『往更早抓取』。")
        return
    df_full = _bars_to_df(bars)
    _render_kline_workspace(
        pick, df_full,
        key_prefix=f"board_focus_{pick}",
        default_window="近 6 個月",
        default_panels=("kline", "volume", "macd", "rsi"),
        show_fetch_more=True,
    )


def _run_batch_refresh(symbols: List[str], months: int) -> None:
    """『一鍵更新所有 K 線』後端：分檔 → 分月 fetch，含進度條。"""
    from bot.technicals import fetch_kline_range

    progress = st.progress(0, text="準備中...")
    end = dt.date.today()
    start = end.replace(day=1)
    try:
        start = start.replace(year=start.year - months // 12)
        remaining = months % 12
        if remaining:
            new_month = start.month - remaining
            new_year = start.year
            while new_month <= 0:
                new_month += 12
                new_year -= 1
            start = dt.date(new_year, new_month, 1)
    except ValueError:
        start = end - dt.timedelta(days=months * 31)

    ok, fail = 0, 0
    total = len(symbols)
    for i, sym in enumerate(symbols, start=1):
        try:
            fetch_kline_range(
                sym,
                start_date=start, end_date=end,
                root=PROJECT_ROOT, save_to_db=True,
                request_delay_sec=0.4,
            )
            ok += 1
        except Exception as e:
            fail += 1
            st.warning(f"{sym} 抓取失敗：{e}")
        progress.progress(
            i / total,
            text=f"[{i}/{total}] {sym} ({start.isoformat()} ~ {end.isoformat()})",
        )
    progress.empty()
    st.success(f"完成：成功 {ok} 檔 / 失敗 {fail} 檔，已寫入 price_history")


def _run_batch_extend(symbols: List[str], years: int, delay: float) -> None:
    """批量對所有股票呼叫 extend_kline_backward。"""
    from bot.technicals import extend_kline_backward

    outer = st.progress(0, text=f"批量抓取 {len(symbols)} 檔的 {years} 年歷史...")
    inner = st.empty()
    total = len(symbols)
    ok, fail = 0, 0
    for i, sym in enumerate(symbols, start=1):
        sym_counter = {"rows": 0, "months": 0}

        def cb(idx: int, total_months: int, label: str, fetched: int) -> None:
            sym_counter["rows"] += fetched
            sym_counter["months"] += 1
            inner.progress(
                idx / max(total_months, 1),
                text=(
                    f"[{i}/{total}] {sym} · 月 {idx}/{total_months} ({label}) "
                    f"· 累計新增 {sym_counter['rows']} 根"
                ),
            )

        try:
            extend_kline_backward(
                sym, years_back=years, root=PROJECT_ROOT,
                request_delay_sec=delay, on_progress=cb,
            )
            ok += 1
        except Exception as e:
            fail += 1
            st.warning(f"{sym} 往前抓失敗：{e}")
        outer.progress(i / total, text=f"完成 {sym} ({i}/{total})")
    inner.empty()
    outer.empty()
    st.success(f"批量完成：成功 {ok} 檔 / 失敗 {fail} 檔")


def page_board() -> None:
    """K 線看板：縮圖 grid / 並排大圖 / 單檔專注三種模式 + 長時間範圍 + 批次往前抓。"""
    st.title("📊 K 線看板 (Stock Board)")
    st.caption(
        "三種看板模式：縮圖 grid (多檔一覽) · 並排大圖 (2~3 檔比較) · 單檔專注 (完整 K 線工作台)。"
        "資料來源為本地 SQLite `price_history`，"
        "支援批次往更早 (5/10 年) 抓取，並可推送到 Google Sheets。"
    )

    db = _open_db_for_page()

    # ---- 來源選擇 ----
    src_col1, src_col2 = st.columns([3, 2])
    source = src_col1.radio(
        "監控池來源",
        ["Watchlist", "DB 已有資料的全部 symbol", "自訂"],
        horizontal=True, key="board_source",
    )
    view_mode = src_col2.radio(
        "顯示模式",
        ["縮圖 grid", "並排大圖", "單檔專注"],
        horizontal=True, key="board_view_mode",
    )

    if source == "Watchlist":
        wlist = wl.load(PROJECT_ROOT)
        symbols = [i.ticker for i in wlist.items]
    elif source == "DB 已有資料的全部 symbol":
        symbols = db.list_price_symbols()
    else:
        custom = src_col1.text_input(
            "自訂代號 (逗號分隔)", value="2330,2454,2317,0050",
            key="board_custom",
        )
        symbols = [s.strip() for s in custom.split(",") if s.strip()]

    if not symbols:
        st.info("尚未選定股票。可到「個股總覽」加入 watchlist，或選『自訂』直接輸入代號。")
        return

    # ---- 時間範圍 + 抓取選項 ----
    pc1, pc2, pc3, pc4, pc5 = st.columns([1.2, 1.0, 1.0, 1.2, 1.4])
    window_label = pc1.selectbox(
        "顯示時間範圍",
        [w[0] for w in KLINE_WINDOWS if w[0] != "自訂日期"],
        index=2, key="board_window",
    )
    window_days_map = {w[0]: w[1] for w in KLINE_WINDOWS}
    window_days = window_days_map.get(window_label, 130)
    n_days = window_days if isinstance(window_days, int) and window_days > 0 else None

    cols_per_row = pc2.selectbox(
        "每列張數", [1, 2, 3, 4, 6], index=1, key="board_cols",
    )
    candle_width = pc3.slider(
        "K 棒寬度",
        min_value=1,
        max_value=14,
        value=_auto_candle_width(n_days if n_days is not None else 2500),
        step=1,
        key=f"board_candle_width_{window_label}",
        help="預設會依顯示時間範圍調整；長週期可調細，短週期可調寬。",
    )
    refresh_months = pc4.selectbox(
        "一鍵更新範圍",
        [3, 6, 12, 24, 36, 60, 120],
        index=1,
        format_func=lambda m: f"近 {m} 個月" if m < 12 else f"近 {m // 12} 年",
        key="board_refresh_months",
    )
    refresh_clicked = pc5.button(
        "🔄 一鍵更新所有 K 線",
        type="primary",
        use_container_width=True,
        help=f"對清單中每檔抓近 {refresh_months} 個月 K 線，分月寫入 SQLite。",
    )

    # ---- 一鍵更新 (改用 fetch_kline_range，分月 + 進度) ----
    if refresh_clicked:
        _run_batch_refresh(symbols, refresh_months)
        st.rerun()

    # ---- 批量往前抓更早 ----
    with st.expander("⏪ 批量往更早抓取所有股票 (5 / 10 年歷史)", expanded=False):
        eb1, eb2, eb3 = st.columns([1.5, 1.5, 2])
        ext_years = eb1.selectbox(
            "往前再抓",
            [1, 2, 3, 5, 10],
            index=2,
            format_func=lambda y: f"{y} 年",
            key="board_ext_years",
        )
        ext_delay = eb2.slider(
            "每月請求間隔 (秒)", 0.2, 2.0, 0.5, 0.1,
            key="board_ext_delay",
            help="抓 10 年 = 約 120 個月。建議 0.5+ 秒避免 TWSE rate limit。",
        )
        ext_clicked = eb3.button(
            f"⏪ 對 {len(symbols)} 檔開始批量抓取 {ext_years} 年",
            use_container_width=True,
            key="board_ext_btn",
        )
        if ext_clicked:
            _run_batch_extend(symbols, ext_years, ext_delay)

    # ---- 摘要表 ----
    summary = _board_rows_summary(symbols, db)
    if summary.empty:
        st.warning("symbols 為空。")
        return

    sort_col1, sort_col2, sort_col3 = st.columns([2, 2, 2])
    sort_by = sort_col1.selectbox(
        "排序依據",
        ["chg_1d_%", "chg_5d_%", "chg_20d_%", "vol_ratio", "close", "symbol"],
        index=0, key="board_sort",
    )
    ascending = sort_col2.toggle("升冪", value=False, key="board_asc")
    only_up = sort_col3.toggle("只顯示上漲", value=False, key="board_up")

    df_view = summary.copy()
    if only_up and "chg_1d_%" in df_view.columns:
        df_view = df_view[df_view["chg_1d_%"].fillna(0) > 0]
    df_view = df_view.sort_values(
        sort_by, ascending=ascending, na_position="last",
    ).reset_index(drop=True)

    st.markdown("### 摘要表")
    st.dataframe(
        df_view,
        hide_index=True,
        use_container_width=True,
        column_config={
            "chg_1d_%": st.column_config.NumberColumn("1 日 %", format="%+.2f"),
            "chg_5d_%": st.column_config.NumberColumn("5 日 %", format="%+.2f"),
            "chg_20d_%": st.column_config.NumberColumn("20 日 %", format="%+.2f"),
            "vol_ratio": st.column_config.NumberColumn("量比", format="%.2f"),
            "close": st.column_config.NumberColumn("收盤", format="%.2f"),
            "volume": st.column_config.NumberColumn("成交張", format="%.0f"),
            "high_60d": st.column_config.NumberColumn("近 60 高", format="%.2f"),
            "low_60d": st.column_config.NumberColumn("近 60 低", format="%.2f"),
        },
    )

    csv_buf = df_view.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "下載摘要 CSV",
        data=csv_buf,
        file_name=f"board_{dt.date.today().isoformat()}.csv",
        mime="text/csv",
    )

    sorted_syms = df_view["symbol"].tolist()

    # ---- 三種顯示模式 ----
    if view_mode == "縮圖 grid":
        _render_grid_view(
            sorted_syms, df_view, db,
            n_days=n_days,
            cols_per_row=cols_per_row,
            candle_width=candle_width,
        )
    elif view_mode == "並排大圖":
        _render_compare_view(
            sorted_syms, df_view, db,
            window_label=window_label,
            cols_per_row=cols_per_row,
            candle_width=candle_width,
        )
    else:  # 單檔專注
        _render_focus_view(sorted_syms, db)

    # ---- Google Sheets 同步快捷 ----
    st.markdown("---")
    st.markdown("### 把歷史 K 線同步到 Google Sheets")
    st.caption(
        "把 `price_history` 整張表推到雲端 worksheet (覆蓋整張)，"
        "另一台電腦或手機可 Pull 回本地。完整設定請見「資料庫 / 雲端同步」頁。"
    )
    cfg = load_config_from_env()
    sc1, sc2, sc3 = st.columns(3)
    sc1.markdown(
        f"雲端狀態: {_badge('啟用', 'green') if cfg.enabled else _badge('未設定', 'gray')}",
        unsafe_allow_html=True,
    )
    if cfg.enabled:
        if sc2.button("⬆ Push price_history 到 Sheets",
                      use_container_width=True, key="board_push"):
            try:
                sync = GoogleSheetSync(cfg, db=db)
                with st.spinner("Pushing price_history..."):
                    r = sync.push("price_history")
                st.markdown(_format_sync_result(r))
            except Exception as e:
                st.error(f"Push 失敗: {e}")
        if sc3.button("⬇ Pull price_history (覆蓋本地)",
                      use_container_width=True, key="board_pull"):
            try:
                sync = GoogleSheetSync(cfg, db=db)
                with st.spinner("Pulling price_history..."):
                    r = sync.pull("price_history")
                st.markdown(_format_sync_result(r))
                st.rerun()
            except Exception as e:
                st.error(f"Pull 失敗: {e}")
    else:
        st.info("尚未設定 Google Sheets 雲端同步。請到「資料庫 / 雲端同步」頁完成設定。")


# ======================================================================
# 頁面: 資料庫 / 雲端同步
# ======================================================================


def _resolved_db_path() -> Path:
    env = load_env()
    p = env.get("STOCK_DB_PATH", "").strip()
    if p:
        return Path(p).expanduser()
    return default_db_path(PROJECT_ROOT)


def _open_db_for_page() -> StockDB:
    """開啟由 .env 指定的 DB；session 內快取一個 handle。"""
    target = _resolved_db_path()
    cached = st.session_state.get("_db_handle")
    if cached is None or cached.path != target:
        reset_db_singleton()
        cached = StockDB.open(path=target)
        st.session_state["_db_handle"] = cached
    return cached


def _format_sync_result(r: "TableSyncResult") -> str:
    if r.error:
        return f"❌ `{r.table}`: {r.error}"
    if r.skipped:
        return f"⚠️ `{r.table}`: 跳過"
    return f"✅ `{r.table}`: {r.direction} {r.rows} 筆 {('· ' + r.note) if r.note else ''}"


def page_database() -> None:
    st.title("資料庫 / 雲端同步")
    st.caption(
        "本地 SQLite 集中管理冷/溫資料；可選擇透過 Google Sheets 在多台電腦間共用。"
    )

    db = _open_db_for_page()

    # ---- DB 狀態 ----
    st.markdown("### 本地資料庫狀態")
    c1, c2, c3 = st.columns([2, 1, 1])
    c1.code(str(db.path), language="text")
    try:
        size = db.path.stat().st_size if db.path.exists() else 0
        c2.metric("檔案大小", f"{size/1024:,.1f} KB")
    except Exception:
        c2.metric("檔案大小", "—")
    c3.metric("Tables", len(ALL_TABLES))

    path_lower = str(db.path).lower().replace("\\", "/")
    drive_hint = "/my drive/" in path_lower or "googledrive" in path_lower
    if drive_hint:
        st.info(
            "📁 DB 看起來放在 Google Drive 同步資料夾。**單機共用沒問題，但兩台電腦不要同時開啟，**"
            "避免 SQLite WAL 衝突。多機並行請改用下方「Google Sheets 雲端同步」。"
        )

    counts = db.table_counts()
    summary_rows = []
    for t in ALL_TABLES:
        meta = db.get_sync_meta(t) if t in SYNCABLE_TABLES else None
        summary_rows.append({
            "table": t,
            "rows": counts.get(t, 0),
            "last_push": (meta.last_push_at if meta else "—") or "—",
            "last_pull": (meta.last_pull_at if meta else "—") or "—",
            "last_error": (meta.last_error if meta else "") or "",
        })
    st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)

    st.markdown("---")

    # ---- 雲端同步 ----
    st.markdown("### Google Sheets 雲端同步")
    cfg = load_config_from_env()
    cc1, cc2, cc3 = st.columns([2, 2, 1])
    cc1.markdown(
        f"Sheet ID: {_badge('已設定', 'green') if cfg.sheet_id else _badge('未設定', 'gray')}",
        unsafe_allow_html=True,
    )
    cc2.markdown(
        f"Service Account: {_badge('已設定', 'green') if cfg.service_account_json else _badge('未設定', 'gray')}",
        unsafe_allow_html=True,
    )
    cc3.markdown(
        f"狀態: {_badge('啟用', 'green') if cfg.enabled else _badge('停用', 'red')}",
        unsafe_allow_html=True,
    )

    with st.expander("🛠 雲端同步設定步驟 (第一次使用必看)", expanded=not cfg.enabled):
        st.markdown(
            """
            1. 到 [Google Cloud Console](https://console.cloud.google.com) 建立 / 選一個 Project
            2. 啟用 **Google Sheets API** 與 **Google Drive API**
            3. 建立 **Service Account**，下載金鑰 JSON 檔
            4. 開一張 Google Sheet (空白即可)，把 SA 的 email 加為「**編輯者**」
            5. 在「組態設定」頁填入：
               - `GOOGLE_SHEET_ID` = Sheet URL 中 `/d/` 後的字串
               - `GOOGLE_SA_JSON_PATH` = 剛下載的 JSON 檔絕對路徑
            6. 安裝雲端套件：`uv sync --extra cloud`
            7. (選用) 在 Sheet 上掛 **Google Form**：手機就能新增股票 → Forms 寫進 Sheet → 用「Pull」拉回本地

            完整文件: `docs/cloud_sync_setup.md`
            """
        )

    if not cfg.enabled:
        st.warning("雲端同步未啟用 — 上方欄位填齊後才能 push/pull。")
    else:
        cs1, cs2, cs3, cs4 = st.columns(4)
        if cs1.button("🔌 測試連線", use_container_width=True):
            try:
                sync = GoogleSheetSync(cfg, db=db)
                title = sync.ping()
                st.success(f"連線成功 — Sheet 標題：「{title}」")
            except CloudSyncDependencyError as e:
                st.error(str(e))
            except Exception as e:
                st.error(f"連線失敗: {e}")

        if cs2.button("⬇ Pull 全部", help="把雲端覆蓋到本地", use_container_width=True):
            try:
                sync = GoogleSheetSync(cfg, db=db)
                with st.spinner("Pulling from Google Sheets..."):
                    results = sync.pull_all()
                for r in results:
                    st.markdown(_format_sync_result(r))
            except Exception as e:
                st.error(f"Pull 失敗: {e}")

        if cs3.button("⬆ Push 全部", help="把本地覆蓋到雲端",
                      type="primary", use_container_width=True):
            try:
                sync = GoogleSheetSync(cfg, db=db)
                with st.spinner("Pushing to Google Sheets..."):
                    results = sync.push_all()
                for r in results:
                    st.markdown(_format_sync_result(r))
            except Exception as e:
                st.error(f"Push 失敗: {e}")

        if cs4.button("🔄 智能同步", help="依 updated_at 自動判斷方向",
                      use_container_width=True):
            try:
                sync = GoogleSheetSync(cfg, db=db)
                with st.spinner("Syncing..."):
                    results = sync.sync_all()
                for r in results:
                    st.markdown(_format_sync_result(r))
            except Exception as e:
                st.error(f"Sync 失敗: {e}")

        st.markdown("**單表同步** (細部控制)")
        tcol1, tcol2, tcol3, tcol4 = st.columns([2, 1, 1, 1])
        picked = tcol1.selectbox("選擇 table", list(SYNCABLE_TABLES))
        if tcol2.button("⬇ Pull", key="single_pull", use_container_width=True):
            sync = GoogleSheetSync(cfg, db=db)
            r = sync.pull(picked)
            st.markdown(_format_sync_result(r))
        if tcol3.button("⬆ Push", key="single_push", use_container_width=True):
            sync = GoogleSheetSync(cfg, db=db)
            r = sync.push(picked)
            st.markdown(_format_sync_result(r))
        if tcol4.button("🔄 Sync", key="single_sync", use_container_width=True):
            sync = GoogleSheetSync(cfg, db=db)
            r = sync.sync(picked)
            st.markdown(_format_sync_result(r))

    st.markdown("---")

    # ---- 瀏覽 / 編輯資料 ----
    st.markdown("### 瀏覽 / 編輯資料")

    tabs = st.tabs([
        "stock_info", "watchlist", "etf_meta",
        "monthly_revenue", "quarterly_report", "price_history", "匯入 CSV",
    ])

    # --- stock_info ---
    with tabs[0]:
        st.caption("冷資料 — 公司基本面 (公司名 / 產業 / 上市日 / 股本)")
        infos = db.list_stock_info()
        if infos:
            df = pd.DataFrame([dataclasses_asdict(i) for i in infos])
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("尚無資料。可至「個股深入分析」抓取，或於「匯入 CSV」上傳。")

        with st.expander("➕ 手動新增 / 編輯一筆"):
            wc = st.columns(3)
            sym = wc[0].text_input("symbol", key="si_sym")
            name = wc[1].text_input("name", key="si_name")
            ind = wc[2].text_input("industry", key="si_ind")
            wc2 = st.columns(3)
            mkt = wc2[0].selectbox("market", ["TWSE", "TPEX", "EMG"], key="si_mkt")
            ld = wc2[1].text_input("listed_date (YYYY-MM-DD)", key="si_ld")
            cap = wc2[2].text_input("capital (億)", key="si_cap")
            note = st.text_input("note", key="si_note")
            if st.button("儲存", key="si_save"):
                if not sym.strip():
                    st.error("symbol 必填")
                else:
                    try:
                        cap_v = float(cap) if cap.strip() else 0.0
                    except ValueError:
                        cap_v = 0.0
                    db.upsert_stock_info(StockInfo(
                        symbol=sym.strip(), name=name.strip(), industry=ind.strip(),
                        market=mkt, listed_date=ld.strip(), capital=cap_v,
                        note=note.strip(),
                    ))
                    st.success(f"{sym} 已寫入 DB")
                    st.rerun()

    # --- watchlist ---
    with tabs[1]:
        st.caption("使用者監控清單 (與雲端同步可在多機共用)")
        watch_rows = db.list_watchlist()
        if watch_rows:
            df = pd.DataFrame([dataclasses_asdict(w) for w in watch_rows])
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("Watchlist 是空的。")

        with st.expander("➕ 新增 / 編輯"):
            wcol = st.columns(3)
            wsym = wcol[0].text_input("symbol", key="wl_sym")
            wname = wcol[1].text_input("name", key="wl_name")
            wtags = wcol[2].text_input("tags (逗號分隔)", key="wl_tags")
            wnote = st.text_input("note", key="wl_note")
            ba1, ba2 = st.columns(2)
            if ba1.button("儲存", key="wl_save"):
                if not wsym.strip():
                    st.error("symbol 必填")
                else:
                    db.upsert_watch(WatchlistRow(
                        symbol=wsym.strip(), name=wname.strip(),
                        tags=wtags.strip(), note=wnote.strip(),
                    ))
                    st.success(f"{wsym} 已加入 watchlist")
                    st.rerun()
            del_sym = ba2.text_input("欲刪除 symbol", key="wl_del")
            if st.button("刪除", key="wl_del_btn") and del_sym.strip():
                db.remove_watch(del_sym.strip())
                st.success(f"{del_sym} 已刪除")
                st.rerun()

    # --- etf_meta ---
    with tabs[2]:
        st.caption("主動式 ETF 基本資料 (與 active_etfs.json 互通)")
        metas = db.list_etf_meta()
        if metas:
            df = pd.DataFrame([dataclasses_asdict(m) for m in metas])
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("尚無資料。")

    # --- monthly_revenue ---
    with tabs[3]:
        st.caption("每月營收 (溫資料 — 每月 10 號後更新)")
        sym_q = st.text_input("查詢 symbol", key="mr_q", placeholder="留空則列出全部")
        if sym_q.strip():
            mrs = db.get_monthly_revenue(sym_q.strip())
            rows_view = [dataclasses_asdict(m) for m in mrs]
        else:
            cur = db.conn.execute(
                "SELECT * FROM monthly_revenue ORDER BY year_month DESC, symbol LIMIT 500"
            )
            rows_view = [dict(r) for r in cur.fetchall()]
        if rows_view:
            st.dataframe(pd.DataFrame(rows_view),
                         use_container_width=True, hide_index=True)
        else:
            st.info("查無資料。")

    # --- quarterly_report ---
    with tabs[4]:
        st.caption("季報 EPS / 三率 (溫資料)")
        cur = db.conn.execute(
            "SELECT * FROM quarterly_report ORDER BY period DESC, symbol LIMIT 500"
        )
        qs = [dict(r) for r in cur.fetchall()]
        if qs:
            st.dataframe(pd.DataFrame(qs), use_container_width=True, hide_index=True)
        else:
            st.info("尚無資料。")

    # --- price_history ---
    with tabs[5]:
        st.caption(
            "歷史 K 線 OHLCV (溫資料)。由「個股深入分析」抓 K 線或「K 線看板 → 一鍵更新」自動寫入。"
        )
        summary_ph = db.price_history_summary()
        if summary_ph:
            st.markdown("##### 各 symbol K 線筆數 / 最新日期")
            st.dataframe(
                pd.DataFrame(summary_ph),
                hide_index=True, use_container_width=True,
            )
            phc1, phc2 = st.columns([2, 3])
            ph_sym = phc1.text_input(
                "查詢 symbol 的歷史 K 線", value="", placeholder="2330",
                key="ph_q",
            )
            ph_n = phc2.slider("顯示最近 N 根", 30, 500, 120, step=10, key="ph_n")
            if ph_sym.strip():
                bars = db.get_price_history(
                    ph_sym.strip(), limit=int(ph_n), ascending=False,
                )
                if bars:
                    st.dataframe(
                        pd.DataFrame([dataclasses_asdict(b) for b in bars]),
                        hide_index=True, use_container_width=True,
                    )
                else:
                    st.info(f"DB 中查無 {ph_sym} 的 K 線；可到「K 線看板」一鍵更新。")
        else:
            st.info(
                "尚無 K 線資料。請到「📊 K 線看板」或「個股深入分析」抓取，"
                "或從 CSV / Google Sheets 匯入。"
            )

    # --- 匯入 CSV ---
    with tabs[6]:
        st.caption("把 CSV 內容寫進指定 table (欄位名稱要對齊 schema)")
        target = st.selectbox("目標 table", list(SYNCABLE_TABLES), key="imp_target")
        mode = st.radio(
            "寫入模式",
            ["upsert (合併)", "replace (覆蓋整張表)"],
            horizontal=True, key="imp_mode",
        )
        f = st.file_uploader("選 CSV", type=["csv"], key="imp_csv")
        if f is not None:
            try:
                df = pd.read_csv(f, encoding="utf-8-sig", dtype=str).fillna("")
            except Exception:
                f.seek(0)
                df = pd.read_csv(f, encoding="utf-8", dtype=str).fillna("")
            st.write("**預覽 (前 10 筆)**")
            st.dataframe(df.head(10), use_container_width=True)
            if st.button("執行匯入", type="primary", key="imp_run"):
                from bot.cloud_sync import _coerce_row
                cols = db._table_columns(target)
                rows_csv = df.to_dict(orient="records")
                typed = [_coerce_row(target, cols, r) for r in rows_csv]
                if mode.startswith("replace"):
                    n = db.replace_table_rows(target, typed)
                else:
                    n = db.upsert_rows(target, typed)
                st.success(f"已寫入 {n} 筆到 {target}")
                st.rerun()


# ======================================================================
# 主程式
# ======================================================================


# ======================================================================
# 頁面: 今日當沖戰情室
# ======================================================================


def page_intraday() -> None:
    st.title("⚡ 今日當沖戰情室")
    st.caption(
        "盤前 8:30 流程: 新聞 (鉅亨網) → LLM 萃取題材 → 合併候選股 → 算當沖分 → LLM 戰情簡報。"
        " 一鍵跑完整流程: `uv run stock-intraday`。"
    )
    _llm_caption(
        "「🤖 重抓新聞 + 重跑」「🤖 用快取重跑」**至少各呼叫 2 次 Gemini** "
        "(theme_radar 萃取題材 + intraday_brief 寫戰情簡報)。"
        "切換/載入日期只讀 DB 或舊檔，不會消耗 LLM。"
    )

    from bot.intraday_pipeline import load_intraday_by_date, run_intraday
    from bot.utils import now_tw

    cs = st.columns([2, 1, 1, 1])
    today = now_tw().date()
    view_date = cs[0].date_input(
        "查看日期",
        value=today,
        key="intra_view_date",
        help="預設今天；切換日期只會讀取已保存的 DB/JSON 紀錄，不會呼叫 LLM。",
    )
    if isinstance(view_date, dt.datetime):
        view_date = view_date.date()
    date_iso = view_date.isoformat()
    report_key = f"intra:{date_iso}"
    is_today = view_date == today

    if cs[1].button(
        "載入此日期 (不呼叫 LLM)",
        use_container_width=True,
        key="intra_load",
        help="只讀取選定日期的 intraday report，不會呼叫 Gemini。",
    ):
        st.session_state["intra_report"] = load_intraday_by_date(PROJECT_ROOT, view_date)
        st.session_state["intra_report_key"] = report_key
    if cs[2].button(
        "🤖 重抓新聞 + 重跑",
        type="primary",
        use_container_width=True,
        key="intra_refresh",
        disabled=not is_today,
        help=(
            (
                "重抓鉅亨網新聞，跑 theme_radar + intraday_brief 兩次 LLM 呼叫。"
                if is_today else "只能重跑今天；看舊紀錄請用日期載入。"
            )
            + "\n\n" + LLM_HINT_DIRECT
        ),
    ):
        env_values = load_env()
        from bot.config import Settings as S
        # 確保 GEMINI_API_KEY 從 .env 載到 process env
        import os
        for k, v in env_values.items():
            if v and not os.environ.get(k):
                os.environ[k] = v
        s = S()
        with st.spinner("LLM 跑當沖管線中 (預估 30-60 秒)..."):
            report = run_intraday(
                project_root=PROJECT_ROOT,
                settings=s,
                force_refresh_news=True,
            )
        from bot.intraday_pipeline import _report_to_json
        st.session_state["intra_report"] = _report_to_json(report)
        st.session_state["intra_report_key"] = report_key
    if cs[3].button(
        "🤖 用快取重跑",
        use_container_width=True,
        key="intra_cached_run",
        disabled=not is_today,
        help=(
            (
                "新聞用快取 (省抓取時間)，但仍會跑 theme_radar + intraday_brief 兩次 LLM。"
                if is_today else "只能重跑今天；看舊紀錄請用日期載入。"
            )
            + "\n\n" + LLM_HINT_DIRECT
        ),
    ):
        env_values = load_env()
        from bot.config import Settings as S
        import os
        for k, v in env_values.items():
            if v and not os.environ.get(k):
                os.environ[k] = v
        s = S()
        with st.spinner("跑當沖管線..."):
            report = run_intraday(project_root=PROJECT_ROOT, settings=s)
        from bot.intraday_pipeline import _report_to_json
        st.session_state["intra_report"] = _report_to_json(report)
        st.session_state["intra_report_key"] = report_key

    if (
        st.session_state.get("intra_report_key") != report_key
        or "intra_report" not in st.session_state
        or st.session_state.get("intra_report") is None
    ):
        st.session_state["intra_report"] = load_intraday_by_date(PROJECT_ROOT, view_date)
        st.session_state["intra_report_key"] = report_key

    data = st.session_state.get("intra_report")
    if not data:
        st.warning(f"尚無 {date_iso} 的當沖報告。請切換日期，或今天執行 `uv run stock-intraday` 產生新紀錄。")
        return

    # ---- 頂部 KPI ----
    db_ts = data.get("_db_generated_at") or data.get("_db_updated_at") or ""
    cs[0].caption(
        f"asof: {data.get('asof', '-')} ｜ "
        f"市場氛圍: **{data.get('market_tone', '-').upper()}** ｜ "
        f"題材 {len(data.get('themes', []))} 個 ｜ 候選 {len(data.get('rankings', []))} 檔"
        + (f" ｜ DB {db_ts}" if db_ts else "")
    )
    if data.get("errors"):
        st.error("管線錯誤: " + " / ".join(data["errors"][:3]))

    overall = data.get("overall_brief", "")
    if overall:
        st.info(overall)

    # ---- LLM 戰情簡報 ----
    if data.get("brief_md"):
        with st.expander("🤖 LLM 戰情簡報", expanded=True):
            st.markdown(data["brief_md"])
            st.caption(
                f"prompt: {data.get('brief_prompt_id','')} v{data.get('brief_prompt_version','')}"
            )

    # ---- 今日熱門題材 ----
    st.markdown("### 🔥 今日熱門題材")
    themes = data.get("themes") or []
    if not themes:
        st.info("尚未萃取題材")
    else:
        cols = st.columns(min(3, len(themes)))
        for i, t in enumerate(themes):
            col = cols[i % len(cols)]
            heat = int(t.get("heat", 0))
            with col:
                st.markdown(
                    f"#### {t.get('theme', '')}  "
                    f"{'🔥' * heat}{'·' * (5-heat)}"
                )
                st.caption(
                    f"{t.get('category','')} ｜ {t.get('horizon','')} ｜ 熱度 {heat}/5"
                )
                for d in (t.get("drivers") or [])[:2]:
                    st.markdown(f"- {d}")
                cands = t.get("candidate_tickers") or []
                if cands:
                    chips = " ".join([
                        f"`{c.get('ticker','')}` {c.get('name','')[:6]}"
                        for c in cands[:6]
                    ])
                    st.markdown(f"**標的:** {chips}")
                for r in (t.get("risks") or [])[:1]:
                    st.caption(f"⚠ {r}")

    # ---- 候選股排序 ----
    st.markdown("### 📋 當沖候選排行 (按 day_trade 分)")
    rankings = data.get("rankings") or []
    if rankings:
        rows = []
        for r in rankings:
            prem = r.get("adr_premium_pct")
            rows.append({
                "代號": r["ticker"],
                "名稱": r.get("name", ""),
                "題材": r.get("theme", ""),
                "當沖分": r.get("day_trade_score", 0),
                "技術分": r.get("technical_score", 50),
                "今日%": r.get("today_pct_change", 0),
                "量比": r.get("volume_ratio", 0),
                "動作": r.get("action", "HOLD"),
                "美股連動": r.get("us_market_score", 50),
                "ADR 溢價%": prem if prem is not None else float("nan"),
                "籌碼": r.get("chip_summary_text", ""),
                "來源": ",".join(r.get("sources") or []),
            })
        df = pd.DataFrame(rows)
        st.dataframe(
            df, hide_index=True, use_container_width=True,
            column_config={
                "當沖分": st.column_config.ProgressColumn(
                    "當沖分", min_value=0, max_value=100, format="%.1f",
                ),
                "技術分": st.column_config.ProgressColumn(
                    "技術分", min_value=0, max_value=100, format="%.0f",
                ),
                "今日%": st.column_config.NumberColumn(format="%+.2f%%"),
                "量比": st.column_config.NumberColumn(format="%.2f"),
                "美股連動": st.column_config.ProgressColumn(
                    "美股連動", min_value=0, max_value=100, format="%.0f",
                ),
                "ADR 溢價%": st.column_config.NumberColumn(format="%+.2f%%"),
            },
        )

        # 快速跳轉到深入分析
        st.markdown("##### 跳轉到深入分析")
        pick = st.selectbox(
            "選一檔看 360 度視角",
            ["(none)"] + [f"{r['ticker']} {r.get('name','')}" for r in rankings[:10]],
            key="intra_pick",
        )
        if pick != "(none)":
            tk = pick.split()[0]
            if st.button(f"分析 {pick}", type="primary"):
                st.session_state["detail_ticker"] = tk
                st.session_state.pop("detail_cache", None)
                st.session_state.pop("detail_cache_key", None)
                st.session_state["page"] = "個股深入分析"
                st.rerun()


def page_intraday_live() -> None:
    st.title("⚡ 今日當沖即時追蹤")
    st.caption(
        "自動刷新今天 LLM 點名過的股票，只更新公開資料/本地狀態；"
        "LLM 即時推演與檢討必須手動按鈕觸發。"
    )
    _llm_caption(
        "下方追蹤表格自動刷新不會呼叫 LLM。只有按「手動刷新 LLM 即時推演 / 檢討」時，"
        "才會把早盤分析、刷新後狀態、新聞與宏觀資料送 Gemini。"
    )

    from bot.config import Settings as S
    from bot.intraday_live import (
        build_live_tracking_rows,
        load_live_review,
        save_live_review,
    )
    from bot.intraday_pipeline import load_intraday_by_date
    from bot.utils import now_tw

    today = now_tw().date()
    report = load_intraday_by_date(PROJECT_ROOT, today)
    if not report:
        st.warning("今天尚無當沖戰情室報告。請先到「今日當沖戰情室」執行一次盤前/重跑流程。")
        return

    controls = st.columns([1.2, 1, 0.9, 1.2, 1])
    max_tickers = int(controls[0].number_input(
        "追蹤檔數",
        min_value=5,
        max_value=30,
        value=12,
        step=1,
        help="從 LLM 排名與題材候選中依序取出。",
    ))
    auto_refresh = controls[1].toggle(
        "自動刷新",
        value=True,
        key="intraday_live_auto_refresh",
        help="只刷新表格資料，不會自動呼叫 LLM。",
    )
    refresh_seconds = int(controls[2].number_input(
        "秒數",
        min_value=10,
        max_value=180,
        value=30,
        step=10,
    ))
    refresh_technicals = controls[3].toggle(
        "慢速技術重算",
        value=False,
        key="intraday_live_refresh_tech",
        help="開啟後才會重新抓公開 K 線/技術資料；自動刷新預設只抓即時報價。",
    )
    refresh_chips = controls[4].toggle(
        "刷新籌碼",
        value=False,
        key="intraday_live_refresh_chips",
        help="較慢；通常手動 LLM 檢討時會自動納入。",
    )

    if st.button("立即刷新追蹤表", use_container_width=True):
        st.session_state["intraday_live_force_refresh"] = time.time()

    with st.spinner("刷新今日 LLM 點名股票狀態..."):
        tracking = build_live_tracking_rows(
            report,
            root=PROJECT_ROOT,
            max_tickers=max_tickers,
            refresh_quotes=True,
            refresh_technicals=refresh_technicals,
            refresh_chips=refresh_chips,
            refresh_news=False,
            include_news=False,
        )

    rows = tracking.get("rows") or []
    col_a, col_b, col_c, col_d = st.columns(4)
    col_a.metric("報告日期", str(report.get("asof") or today.isoformat()))
    col_b.metric("追蹤檔數", f"{len(rows)}")
    col_c.metric("報價來源", "TWSE MIS")
    col_d.metric("表格刷新", str(tracking.get("asof", "-")).split("T")[-1])

    if not rows:
        st.info("今日 LLM 報告沒有可追蹤的股票代號。")
        return

    stale_count = sum(
        1
        for row in rows
        if row.get("quote_date") and row.get("quote_date") != today.isoformat()
    )
    missing_quote_count = sum(1 for row in rows if row.get("quote_price") is None)
    estimated_quote_count = sum(
        1
        for row in rows
        if row.get("quote_price") is not None
        and row.get("quote_price_basis") not in ("", "last_trade")
    )
    if stale_count:
        st.warning(f"{stale_count} 檔報價日期不是今天，已標成「非今日資料」。")
    if missing_quote_count:
        st.info(f"{missing_quote_count} 檔尚未取得 TWSE 即時報價；不會用日 K 快照假裝盤中數值。")
    if estimated_quote_count:
        st.caption(f"{estimated_quote_count} 檔 MIS 未提供最新成交價，已用前揭示價或買賣中間價估算。")

    table_rows = []
    basis_labels = {
        "last_trade": "成交價",
        "previous_trade": "前揭示價",
        "bid_ask_mid": "買賣中間價",
        "best_bid": "買一價",
        "best_ask": "賣一價",
    }
    for row in rows:
        table_rows.append({
            "代號": row.get("ticker", ""),
            "名稱": row.get("name", ""),
            "題材": row.get("theme", ""),
            "即時價": row.get("quote_price"),
            "即時%": row.get("quote_pct_change"),
            "即時量": row.get("quote_volume"),
            "價格基準": basis_labels.get(row.get("quote_price_basis", ""), row.get("quote_price_basis", "")),
            "買一": row.get("quote_best_bid"),
            "賣一": row.get("quote_best_ask"),
            "報價日": row.get("quote_date", ""),
            "報價時間": row.get("quote_time", ""),
            "來源": row.get("quote_source") or row.get("technical_source", ""),
            "初始當沖分": row.get("initial_day_trade_score", 0),
            "初始技術分": row.get("initial_technical_score", 50),
            "技術分": row.get("current_technical_score"),
            "技術變化": row.get("score_delta"),
            "初始今日%": row.get("initial_pct_change", 0),
            "早盤量比": row.get("initial_volume_ratio"),
            "慢速量比": row.get("current_volume_ratio"),
            "技術來源": row.get("technical_source", ""),
            "規則檢討": row.get("correctness", ""),
            "狀態": row.get("status", ""),
            "理由": row.get("reason", ""),
            "籌碼": row.get("chip_text", ""),
            "新聞": " / ".join(row.get("news_titles") or []),
            "最後K日": row.get("current_last_date", ""),
        })
    df = pd.DataFrame(table_rows)
    st.dataframe(
        df,
        hide_index=True,
        use_container_width=True,
        column_config={
            "初始當沖分": st.column_config.ProgressColumn(
                "初始當沖分", min_value=0, max_value=100, format="%.1f",
            ),
            "初始技術分": st.column_config.ProgressColumn(
                "初始技術分", min_value=0, max_value=100, format="%.1f",
            ),
            "技術分": st.column_config.ProgressColumn(
                "技術分", min_value=0, max_value=100, format="%.1f",
            ),
            "初始今日%": st.column_config.NumberColumn(format="%+.2f%%"),
            "即時價": st.column_config.NumberColumn(format="%.2f"),
            "即時%": st.column_config.NumberColumn(format="%+.2f%%"),
            "即時量": st.column_config.NumberColumn(format="%.0f"),
            "買一": st.column_config.NumberColumn(format="%.2f"),
            "賣一": st.column_config.NumberColumn(format="%.2f"),
            "早盤量比": st.column_config.NumberColumn(format="%.2f"),
            "慢速量比": st.column_config.NumberColumn(format="%.2f"),
            "技術變化": st.column_config.NumberColumn(format="%+.1f"),
        },
    )

    st.markdown("### LLM 即時推演 / 檢討")
    latest_review = load_live_review(PROJECT_ROOT, today)
    env_values = load_env()
    api_ready = bool(env_values.get("GEMINI_API_KEY"))
    ran_manual_llm = False

    if not api_ready:
        st.info("尚未設定 GEMINI_API_KEY；可以看自動追蹤表，但無法手動刷新 LLM 推演。")

    if st.button(
        "🤖 手動刷新 LLM 即時推演 / 檢討",
        type="primary",
        disabled=not api_ready,
        help=LLM_HINT_DIRECT,
        use_container_width=True,
    ):
        ran_manual_llm = True
        import os
        for key, value in env_values.items():
            if value and not os.environ.get(key):
                os.environ[key] = value

        from bot.llm_analyzer import GeminiClient, gemini_call
        from bot.market_macro import fetch_macro_snapshot, macro_to_dict
        from bot.news_fetcher import fetch_today_news, news_to_compact_text
        from bot.prompt_registry import get_registry

        settings = S()
        client = GeminiClient(api_key=settings.gemini_api_key, model=settings.gemini_model)
        registry = get_registry(PROJECT_ROOT / "prompts")
        registry.reload()
        with st.spinner("刷新新資料並請 LLM 檢討早盤判斷..."):
            fresh_tracking = build_live_tracking_rows(
                report,
                root=PROJECT_ROOT,
                max_tickers=max_tickers,
                refresh_quotes=True,
                refresh_technicals=True,
                refresh_chips=True,
                refresh_news=True,
                include_news=True,
            )
            macro = fetch_macro_snapshot(
                root=PROJECT_ROOT,
                force_refresh=True,
                use_cache=True,
            )
            news_items = fetch_today_news(
                limit=120,
                use_cache=False,
                force_refresh=True,
                root=PROJECT_ROOT,
            )
            raw, info = gemini_call(
                "intraday_live_review",
                client=client,
                registry=registry,
                metadata={"task": "intraday_live_review", "asof": today.isoformat()},
                asof_time=now_tw().isoformat(timespec="seconds"),
                original_brief=report.get("brief_md") or report.get("overall_brief") or "",
                themes_json=json.dumps(report.get("themes") or [], ensure_ascii=False, indent=2),
                live_rows_json=json.dumps(fresh_tracking.get("rows") or [], ensure_ascii=False, indent=2),
                macro_json=json.dumps(macro_to_dict(macro), ensure_ascii=False, indent=2),
                news_text=news_to_compact_text(news_items, max_chars=5000),
            )
        if raw:
            payload = {
                "generated_at": now_tw().isoformat(timespec="seconds"),
                "prompt_id": info.get("prompt_id", ""),
                "prompt_version": info.get("prompt_version", ""),
                "llm_info": info,
                "tracking": fresh_tracking,
            }
            save_live_review(
                root=PROJECT_ROOT,
                report_date=today,
                markdown=raw,
                payload=payload,
            )
            latest_review = {"markdown": raw, **payload}
            st.success("LLM 即時推演已更新。")
        else:
            st.warning("LLM 未產出內容，請到 LLM 呼叫紀錄查看錯誤。")

    if latest_review and latest_review.get("markdown"):
        st.caption(
            "上次手動檢討: "
            + str(latest_review.get("generated_at") or latest_review.get("path") or "")
        )
        st.markdown(latest_review["markdown"])
    else:
        st.caption("尚未做過手動 LLM 即時推演。")

    if auto_refresh and not ran_manual_llm:
        st.caption(f"自動刷新已開啟：{refresh_seconds} 秒後更新追蹤表。LLM 不會自動呼叫。")
        time.sleep(refresh_seconds)
        st.rerun()


# ======================================================================
# 頁面: 明日當沖關注
# ======================================================================


def page_next_day_watch() -> None:
    st.title("🌙 明日當沖關注")
    st.caption(
        "盤後 14:00-18:00 跑 draft 初版、隔日凌晨 02:00-06:00 跑 update 更新版。"
        "綜合「題材延續 + 強勢承接 + 明日事件」三大來源，產出明日預備清單。"
        " 一鍵跑：`uv run stock-nextday` 或 `uv run stock-nextday --mode update`。"
    )
    _llm_caption(
        "「🤖 跑 draft 初版」與「🤖 跑 update 更新版」**各呼叫 2 次 Gemini** "
        "(next_day_radar 萃題材 + next_day_brief 寫簡報)。"
        "切換日期/版本只讀 DB 或舊檔，不會消耗 LLM。"
    )

    from bot.next_day_watch_pipeline import (
        load_next_day_by_date,
        next_trading_day,
        run_next_day_watch,
    )
    from bot.utils import now_tw

    today = now_tw().date()
    target = next_trading_day(today)

    cs = st.columns([2, 1, 1, 1])
    view_date = cs[0].date_input(
        "查看目標交易日",
        value=target,
        key="nextday_target_date",
        help="預設下一個交易日；切換日期只會讀取該目標日已保存的 DB/JSON 紀錄，不會呼叫 LLM。",
    )
    if isinstance(view_date, dt.datetime):
        view_date = view_date.date()
    date_iso = view_date.isoformat()
    mode_choice = cs[1].selectbox(
        "讀取版本",
        ["自動 (優先 update)", "draft", "update"],
        key="nextday_view_mode",
        help="只影響回放讀取；draft/update 報告會各自保留。",
    )
    mode_for_load = None if mode_choice.startswith("自動") else mode_choice
    prefer_update = mode_choice != "draft"
    report_key = f"nextday-target:{date_iso}:{mode_choice}"
    is_current_target = view_date == target

    if cs[2].button(
        "🤖 跑 draft (盤後初版)",
        type="primary",
        use_container_width=True,
        key="nextday_draft",
        disabled=not is_current_target,
        help=(
            (
                "盤後跑：用今日收盤資料 + 美股盤後 macro。"
                "跑 next_day_radar + next_day_brief 兩次 LLM。"
                if is_current_target else "只能生成目前下一個交易日的明日關注；看舊紀錄請直接切日期/版本。"
            )
            + "\n\n" + LLM_HINT_DIRECT
        ),
    ):
        env_values = load_env()
        from bot.config import Settings as S
        import os
        for k, v in env_values.items():
            if v and not os.environ.get(k):
                os.environ[k] = v
        s = S()
        with st.spinner("跑明日預備清單 (draft) — 預估 60-120 秒..."):
            report = run_next_day_watch(
                mode="draft",
                project_root=PROJECT_ROOT,
                settings=s,
                force_refresh_news=True,
            )
        from bot.next_day_watch_pipeline import _report_to_json
        st.session_state["nextday_report"] = _report_to_json(report)
        st.session_state["nextday_report_key"] = report_key

    if cs[3].button(
        "🤖 跑 update (凌晨更新)",
        use_container_width=True,
        key="nextday_update",
        disabled=not is_current_target,
        help=(
            (
                "凌晨美股收盤後跑：強制重抓 macro (含今夜美股實際走勢)。"
                "跑 next_day_radar + next_day_brief 兩次 LLM。"
                if is_current_target else "只能生成目前下一個交易日的明日關注；看舊紀錄請直接切日期/版本。"
            )
            + "\n\n" + LLM_HINT_DIRECT
        ),
    ):
        env_values = load_env()
        from bot.config import Settings as S
        import os
        for k, v in env_values.items():
            if v and not os.environ.get(k):
                os.environ[k] = v
        s = S()
        with st.spinner("跑明日預備清單 (update) — 重抓 macro，預估 60-120 秒..."):
            report = run_next_day_watch(
                mode="update",
                project_root=PROJECT_ROOT,
                settings=s,
                force_refresh_macro=True,
            )
        from bot.next_day_watch_pipeline import _report_to_json
        st.session_state["nextday_report"] = _report_to_json(report)
        st.session_state["nextday_report_key"] = report_key

    if st.session_state.get("nextday_report_key") != report_key:
        st.session_state["nextday_report"] = load_next_day_by_date(
            PROJECT_ROOT,
            view_date,
            prefer_update=prefer_update,
            mode=mode_for_load,
        )
        st.session_state["nextday_report_key"] = report_key

    data = st.session_state.get("nextday_report")
    if not data:
        st.warning(
            f"尚無目標日 {date_iso} 的明日當沖關注紀錄 ({mode_choice})。"
            "可切換日期/版本，或今天執行 `uv run stock-nextday`。"
        )
        return

    # ---- 頂部 KPI ----
    mode_label = {"draft": "盤後 draft", "update": "凌晨 update"}.get(
        str(data.get("mode", "")), data.get("mode", "?")
    )
    db_ts = data.get("_db_generated_at") or data.get("_db_updated_at") or ""
    st.caption(
        f"目前 {today.isoformat()} ｜ 預設目標 **{target.isoformat()}** ｜ "
        f"產生日: **{data.get('asof', '-')}** ｜ "
        f"目標日: **{data.get('target_date', '-')}** ｜ "
        f"模式: **{mode_label}** ｜ "
        f"市場氛圍: **{str(data.get('market_tone', '-')).upper()}** ｜ "
        f"題材 {len(data.get('carry_themes', []))} ｜ "
        f"事件 {len(data.get('event_focus', []))} ｜ "
        f"候選 {len(data.get('rankings', []))}"
        + (f" ｜ DB {db_ts}" if db_ts else "")
    )
    if data.get("errors"):
        st.error("管線錯誤: " + " / ".join(data["errors"][:3]))

    overall = data.get("overall_brief", "")
    if overall:
        st.info(overall)

    # ---- LLM 戰情簡報 ----
    if data.get("brief_md"):
        with st.expander("🤖 明日預備簡報 (LLM 寫)", expanded=True):
            st.markdown(data["brief_md"])
            st.caption(
                f"prompt: {data.get('brief_prompt_id','')} "
                f"v{data.get('brief_prompt_version','')}"
            )

    # ---- 三大來源 ----
    tab_themes, tab_events, tab_strong = st.tabs([
        "🔥 題材延續", "🗓 明日事件", "💪 強勢承接 (LLM)",
    ])

    with tab_themes:
        themes = data.get("carry_themes") or []
        if not themes:
            st.info("尚未萃取題材")
        else:
            cols = st.columns(min(3, len(themes)))
            for i, t in enumerate(themes):
                col = cols[i % len(cols)]
                heat = int(t.get("heat", 0))
                with col:
                    st.markdown(
                        f"#### {t.get('theme', '')}  "
                        f"{'🔥' * heat}{'·' * (5-heat)}"
                    )
                    st.caption(
                        f"{t.get('category','')} ｜ 熱度 {heat}/5"
                    )
                    for d in (t.get("drivers") or [])[:2]:
                        st.markdown(f"- {d}")
                    cands = t.get("candidate_tickers") or []
                    if cands:
                        chip_lines = []
                        for c in cands[:6]:
                            tk = c.get("ticker", "")
                            nm = (c.get("name") or "")[:6]
                            role = c.get("role", "")
                            chip_lines.append(f"`{tk}` {nm} ({role})")
                        st.markdown("**標的:** " + "  ".join(chip_lines))
                    for r in (t.get("risks") or [])[:1]:
                        st.caption(f"⚠ {r}")

    with tab_events:
        events = data.get("event_focus") or []
        if not events:
            st.info("尚未抓到明日法說/事件")
        else:
            for ev in events:
                st.markdown(
                    f"#### {ev.get('event','')} ｜ {ev.get('event_time','未指定')}"
                )
                st.caption(ev.get("expected_impact", "") or "")
                tickers_rows = []
                for ct in ev.get("tickers", []) or []:
                    tickers_rows.append({
                        "代號": ct.get("ticker", ""),
                        "名稱": ct.get("name", ""),
                        "立場": ct.get("side", ""),
                    })
                if tickers_rows:
                    st.dataframe(
                        pd.DataFrame(tickers_rows),
                        hide_index=True, use_container_width=True,
                    )

    with tab_strong:
        sc_list = data.get("strong_carry_llm") or []
        if not sc_list:
            st.info("LLM 未挑出強勢承接 (可能今日量縮或全市場偏弱)")
        else:
            rows = []
            for sc in sc_list:
                rows.append({
                    "代號": sc.get("ticker", ""),
                    "名稱": sc.get("name", ""),
                    "今日漲跌%": sc.get("today_pct", 0),
                    "量比": sc.get("volume_ratio", 0),
                    "觀察等級": sc.get("watch_level", ""),
                    "原因": sc.get("reason", ""),
                })
            st.dataframe(
                pd.DataFrame(rows), hide_index=True, use_container_width=True,
                column_config={
                    "今日漲跌%": st.column_config.NumberColumn(format="%+.2f%%"),
                    "量比": st.column_config.NumberColumn(format="%.2f"),
                },
            )

    # ---- 排序候選股 ----
    st.markdown("### 📋 明日預備排行 (按 next_day_score)")
    rankings = data.get("rankings") or []
    if rankings:
        rows = []
        for r in rankings:
            prem = r.get("adr_premium_pct")
            rows.append({
                "代號": r["ticker"],
                "名稱": r.get("name", ""),
                "明日分": r.get("next_day_score", 0),
                "強勢分": r.get("strength_score", 0),
                "今日%": r.get("today_pct_change", 0),
                "量比": r.get("volume_ratio", 0),
                "題材": r.get("theme", ""),
                "事件": r.get("event", ""),
                "進場邏輯": r.get("entry_logic", ""),
                "ADR 溢價%": prem if prem is not None else float("nan"),
                "籌碼": r.get("chip_summary_text", ""),
                "來源": ",".join(r.get("sources") or []),
                "備註": r.get("note", "")[:30],
            })
        df = pd.DataFrame(rows)
        st.dataframe(
            df, hide_index=True, use_container_width=True,
            column_config={
                "明日分": st.column_config.ProgressColumn(
                    "明日分", min_value=0, max_value=100, format="%.1f",
                ),
                "強勢分": st.column_config.ProgressColumn(
                    "強勢分", min_value=0, max_value=100, format="%.0f",
                ),
                "今日%": st.column_config.NumberColumn(format="%+.2f%%"),
                "量比": st.column_config.NumberColumn(format="%.2f"),
                "ADR 溢價%": st.column_config.NumberColumn(format="%+.2f%%"),
            },
        )

        st.markdown("##### 跳轉到深入分析")
        pick = st.selectbox(
            "選一檔看 360 度視角",
            ["(none)"] + [f"{r['ticker']} {r.get('name','')}" for r in rankings[:10]],
            key="nextday_pick",
        )
        if pick != "(none)":
            tk = pick.split()[0]
            if st.button(f"分析 {pick}", type="primary", key="nextday_detail_btn"):
                st.session_state["detail_ticker"] = tk
                st.session_state.pop("detail_cache", None)
                st.session_state.pop("detail_cache_key", None)
                st.session_state["page"] = "個股深入分析"
                st.rerun()


# ======================================================================
# 頁面: 美股 / 跨市場
# ======================================================================


def page_macro() -> None:
    st.title("美股 / 跨市場連動")
    st.caption(
        "追蹤美股大盤 (S&P/NASDAQ/SOX/VIX)、加權指數、重要科技股與 ADR 溢價。"
        "免費 API (yfinance)，每日自動快取於 `data/macro/`。"
    )

    from bot.market_macro import (
        fetch_macro_snapshot,
        load_supply_chain,
        macro_to_dict,
        save_supply_chain,
    )

    cs = st.columns([2, 1, 1])
    if cs[1].button("使用快取", use_container_width=True, key="macro_cache"):
        st.session_state["macro_force"] = False
        st.session_state.pop("macro_data", None)
    if cs[2].button("重新抓取", type="primary", use_container_width=True, key="macro_refresh"):
        st.session_state["macro_force"] = True
        st.session_state.pop("macro_data", None)

    if "macro_data" not in st.session_state:
        force_macro = st.session_state.get("macro_force", False)
        status = st.status(
            "載入跨市場資料",
            expanded=True,
        )
        if force_macro:
            status.write("重新抓取：呼叫 yfinance、TWSE 收盤價與 TAIFEX 期貨資料。")
        else:
            status.write("先讀今日 `data/macro/` 快取；若不存在才抓外部資料。")
        try:
            snap = fetch_macro_snapshot(
                root=PROJECT_ROOT,
                force_refresh=force_macro,
            )
            st.session_state["macro_data"] = macro_to_dict(snap)
            status.update(
                label="跨市場資料載入完成",
                state="complete",
                expanded=False,
            )
        except Exception as exc:
            status.update(label="跨市場資料載入失敗", state="error", expanded=True)
            raise exc
    data = st.session_state["macro_data"]

    cs[0].caption(
        f"asof: {data.get('asof_date', '-')} ｜ "
        f"USDTWD={data.get('usdtwd', 0):.3f} ｜ "
        f"{'快取' if data.get('cached') else '即時抓取'}"
    )

    # ---- 指數 ----
    st.markdown("### 主要指數")
    indices = data.get("indices", {})
    ic = st.columns(6)
    for i, sym in enumerate(["^GSPC", "^IXIC", "^DJI", "^SOX", "^VIX", "^TWII"]):
        q = indices.get(sym)
        if q:
            ic[i].metric(
                q.get("name", sym),
                f"{q.get('price', 0):.2f}",
                f"{q.get('pct_change', 0):+.2f}%",
            )

    # ---- 美股科技龍頭 ----
    st.markdown("### 重要美股 / ADR")
    stocks = data.get("stocks", {})
    rows = []
    for sym, info in stocks.items():
        rows.append({
            "代號": sym,
            "名稱": info.get("name", sym),
            "類別": info.get("role", ""),
            "收盤 (USD)": round(info.get("price", 0), 2),
            "漲跌 %": round(info.get("pct_change", 0), 2),
            "母股": info.get("parent_tw_ticker", ""),
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("漲跌 %", ascending=False)
    st.dataframe(
        df, hide_index=True, use_container_width=True,
        column_config={
            "漲跌 %": st.column_config.NumberColumn(format="%.2f%%"),
        },
    )

    # ---- ADR 溢價 ----
    st.markdown("### ADR 溢價 / 折價")
    st.caption("公允台股價 = ADR USD × USDTWD ÷ ADR 比例；溢價>0 表示隔日台股早盤偏多")
    prem = data.get("adr_premiums", [])
    if prem:
        st.dataframe(
            pd.DataFrame(prem), hide_index=True, use_container_width=True,
            column_config={
                "premium_pct": st.column_config.NumberColumn("溢價 %", format="%.2f%%"),
                "fair_tw_price": st.column_config.NumberColumn("公允價", format="%.2f"),
                "tw_price_twd": st.column_config.NumberColumn("實際價", format="%.2f"),
            },
        )
    else:
        st.info("尚未抓到 ADR 溢價 (請點重新抓取)")

    # ---- LLM 跨市場簡報 ----
    st.markdown("---")
    st.markdown("### LLM 跨市場簡報 (美股 → 台股早盤影響)")
    st.caption(
        "🤖 此區塊的「產出簡報」按鈕會呼叫 Gemini (`us_market_brief` prompt)。"
        "上方指數/ADR/供應鏈資料皆走 yfinance + 本地 JSON，不消耗 LLM。"
    )

    env_values = load_env()
    api_key = env_values.get("GEMINI_API_KEY", "")
    if not api_key:
        st.info("尚未設定 GEMINI_API_KEY，無法產出 LLM 簡報")
    else:
        if st.button(
            "🤖 呼叫 Gemini 產出簡報",
            type="primary",
            key="macro_brief",
            help=LLM_HINT_DIRECT,
        ):
            from bot.llm_analyzer import GeminiClient, gemini_call

            client = GeminiClient(
                api_key=api_key,
                model=env_values.get("GEMINI_MODEL", "gemini-2.5-flash"),
            )
            with st.spinner("Gemini 正在撰寫美股/ADR 對台股影響簡報..."):
                raw, info = gemini_call(
                    "us_market_brief",
                    client=client,
                    metadata={"task": "us_market_brief", "source": "dashboard"},
                    asof_date=data.get("asof_date", ""),
                    indices_json=json.dumps(data.get("indices", {}), ensure_ascii=False, indent=2),
                    stocks_json=json.dumps(data.get("stocks", {}), ensure_ascii=False, indent=2),
                    adr_premiums_json=json.dumps(data.get("adr_premiums", []), ensure_ascii=False, indent=2),
                    supply_chain_json=json.dumps(
                        load_supply_chain(PROJECT_ROOT).get("us_stocks", {}),
                        ensure_ascii=False, indent=2,
                    ),
                )
            if raw:
                st.session_state["us_brief"] = (raw, info)

        brief = st.session_state.get("us_brief")
        if brief:
            md, info = brief
            st.caption(f"prompt: {info.get('prompt_id', '')} v{info.get('prompt_version','')} "
                       f"｜ 延遲 {info.get('latency_ms', 0)} ms")
            st.markdown(md)

    # ---- 供應鏈對照表編輯 ----
    st.markdown("---")
    st.markdown("### 美股 → 台股供應鏈對照表")
    sc = load_supply_chain(PROJECT_ROOT)
    us_stocks = sc.get("us_stocks", {})
    st.caption(f"目前載入 {len(us_stocks)} 檔美股對照。檔案位置: `data/supply_chain.json`")

    with st.expander("檢視 / 編輯對照表", expanded=False):
        sel = st.selectbox("選擇美股", sorted(us_stocks.keys()), key="sc_sel")
        if sel:
            info = us_stocks.get(sel, {})
            st.markdown(f"**{sel} {info.get('name', '')}** ({info.get('category','')})")
            tw_rows = info.get("tw_supply_chain", []) or []
            st.dataframe(pd.DataFrame(tw_rows), hide_index=True, use_container_width=True)

        edited = st.text_area(
            "supply_chain.json 原文編輯",
            value=json.dumps(sc, ensure_ascii=False, indent=2),
            height=300, key="sc_edit",
        )
        if st.button("儲存", key="sc_save"):
            try:
                new_sc = json.loads(edited)
                save_supply_chain(new_sc, PROJECT_ROOT)
                st.success("已儲存")
                st.rerun()
            except json.JSONDecodeError as e:
                st.error(f"JSON 格式錯誤: {e}")


def page_market_calendar() -> None:
    import calendar as _calendar
    import html as _html

    from bot.market_calendar import (
        build_market_calendar,
        fetch_ex_dividend_events,
    )

    st.title("台股行事曆")
    st.caption("整合法說會、除權息與重要國際科技展。日期以台北時間為準。")

    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
    month_options = [_shift_month_for_ui(today, delta) for delta in range(-6, 10)]
    month_labels = [f"{d.year}-{d.month:02d}" for d in month_options]
    default_idx = next((i for i, d in enumerate(month_options) if d.year == today.year and d.month == today.month), 0)

    ctrl = st.columns([1.0, 1.5, 1.2, 1.2, 0.8])
    picked_month = ctrl[0].selectbox("月份", month_labels, index=default_idx, key="market_cal_month")
    selected_groups = ctrl[1].multiselect(
        "事件類型",
        ["法說會", "除權息", "國際展覽"],
        default=["法說會", "除權息", "國際展覽"],
        key="market_cal_groups",
    )
    dividend_scope_label = ctrl[2].selectbox(
        "除權息範圍",
        ["個股", "全部商品"],
        key="market_cal_div_scope",
        help="個股會排除 ETF、債券 ETF、REIT、ETN，讓月曆比較乾淨。",
    )
    query = ctrl[3].text_input("搜尋代號 / 公司 / 展覽", value="", key="market_cal_query")
    max_per_day = int(ctrl[4].number_input("每日顯示", min_value=2, max_value=20, value=6, step=1))

    year, month = [int(x) for x in picked_month.split("-")]
    month_start = dt.date(year, month, 1)
    month_end = _month_end_for_ui(month_start)
    cal = _calendar.Calendar(firstweekday=0)
    weeks = cal.monthdatescalendar(year, month)
    grid_start = weeks[0][0]
    grid_end = weeks[-1][-1]

    include_conf = "法說會" in selected_groups
    include_div = "除權息" in selected_groups
    include_expo = "國際展覽" in selected_groups
    dividend_scope = "all" if dividend_scope_label == "全部商品" else "stock"

    c_refresh, c_hint = st.columns([1, 4])
    if c_refresh.button("重新整理資料", key="market_cal_refresh"):
        try:
            from bot.conference_calendar import update_calendar
            status = st.status("更新市場行事曆資料", expanded=True)
            status.write("更新 MOPS 法說會快取。")
            status.write("更新 TWSE/TPEx 除權息資料，並套用目前選擇的商品範圍。")
            try:
                update_calendar(root=PROJECT_ROOT)
                fetch_ex_dividend_events(
                    grid_start,
                    grid_end,
                    root=PROJECT_ROOT,
                    use_cache=False,
                    security_scope=dividend_scope,
                )
                status.update(label="市場行事曆資料已更新", state="complete", expanded=False)
            except Exception as exc:
                status.update(label="市場行事曆資料更新失敗", state="error", expanded=True)
                raise exc
            st.success("行事曆資料已更新")
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            st.error(f"更新失敗: {exc}")
    c_hint.caption("展覽清單可編輯 `data/calendar/exhibitions.json`，缺檔時會自動產生初始清單。")

    status = st.status("載入市場行事曆", expanded=False)
    try:
        status.write(
            "載入類別："
            + " / ".join(
                label
                for label, enabled in (
                    ("法說會", include_conf),
                    ("除權息", include_div),
                    ("國際展覽", include_expo),
                )
                if enabled
            )
        )
        if include_conf:
            from bot.conference_calendar import ensure_calendar_fresh
            status.write("檢查 MOPS 法說會快取是否在 24 小時內。")
            ensure_calendar_fresh(root=PROJECT_ROOT, max_age_hours=24)
        events = build_market_calendar(
            grid_start,
            grid_end,
            root=PROJECT_ROOT,
            include_conferences=include_conf,
            include_dividends=include_div,
            include_exhibitions=include_expo,
            dividend_security_scope=dividend_scope,
        )
        status.update(label=f"行事曆載入完成：{len(events)} 筆事件", state="complete", expanded=False)
    except Exception as exc:  # noqa: BLE001
        status.update(label="行事曆載入失敗", state="error", expanded=True)
        st.error(f"載入行事曆失敗: {exc}")
        events = []

    if query.strip():
        terms = [t.lower() for t in query.split() if t.strip()]
        events = [e for e in events if all(t in e.search_blob() for t in terms)]

    month_events = [e for e in events if e.overlaps(month_start, month_end)]
    upcoming = [e for e in month_events if e.effective_end_date >= today]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("本月事件", len(month_events))
    m2.metric("法說會", sum(1 for e in month_events if e.category == "conference"))
    m3.metric("除權息", sum(1 for e in month_events if e.category.startswith("ex_")))
    m4.metric("展覽", sum(1 for e in month_events if e.category == "exhibition"))

    st.markdown(
        _render_market_calendar_grid(
            weeks,
            events,
            month=month,
            today=today,
            max_per_day=max_per_day,
            html_escape=_html.escape,
        ),
        unsafe_allow_html=True,
    )

    tab_month, tab_upcoming, tab_sources = st.tabs(["本月明細", "接下來事件", "資料來源"])
    with tab_month:
        _render_market_calendar_table(month_events)
    with tab_upcoming:
        _render_market_calendar_table(sorted(upcoming, key=lambda e: (e.date, e.time, e.title))[:80])
    with tab_sources:
        st.markdown(
            "- 法說會：MOPS 法說會行事曆，沿用本專案 `conference_calendar` 快取。\n"
            "- 除權息：TWSE `TWT48U_ALL` 與 TPEx `tpex_exright_prepost` 官方 OpenAPI；預設只顯示個股，可切換全部商品。\n"
            "- 展覽：`data/calendar/exhibitions.json`，預設只保留官方確認的主展：COMPUTEX、CYBERSEC、Automation Taipei、SEMICON Taiwan。"
        )
        if month_events:
            source_rows = sorted({
                (e.source, e.url)
                for e in month_events
                if e.source or e.url
            })
            st.dataframe(
                pd.DataFrame([{"來源": s, "連結": u} for s, u in source_rows]),
                hide_index=True,
                use_container_width=True,
            )


def _shift_month_for_ui(day: dt.date, delta: int) -> dt.date:
    month = day.month + delta
    year = day.year + (month - 1) // 12
    month = ((month - 1) % 12) + 1
    return dt.date(year, month, 1)


def _month_end_for_ui(day: dt.date) -> dt.date:
    next_month = _shift_month_for_ui(day, 1)
    return next_month - dt.timedelta(days=1)


def _render_market_calendar_table(events: List[Any]) -> None:
    from bot.market_calendar import event_to_row

    if not events:
        st.info("這個範圍沒有符合條件的事件。")
        return
    st.dataframe(
        pd.DataFrame([event_to_row(e) for e in events]),
        hide_index=True,
        use_container_width=True,
    )


def _render_market_calendar_grid(
    weeks: List[List[dt.date]],
    events: List[Any],
    *,
    month: int,
    today: dt.date,
    max_per_day: int,
    html_escape,
) -> str:
    weekday_labels = ["一", "二", "三", "四", "五", "六", "日"]
    parts = [
        """
<style>
.market-cal {
  display: grid;
  grid-template-columns: repeat(7, minmax(0, 1fr));
  gap: 1px;
  border: 1px solid #d9e2ec;
  background: #d9e2ec;
  margin: 12px 0 20px;
}
.market-cal-head {
  background: #f8fafc;
  padding: 8px;
  text-align: center;
  font-weight: 700;
  color: #334155;
}
.market-cal-day {
  min-height: 138px;
  background: #fff;
  padding: 7px;
  overflow: hidden;
}
.market-cal-muted {
  background: #f8fafc;
  color: #94a3b8;
}
.market-cal-today {
  box-shadow: inset 0 0 0 2px #2563eb;
}
.market-cal-date {
  font-weight: 700;
  font-size: 13px;
  margin-bottom: 5px;
}
.market-cal-chip {
  display: block;
  border-left: 4px solid #64748b;
  background: #f1f5f9;
  color: #0f172a;
  border-radius: 6px;
  padding: 3px 5px;
  margin: 3px 0;
  font-size: 12px;
  line-height: 1.25;
  white-space: normal;
}
.market-cal-conference { border-left-color: #2563eb; background: #eff6ff; }
.market-cal-ex-dividend { border-left-color: #16a34a; background: #f0fdf4; }
.market-cal-ex-right { border-left-color: #d97706; background: #fffbeb; }
.market-cal-ex-right-dividend { border-left-color: #059669; background: #ecfdf5; }
.market-cal-exhibition { border-left-color: #7c3aed; background: #f5f3ff; }
.market-cal-more {
  color: #64748b;
  font-size: 12px;
  margin-top: 4px;
}
</style>
<div class="market-cal">
""",
    ]
    for label in weekday_labels:
        parts.append(f'<div class="market-cal-head">{label}</div>')

    for week in weeks:
        for day in week:
            day_events = [e for e in events if e.occurs_on(day)]
            classes = ["market-cal-day"]
            if day.month != month:
                classes.append("market-cal-muted")
            if day == today:
                classes.append("market-cal-today")
            parts.append(f'<div class="{" ".join(classes)}">')
            parts.append(f'<div class="market-cal-date">{day.day}</div>')
            for event in day_events[:max_per_day]:
                cls = "market-cal-" + event.category.replace("_", "-")
                label = _market_calendar_event_label(event)
                parts.append(
                    f'<span class="market-cal-chip {cls}">'
                    f'{html_escape(label)}'
                    "</span>"
                )
            hidden = len(day_events) - max_per_day
            if hidden > 0:
                parts.append(f'<div class="market-cal-more">+{hidden} more</div>')
            parts.append("</div>")
    parts.append("</div>")
    return "\n".join(parts)


def _market_calendar_event_label(event: Any) -> str:
    if event.category == "conference":
        label = " ".join(x for x in (event.time, event.ticker, event.company, "法說") if x)
    elif event.category == "exhibition":
        label = event.title
    else:
        label = " ".join(x for x in (event.ticker, event.company, event.category_label) if x)
    return label if len(label) <= 34 else label[:31] + "..."


def _quick_scheduler_env(include_llm_reports: bool) -> Dict[str, str]:
    env = {"SCHEDULER_ENABLED": "true"}
    if include_llm_reports:
        env.update({
            "SCHEDULER_INTRADAY_ENABLED": "true",
            "SCHEDULER_NEXTDAY_DRAFT_ENABLED": "true",
            "SCHEDULER_NEXTDAY_UPDATE_ENABLED": "true",
        })
    return env


def _render_sidebar_quick_controls() -> None:
    env_values = load_env()
    bot_runner = get_runner(PROJECT_ROOT)
    scheduler_runner = get_scheduler_runner(PROJECT_ROOT)
    bot_running = bot_runner.is_running()
    scheduler_running = scheduler_runner.is_running()

    st.sidebar.markdown("#### 快速啟動")
    st.sidebar.markdown(
        f"{_badge('自動更新 ON' if scheduler_running else '自動更新 OFF', 'green' if scheduler_running else 'gray')} "
        f"{_badge('BOT ON' if bot_running else 'BOT OFF', 'green' if bot_running else 'gray')}",
        unsafe_allow_html=True,
    )

    include_reports = st.sidebar.toggle(
        "包含當沖 LLM 報告",
        value=True,
        key="quick_auto_llm_reports",
        disabled=scheduler_running,
        help=(
            "啟動自動更新時，臨時打開 stock-intraday、stock-nextday draft/update。"
            "同一天已有報告時會跳過，避免重複呼叫 Gemini。"
        ),
    )
    if include_reports and not env_values.get("GEMINI_API_KEY"):
        st.sidebar.caption("尚未設定 GEMINI_API_KEY；當沖報告會無法產生 LLM 簡報。")

    c1, c2 = st.sidebar.columns(2)
    if c1.button("啟動自動更新", disabled=scheduler_running, use_container_width=True):
        rec = scheduler_runner.start(
            run_mode="scheduler",
            extra_env=_quick_scheduler_env(include_reports),
        )
        st.sidebar.success(f"自動更新已啟動 PID {rec.pid}")
        time.sleep(0.4)
        st.rerun()
    if c2.button("停止更新", disabled=not scheduler_running, use_container_width=True):
        ok = scheduler_runner.stop()
        st.sidebar.success("自動更新已停止" if ok else "自動更新停止逾時")
        time.sleep(0.4)
        st.rerun()

    scheduler_record = scheduler_runner.current()
    if scheduler_running and scheduler_record:
        elapsed = time.time() - scheduler_record.started_at
        st.sidebar.caption(f"更新 PID {scheduler_record.pid} · {_human_duration(elapsed)}")

    mode_options = ["watch", "report", "trade"]
    default_mode = env_values.get("RUN_MODE", "watch")
    mode_index = mode_options.index(default_mode) if default_mode in mode_options else 0
    bot_mode = st.sidebar.selectbox(
        "BOT 模式",
        mode_options,
        index=mode_index,
        key="quick_bot_mode",
        help="watch=看盤監測；report=公開資料報表；trade=依設定交易。",
    )
    b1, b2 = st.sidebar.columns(2)
    if b1.button("啟動 BOT", disabled=bot_running, use_container_width=True):
        rec = bot_runner.start(run_mode=bot_mode)
        st.sidebar.success(f"BOT 已啟動 PID {rec.pid}")
        time.sleep(0.4)
        st.rerun()
    if b2.button("停止 BOT", disabled=not bot_running, use_container_width=True):
        ok = bot_runner.stop()
        st.sidebar.success("BOT 已停止" if ok else "BOT 停止逾時")
        time.sleep(0.4)
        st.rerun()

    st.sidebar.write("")


PAGES = {
    # 研究與分析
    "功能總覽": page_overview,
    "今日當沖戰情室": page_intraday,
    "今日當沖即時追蹤": page_intraday_live,
    "明日當沖關注": page_next_day_watch,
    "K 線看板": page_board,
    "個股總覽": page_watchlist,
    "目前持股分析": page_portfolio,
    "個股深入分析": page_ticker_detail,
    "自動化管線": page_pipeline,
    # 監控與訊號
    "美股 / 跨市場": page_macro,
    "跟單訊號": page_follow_signals,
    "主動 ETF 追蹤": page_etf_tracker,
    "LLM 法說分析": page_llm_analysis,
    # 執行與紀錄
    "啟動 / 監控": page_runner,
    "🛡 風控中心": page_risk_center,
    "交易可行性檢查": page_preflight,
    "交易紀錄": page_trades,
    "報表分析": page_reports,
    # 系統與診斷
    "組態設定": page_config,
    "資料庫 / 雲端同步": page_database,
    "Prompt 管理": page_prompts,
    "LLM 呼叫紀錄": page_llm_log,
    "日誌檢視": page_logs,
    "通知測試": page_notifier,
    "策略與文件": page_docs,
}

NAV_GROUPS = {
    "🔍 研究與分析": ["功能總覽", "今日當沖戰情室", "今日當沖即時追蹤", "明日當沖關注", "K 線看板", "個股總覽", "目前持股分析", "個股深入分析", "自動化管線"],
    "📡 監控與訊號": ["美股 / 跨市場", "跟單訊號", "主動 ETF 追蹤", "LLM 法說分析"],
    "⚡ 執行與紀錄": ["啟動 / 監控", "🛡 風控中心", "交易可行性檢查", "交易紀錄", "報表分析"],
    "⚙️ 系統與診斷": ["組態設定", "資料庫 / 雲端同步", "Prompt 管理",
                  "LLM 呼叫紀錄", "日誌檢視", "通知測試", "策略與文件"],
}

PAGES["台股行事曆"] = page_market_calendar
try:
    _market_calendar_group = next(
        (
            group
            for group, page_names in NAV_GROUPS.items()
            if any(PAGES.get(page_name) is page_llm_analysis for page_name in page_names)
        ),
        None,
    )
    if _market_calendar_group and "台股行事曆" not in NAV_GROUPS[_market_calendar_group]:
        _group_pages = NAV_GROUPS[_market_calendar_group]
        _insert_at = next(
            (i + 1 for i, page_name in enumerate(_group_pages) if PAGES.get(page_name) is page_llm_analysis),
            len(_group_pages),
        )
        _group_pages.insert(_insert_at, "台股行事曆")
except Exception:
    pass


def main_app() -> None:
    st.set_page_config(
        page_title="Stock Bot Dashboard",
        page_icon="📈",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    st.sidebar.title("Stock Bot")
    st.sidebar.caption("台股當沖機器人 儀表板")
    _render_sidebar_quick_controls()

    if "page" not in st.session_state:
        st.session_state.page = "功能總覽"

    page_keys = list(PAGES.keys())
    if st.session_state.page not in page_keys:
        st.session_state.page = page_keys[0]

    # 分組導覽：每個區塊用 markdown 標題 + 一組 radio
    for group_name, pages_in_group in NAV_GROUPS.items():
        st.sidebar.markdown(f"#### {group_name}")
        for p in pages_in_group:
            is_current = st.session_state.page == p
            label = f"▸ {p}" if is_current else f"　{p}"
            if st.sidebar.button(
                label,
                key=f"nav_{p}",
                use_container_width=True,
                type="primary" if is_current else "secondary",
            ):
                st.session_state.page = p
                st.rerun()
        st.sidebar.write("")  # 區塊之間的空隙

    pick = st.session_state.page

    st.sidebar.markdown("---")
    runner = get_runner(PROJECT_ROOT)
    if runner.is_running():
        st.sidebar.markdown(_badge("Bot 執行中", "green"), unsafe_allow_html=True)
    else:
        st.sidebar.markdown(_badge("Bot 待機", "gray"), unsafe_allow_html=True)

    try:
        env_values_for_sb = load_env()
        if env_values_for_sb.get("GEMINI_API_KEY"):
            st.sidebar.markdown(
                _badge("🤖 LLM 已啟用 (會收費)", "purple"),
                unsafe_allow_html=True,
            )
        else:
            st.sidebar.markdown(_badge("LLM 未啟用", "gray"), unsafe_allow_html=True)
        try:
            reg = get_registry(PROJECT_ROOT / "prompts")
            st.sidebar.caption(f"Prompt 載入: {len(reg.list_ids())} 個")
        except Exception:
            pass
        try:
            today_log = get_call_logger().read(dt.date.today(), limit=10000)
            tokens_in = sum(r.tokens_in or 0 for r in today_log)
            tokens_out = sum(r.tokens_out or 0 for r in today_log)
            st.sidebar.caption(
                f"今日 LLM 呼叫: {len(today_log)} 筆 "
                f"({tokens_in:,}→{tokens_out:,} tokens)"
            )
        except Exception:
            pass
    except Exception:
        pass

    st.sidebar.caption(f".env: `{env_path()}`")
    st.sidebar.caption(f"專案: `{PROJECT_ROOT}`")

    PAGES[pick]()


def run() -> None:
    """從 console script 啟動 (`uv run stock-dashboard`)。

    透過呼叫 `streamlit run <此檔案>` 來啟動，這樣使用者不必記 streamlit 指令。
    """
    script = Path(__file__).resolve()
    cmd = [sys.executable, "-m", "streamlit", "run", str(script)]
    extra = sys.argv[1:]
    if extra:
        cmd.append("--")
        cmd.extend(extra)
    try:
        sys.exit(subprocess.call(cmd))
    except KeyboardInterrupt:
        sys.exit(0)


# Streamlit 用 `streamlit run dashboard.py` 啟動時，script 的 __name__ 為 "__main__"。
# 透過 console script (`stock-dashboard`) 進來時走 run() → 再 spawn streamlit。
if __name__ == "__main__":
    main_app()
