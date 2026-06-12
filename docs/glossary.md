# 術語對照表

本文件為 Stock Bot 專案的**唯一術語參照**。其他 docs、prompts、README 若與此衝突，以本表與程式碼為準。

---

## 代號與行情欄位

| 標準概念 | 程式（交易核心） | 程式（評分/LLM） | 中文 |
|----------|------------------|------------------|------|
| 股票代號 | `symbol` | `ticker` | 代號、監控標的 |
| 漲跌幅 | `pct_chg`（`MarketTick`） | `pct_change`（`scoring`） | 漲幅、漲跌 % |

> `ticker` ≡ `symbol`；`pct_change` ≡ `pct_chg`。LLM 快取路徑為 `data/auto_llm/{symbol}.json`，prompt 變數多用 `ticker`。

---

## 損益（PnL）

| 術語 | 定義 | 程式 |
|------|------|------|
| **淨利 %** | 扣手續費與當沖證交稅後的損益百分比 | `position_net_pnl_pct()`、`net_pnl_pct()` |
| **毛價差 %** | 僅用報價 `(現價 - 均價) / 均價`（**不**用於 bot 停損停利） | 舊文件公式，已淘汰 |

Bot 的 `STOP_LOSS_PCT`、`TAKE_PROFIT_PCT`、`TRAILING_STOP_PCT` 皆以**淨利 %** 判斷。詳見 [trading-rules.md](trading-rules.md)。

---

## 停損停利三層（勿混淆）

| 層級 | 來源 | 典型數值 | 說明 |
|------|------|----------|------|
| **A. Bot 自動下單** | `.env` + `BaseStrategy` | 停損 -3%、移動停利啟動 +6%、回撤 2% | 實際觸發委託的參數 |
| **B. LLM 戰情建議** | `intraday_brief`、`next_day_brief` 等 | 停損 -1%、參考停利 +2% | **人工參考**，非 bot 參數 |
| **C. 評分量表建議** | `scoring.py` 各時間框架 | 當沖 -1%/+2% 等 | 分析用建議，非 bot 參數 |

### `TAKE_PROFIT_PCT` 語意

**移動停利啟動門檻**（淨利 % 須先達此值才進入追蹤期），**不是**固定停利出場點。出場由 `TRAILING_STOP_PCT` 從淨利高點回撤觸發。

---

## 持倉相關用詞

| 中文 | 使用情境 | 程式/API |
|------|----------|----------|
| **持倉** | 策略追蹤的均價、數量、監控期 | `PositionInfo`、`positions` |
| **部位** | 策略內部狀態、AI 標記、清倉 log | `owner_tag`、log「部位已清空」 |
| **庫存** | 券商帳戶實際持股 | `broker.list_positions()` |
| **持股** | 投組分析、長期手動標的 | `MANUAL_HOLD_SYMBOLS`、`portfolio_analysis` |

---

## 風控層級

| 層級 | 中文 | 程式 | 說明 |
|------|------|------|------|
| Preflight | 交易可行性檢查、**7 個分區** | `run_preflight()` | 連線層：能不能送單；最後一區為**持倉安全** |
| RiskGuard | **12 道閘門** | `RiskGuard.check_entry()` | 每一筆買單送單前 |
| LLM Gate | AI 進場/賣出閘門 | `LlmGate.allow_entry/exit()` | 可選；與 RiskGuard 獨立 |
| Kill Switch | 緊急拉閘 | `data/.kill_switch` | 只擋新進場，不擋平倉 |

### 閘門 vs 門檻

- **閘門**：可阻擋交易的檢查（RiskGuard、LLM Gate、Preflight fail）
- **門檻**：數值觸發點（`STOP_LOSS_PCT`、`TAKE_PROFIT_PCT`、`SELL_PROFIT_TARGETS`）

---

## 策略對照

| `STRATEGY_TYPE` | 類別 | 說明 |
|-----------------|------|------|
| `configurable`（預設） | `ConfigurableStrategy` | env 漲幅區間 + LLM 閘門 + 回落買回 |
| `etf_follow` | `EtfFollowStrategy` | 主動 ETF 共識跟單 |

> 舊值 `default` 會自動對應為 `configurable`。

出場規則（停損、移動停利、收盤全出）由 `BaseStrategy` 共用。

---

## 執行模式與虛擬交易

| 模式 | 說明 |
|------|------|
| `trade` | 實單/模擬下單 |
| `watch` | Shioaji 行情，不下單 |
| `report` | TWSE 延遲資料，不下單 |

| 英文訊號 | 中文 log | 說明 |
|----------|----------|------|
| `would-buy` | `[虛擬買入]` | watch/report 虛擬進場 |
| `would-sell` | `[虛擬賣出]` | watch/report 虛擬出場 |
| `sell-blocked` | — | 賣出被 `SELL_PROFIT_TARGETS` 或 LLM 閘門擋下 |

---

## AI 部位標記（兩層）

| 層 | 欄位 | 用途 |
|----|------|------|
| 券商委託 | `custom_field=AIBUY`（買）/ `AITP`/`AISL` 等（賣） | 送單時標記 |
| 本地持倉 | `owner_tag=AI` | 判斷是否允許自動賣出 |

非 `owner_tag=AI` 的部位不會被 bot 自動賣出。

---

## 期貨價差

**台指期正逆價差** = 期貨價 − 現貨價

- **正價差**：期貨 > 現貨（情緒偏熱）
- **逆價差**：期貨 < 現貨（情緒偏保守）

亦稱「期現貨價差」。本專案期貨僅作**領先指標**，不下單。見 [futures-spot-strategy.md](futures-spot-strategy.md)。

---

## 言行反查（兩套 schema）

中文皆可稱「言行反查」，但 prompt 與 JSON 欄位不同：

| Prompt | 判定欄位 | 比對對象 |
|--------|----------|----------|
| `logic_check` | `verdict` | 法說語意 vs 籌碼面 |
| `analyst_real_sentiment` | `discrepancy` | 外資報告語意 vs 實際買賣超 |

評分系統的 `logic` factor 對應 `logic_check`；`analyst_real_sentiment` 為獨立分析流程。

---

## 市場氛圍（LLM JSON）

`market_tone`：`risk_on`（偏多）/ `neutral`（中性）/ `risk_off`（偏空）

---

## 券商平台

永豐金證券交易平台拼寫：**iLeader**（非 e leader）。

---

## 相關文件

- [trading-rules.md](trading-rules.md) — 自動買賣規則
- [architecture.md](architecture.md) — 系統架構
- [operations.md](operations.md) — 日常操作與 log 對照
- [prompts/README.md](../prompts/README.md) — Prompt 清單
