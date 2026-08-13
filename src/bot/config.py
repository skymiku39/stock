from __future__ import annotations

import datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- 執行模式 ---
    run_mode: Literal["trade", "watch", "report"] = "watch"
    # 當沖實單已封存；trade 需 DAY_TRADING_UNFREEZE=true
    day_trading_archived: bool = True
    day_trading_unfreeze: bool = False
    broker_backend: Literal["shioaji"] = "shioaji"
    market_source: Literal["shioaji", "twse_public", ""] = ""
    report_poll_seconds: int = 5
    report_output_dir: str = "data/reports"

    # --- Shioaji 登入 ---
    api_key: str = ""
    secret_key: str = ""

    # --- 電子憑證 ---
    ca_path: str = ""
    ca_password: str = ""
    person_id: str = ""

    # --- 模擬模式 ---
    simulation: bool = True

    # --- 監控股票 (逗號分隔, e.g. "2330,0050,2881") ---
    symbols: Annotated[list[str], NoDecode] = []

    @field_validator("symbols", mode="before")
    @classmethod
    def _parse_symbols(cls, v: object) -> list[str]:
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return list(v)  # type: ignore[arg-type]

    # --- 監控池四源合併 (watch_symbol_pool) ---
    symbols_auto_merge: bool = True
    symbols_merge_top_n: int = 10          # 每個來源最多取幾檔
    symbols_merge_max_total: int = 24      # 合併後監控上限
    symbols_merge_refresh_min: int = 10    # 盤中刷新間隔 (分鐘)
    symbols_merge_budget_filter: bool = True  # 依 DAILY_FUND_BUDGET 過濾買不起的標的

    # --- 策略時間 ---
    enter_cutoff_time: datetime.time = datetime.time(9, 30)
    exit_time: datetime.time = datetime.time(13, 15)
    # 午盤獲利平倉窗口起點；設定後 [起點, exit_time) 內淨利 > 0 即賣出（不等移動停利 2%）
    profit_exit_start_time: datetime.time | None = None

    @field_validator(
        "enter_cutoff_time",
        "exit_time",
        "profit_exit_start_time",
        "scheduler_intraday_time",
        "scheduler_intraday_end_time",
        "scheduler_nextday_draft_time",
        "scheduler_nextday_draft_end_time",
        "scheduler_nextday_update_time",
        "scheduler_nextday_update_end_time",
        mode="before",
    )
    @classmethod
    def _parse_time(cls, v: object) -> datetime.time | None:
        if v in (None, ""):
            return None
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
    sell_profit_targets: Annotated[dict[str, float], NoDecode] = Field(default_factory=dict)
    # 例: SELL_PROFIT_TARGETS=2330:8,0050:5.5
    # 有設定的股票，AI 部位只有在報酬率達到該門檻後才允許自動賣出。

    @field_validator("sell_profit_targets", mode="before")
    @classmethod
    def _parse_sell_profit_targets(cls, v: object) -> dict[str, float]:
        if v in (None, ""):
            return {}
        if isinstance(v, dict):
            return {str(k).strip(): float(val) for k, val in v.items() if str(k).strip()}
        if isinstance(v, str):
            out: dict[str, float] = {}
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
    daily_fund_budget: int = 0           # 每日持股預算 (0=沿用 max_fund)，例 10000
    max_lot_per_symbol: int = 2          # 單檔最大持有張數

    # --- 資金控管 (進階風控) ---
    per_order_max_cost_twd: int = 0      # 單筆委託金額上限 (0=不限)，例 100000
    max_open_positions: int = 0          # 同時最多 N 檔在倉 (0=不限)
    daily_max_orders: int = 0            # 當日進場單數上限 (0=不限)
    per_symbol_daily_max_orders: int = 0 # 單檔當日進場上限 (0=不限)
    reentry_cooldown_seconds: int = 0    # 平倉後同檔冷卻秒數 (0=立即可再進)
    allow_same_day_reentry: bool = True  # 同檔當日多次進出（回落買回）
    rebuy_target_net_pct: float = 2.0    # 回落買回目標淨利 %（含手續費+稅）

    # --- 帳戶餘額檢查 (trade 模式進場前) ---
    check_account_balance: bool = True   # 下單前讀 account_balance；讀不到則拒絕進場

    # --- 交易成本 (當沖淨利計算) ---
    broker_fee_discount: float = 0.28    # 手續費折扣倍率，例 0.28 = 28 折
    broker_min_fee: float = 1.0          # 最低手續費（元）
    day_trade_tax_rate: float = 0.0015   # 當沖證交稅率 0.15%

    # --- 進場條件 (configurable 策略) ---
    min_pct_chg_on_entry: float = 1.0    # 全域最低進場漲幅 %
    # 例: BUY_ENTRY_TARGETS=2330:1:5,0050:0.5:3
    buy_entry_targets: Annotated[dict[str, tuple[float, float]], NoDecode] = Field(
        default_factory=dict,
    )
    max_pct_chg_on_entry: float = 0.0    # 漲幅超過 N% 不進場 (0=不限)
    min_price: float = 0.0               # 最低股價 (0=不限)，避免低價股
    max_price: float = 0.0               # 最高股價 (0=不限)，避免超高價股
    blacklist_symbols: Annotated[list[str], NoDecode] = []   # 強制不交易的代號
    # 手動長期持股：永不自動監控/交易（併入風控黑名單）
    manual_hold_symbols: Annotated[list[str], NoDecode] = []

    # --- 損失熔斷 ---
    daily_max_loss_twd: int = 0          # 當日實現虧損絕對值上限 (0=不限)
    daily_max_loss_pct: float = 0.0      # 當日虧損占 effective_fund_cap 百分比 (0=不限)
    # 兩者擇較嚴者；觸發後 RiskGuard 會自動拉起 kill switch

    @field_validator("buy_entry_targets", mode="before")
    @classmethod
    def _parse_buy_entry_targets(cls, v: object) -> dict[str, tuple[float, float]]:
        if v in (None, ""):
            return {}
        if isinstance(v, dict):
            out: dict[str, tuple[float, float]] = {}
            for sym, val in v.items():
                symbol = str(sym).strip()
                if not symbol:
                    continue
                if isinstance(val, (list, tuple)) and len(val) >= 2:
                    out[symbol] = (float(val[0]), float(val[1]))
                else:
                    raise ValueError(
                        f"BUY_ENTRY_TARGETS[{symbol!r}] must be (min_pct, max_pct)"
                    )
            return out
        if isinstance(v, str):
            parsed: dict[str, tuple[float, float]] = {}
            for raw_item in v.replace(";", ",").split(","):
                item = raw_item.strip()
                if not item:
                    continue
                parts = item.split(":")
                if len(parts) != 3:
                    raise ValueError(
                        "BUY_ENTRY_TARGETS must use SYMBOL:MIN_PCT:MAX_PCT triples, "
                        f"got {item!r}"
                    )
                symbol, min_pct, max_pct = (p.strip() for p in parts)
                if not symbol:
                    raise ValueError("BUY_ENTRY_TARGETS contains an empty symbol")
                parsed[symbol] = (float(min_pct), float(max_pct))
            return parsed
        raise ValueError(f"Cannot parse buy_entry_targets from {v!r}")

    @field_validator("blacklist_symbols", mode="before")
    @classmethod
    def _parse_blacklist(cls, v: object) -> list[str]:
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return list(v)  # type: ignore[arg-type]

    @field_validator("manual_hold_symbols", mode="before")
    @classmethod
    def _parse_manual_hold(cls, v: object) -> list[str]:
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return list(v)  # type: ignore[arg-type]

    # --- 零股 (小額資金) ---
    use_odd_lot: bool = False
    odd_lot_max_shares: int = 999

    # --- LLM 進出場閘門 ---
    llm_gate_enabled: bool = False
    llm_min_sentiment_score: float = 0.2
    llm_min_day_trade_score: float = 62.0
    llm_exit_on_negative: bool = False
    llm_refresh_on_entry: bool = False
    llm_sell_gate_enabled: bool = False
    llm_sell_gate_bypass_stop_loss: bool = True
    llm_sell_gate_bypass_close: bool = True
    llm_sell_gate_bypass_afternoon: bool = True
    llm_refresh_on_exit: bool = True
    llm_sell_min_confidence: float = 0.5
    # 盤中定時 + 成交事件觸發：刷新 auto_llm 並產出 intraday_live_review
    llm_intraday_review_enabled: bool = False
    llm_intraday_review_interval_min: int = 60
    # 無持倉時縮短檢討間隔（分鐘）；有持倉時沿用 llm_intraday_review_interval_min
    llm_intraday_review_interval_flat_min: int = 10

    # --- Telegram 通知 (留空則不啟用) ---
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- 微笑曲線策略 (stock-smile-backtest / 規劃中 live) ---
    smile_buy_tiers: str = "1:1,3:2,5:3,8:4"
    smile_base_lot: int = 1
    smile_regular_tax_rate: float = 0.003  # 一般證交稅（非當沖）
    smile_reference_prices: Annotated[dict[str, float], NoDecode] = Field(
        default_factory=dict,
    )

    @field_validator("smile_reference_prices", mode="before")
    @classmethod
    def _parse_smile_reference_prices(cls, v: object) -> dict[str, float]:
        if v in (None, ""):
            return {}
        if isinstance(v, dict):
            return {str(k).strip(): float(val) for k, val in v.items() if str(k).strip()}
        if isinstance(v, str):
            parsed: dict[str, float] = {}
            for raw_item in v.replace(";", ",").split(","):
                item = raw_item.strip()
                if not item:
                    continue
                parts = item.split(":")
                if len(parts) != 2:
                    raise ValueError(
                        "SMILE_REFERENCE_PRICES must use SYMBOL:PRICE pairs, "
                        f"got {item!r}",
                    )
                sym, px = (p.strip() for p in parts)
                if not sym:
                    raise ValueError("SMILE_REFERENCE_PRICES contains an empty symbol")
                parsed[sym] = float(px)
            return parsed
        raise ValueError(f"Cannot parse smile_reference_prices from {v!r}")

    # --- 策略類型 ---
    strategy_type: Literal["etf_follow", "configurable", "smile_curve"] = "configurable"

    @field_validator("strategy_type", mode="before")
    @classmethod
    def _normalize_strategy_type(cls, v: object) -> object:
        if isinstance(v, str) and v.strip().lower() == "default":
            return "configurable"
        return v

    # --- 主動 ETF 跟單參數 ---
    etf_min_consensus_new: int = 2
    etf_min_consensus_add: int = 3
    etf_max_pct_chg_on_entry: float = 4.0

    # --- Gemini LLM ---
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    # 允許 auto_llm 在無 research_ticker prompt 時退回 analyze_presentation
    auto_llm_allow_legacy: bool = False

    # --- LLM 提供者 (統一路由) ---
    # chain = Gemini 閘道 → Cursor 閘道 → Gemini SDK（推薦）
    # gemini_gateway = 僅 Gemini 瀏覽器閘道 (port 8816)
    # cursor = 僅 Cursor 閘道 (port 8815)
    # gemini = 僅 Gemini SDK API (需 API Key)
    llm_provider: Literal["chain", "gemini_gateway", "cursor", "gemini"] = "chain"

    # --- Gemini 閘道 (蹭google的geminiAI) ---
    gemini_gateway_base_url: str = "http://127.0.0.1:8816"
    gemini_gateway_timeout: float = 180
    gemini_gateway_model_label: str = "gemini-auto"

    # --- Cursor 閘道 (蹭cursor的AI) ---
    cursor_llm_base_url: str = "http://127.0.0.1:8815"
    cursor_llm_timeout: float = 180
    cursor_llm_model_label: str = "cursor-auto"

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
    # 公司基本資料 (名稱/產業/上市日) 補齊；靜態資料，預設一天一次；<=0 停用
    scheduler_company_interval_min: int = 1440
    # 傳給 stock-auto-research 的額外參數 (例如 "--no-brief" 省 LLM 成本)
    scheduler_research_args: str = ""
    # One-shot LLM report jobs. Kept off by default to avoid surprise token usage.
    scheduler_intraday_enabled: bool = False
    scheduler_intraday_time: datetime.time = datetime.time(8, 30)
    scheduler_intraday_end_time: datetime.time = datetime.time(14, 30)
    scheduler_nextday_draft_enabled: bool = False
    scheduler_nextday_draft_time: datetime.time = datetime.time(14, 0)
    scheduler_nextday_draft_end_time: datetime.time = datetime.time(18, 0)
    scheduler_nextday_update_enabled: bool = False
    scheduler_nextday_update_time: datetime.time = datetime.time(2, 0)
    scheduler_nextday_update_end_time: datetime.time = datetime.time(6, 0)
    # True=資料刷新只在台股交易時段 (含盤前盤後緩衝) 與平日執行
    scheduler_market_hours_only: bool = True
    scheduler_run_on_start: bool = True       # 啟動時先立刻跑一輪
    # 盤中自動托管 stock-bot 監測子行程 (開盤啟動、收盤停止)
    scheduler_supervise_monitor: bool = False
    scheduler_monitor_mode: Literal["watch", "report", "trade"] = "watch"
    # 只看不買：約 10 元熱度 + 熱門個股期貨快照 (分鐘)；<=0 停用
    scheduler_watch_snapshot_interval_min: int = 0

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
    # Google Sheets 自動同步間隔 (分鐘)；<=0 停用；由 stock-scheduler 呼叫 stock-cloud-sync
    scheduler_cloud_sync_interval_min: int = 0
    # 慢速補齊歷史日 K (TWSE/yfinance 月別)；<=0 停用；由 stock-scheduler 呼叫 stock-history-fetch
    scheduler_history_fetch_interval_min: int = 60
    scheduler_history_fetch_args: str = "--once --batch-size 5 --delay 5"
    # 僅在收盤後時段執行（台股 13:30 收盤，預設 14:00~23:00 每小時一批）
    scheduler_history_fetch_time: datetime.time = datetime.time(14, 0)
    scheduler_history_fetch_end_time: datetime.time = datetime.time(23, 0)

    # --- 事件鏈 (Pub/Sub 下游自動觸發) ---
    # true：QuantDataFetchCompleted 後自動跑微笑曲線選股
    event_chain_smile_screen: bool = False

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

    def effective_fund_cap(self) -> int:
        """實際可用資金上限：daily_fund_budget 優先，否則 max_fund。"""
        if self.daily_fund_budget > 0:
            return self.daily_fund_budget
        return self.max_fund

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
