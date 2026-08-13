"""Dashboard navigation and main Streamlit app shell."""
from __future__ import annotations

import time
from pathlib import Path

import streamlit as st

from bot.dashboard import pages as dash_pages
from bot.dashboard.cache_helpers import cached_env_values, cached_llm_today_stats
from bot.dashboard.common import (
    DASHBOARD_UI_BUILD,
    PROJECT_ROOT,
    _badge,
    _debug_log_session,
    _human_duration,
    write_dashboard_heartbeat,
)
from bot.env_io import env_path
from bot.process_runner import get_runner, get_scheduler_runner
from bot.prompt_registry import get_registry


def _render_sidebar_quick_controls() -> None:
    env_values = cached_env_values()
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
            extra_env=dash_pages._quick_scheduler_env(include_reports),
        )
        st.sidebar.success(f"自動更新已啟動 PID {rec.pid}")
        st.rerun()
    if c2.button("停止更新", disabled=not scheduler_running, use_container_width=True):
        ok = scheduler_runner.stop()
        st.sidebar.success("自動更新已停止" if ok else "自動更新停止逾時")
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
        st.rerun()
    if b2.button("停止 BOT", disabled=not bot_running, use_container_width=True):
        ok = bot_runner.stop()
        st.sidebar.success("BOT 已停止" if ok else "BOT 停止逾時")
        st.rerun()

    st.sidebar.write("")


PAGES = {
    # 研究與分析
    "功能總覽": dash_pages.page_overview,
    "今日當沖戰情室": dash_pages.page_intraday,
    "今日當沖即時追蹤": dash_pages.page_intraday_live,
    "明日當沖關注": dash_pages.page_next_day_watch,
    "K 線看板": dash_pages.page_board,
    "盤中強弱排行": dash_pages.page_market_movers,
    "個股總覽": dash_pages.page_watchlist,
    "目前持股分析": dash_pages.page_portfolio,
    "個股深入分析": dash_pages.page_ticker_detail,
    "自動化管線": dash_pages.page_pipeline,
    # 監控與訊號
    "美股 / 跨市場": dash_pages.page_macro,
    "熱門個股期貨": dash_pages.page_hot_stock_futures,
    "跟單訊號": dash_pages.page_follow_signals,
    "主動 ETF 追蹤": dash_pages.page_etf_tracker,
    "LLM 法說分析": dash_pages.page_llm_analysis,
    # 執行與紀錄
    "啟動 / 監控": dash_pages.page_runner,
    "🛡 風控中心": dash_pages.page_risk_center,
    "交易可行性檢查": dash_pages.page_preflight,
    "交易紀錄": dash_pages.page_trades,
    "報表分析": dash_pages.page_reports,
    "模擬交易": dash_pages.page_simulation,
    # 系統與診斷
    "組態設定": dash_pages.page_config,
    "資料庫 / 雲端同步": dash_pages.page_database,
    "Prompt 管理": dash_pages.page_prompts,
    "LLM 呼叫紀錄": dash_pages.page_llm_log,
    "日誌檢視": dash_pages.page_logs,
    "通知測試": dash_pages.page_notifier,
    "策略與文件": dash_pages.page_docs,
}

NAV_GROUPS = {
    "🔍 研究與分析": ["功能總覽", "今日當沖戰情室", "今日當沖即時追蹤", "明日當沖關注", "K 線看板", "盤中強弱排行", "個股總覽", "目前持股分析", "個股深入分析", "自動化管線"],
    "📡 監控與訊號": [
        "美股 / 跨市場",
        "熱門個股期貨",
        "跟單訊號",
        "主動 ETF 追蹤",
        "LLM 法說分析",
    ],
    "⚡ 執行與紀錄": ["啟動 / 監控", "模擬交易", "🛡 風控中心", "交易可行性檢查", "交易紀錄", "報表分析"],
    "⚙️ 系統與診斷": ["組態設定", "資料庫 / 雲端同步", "Prompt 管理",
                  "LLM 呼叫紀錄", "日誌檢視", "通知測試", "策略與文件"],
}

PAGES["台股行事曆"] = dash_pages.page_market_calendar
try:
    _market_calendar_group = next(
        (
            group
            for group, page_names in NAV_GROUPS.items()
            if any(PAGES.get(page_name) is dash_pages.page_llm_analysis for page_name in page_names)
        ),
        None,
    )
    if _market_calendar_group and "台股行事曆" not in NAV_GROUPS[_market_calendar_group]:
        _group_pages = NAV_GROUPS[_market_calendar_group]
        _insert_at = next(
            (i + 1 for i, page_name in enumerate(_group_pages) if PAGES.get(page_name) is dash_pages.page_llm_analysis),
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
                if st.session_state.page != p:
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
        env_values_for_sb = cached_env_values()
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
            n_calls, tokens_in, tokens_out = cached_llm_today_stats(str(PROJECT_ROOT))
            if n_calls:
                st.sidebar.caption(
                    f"今日 LLM 呼叫: {n_calls}+ 筆 "
                    f"({tokens_in:,}→{tokens_out:,} tokens)"
                )
        except Exception:
            pass
    except Exception:
        pass

    st.sidebar.caption(f".env: `{env_path()}`")
    st.sidebar.caption(f"專案: `{PROJECT_ROOT}`")
    st.sidebar.caption(f"UI build: `{DASHBOARD_UI_BUILD}`")
    try:
        from bot.dashboard import common as _dash_common

        st.sidebar.caption(f"模組: `{Path(_dash_common.__file__).name}`")
    except Exception:
        pass

    write_dashboard_heartbeat(pick)
    _debug_log_session(
        "H11",
        "nav.py:main_app",
        "dashboard page render",
        {"page": pick},
    )
    st.caption(
        f"🔧 儀表板版本 **`{DASHBOARD_UI_BUILD}`** · 專案 `{PROJECT_ROOT}` · "
        f"目前頁面：**{pick}**"
    )
    PAGES[pick]()


if __name__ == "__main__":
    main_app()

