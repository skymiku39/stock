# Stock Bot - 台股當沖自動交易機器人

基於 **Shioaji (永豐金證券)** API 的台股當沖自動交易機器人。

> **Disclaimer**: 本專案僅供教學與參考之用，實務交易應自行評估並承擔相關風險。

## 功能特色

- **三種執行模式** -- `trade` 自動交易 / `watch` 看盤不下單 / `report` 純報表分析
- **Shioaji 原生整合** -- 使用 Decorator-based callback，Pythonic 風格
- **Queue 解耦架構** -- Tick 行情與策略運算分離，避免阻塞回呼執行緒
- **盤前收盤價載入** -- 透過 snapshots API 取得精確的前日收盤價
- **多檔資金追蹤** -- 即時追蹤已用資金，避免超額下單
- **移動停利** -- 追蹤持倉最高價，從高點回撤 N% 觸發出場
- **部位管理** -- 即時追蹤持倉均價、數量，成交回報自動更新
- **委託單追蹤** -- 防止重複下單，自動輪詢委託狀態
- **斷線重連** -- 指數退避重試，自動恢復登入與行情訂閱
- **收盤全出場** -- 指定時間自動市價清倉
- **交易紀錄匯出** -- 收盤後自動匯出 CSV 至 `data/` 目錄
- **訊號與報表匯出** -- watch/report 模式匯出 would-buy/sell 訊號及分析損益
- **Telegram 通知** -- 買賣/停損停利/收盤摘要即時推播到手機
- **模擬模式** -- `SIMULATION=true` 即可使用模擬環境測試

## 三種模式

| 模式 | 行情來源 | 需要 API Key | 啟用 CA | 下單 | 輸出 |
|------|---------|:------------:|:-------:|:----:|------|
| `trade` | Shioaji 即時行情 | ✅ | ✅ (實單) | ✅ | `data/trades_*.csv` |
| `watch` | Shioaji 即時行情 | ✅ | ❌ | ❌ | `data/reports/signals_*.csv` + `report_*.csv` |
| `report` | TWSE 公開延遲資料 | ❌ | ❌ | ❌ | `data/reports/signals_*.csv` + `report_*.csv` |

### trade — 自動交易 (預設)

- 完整的 Shioaji 登入流程，需要 API Key + Secret Key
- 實單模式需啟用電子憑證 (CA)
- 策略觸發進出場條件時真正送出委託
- `SIMULATION=true` 時使用 Shioaji 模擬環境

### watch — 看盤模式

- 使用 Shioaji 行情 API 接收即時 Tick 資料
- **不啟用 CA、不會送出任何委託**
- 觸發進出場條件時記錄 would-buy / would-sell 訊號
- 虛擬部位會追蹤停損、移動停利與收盤出場
- 適合先觀察策略表現，確認邏輯再切換到 trade 模式

### report — 報表分析模式

- **不需要 Shioaji API Key**，不登入券商
- 從 TWSE 公開資訊觀測站輪詢延遲報價 (延遲 ≥ 20 分鐘)
- 以「輪詢公開報價形成 tick-like 序列」分析策略表現
- 結束時輸出訊號明細與分析報表
- 適合無券商帳號或不想登入時的初步策略驗證

> **公開延遲資料限制**: TWSE 公開資訊延遲 20 分鐘以上，report 模式的損益為分析用虛擬成交，不代表實際可成交價格。完整歷史逐筆行情通常需要資料供應商。參考 [TWSE Information Services](https://wwwc.twse.com.tw/en/products/information/information.html)。

## 前置作業

1. **永豐金開戶** 並申請 API 權限 (trade/watch 模式)
2. 取得 **API Key / Secret Key**
3. 下載並安裝 **電子憑證 (.pfx)** (trade 模式實單)

> report 模式不需以上步驟，只要安裝好程式即可。

詳細步驟請參考 [Shioaji 官方文檔](https://sinotrade.github.io/)。

## 安裝

```bash
# 安裝 uv (如尚未安裝)
pip install uv

# 克隆專案後安裝依賴
cd stock
uv sync

# (選用) 安裝開發依賴 (pytest 等)
uv sync --extra dev
```

## 設定

複製 `.env.example` 為 `.env`，填入你的實際資訊：

```bash
cp .env.example .env
```

主要設定項目：

| 變數 | 說明 | 預設值 |
|------|------|--------|
| `RUN_MODE` | 執行模式 (`trade` / `watch` / `report`) | `trade` |
| `MARKET_SOURCE` | 行情來源 (留空自動判斷) | (自動) |
| `REPORT_POLL_SECONDS` | report 模式輪詢間隔 (秒) | `5` |
| `REPORT_OUTPUT_DIR` | 報表輸出目錄 | `data/reports` |
| `API_KEY` | Shioaji API Key | (trade/watch 必填) |
| `SECRET_KEY` | Shioaji Secret Key | (trade/watch 必填) |
| `CA_PATH` | 電子憑證路徑 (.pfx) | (trade 實單必填) |
| `CA_PASSWORD` | 憑證密碼 | (trade 實單必填) |
| `PERSON_ID` | 身分證字號 | (trade 實單必填) |
| `SIMULATION` | 模擬模式 | `true` |
| `SYMBOLS` | 監控股票 (逗號分隔) | `2330,0050` |
| `ENTER_CUTOFF_TIME` | 停止進場時間 | `09:30` |
| `EXIT_TIME` | 全部出場時間 | `13:15` |
| `STOP_LOSS_PCT` | 停損百分比 | `-3.0` |
| `TAKE_PROFIT_PCT` | 停利門檻百分比 | `6.0` |
| `TRAILING_STOP_PCT` | 移動停利回撤百分比 | `2.0` |
| `MAX_FUND` | 總資金上限 | `500000` |
| `MAX_LOT_PER_SYMBOL` | 每檔最大張數 | `2` |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token | (選填) |
| `TELEGRAM_CHAT_ID` | Telegram Chat ID | (選填) |

## 使用方式

```bash
# 自動交易模式 (預設)
uv run stock-bot

# 看盤模式 (需要 .env 中的 API Key)
RUN_MODE=watch uv run stock-bot

# 報表模式 (不需要 API Key)
RUN_MODE=report SYMBOLS=2330,0050 uv run stock-bot
```

PowerShell 使用者：

```powershell
# 報表模式
$env:RUN_MODE="report"; $env:SYMBOLS="2330,0050"; uv run stock-bot
```

## 專案結構

```
src/bot/
  main.py            # 程式進入點 (多模式分流)
  config.py          # 組態管理 (pydantic-settings + .env)
  broker.py          # Shioaji 連線管理 (登入/行情/下單/斷線重連)
  strategy.py        # 策略引擎 (BaseStrategy + MyStrategy)
  models.py          # 資料模型 (PositionInfo, MarketTick, SignalEvent)
  market_source.py   # TWSE 公開延遲行情來源
  signal_recorder.py # 訊號記錄與報表匯出 (watch/report)
  recorder.py        # 交易紀錄收集與 CSV 匯出 (trade)
  notifier.py        # Telegram 推播通知
  utils.py           # 工具函數 (Logger, 時間)
tests/
  test_config.py     # Settings 解析測試
  test_strategy.py   # 策略模式測試
  test_market_source.py  # TWSE 資料解析測試
```

## 自訂策略

繼承 `BaseStrategy` 並實作 `on_tick` 方法：

```python
from bot.strategy import BaseStrategy
from bot.models import MarketTick

class MyCustomStrategy(BaseStrategy):
    def on_tick(self, tick: MarketTick) -> None:
        symbol = tick.symbol
        price = tick.price

        # 你的策略邏輯 ...

        if should_buy:
            self._place_buy(symbol, price, quantity=1)

        if should_sell and symbol in self.positions:
            self._place_stop_sell(symbol, self.positions[symbol].quantity)
```

然後在 `main.py` 中替換 `MyStrategy` 為你的策略類別即可。
三種模式皆可使用，watch/report 模式會自動以虛擬成交記錄訊號。

## 參考

- [Shioaji 官方文檔](https://sinotrade.github.io/)
- [TWSE 公開資訊觀測站](https://wwwc.twse.com.tw/)
- [StrategyExecutor_feather](https://github.com/phenomenoner/StrategyExecutor_feather) (架構參考)
