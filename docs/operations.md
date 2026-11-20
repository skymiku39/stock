# 日常運維手冊

Windows 常駐排程與 Task Scheduler 設定請見 [automation-plan.md](automation-plan.md)；本手冊其餘章節列出日常人工操作與 log 檢查。

排程任務的執行上限由 `SCHEDULER_JOB_TIMEOUT_SECONDS` 設定，預設 3600 秒且必須大於 0。
逾時會終止該任務的子行程樹、釋放任務鎖並記錄 exit code 124；完成前已提交的資料不會自動回滾。
盤中長駐 monitor 有獨立啟停生命週期，不套用這個單次任務上限。

Dashboard 背景任務另由 `DASHBOARD_TASK_TIMEOUT_SECONDS` 限制，預設 3600 秒且必須大於 0。
每個任務在獨立子程序執行；逾時會先終止該程序樹，再將任務標記 `timed_out` 並繼續佇列。
可在任務中心查看原因及手動重試，不會自動重跑 LLM。服務正常關閉會終止執行中的工作並標記
`interrupted`；服務意外退出時，工作程序透過父程序連線中斷停止，狀態由心跳租約恢復。
已提交的資料不會回滾，外部服務可能已處理請求；重試前請先確認結果。
各任務程序的輸出保存在 `logs/dashboard_tasks/<task-id>.log`。
若無法確認程序終止，任務會標記 `termination_failed` 並阻擋後續執行，重新啟動也不會解除。
此時先由維護者依錯誤中的 PID 確認程序樹已停止，再修復該任務狀態；不要直接重試或清空佇列。

`data/scheduler_status.json` 的 `heartbeat_at` 只表示排程器存活。檢查實際成果請看
`jobs.<任務>.state`、`exit_code`、`started_at`、`finished_at`、`last_success_at` 與 `log_path`。
狀態分為 `running`、`succeeded`、`failed`、`timed_out`、`interrupted`、`dry_run`；
啟動時若發現上次仍為 running，會標記 interrupted。dry-run 不更新真正的成功時間。

## 零、研究系統整合體檢（唯讀）

交易用 `stock-preflight` 與研究系統的整合體檢是兩個不同入口。每天啟動研究或
排程前，先在專案根目錄執行：

```powershell
cd C:\skymiku\stock
uv run stock-system-preflight --root C:\skymiku\stock --json
uv run stock-system-preflight --root C:\skymiku\stock --imports
```

此檢查不抓外網、不呼叫 Gemini、不執行 Google Sheets 同步、不寫入 SQLite，也不會
觸發下單。它會確認 SQLite integrity、raw archive hash、research／evidence
provenance、FTS 索引，以及 Gemini／Google Sheets 是否已配置。`GEMINI_API_KEY`
或 Google 憑證缺少時是可預期的 `info`；本地核心的完整性錯誤才是 `fail`。

若出現 `research_semantic_partial` 或 `source_runs_without_raw_manifest`，這代表
既有資料仍有明確標示的歷史限制，不可依 URL、標題或相似度猜補來源；回答層應維持
exact-only 與「我不確定」閘門。即時筆數請以當次 `stock-data-audit --json` 為準，
勿沿用其他文件中的歷史 4／43 快照。

若 Windows 上已有舊版 Dashboard 正在執行、導致 `uv run` 無法更新 `.venv\Scripts`
入口檔，使用等價的模組入口：

```powershell
uv run --no-sync python -m bot.system_preflight_cli --root C:\skymiku\stock --json
```

## 〇・一、研究衍生鏈隔離（預設 dry-run）

已知錯誤的 research／evidence／claim 衍生鏈可用 `stock-research-quarantine`
標記為不可引用。此入口**不會刪除** raw archive 或關聯列，也**不會**依 URL、標題或
相似度猜測 provenance；只接受明確的 `source_document_id`。

```powershell
# 預設唯讀 dry-run（不寫入）
uv run stock-research-quarantine --root C:\skymiku\stock

# 列出已隔離鏈（唯讀，含受影響 ID）
uv run stock-research-quarantine --root C:\skymiku\stock --list

# 預覽單一來源會影響哪些 ID（仍不寫入）
uv run stock-research-quarantine --root C:\skymiku\stock `
  --source-document-id <exact-source-document-id>

# 實際隔離：必須同時提供具體 source_document_id 與 reason；先備份再執行
uv run stock-db-backup --root C:\skymiku\stock --json
uv run stock-research-quarantine --root C:\skymiku\stock `
  --source-document-id <exact-source-document-id> `
  --reason "說明為何隔離" `
  --apply
```

缺少 `--source-document-id` 時，`--apply` 會被拒絕（exit code 2）。此命令與交易／
下單路徑無關，請勿對正式庫直接 `--apply` 除非已確認 exact ID 與備份。

## 〇・二、LLM 執行與輸入證據

使用唯讀稽核查看目前分類：

```powershell
uv run --no-sync stock-data-audit --root C:\skymiku\stock --json
```

`database.llm_run_provenance.counts` 區分 `linked`、`partial`、
`successful_without_evidence`、`failed`、`not_required`、
`legacy_unresolved` 與 `unassessed`；`by_task` 另外保留任務種類、
執行狀態及各類筆數。失敗呼叫與明確登錄的 `query_plan` 不列為成功分析缺證據；
沒有新版 metadata 的舊紀錄維持未判定，仍缺連結時繼續列入缺口。
稽核會重查資料表的實際連結，不直接相信當時保存的 `linked` 標記。

新 JSONL 的 `call_id` 識別一次呼叫；run metadata 的 `pipeline_run_id`
只用於批次分組。相同秒數、相同 prompt 或相同 pipeline 的呼叫不會互相覆寫。
舊 JSONL 保持原有識別方式；重播新版紀錄會使用相同 call ID。

`research_ticker` 在模型呼叫之前登錄來源，再讓 JSONL、分析歷史和 run
保存相同輸入 ID；登錄失敗會略過模型呼叫。run metadata 的
`input_evidence` 保存預期、已連結及未解析的 ID。這只代表輸入來源的關聯；
`linked` 不代表所有輸入都有官方 raw，也不代表模型的結論已驗證。

呼叫日誌與 `stock-provenance-backfill --apply` 的結果預設不可發布，
回填的 claims 保持 `unverified`。回填只接受輸入 metadata 明確列出的 ID，
不把模型輸出自稱的引用當成輸入證明。Ask 通過既有回答驗證後，必須將 run、
全部引用、output 與 claims 一併提交，才會留下可發布結果；任何寫入失敗都回滾。
JSONL 保留於 `log/llm_calls/`，可供後續重播。

正式歷史資料不會因程式升級而自動重寫。需要回填時先備份，在資料副本驗證
結果；沒有原始輸入證明的內容維持未驗證，不能靠猜測補造證據 ID。

## 〇・三、每日資料收集與新鮮度檢查

排程啟用 `SCHEDULER_DAILY_ALL_ENABLED` 時有三個相關工作：

| 工作 | 間隔 | 需要模型 | 內容 |
|---|---|---|---|
| `daily_inputs` | 5 分鐘 | 否 | 鉅亨新聞（日快取累積）、RSS 存檔、主要追蹤與擴充名單的日 K、新鮮度檢查 |
| `collection` | 60 分鐘 | 否 | ETF 持股、籌碼、官方財務（月營收、本益比、季報、股利）；當天收集完成後直接結束 |
| `research` | 依設定 | 是 | 個股研究；模型額度被擋時排程不會啟動它 |

**擴充名單**是追蹤的 ETF 本身、各 ETF 最新一期的台股持股，加上 `config/backtest_universe.json`。
`daily_inputs` 以主要追蹤標的（`.env` 的 `SYMBOLS` 與觀察清單）最新的來源日為目標日，
已到目標日的不連線；落後的逐檔抓，每輪最多 240 秒，沒抓完下一輪接續。
結果在 `data/daily_research/<日期>/daily-inputs.json` 的 `extended`：

- `pending`：還沒輪到，下一輪會抓。
- `behind`：抓過但來源日仍較舊（停牌、下市），同一個目標日不重抓。
- `failed`：同一個目標日已失敗的標的與原因；每檔最多試三次。

海外持股（例如 `NVDA US`）沒有 TWSE／TPEx 來源，不在擴充名單內，籌碼也不抓。

**收集完成的判斷**在 `data/daily_research/<日期>/status.json`：`collection_complete` 與
`official_complete` 都是 true，當天就不再重跑收集管線（約 20 分鐘）。管線的 warnings 分兩種：

- 代表沒收到資料的（例如台股代號在全市場有資料的日子查不到籌碼、台指期只拿到快取），
  會讓 `collection_complete` 維持 false，下一輪重試。
- 資料已取得、只需註明限制的，另外列在 `collection_advisory_warnings`，不影響完成：
  各來源觀測日不同（台股白天時美股與 ADR 只到前一個交易日、匯率已是當天），以及
  個股在回顧期間少了幾天籌碼（例如停牌）。籌碼的預期天數以全市場實際有資料的日數為準，
  當天盤後才公布的籌碼不算缺漏。

新鮮度檢查的「每日收集」一項會列出今天尚未完成的原因。
新納入的標的先只有近一個月日 K；較早的歷史由 `history_fetch` 佇列補：佇列跑完後每天會把
擴充名單併入一次，並依「月份由近到遠、同一個月內逐檔」的順序回補。

**RSS 存檔**寫在 `data/news/rss_<日期>.json`，依網址去重、累積當天看過的項目；
每個來源的狀態在同一個檔案的 `feeds`。手動執行：

```powershell
.venv\Scripts\python.exe -m bot.rss_archive --root C:\skymiku\stock
```

**新鮮度檢查**直接看資料本身的最新日期，不看工作有沒有回報成功：

```powershell
.venv\Scripts\python.exe -m bot.data_freshness --root C:\skymiku\stock
```

結束碼 0 全部正常、1 有警告、2 有落後。每輪 `daily_inputs` 也會把結果寫到
`data/freshness_status.json`，並附在當天的 `summary.txt`。假日無法從資料判斷，
所以落後一個工作日只算警告。

**日 K 交叉核對**用交易所的全市場單日行情（另一個端點）核對資料庫同一天的日 K，
開高低收必須完全相同；每輪 `daily_inputs` 會對目標日自動執行，也可以手動指定日期：

```powershell
.venv\Scripts\python.exe -m bot.price_crosscheck --root C:\skymiku\stock --date 2026-11-16
```

結束碼 0 全部一致、1 名單內有標的尚未入庫或全市場日報沒有該代號、2 有價格不符。
結果在 `data/price_crosscheck/<日期>.json`。

**前瞻追蹤**把 #19 選定的兩組參數凍結，只用 2026-11-01 之後的行情記帳（詳見
`src/bot/forward_tracker.py` 開頭的說明）。資料日前進時自動重算，結果在
`data/forward_tracking/latest.md`；手動重算：

```powershell
.venv\Scripts\python.exe -m bot.forward_tracker --root C:\skymiku\stock --force
```

只想補資料、不想動用模型時：

```powershell
.venv\Scripts\python.exe -m bot.daily_research --root C:\skymiku\stock --collect-only
```

注意：排程器是常駐程序，`collection` 這類**新增的工作**要重啟排程器後才會出現；
既有工作的程式改動則在下一次執行時就生效，因為每次都是另開程序。
重啟之前，`daily_inputs` 會在當天的收集還沒完成、且 90 分鐘內沒有任何收集嘗試時代跑一次
`--collect-only`，所以不重啟也不會停更，只是那一輪 `daily_inputs` 會多花十幾分鐘。

`extended` 另外有 `retired`：最新日 K 比目標日舊 90 天以上的標的（下市、長期停牌），
不會每天重抓，也不算失敗；恢復交易時由 `history_fetch` 補回。

---

## 〇・四、實際持股的微笑盯盤（只提醒、不下單）

微笑策略只認模擬帳戶的部位。要用微笑規則盯券商帳戶裡的實際持股，用：

```powershell
.venv\Scripts\python.exe -m bot.smile_watch --symbol 1514 --reference 105
```

- **排程**：`config/smile_watch.json` 存在時，排程器會有 `smile_watch` 工作（平日 09:00～13:30，
  執行 `--config config/smile_watch.json --max-minutes 50`；每輪 50 分鐘正常結束、下一分鐘再啟動，
  避開 `scheduler_job_timeout_seconds`）。`SCHEDULER_SMILE_WATCH_ENABLED=false` 可停用。
  改名單不用重啟排程器（下一輪就會讀到）；第一次新增名單檔才要重啟。盤中這個工作幾乎一直是
  running，重啟排程器前「沒有工作在執行」的檢查可以不算它（中斷只會少盯幾十秒）。
- 名單格式：`{"targets": [{"symbol": "1514", "reference": 105}]}`；每筆可加 `stop_loss_pct`
  （覆寫 `SMILE_SYMBOL_STOP_LOSS_PCT`）與 `holding`（`"4@85.65"`，手動給張數與均價、不讀券商）。
- 啟動時與之後每 30 分鐘唯讀查詢一次券商實際庫存（只登入與列出持股），整個部位視為一批、
  成本用券商的均價。名單內的標的沒有庫存時不盯，之後買進會在下一次查詢時開始盯。
- 每 20 秒輪詢公開資訊觀測站的報價（成交價優先，這一輪沒有成交價時用委買委賣中間價）；
  手動執行時 13:30 自動結束。假日沒有當日報價，不會有任何判斷。
- 訊號：`take_profit`（回到標準價且整個部位有淨利，或達 `SMILE_TAKE_PROFIT_PCT`）、
  `breakeven`（部位轉為淨利）、`stop_loss`（達 `SMILE_SYMBOL_STOP_LOSS_PCT`，可用
  `--stop-loss-pct` 覆寫）、`stop_near`（距停損價 3% 以內）、`add_buy`（跌幅已達階梯且張數低於
  `MAX_LOT_PER_SYMBOL`）。同一個訊號每個交易日只提醒一次。
- 輸出在 `data/smile_watch/`：`status-<代號>.json`（最新狀態）、`alerts.jsonl`（每次提醒）、
  `state.json`（當天已提醒的訊號）。提醒會跳本機通知（見下方）；有設定 Telegram 另外推播。
- **本機通知**（`bot/local_notify.py`，`LOCAL_NOTIFY_ENABLED=false` 可停用）：在這台電腦跳 Windows
  桌面通知，不經外部服務、不用額外套件。排程器的程序在工作階段 0（S4U），從那裡發的通知不會出現在
  桌面，所以背景程序只把通知寫進 `data/notifications/pending/`，再觸發排程工作
  「SkyMiku Stock Local Notify」（以互動方式登入）到桌面上顯示。這個工作要先註冊一次：
  `powershell -ExecutionPolicy Bypass -File scripts\install_local_notify_task.ps1`（加 `-Remove` 移除；
  不需要系統管理員）。沒註冊時通知留在佇列、log 會有一則警告；排隊超過 12 小時的不再跳出。
  沒有登入桌面時不會顯示，下次登入時補跳。檢查：`python -m bot.local_notify --status`；
  測試：`--test "標題\n內文"`（直接顯示）、`--queue-test "標題\n內文"`（走排隊與排程工作）。
  紀錄在 `data/notifications/history.jsonl`。Windows 的「請勿打擾」開啟時通知只會進通知中心。
- 程式不建立也不送出任何委託，買賣由使用者自己操作。沒有逐批成交紀錄，所以不知道已經買過
  哪幾階；也沒有「時事＋即時股價」的情緒賣出判斷。

---

## 〇・五、ETF 關注名單（主動式＋大型被動式）

從 ETF 持股收斂出比較需要關注的股票。每輪 `daily_inputs` 會自動重算，也可以手動執行：

```powershell
.venv\Scripts\python.exe -m bot.etf_focus --print
```

- 輸出在 `data/etf_focus/`：`latest.md`（報告）、`latest.json`、`<持股日期>.json`（每天留一份，
  之後才有歷史可以檢驗這份名單有沒有用）。
- **被動式 ETF 名單**在 `config/passive_etfs.json`（目前 0050、0056、00878、00919、00940、00713）。
  加一筆 `{"symbol": "代號", "name": "名稱"}` 即可，持股來源預設用 etfinfo.tw；改名單不用重啟排程器。
  名單內的 ETF 會一起抓持股、它們和持股的日 K 也併入每日收集的擴充名單。006208 和 0050 追蹤同一個
  指數，列入會重複計數；00929 的持股頁解析不到完整持股。
- 主動式與被動式**分開計數**：被動式照指數規則選股，只當背景資訊。跟單策略、共識訊號、研究焦點
  （`load_active_etfs`）仍只看主動式；持股收集、每日日 K、新鮮度檢查、關注名單用 `load_tracked_etfs`。
- 只納入台股權重合計 ≥ 50% 的 ETF。加減碼用股數比較（約 28 天前的那一期），並扣掉整檔 ETF 的
  規模變化（兩期共同持股的股數比值中位數），扣掉後變動超過 ±5% 才算。
- 標籤：共識核心（一半以上的主動式持有）、主動加碼／主動減碼（淨加碼家數達主動式的 20%、至少 3 家）、
  被動納入／被動剔除（比較期間指數調整）。門檻是暫定值，寫在 `bot/etf_focus.py` 開頭。
- 這是關注名單，不是買賣訊號：持股快照從 2026-09 才開始累積，沒有足夠歷史可以驗證。

### 自己的持股也併入每日收集

每日收集的擴充名單除了 ETF 與 ETF 持股，還包含使用者實際持有或曾經持有的股票（`bot/held_symbols.py`），
兩個來源取聯集、只增不減：

- `config/held_symbols.json`：手動維護，`{"symbols": ["1514"]}`。想固定追蹤的股票加在這裡。
- `data/held_symbols_seen.json`：微笑盯盤每次讀券商庫存時，把查到的所有持股（含不在盯盤名單的、零股）
  自動記下。賣掉後仍留著；要移除就手動刪掉那一筆。盯盤沒在跑（沒有 `config/smile_watch.json`）時
  不會自動記錄，新買的股票要手動加。

---

## 一、每日操作流程

### 盤前（08:00 ~ 09:00）

1. **確認環境**
   ```powershell
   cd C:\skymiku\stock
   ```

2. **檢查 .env 設定**
   - 確認 `RUN_MODE`：預設為 `watch`（看盤不下單）；`report` 公開延遲資料報表；`trade` 需明確設定（當沖實單預設封存）
   - 確認 `SYMBOLS` 是否需要更新（今天要監控哪些股票）
   - `trade` 模式才需要確認 `SIMULATION` 設定（`true` = 模擬，`false` = 實單）

3. **啟動機器人**
   ```powershell
   uv run stock-bot
   ```

   不使用 Shioaji API 的報表模式：
   ```powershell
   $env:RUN_MODE="report"; $env:SYMBOLS="2330,0050"; uv run stock-bot
   ```

   使用 Shioaji 行情但不下單的看盤模式（與預設相同，可省略）：
   ```powershell
   $env:RUN_MODE="watch"; uv run stock-bot
   ```

4. **確認啟動成功**

   正常啟動的 log 順序（範例為 `run_mode=watch`）：
   ```
   [INFO] main: === Stock Bot 啟動 ===
   [INFO] main: 組態載入完成 (run_mode=watch, market_source=shioaji, simulation=true)
   ```

   交易／券商連線層另用 `stock-preflight`；研究／資料完整性請用
   `stock-system-preflight` 與只讀 `stock-data-audit`（見上方「零」節），
   不要把歷史驗證快照當成目前計數。

5. **確認 Telegram 通知**

   如有設定 Telegram，你會收到啟動通知。

### 盤中（09:00 ~ 13:30）

- 機器人自動運行，不需手動介入
- 可透過終端 log 或 Telegram 即時監控狀態
- 重要事件（買進、賣出、停損）都會自動通知

### 收盤後（13:30 ~）

1. **確認機器人結束**

   正常結束的 log：
   ```
   [INFO] strategy: 所有部位已清空，策略結束
   [INFO] recorder: 交易紀錄已匯出: data/trades_2026-05-12.csv
   [INFO] main: 交易摘要:
   成交筆數: 4
   買進金額: 600,000
   賣出金額: 618,000
   商品: 2330, 0050
   [INFO] main: === Stock Bot 結束 ===
   ```

2. **檢查交易紀錄**
   - `trade` 模式 CSV 檔案位於 `data/trades_YYYY-MM-DD.csv`
   - 欄位: datetime, symbol, action, price, quantity, ordno, custom_field, amount
   - `watch/report` 模式位於 `data/reports/signals_YYYY-MM-DD.csv` 與 `report_YYYY-MM-DD.csv`

3. **檢查 log 檔案**
   - Log 位於 `log/` 目錄
   - 每個模組有獨立的 log 檔：`main_2026-05-12.log`、`broker_2026-05-12.log` 等

## 二、手動停止

### 正常停止

按 `Ctrl+C`，系統會：
1. 收到 SIGINT 信號
2. 呼叫 `strategy.stop()`
3. 匯出交易紀錄
4. 發送 Telegram 停機通知
5. 登出 Shioaji
6. 結束程式

### 強制停止

如果 `Ctrl+C` 無回應，按第二次 `Ctrl+C` 強制終止。

**注意**: 強制終止不會匯出交易紀錄，但 log 檔仍然可查。

## 三、Log 檢查指南

### Log 位置

```
log/
  main_2026-05-12.log      ← 主程式流程
  broker_2026-05-12.log    ← 連線、訂閱、下單
  strategy_2026-05-12.log  ← 策略判斷、部位變化
  recorder_2026-05-12.log  ← 紀錄匯出
  notifier_2026-05-12.log  ← 通知發送
```

### 重要 log 訊息

| Log 內容 | 意義 |
|----------|------|
| `[進場] 2330 漲幅 2.5%` | 觸發進場條件 |
| `成交回報: Buy 2330 2 張 @ 600.00` | 買入成交 |
| `[停損] 2330 PnL=-3.5%` | 觸發停損 |
| `[移動停利] 2330 PnL=6.2% 高點=636.00 回撤=2.1%` | 觸發移動停利 |
| `全出場: 2330 2 張 (市價 IOC)` | 收盤清倉 |
| `[虛擬買入] 2330 600.00 x 1` | watch/report 模式產生 `would-buy` 訊號 |
| `[虛擬賣出] 2330 610.00 x 1` | watch/report 模式產生 `would-sell` 訊號 |

> **持倉用詞**：策略 log 用「部位」；券商 API 用「庫存」；`PositionInfo` 稱「持倉」；投組分析用「持股」。對照見 [glossary.md](glossary.md)。
| `嘗試重連 (1/10)` | 行情斷線，開始重連 |
| `重連成功!` | 重連成功 |
| `已達重連上限` | 重連失敗，需人工處理 |

### 過濾特定 log

```powershell
# 只看進出場相關
Select-String -Path "log\strategy_*.log" -Pattern "\[進場\]|\[停損\]|\[移動停利\]|全出場|成交回報"

# 只看錯誤
Select-String -Path "log\*.log" -Pattern "ERROR|EXCEPTION|失敗"

# 只看特定股票
Select-String -Path "log\strategy_*.log" -Pattern "2330"
```

## 四、異常處理

### 4.1 登入失敗

**症狀**: `登入失敗` 錯誤，程式直接結束

**排查步驟**:
1. 確認 `.env` 中 `API_KEY` 和 `SECRET_KEY` 是否正確
2. 確認 API 權限是否已在 iLeader 開通
3. 確認網路連線正常
4. 確認 Shioaji 服務狀態: https://www.sinotrade.com.tw/

### 4.2 行情斷線

**症狀**: Log 出現 `嘗試重連`

**系統行為**: 自動進行指數退避重連（5s → 10s → 20s → ... → 120s，最多 10 次）

**成功**: Log 顯示 `重連成功!`，系統自動恢復

**失敗**: Log 顯示 `已達重連上限 (10 次)，放棄重連`
- 手動停止程式（`Ctrl+C`）
- 確認網路連線
- 重新啟動

### 4.3 下單失敗

**症狀**: Log 出現 `下單失敗` 或 `委託失敗`

**常見原因**:
- 憑證過期或未啟用 → 重新下載憑證
- 資金不足 → 檢查帳戶餘額
- 合約不存在 → 確認股票代碼
- 已超過漲跌幅限制 → 正常行為，不需處理

### 4.4 Telegram 通知失敗

**症狀**: Log 出現 `Telegram 發送失敗`

**排查步驟**:
1. 確認 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID` 正確
2. 確認 Bot 沒有被停用
3. Telegram 通知失敗不影響交易，可忽略

### 4.5 程式當掉（未正常出場）

如果程式意外終止，可能有未平倉的部位：
1. 立即登入券商 App 或 iLeader
2. 手動檢查持倉
3. 手動平倉（當沖策略不應留隔夜）
4. 檢查 log 定位問題原因

### 4.6 微笑策略狀態檔損毀

啟動時若出現 `smile_state.json 損毀` 的 RuntimeError（策略不會自動重置，fail closed）：
1. 確認路徑：`data/simulation/data/smile_state.json`（訊息內也會列出絕對路徑）。
2. 可從同目錄 `smile_state.json.bak` 還原（最後一次成功寫入前的版本）：複製 `.bak` 覆蓋主檔後重啟。
3. 若確定沒有微笑持倉與未結委託，可刪除主檔，讓策略重建標準價。
4. 重啟後確認持倉與微笑批次對帳通過。

### 4.7 微笑策略單檔停損後重新啟用

單檔停損（`SMILE_SYMBOL_STOP_LOSS_PCT`）出清後，該檔在 `smile_state.json` 會記成 `"disabled": true`，啟動時 log 也會提示。策略不會自動恢復買進。要重新啟用：

1. 停止微笑策略，確認該檔沒有持倉與未結委託。
2. 備份 `data/simulation/data/smile_state.json`。
3. 把該代號的 `"disabled"` 改成 `false`；若要從目前價格重新起算，同時把 `"reference"` 改成 `0`。
4. 重啟後確認 log 不再出現該檔的停用提示。

## 五、定期維護

### 每週

- 檢查 `log/` 目錄大小，清理過舊的 log
- 檢查 `data/` 目錄，備份交易紀錄
- 備份本地 SQLite：`uv run stock-db-backup`（預設保留最近 14 份於 `data/backups/`）；
  從其他工作目錄執行時使用 `uv run stock-db-backup --root C:\\skymiku\\stock --json`
- 回顧本週交易績效

### 每月

- 更新依賴套件: `uv sync --upgrade`
- 檢查 Shioaji SDK 是否有新版本
- 統計月度績效指標（勝率、盈虧比、期望值）
- 評估是否需要調整策略參數

### 不定期

- 電子憑證到期前重新申請
- API Key 如有安全疑慮，重新產生
- 檢查 `.gitignore` 是否涵蓋所有敏感檔案

## 六、清理命令

```powershell
# 清理 30 天前的 log
Get-ChildItem log\*.log | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } | Remove-Item

# 備份交易紀錄
Copy-Item data\*.csv backup\

# 清理 Python 快取
Get-ChildItem -Recurse __pycache__ | Remove-Item -Recurse -Force
```
