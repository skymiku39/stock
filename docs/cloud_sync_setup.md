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

**最簡單，但兩台電腦不能同時使用**(會 corrupt SQLite)。

1. 把 Google Drive desktop 安裝好 (掛載成 `G:\` 或 `~/Google Drive/`)
2. 在 Drive 內建一個資料夾，例如 `G:\My Drive\stock-shared\`
3. 編輯 `.env`：
   ```
   STOCK_DB_PATH=G:/My Drive/stock-shared/stock.db
   ```
4. 啟動 dashboard，DB 會直接在 Drive 資料夾建立並被 Drive 自動同步

> **重要**：要切換到另一台電腦時，請先關閉目前這台的 dashboard / bot，
> 等 Drive 圖示顯示「已同步」後再到另一台啟動。否則 SQLite WAL 檔可能衝突。

---

## 模式 B：Google Sheets API 同步 (推薦)

**多機並行安全，且可從手機 / Forms 直接編輯。**

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
5. 回去 Google Sheet 確認，會看到自動建好的 5 個 worksheet:
   - `stock_info`
   - `etf_meta`
   - `monthly_revenue`
   - `quarterly_report`
   - `watchlist`
6. 之後在 Sheet 上手動編輯 / 加 row → 回 dashboard 點「**⬇ Pull 全部**」就會拉進本地

### B-8. 多機協作流程

每台電腦只要做兩件事：
1. **開機 / 開始工作前** → 點「⬇ Pull 全部」或「🔄 智能同步」
2. **下班 / 換機器前** → 點「⬆ Push 全部」或「🔄 智能同步」

「智能同步」邏輯：比較本地與雲端的 `MAX(updated_at)`，新的覆蓋舊的。
規則簡單可預期 — 想要更安全的話，固定 pull 後再做事、做完 push 就好。

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

之後在 dashboard 按「⬇ Pull」就會把手機新增的股票拉進本地 DB 與 JSON。

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

* 想自動定時同步？寫個 Windows Task Scheduler / cron job 在每天開盤前後執行：
  ```python
  from bot.cloud_sync import GoogleSheetSync, load_config_from_env
  sync = GoogleSheetSync(load_config_from_env())
  sync.sync_all()
  ```
* 想要更細的衝突解決 (row-level merge)？可以在 `cloud_sync.py` 自己加一個
  `merge(table)` 方法，比對 PK 後依 `updated_at` 逐 row 取最新。
