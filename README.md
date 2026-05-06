# Stock Bot - 台股當沖自動交易機器人

基於 **Shioaji (永豐金證券)** API 的台股當沖自動交易機器人。

> **Disclaimer**: 本專案僅供教學與參考之用，實務交易應自行評估並承擔相關風險。

## 功能特色

- **Shioaji 原生整合** -- 使用 Decorator-based callback，Pythonic 風格
- **Queue 解耦架構** -- Tick 行情與策略運算分離，避免阻塞回呼執行緒
- **部位管理** -- 即時追蹤持倉均價、數量，成交回報自動更新
- **委託單追蹤** -- 防止重複下單，自動輪詢委託狀態
- **收盤全出場** -- 指定時間自動市價清倉
- **模擬模式** -- `SIMULATION=true` 即可使用模擬環境測試

## 前置作業

1. **永豐金開戶** 並申請 API 權限
2. 取得 **API Key / Secret Key**
3. 下載並安裝 **電子憑證 (.pfx)**

詳細步驟請參考 [Shioaji 官方文檔](https://sinotrade.github.io/)。

## 安裝

```bash
# 安裝 uv (如尚未安裝)
pip install uv

# 克隆專案後安裝依賴
cd stock
uv sync
```

## 設定

複製 `.env.example` 為 `.env`，填入你的實際資訊：

```bash
cp .env.example .env
```

主要設定項目：

| 變數 | 說明 | 預設值 |
|------|------|--------|
| `API_KEY` | Shioaji API Key | (必填) |
| `SECRET_KEY` | Shioaji Secret Key | (必填) |
| `CA_PATH` | 電子憑證路徑 (.pfx) | (下單必填) |
| `CA_PASSWORD` | 憑證密碼 | (下單必填) |
| `PERSON_ID` | 身分證字號 | (下單必填) |
| `SIMULATION` | 模擬模式 | `true` |
| `SYMBOLS` | 監控股票 (逗號分隔) | `2330,0050` |
| `ENTER_CUTOFF_TIME` | 停止進場時間 | `09:30` |
| `EXIT_TIME` | 全部出場時間 | `13:15` |
| `STOP_LOSS_PCT` | 停損百分比 | `-3.0` |
| `TAKE_PROFIT_PCT` | 停利百分比 | `6.0` |
| `MAX_FUND` | 總資金上限 | `500000` |
| `MAX_LOT_PER_SYMBOL` | 每檔最大張數 | `2` |

## 使用方式

```bash
# 透過 uv 執行
uv run stock-bot

# 或直接用 Python
uv run python -m bot.main
```

## 專案結構

```
src/bot/
  main.py       # 程式進入點
  config.py     # 組態管理 (pydantic-settings + .env)
  broker.py     # Shioaji 連線管理 (登入/行情/下單)
  strategy.py   # 策略引擎 (BaseStrategy + MyStrategy)
  models.py     # 資料模型 (PositionInfo, OrderRecord)
  utils.py      # 工具函數 (Logger, 時間)
```

## 自訂策略

繼承 `BaseStrategy` 並實作 `on_tick` 方法：

```python
from bot.strategy import BaseStrategy
from shioaji import Exchange, TickSTKv1

class MyCustomStrategy(BaseStrategy):
    def on_tick(self, exchange: Exchange, tick: TickSTKv1) -> None:
        symbol = tick.code
        price = float(tick.close)

        # 你的策略邏輯 ...

        if should_buy:
            self._place_buy(symbol, price, quantity=1)

        if should_sell and symbol in self.positions:
            self._place_stop_sell(symbol, self.positions[symbol].quantity)
```

然後在 `main.py` 中替換 `MyStrategy` 為你的策略類別即可。

## 參考

- [Shioaji 官方文檔](https://sinotrade.github.io/)
- [StrategyExecutor_feather](https://github.com/phenomenoner/StrategyExecutor_feather) (架構參考)
