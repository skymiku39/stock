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
