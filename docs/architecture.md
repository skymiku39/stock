# 系統架構技術文件

## 概述

Stock Bot 是一支支援自動交易、看盤訊號與公開延遲資料報表分析的台股策略機器人，核心採用 **Market Source / Broker / Strategy** 分層架構，讓行情來源、下單執行與策略邏輯彼此解耦。

## 執行模式

| 模式 | 行情來源 | 登入 Shioaji | 啟用 CA | 下單 | 主要輸出 |
|------|----------|:------------:|:-------:|:----:|----------|
| `trade` | Shioaji 即時行情 | 是 | 實單時是 | 是 | `data/trades_*.csv` |
| `watch` | Shioaji 即時行情 | 是 | 否 | 否 | `data/reports/signals_*.csv`、`report_*.csv` |
| `report` | TWSE 公開延遲資料 | 否 | 否 | 否 | `data/reports/signals_*.csv`、`report_*.csv` |

`SIMULATION=true` 是 Shioaji 的模擬交易環境，只影響 `trade` 模式；如果目標是「只看盤不下單」，請使用 `RUN_MODE=watch` 或 `RUN_MODE=report`。

## 架構圖

```
                          ┌─────────────────────────────────────┐
                          │            main.py                  │
                          │  載入 Settings → 依模式建立元件 →   │
                          │  建立 Strategy → strategy.run()     │
                          └──────────┬──────────────────────────┘
                                     │
                 ┌───────────────────┼───────────────────┐
                 │                   │                   │
          ┌──────▼──────┐    ┌───────▼───────┐   ┌──────▼──────┐
          │  config.py  │    │  broker.py /  │   │ strategy*.py│
          │  Settings   │    │ market_source │   │ BaseStrategy│
          │ (pydantic)  │    │ (Shioaji SDK) │   │ + STRATEGY  │
          └──────┬──────┘    └───────┬───────┘   └──────┬──────┘
                 │                   │                   │
                 │            ┌──────┴──────┐    ┌──────┴──────┐
                 │            │ Shioaji/TWSE│    │ recorder.py │
                 │            │ data source │    │ notifier.py │
                 │            └─────────────┘    │ models.py   │
                 │                               └─────────────┘
           ┌─────▼─────┐
           │   .env     │
           └───────────┘
```

## 模組職責

### main.py -- 程式進入點

負責組裝所有元件並啟動系統的生命週期：

1. 載入 `Settings`（從 `.env`）
2. `trade/watch` 建立 `SjBroker` 並登入；`report` 建立 `TwsePublicMarketSource`
3. 依 `STRATEGY_TYPE` 建立策略（預設 `ConfigurableStrategy`；可選 `EtfFollowStrategy`）並執行 `run()`
4. 收到中斷信號或策略結束後，依模式匯出交易紀錄或訊號報表，必要時登出 Shioaji

### config.py -- 組態管理

使用 `pydantic-settings` 從 `.env` 檔案讀取所有設定值：

| 分類 | 設定項 | 類型 | 說明 |
|------|--------|------|------|
| 模式 | `run_mode` | str | `trade` / `watch` / `report` |
| 行情 | `market_source` | str | 留空自動依模式選擇 |
| 報表 | `report_poll_seconds`, `report_output_dir` | int / str | 公開資料輪詢秒數與輸出目錄 |
| 登入 | `api_key`, `secret_key` | str | Shioaji API 憑證 |
| 憑證 | `ca_path`, `ca_password`, `person_id` | str | 電子憑證路徑與密碼 |
| 模式 | `simulation` | bool | True = 模擬環境 |
| 標的 | `symbols` | List[str] | 逗號分隔的股票代碼 |
| 時間 | `enter_cutoff_time`, `exit_time` | time | 進場截止 / 全出場時間 |
| 風控 | `stop_loss_pct`, `take_profit_pct`, `trailing_stop_pct` | float | 停損 / 移動停利啟動門檻 / 淨利回撤出場 |
| 資金 | `max_fund`, `max_lot_per_symbol` | int | 總資金上限 / 每檔張數 |
| 通知 | `telegram_bot_token`, `telegram_chat_id` | str | Telegram 推播 |

### broker.py -- SjBroker 連線管理層

封裝 Shioaji SDK 的所有低階操作，對外提供簡潔介面：

**登入/登出**
- `login()` -- 初始化 Shioaji、登入帳號、依模式決定是否啟用憑證、綁定回呼
- `logout()` -- 安全登出並釋放資源

**合約**
- `get_contract(symbol)` -- 取得合約物件並快取

**行情**
- `subscribe_tick(symbol)` -- 訂閱 Tick 行情
- `subscribe_bidask(symbol)` -- 訂閱五檔報價
- `unsubscribe(symbol)` -- 取消訂閱
- `get_snapshots(symbols)` -- 取得前日收盤價（reference）

**下單**
- `place_order(...)` -- 通用下單（限價/市價、ROD/IOC/FOK）
- `place_market_sell(...)` -- 市價 IOC 賣出（停損/全出場用）
- 非 `trade` 模式會在 broker 層攔截下單，回傳 `None`

### market_source.py -- TWSE 公開延遲行情來源

- `TwsePublicMarketSource` -- 不登入 Shioaji，輪詢 TWSE 公開延遲報價
- `get_prev_close(symbols)` -- 從公開資料取得前日收盤價
- `poll()` -- 將公開報價正規化為 `MarketTick`

公開資料並非完整歷史逐筆行情，而是以輪詢形成 tick-like 序列，適合初步報表分析。

**事件**
- `set_on_tick(callback)` -- 設定 Tick 回呼
- `set_on_bidask(callback)` -- 設定 BidAsk 回呼
- `set_on_order(callback)` -- 設定委託/成交回呼

**斷線重連**
- `_handle_reconnect()` -- 指數退避重連（最多 10 次，5s→120s）

### events/ + protocols/ -- 領域事件 Pub/Sub（SOLID）

專案採 **Publish/Subscribe** 作為跨模組通訊主軸，搭配 **protocol 介面** 落實依賴反轉 (DIP)。

#### 事件匯流排 (`src/bot/events/`)

| 模組 | 職責 (SRP) |
|------|------------|
| `types.py` | 不可變領域事件（資料、交易、管線） |
| `protocols.py` | `EventPublisher` / `EventSubscriber` 介面 (ISP, DIP) |
| `bus.py` | `InMemoryEventBus` — 僅負責路由 |
| `handlers.py` | 橫切 handler（`LoggingEventHandler`, `JsonlEventRecorder`） |
| `trading_handlers.py` | 交易側效應（通知 / 紀錄 / 風控） |
| `pipeline_helpers.py` | 管線步驟/完成事件發布輔助 |
| `wiring.py` | `wire_trading_handlers`, `wire_application_handlers` |

#### 依賴反轉介面 (`src/bot/protocols/`)

| Protocol | 實作 |
|----------|------|
| `BrokerProtocol` | `SjBroker` |
| `MarketSourceProtocol` | `TwsePublicMarketSource` |
| `NotifierProtocol` | `TelegramNotifier` |
| `TradeRecorderProtocol` | `TradeRecorder` |

#### 應用組裝 (`app_bootstrap.py`)

所有 CLI 與 `stock-bot` 透過 `bootstrap_event_bus()` / `get_or_create_bus()` 取得同一匯流排，並自動掛載 JSONL 稽核。

#### 已發布事件

| 領域 | 事件 | 發布者 |
|------|------|--------|
| 資料 | `DailyKlineFetched`, `QuantDataFetchCompleted` | `watch_data_fetch` |
| 選股 | `SmileScreenCompleted`, `SmileAuditCompleted` | smile CLI |
| 交易 | `BotStarted`, `TickReceived`, `TradeBuyFilled`, `TradeSellFilled`, `RiskEntryBlocked`, `ClosureCompleted`, `BotShutdownRequested` | `BaseStrategy`, `main` |
| 管線 | `PipelineStepCompleted`, `PipelineCompleted` | `data_pipeline`, `intraday_pipeline`, `next_day_watch_pipeline` |
| 排程 | `SchedulerStarted`, `SchedulerJobCompleted` | `scheduler` |

#### 訂閱者（handler）

- `NotificationHandler` → Telegram 推播
- `TradeRecordingHandler` → CSV 成交紀錄
- `RiskPostTradeHandler` → 成交後資金/部位狀態
- `LoggingEventHandler` / `JsonlEventRecorder` → 稽核日誌

**事件鏈 (可選)**：`.env` 設 `EVENT_CHAIN_SMILE_SCREEN=true` 時，`QuantDataFetchCompleted` 自動觸發微笑曲線選股（`events/chain_handlers.py`）。

**擴充方式 (Open/Closed)**：新增事件型別 + `bus.subscribe(EventType, handler)`，無需修改 bus 或策略核心。

**策略層行情**：Shioaji callback → Queue（高頻執行緒安全）→ `on_tick()`，同時發布 `TickReceived` 供儀表板/監控訂閱。

### strategy.py -- 策略引擎

分為兩層：

**BaseStrategy (抽象基底)**

提供所有策略共用的基礎設施：
- **Queue 解耦**: Tick 回呼 → Queue → 獨立消費者 Thread（與 `events/` Pub/Sub 互補）
- **部位管理**: `positions` dict，成交回報自動更新均價/數量
- **AI 部位標籤**: 買單使用 `AIBUY`，成交後部位標記 `owner_tag=AI`
- **委託追蹤**: `pending_orders` 防止重複下單
- **資金追蹤**: `_fund_used` 即時追蹤已投入金額
- **收盤出場**: 獨立 Thread 在 `exit_time` 後市價清倉
- **委託輪詢**: 獨立 Thread 定期清理已完成的 pending orders
- **虛擬成交**: `watch/report` 模式以觀察價記錄 would-buy / would-sell，永不送單

**策略實作（依 `STRATEGY_TYPE`）**

| 類型 | 類別 | 進場特色 |
|------|------|----------|
| `configurable`（預設） | `ConfigurableStrategy` | env 漲幅區間 + LLM 進場閘門 + 回落買回 |
| `etf_follow` | `EtfFollowStrategy` | ETF 共識訊號 + 漲幅區間 |

**出場規則（BaseStrategy 共用，皆以淨利 % 判斷）**：
- 使用者目標賣出：若 `SELL_PROFIT_TARGETS` 有該檔門檻，須達標才允許自動賣出
- 停損：淨利 PnL <= `stop_loss_pct`
- 移動停利：淨利 PnL 先達 `take_profit_pct`（啟動門檻），再從淨利高點回撤 >= `trailing_stop_pct`
- 全出場：`exit_time` 後由 BaseStrategy 自動處理

詳見 [trading-rules.md](trading-rules.md)、[glossary.md](glossary.md)。

### models.py -- 資料模型

- `PositionInfo` -- 持倉資訊（股號、均價、數量、進場時間）
- `owner_tag=AI` -- 標記此部位由本工具買進，所有自動賣出會先檢查此標籤
- `OrderRecord` -- 委託紀錄（委託號、股號、方向、類型）
- `MarketTick` -- 跨來源正規化行情
- `SignalEvent` -- 看盤/報表模式的交易意圖訊號（`would-buy` / `would-sell` / `sell-blocked`）

### recorder.py -- 交易紀錄

- `record_deal(msg)` -- 暫存成交回報
- `export_csv()` -- 匯出 CSV 至 `data/trades_YYYY-MM-DD.csv`，包含 `custom_field` 與 `owner_tag`
- `summary()` -- 產生統計摘要（筆數、買賣金額）

### signal_recorder.py -- 訊號與分析報表

- `record(event)` -- 記錄 would-buy / would-sell
- `record_tick(tick)` -- 追蹤觀察到的最高/最低價
- `export_signals_csv()` -- 匯出訊號明細
- `export_report()` -- 匯出每檔訊號次數、進出場價與分析損益

### notifier.py -- Telegram 通知

- 背景 Thread 發送，不阻塞主流程
- 通知時機：啟動、買進、賣出、收盤摘要、停機、異常

### utils.py -- 工具函數

- `now_tw()` / `now_tw_time()` -- 台灣時區時間
- `get_logger(name)` -- 建立 Logger（同時輸出到檔案與終端）
- `mk_folder(path)` -- 建立資料夾

## 執行緒模型

```
主執行緒 (main)
  │
  ├── strategy.run()  ← 阻塞等待
  │
  ├── [daemon] tick-consumer     ← 從 Queue 取 Tick 送入 on_tick()
  ├── [daemon] order-updater     ← 輪詢委託狀態
  ├── [daemon] closure           ← 收盤全出場
  │
  └── [Shioaji 內建]
       ├── Tick 回呼 Thread       ← 將 Tick 推入 Queue
       └── Order 回呼 Thread      ← 成交/委託回報
```

**為什麼用 Queue 解耦？**

Shioaji 的 Tick 回呼在 SDK 內部的執行緒上運行。如果在回呼中直接執行策略運算（可能耗時），會阻塞後續 Tick 的接收。Queue 解耦確保 Tick 回呼永遠在 < 1ms 內完成。

## 資料流

```
Shioaji Server
    │
    ├── Tick 行情 ──→ _enqueue_tick() ──→ Queue ──→ _tick_consumer() ──→ on_tick()
    │                                                                      │
    │                                                              ┌───────┴───────┐
    │                                                              │ 進場/出場判斷  │
    │                                                              └───────┬───────┘
    │                                                                      │
    │                                                          _place_buy() / _place_stop_sell()
    │                                                                      │
    │                                                              broker.place_order()
    │                                                                      │
    ├── 委託回報 ──→ _on_order_callback() ──→ _handle_order_event()        │
    │                                                                      │
    └── 成交回報 ──→ _on_order_callback() ──→ _handle_deal()              │
                                                  │                        │
                                          ┌───────┴───────┐               │
                                          │ 更新 positions │               │
                                          │ 更新 _fund_used│               │
                                          │ recorder.record│               │
                                          │ notifier.send  │               │
                                          └───────────────┘               │
                                                                Shioaji Server
```

## 依賴關係

```
shioaji          1.3.3   永豐金交易 API
pydantic-settings 2.x    組態管理
python-dotenv    1.x     .env 讀取
pandas           3.x     資料匯出
requests         2.x     Telegram API (shioaji 已附帶)
```

## LLM 架構（雙閘道容錯鏈）

### 設計原則

- **不依賴付費 API Key**：優先使用本機閘道（Gemini 瀏覽器閘道、Cursor CLI 閘道）
- **依賴反轉**：所有管線依賴 `protocols/llm.LlmClient` 協定
- **容錯鏈**：`ChainedLlmClient` 依序嘗試多個後端，第一個成功即回傳

### 架構圖

```
LLM_PROVIDER=chain (預設)

create_llm_client(settings)
        │
        ▼
┌─── ChainedLlmClient ───┐
│                         │
│  1. GeminiGatewayClient │ ─── HTTP ──→ 蹭google的geminiAI (port 8816)
│  2. CursorGatewayClient │ ─── HTTP ──→ 蹭cursor的AI (port 8815)
│  3. GeminiClient (SDK)  │ ─── API ──→ Google Gemini (需 API Key)
│                         │
└─────────────────────────┘
        │
        ▼ (text, metadata)
```

### 相關模組

| 模組 | 職責 |
|------|------|
| `protocols/llm.py` | `LlmClient` Protocol |
| `gemini_gateway.py` | Gemini 瀏覽器閘道 HTTP 客戶端 |
| `cursor_llm.py` | Cursor CLI 閘道 HTTP 客戶端 |
| `chained_llm.py` | 容錯鏈（依序嘗試多個後端） |
| `llm_analyzer.py` | 工廠 `create_llm_client()` + 統一 `llm_call()` + Gemini SDK |
| `llm_smoke.py` | 統一連線煙霧測試 CLI |
| `prompt_registry.py` | Prompt YAML 載入/渲染 |
| `llm_log.py` | 呼叫 JSONL 稽核日誌 |

### 啟動前提

```powershell
# 1. 啟動 Gemini 閘道（需已登入 Google 帳號）
cd D:\skymiku\蹭google的geminiAI && uv run gemini-gateway

# 2. 啟動 Cursor 閘道（需 Cursor 訂閱）
cd D:\skymiku\蹭cursor的AI && uv run cursor-gateway

# 3. 或用一鍵腳本
.\scripts\start_full_auto.ps1
```

### 自動化排程

`stock-scheduler` 常駐背景，定時執行所有研究/分析管線。
所有 LLM 呼叫自動走容錯鏈，無需手動切換後端。

Windows Task Scheduler 設定：
```powershell
.\scripts\setup_win_scheduler.ps1     # 安裝
.\scripts\setup_win_scheduler.ps1 -Remove  # 移除
```
