# 系統架構技術文件

## 概述

Stock Bot 是一支基於 Shioaji (永豐金證券) API 的台股當沖自動交易機器人，採用 **Broker-Strategy 雙層架構**，將券商 SDK 操作與策略邏輯完全解耦。

## 架構圖

```
                          ┌─────────────────────────────────────┐
                          │            main.py                  │
                          │  載入 Settings → 建立 Broker →      │
                          │  建立 Strategy → strategy.run()     │
                          └──────────┬──────────────────────────┘
                                     │
                 ┌───────────────────┼───────────────────┐
                 │                   │                   │
          ┌──────▼──────┐    ┌───────▼───────┐   ┌──────▼──────┐
          │  config.py  │    │  broker.py    │   │ strategy.py │
          │  Settings   │    │  SjBroker     │   │ BaseStrategy│
          │ (pydantic)  │    │ (Shioaji SDK) │   │ MyStrategy  │
          └──────┬──────┘    └───────┬───────┘   └──────┬──────┘
                 │                   │                   │
                 │            ┌──────┴──────┐    ┌──────┴──────┐
                 │            │  Shioaji    │    │ recorder.py │
                 │            │  API Server │    │ notifier.py │
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
2. 建立 `SjBroker` 並登入
3. 建立 `MyStrategy` 並執行 `run()`
4. 收到中斷信號或策略結束後，匯出紀錄、發送通知、登出

### config.py -- 組態管理

使用 `pydantic-settings` 從 `.env` 檔案讀取所有設定值：

| 分類 | 設定項 | 類型 | 說明 |
|------|--------|------|------|
| 登入 | `api_key`, `secret_key` | str | Shioaji API 憑證 |
| 憑證 | `ca_path`, `ca_password`, `person_id` | str | 電子憑證路徑與密碼 |
| 模式 | `simulation` | bool | True = 模擬環境 |
| 標的 | `symbols` | List[str] | 逗號分隔的股票代碼 |
| 時間 | `enter_cutoff_time`, `exit_time` | time | 進場截止 / 全出場時間 |
| 風控 | `stop_loss_pct`, `take_profit_pct`, `trailing_stop_pct` | float | 停損/停利/移動停利 |
| 資金 | `max_fund`, `max_lot_per_symbol` | int | 總資金上限 / 每檔張數 |
| 通知 | `telegram_bot_token`, `telegram_chat_id` | str | Telegram 推播 |

### broker.py -- SjBroker 連線管理層

封裝 Shioaji SDK 的所有低階操作，對外提供簡潔介面：

**登入/登出**
- `login()` -- 初始化 Shioaji、登入帳號、啟用憑證、綁定回呼
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

**事件**
- `set_on_tick(callback)` -- 設定 Tick 回呼
- `set_on_bidask(callback)` -- 設定 BidAsk 回呼
- `set_on_order(callback)` -- 設定委託/成交回呼

**斷線重連**
- `_handle_reconnect()` -- 指數退避重連（最多 10 次，5s→120s）

### strategy.py -- 策略引擎

分為兩層：

**BaseStrategy (抽象基底)**

提供所有策略共用的基礎設施：
- **Queue 解耦**: Tick 回呼 → Queue → 獨立消費者 Thread
- **部位管理**: `positions` dict，成交回報自動更新均價/數量
- **委託追蹤**: `pending_orders` 防止重複下單
- **資金追蹤**: `_fund_used` 即時追蹤已投入金額
- **收盤出場**: 獨立 Thread 在 `exit_time` 後市價清倉
- **委託輪詢**: 獨立 Thread 定期清理已完成的 pending orders

**MyStrategy (示範策略)**

繼承 BaseStrategy，實作 `on_tick()`：
- 進場：漲幅 1%~5%、時間 < enter_cutoff_time
- 停損：PnL <= stop_loss_pct
- 移動停利：PnL >= take_profit_pct 且從高點回撤 >= trailing_stop_pct
- 全出場：exit_time 後由 BaseStrategy 自動處理

### models.py -- 資料模型

- `PositionInfo` -- 持倉資訊（股號、均價、數量、進場時間）
- `OrderRecord` -- 委託紀錄（委託號、股號、方向、類型）

### recorder.py -- 交易紀錄

- `record_deal(msg)` -- 暫存成交回報
- `export_csv()` -- 匯出 CSV 至 `data/trades_YYYY-MM-DD.csv`
- `summary()` -- 產生統計摘要（筆數、買賣金額）

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
