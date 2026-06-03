from __future__ import annotations

import datetime
from typing import Annotated, Dict, List, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- 執行模式 ---
    run_mode: Literal["trade", "watch", "report"] = "trade"
    broker_backend: Literal["shioaji", "t4"] = "shioaji"
    market_source: Literal["shioaji", "twse_public", ""] = ""
    report_poll_seconds: int = 5
    report_output_dir: str = "data/reports"

    # --- Shioaji 登入 ---
    api_key: str = ""
    secret_key: str = ""

    # --- T4 DLL 下單元件 (Windows-only；目前作為下單 adapter 測試骨架) ---
    t4_dll_path: str = ""
    t4_dll_dir: str = ""
    t4_login_id: str = ""
    t4_login_password: str = ""
    t4_person_id: str = ""
    t4_ca_path: str = ""
    t4_ca_password: str = ""
    t4_stock_branch: str = ""
    t4_stock_account: str = ""

    # --- 電子憑證 ---
    ca_path: str = ""
    ca_password: str = ""
    person_id: str = ""

    # --- 模擬模式 ---
    simulation: bool = True

    # --- 監控股票 (逗號分隔, e.g. "2330,0050,2881") ---
    symbols: Annotated[List[str], NoDecode] = []

    @field_validator("symbols", mode="before")
    @classmethod
    def _parse_symbols(cls, v: object) -> List[str]:
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return list(v)  # type: ignore[arg-type]

    # --- 策略時間 ---
    enter_cutoff_time: datetime.time = datetime.time(9, 30)
    exit_time: datetime.time = datetime.time(13, 15)

    @field_validator("enter_cutoff_time", "exit_time", mode="before")
    @classmethod
    def _parse_time(cls, v: object) -> datetime.time:
        if isinstance(v, datetime.time):
            return v
        if isinstance(v, str):
            parts = v.split(":")
            return datetime.time(int(parts[0]), int(parts[1]))
        raise ValueError(f"Cannot parse time from {v!r}")

    # --- 停損停利 (百分比) ---
    stop_loss_pct: float = -3.0
    take_profit_pct: float = 6.0
    trailing_stop_pct: float = 2.0  # 從最高點回撤此百分比則觸發停利
    sell_profit_targets: Annotated[Dict[str, float], NoDecode] = Field(default_factory=dict)
    # 例: SELL_PROFIT_TARGETS=2330:8,0050:5.5
    # 有設定的股票，AI 部位只有在報酬率達到該門檻後才允許自動賣出。

    @field_validator("sell_profit_targets", mode="before")
    @classmethod
    def _parse_sell_profit_targets(cls, v: object) -> Dict[str, float]:
        if v in (None, ""):
            return {}
        if isinstance(v, dict):
            return {str(k).strip(): float(val) for k, val in v.items() if str(k).strip()}
        if isinstance(v, str):
            out: Dict[str, float] = {}
            for raw_item in v.replace(";", ",").split(","):
                item = raw_item.strip()
                if not item:
                    continue
                if ":" in item:
                    symbol, pct = item.split(":", 1)
                elif "=" in item:
                    symbol, pct = item.split("=", 1)
                else:
                    raise ValueError(
                        "SELL_PROFIT_TARGETS must use SYMBOL:PCT pairs, "
                        f"got {item!r}"
                    )
                symbol = symbol.strip()
                if not symbol:
                    raise ValueError("SELL_PROFIT_TARGETS contains an empty symbol")
                out[symbol] = float(pct.strip())
            return out
        raise ValueError(f"Cannot parse sell_profit_targets from {v!r}")

    # --- 資金控管 (基本) ---
    max_fund: int = 500_000              # 總可用資金上限 (TWD)
    max_lot_per_symbol: int = 2          # 單檔最大持有張數

    # --- 資金控管 (進階風控) ---
    per_order_max_cost_twd: int = 0      # 單筆委託金額上限 (0=不限)，例 100000
    max_open_positions: int = 0          # 同時最多 N 檔在倉 (0=不限)
    daily_max_orders: int = 0            # 當日進場單數上限 (0=不限)
    per_symbol_daily_max_orders: int = 0 # 單檔當日進場上限 (0=不限)
    reentry_cooldown_seconds: int = 0    # 平倉後同檔冷卻秒數 (0=立即可再進)

    # --- 進場條件 ---
    max_pct_chg_on_entry: float = 0.0    # 漲幅超過 N% 不進場 (0=不限)
    min_price: float = 0.0               # 最低股價 (0=不限)，避免低價股
    max_price: float = 0.0               # 最高股價 (0=不限)，避免超高價股
    blacklist_symbols: Annotated[List[str], NoDecode] = []   # 強制不交易的代號

    # --- 損失熔斷 ---
    daily_max_loss_twd: int = 0          # 當日實現虧損絕對值上限 (0=不限)
    daily_max_loss_pct: float = 0.0      # 當日虧損占 max_fund 百分比 (0=不限)
    # 兩者擇較嚴者；觸發後 RiskGuard 會自動拉起 kill switch

    @field_validator("blacklist_symbols", mode="before")
    @classmethod
    def _parse_blacklist(cls, v: object) -> List[str]:
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return list(v)  # type: ignore[arg-type]

    # --- Telegram 通知 (留空則不啟用) ---
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- 策略類型 ---
    strategy_type: Literal["default", "etf_follow"] = "default"

    # --- 主動 ETF 跟單參數 ---
    etf_min_consensus_new: int = 2
    etf_min_consensus_add: int = 3
    etf_max_pct_chg_on_entry: float = 4.0

    # --- Gemini LLM ---
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"

    # --- 自動化研究管線 ---
    pipeline_chip_lookback_days: int = 5
    pipeline_min_consensus: int = 2
    pipeline_fetch_etf: bool = True
    pipeline_fetch_chips: bool = True
    pipeline_generate_brief: bool = True
    pipeline_fetch_macro: bool = True
    pipeline_generate_us_brief: bool = True

    # --- 背景排程器 (stock-scheduler) ---
    # 讓資料更新與盤中監測「持續自動執行」，不需開著儀表板。
    scheduler_enabled: bool = True
    scheduler_tick_seconds: int = 30          # 主迴圈每隔幾秒檢查一次有無到期任務
    # 輕量行情/總經刷新 (不打 LLM，成本低)；<=0 停用
    scheduler_macro_interval_min: int = 30
    scheduler_fundamentals_interval_min: int = 360
    scheduler_fundamentals_args: str = "--limit 3 --stale-days 30 --delay-seconds 30"
    # 完整研究管線 (ETF/籌碼/基本面/法說 + LLM 簡報)；<=0 停用
    scheduler_research_interval_min: int = 240
    # 傳給 stock-auto-research 的額外參數 (例如 "--no-brief" 省 LLM 成本)
    scheduler_research_args: str = ""
    # True=資料刷新只在台股交易時段 (含盤前盤後緩衝) 與平日執行
    scheduler_market_hours_only: bool = True
    scheduler_run_on_start: bool = True       # 啟動時先立刻跑一輪
    # 盤中自動托管 stock-bot 監測子行程 (開盤啟動、收盤停止)
    scheduler_supervise_monitor: bool = False
    scheduler_monitor_mode: Literal["watch", "report", "trade"] = "watch"

    # --- 本地股票資料庫 (SQLite) ---
    # 留空 = 預設 data/stock.db；可指向同步資料夾，例如:
    #   STOCK_DB_PATH=G:/My Drive/stock-shared/stock.db
    stock_db_path: str = ""

    # --- 雲端同步 (Google Sheets，多機共用使用者資料用) ---
    # Sheets URL 中 /d/ 後面那串隨機 ID；留空則停用雲端同步。
    google_sheet_id: str = ""
    # Service Account JSON 檔案路徑 (推薦) 或 JSON 字串。
    # 取得方式請見 docs/cloud_sync_setup.md
    google_sa_json_path: str = ""
    # Optional Google Drive Desktop / shared cloud folder used to mirror
    # fetched JSON/CSV/PDF cache files across machines.
    google_cache_dir: str = ""

    @field_validator("report_poll_seconds")
    @classmethod
    def _validate_report_poll_seconds(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("report_poll_seconds must be greater than 0")
        return v

    @model_validator(mode="after")
    def _resolve_market_source(self) -> Settings:
        if not self.market_source:
            self.market_source = (  # type: ignore[assignment]
                "twse_public" if self.run_mode == "report" else "shioaji"
            )
        return self

    @model_validator(mode="after")
    def _validate_mode_requirements(self) -> Settings:
        if self.run_mode in ("trade", "watch") and self.market_source == "twse_public":
            raise ValueError(
                f"run_mode={self.run_mode!r} 需要 Shioaji 行情，"
                "market_source 不可為 'twse_public'"
            )
        if self.run_mode == "report" and self.market_source != "twse_public":
            raise ValueError(
                "run_mode='report' 不登入 Shioaji，market_source 必須為 'twse_public'"
            )
        if self.run_mode == "trade" and not self.api_key:
            pass  # api_key 可由 .env 延後提供，不在此強制檢查
        return self
