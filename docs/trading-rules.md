# 自動買賣規則說明

## 策略類型總覽

`STRATEGY_TYPE` 決定實際執行的進場邏輯（預設 `configurable`）：

| 值 | 類別 | 進場特色 |
|----|------|----------|
| `configurable`（預設） | ConfigurableStrategy | env 驅動漲幅區間 + LLM 進場閘門 + 回落買回 |
| `etf_follow` | EtfFollowStrategy | ETF 共識訊號 + 漲幅 -1%~4% |
| `smile_curve` | SmileCurveStrategy | 跌幅分階買進、回到標準價、整個部位以平均成本扣費稅後有淨利才全部送限價賣單；目前僅支援模擬 |

> 舊值 `default` 會自動對應為 `configurable`（向後相容）。

**當沖出場規則**（停損、移動停利、收盤全出、AI 標籤授權）由 `BaseStrategy._evaluate_standard_exits()` 共用。微笑策略保留批次部位，不套用當沖收盤強平；出場採平均成本法：價格回到標準價時，整個部位（所有批次）扣手續費與證交稅後淨利 > 0 才一次全部賣出並重設標準價，否則一批都不賣（盤中與回測一致）。

## 模擬買賣驗證

不修改 `.env`、不解除正式交易封存的啟動方式：

```powershell
uv run stock-bot --simulate --strategy configurable
uv run stock-bot --simulate --strategy smile_curve
```

`--simulate` 強制本次使用 `trade + simulation=true`、零股、手動 `SYMBOLS` 清單；微笑策略每階基礎數量為 100 股，仍受本金、單檔與單筆上限約束。兩種指令都是持續監控行情的前景程序；不會修改設定或建立背景排程。微笑策略拒絕正式連線，普通股票與 0050 可用，其餘 ETF 尚未提供商品規則。股票與 0050 的升降單位、0050 賣出稅率依[證交所 ETF 交易規則](https://www.twse.com.tw/zh/products/securities/etf/overview/rules.html)區分。

模擬交易的風控、成交 CSV 與微笑批次狀態位於 `data/simulation/data/`，不混入正式成交紀錄。分階狀態以實際成交回報推進，部分成交保留掛單及剩餘資金保留額；同筆回報不重複入帳，重啟會對帳並恢復零股持倉。微笑賣單使用限價，但限價也可能未成交。

可重複執行的驗收指令：

```powershell
uv run python -m bot.simulation_check --backend local
uv run python -m bot.simulation_check --backend shioaji --timeout 60
```

`local` 不連網，替換撮合端並保留真正的策略、SjBroker 委託建構、SDK Order/Trade、成交事件、風控和 CSV 路徑；以固定行情驗證一般與微笑策略、2330/0050 零股及 0050 整張買賣。它證明應用程式流程，不能當作券商成交驗收。

`shioaji` 強制模擬連線，僅測 0050 一張，買進成交且回報入帳後才允許賣出；只有買賣都成交、回報完成且券商與程式持倉歸零才回報 `passed`。若已有 0050 模擬持倉則停止，不混入測試。券商文件仍標示[模擬環境不支援零股](https://sinotrade.github.io/tutor/simulation/)，所以券商驗收使用整張。逾時只撤本次尚未成交的委託，結果存至 `work/simulation-*/result.json`；`PreSubmitted`、權限不足、僅送單成功均不算通過。若成交後的出場未完成，報告會保留未平倉數量，需先處理該模擬持倉再重跑。

微笑回測頁面預設讀取最新本機日 K，也可選固定歷史快照重現舊結果。可調零股與每階數量；資料庫以唯讀開啟，不自動補抓資料。頁面回測不會向券商送單。

`scripts/run_simulation_acceptance.ps1` 是同一個券商模擬驗收的隱藏視窗包裝，固定 0050 一張、每階段最多 60 秒，輸出至 `work/simulation-broker-opening-時間/`。它不會啟動持續交易服務，`-CheckOnly` 僅檢查入口。

---

## 做多當沖核心原則（BaseStrategy 共用）

| 原則 | 說明 |
|------|------|
| 僅做多 | 只買現股、賣出數量不得超過持倉；不做融券做空 |
| 含費損益 | `STOP_LOSS_PCT` / `TAKE_PROFIT_PCT` / `TRAILING_STOP_PCT` 皆以**扣手續費+當沖稅後淨利 %** 判斷 |
| 漲停潛力 | LLM 快取 `limit_up_potential=true` 時，**不觸發**移動停利回撤賣出 |
| 收盤清倉 | 13:15 後背景 Thread 嘗試市價全出，不留隔夜（見下方例外） |

交易成本公式（[`src/bot/trade_cost.py`](../src/bot/trade_cost.py)）：

```
買進成本 = 成交金額 × (1 + 0.1425% × 折扣) + 最低手續費
賣出入帳 = 成交金額 × (1 - 0.1425% × 折扣 - 0.15% 當沖稅) - 最低手續費
淨利 % = (賣出入帳 - 買進成本) / 買進成本 × 100
```

相關 `.env`：`BROKER_FEE_DISCOUNT`、`BROKER_MIN_FEE`、`DAY_TRADE_TAX_RATE`。

### 淨利 PnL 計算（停損 / 移動停利皆用此公式）

程式透過 `position_net_pnl_pct()` 計算，**不是**毛報酬 `(現價-均價)/均價`：

```
淨利(%) = position_net_pnl_pct(均價, 現價, 張數, 單位, settings)
```

含手續費與當沖稅後，實際觸發停損的價格會比毛報酬估算略低（約 0.3~0.5% 差距）。

## 時間軸

```
08:30          09:00         09:30                   13:15     13:30
  │              │             │                       │         │
  ├──盤前準備──┤├──進場窗口──┤├────持倉監控期────────┤├──收盤──┤
  │              │             │                       │         │
  取得前日收盤    開盤           停止首次進場            全出場    收盤
  訂閱行情                     (ENTER_CUTOFF_TIME)    (EXIT_TIME)
```

ConfigurableStrategy 的回落買回窗口延伸至 `EXIT_TIME` 前。

---

## 一、ConfigurableStrategy（預設，`STRATEGY_TYPE=configurable`）

### 額外能力

| 功能 | 說明 |
|------|------|
| env 漲幅區間 | `MIN_PCT_CHG_ON_ENTRY`、`MAX_PCT_CHG_ON_ENTRY`、`BUY_ENTRY_TARGETS` |
| LLM 進場閘門 | `LLM_GATE_ENABLED=true` 時須通過情緒/當沖分數 |
| 回落買回 | `ALLOW_SAME_DAY_REENTRY=true` 且淨利 ≥ `REBUY_TARGET_NET_PCT` |
| LLM 負向出場 | `LLM_EXIT_ON_NEGATIVE=true` 時 sentiment 轉負可觸發賣出 |

### 進場：首次

- 時間 < `ENTER_CUTOFF_TIME`
- `in_entry_range(symbol, pct_chg)` — 預設 1% < 漲幅 < 5%（`entry_rules.py`）
- 通過 `llm_gate.allow_entry()`（若啟用）

### 進場：回落買回

- 時間 < `EXIT_TIME`（比首次進場窗口更長）
- 現價 < 上次賣出價，且買回→以賣出價平倉的淨利 ≥ `REBUY_TARGET_NET_PCT`（預設 2%）
- 受 `PER_SYMBOL_DAILY_MAX_ORDERS`、`REENTRY_COOLDOWN_SECONDS` 限制

### 張數計算

使用 `_calc_quantity()`，**含手續費**；支援零股模式 `USE_ODD_LOT=true`。

---

## 二、EtfFollowStrategy（`STRATEGY_TYPE=etf_follow`）

- 須有 ETF 共識訊號（新建倉 ≥ `ETF_MIN_CONSENSUS_NEW`、加碼 ≥ `ETF_MIN_CONSENSUS_ADD`）
- 漲幅：`-1% < pct_chg < ETF_MAX_PCT_CHG_ON_ENTRY`（預設 4%）
- 張數依共識強度可 boost×2；資金上限用 `effective_fund_cap()`（含手續費）
- **LLM 進場閘（刻意預設關閉）**：跟單以共識訊號為主，**預設不經** `llm_gate.allow_entry()`。
  若要與 `ConfigurableStrategy` 對齊，設 `ETF_FOLLOW_LLM_GATE=true`（且通常同時啟用 `LLM_GATE_ENABLED=true`）。

---

## 三、出場規則（BaseStrategy 共用）

### 4.0 出場授權與 AI 標籤

| 條件 | 說明 |
|------|------|
| AI 買進標籤 | 買單 `custom_field=AIBUY` → 部位 `owner_tag=AI`；非 AI 部位不會被自動賣出 |
| 使用者指定門檻 | `SELL_PROFIT_TARGETS=2330:8,0050:5.5` 設定後，該檔須達門檻才允許自動賣出 |

> **⚠️ SELL_PROFIT_TARGETS 風險**：有設定的股票，**未達標時停損、移動停利、收盤全出皆會被擋下**，可能保留隔夜部位。當沖請謹慎使用，或確保門檻低於停損線。

> **⚠️ LLM 賣出閘門**：`LLM_SELL_GATE_ENABLED=true` 時，AI 偏多可擋下移動停利；收盤全出預設由 `LLM_SELL_GATE_BYPASS_CLOSE=true` 略過（見參數表）。停損由 `LLM_SELL_GATE_BYPASS_STOP_LOSS=true`（預設）略過。

### 4.1 固定停損

| 項目 | 值 |
|------|-----|
| 觸發條件 | 淨利 PnL ≤ `STOP_LOSS_PCT`（預設 -3%） |
| 下單方式 | 市價 IOC 賣出 |
| 備註標記 | `sl` |

### 4.2 移動停利 (Trailing Stop)

**階段 1 — 獲利門檻**：淨利 PnL 須先達 `TAKE_PROFIT_PCT`（預設 6%）。

**階段 2 — 回撤出場**：追蹤淨利高點（high watermark），當 `(peak - current) >= TRAILING_STOP_PCT`（預設 2%）時觸發。

| 條件 | 說明 |
|------|------|
| 淨利 ≥ 門檻 | 追蹤期間 peak ≥ `TAKE_PROFIT_PCT` |
| 回撤 ≥ 閾值 | `peak - current >= TRAILING_STOP_PCT`（淨利 % 點差，非價格 %） |
| 漲停潛力 | `limit_up_potential=true` 時不觸發 |

### 4.3 收盤全出場

| 項目 | 值 |
|------|-----|
| 觸發條件 | 當前時間 >= `EXIT_TIME`（預設 13:15） |
| 下單方式 | 市價 IOC 賣出 |
| 備註標記 | `close` |
| 執行方式 | 獨立背景 Thread，每秒檢查 |

若全部被 `SELL_PROFIT_TARGETS` 或 LLM 賣出閘門擋下，策略會記錄警告並**保留部位**，不再強制清倉。

---

## 五、RiskGuard 閘門（所有買單必經）

不論策略類型，實際買單須通過 `RiskGuard.check_entry()`：

| 規則 | 說明 |
|------|------|
| Kill Switch | `data/.kill_switch` 存在 → 拒絕進場 |
| 黑名單 / 手動持股 | `BLACKLIST_SYMBOLS`、`MANUAL_HOLD_SYMBOLS` |
| 漲幅上限 | `MAX_PCT_CHG_ON_ENTRY > 0` 時，`abs(pct_chg)` 超過上限拒絕（含跌幅過大） |
| 下單頻率 | `DAILY_MAX_ORDERS`、`PER_SYMBOL_DAILY_MAX_ORDERS` |
| 再進場冷卻 | `REENTRY_COOLDOWN_SECONDS > 0` 時，平倉後 N 秒內拒絕（**優先於**策略層 `ALLOW_SAME_DAY_REENTRY`） |
| 損失熔斷 | `DAILY_MAX_LOSS_TWD` / `DAILY_MAX_LOSS_PCT` — **僅計已實現損益**，觸發後自動拉 Kill Switch |
| 資金 / 餘額 | `effective_fund_cap()`、`CHECK_ACCOUNT_BALANCE` |

出場 `check_exit()` 永遠允許（平倉優先）。

---

## 六、防護機制

### 6.1 防重複下單

每個 symbol 有 `pending_orders` 清單；有待成交委託時不送新單。

### 6.2 進場鎖

`_enter_placed` 防止重複送單；平倉後清除。ConfigurableStrategy 支援回落買回（見 §二）。

### 6.3 資金上限（同時曝險）

`_fund_used` 追蹤**同時占用額度**：買進時 `+= buy_cash_required`（含手續費），賣出時 `-= buy_cash_required(成本價, 賣出數量)`；完全平倉後應歸零（無其他持倉時）。`RiskGuard` 與策略共用 `effective_fund_cap()`；trade 模式送單後另有 `pending_exposure` 暫扣，成交或取消後釋放。狀態持久化於 `data/risk_state_*.json`。

### 6.4 委託狀態輪詢

獨立 Thread 每 3 秒清理已完成 pending orders。

### 6.5 啟動安全檢查（trade 模式）

比對券商庫存與本工具 AI 紀錄；異常 → Kill Switch。`MANUAL_HOLD_SYMBOLS` 可排除手動持股。

---

## 七、設定參數一覽

| 參數 | 預設值 | 影響 |
|------|--------|------|
| `STRATEGY_TYPE` | `configurable` | 策略類型（見總覽表；`default` 自動對應 `configurable`） |
| `ENTER_CUTOFF_TIME` | 09:30 | 首次進場窗口結束 |
| `EXIT_TIME` | 13:15 | 全出場時間 |
| `STOP_LOSS_PCT` | -3.0 | 固定停損（淨利 %） |
| `TAKE_PROFIT_PCT` | 6.0 | 移動停利啟動門檻（淨利 %） |
| `TRAILING_STOP_PCT` | 2.0 | 淨利高點回撤出場（% 點差） |
| `SELL_PROFIT_TARGETS` | 空 | 每檔賣出門檻；**未達標會擋停損/收盤** |
| `DAILY_FUND_BUDGET` | 0 | 每日曝險上限；>0 優先於 MAX_FUND |
| `MAX_FUND` | 500,000 | 總可用資金 |
| `ALLOW_SAME_DAY_REENTRY` | true | 策略層允許當日再進（受冷卻限制） |
| `REENTRY_COOLDOWN_SECONDS` | 0 | RiskGuard 再進場冷卻（秒） |
| `REBUY_TARGET_NET_PCT` | 2.0 | 回落買回淨利門檻（ConfigurableStrategy） |
| `LLM_GATE_ENABLED` | false | LLM 進場閘門（ConfigurableStrategy） |
| `ETF_FOLLOW_LLM_GATE` | false | EtfFollow 亦套用 LLM 進場閘（通常搭配上一項） |
| `LLM_SELL_GATE_ENABLED` | false | 賣出前 AI 分析 |
| `LLM_SELL_GATE_BYPASS_STOP_LOSS` | true | 停損略過 AI 賣出閘門 |
| `LLM_SELL_GATE_BYPASS_CLOSE` | true | 收盤全出略過 AI 賣出閘門 |
| `MAX_LOT_PER_SYMBOL` | 2 | 每檔最大張數 |
| `MAX_PCT_CHG_ON_ENTRY` | 0 | RiskGuard 漲跌幅上限（0=不檢查） |
| `MANUAL_HOLD_SYMBOLS` | 空 | 手動持股，永不自動交易 |

> **簡報 / 評分建議值**：`prompts/intraday_brief.yaml` 與 `scoring.py` 的當沖停損 -1% / 停利 +2% 僅供人工參考，**不會**被 bot 自動執行。Bot 預設為停損 -3%、移動停利 6%/2%。

---

## 八、自訂策略

繼承 `BaseStrategy` 並實作 `on_tick()` 即可替換買賣邏輯：

```python
from bot.strategy import BaseStrategy

class MyCustomStrategy(BaseStrategy):
    def on_tick(self, tick) -> None:
        # 你的策略邏輯 ...
        pass
```

BaseStrategy 提供部位管理、防重複下單、收盤出場、交易紀錄、通知等功能。
