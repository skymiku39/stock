# 自動買賣規則說明

## 策略概述

MyStrategy 是一支**台股現股當沖**策略，核心邏輯為：

> 開盤後尋找「溫和上漲」的標的買進，盤中透過移動停利鎖定利潤、固定百分比停損控制風險，收盤前強制清倉確保不留隔夜部位。

## 時間軸

```
08:30          09:00         09:30                   13:15     13:30
  │              │             │                       │         │
  ├──盤前準備──┤├──進場窗口──┤├────持倉監控期────────┤├──收盤──┤
  │              │             │                       │         │
  取得前日收盤    開盤           停止進場                全出場    收盤
  訂閱行情                     (ENTER_CUTOFF_TIME)    (EXIT_TIME)
```

## 一、進場規則

### 觸發條件

所有條件必須**同時成立**：

| # | 條件 | 程式碼對應 | 說明 |
|---|------|-----------|------|
| 1 | 時間 < `ENTER_CUTOFF_TIME` | `cur_time < settings.enter_cutoff_time` | 預設 09:30 前 |
| 2 | 該股尚未進場過 | `symbol not in _enter_placed` | 每檔每天只進場一次 |
| 3 | 沒有待成交委託 | `not _has_pending(symbol)` | 防止重複下單 |
| 4 | 目前無持倉 | `symbol not in positions` | 不加碼 |
| 5 | 漲幅 1% ~ 5% | `1.0 < pct_chg < 5.0` | 溫和上漲區間 |
| 6 | 剩餘資金足夠 | `_calc_lots(price) > 0` | 不超過 MAX_FUND |

### 漲幅計算

```
漲幅(%) = (當前價 - 前日收盤) / 前日收盤 * 100
```

前日收盤來源優先順序：
1. 合約物件的 `reference` 屬性（最精確）
2. Snapshots API 的 `close - change_price`（次之）
3. Tick 的 `pct_chg` 反推（fallback）

### 下單方式

- **買賣方向**: 買進 (Buy)
- **價格類型**: 限價 (LMT)
- **委託條件**: ROD（當日有效）
- **張數**: `min(剩餘資金可買張數, MAX_LOT_PER_SYMBOL)`

### 張數計算邏輯

```
每張成本 = 當前股價 * 1000
剩餘資金 = MAX_FUND - 已投入金額
可買張數 = min(剩餘資金 / 每張成本, MAX_LOT_PER_SYMBOL)
```

**範例**: 股價 600 元，MAX_FUND = 500,000，已用 0：
- 每張成本 = 600,000
- 可買 = 0 張（資金不足）

**範例**: 股價 50 元，MAX_FUND = 500,000，已用 100,000：
- 剩餘 = 400,000
- 每張 = 50,000
- 可買 = min(8, MAX_LOT_PER_SYMBOL) = 2 張

## 二、出場規則

### 2.1 固定停損

| 項目 | 值 |
|------|-----|
| 觸發條件 | PnL <= `STOP_LOSS_PCT` (預設 -3%) |
| 下單方式 | 市價 IOC 賣出 |
| 備註標記 | `sl` |

```
PnL(%) = (當前價 - 持倉均價) / 持倉均價 * 100
```

**範例**: 均價 100 元，當前價 96.5 元 → PnL = -3.5% → 觸發停損

### 2.2 移動停利 (Trailing Stop)

移動停利分兩個階段：

**階段 1 -- 獲利門檻**
PnL 必須先達到 `TAKE_PROFIT_PCT`（預設 6%），表示「已經有足夠的浮動利潤」。

**階段 2 -- 回撤出場**
達到門檻後，系統開始追蹤持倉期間的最高價（high watermark）。當價格從最高點回撤超過 `TRAILING_STOP_PCT`（預設 2%），即觸發出場。

觸發條件（兩者必須同時成立）：

| 條件 | 公式 |
|------|------|
| PnL >= 門檻 | `(當前價 - 均價) / 均價 * 100 >= TAKE_PROFIT_PCT` |
| 回撤 >= 閾值 | `(最高價 - 當前價) / 最高價 * 100 >= TRAILING_STOP_PCT` |

**範例**:
- 均價 100 元，最高曾到 110 元，當前 107 元
- PnL = 7% (>= 6% 門檻) -- 通過
- 回撤 = (110 - 107) / 110 * 100 = 2.73% (>= 2% 閾值) -- 通過
- 觸發移動停利

**對比固定停利的優勢**:
- 固定停利：漲到 6% 就出場，錯過後續漲幅
- 移動停利：漲到 6% 後繼續追蹤，漲到 15% 再回撤 2% (=14.7%) 才出場

### 2.3 收盤全出場

| 項目 | 值 |
|------|-----|
| 觸發條件 | 當前時間 >= `EXIT_TIME` (預設 13:15) |
| 下單方式 | 市價 IOC 賣出 |
| 備註標記 | `close` |
| 執行方式 | 獨立背景 Thread，每秒檢查一次 |

此機制確保：
- 當日所有部位在收盤前清空
- 不留隔夜部位（當沖策略的核心原則）
- 等待該檔的 pending orders 完成後才送出清倉單

## 三、防護機制

### 3.1 防重複下單

每個 symbol 都有 `pending_orders` 清單。只要有待成交的委託，就不會送出新的委託。

### 3.2 每檔只進場一次

`_enter_placed` 集合記錄已經進場過的 symbol。即使停損出場，當天也不會再次進場同一支股票。

### 3.3 資金上限

`_fund_used` 即時追蹤已投入金額。買入時累加、賣出時扣減。`_calc_lots()` 根據剩餘額度計算可買張數，確保不超過 `MAX_FUND`。

### 3.4 委託狀態輪詢

獨立 Thread 每 3 秒透過 `api.update_status()` + `api.list_trades()` 清理已完成（Filled/Cancelled/Failed）的 pending orders，避免委託「卡住」。

## 四、設定參數一覽

| 參數 | 預設值 | 影響 |
|------|--------|------|
| `ENTER_CUTOFF_TIME` | 09:30 | 進場窗口結束時間 |
| `EXIT_TIME` | 13:15 | 全出場時間 |
| `STOP_LOSS_PCT` | -3.0 | 固定停損百分比 |
| `TAKE_PROFIT_PCT` | 6.0 | 移動停利啟動門檻 |
| `TRAILING_STOP_PCT` | 2.0 | 從高點回撤多少出場 |
| `MAX_FUND` | 500,000 | 總可用資金 |
| `MAX_LOT_PER_SYMBOL` | 2 | 每檔最大張數 |
| `SYMBOLS` | 2330,0050 | 監控清單 |

## 五、自訂策略

繼承 `BaseStrategy` 並實作 `on_tick()` 即可替換買賣邏輯：

```python
from bot.strategy import BaseStrategy
from shioaji import Exchange, TickSTKv1

class MyCustomStrategy(BaseStrategy):
    def on_tick(self, exchange: Exchange, tick: TickSTKv1) -> None:
        # 你的策略邏輯 ...
        pass
```

BaseStrategy 提供的部位管理、防重複下單、收盤出場、交易紀錄、通知等功能都會自動繼承。
