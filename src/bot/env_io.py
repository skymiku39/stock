"""env_io -- .env 安全讀寫工具，供儀表板載入/顯示/更新組態。

不依賴 pydantic-settings，避免 UI 在缺欄位時就 raise ValidationError，
讓使用者可以在 Streamlit 介面慢慢補齊欄位再儲存。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class EnvField:
    """單一組態欄位的中繼資料 (給 UI 用)。"""

    key: str
    label: str
    section: str
    kind: str  # "str" | "int" | "float" | "bool" | "time" | "select" | "password" | "symbols"
    default: Any = ""
    help: str = ""
    options: List[str] = field(default_factory=list)
    secret: bool = False


# ----------------------------------------------------------------------
# 欄位定義 (對齊 src/bot/config.py 的 Settings)
# ----------------------------------------------------------------------

ENV_FIELDS: List[EnvField] = [
    # 執行模式
    EnvField(
        "RUN_MODE", "執行模式", "執行模式", "select",
        default="trade", options=["trade", "watch", "report"],
        help="trade=自動交易 / watch=看盤不下單 / report=純報表分析",
    ),
    EnvField(
        "MARKET_SOURCE", "行情來源", "執行模式", "select",
        default="", options=["", "shioaji", "twse_public"],
        help="留空自動依模式判斷 (trade/watch→shioaji, report→twse_public)",
    ),
    EnvField(
        "REPORT_POLL_SECONDS", "公開資料輪詢秒數", "執行模式", "int",
        default="5", help="僅 report 模式有效",
    ),
    EnvField(
        "REPORT_OUTPUT_DIR", "報表輸出目錄", "執行模式", "str",
        default="data/reports",
    ),

    # Shioaji 登入
    EnvField(
        "API_KEY", "API Key", "Shioaji 登入", "password",
        secret=True, help="trade/watch 模式必填",
    ),
    EnvField(
        "SECRET_KEY", "Secret Key", "Shioaji 登入", "password",
        secret=True, help="trade/watch 模式必填",
    ),

    # 電子憑證
    EnvField(
        "CA_PATH", "憑證路徑 (.pfx)", "電子憑證", "str",
        help="trade 模式實單必填",
    ),
    EnvField(
        "CA_PASSWORD", "憑證密碼", "電子憑證", "password",
        secret=True, help="trade 模式實單必填",
    ),
    EnvField(
        "PERSON_ID", "身分證字號", "電子憑證", "password",
        secret=True, help="trade 模式實單必填",
    ),
    EnvField(
        "SIMULATION", "模擬模式", "電子憑證", "bool",
        default="true", help="true 不會送出真實委託 (僅 trade 模式有意義)",
    ),

    # 監控與時間
    EnvField(
        "SYMBOLS", "監控股票 (逗號分隔)", "策略", "symbols",
        default="2330,0050",
    ),
    EnvField(
        "ENTER_CUTOFF_TIME", "停止進場時間", "策略", "time",
        default="09:30",
    ),
    EnvField(
        "EXIT_TIME", "全部出場時間", "策略", "time",
        default="13:15",
    ),

    # 風控
    EnvField(
        "STOP_LOSS_PCT", "停損百分比", "風控", "float",
        default="-3.0",
    ),
    EnvField(
        "TAKE_PROFIT_PCT", "停利門檻百分比", "風控", "float",
        default="6.0",
    ),
    EnvField(
        "TRAILING_STOP_PCT", "移動停利回撤百分比", "風控", "float",
        default="2.0",
    ),
    EnvField(
        "MAX_FUND", "資金上限 (元)", "風控", "int",
        default="500000",
        help="總可用資金的天花板，所有持倉成本加總不會超過此值",
    ),
    EnvField(
        "MAX_LOT_PER_SYMBOL", "每檔最大張數", "風控", "int",
        default="2",
    ),

    # 進階風控 - 資金 / 持倉
    EnvField(
        "PER_ORDER_MAX_COST_TWD", "單筆委託金額上限 (元)", "進階風控", "int",
        default="0",
        help="單筆委託成本不超過此值，0=不限。例如填 100000 = 每筆委託最多 10 萬",
    ),
    EnvField(
        "MAX_OPEN_POSITIONS", "同時最多在倉檔數", "進階風控", "int",
        default="0",
        help="0=不限。例如填 3 表示同一時間最多持有 3 檔",
    ),

    # 進階風控 - 下單頻率
    EnvField(
        "DAILY_MAX_ORDERS", "當日最多進場次數", "進階風控", "int",
        default="0",
        help="0=不限。例如填 5 表示一天最多進場 5 次",
    ),
    EnvField(
        "PER_SYMBOL_DAILY_MAX_ORDERS", "單檔當日進場次數上限", "進階風控", "int",
        default="0",
        help="0=不限。建議設 1~2，避免同一檔反覆進出",
    ),
    EnvField(
        "REENTRY_COOLDOWN_SECONDS", "同檔再進場冷卻 (秒)", "進階風控", "int",
        default="0",
        help="0=立即可再進。建議 300~900 (5~15 分鐘) 避免追漲殺跌",
    ),

    # 進階風控 - 進場條件
    EnvField(
        "MAX_PCT_CHG_ON_ENTRY", "進場最大漲幅 (%)", "進階風控", "float",
        default="0",
        help="0=不限。例如填 4 表示漲幅超過 4% 不再進場 (防追高)",
    ),
    EnvField(
        "MIN_PRICE", "最低股價過濾 (元)", "進階風控", "float",
        default="0",
        help="0=不限。例如填 10 表示低於 10 元的股票不進場",
    ),
    EnvField(
        "MAX_PRICE", "最高股價過濾 (元)", "進階風控", "float",
        default="0",
        help="0=不限。避免買到高價股 (例如台積電 1000+)",
    ),
    EnvField(
        "BLACKLIST_SYMBOLS", "黑名單 (逗號分隔)", "進階風控", "str",
        default="",
        help="這些代號永遠不會被下單，例如 2498,3034",
    ),

    # 進階風控 - 損失熔斷
    EnvField(
        "DAILY_MAX_LOSS_TWD", "當日最大虧損 (元)", "進階風控", "int",
        default="0",
        help="0=不限。當日已實現虧損達此值，自動拉起 kill switch 停止開新倉",
    ),
    EnvField(
        "DAILY_MAX_LOSS_PCT", "當日最大虧損 (占資金 %)", "進階風控", "float",
        default="0",
        help="0=不限。與上面取較嚴者。例如填 2 = 虧損超過 max_fund 的 2% 即熔斷",
    ),

    # Telegram
    EnvField(
        "TELEGRAM_BOT_TOKEN", "Telegram Bot Token", "Telegram 通知", "password",
        secret=True, help="留空則不啟用",
    ),
    EnvField(
        "TELEGRAM_CHAT_ID", "Telegram Chat ID", "Telegram 通知", "str",
        help="留空則不啟用",
    ),

    # 主動 ETF 跟單
    EnvField(
        "STRATEGY_TYPE", "策略類型", "策略", "select",
        default="default", options=["default", "etf_follow"],
        help="default=當沖示範策略 / etf_follow=主動 ETF 共識跟單",
    ),
    EnvField(
        "ETF_MIN_CONSENSUS_NEW", "新建倉共識最小 ETF 數", "策略", "int",
        default="2", help="同一檔個股被 N 檔以上 ETF 同時新建倉",
    ),
    EnvField(
        "ETF_MIN_CONSENSUS_ADD", "共識加碼最小 ETF 數", "策略", "int",
        default="3",
    ),
    EnvField(
        "ETF_MAX_PCT_CHG_ON_ENTRY", "進場最大漲幅 (%)", "策略", "float",
        default="4.0",
    ),

    # Gemini LLM
    EnvField(
        "GEMINI_API_KEY", "Google Gemini API Key", "LLM 分析", "password",
        secret=True,
        help="Google AI Studio 免費 API Key，用於法說會語意解析",
    ),
    EnvField(
        "GEMINI_MODEL", "Gemini 模型", "LLM 分析", "select",
        default="gemini-2.5-flash",
        options=[
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-2.5-pro",
            "gemini-2.0-flash",
        ],
    ),

    # 自動化管線
    EnvField(
        "PIPELINE_CHIP_LOOKBACK_DAYS", "籌碼面回顧天數", "自動化管線", "int",
        default="5", help="auto-research 抓三大法人/借券/融資 的回顧天數",
    ),
    EnvField(
        "PIPELINE_MIN_CONSENSUS", "共識焦點門檻", "自動化管線", "int",
        default="2", help="個股被 N 檔以上 ETF 持有才算焦點",
    ),
    EnvField(
        "PIPELINE_FETCH_ETF", "自動抓 ETF 持股", "自動化管線", "bool",
        default="true",
    ),
    EnvField(
        "PIPELINE_FETCH_CHIPS", "自動抓籌碼面", "自動化管線", "bool",
        default="true",
    ),
    EnvField(
        "PIPELINE_GENERATE_BRIEF", "產出每日簡報", "自動化管線", "bool",
        default="true",
    ),
    EnvField(
        "PIPELINE_FETCH_MACRO", "自動抓美股 / 加權", "自動化管線", "bool",
        default="true",
        help="使用 yfinance 抓 S&P/NASDAQ/SOX/VIX/TWII + 重要美股 + ADR 溢價",
    ),
    EnvField(
        "PIPELINE_GENERATE_US_BRIEF", "產出美股跨市場簡報", "自動化管線", "bool",
        default="true",
        help="呼叫 Gemini 把美股盤後翻譯為對台股供應鏈的影響評估",
    ),

    # 資料庫 / 雲端同步
    EnvField(
        "STOCK_DB_PATH", "本地股票 DB 路徑", "資料庫與雲端", "str",
        default="",
        help="留空 = data/stock.db。要用 Google 雲端硬碟單機共用時，"
             "請改成 Drive 同步資料夾下的路徑，例如 "
             "G:/My Drive/stock-shared/stock.db",
    ),
    EnvField(
        "GOOGLE_SHEET_ID", "Google Sheet ID", "資料庫與雲端", "str",
        default="",
        help="Sheet URL 中 /d/ 後面那串隨機 ID。空 = 不啟用雲端同步。",
    ),
    EnvField(
        "GOOGLE_SA_JSON_PATH", "Service Account JSON 路徑", "資料庫與雲端", "str",
        default="",
        help="Google Cloud 的 service account 金鑰 JSON 檔案路徑。"
             "設定步驟見 docs/cloud_sync_setup.md",
    ),
    EnvField(
        "GOOGLE_CACHE_DIR", "Google cache folder", "雲端快取資料夾", "str",
        default="",
        help="Optional Google Drive Desktop/shared folder. Cached JSON/CSV/PDF files "
             "under data/ are mirrored here and restored before refetch.",
    ),
]


def field_map() -> Dict[str, EnvField]:
    return {f.key: f for f in ENV_FIELDS}


def grouped_fields() -> Dict[str, List[EnvField]]:
    groups: Dict[str, List[EnvField]] = {}
    for f in ENV_FIELDS:
        groups.setdefault(f.section, []).append(f)
    return groups


# ----------------------------------------------------------------------
# .env 解析 / 序列化
# ----------------------------------------------------------------------


def env_path(root: Optional[Path] = None) -> Path:
    base = root or Path.cwd()
    return base / ".env"


def load_env(path: Optional[Path] = None) -> Dict[str, str]:
    """簡易 .env parser，支援 KEY=VALUE 與註解。"""
    p = path or env_path()
    if not p.exists():
        return {}
    data: Dict[str, str] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            continue
        k, v = s.split("=", 1)
        data[k.strip()] = v.strip().strip('"').strip("'")
    return data


def save_env(values: Dict[str, str], path: Optional[Path] = None) -> Path:
    """覆寫 .env (依 ENV_FIELDS 的順序)，自動備份原檔到 .env.bak。"""
    p = path or env_path()
    if p.exists():
        shutil.copy2(p, p.with_suffix(p.suffix + ".bak"))

    lines: List[str] = []
    groups = grouped_fields()
    for section, fields_in_section in groups.items():
        lines.append(f"# === {section} ===")
        for f in fields_in_section:
            v = values.get(f.key, "")
            if v == "" and f.kind == "bool":
                v = str(f.default).lower()
            lines.append(f"{f.key}={v}")
        lines.append("")

    extra = set(values.keys()) - set(field_map().keys())
    if extra:
        lines.append("# === 其他 ===")
        for k in sorted(extra):
            lines.append(f"{k}={values[k]}")
        lines.append("")

    p.write_text("\n".join(lines), encoding="utf-8")
    return p


# ----------------------------------------------------------------------
# 驗證
# ----------------------------------------------------------------------


def validate(values: Dict[str, str]) -> List[str]:
    """回傳錯誤訊息列表 (空 list 表示通過)。"""
    errors: List[str] = []
    fmap = field_map()

    for key, v in values.items():
        f = fmap.get(key)
        if f is None:
            continue
        if v == "":
            continue
        try:
            if f.kind == "int":
                int(v)
            elif f.kind == "float":
                float(v)
            elif f.kind == "bool":
                if v.lower() not in ("true", "false", "1", "0", "yes", "no"):
                    errors.append(f"{f.label}({key}) 必須是 true/false")
            elif f.kind == "time":
                parts = v.split(":")
                if len(parts) != 2:
                    raise ValueError
                int(parts[0])
                int(parts[1])
            elif f.kind == "select" and f.options and v not in f.options:
                errors.append(
                    f"{f.label}({key}) 必須是 {'/'.join(o for o in f.options if o) or '(空)'} 其中之一"
                )
        except ValueError:
            errors.append(f"{f.label}({key}) 格式錯誤: {v!r}")

    run_mode = values.get("RUN_MODE", "trade")
    if run_mode in ("trade", "watch"):
        if not values.get("API_KEY"):
            errors.append(f"run_mode={run_mode} 需要 API_KEY")
        if not values.get("SECRET_KEY"):
            errors.append(f"run_mode={run_mode} 需要 SECRET_KEY")

    if run_mode == "trade" and values.get("SIMULATION", "true").lower() == "false":
        for k in ("CA_PATH", "CA_PASSWORD", "PERSON_ID"):
            if not values.get(k):
                errors.append(f"實單模式 (SIMULATION=false) 需要 {k}")

    poll = values.get("REPORT_POLL_SECONDS", "")
    if poll:
        try:
            if int(poll) <= 0:
                errors.append("REPORT_POLL_SECONDS 必須 > 0")
        except ValueError:
            pass

    return errors


def masked(values: Dict[str, str]) -> Dict[str, str]:
    """回傳遮罩過後的版本 (供顯示)。"""
    fmap = field_map()
    out: Dict[str, str] = {}
    for k, v in values.items():
        f = fmap.get(k)
        if f and f.secret and v:
            out[k] = "•" * 8
        else:
            out[k] = v
    return out


__all__ = [
    "ENV_FIELDS",
    "EnvField",
    "env_path",
    "field_map",
    "grouped_fields",
    "load_env",
    "masked",
    "save_env",
    "validate",
]
