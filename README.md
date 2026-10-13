# Stock Bot - 台股研究與分析平台

基於 **Shioaji (永豐金證券)** API 的台股研究、法說分析與行情工具；**當沖自動交易模組已封存**（2026-06-11）。

> **封存說明**：`RUN_MODE=trade` 預設已阻擋。日常請用 `watch` / `report` 或 `stock-dashboard`；詳見 [docs/archive/day-trading.md](docs/archive/day-trading.md)。  
> 暫時恢復舊版當沖實單：`.env` 設 `DAY_TRADING_UNFREEZE=true`。

> **Disclaimer**: 本專案僅供教學與參考之用，實務交易應自行評估並承擔相關風險。

## 功能特色

- **三種執行模式** -- `trade` 自動交易 / `watch` 看盤不下單 / `report` 純報表分析
- **Shioaji 原生整合** -- 使用 Decorator-based callback，Pythonic 風格
- **Queue 解耦架構** -- Tick 行情與策略運算分離，避免阻塞回呼執行緒
- **盤前收盤價載入** -- 透過 snapshots API 取得精確的前日收盤價
- **多檔資金追蹤** -- 即時追蹤已用資金，避免超額下單
- **移動停利** -- 追蹤持倉淨利高點，從高點回撤 N 百分點觸發出場（含費損益）
- **部位管理** -- 即時追蹤持倉均價、數量，成交回報自動更新
- **委託單追蹤** -- 防止重複下單，自動輪詢委託狀態
- **斷線重連** -- 指數退避重試，自動恢復登入與行情訂閱
- **收盤全出場** -- 指定時間自動市價清倉
- **交易紀錄匯出** -- 收盤後自動匯出 CSV 至 `data/` 目錄
- **訊號與報表匯出** -- watch/report 模式匯出 would-buy/sell 訊號及分析損益
- **Telegram 通知** -- 買賣/停損停利/收盤摘要即時推播到手機
- **模擬模式** -- `SIMULATION=true` 即可使用模擬環境測試
- **Web 儀表板** -- `stock-dashboard` 啟動 Streamlit 介面，一站瀏覽所有功能
- **主動式 ETF 跟單** -- 內建 **27 檔主動式 `*A` ETF + ALI006（安聯台灣科技基金）＝ 28 筆** 持股追蹤清單（見 `src/bot/active_etf.py` 的 `DEFAULT_ACTIVE_ETFS`；可被 `data/active_etfs.json` 覆寫）
- **MOPS 法說會爬蟲** -- 抓法人說明會行事曆與個股重大訊息
- **🤖 全自動法說研究** -- 自動抓行事曆 + 上網搜尋 (DuckDuckGo + Google News) + 鉅亨新聞 + LLM 結構化分析，不需貼逐字稿
- **法說會行事曆自動快取** -- 上月 / 本月 / 下月 / +2 月四個月份自動每日更新，dashboard 進入即用
- **Gemini LLM 分析** -- 免費 API 解析法說會語意、提取情緒/Capex/毛利率指引/催化劑展望
- **言行反查** -- 對比管理階層語意 vs 籌碼面，偵測疑似出貨/吸籌
- **Prompt 版本化管理** -- 所有 LLM prompt 集中於 `prompts/*.yaml`，UI 可直接編輯
- **LLM 呼叫全紀錄** -- 每次呼叫的 input/output/延遲/tokens 自動寫入 JSONL
- **ETF 持股自動抓取** -- 依 URL 配置 HTTP 下載 + Gemini 自動抽取 JSON
- **籌碼面自動拉取** -- TWSE OpenAPI 自動抓三大法人/借券/融資/鉅額交易
- **大戶 vs 散戶結構** -- TDCC 集保戶股權分散表 (週) 自動拉取與趨勢追蹤
- **基本面 360 度** -- 月營收 (YoY/MoM)、PER/PBR/殖利率、歷年股利、季度 EPS、三率
- **技術面指標** -- TWSE STOCK_DAY 日 K + MA/MACD/RSI/KD/布林 + 訊號摘要
- **K 線型態判讀** -- 單根 K 棒自動分類為 16 種型態 (大/中/小紅黑K、鎚子/倒鎚、紡錘、十字/T/倒T/一字線)，附偏多偏空與市場訊號
- **Q1～Q4 季報節奏** -- 滾動 EPS、季度營收聚合、季節性焦點框架
- **一鍵研究管線** -- `stock-auto-research` 把 ETF×籌碼×法說×LLM×每日簡報全部串連
- **K 線看板 (Stock Board)** -- 三種顯示模式 (縮圖 grid / 並排大圖 / 單檔專注)、10 段時間範圍 (1 個月 ~ 10 年 / 全部 / 自訂日期)、可手動 Y 軸縮放、批次分月往前抓 1/2/3/5/10 年歷史，並直接推送到 Google Sheets
- **個股總覽 Watchlist** -- 表格化呈現多檔個股，四時間框架評分排序篩選
- **個股深入分析** -- 單檔股票完整 KPI + 4 張量表卡 + 11 個分頁 (分析/基本面/技術面/籌碼面/股利/季報/美股連動/原始資料/購買策略/目前狀況/歷史狀況)
- **評分量表系統** -- 九個 factor × 四時間框架 (當沖/短/中/長) 加權算分，附完整策略 (進場/停損/停利/部位)
- **本地 SQLite 冷/溫資料庫** -- 集中管理公司基本面、ETF Meta、月營收、季報、watchlist、**歷史日 K 線**；Cache-Aside 降低 API 用量
- **歷史 K 線入庫** -- TWSE 抓到的 OHLCV 自動寫入 `price_history` table，跨機可同步、可離線分析
- **Google Sheets 雲端同步** -- 多台電腦/手機共用同一份資料 (含歷史 K 線)；推/拉/智能同步三鍵搞定，可接 Google Forms 從手機新增股票
- **Google Drive 快取鏡像** -- 可設定 `GOOGLE_CACHE_DIR`，讓已抓取的 JSON/CSV/PDF 快取在多台電腦間共用，避免重複抓 TWSE/MOPS/yfinance

## 可以買賣的市場與限制

| 市場 | 角色 | 自動下單 | 資料來源 |
|------|------|:--------:|---------|
| **台股** (上市 / 上櫃) | 真實/模擬交易 | ✅ (透過永豐 Shioaji) | Shioaji 即時行情 + TWSE / TPEx OpenAPI (日K、月營收、估值、季報、籌碼皆涵蓋上市與上櫃) |
| **美股 / ADR** | 純分析 | ❌ | yfinance (盤後快照) |
| **加權指 / SOX / VIX** | 純分析 (跨市場連動) | ❌ | yfinance |
| 台指期（僅行情／正逆價差分析） | 純分析 | ❌（不含期貨下單） | TAIFEX DailyMarketReportFut；支援 raw/research/evidence/claim 追溯 |
| 選擇權、興櫃 | — | ❌ (未實作) | — |

### Preflight 交易可行性（7 個分區）

以下為「能不能連上券商、現在能不能送單」的**連線層檢查**（由 `stock-preflight` 驗證）。
儀表板會顯示 **7 個分區**：環境 / CA / 連線 / 帳戶 / 風控 / 時間 / **持倉安全**（最後一區）。

常見前提條件包括：

| # | 條件 | 設定位置 | 驗證方式 |
|---|------|----------|---------|
| 1 | `API_KEY` / `SECRET_KEY` 已設定 | `.env` 或「組態設定」頁 | 「交易可行性檢查」自動驗證 |
| 2 | **API 金鑰開了「下單」權限** (不只是 Data) | 永豐 iLeader → API 金鑰管理 | 嘗試 `list_positions` 拿不到 401 |
| 3 | 線上協議已簽署 `stock_account.signed=True` | 永豐 iLeader | 登入後讀 stock_account |
| 4 | 電子憑證 `.pfx` 存在、密碼正確、未過期 | `CA_PATH` / `CA_PASSWORD` | 用 cryptography 解 PFX 拿過期日 |
| 5 | `RUN_MODE=trade` (`watch` / `report` 不會下單) | `.env` | settings.run_mode |
| 6 | `SIMULATION=false` (true 為模擬環境) | `.env` | settings.simulation |
| 7 | 現在處於台股盤中 **09:00–13:30** 且為交易日 | — | preflight 即時判斷 |

**持倉安全**（第 7 分區）：監控標的不得與券商「手動/外部」庫存混倉，且本工具 AI 紀錄須與券商庫存對帳一致。
`stock-bot` 啟動時會再跑一次啟動安全檢查（見下方）。

> 缺任何一項 → preflight 會明確列出「為什麼還不能下單」+ 修正建議。
> 下方「12 道閘門」則是**每一筆買單**送單前 `RiskGuard.check_entry()` 的規則，兩者層級不同。

### 一鍵體檢

```bash
uv run stock-preflight                # 含真實 Shioaji 連線測試
uv run stock-preflight --no-login     # 只檢查 .env 設定
uv run stock-preflight --json         # 給 CI / 通知用
```

Exit code：`0` 無 fail／`2` 有阻擋項（人類可讀與 `--json` 相同）。

研究、證據與可選服務使用另一個不碰網路、不呼叫 LLM、不寫入資料庫的整合檢查：

```powershell
uv run stock-system-preflight --root C:\skymiku\stock --json
uv run stock-system-preflight --root C:\skymiku\stock --imports
```

它會唯讀檢查 SQLite integrity、raw SHA-256、FTS、provenance orphan、research
缺口，以及 Gemini／Google Sheets 設定狀態。缺少可選服務只會標示 `info`；真正的
資料完整性錯誤才會以 exit code `2` 阻擋。需要實際 API／來源驗證時，仍要另外執行
`stock-gemini-test`、`stock-validate --cloud` 或 `stock-validate --shioaji`。
若目前有舊版 Dashboard 佔用 `.venv\Scripts` 的入口檔，也可直接使用
`uv run --no-sync python -m bot.system_preflight_cli --root C:\skymiku\stock --json`。

或在儀表板 **⚡ 執行與紀錄 → 🩺 交易可行性檢查** 點「立刻檢查」，
會顯示 **7 個分區**（環境 / CA / 連線 / 帳戶 / 風控 / 時間 / **持倉安全**）、
每項通過/警告/阻擋狀態與修復建議。

`stock-bot` 啟動時還會再跑一次**啟動安全檢查**（券商庫存 vs 本工具 AI 紀錄）；
若重疊或對帳失敗會自動拉 Kill Switch（僅擋新進場）。

## 🛡 資金/風險控制 (12 道閘門 + Kill Switch)

所有「實際下單」路徑都必經 `RiskGuard.check_entry()`，要全部通過才能送單；
任一條 fail 都會被擋下並寫進 `data/risk_state_*.json` 供追蹤。

### A. 資金上限 (`.env`)

| 設定 | 預設 | 說明 |
|------|------|------|
| `DAILY_FUND_BUDGET` | 0 (沿用 MAX_FUND) | **每日同時曝險上限** (元)；>0 時優先於 `MAX_FUND`，例 10000 = 同時最多占用 1 萬 |
| `MAX_FUND` | 500000 | 總可用資金天花板 (元)；`DAILY_FUND_BUDGET=0` 時生效 |
| `PER_ORDER_MAX_COST_TWD` | 0 (不限) | 單筆委託金額上限；例 100000 = 每次下單最多 10 萬 |

程式內以 `effective_fund_cap()` 取實際上限（`DAILY_FUND_BUDGET` 優先）。
買進成本以 `buy_cash_required` 計入（含手續費）；`CHECK_ACCOUNT_BALANCE=true` 時進場前還會讀券商 `account_balance`，讀不到則拒絕進場。
儀表板「已用資金」= 當日同時占用額度（買進含手續費、賣出依成本價釋放），完全平倉後歸零可再買。回落買回（`rebuy`）不受 `PER_SYMBOL_DAILY_MAX_ORDERS` 限制。

### B. 持倉控制 (`.env`)

| 設定 | 預設 | 說明 |
|------|------|------|
| `MAX_LOT_PER_SYMBOL` | 2 | 單檔最多持有張數 |
| `MAX_OPEN_POSITIONS` | 0 (不限) | 同時最多 N 檔在倉 |
| `BLACKLIST_SYMBOLS` | (空) | 永遠不下單的代號，逗號分隔 |
| `MANUAL_HOLD_SYMBOLS` | (空) | 手動長期持股；併入黑名單、永不自動交易（語意上標記「這檔是我手動拿的」） |

### B2. 出場授權 (`.env`)

| 設定 | 預設 | 說明 |
|------|------|------|
| `SELL_PROFIT_TARGETS` | (空) | 每檔使用者指定賣出門檻；例 `2330:8,0050:5.5` |

本工具有**兩層標記**（勿混淆）：
- 送單時 `custom_field=AIBUY`（券商委託欄位）
- 成交後本地紀錄 `owner_tag=AI`（`data/trades_*.csv` 與持倉追蹤）

自動賣出只處理 `owner_tag=AI` 的部位。若某檔有設定 `SELL_PROFIT_TARGETS`，須達門檻才賣；
未達標時停損、移動停利與收盤全出場會被擋下。

| 設定 | 預設 | 說明 |
|------|------|------|
| `LLM_SELL_GATE_ENABLED` | false | 技術賣點觸發後，須通過 AI 分析才送賣單 |
| `LLM_SELL_GATE_BYPASS_STOP_LOSS` | true | 停損是否略過 AI 賣出閘門（建議 true） |
| `LLM_SELL_GATE_BYPASS_CLOSE` | true | 收盤全出是否略過 AI 閘門（建議 true，避免留倉） |
| `LLM_SELL_MIN_CONFIDENCE` | 0.5 | AI 允許賣出的最低信心 |

> 若 `LLM_SELL_GATE_BYPASS_CLOSE=false` 且 AI 偏多，收盤全出可能被擋下而保留隔夜部位。

### C. 損失熔斷 (`.env`) - **🚨 最重要**

當日已實現虧損超過下方門檻時，**自動拉起 Kill Switch**，立刻禁止所有新進場：

| 設定 | 預設 | 說明 |
|------|------|------|
| `DAILY_MAX_LOSS_TWD` | 0 (不限) | 絕對值，例 5000 = 一天虧 5000 元就熔斷 |
| `DAILY_MAX_LOSS_PCT` | 0 (不限) | 占 **effective_fund_cap()** 百分比，例 1.0 + 預算 1 萬 = 虧 100 元熔斷 |

兩者擇較嚴者；觸發後**仍可平倉**（停損/停利/收盤強平），但不會再開新倉。

### D. 下單頻率 (`.env`)

| 設定 | 預設 | 說明 |
|------|------|------|
| `DAILY_MAX_ORDERS` | 0 (不限) | 當日最多進場 N 次 |
| `PER_SYMBOL_DAILY_MAX_ORDERS` | 0 (不限) | 單檔當日最多 N 次，建議 1~2 |
| `REENTRY_COOLDOWN_SECONDS` | 0 | 平倉後同檔需等 N 秒才能再進，建議 300~900 |

### E. 進場條件 (`.env`)

| 設定 | 預設 | 說明 |
|------|------|------|
| `MAX_PCT_CHG_ON_ENTRY` | 0 (不限) | 漲幅超過 N% 不進場，建議 3~5 (防追高) |
| `MIN_PRICE` | 0 (不限) | 最低股價，建議 ≥ 10 元 |
| `MAX_PRICE` | 0 (不限) | 最高股價，避免買到 1000+ 元股 |

### F. 🔴 Kill Switch (緊急開關)

任何時候都可以「一鍵切掉所有新進場」：

```bash
# 拉閘 (建立檔案)
echo manual > data/.kill_switch

# 解除
del data/.kill_switch     # Windows
rm data/.kill_switch      # Unix
```

或在儀表板 **⚡ 執行與紀錄 → 🛡 風控中心** 點「🔴 緊急拉閘」按鈕。

> Kill Switch **只擋新進場，不擋平倉** — 停損 / 停利 / 收盤強平永遠會執行，
> 因為它們本身就是降低風險的動作。

### 推薦的保守設定 (新手 / 小額)

**大額帳戶範例**（50 萬）：

```env
# 50 萬資金，單筆最多 5 萬，最多 3 檔，當日虧 1% 就收手
MAX_FUND=500000
PER_ORDER_MAX_COST_TWD=50000
MAX_LOT_PER_SYMBOL=1
MAX_OPEN_POSITIONS=3

DAILY_MAX_ORDERS=5
PER_SYMBOL_DAILY_MAX_ORDERS=1
REENTRY_COOLDOWN_SECONDS=600

MAX_PCT_CHG_ON_ENTRY=4
MIN_PRICE=10
MAX_PRICE=500

DAILY_MAX_LOSS_PCT=1.0     # 一天最多虧 1% (= 5000 元)
STOP_LOSS_PCT=-2.0          # 單筆停損 -2%（淨利 %）
TAKE_PROFIT_PCT=4.0         # 移動停利啟動門檻 +4%（淨利 %，非固定停利）
```

**每日 1 萬零股當沖範例**：

```env
DAILY_FUND_BUDGET=10000
MAX_FUND=10000
PER_ORDER_MAX_COST_TWD=10000
MAX_LOT_PER_SYMBOL=1
USE_ODD_LOT=true
DAILY_MAX_ORDERS=10
PER_SYMBOL_DAILY_MAX_ORDERS=5
DAILY_MAX_LOSS_PCT=1.0      # 1% of 1 萬 = 100 元熔斷
LLM_SELL_GATE_ENABLED=true
# MANUAL_HOLD_SYMBOLS=2330  # 若手動長期持有某檔，列在此避免混倉
```

### 風控中心儀表板

`⚡ 執行與紀錄 → 🛡 風控中心` 一頁看完：

- 🟢/🔴 Kill Switch 狀態 + 拉閘/解除按鈕
- 已用資金 / 剩餘可用 / 在倉檔數 / 今日進場次數 (含進度條)
- 已實現損益 / 未實現損益 / 距離熔斷剩餘空間
- 12 道閘門的當前設定值表 (連結到對應 ENV 名稱)
- 各檔今日進場次數
- 最近 20 筆被擋下的進場 (含原因)

## 三種執行模式

| 模式 | 行情來源 | 需要 API Key | 啟用 CA | 下單 | 輸出 |
|------|---------|:------------:|:-------:|:----:|------|
| `trade` | Shioaji 即時行情 | ✅ | ✅ (實單) | ✅ | `data/trades_*.csv` |
| `watch` | Shioaji 即時行情 | ✅ | ❌ | ❌ | `data/reports/signals_*.csv` + `report_*.csv` |
| `report` | TWSE 公開延遲資料 | ❌ | ❌ | ❌ | `data/reports/signals_*.csv` + `report_*.csv` |

### watch — 看盤模式（預設 `RUN_MODE=watch`）

- 使用 Shioaji 行情 API 接收即時 Tick 資料
- **不啟用 CA、不會送出任何委託**
- 觸發進出場條件時記錄 would-buy / would-sell 訊號
- 虛擬部位會追蹤停損、移動停利與收盤出場
- 適合日常觀察；程式與 `.env.example` 預設皆為 `watch`

### trade — 自動交易（需明確設定；當沖實單預設封存）

- 完整的 Shioaji 登入流程，需要 API Key + Secret Key
- 實單模式需啟用電子憑證 (CA)；且當沖 trade 預設封存，需 `DAY_TRADING_UNFREEZE=true` 才可能啟動
- 策略觸發進出場條件時真正送出委託
- `SIMULATION=true` 時使用 Shioaji 模擬環境

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

### 執行測試

先安裝開發依賴（含 pytest）：

```bash
uv sync --extra dev
```

建議透過 `stock-test` 跑測試（內部以 `sys.executable -m pytest` 子行程執行，`cwd` 為專案 root，發現範圍錨定 `tests/`）：

```bash
# unit：套用 -m "not integration and not slow"
uv run stock-test unit

# regression：完整跑 tests/（不加 marker 過濾）
uv run stock-test regression

# 傳透 pytest 參數（`--` 之後原樣保留順序）
uv run stock-test unit -- -q
uv run stock-test regression -- -k "test_config or test_event_bus"

# 指定專案 root（可放在子命令之前；預設為呼叫端 cwd，且須含 tests/）
uv run stock-test --root C:\path\to\stock unit
uv run stock-test --root C:\path\to\stock regression -- -q

# 模組備援（入口尚未刷新或 shim 被鎖定時）
uv run python -m bot.test_cli unit
uv run --no-sync python -m bot.test_cli --root C:\path\to\stock regression -- -q
```

**Profile 語意**

| Profile | 行為 |
|---------|------|
| `unit` | `pytest <root>/tests -m "not integration and not slow"` |
| `regression` | `pytest <root>/tests`（不過濾 marker） |

`pyproject.toml` 已註冊 `integration` / `slow` markers。多數既有測試尚未標註時，兩 profile 收集結果會重疊；為整合／慢測加上 marker 後，`unit` 會排除它們。`unit` 拒絕 `--` 後方的 `-m` / `-mslow` / `--markexpr`（避免蓋掉 unit 過濾）；自訂 marker 請用 `regression` 或直接呼叫 pytest。

Exit code：原樣回傳 pytest 的結束碼（通常 `0` 通過／非 `0` 失敗）。若未安裝 pytest，回傳 `1` 並提示 `uv sync --extra dev`；`--root` 無效（不存在、非目錄、或缺 `tests/`）則回傳 `2`。

一般測試禁止真實 HTTP／socket 連線；漏掉來源替身會直接失敗，不會靠網路狀態決定結果。
需要真實服務的案例須標記 `integration`，並明確執行
`uv run stock-test regression -- -m integration --allow-network`；沒有這個選項時會跳過。
CI 在 Windows 與 Linux 使用 Python 3.11、`uv sync --locked --extra dev` 及 unit profile。

> **注意**：若 `stock-dashboard`（Streamlit 儀表板）正在執行，`uv run …` 可能因
> `.venv\Scripts\*.exe` 被鎖定而失敗。此時請改用模組備援，或先關閉儀表板程序。

仍可直接呼叫 pytest（不經 `stock-test`）：

```bash
.venv/Scripts/python.exe -m pytest tests/ -v
uv run pytest tests/ -v
```
## 設定

複製 `.env.example` 為 `.env`，填入你的實際資訊：

```bash
cp .env.example .env
```

主要設定項目：

| 變數 | 說明 | 預設值 |
|------|------|--------|
| `RUN_MODE` | 執行模式 (`trade` / `watch` / `report`) | `watch` |
| `MARKET_SOURCE` | 行情來源 (留空自動判斷) | (自動) |
| `REPORT_POLL_SECONDS` | report 模式輪詢間隔 (秒) | `5` |
| `REPORT_OUTPUT_DIR` | 報表輸出目錄 | `data/reports` |
| `API_KEY` | Shioaji API Key | (trade/watch 必填) |
| `SECRET_KEY` | Shioaji Secret Key | (trade/watch 必填) |
| `CA_PATH` | 電子憑證路徑 (.pfx) | (trade 實單必填) |
| `CA_PASSWORD` | 憑證密碼 | (trade 實單必填) |
| `PERSON_ID` | 身分證字號 | (trade 實單必填) |
| `SIMULATION` | 模擬模式 | `true` |
| `SYMBOLS` | 監控股票 (逗號分隔) | `.env.example` 範例預設 `2330,0050`；Settings 未載入 env 時 fallback 為空 |
| `ENTER_CUTOFF_TIME` | 停止進場時間 | `09:30` |
| `EXIT_TIME` | 全部出場時間 | `13:15` |
| `STOP_LOSS_PCT` | 停損百分比 | `-3.0` |
| `TAKE_PROFIT_PCT` | 移動停利啟動門檻（淨利 %，非固定停利） | `6.0` |
| `TRAILING_STOP_PCT` | 移動停利回撤百分比 | `2.0` |
| `SELL_PROFIT_TARGETS` | 每檔使用者指定賣出門檻，例如 `2330:8` | (空) |
| `MAX_FUND` | 總資金上限 | `500000` |
| `MAX_LOT_PER_SYMBOL` | 每檔最大張數 | `2` |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token | (選填) |
| `TELEGRAM_CHAT_ID` | Telegram Chat ID | (選填) |

## 使用方式

```bash
# 看盤模式（預設 RUN_MODE=watch；需要 .env 中的 API Key）
uv run stock-bot

# 報表模式 (不需要 API Key)
RUN_MODE=report SYMBOLS=2330,0050 uv run stock-bot

# 自動交易模式（需明確設定；當沖實單另需解凍）
RUN_MODE=trade uv run stock-bot
```

PowerShell 使用者：

```powershell
# 報表模式
$env:RUN_MODE="report"; $env:SYMBOLS="2330,0050"; uv run stock-bot
```

## Web 儀表板 (推薦)

最新介面為 **Stock Lab 台股研究工作台**：首頁提供常用研究入口，側欄可搜尋功能，
分析與市場搜尋可在「任務中心」跨頁追蹤。「本次更新」會列出可核對的改動。
操作與背景任務範圍請見 [2026-10-12 介面更新](docs/dashboard-update-2026-10-12.md)。

把所有功能聚合成一個 Streamlit 網頁，免記指令、免手改 `.env`：

```bash
uv run stock-dashboard
# 或（shim：轉呼叫 bot.dashboard.main_app / run）
uv run streamlit run src/bot/dashboard.py
```

啟動後瀏覽器會打開本機 Streamlit 頁面。**頁面名稱、數量與導覽分組以
`src/bot/dashboard/nav.py` 的 `PAGES`／`NAV_GROUPS` 為權威現況**（含晚點插入的
「台股行事曆」）；能力盤點報告也從此掃描，勿以本 README 的舊表為準。

常用頁面摘要（非完整清單）：

| 區塊 | 代表性頁面 |
|------|-----------|
| 🔍 研究與分析 | 功能總覽、今日當沖戰情室／即時追蹤、明日當沖關注、K 線看板、個股總覽／深入分析、自動化管線 |
| 📡 監控與訊號 | 美股／跨市場、熱門個股期貨、跟單訊號、主動 ETF 追蹤（27 `*A` + ALI006＝28）、LLM 法說分析、台股行事曆 |
| ⚡ 執行與紀錄 | 啟動／監控、模擬交易、風控中心、交易可行性檢查、交易紀錄、報表分析 |
| ⚙️ 系統與診斷 | 組態、資料庫／雲端同步、Prompt／LLM 紀錄、記憶與證據、日誌、通知、文件 |

### 策略切換

預設 `STRATEGY_TYPE=configurable` 使用 **ConfigurableStrategy**（env 漲幅區間 + LLM 閘門 + 回落買回）。可改 `etf_follow`（EtfFollowStrategy）。詳見 [docs/trading-rules.md](docs/trading-rules.md) 與 [docs/glossary.md](docs/glossary.md)。

### 啟用 ETF 跟單策略

在 `.env` 設定：

```
STRATEGY_TYPE=etf_follow
ETF_MIN_CONSENSUS_NEW=2
ETF_MIN_CONSENSUS_ADD=3
ETF_MAX_PCT_CHG_ON_ENTRY=4.0
```

接著到「主動 ETF 追蹤」頁匯入至少兩日的 ETF 持股 CSV，重啟 bot 即可。

### 啟用 Gemini LLM 法說分析

到 [Google AI Studio](https://aistudio.google.com) 取得免費 API Key，
填入 `.env` 的 `GEMINI_API_KEY` (預設模型 `gemini-2.5-flash`)。
無 API key 時系統會自動退回「純規則式」邏輯反查，依然可用。

```bash
GEMINI_API_KEY=your_google_ai_studio_key
GEMINI_MODEL=gemini-2.5-flash

uv run stock-gemini-test
```

`stock-gemini-test` 會讀取 `.env`、建立 Gemini client、送出一段短 prompt，
並回報模型、延遲與 token metadata；成功後再執行下方自動化研究管線。

### 🤖 全自動 LLM 個股研究 (新)

「LLM 法說分析」過去要使用者手動貼逐字稿；新版**全程無人值守**：

```bash
uv run stock-auto-research --llm-only                  # 跑 watchlist 所有檔
uv run stock-auto-research --llm-only 2330,2317,3231   # 指定多檔
uv run stock-auto-research --llm-only --upcoming       # 加入「未來 14 天有法說會」的個股
uv run stock-auto-research --llm-only --refresh        # 略過 12h 快取，強制重打 LLM
```

流程：
1. **自動更新 MOPS 法說會行事曆** (上月 / 本月 / 下月 / +2 月)
2. 對每檔 ticker 自動彙整素材：
   * MOPS 重大訊息近一年
   * 鉅亨網今日新聞 (含此股代號的)
   * **DuckDuckGo HTML 搜尋** + **Google News RSS** (多個 query 聚合)
   * 部分高品質網頁抓主要原文
   * 此股近一年舉行過 / 未來 60 天即將舉行的法說會
3. 餵 Gemini `research_ticker` prompt → 結構化 JSON
4. 若有籌碼面資料 → 自動跑 `logic_check` 言行反查
5. 結果寫到 `data/auto_llm/<ticker>.json` 並 append `data/auto_llm/research_log.jsonl`

每個 ticker 12 小時內不會重複打 API；可用 `--refresh` 強制。

### 📅 法說會行事曆自動快取

```bash
uv run stock-calendar-update                # MOPS 法說會 + 全球科技事件
uv run stock-calendar-update --global-only  # 只更新全球科技事件
uv run stock-calendar-update --upcoming 14  # 抓完印出未來 14 天的法說會與全球事件
```

結果存在：
- `data/calendar/conferences_<YYYY-MM>.json` — MOPS 法說會
- `data/calendar/global_tech_events.json` — 科技巨頭發表會 (Apple / Microsoft / Google / Samsung / Meta / AWS / NVIDIA / AMD) + CES/MWC/GTC，自動抓取官方頁與新聞關鍵字，並映射 `supply_chain.json` 台股供應鏈

dashboard「台股行事曆」、`stock-auto-research --llm-only`、明日當沖關注與 LLM 個股研究都會使用這些快取。
全球科技事件預設 12 小時內視為新鮮 (可用 `GLOBAL_EVENTS_MAX_AGE_HOURS` 調整)。

單次／替代排程範例（若已啟用常駐 `stock-scheduler`，不要再重複排這組命令）：
```
uv run stock-calendar-update --upcoming 14
uv run stock-auto-research --llm-only --upcoming
```

dashboard 「LLM 法說分析」頁進入時也會自動 ensure 行事曆「24 小時內」是新鮮的；
過期才重抓，可手動點 「🔄 立即重抓」按鈕強制。

### 🤖 哪些操作會呼叫 LLM (消耗 Gemini API 額度)？

> 所有「會打 Gemini」的按鈕在 dashboard 都已加上 **🤖** 前綴，
> 滑鼠懸停 (`help=`) 還會顯示詳細消耗說明。側欄會顯示今日累計 LLM 呼叫筆數與 tokens。

| 觸發點 | 操作 / 按鈕 | 呼叫類型 | 大致次數 |
|--------|------------|---------|---------|
| 🤖 LLM 法說分析 (頁) | 用 Gemini 分析 / 執行反查 | **直接** | 1-2 次 / 按 |
| 🤖 自動化管線 (頁) | 立即執行管線 (LLM toggle 開) | **直接** | 數次 ~ 數十次 (依焦點數) |
| 🤖 主動 ETF 追蹤 (頁) | 立即抓取所有 (有 URL 的) | **直接** | 每檔 ETF 1 次 |
| 🤖 今日當沖戰情室 (頁) | 重抓新聞 + 重跑 / 用快取重跑 | **直接** | 至少 2 次 (theme_radar + intraday_brief) |
| 🤖 明日當沖關注 (頁) | 跑 draft (盤後) / 跑 update (凌晨) | **直接** | 至少 2 次 (next_day_radar + next_day_brief) |
| 🤖 美股 / 跨市場 (頁) | 呼叫 Gemini 產出簡報 | **直接** | 1 次 |
| 🤖 個股深入分析 (頁) | 分析 / 強制重抓全部資料 | **隱式自動** | 該檔缺 LLM 法說時自動 1 次 (12h 快取) |
| 🤖 個股總覽 (頁) | 計算評分 | **隱式自動** | 對每檔缺 LLM 法說的個股各 1 次 |
| `stock-auto-research` (CLI) | 整段流程 | **直接** | 焦點檔數 + 1 (每日簡報) |
| `stock-intraday` (CLI) | 整段流程 | **直接** | 至少 2 次 |
| `stock-nextday` (CLI) | `--mode draft / update` | **直接** | 至少 2 次 (next_day_radar + next_day_brief) |
| `stock-macro-update --brief` (CLI) | `--brief` flag | **直接** | 1 次 |
| Prompt 管理 / LLM 呼叫紀錄 (頁) | 編輯 / 檢視 | ❌ 不呼叫 | 0 |
| 其他頁面 (組態/資料庫/風控/報表/...) | — | ❌ 不呼叫 | 0 |

**控管方式**：

* **想完全不消耗 LLM**：清空 `.env` 的 `GEMINI_API_KEY`。
  所有自動 LLM 都會 graceful-skip，「直接」按鈕會被 disable 或退回純規則式。
* **想知道實際用了多少**：到 dashboard 「⚙️ LLM 呼叫紀錄」頁
  按日期看每筆 input / output / 延遲 / tokens；側欄會即時顯示今日累計。
* **想避免重複呼叫**：自動 LLM 預設 12 小時內共用快取
  (`data/auto_llm/<ticker>.json`)，重複按「分析」不會重打。

### 一鍵自動化研究管線

把所有功能串成單一 pipeline：

```bash
uv run stock-auto-research                       # 用 .env 預設執行全部步驟
uv run stock-auto-research --no-etf              # 跳過 ETF 抓取
uv run stock-auto-research --pdf 2330=tsmc.pdf   # 額外送一份法說會 PDF
```

執行流程：
0. **法說會行事曆自動更新** — 抓 MOPS 上月/本月/下月/+2 月，
   並把「未來 14 天有法說會」的個股全部納入焦點清單
1. **ETF 持股自動抓取** — 依 `data/active_etfs.json` 中每檔 ETF 的 `holdings_url`
   抓網頁，餵給 Gemini 的 `extract_etf_holdings` prompt 抽出結構化 JSON，
   存為 `data/etf_holdings/<symbol>/<YYYY-MM-DD>.csv`
2. **共識計算** — 計算 Top 共識持股、共識新建倉、共識加碼
3. **焦點個股** — 取「被 ≥N 檔 ETF 持有」+「新建倉/共識加碼」
   +「未來 14 天有法說會」+ 使用者額外指定
4. **籌碼面自動拉取** — 對每個焦點個股呼叫 TWSE OpenAPI，
   取近 N 日外資/投信/自營商/借券/融資/鉅額交易
5. **法說會 LLM 解析** — 若有提供逐字稿/PDF，呼叫 `analyze_presentation` prompt
5a. **🤖 自動研究 (新)** — 對所有焦點個股自動「行事曆 + 上網搜尋 + LLM」，
   不需要逐字稿；結果寫到 `data/auto_llm/<ticker>.json`
6. **言行反查** — 配對 (法說情緒, 籌碼動向) 呼叫 `logic_check`
7. **每日簡報** — 把所有結果送進 `daily_brief` prompt，產出 Markdown 報告

所有結果寫到 `data/pipeline_runs/<timestamp>/run.json` + `daily_brief.md`，
並 append 一筆到 `data/pipeline_runs/manifest.jsonl`。

### ⚡ 今日當沖戰情室 (intraday_pipeline.py)

**「先定主題 → 找股 → 排序」的盤前選股流程**。專為「今天當沖」設計：

```
[鉅亨網新聞 200 條] + [美股盤後] + [強勢類股]
                │
                ▼
        ┌────────────────┐
        │ LLM theme_radar │ → 5-8 個今日熱門題材 + 對應台股代號
        └────────────────┘
                │
                ▼
    候選股池 (合併: 題材股 + 供應鏈 + ETF 共識 + watchlist)
                │
                ▼
    對每檔算 day_trade 分 (美股 18% + 技術 42% + 籌碼 18% + 風險 10%)
                │
                ▼
        ┌────────────────┐
        │ LLM intraday_brief│ → 戰情簡報 (大盤定調 + Top 5 + 規則)
        └────────────────┘
```

**CLI (建議排程：每天 08:30)**

```bash
uv run stock-intraday                  # 用快取
uv run stock-intraday --refresh-news   # 強制重抓新聞
uv run stock-intraday --limit 30       # 候選 30 檔
```

**儀表板 → 研究與分析 → 今日當沖戰情室**：
- 上方：市場氛圍 (risk_on/neutral/risk_off) + 總結簡報
- 中段：5-8 張題材卡 (熱度星級 + 驅動原因 + 標的清單 + 風險)
- 下段：候選股表格 (當沖分 progress bar + 美股連動 + ADR 溢價%)
- 底部：完整 LLM 戰情簡報 (Markdown)

**新增模組**:
- `src/bot/news_fetcher.py` — 鉅亨網 API + 每日快取 + 自動偵測股票代號
- `src/bot/intraday_pipeline.py` — 主流程編排
- `src/bot/intraday_cli.py` — `stock-intraday` 入口
- `prompts/theme_radar.yaml` — LLM 萃取題材 (輸出 JSON)
- `prompts/intraday_brief.yaml` — LLM 寫戰情簡報 (輸出 Markdown)

---

### 🌙 明日當沖關注 (next_day_watch_pipeline.py)

**定位**：與「今日當沖戰情室」互補。今日當沖在盤前 08:30 跑，
明日當沖關注則在 **盤後 14:00-18:00**（draft 初版）與 **隔日凌晨 02:00-06:00**（update 更新版）跑，
專為「明日當沖預備」設計，綜合三大候選來源：

1. **題材延續 (carry_themes)** — 沿用今日 / 今晚熱門題材，挑明日續熱的補漲、二線、設備代工
2. **強勢承接 (strong_carry)** — 今日收盤漲幅 > 0、量比放大、外資/投信買超的個股 (規則層自動掃描 + LLM 篩選)
3. **明日事件 (event_focus)** — 法說 / 財報 / 權息 / 全球科技 (WWDC/GTC) / 國際展覽對應受惠股 (來源：`conference_calendar` + `global_tech_events` + 新聞)

```
[今日新聞] + [今日 K 線/籌碼掃描 (watchlist + ETF + 明日法說)] + [美股 macro] + [明日法說]
                │
                ▼
        ┌───────────────────┐
        │ LLM next_day_radar │ → carry_themes / event_focus / strong_carry
        └───────────────────┘
                │
                ▼
    合併池 + 算 next_day_score (50% 強勢 + 30% 題材熱 + 20% 事件加分)
                │
                ▼
        ┌────────────────────┐
        │ LLM next_day_brief │ → 明日預備清單 (定調 + 三大焦點 + Top 6 + 進場規則)
        └────────────────────┘
```

**CLI (建議排程：每天 14:30 跑 draft、隔日 02:30 跑 update)**

```bash
uv run stock-nextday                       # 預設 draft (盤後初版)
uv run stock-nextday --mode update         # 凌晨更新版 (自動 force_refresh_macro)
uv run stock-nextday --refresh-news        # 強制重抓新聞
uv run stock-nextday --limit 30            # 排序輸出 30 檔
uv run stock-nextday --scan-limit 80       # 強勢承接掃描範圍 80 檔
```

**儀表板 → 研究與分析 → 明日當沖關注**：
- 三顆按鈕：載入最新 / 跑 draft / 跑 update
- 上方：目標明日交易日 + 模式 + 市場氛圍
- 中段：三大來源分頁 (題材延續 / 明日事件 / 強勢承接 LLM)
- 下段：候選股表格 (明日分 / 強勢分 / 今日% / 量比 / 題材 / 事件 / 進場邏輯)
- 底部：完整 LLM 預備清單簡報 (Markdown)

**輸出**：
- `data/next_day_watch/<明日日期>/report.json` (draft)
- `data/next_day_watch/<明日日期>/report_update.json` (update)
- `data/next_day_watch/<明日日期>/next_day_brief.md` / `next_day_brief_update.md`

**新增模組**:
- `src/bot/next_day_watch_pipeline.py` — 主流程編排 + next_day_score 規則層
- `src/bot/next_day_watch_cli.py` — `stock-nextday` 入口
- `prompts/next_day_radar.yaml` — LLM 萃題材/事件/強勢承接 (JSON)
- `prompts/next_day_brief.yaml` — LLM 寫明日預備清單 (Markdown)

---

### 美股 / 跨市場連動 (market_macro.py)

把「**美股盤後 → 隔日台股早盤**」的資訊鏈整進系統，由三層組成：

1. **資料層** (`market_macro.py`)
   - 用 yfinance (免註冊免 API key) 抓 **S&P 500 / NASDAQ / Dow / 費半 SOX / VIX / 加權指數** 與 **NVDA / AMD / AAPL / MSFT / GOOGL / META / AMZN / TSLA / ASML / AVGO / MU + TSM/UMC/ASX ADR**
   - 自動算 **ADR 公允台股價 + 溢價/折價%** (TSM=1:5、UMC=1:5、ASX=1:2，USDTWD 由 `TWD=X` 即時抓)
   - 每日快取於 `data/macro/macro_YYYY-MM-DD.json`，避免重複打 API

2. **對照層** (`data/supply_chain.json`)
   - 本機 JSON：美股代號 → 台股供應鏈條目（含 `weight`／`role`），供 us_market factor 與跨市場頁編輯
   - 與 `prompts/archive/supply_chain_impact.yaml` **不同**：後者是尚未接線的 LLM 設計稿（見 `prompts/archive/README.md`），不是本檔的管線實作
   - 可在儀表板「美股 / 跨市場」頁直接編輯 JSON 並儲存

3. **分析層 (LLM)**
   - `prompts/us_market_brief.yaml` — 把美股盤後資料 + 供應鏈對照丟給 Gemini，產出「對台股早盤影響」中文簡報，含偏多/偏空標的清單
   - `prompts/archive/supply_chain_impact.yaml` — 已封存設計稿；尚無美股→台股供應鏈 LLM 管線接線
   - `prompts/archive/analyst_real_sentiment.yaml` — 已封存設計稿；外資報告語意流程未建置

**CLI 排程化**

```bash
# 僅抓 macro 資料 (建議排程：每天 06:00 美股收盤後)
uv run stock-macro-update

# 抓 + LLM 跨市場簡報
uv run stock-macro-update --brief

# 強制重抓 (繞過日快取)
uv run stock-macro-update --no-cache --brief
```

Windows Task Scheduler 範例：每天 06:30 跑一次 `uv run stock-macro-update --brief` (cwd 設專案根目錄)。

---

### 持續自動更新 + 盤中監測 (stock-scheduler)

讓資料更新與盤中監測**持續在背景自動跑**，不必開著儀表板：

```bash
uv run stock-scheduler            # 常駐執行 (Ctrl+C 結束)
uv run stock-scheduler --once     # 到期任務各跑一次即結束 (搭配 Windows 工作排程器)
uv run stock-scheduler --dry-run  # 只印排程計畫，不實際執行
```

- **macro** 任務：定期呼叫 `stock-macro-update` 刷新行情/總經 (預設每 30 分，不打 LLM)。
- **research** 任務：定期呼叫 `stock-auto-research` 跑完整研究管線 (預設每 240 分，含 LLM 簡報)。
  預設每輪最多完整研究 12 檔缺少或過期快取的個股，並輪替延後名單；12 小時有效快取會略過。
  可用 `SCHEDULER_RESEARCH_MAX_TICKERS` 調整，設為 `0` 代表不設上限。也會回頭重抓**新掛牌 ETF**持股。
- **company** 任務：定期呼叫 `stock-company-update` 補齊公司基本資料 (名稱/**產業別**/上市日，預設一天一次)。
  解決持股分析與查資料頁面大量顯示「未分類」、名稱空白的問題。
- **fundamentals** 任務：定期呼叫 `stock-fundamentals-refresh` 慢速刷新基本面佇列 (預設每 360 分)。
- **cloud_sync** 任務：定期呼叫 `stock-cloud-sync` 同步 Google Sheets (需設定 `GOOGLE_*`，見 `docs/cloud_sync_setup.md`)。
- **monitor** 托管 (選用，`SCHEDULER_SUPERVISE_MONITOR=true`)：開盤自動啟動 `stock-bot` 監測子行程、收盤自動停止。
- 各任務間隔、是否只在交易時段執行，皆由 `.env` 的 `SCHEDULER_*` 控制 (見 `.env.example`)。
- 排程器以作業系統檔案鎖避免第二個程序重複執行；Windows 常駐工作設定見 [docs/automation-plan.md](docs/automation-plan.md)。

> 請在「登入時常駐 daemon」與「外部定時觸發 `--once`」兩種模式中擇一。此 Windows 環境採用登入時常駐；不要同時設定兩種觸發。

### 零股下單流程測試 (stock-oddlot-test)

在 **Shioaji 模擬環境**下完整跑一次「盤中零股」買→查→賣流程，驗證下單程式碼路徑，
**不會送出任何真實委託** (強制 `simulation=true`，偵測到非模擬即中止)：

```bash
uv run stock-oddlot-test                      # 預設 2330，買賣各 10 股
uv run stock-oddlot-test --symbol 0050 --shares 5
uv run stock-oddlot-test --no-sell            # 只測買進
```

> 註：若 API 金鑰僅有「行情(Data)」權限，下單會回 401「Token doesn't have permission」，
> 此時需到永豐 Shioaji API 後台為金鑰開通「下單」權限 (程式流程本身已驗證正常)。

**儀表板「美股 / 跨市場」頁**：六個指數 KPI、美股漲跌排序表、ADR 溢價表、LLM 簡報按鈕、供應鏈 JSON 編輯器。

**個股深入分析「🌐 美股連動」分頁**：自動列出此股對應的美股客戶與當夜表現、加權連動 %、若自身有 ADR 則顯示溢價分析。

---

### 評分量表系統 (scoring.py)

每檔股票對 **四個時間框架** 都會算一個 0-100 分：

| 時間框架 | 持有期 | 主要 factor 權重 | 停損/停利 |
|---------|-------|------------------|-----------|
| 當沖    | 當日 | 技術 42% / **美股 18%** / 籌碼 18% / 集保 7% / ETF 5% / 風險 10%               | -1%/+2%  |
| 短期    | 1-2 週 | 技術 22% / **美股 15%** / 籌碼 18% / 集保 10% / ETF 12% / 基本面 10% / LLM 8% / 風險 5% | -4%/+8%  |
| 中期    | 1-3 月 | 基本面 20% / LLM 16% / 言行 14% / ETF 16% / **美股 10%** / 集保 8% / 籌碼 6% / 技術 5% / 風險 5% | -8%/+20% |
| 長期    | >3 月 | 基本面 28% / LLM 22% / 言行 18% / ETF 17% / **美股 5%** / 集保 5% / 風險 5%      | -15%/+40% |

九個 factor 詳細邏輯在 `src/bot/scoring.py`：
* `llm_sentiment` — Gemini 法說情緒分 + confidence 混合
* `logic` — 言行反查結果 (`logic_check`：法說語意 vs 籌碼面)
* `etf_consensus` — 持有 ETF 檔數 + 共識新建倉/加碼加分
* `chips` — 三大法人累計 + 借券/融資扣分項
* `technical` — MA/MACD/RSI/KD/量比 (技術面 snapshot)
* `fundamental` — 月營收 YoY 連續 + PER + ROE/三率
* `distribution` — TDCC 集保戶大戶 vs 散戶結構
* **`us_market`** — 美股供應鏈夥伴當夜表現 + SOX + VIX + ADR 溢價
* `risk` — 借券暴增、融資爆量、LLM 標記的 risk

當沖框 us_market 權重最高，原因：美股 (尤其費半) 直接決定隔日台股早盤跳空方向。

每張時間框卡片會在 UI 顯示：總分、建議行動 (`強烈買進` / `買進` / `觀望` / `減碼` / `賣出`)、
各 factor 分數明細、進場/停損/停利/部位/盈虧比/進場邏輯文字。

### 儀表板導覽（權威現況）

側欄分組以 `src/bot/dashboard/nav.py` 的 `NAV_GROUPS` 為準（摘要）：

```
🔍 研究與分析  → 功能總覽 / 今日當沖戰情室 / 今日當沖即時追蹤 / 明日當沖關注 / K 線看板 / 盤中強弱排行 / 個股總覽 / 目前持股分析 / 個股深入分析 / 自動化管線
📡 監控與訊號  → 美股 / 跨市場 / 熱門個股期貨 / 跟單訊號 / 主動 ETF 追蹤 / LLM 法說分析 / 台股行事曆
⚡ 執行與紀錄  → 啟動 / 監控 / 模擬交易 / 風控中心 / 交易可行性檢查 / 交易紀錄 / 報表分析
⚙️  系統與診斷  → 組態設定 / 資料庫雲端 / Prompt 管理 / LLM 呼叫紀錄 / 記憶與證據 / 日誌 / 通知 / 文件
```

頁面計數與名稱請以程式掃描結果或 `docs/reports/system_capability_report.html` 為準，勿依賴本 README 舊表。

### Prompt 管理 (prompts/*.yaml)

所有對 LLM 的 prompt 都集中在 `prompts/` 目錄。完整清單與狀態（active / planned / archived）見 **[prompts/README.md](prompts/README.md)**。

在儀表板「Prompt 管理」頁可直接編輯 YAML 並即時生效；每筆 LLM 呼叫的
`input` / `output` / `latency_ms` / `tokens_in` / `tokens_out` 都會自動寫入
`log/llm_calls/llm_calls_YYYY-MM-DD.jsonl`，可在「LLM 呼叫紀錄」頁追溯與審計。

## 專案結構

```
src/bot/
  main.py                  # 程式進入點 (多模式分流 + 策略切換)
  config.py                # 組態管理 (pydantic-settings + .env)
  broker.py                # Shioaji 連線管理 (登入/行情/下單/斷線重連)
  strategy.py              # 策略引擎 (BaseStrategy)
  strategy_configurable.py # ConfigurableStrategy (預設當沖)
  strategy_etf_follow.py   # 主動 ETF 共識跟單策略
  models.py                # 資料模型 (PositionInfo, MarketTick, SignalEvent)
  market_source.py         # TWSE 公開延遲行情來源
  signal_recorder.py       # 訊號記錄與報表匯出 (watch/report)
  recorder.py              # 交易紀錄收集與 CSV 匯出 (trade)
  notifier.py              # Telegram 推播通知
  utils.py                 # 工具函數 (Logger, 時間)
  active_etf.py            # 主動式 ETF 清單與持股資料模型（含 ALI006 共同基金）
  fund_holdings_parser.py  # MoneyDJ 共同基金持股確定性解析（免 LLM）
  etf_consensus.py         # 共識持股 / 加碼 / 抬轎候選計算
  mops_scraper.py          # MOPS 法說會 / 重大訊息爬蟲
  llm_analyzer.py          # Gemini LLM 法說會語意解析 + 邏輯反查
  prompt_registry.py       # Prompt YAML 載入 / 渲染 / 儲存
  llm_log.py               # 所有 LLM 呼叫的 JSONL 紀錄
  etf_holdings_fetcher.py  # ETF 持股自動抓取 (HTTP + LLM 抽取)
  chips_fetcher.py         # TWSE 籌碼面自動拉取 (三大法人/借券/融資)
  chip_distribution.py     # TDCC 集保戶股權分散 (大戶 vs 散戶)
  fundamentals_fetcher.py  # TWSE 月營收/PER/PBR/殖利率/股利 + 季報手動匯入
  technicals.py            # TWSE 日K + MA/MACD/RSI/KD/布林 + 訊號摘要
  candle_patterns.py       # 單根 K 棒型態辨識 (16 種型態 + 偏多偏空 + 市場訊號)
  quarterly.py             # Q1-Q4 季度框架 + 滾動 EPS + 季度營收聚合
  market_macro.py          # 美股/加權/VIX/ADR 溢價抓取 + 供應鏈對照查詢 (yfinance)
  macro_update.py          # stock-macro-update CLI (抓 macro + 可選 LLM 簡報)
  web_search.py            # 免註冊網頁搜尋 (DuckDuckGo HTML + Google News RSS) + 抓網頁文字
  conference_calendar.py   # 法說會行事曆自動抓取與本地快取 (上月/本月/下月/+2 月)
  conference_calendar_cli.py # stock-calendar-update CLI 入口
  auto_llm.py              # 全自動 LLM 個股研究 (行事曆 + 搜尋 + 新聞 + LLM + 反查)
  preflight.py             # 交易可行性檢查邏輯 (七大區塊體檢)
  position_safety.py       # 啟動安全檢查 (混倉重疊 / 對帳)
  preflight_cli.py         # stock-preflight CLI 入口
  risk_guard.py            # 資金/風險守門員 (12 道閘門 + Kill Switch)
  data_pipeline.py         # 自動化研究管線編排器 (含 macro + us_brief 步驟)
  auto_research.py         # stock-auto-research CLI 進入點
  scoring.py               # 評分量表系統 (4 時間框 × 9 factor，含美股連動)
  ticker_view.py           # 個股 360 度 TickerSnapshot 整合 (含 macro/us_related/adr)
  watchlist.py             # JSON-backed 個股 Watchlist 管理
  dashboard.py             # Streamlit 入口 shim（轉呼叫 bot.dashboard）
  dashboard/               # 實際頁面與導覽（nav.py 為頁面權威清單）
  env_io.py                # 儀表板的 .env 讀寫/驗證工具
  process_runner.py        # 儀表板的 stock-bot 子行程管理 (啟動/停止/tail log)
  stock_db.py              # 集中管理冷/溫資料的 SQLite 資料庫 + DAO (含 price_history) (Cache-Aside)
  cloud_sync.py            # Google Sheets 雙向同步 (push / pull / 智能 sync，含 price_history)
prompts/                   # 見 prompts/README.md（含 planned / archive）
prompts/archive/           # 已封存、不被 PromptRegistry 載入的設計稿
data/
  supply_chain.json            # 美股 → 台股供應鏈對照表（資料層；非 archived prompt）
  macro/                       # 每日美股/ADR 快取 JSON
tests/                     # 代表性單元／整合測試（完整清單以 tests/ 目錄為準，勿以本表為完整 inventory）
  test_config.py           # Settings 解析測試
  test_strategy.py         # 策略模式測試
  test_capability_report.py # 能力盤點報告離線／audit-json 契約
  ...                      # 其餘模組測試見 tests/
```

## K 線看板 (Stock Board)

「📊 K 線看板」頁面把多檔股票的 K 線一次攤開，省去逐檔點開的步驟。重構後支援多種看板模式、長時段歷史以及批次往前抓取。

### 三種顯示模式

| 模式 | 適用情境 |
|------|----------|
| **縮圖 grid** (預設) | 多檔一覽，每檔 mini 蠟燭縮圖；2/3/4/6 欄可調 |
| **並排大圖** | 選 2~4 檔做完整 K + 量比較；可手動 Y 軸統一刻度 |
| **單檔專注** | 進入「完整 K 線工作台」，含時間範圍/Y軸/面板選擇/往前抓取等所有控制 |

### 時間範圍 (10 段)

`近 1 個月` / `近 3 個月` / `近 6 個月` (預設) / `近 1 年` / `近 2 年` / `近 3 年` / `近 5 年` / `近 10 年` / `全部` / `自訂日期`。
單檔專注模式 (含「個股深入分析 → 技術面」) 可選上述含 **自訂起訖日** 的完整 10 段；K 線看板縮圖／並排模式則不含「自訂日期」。

### 面板選擇

可任選顯示：📈 K 線 + 均線 / 📊 成交量 / MACD / RSI / KD / 布林通道。
排列方式三選一：**堆疊** (預設) / **分頁** / **並排兩欄**。

### Y 軸範圍

預設自動 (`Scale(zero=False)`)，需要放大細節時切「手動」可填入 min/max。
並排大圖模式亦支援統一 Y 軸，方便跨檔比較。

### 一鍵更新所有 K 線 (分月抓取)

選清單後點「🔄 一鍵更新所有 K 線」，可選範圍 3 個月 / 6 個月 / 1 年 / 2 年 / 3 年 / 5 年 / 10 年。
底層呼叫 `fetch_kline_range()` **分月** 抓 + 進度條，新增資料同步寫進 `price_history` 與 `data/technicals/<ticker>/daily_kline.csv`。

### ⏪ 批量往更早抓取 (5/10 年歷史)

不想一次抓滿 10 年？用「批量往更早抓取」展開區：

- **往前 N 年**: 1 / 2 / 3 / 5 / 10
- **每月請求間隔**: 0.2~2.0 秒 (預設 0.5 s，避免 TWSE rate limit)
- **進度顯示**: 外圈是「目前抓到第幾檔」，內圈是「該檔抓到第幾個月」，可隨時關閉視窗
- **斷點續抓**: 已存在的月份預設跳過 (邊界月仍會重抓，保證資料一致)

實際抓取邏輯：

```text
fetch_kline_range(ticker, start_date, end_date,
                  direction="backward", request_delay_sec=0.5,
                  on_progress=cb)
```
分月迴圈 + `time.sleep(delay)` + 進度回呼 + 自動合併到既有 CSV/DB。

```python
from bot.technicals import (
    fetch_kline_range, extend_kline_backward, get_kline_coverage,
)
cov = get_kline_coverage("2330")  # {'earliest': '2024-01-02', 'latest': '2026-05-28', 'rows': 580}
# 從現有最早日期再往前抓 5 年
extend_kline_backward("2330", years_back=5, request_delay_sec=0.6)
```

底層儲存：所有 K 線存在 `data/stock.db` 的 `price_history` table，schema 為
`(symbol, date) PK + open / high / low / close / volume / source / note / updated_at`。
TWSE 抓回來的 K 線會同時寫進 `data/technicals/<ticker>/daily_kline.csv` 與 DB，
舊有 CSV 流程仍可用。

```python
from bot.stock_db import StockDB, default_db_path
db = StockDB.open(path=default_db_path())
bars = db.get_price_history("2330", limit=120)
print(bars[-1].close, bars[-1].date)
```

### 把 K 線同步到 Google Sheets

`price_history` 已是 `SYNCABLE_TABLES` 之一，啟用 Google Sheets 同步後 (見下一節)，
任何一台機器都可：

* `K 線看板 → ⬆ Push price_history 到 Sheets`：本地覆蓋雲端
* `K 線看板 → ⬇ Pull price_history (覆蓋本地)`：把另一台機器的 K 線抓回來
* `資料庫 / 雲端同步 → 智能同步`：依 `updated_at` 自動決定方向

> 注意：歷史 K 線的單檔量可達數百筆；建議監控池在 20 檔以下，初次 push 約 1-3 秒/檔。

## 雲端同步 (多機共用資料)

把冷/溫資料 (公司基本面 / 監控清單 / 月營收 / 季報 / **歷史 K 線**) 在多台電腦間共用：

```bash
# 安裝雲端同步套件
uv sync --extra cloud
```

**本機備份（建議先做）**：

```bash
uv run stock-db-backup              # → data/backups/stock_YYYYMMDD_HHMMSS.db
uv run stock-db-backup --json       # CI / 腳本用
uv run stock-db-backup --root C:\\skymiku\\stock --json  # 從其他 CWD 執行
```

`STOCK_DB_PATH`（若設定）會被所有 CLI／dashboard／管線統一使用；未設定則為 `data/stock.db`。

**模式 A — Google Drive 掛載 DB**（不建議日常；兩機不可同時開同一檔）：
把 `.env` 的 `STOCK_DB_PATH` 指向 Drive 同步資料夾即可。

```
STOCK_DB_PATH=G:/My Drive/stock-shared/stock.db
```

> 注意：兩台電腦不要同時開啟同一個 DB 檔，否則可能損毀。較安全做法是本機 DB + `stock-db-backup` + 模式 B。

**模式 B — 多機並行** (推薦)：
用 Google Sheets 當源頭，本地 SQLite 當快取。
適合「家中 PC + 公司筆電 + 手機 Forms 新增股票」這種情境。

完整設定步驟 (Google Cloud / Service Account / Forms) 請見
**[docs/cloud_sync_setup.md](docs/cloud_sync_setup.md)**。

設定完成後，到 dashboard「⚙️ 系統與診斷 → 資料庫 / 雲端同步」
可一鍵 Push / Pull / 智能同步，並瀏覽所有 table 內容與最近同步狀態。

## 個股 360 度分析框架

dashboard 的「個股深入分析」頁採用 **3D + 催化劑** 視角，把實務市場常用的研究流程拆解為：

### 一、基本面 (Fundamental)：尋找好公司與內在價值

| 量表 / 圖表 | 對應模組 | 來源 |
|------------|---------|------|
| 每股盈餘 EPS / 季度三率 | `fundamentals_fetcher.QuarterlyFinancials` | 手動匯入 `data/fundamentals_manual/<ticker>.json` |
| 本益比 PER / 股價淨值比 PBR / 現金殖利率 | `fundamentals_fetcher.ValuationDaily` | TWSE OpenAPI `BWIBBU_ALL` |
| 月營收 + YoY/MoM + 累計年增率 | `fundamentals_fetcher.MonthlyRevenue` | TWSE OpenAPI `t187ap05_L` |
| 歷年股利 + 盈餘分配率 + 填息天數 | `fundamentals_fetcher.DividendRecord` | TWSE OpenAPI `t187ap46_L_ex` + 季報衍生 |
| 三率走勢圖 / 月營收走勢圖 (含 YoY 線) | dashboard 基本面分頁 | Altair |

### 二、技術面 (Technical)：決定進出場時機

| 量表 / 圖表 | 對應模組 | 來源 |
|------------|---------|------|
| MA(5/10/20/60/120)、量比 | `technicals.add_moving_averages` | TWSE `STOCK_DAY` |
| MACD(12,26,9) + 黃金/死亡交叉 | `technicals.add_macd` + `derive_signals` | 同上 |
| RSI(14) + 超買超賣 | `technicals.add_rsi` | 同上 |
| KD (Stochastic 9,3,3) | `technicals.add_kd` | 同上 |
| 布林通道 (20, 2σ) | `technicals.add_bollinger` | 同上 |
| 日 K + 均線 + 成交量 + MACD + RSI + KD + 布林多面板圖；可自訂時間範圍 (1m~10y / 自訂日期) 與 Y 軸範圍 | dashboard 技術面分頁 K 線工作台 | Altair |

### 三、籌碼面 (Chip)：追蹤主力資金流向

| 量表 / 圖表 | 對應模組 | 來源 |
|------------|---------|------|
| 三大法人買賣超 (外資/投信/自營商) | `chips_fetcher.ChipDailyRow` | TWSE T86 |
| 借券賣出、融資融券餘額變動 | 同上 | TWSE TWT93U / MI_MARGN |
| 鉅額交易淨額 | 同上 | TWSE BFIAUU |
| **大戶 / 超大戶 / 散戶持股比例** | `chip_distribution.DistributionWeekly` | **TDCC 集保戶股權分散表** |
| 大戶結構解讀 (吸籌 / 出貨 / 中性) | `chip_distribution.interpret_distribution` | 週度趨勢計算 |
| 大戶 vs 散戶趨勢線圖 | dashboard 籌碼面分頁 | Altair |
| 主動 ETF 共識持有與加碼 | `etf_consensus` | 27 檔 `*A` + ALI006（共 28） |

### 四、配股配息：衡量資金回報與成熟度

| 量表 / 圖表 | 對應模組 | 來源 |
|------------|---------|------|
| 每年現金/股票股利 + 合計 | `fundamentals_fetcher.DividendRecord` | TWSE |
| 盈餘分配率 (`payout_ratio`) | `_enrich_dividends_with_payout` | 季報 EPS 衍生 |
| 填息天數 (`fill_days`) | dashboard 股利分頁 | TWSE 或手動補 |
| 歷年股利穩定度 (CV)、近 5 年平均分配率 | dashboard 股利分頁 | 衍生計算 |

### 五、法說會 (Catalyst)：產業前景望遠鏡

| 量表 / 圖表 | 對應模組 | 來源 |
|------------|---------|------|
| 經營階層展望、Capex 信號、毛利率展望 | `llm_analyzer.analyze_presentation` | Gemini 解析法說會 |
| 風險點、key_metrics | 同上 | 同上 |
| 言行反查 (法說 vs 籌碼) | `llm_analyzer.logic_check` | Gemini |
| 法說會行事曆、重大訊息 | `mops_scraper` | MOPS |

### 六、Q1～Q4 季度紀錄：基本面落實的計分板

| 量表 / 圖表 | 對應模組 | 用途 |
|------------|---------|------|
| 滾動式累計 EPS (Q1/H1/9M/FY) | `quarterly.rolling_eps_series` | 抓「9M EPS > 去年全年」訊號 |
| 季度營收聚合 (從月營收) | `quarterly.revenue_quarterly_aggregate` | YoY 比較 |
| 當期市場焦點框架 (作夢/淡季/旺季/年報) | `quarterly.quarter_focus` | 依日期動態推估 |
| 滾動 EPS 條形圖 (今年 vs 去年同期) | dashboard 季報分頁 | Altair |

### 綜合分析邏輯（評分系統）

`scoring.py` 將多面向資料壓縮為 **九個 0-100 分 factor**，並依時間框架做加權（完整權重見上方「評分量表系統」章節）：

| Factor          | 程式 ID | 當沖 | 短期 | 中期 | 長期 |
|-----------------|---------|:----:|:----:|:----:|:----:|
| 技術面           | `technical` | 42% | 22% |  5% |  —  |
| 美股連動         | `us_market` | 18% | 15% | 10% |  5% |
| 三大法人         | `chips` | 18% | 18% |  6% |  —  |
| 大戶結構 (TDCC) | `distribution` |  7% | 10% |  8% |  5% |
| ETF 共識        | `etf_consensus` |  5% | 12% | 16% | 17% |
| 基本面           | `fundamental` |  —  | 10% | 20% | 28% |
| 法說語意         | `llm_sentiment` |  —  |  8% | 16% | 22% |
| 言行一致性       | `logic` |  —  |  —  | 14% | 18% |
| 風險警示         | `risk` | 10% |  5% |  5% |  5% |

> 評分量表的停損/停利建議為**分析參考**，與 bot 自動下單參數不同。見 [docs/glossary.md](docs/glossary.md)。

對應到 `STRONG_BUY / BUY / HOLD / REDUCE / SELL` 五級建議，
搭配每個時間框架的進場/停損/停利規則。

## 手動補季度 EPS / 三率資料

由於 MOPS 季報需要登入或反爬蟲，本專案讓使用者把季度資料手動匯入：

```jsonc
// data/fundamentals_manual/2330.json
{
  "ticker": "2330",
  "quarterlies": [
    {
      "year": 2025, "quarter": 3,
      "eps": 14.71,
      "gross_margin": 59.1,
      "operating_margin": 48.5,
      "net_margin": 41.0,
      "revenue": 989000000,
      "operating_income": 480000000,
      "net_income": 405000000,
      "roe": 8.2
    },
    { "year": 2025, "quarter": 2, "eps": 13.50, "gross_margin": 58.6, "operating_margin": 47.9, "net_margin": 40.5, "roe": 7.9 },
    { "year": 2025, "quarter": 1, "eps": 12.10, "gross_margin": 56.2, "operating_margin": 45.8, "net_margin": 38.1, "roe": 7.1 }
  ]
}
```

存好後重整 dashboard，即可在「基本面 / 季報 Q1-Q4」分頁看到三率走勢、滾動 EPS 與 Q1~Q4 框架。

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

然後在 `main.py` 依 `STRATEGY_TYPE` 分支加入你的策略類別，或擴充現有 `configurable` / `etf_follow`。
三種模式皆可使用，watch/report 模式會自動以虛擬成交記錄訊號。

## 策略規劃文件

- **[docs/setup.md](docs/setup.md)** -- 安裝與開戶設定（權威安裝指南）
- **[docs/architecture.md](docs/architecture.md)** -- 系統架構與模組職責
- **[docs/operations.md](docs/operations.md)** -- 日常運維、排程與 log
- **[docs/automation-plan.md](docs/automation-plan.md)** -- Windows 常駐排程器、工作觸發與研究批次上限
- **[docs/glossary.md](docs/glossary.md)** -- 全專案術語對照表（代號、損益、停損停利、持倉、風控層級）
- **[docs/trading-rules.md](docs/trading-rules.md)** -- 自動買賣規則（ConfigurableStrategy / EtfFollowStrategy / 出場規則）
- **[docs/profit-plan.md](docs/profit-plan.md)** -- 資金與風險規劃
- **[docs/dashboard-sop.md](docs/dashboard-sop.md)** -- Dashboard 開發與操作 SOP
- **[prompts/README.md](prompts/README.md)** -- Prompt 清單與狀態（active / planned / archived）
- **[docs/futures-spot-strategy.md](docs/futures-spot-strategy.md)** -- 「期貨為輔、現貨為主」的 AI 自動化交易策略架構（規劃文件）
- **[docs/data-sources.md](docs/data-sources.md)** -- 所有外部資料源清單與 `uv run stock-validate` 用法

## 參考

- [Shioaji 官方文檔](https://sinotrade.github.io/)
- [TWSE 公開資訊觀測站](https://wwwc.twse.com.tw/)
- [StrategyExecutor_feather](https://github.com/phenomenoner/StrategyExecutor_feather) (架構參考)
