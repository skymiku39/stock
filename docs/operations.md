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
