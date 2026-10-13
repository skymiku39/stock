# 雲端同步設定指南

本文件說明如何把本機 SQLite (`data/stock.db`) 與 Google Sheets 連動，
讓多台電腦 / 手機共用同一份股票資料。

---

## 架構回顧

```
雲端 (多機共用)            本地 (每台機器各一份)            外部 API
┌──────────────────┐      ┌─────────────────┐         ┌───────────┐
│ Google Sheets    │←pull→│ data/stock.db   │ ←fetch─ │ TWSE/MOPS │
│ - stock_info     │      │ (SQLite + WAL)  │         │ Shioaji   │
│ - watchlist      │←push─│ Cache-Aside     │         └───────────┘
│ - etf_meta       │      │ 應用查這裡       │
│ - monthly_revenue│      └─────────────────┘
│ - quarterly_…    │
└──────────────────┘
   ↑ Google Forms (選用) — 手機新增股票
```

支援兩種雲端共用模式，依需求二選一：

| 模式 | 適合情境 | 步驟難度 |
|------|---------|---------|
| **A. Google Drive 檔案同步** | 只有一台機器，但想自動備份 | ⭐ 零設定 |
| **B. Google Sheets API 同步** | 多台機器 / 手機 Forms 輸入 / 即時協作 | ⭐⭐ 一次性 |

---

## 模式 A：Google Drive 直接同步 DB 檔

**不建議作為日常方案。** 兩台電腦不能同時開啟同一份 `.db`（會損壞 SQLite / WAL）。  
若只是要「防萬一」，請改用本機備份：

```powershell
uv run stock-db-backup
# 產物在 data/backups/stock_YYYYMMDD_HHMMSS.db
```

若仍要用 Drive 掛載檔案：

1. 把 Google Drive desktop 安裝好 (掛載成 `G:\` 或 `~/Google Drive/`)
2. 在 Drive 內建一個資料夾，例如 `G:\My Drive\stock-shared\`
3. 編輯 `.env`：
   ```
   STOCK_DB_PATH=G:/My Drive/stock-shared/stock.db
   ```
4. **所有** CLI／dashboard／管線都會經 `default_db_path()` 讀取此路徑（已統一）

> **重要**：要切換到另一台電腦時，請先關閉目前這台的 dashboard / bot，
> 等 Drive 圖示顯示「已同步」後再到另一台啟動。否則 SQLite WAL 檔可能衝突。
> **更安全做法**：本機 `data/stock.db` + `stock-db-backup` + 模式 B Sheets 同步。

---

## 模式 B：Google Sheets API 同步 (推薦)

**多機並行安全，且可從手機 / Forms 直接編輯。**

`stock-cloud-sync --pull` 會先自動備份本地 DB（`data/backups/stock_*_pre-pull.db`），
再以通過 schema／semantic audit 的遠端資料 **整表覆蓋** 本地；雲端 worksheet
若只有 header 或完全空白，會保留本地資料，不會因同步誤清空 canonical table。

### B-1. 建立 Google Cloud Project (一次性)

1. 開啟 https://console.cloud.google.com 並登入
2. 點左上 Project 下拉 → **New Project** → 取個名字 (例如 `stock-bot-sync`)
3. 切到該 Project

### B-2. 啟用必需的 API

1. 左側選單 → **APIs & Services** → **Library**
2. 搜尋 `Google Sheets API` → **Enable**
3. 搜尋 `Google Drive API` → **Enable**

### B-3. 建立 Service Account 並下載金鑰

1. **APIs & Services** → **Credentials**
2. 點 **+ Create Credentials** → **Service account**
3. 隨便取名 (例如 `stock-bot-sa`)，**Role 可留空**，點 **Done**
4. 在 Service accounts 清單點剛建的 SA → **Keys** → **Add Key** → **Create new key** → **JSON** → **Create**
5. 瀏覽器會下載一個 JSON 檔，**請妥善保存** (這就是你的雲端密碼)

> 範例檔名：`stock-bot-sa-2026abc.json`
>
> JSON 內有一行 `"client_email": "stock-bot-sa@xxx.iam.gserviceaccount.com"`，
> **記下這個 email**，下一步用得到。

### B-4. 建立 Google Sheet 並授權

1. 到 https://sheets.google.com **新增一張空白 Sheet**，取名 `stock-bot-data`
2. 從網址抓出 Sheet ID：
   ```
   https://docs.google.com/spreadsheets/d/【這一段就是 ID】/edit
   ```
3. 點右上角 **分享 / Share**，把 Step B-3 拿到的 SA email 加為「**編輯者 (Editor)**」
4. 不需要勾「通知人員」，直接送出

### B-5. 設定本專案

把以下兩行加進 `.env`：

```
GOOGLE_SHEET_ID=【B-4 抓到的 Sheet ID】
GOOGLE_SA_JSON_PATH=【B-3 下載的 JSON 檔絕對路徑】
```

範例 (Windows)：
```
GOOGLE_SHEET_ID=1AbC2dEFghIjklMnopQrSTuVwxYZ0123456789AbCdE
GOOGLE_SA_JSON_PATH=C:/Users/ASUS/secrets/stock-bot-sa-2026abc.json
```

### B-6. 安裝雲端套件

```bash
uv sync --extra cloud
```

這會裝 `gspread` 與 `google-auth`。沒裝這兩個套件的話，本地 DB 仍可正常用，
只是按下「Push/Pull」會跳「依賴未安裝」錯誤。

### B-7. 首次同步

1. 啟動 dashboard：`uv run stock-dashboard`
2. 左側選「⚙️ 系統與診斷 → 資料庫 / 雲端同步」
3. 點「**🔌 測試連線**」，應該看到 `連線成功 — Sheet 標題：「stock-bot-data」`
4. 點「**⬆ Push 全部**」把本地空表結構推到雲端
5. 回去 Google Sheet 確認，會看到目前 10 個可同步 worksheet:
   - `stock_info`
   - `etf_meta`
   - `monthly_revenue`
   - `valuation_daily`
   - `dividend_history`
   - `quarterly_report`
   - `watchlist`
   - `price_history`
   - `llm_analysis_history`
   - `llm_daily_reports`
6. 之後在 Sheet 上手動編輯 / 加 row → 回 dashboard 點「**⬇ Pull 全部**」就會拉進本地

### B-8. 多機協作流程

每台電腦只要做兩件事：
1. **開機 / 開始工作前** → 點「⬇ Pull 全部」或「🔄 智能同步」
2. **下班 / 換機器前** → 點「⬆ Push 全部」或「🔄 智能同步」

「智能同步」預設仍是相容模式：比較本地與雲端的 `MAX(updated_at)`，新的覆蓋舊的。
對歷史表或多機同時寫入，建議改用逐列合併：

```text
uv run stock-cloud-sync --row-level
uv run stock-cloud-sync --row-level --tables price_history,monthly_revenue
```

逐列模式依主鍵與 `updated_at` 合併，保留兩端各自新增的資料；同時間戳但內容
不同時保留本地並在結果中回報衝突數。正式切換前請先在備份資料庫與測試 Sheet
觀察一次結果。

同步安全規則：Google Sheets 是可編輯鏡像，不是 raw archive 或 evidence 的來源。
pull／row-level sync／table-level sync 會先檢查 canonical 資料的必要欄位、日期、
數值與 OHLC invariant；無效資料整批拒絕，不會先清空本地資料。未變更的本地列會
保留 `source_record_id`、品質與 evidence metadata；新增或被手動修改的雲端列會
清除不可信的來源欄位並標為 `quality_status=unknown`，必須重新由正式來源擷取後
才可作為有證據的分析資料。

同步邊界同時在 CLI 與 `GoogleSheetSync` API 內強制執行：只有 10 張
`SYNCABLE_TABLES` 可同步；`source_runs`、`evidence_items`、記憶、research
與其他 local-only 表即使由程式直接呼叫 API 也會被拒絕，不能讓 Google Sheets
取代本地 raw／evidence SSOT。

Push 的寫入順序是「先更新新 payload，再清理多餘舊列」，不會先把 worksheet
清空；若更新請求在網路中斷，原有雲端資料仍保留，避免同步失敗造成空表。

---

## 進階：用 Google Forms 從手機新增股票

讓你坐捷運看到一檔有興趣的股票時，掏出手機 30 秒就能加進 watchlist。

### F-1. 建立 Form

1. 開啟 https://forms.google.com → 空白表單
2. 表單名稱：`新增股票到 Watchlist`
3. 加幾個問題 (跟 `watchlist` table 欄位對齊)：
   | 問題 | 類型 | 必填 |
   |------|------|:----:|
   | symbol | 簡答題 | ✅ |
   | name | 簡答題 | — |
   | tags (逗號分隔) | 簡答題 | — |
   | note | 段落 | — |

### F-2. 把回覆連到既有 Sheet

1. Form 編輯畫面 → **回覆** 分頁 → 試算表圖示
2. 選 **選取現有的試算表** → 選 B-4 建的 `stock-bot-data`
3. Google 會在 Sheet 中**新增一個 worksheet** (預設叫 `表單回應 1`)，把每次表單回覆變成一列

### F-3. 從 Sheet 同步到主表 (兩種做法)

**方法 1 (推薦)：在 Sheet 內用公式自動 mirror**

在 `watchlist` worksheet 的 A1 寫：
```
=ARRAYFORMULA(IFERROR(IF(LEN('表單回應 1'!B2:B)>0, {'表單回應 1'!B2:B, '表單回應 1'!C2:C, '表單回應 1'!D2:D, '表單回應 1'!E2:E, '', TEXT('表單回應 1'!A2:A,"YYYY-MM-DD HH:mm:ss")}, )))
```
(欄位順序依你的問題微調)

**方法 2：手動把表單回應複製到 `watchlist`**

懶得寫公式，每天看一次就好。

之後在 dashboard 按「⬇ Pull」會把手機新增的股票寫入本地 SQLite `watchlist`
（SSOT）。`data/watchlist.json` 僅在本機透過 `bot.watchlist` 新增／更新／移除時
寫成衍生相容快照；Pull 本身不會改 JSON，也不應再以過期 JSON 刪 SQLite 列。

---

## 故障排除

| 錯誤訊息 | 可能原因 | 解法 |
|---------|---------|------|
| `需要安裝雲端同步套件` | 沒裝 gspread | `uv sync --extra cloud` |
| `無法開啟 Google Sheet` | SA 沒被加為編輯者 | 在 Sheet 右上「分享」加 SA email |
| `Service account 檔案不存在` | JSON 路徑錯誤 | 用絕對路徑、注意 Windows 用 `/` 而非 `\` |
| `Permission denied: Google Sheets API has not been used` | 沒啟用 API | 回 GCP Console 啟用 Sheets API |
| 雙機都 Push 結果有人覆蓋 | 沒先 Pull 就 Push | 養成「Pull → 做事 → Push」習慣，或固定用「🔄 智能同步」 |

---

## 安全性提醒

- **Service Account JSON 等同於密碼**，請加進 `.gitignore`，不要 commit
- 預設 `.env.example` 不包含 JSON 內文，只放路徑
- 如果不小心 commit 了金鑰，**立刻到 GCP Console 把該 key 刪除並重新產生**
- Sheet 本身只給 SA 編輯者權限即可，你自己用 Google 帳號是擁有者，雙方都能編

---

## 進一步擴充

* 想自動定時同步？使用 CLI（見下方「LLM 多機同步 SOP」）或把
  `SCHEDULER_CLOUD_SYNC_INTERVAL_MIN` 設為正數，讓 `stock-scheduler` 定期執行
  `stock-cloud-sync`。
* 想要更細的衝突解決 (row-level merge)，請使用 `stock-cloud-sync --row-level`。

---

## 混合模式：靜態資料路徑總表

本專案用 **兩條通道** 上雲（建議同時設定）：

| 通道 | 環境變數 | 適用資料 |
|------|---------|---------|
| **Drive 檔案鏡像** | `GOOGLE_CACHE_DIR` | `data/` 下 JSON / CSV / PDF 快取 |
| **Google Sheets** | `GOOGLE_SHEET_ID` + `GOOGLE_SA_JSON_PATH` | `stock.db` 內可同步表格 |

### 靜態／半靜態檔案 (`data/`)

| 路徑 | 用途 | Drive 鏡像 | Sheets |
|------|------|:----------:|:------:|
| `data/meta/company_info.json` | 上市/上櫃公司基本資料快取 | 是 | 經 `stock_info` 表 |
| `data/meta/market_map.json` | 市場別對照 (twse/tpex) | 是 | 否 |
| `data/supply_chain.json` | 美股→台股供應鏈 | 是 | 否 |
| `data/active_etfs.json` | 主動式 ETF 清單 | 是 | `etf_meta`（研究管線啟動時以 config-only metadata 物化） |
| `data/calendar/conferences_*.json` | 法說會行事曆 | 是 | 否 |
| `data/calendar/exhibitions.json` | 展覽清單 | 是 | 否 |
| `data/macro/macro_*.json` | 總經快取 | 是 | 否 |
| `data/fundamentals/**` | 基本面快取 | 是 | `monthly_revenue` / `quarterly_report` |
| `data/chips/**`, `data/distribution/**` | 籌碼／集保 | 是 | 否 |
| `data/technicals/<ticker>/` | K 線 CSV | 是 | `price_history` |
| `data/etf_holdings/**` | ETF 持股 | 是 | 否 |
| `data/mops_downloads/`, `data/mops_cache/` | MOPS 下載 | 是 | 否 |
| `data/watchlist.json` | 觀察清單相容快照（SQLite 為 SSOT；僅未初始化時匯入一次） | 是 | 經 SQLite `watchlist` |

Row-level 同步若遇到兩端相同時間但內容不同，會保留雙方內容於本地
`sync_conflicts`（不會同步到 Google Sheets）。可用下列指令查詢與裁決：

```powershell
uv run stock-sync-conflicts --root C:\skymiku\stock --status open --json
uv run stock-sync-conflicts --root C:\skymiku\stock --resolve <conflict_id> --resolution "保留本地版本"
```

### LLM 產物

| 路徑 / 表 | 內容 | Drive 鏡像 | Sheets |
|-----------|------|:----------:|:------:|
| `log/llm_calls/llm_calls_*.jsonl` | 完整 prompt/response 日誌 | 否 | 經 `llm_analysis_history`（摘要欄位） |
| `data/auto_llm/<ticker>.json` | 個股自動研究 | 是 | `llm_analysis_history` |
| `data/auto_llm/research_log.jsonl` | CLI 批次摘要 | 是 | 否 |
| `data/pipeline_runs/<ts>/` | 法說管線輸出 | 是 | 否 |
| `data/intraday/<date>/` | 盤中戰情報告 | 是 | `llm_daily_reports` |
| `data/next_day_watch/<date>/` | 明日當沖報告 | 是 | `llm_daily_reports` |

`stock.db` 建議**每台機器各一份**（本地路徑），不要兩台同時開啟放在 Drive 上的同一個 `.db` 檔。

---

## LLM 多機同步 SOP（混合模式）

### 必要設定

```env
GOOGLE_CACHE_DIR=G:/My Drive/stock-cloud-cache
GOOGLE_SHEET_ID=你的_Sheet_ID
GOOGLE_SA_JSON_PATH=.secrets/your-sa.json
SCHEDULER_CLOUD_SYNC_INTERVAL_MIN=60   # 選用：排程自動 sync；0=停用
```

並安裝雲端套件：`uv sync --extra cloud`

### 日常流程

1. **機器 A** 跑研究（`stock-auto-research` / `stock-auto-research --llm-only` / `stock-nextday` 等）
   - 結果寫入本地 `stock.db`（`llm_analysis_history`、`llm_daily_reports`）
   - 大型 JSON/MD 自動鏡像到 `GOOGLE_CACHE_DIR`（若已設定）
2. **上傳結構化資料**（擇一）：
   - `uv run stock-cloud-sync --push`
   - 或 `uv run stock-cloud-sync`（智能 sync，依 `updated_at`）
   - 或僅 LLM 表：`uv run stock-cloud-sync --push --tables llm_analysis_history,llm_daily_reports`
3. **機器 B** 開始工作前：
   - `uv run stock-cloud-sync --pull`（或 `--sync`）
   - 讀檔時會從 `GOOGLE_CACHE_DIR` 還原缺的 `data/auto_llm/`、`data/next_day_watch/` 等
4. Dashboard「明日當沖 / 個股 LLM」會**優先讀 DB**，因此 Pull 後即可看到相同分析。

### CLI 參考

```bash
uv run stock-cloud-sync                  # 全部 SYNCABLE 表智能 sync
uv run stock-cloud-sync --push           # 本地 → Sheets
uv run stock-cloud-sync --pull           # Sheets → 本地
uv run stock-cloud-sync --tables llm_analysis_history,llm_daily_reports
uv run stock-cloud-sync --root C:\skymiku\stock --push  # 從其他工作目錄啟動
```

`--root` 會同時決定 `.env`、`data/stock.db` 與 pull 前備份位置；排程或
Windows Task Scheduler 不應依賴啟動時的工作目錄。

---

## Google cache folder (cross-machine fetch cache)

Use `GOOGLE_CACHE_DIR` when two machines should reuse fetched JSON/CSV/PDF
artifacts instead of refetching them from TWSE, MOPS, yfinance, news sources,
or ETF pages.

Recommended setup:

```env
GOOGLE_CACHE_DIR=G:/My Drive/stock-cloud-cache
```

Behavior:

- When a fetcher writes a cache file under `data/`, the app mirrors it to the
  same relative path under `GOOGLE_CACHE_DIR`.
- Immutable `data/raw/<source>/<sha256>.*` files registered by `SourceRegistry`
  are mirrored as well; if a known raw file is missing locally, research replay
  restores it from the mirror and still verifies the manifest SHA-256.
- Before a fetcher calls the external source, it restores the local file from
  `GOOGLE_CACHE_DIR` if the local file is missing or older.
- Keep `data/stock.db` local on each machine. Use Google Sheets for structured
  SQLite table sync, and use `GOOGLE_CACHE_DIR` for large/raw file caches.
- Cloud mirror／restore 是 best-effort；Drive 權限、磁碟或網路暫時失敗時，
  本地 canonical data 仍會保存，下一次同步可再補鏡像或還原。


## Watchlist 保存與刪除規則

- SQLite 提交成功後才原子更新 JSON 相容快照。資料庫錯誤會直接回報；若只剩快照寫入失敗，訊息會明確告知 SQLite 已儲存。
- JSON 只在首次初始化且 SQLite 沒有列時匯入。已初始化的空清單不再重匯舊 JSON。
- 編輯器以載入版本檢查衝突；未修改的舊列不會覆蓋其他人的新修改。遇到衝突，重新載入後再編輯。
- 移除會保留 `deleted_at` 與 `revision`，這些刪除標記也會同步。缺列不代表刪除，請從清單操作移除；不要從 Sheets 直接刪除資料列。
- Watchlist 的 `sync`、`push` 一律先逐列合併，再寫回 Sheets；`pull` 在本地合併且保留刪除標記。此例外避免整表覆寫清掉另一端的刪除紀錄或新增項目。
- 明確重新加入項目才可恢復，並由 `restored_from` 記錄已觀察到的刪除版本。請保留這三個同步欄位，不要自行編輯。
- 舊版六欄 Sheets 可在首次同步時隨資料一併升級；所有同步端必須先升級此版本，再恢復跨機同步，避免舊程式移除新欄位。
- Google Sheets 沒有提供此流程所需的整表條件式提交；目前可偵測本地快照過期，但多台同時寫 Sheets 仍需錯開操作並再次同步確認。離線測試不代表真實帳號或多機同步已驗收。

資料庫 migration 1–5 保留既有歷史校驗碼；從 migration 6 起使用固定 SQL 與不可變校驗碼，升級與紀錄在同一交易提交。遇到校驗碼不符會停止開啟，請先備份資料庫並核對程式版本，不要手動改寫校驗碼。
