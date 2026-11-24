# FinMind 台股交叉核對（第二階段）

資料來源：FinMind（https://finmindtrade.com）。報告與日誌一律標示來源 FinMind；僅限個人、非商業使用，不轉散布原始資料。

## 目標

以 FinMind 當第二來源，核對資料庫（公開資訊觀測站、證交所來源）的月營收與損益表關鍵科目，找出不一致或資料庫缺漏，寫成報告並接到資料新鮮度檢查（`bot.data_freshness`）當一項提醒。定位是「交叉核對」，不取代、不回寫資料庫。

## 範圍

- 對象：先只做持股（`bot.held_symbols.load_held_symbols`，即 `config/held_symbols.json` 與 `data/held_symbols_seen.json` 的聯集）；`--symbols` 可指定。之後再擴到追蹤名單。
- 月營收：`TaiwanStockMonthRevenue`，最近 3 個月（以 FinMind 回傳的最新 3 個營收月份為準）。
- 損益表：`TaiwanStockFinancialStatements`，最近 4 季；科目 `Revenue`、`EPS`、`OperatingIncome`，比對營收、EPS、營業利益率（OperatingIncome / Revenue）。
- 執行：`python -m bot.free_sources.finmind_crosscheck [--root .] [--db 路徑] [--symbols 2330,1514] [--dry-run]`。
- 輸出：`<root>/data/reports/finmind_crosscheck_<台灣日期>.json`（原子寫入，同日覆寫）。

## 不做的事

- 不寫入資料庫、不修正資料；只報告。
- 不核對資產負債表、現金流量表、籌碼、價格（之後的階段）。
- 不查全市場（免費額度不允許）；不接進排程（此階段只提供指令與新鮮度檢查）。
- 不影響任何交易路徑；保持 SIMULATION=true。

## 比對規則（2026-11-24 以 2330、1514 實測確認）

| 項目 | 規則 |
|---|---|
| 單位 | 資料庫營收為千元，FinMind 為元：FinMind 除以 1000 再比 |
| 月份對齊 | FinMind `date` 是營收月份的下個月 1 日，改用 `revenue_year`、`revenue_month` 組成 `YYYY-MM` 對資料庫 `year_month` |
| 季別對齊 | 資料庫 `quarterly_report` 存「年初累計」（Q3 = Q1+Q2+Q3），FinMind 為單季；FinMind 的 Q1..Qn 加總後再比。累計所需任一季缺資料 → `source_missing`（無法驗證） |
| 季別代碼 | FinMind `date` 月份 3/6/9/12 對應 `YYYYQ1..Q4` |
| 營收容許誤差 | 相對 0.1% |
| EPS 容許誤差 | 絕對 0.05 元，或相對 0.5%，取較大者（累計加權股數造成的小差異） |
| 營業利益率容許誤差 | 0.05 個百分點 |
| 資料庫缺資料 | 該期沒有列，或值為 0 而 FinMind 非 0 → `db_missing`（算提醒，不算不一致） |
| FinMind 缺資料 | `source_missing`，不算不一致 |
| 整體狀態 | `ok`（全部請求成功）、`partial`、`error`、`quota_exceeded`、`skipped`（沒有 `FINMIND_TOKEN`）、`dry_run` |

報告欄位：`source`、`date`、`generated_at`、`symbols`、`requests`、`summary`（match / mismatch / db_missing / source_missing / total）、`inconsistent`、`has_db_missing`、`errors`、`items[]`（symbol、period、field、db、finmind、diff、unit、status）。

資料新鮮度檢查（「FinMind 交叉核對」）：沒有報告 → 資訊；有 mismatch 或 db_missing → 警告；報告超過 3 個日曆日 → 警告；報告狀態為 quota_exceeded/error/partial → 警告；skipped → 資訊；其餘正常。

## 驗收標準與對應測試

1. 沒有金鑰 → skipped，不連網、不寫檔：`test_skipped_without_token`
2. 單位、月份、累計季別換算正確，一致時全部 match：`test_all_match_and_report_written`
3. 超出容許誤差 → mismatch，容許誤差內 → match：`test_mismatch_and_tolerance`
4. 資料庫缺期或為 0 → db_missing；FinMind 缺季 → source_missing：`test_db_missing_and_source_missing`
5. QuotaExceeded 或 402 → 停止後續請求並回報 quota_exceeded，仍寫報告：`test_quota_exceeded_stops`
6. 單一請求失敗 → partial，不中斷其他：`test_source_error_is_partial_and_redacted`
7. 報告原子寫入於 `data/reports/finmind_crosscheck_<台灣日期>.json` 並標示來源 FinMind：`test_all_match_and_report_written`
8. `--dry-run` 不連網不寫檔、`--symbols` 解析、預設用持股名單：`test_main_dry_run_and_default_symbols`
9. 資料新鮮度：無報告=info、一致=ok、不一致/缺資料/過期/額度=warn、skipped=info，且不影響既有檢查：`tests/test_data_freshness.py::test_finmind_crosscheck_check_*`
10. 實測（不進測試）：2330 與 1 檔持股共 4 次請求，真實資料庫唯讀，輸出寫暫存目錄。

## 用量預估

每檔 2 次請求（月營收 1、損益表 1，損益表一次抓近 2 年多季）。持股 N 檔 = 2N 次，遠低於免費 600 次／小時（程式自訂上限 500／小時，記錄於 `data/api_quota/finmind.json`）。擴到約 280 檔需約 560 次，須在一個小時內分批或分兩小時跑；達上限即停止並回報 `quota_exceeded`。

## 第三階段：排程與擴大範圍（2026-11-24）

### 目標

1. 交叉核對每天自動跑一次，不必手動下指令；沒有金鑰就自己略過、不報錯。
2. 範圍從持股擴到追蹤名單，且不超過 FinMind 每小時 600 次的免費額度。
3. 查清楚 1514 的 2025Q4、2026Q1 為什麼資料庫沒有，提出補資料做法（不直接改 `data/`）。

### 設計

- 排程：`scheduler.py` 新增 `finmind_crosscheck`，平日 21:00–23:30 每天一次（`run_once_per_day`）。開關與時段：
  `SCHEDULER_FINMIND_CROSSCHECK_ENABLED`（預設 true）、`SCHEDULER_FINMIND_CROSSCHECK_TIME`（21:00）、`SCHEDULER_FINMIND_CROSSCHECK_END_TIME`（23:30）。
  為什麼選晚上：上市櫃公司月營收在每月 10 日前申報、季報在 5/15、8/14、11/14、3/31 前申報，公開資訊觀測站當天就有；
  本專案每小時的 `collection` 工作會把官方資料寫進資料庫，FinMind 也是當天晚上更新（FinMind 的確切更新時間未確認）。
  晚上兩邊都更新完再比，才不會把「其中一邊還沒更新」誤報成不一致。失敗（額度用完、錯誤）時排程器在時段內每 5 分鐘重試。
- 範圍：持股（`load_held_symbols`）＋追蹤名單（`bot.daily_research.tracked_tickers`：`SYMBOLS`、watchlist、主動式 ETF 最新持股），
  只留公司代號（`[1-9]\d{3}`，ETF 沒有財報）。2026-11-24 估計約 206 檔、每檔 2 次請求 ≈ 412 次。
- 分批：每次執行的請求上限 `FINMIND_CROSSCHECK_MAX_REQUESTS`（預設 300，即 150 檔），同時不超過用量計數器該小時剩下的次數
  （`QuotaTracker.remaining()`，自訂上限 500／小時）。記錄檔 `data/free_sources/finmind_crosscheck_state.json` 存每檔上次核對日、
  資料庫指紋（最新月營收月份與筆數、最新季別與筆數）與問題數。
  每檔依優先序排：① 持股（每天）② 從沒核對過 ③ 資料庫有新資料（指紋變了，例如當週公布了月營收或季報）
  ④ 超過 `FINMIND_CROSSCHECK_RECHECK_DAYS`（預設 7）天沒核對；都不符合的不查。同優先序內，上次核對越早越先。當天已核對過的不重查。
  用不完的額度不硬花；排不進這次的標的列為 `deferred`，下一次再排。
- 報告新增 `batch`（scope／due／selected／deferred／not_due／budget）與 `outstanding`：記錄檔中、這次沒重查、上次核對仍有
  不一致或缺資料的標的，避免分批後前一天的問題從報告消失。資料新鮮度檢查對 `outstanding` 也給警告。
- 指定 `--symbols` 時不分批，照指定的查；`--max-requests` 可覆寫上限。`--dry-run` 會列出這次會查哪些標的。

### 驗收標準與對應測試

| # | 標準 | 測試 |
|---|---|---|
| 3.1 | 預設有 `finmind_crosscheck` 工作：平日 21:00–23:30、每天一次、帶 `--root`；開關關掉就沒有這個工作 | `tests/test_scheduler.py::test_scheduler_finmind_crosscheck_job` |
| 3.2 | 時段可由設定字串（`"20:30"`）覆寫 | `tests/test_scheduler.py::test_scheduler_finmind_crosscheck_job` |
| 3.3 | 沒有金鑰 → `skipped`、結束碼 0、不連網、不寫報告 | `test_skipped_without_token`、`test_main_skipped_exit_code` |
| 3.4 | 優先序：持股 > 從沒核對 > 指紋變了 > 超過重查天數；未到期的不查；當天已查過的不重查 | `test_select_batch_priority_and_not_due` |
| 3.5 | 每次請求數 ≤ min(設定上限, 該小時剩餘額度)，每檔 2 次；超出的列 `deferred` | `test_batch_respects_budget_and_quota_remaining` |
| 3.6 | 該小時剩餘額度不足一檔 → 不送請求、狀態 `quota_exceeded`、結束碼 1（排程器稍後重試） | `test_batch_no_budget_is_quota_exceeded` |
| 3.7 | 兩次執行接力：第二次不重查第一次已查的、改查剩下的；只有兩個請求都成功的標的才記進記錄檔 | `test_batches_rotate_across_runs` |
| 3.8 | 報告的 `outstanding` 帶出先前核對有問題、這次沒重查的標的；新鮮度檢查因此給警告 | `test_outstanding_carried_into_report`、`tests/test_data_freshness.py::test_finmind_crosscheck_check_warns_outstanding` |
| 3.9 | 範圍＝持股＋追蹤名單，只留公司代號；追蹤名單讀取失敗時退回只查持股 | `test_scope_merges_tracked_and_filters_etf` |
| 3.10 | 用量計數器 `remaining()` 回傳各維度剩下次數的最小值，沒限制時回 None | `tests/test_free_sources.py::test_quota_remaining` |
| 3.11 | 實測（不進測試）：用真實資料庫唯讀、暫存 `--root` 跑一次預設範圍的 `--dry-run` 與小上限的實際執行 | 記錄於下方「實測紀錄」 |

### 1514 缺 2025Q4、2026Q1 的原因（2026-11-24 查證）

結論：**來源有抓，存的時候被過濾掉了**（不是 FinMind 或公開資訊觀測站沒資料）。

- 資料庫 `quarterly_report` 中 1514 只有 2026Q2、2026Q3，兩筆都來自證交所 OpenAPI `t187ap06_L_ci`；這個端點只提供「最新一季」的全市場資料，
  所以每天的官方刷新只會補到當季。
- 更早的季別靠 `bot.financial_history.backfill_history`（公開資訊觀測站 `ajax_t163sb04` 歷史頁）回補。這支程式會抓整個市場的頁面、
  原始檔完整保留，但只把 `--symbols` 指定的代號寫進資料庫（`targets` 過濾）。2026-11-06 那次回補指定的 230 檔不含 1514
  （1514 是 2026-11-20 才加進持股名單），所以 2025Q3～2026Q1 只有那 230 檔有資料；2026Q2 起才是全市場約 1,980 檔。
- 驗證：`data/raw/quarterly_financials_history_sii/` 中 2025Q4、2026Q1 的原始檔用 `quarterly_rows()` 解析，都有 1514（亞力）這一列，
  例如 2026Q1 營業收入 2,526,342 千元、EPS 0.73。
- 影響：不只 1514。範圍擴到追蹤名單後，2026Q2 以前的季別多數標的都會是 `db_missing`，新鮮度檢查會持續警告，直到回補為止。

補資料做法（**尚未執行**，要先經使用者同意，因為會寫入真實資料庫）：

```
uv run --offline --no-sync python -m bot.financial_history --root C:\skymiku\stock --symbols 1514 \
  --start 2024-01 --end 2026-03 --quarterly-end 2026Q1 --output <暫存目錄>\history_1514.json
```

- 不加 `--refresh`：`_fetch` 會優先用 `source_documents` 已保留、雜湊驗證過的原始檔，不重新連網（缺的月份才會連網抓）。
- 會寫入 `quarterly_report`、`monthly_revenue` 與對應的研究切片；寫入前先備份 `data/stock.db`。
- 補完後重跑交叉核對（指紋變了，會自動排進下一批）；預期 1514 的 6 項 `db_missing` 變成 `match`。
- 要一次補完整個追蹤名單，就把 `--symbols` 換成追蹤名單；原始檔都在，主要成本是寫入時間。

- 月營收也一樣：1514 在 `monthly_revenue` 只有 2026-08～2026-10 三筆（其他回補過的標的有 94 筆），同一個回補指令會一起補上。

### 實測紀錄（2026-11-24 12:14，真實資料庫唯讀、暫存 `--root`）

範圍取自真實專案的持股與主動式 ETF 最新持股（watchlist 目前是空的），報告、用量與記錄檔都寫在暫存目錄。

| 步驟 | 結果 |
|---|---|
| `--dry-run`（上限 300） | 範圍 207 檔、到期 207、這次選 150（第一個是持股 1514）、延後 57；該小時剩餘額度 500 |
| 實際執行，上限 10 次 | 查 1514、1101、1215、1216、1301 共 10 次請求，狀態 ok；1514 的 2025Q4、2026Q1 共 6 項 `db_missing`，其餘一致 |
| 同日再跑，上限 6 次 | 前 5 檔列為今天已查（`not_due`），改查 1303、1326、1402，45 項全部一致；`outstanding` 帶出 1514 |
| 新鮮度檢查 | 警告：「另有 1 檔先前核對仍有問題（1514）」 |

用量計數器顯示兩次共 16 次請求。
