# 自動化常駐計畫（Windows Task Scheduler）

本文件說明本專案在 Windows 上採用的**每次登入常駐 daemon**模式、與 Task Scheduler 的邊界，以及設定腳本的用途。  
**不宣稱**整條每日研究／交易管線已在真實環境完整跑通成功。

相關運維手冊見 [operations.md](operations.md)。背景排程器程式行為見 README「stock-scheduler」與 `.env.example` 的 `SCHEDULER_*`。

---

## 1. 採用模式：每次登入常駐 daemon

本專案**不以**「每日固定時刻用 CalendarTrigger 重啟一整條 CLI」作為主要自動化模型。

改為：

1. 使用者登入 Windows（AtLogOn / `LogonTrigger`）
2. Task Scheduler 啟動兩個**長駐程序**
3. 程序內部依 `.env` 的間隔與時段自行 tick（macro／research／company 等）

| Windows 工作名稱 | 角色 | 既有 Action（現機核對） |
|------------------|------|-------------------------|
| `SkyMiku Codex LLM App Server` | Codex app-server 常駐；LLM client 使用 loopback `ws://127.0.0.1:4500` | `powershell.exe -File C:\skymiku\stock\scripts\start_codex_app_server.ps1`，cwd `C:\skymiku\stock` |
| `SkyMiku Stock Daily Scheduler` | `stock-scheduler` 常駐 | `C:\skymiku\stock\.venv\Scripts\python.exe -m bot.scheduler --root C:\skymiku\stock`，cwd `C:\skymiku\stock` |

對照專案內命名：

| 層級 | 名稱 |
|------|------|
| Console script（pyproject） | `stock-scheduler` → `bot.scheduler:main` |
| 模組啟動（現有 task） | `python -m bot.scheduler --root <專案根>` |
| 手動一鍵（開發用） | `scripts/start-watch-only.ps1`（另開視窗跑 `uv run stock-scheduler` + dashboard） |
| Codex launcher（生產機） | `scripts/start_codex_app_server.ps1`（路徑慣例：`C:\skymiku\stock\...`；文件路徑寫法亦見 operations／setup 的 `d:\skymiku\stock`） |

> **路徑說明**：operations／setup 示例常用 `d:\skymiku\stock`；現機 Task Action 使用 `C:\skymiku\stock`。設定腳本**不改寫** Action／WorkingDirectory，只調整 trigger／部分 settings。

---

## 2. Task Scheduler 目標設定

由 `scripts/configure_automation_tasks.ps1` 對**既有**工作套用：

| 項目 | App Server | Stock Daily Scheduler |
|------|------------|------------------------|
| Trigger | 單一 `LogonTrigger`（移除每日 `CalendarTrigger`） | 同左 |
| `MultipleInstancesPolicy` | `IgnoreNew` | `IgnoreNew` |
| `ExecutionTimeLimit` | `PT0S`（無限制） | `PT0S` |
| `RestartOnFailure` | **保留定義中已有內容**（現機常見 Count=3 Interval=PT1M） | **設為** Count=3 Interval=`PT2M` |
| Actions / Principal / 其餘適用 settings | 保留 | 保留 |

腳本行為約束：

- 修改前備份原始 XML 至 `%LOCALAPPDATA%\SkyMiku\TaskSchedulerBackup\`
- 支援 `-WhatIf` / `-Confirm`
- 輸出修改前／後快照與驗證結果
- **Fail-closed**：找不到工作或權限不足 → 非零結束；**不建立**新 task；**不停**正在執行的程序
- 僅更新定義；正在跑的 daemon 不會因此被腳本停止

---

## 3. 排程功能落在哪一層

```
LogonTrigger
    └─ SkyMiku Stock Daily Scheduler
           └─ bot.scheduler（常駐 tick）
                  ├─ macro / research / company / fundamentals / …
                  ├─ 可選 monitor 托管 stock-bot
                  └─ 間隔與時段由 SCHEDULER_* 控制
    └─ SkyMiku Codex LLM App Server
           └─ codex app-server（給本機 LLM 相關功能連線）
```

- **Windows Task Scheduler**：負責「登入後把 daemon 拉起來、崩潰重試、避免重複實例」。
- **`stock-scheduler`**：負責「何時跑哪一個 job」（見 README 與 `.env.example`）。
- 研究工作預設每輪最多完整分析 12 檔缺少或超過 12 小時快取的標的；有效快取會略過，其他標的從 `data/auto_research_cursor.json` 記錄的位置輪替續跑。`SCHEDULER_RESEARCH_MAX_TICKERS=0` 可取消上限。
- 每個 job 用非阻塞鎖避免啟動即跑和排程 tick 重複派送。每日 job 只有成功才標記當天完成；失敗後會在執行窗口內每 5 分鐘重試。`--once` 會回傳第一個失敗 job 的非零狀態碼。
- **operations.md**：描述盤前手動確認、`uv run stock-bot` 等**人機操作**流程；與本 daemon 模式並存，不是互相取代的唯一入口。

研究焦點若大於每輪上限，個股 LLM 研究會跨多個排程輪次完成；這不表示 210 檔每日都會全量刷新。價格、總經、ETF、行事曆與其他排程工作仍依各自間隔執行。若要求單輪全量研究，可把上限設為 `0`，但單次時間會隨焦點檔數增加。

可選的 `--once` 模式（`uv run stock-scheduler --once`）適合「外部每 N 分鐘觸發一次」的舊式排程；**本計畫選擇常駐 daemon + AtLogOn**，與 README 建議的 24/7 常駐路徑一致。

---

## 4. 證據界線（目前能／不能斷言的事）

下列屬**設定與命名核對**層級，不是「整日管線已成功」的證據：

| 可核對 | 說明 |
|--------|------|
| 工作名稱存在 | `SkyMiku Codex LLM App Server`、`SkyMiku Stock Daily Scheduler` |
| Action／路徑字串 | 指向 `C:\skymiku\stock` 下 launcher／`.venv` python |
| 腳本與文件已落地 | `scripts/configure_automation_tasks.ps1`、本文件 |
| 設定腳本設計意圖 | AtLogOn-only、IgnoreNew、PT0S、Restart 政策、備份、WhatIf、fail-closed |

下列**不在**本文件／本腳本的保證範圍內（請勿解讀為已驗證成功）：

- 某交易日 research／macro／LLM／雲端同步／監控**整鏈**無錯誤跑完
- Gemini／Shioaji／Google Sheets 憑證與額度在無人值守下長期可用
- worktree 與生產目錄 `C:\skymiku\stock` 程式碼已同步且行為一致
- Task 顯示 `Running` 等同業務結果正確（僅代表行程可能仍在）

### 2026-09-29 實際觀察與修正

- 兩個 Windows 工作原先都同時有登入與每日 trigger，並使用 `IgnoreNew`。常駐程序已經在跑時，每日 trigger 再要求啟動同一工作會被拒絕，產生 `0x800710E0`；這是重複 instance 被拒絕，不是每日資料管線失敗的直接證據。
- 當日研究管線 `20260929_083013` 實際完成：焦點 210 檔、耗時約 7 小時 21 分、`exit=0`，但有 7 項警告，狀態為 `partial`。序列逐檔研究是主要長工序。
- 因此移除每日重複 trigger、給 scheduler 設 3 次／每 2 分鐘的失敗重啟、加上跨程序單例鎖，並讓排程研究逐輪處理缺少或過期快取的個股。快取期限比較也改為保留台灣時區，避免時區相減錯誤被吞掉而永久當成新鮮快取。
- 本次設定腳本已讀回驗證兩個工作均為單一 `LogonTrigger`；2026-09-29 18:26 安全重啟 `stock-scheduler` 後，新程序已啟動帶 `--auto-research-limit 12` 的批次。
- 完成的批次 `20260929_172221` 耗時 3,724.89 秒，焦點 213 檔、本輪選 12 檔、67 檔有有效快取而略過、134 檔延後；錯誤 0、警告 7、`exit=0`、狀態 `partial`。其中 2006 未產出分析；這筆結果是在修正前執行，新的程式會把 `None` 記入 `no_result_tickers` 與警告，避免報成完整成功。
- 此次確認的是一輪受限批次與排程器重啟，不等於 213 檔每日全量刷新，也不等於長期無人值守穩定性已經證明；後續輪次應繼續檢查 `run.json` 的狀態、錯誤與警告。

驗證建議（人工、在授權變更後）：

1. `.\scripts\configure_automation_tasks.ps1 -WhatIf` 檢視預期 delta  
2. 確認後執行（必要時系統管理員／相同使用者權限）  
3. 已以安全重啟確認工作可再次拉起 daemon；實際重新登入觸發仍待下一次登入驗證  
4. 另以 log／`stock-scheduler --dry-run`／各 job 產物做業務驗收（本腳本不做業務驗收）

研究批次上限在專案 `.env` 設為 `SCHEDULER_RESEARCH_MAX_TICKERS=12`；不設定時也採 12。每輪只計算需要新研究的標的，成功快取的個股會略過；每個嘗試結果都更新輪替游標，避免固定排序前段持續占滿名額。若某檔未產出可保存的分析，該輪會標成 `partial` 並記錄在 `no_result_tickers`，之後游標輪回時重試。手動執行 `stock-auto-research` 不帶 `--auto-research-limit` 時仍保留完整批次行為。

---

## 5. 程式碼更新何時生效

Task Scheduler 只負責**啟動命令列**；daemon **進程內**已載入的 Python／腳本不會因 git pull 自動重載。

因此：

- 更新 `bot.scheduler` 或其他依賴套件後，須在**下一次安全重啟或登出再登入**（或手動結束舊行程後再由 task／登入觸發）後，新程式碼才會被載入。
- 僅執行 `configure_automation_tasks.ps1` **不會**停止現行程、也**不會**保證立刻跑到新程式碼。
- App Server（`start_codex_app_server.ps1`／codex）同樣適用：改 launcher 或 Codex 版本後，需重啟該行程才生效。

---

## 6. 使用方式

```powershell
cd C:\skymiku\stock   # 或你的 clone／worktree；腳本本身不依賴 cwd 改 task Action
.\scripts\configure_automation_tasks.ps1 -WhatIf
.\scripts\configure_automation_tasks.ps1 -Confirm
```

Exit code：`0` 成功／`2` 缺工作、權限不足或驗證未過（fail-closed）。

備份目錄：`%LOCALAPPDATA%\SkyMiku\TaskSchedulerBackup\`。

---

## 7. 與其他文件的關係

| 文件 | 關係 |
|------|------|
| [operations.md](operations.md) | 日常人機運維（盤前檢查、手動 `stock-bot`）；不取代 Task 常駐模型 |
| README `stock-scheduler` | job 種類與 `SCHEDULER_*`；建議登入常駐或 `--once` |
| `.env.example` | 排程間隔與監控托管開關的 SSOT |
| `scripts/start-watch-only.ps1` | 開發／手動「只看不買」一鍵；非正式 Task 定義來源 |

本計畫文件只涵蓋 **Task 觸發模型與設定腳本**；業務管線是否「完整成功」需另以當日 log 與產物證明。


### 2026-10-16 可驗證來源研究與失敗重試

現有 research 工作透過 `bot.auto_research` 自動接續官方來源採集、原始資料登錄、證據重播與分析。
未關閉 LLM 或使用 `--no-verified-backlog` 時，無需另外建立 Windows 或 Codex 排程。
每輪維持最多 12 檔的輪替限制，不代表全部追蹤標的每日全量刷新。

官方來源補跑失敗或排除時，會針對同一個 source run 延遲 60、120 秒再試，共最多三次；
已完成結果重新驗證後沿用，來源仍無法驗證的資料不送入分析，最後仍回傳非零狀態。
模型批次本身最多嘗試兩次。每次來源補跑都重新檢查來源及結果，不把舊成功狀態當作本次成功。

週期工作失敗後，在原有允許時段內每 5 分鐘重試，最多額外兩次，仍失敗則回到原間隔。
失敗次數和完成時間保存在 `data/scheduler_status.json`，重啟後保留等待時間。
研究工作的逾時獨立設定為 7200 秒，避免原本接近一小時的採集擠掉官方來源補跑。
設定項為 `SCHEDULER_FAILURE_RETRY_SECONDS`、`SCHEDULER_FAILURE_MAX_RETRIES`、`SCHEDULER_RESEARCH_TIMEOUT_SECONDS`。

現機使用登入後常駐模式，預設研究時段為台北時間週一至週五 08:30–14:30、每 240 分鐘；
電腦必須開機、使用者已登入且網路及模型服務可用。Windows 對 daemon 崩潰另有每 2 分鐘、最多三次重啟。
來源驗證只能證明引用與原始資料一致，不能證明模型結論正確。
