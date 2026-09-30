# C 槽搬移與驗收紀錄

## 現況摘要（2026-09-22）

以下為**文件維護當日**對「何者為現況、何者為歷史快照」的對齊說明；**不發明**
live 計數、來源 ID、憑證或驗收證據。

| 主題 | 現況 |
|------|------|
| 正式工作目錄 | `C:\skymiku\stock`（勿從已清空的 D 槽路徑啟動） |
| 預設 `RUN_MODE` | `watch`（見 `src/bot/config.py`、`.env.example`） |
| 主動追蹤清單 | 內建 **27 檔 `*A` + ALI006 ＝ 28**（`DEFAULT_ACTIVE_ETFS`） |
| Dashboard 頁面 | 以 `src/bot/dashboard/nav.py` 為權威；`src/bot/dashboard.py` 僅為 shim |
| 雲端同步未設定憑證 | `stock-cloud-sync` **exit code 2**（見 `cloud_sync_cli.py`） |
| 即時缺口／完整性 | 請在正式根目錄執行只讀 `stock-data-audit --json` 與 `stock-system-preflight --json`；能力報告可選 `--audit-json` 帶入 audit 產物 |
| 下方各節數字與備份路徑 | **歷史驗收時間線**（2026-09-18～21 等），保留供追溯，**不是**自動更新的現況計數 |

> 歷史段落中若曾寫「目前仍有 4 筆…／43 筆…」，那是當時 audit 快照；現況計數以當次
> `stock-data-audit` 為準，勿把歷史 4／43 抄進能力報告或當日運維結論。

## 正式工作目錄

目前正式使用的專案目錄是：

```text
C:\skymiku\stock
```

`.venv` 已重新以 C 槽專案安裝，editable package、Python import 與 `CA_PATH`
都指向 C 槽。不要再從 `D:\skymiku\stock` 啟動程式。

## 已驗證項目（歷史快照；搬移當期）

> **歷史標記**：本節與後續各時間戳段落為 C 槽搬移／補齊期間的驗收紀錄，數字與
> 備份檔名以當次為準。現況請改看文首「現況摘要（2026-09-22）」並重跑
> `stock-data-audit`／`stock-system-preflight`。

- SQLite `data\stock.db`：42 張表，`PRAGMA integrity_check=ok`。
- C 槽資料庫與恢復備份的表格與筆數一致。
- `bot` 底下 150 個模組全部可匯入，沒有 import failure。
- `pyproject.toml` 的 30 個 `stock-*` CLI 入口全部通過 `--help` smoke。
- 非 integration／slow 回歸測試：735 passed、1 個第三方相容性 warning。
- 公開資料源唯讀驗證：48 項 OK、1 項 TAIFEX exact-cache partial warning、0 failure。
- Shioaji simulation login、2330 contract、snapshot 與 TWSE MIS read-only poll 成功；沒有呼叫下單。
- `stock-preflight`／來源驗證維持 `RUN_MODE=watch`、`SIMULATION=true`。
- `stock-data-audit --json`（**當時**）成功：611/611 raw archive 雜湊一致、SQLite integrity ok、無孤立資料；
  canonical 13,324 rows 且 8 張 canonical table 均為 ok，其中 `dividend_history=2,141`；
  **當時快照**仍有 4 筆沒有可證明
  raw manifest 的 legacy source run，另有 43 筆明確標記
  `legacy_unresolved` 的舊 LLM 分析。

研究層唯讀稽核曾發現 1 筆既有 MOPS HTML/PDF 型態錯誤；該鏈已保留原始檔並 quarantine，
另以真正 27 頁 PDF 建立新的 exact chain，新的簡報入口也已拒絕同類資料。**當時** audit
gap 為 2（研究期間／版本／隔離警告與 4 筆歷史 source run），research errors=0。

## 恢復備份

搬移過程保留一份可恢復備份：

```text
C:\skymiku\stock-D-backup-20260918
```

在確認 C 槽長期運作、且不再需要回復前，不要刪除這份備份。

目前上一份可恢復 SQLite 備份：

```text
C:\skymiku\stock\data\backups\stock_20260920_085255_full-fundamentals-final.db
```

主庫與此備份的 `PRAGMA integrity_check` 均為 `ok`。

最新完整來源驗證後的備份內容與當時 audit 的 594 個 source documents 及
11,185 筆 canonical rows 一致。

TAIFEX fallback 驗收後已建立一份可恢復備份：

```text
C:\skymiku\stock\data\backups\stock_20260920_091150_taifex-fallback-final.db
```

主庫與此備份的 `PRAGMA integrity_check` 均為 `ok`，核心研究層與 canonical
資料筆數一致；上一份備份仍保留，不覆蓋既有恢復點。

全市場股利回填與完整回歸測試後的最新備份：

```text
C:\skymiku\stock\data\backups\stock_20260920_092130_full-fundamentals-dividend-final.db
```

主庫與此備份均為 `PRAGMA integrity_check=ok`，canonical、research、evidence、
claim 與 FTS 計數一致。

問答整合 smoke 後的最新恢復點：

```text
C:\skymiku\stock\data\backups\stock_20260920_093015_ask-integration-final.db
```

主庫與此備份均為 `PRAGMA integrity_check=ok`，問答、evidence、memory 與 FTS
計數一致。

跨資源 smoke 完成後的最新恢復點：

```text
C:\skymiku\stock\data\backups\stock_20260920_093241_final-integration-smoke.db
```

主庫與此備份均為 `PRAGMA integrity_check=ok`，所有核心資料表及 FTS 計數一致。

來源域稽核後的最新恢復點：

```text
C:\skymiku\stock\data\backups\stock_20260920_094021_provenance-domain-audit-final.db
```

主庫與此備份均為 `PRAGMA integrity_check=ok`，canonical、research、evidence、
memory、ask 與 FTS 計數一致。

## 尚待人工處理

1. `D:\skymiku\stock` 內容已清空；本次執行環境拒絕刪除命令，因此目前只剩空目錄。確認沒有程序使用後，可手動刪除這個空目錄。
2. Google Sheets／Drive 同步尚未設定 `GOOGLE_SHEET_ID` 與 `GOOGLE_SA_JSON_PATH`，因此只完成程式與 mock／未啟用路徑驗證，尚未完成實際雲端 live sync。
3. 專案內的歷史 log 可能保留 D 槽 traceback；這些是歷史證據，不應當成目前執行路徑，也不需要改寫。

## 建議啟動方式

```powershell
cd C:\skymiku\stock
uv run --no-sync stock-preflight --no-login --json
uv run --no-sync stock-data-audit --json
uv run --no-sync stock-validate
```

從專案外部目錄執行時，報告也會固定落在 C 槽專案根目錄：

```powershell
cd C:\Windows\Temp
uv run --project C:\skymiku\stock --no-sync stock-validate `
  --root C:\skymiku\stock `
  --json data\validation\external_root_report.json `
  --md data\validation\external_root_report.md
```

本次實跑 exit code=0；45 OK、2 個未啟用警告、0 失敗，且 `C:\Windows\Temp` 沒有產生同名報告；logger 也沒有在外部 CWD 新增檔案。

## 全市場研究層補齊（2026-09-20；歷史）

使用 `stock-fundamentals-refresh --materialize-all` 將既有 raw archive
以 exact manifest 重播至 canonical、research、evidence、claim 與 ingest-run 層
（**只重播既有 exact raw；不宣稱修復歷史 orphan 或猜測 provenance**）：

- 月營收 1,978 筆、估值 1,964 筆、季報 1,974 筆、股利 2,141 筆，物化失敗 0。
- 主庫與 `stock_20260920_092130_full-fundamentals-dividend-final.db` 備份完整性均為 ok。
- 物化不含網路抓取；需要更新資料時，先由 fetcher 建立新的 raw manifest，再執行物化。

## 研究來源契約稽核（2026-09-20）

`bot.research_audit` 以唯讀方式驗證研究文件能回到 exact source document、raw hash、
日期順序、chunks 與 MIME／副檔名；同一來源參照的多版本 raw 不覆蓋。正式庫目前為
14,139 research documents、14,351 chunks；22 組多版本來源雖沒有 `as_of`，但都有
互異且可解析的 `retrieved_at`，因此可辨識「抓取版本」，仍標記「資料期間未標定」；
另有 18 組同一來源參照的多版本衝突候選，全部保留原始 hash。

稽核後建立可恢復備份：
`data\backups\stock_20260920_095807_research-contract-audit-final.db`；主庫與備份
`PRAGMA integrity_check` 均為 `ok`，核心資料筆數一致。

MOPS 修復與 derived chain quarantine 後的最新恢復點：
`data\backups\stock_20260920_101412_mops-repair-final.db`；主庫與備份
`PRAGMA integrity_check` 均為 `ok`，`source_documents=595`、
`research_documents=14,139`、`research_chunks=14,351`、`evidence_items=12,199`、
`claims=71,147`、`memory_items=126`、`ask_runs=89` 一致。

正式來源驗證 follow-up（2026-09-20 10:30）後的最新恢復點：
`data\backups\stock_20260920_103036_source-validation-followup.db`；主庫與備份
`PRAGMA integrity_check` 均為 `ok`，`source_documents=600`、
`research_documents=14,143`、`research_chunks=14,361`、`evidence_items=12,206`、
`claims=71,190`、`memory_items=126`、`ask_runs=89` 一致。

QueryPlan metadata 驗收（2026-09-20 10:41～10:44）後的最新恢復點：
`data\backups\stock_20260920_104452_ask-query-normalization-followup.db`；主庫與備份
`PRAGMA integrity_check` 均為 `ok`，`source_documents=611`、
`research_documents=14,143`、`research_chunks=14,361`、`evidence_items=12,214`、
`claims=71,190`、`memory_items=131`、`ask_runs=94` 一致。

同次 research audit 為 `documents=14,143`、`chunks=14,361`、`errors=0`、
`warnings=3`：1 筆 HTML/PDF quarantine、18 組版本衝突候選、23 組資料期間未標定；
這些都是保留原始證據的可追蹤警告，不是被系統默默合併或刪除。

自動研究狀態傳遞修補（2026-09-20 11:00～11:04）後：
`stock-auto-research` 真實執行為 ETF 28/28 成功、187 個焦點、錯誤 0、警告 9，
其中 83 個標的明確標示沒有籌碼交易日資料，TAIFEX 也標示 exact-cache partial；
最新恢復點為 `data\backups\stock_20260920_110407_pipeline-status-followup.db`。
主庫與備份 integrity 均為 `ok`，`source_documents=647`、
`research_documents=14,179`、`research_chunks=14,506`、`claims=72,887`、
`memory_items=131`、`ask_runs=94` 一致。

2026-09-20 11:15 又完成 macro evidence 品質一致化：混合／未知來源日期、品質備註、
或 TAIFEX partial／cache 都會在研究切片與 `!ask` evidence 標示 `partial`；aligned
且無降級條件才標示 `ok`。完整非 integration／slow 回歸為 `730 passed, 1 warning`。

品質回填後恢復點：
`data\backups\stock_20260920_112140_macro-quality-repair-final.db`。
48 份 macro raw 以 exact source ID 重跑，44 筆 reused、4 筆建立／補齊、0 失敗；
主庫與備份 integrity 均為 `ok`，`source_documents=647`、`evidence_items=12,216`、
`research_documents=14,183`、`research_chunks=14,510`、`claims=72,907` 一致。

2026-09-20 11:26 的 preflight simulation smoke 也已修正：Shioaji 不支援餘額時明確
回傳 `balance_available=false`／`balance=null`，不把未知資料當成 0；完整回歸為
`732 passed, 1 warning`。

2026-09-20 11:30～11:32 的完整來源驗證為 49 項 `48 OK、1 warning、0 failure`；
Shioaji login、2330 contract／snapshot、TWSE MIS poll 均成功，唯一 warning 為 TAIFEX
live 403 的 exact-cache partial。驗證後 raw archive 為 655/655，最新恢復點為
`data\backups\stock_20260920_113251_source-validation-final.db`，主庫與備份 integrity
均為 `ok`。

11:35 的 `stock-ask --live --no-llm --json` 也完成實跑：QueryPlan、news／MOPS／MIS
adapter、freshness 過濾與保守 fallback 正常；30 個 CLI 入口 `--help` 全數成功。
最新問答恢復點為 `data\backups\stock_20260920_113645_ask-cli-final.db`，主庫與備份
integrity 均為 `ok`。

11:41～11:42 的自動研究實跑也完成：ETF 28/28、research 28/28 reused／成功、焦點
187、錯誤 0、警告 6、狀態 partial；最新恢復點為
`data\backups\stock_20260920_114323_auto-research-final.db`，raw 658/658、SQLite
integrity 均為 `ok`。

## MOPS 法說會行事曆研究切片（2026-09-20 11:49～11:53）

已將既有 MOPS 法說會行事曆 raw archive 接入 exact-only research layer：16 份
`source_documents` 中 12 份成功物化，4 份空白快照保留為 `empty`；共 2,384 筆有效
活動、8 筆同快照重複資料去重、0 筆 invalid／failed。每筆活動均可沿
`source_document → research_document/chunk → evidence → source-fact claim` 回放，
不依 URL、標題或相似度猜測來源，且冪等重跑不會重複建立資料。

本輪完整非 integration／slow 回歸為 `735 passed, 1 warning`。最新稽核統計為
`source_runs=1,894`、`source_documents=658`、`research_documents=14,202`、
`research_chunks=14,813`、`evidence_items=12,227`、`evidence_links=12,251`、
`claims=75,322`；raw `658/658` hash 相符、SQLite integrity=ok、FTS 一致、孤立連結
為 0、research errors=0。正式恢復點為：
`data\backups\stock_20260920_115327_mops-calendar-research-final.db`。

## 最新來源驗證（2026-09-20 12:00～12:01）

`stock-validate --root C:\skymiku\stock --shioaji --mis-seconds 3` 完成 **49 OK、0
warning、0 failure**。MOPS、新聞、DuckDuckGo、macro、TAIFEX、28 檔 ETF、Shioaji
simulation 與 TWSE MIS read-only poll 均成功；沒有呼叫下單。

驗證後稽核為 `source_runs=1,924`、`source_documents=662`、
`research_documents=14,205`、`research_chunks=14,816`、`evidence_items=12,229`、
`evidence_links=12,253`、`claims=75,335`；raw `662/662` hash 相符、SQLite
integrity=ok、FTS 一致、orphan=0。最新恢復點為：
`data\backups\stock_20260920_120117_source-validation-49ok-final.db`。

同一階段完成 153 個 `bot` 模組 import sweep，全部成功。未設定 Google Sheets 憑證時，
`stock-cloud-sync --push` 以 exit code 2 拒絕，主庫 SHA-256 前後一致，確認同步模組
不會在未配置或失敗時改動本地資料。

## 搜尋研究層補齊（2026-09-20 12:14～12:17）

Google News RSS／DuckDuckGo HTML 的 95 份既有 exact raw response 已回放：92 份成功
建立 `news_search`／`web_search` research、evidence 與 source-excerpt claims，3 份
空回應標為 `empty`，0 份失敗。完整回歸為 `739 passed, 1 warning`；最新稽核為
`source_documents=664`、`research_documents=14,297`、`research_chunks=15,674`、
`evidence_items=13,803`、`evidence_links=13,827`、`claims=79,239`，raw `664/664`
hash 相符、SQLite integrity=ok、FTS 一致、orphan=0。恢復點為：
`data\backups\stock_20260920_121722_web-search-research-final.db`。

12:18 的 production `stock-ask --live --no-llm` 熱路徑也已驗證：Google News 取得 8 筆，
MOPS 空結果保持 partial，無 LLM 時保守回覆不確定；trace／EvidencePack 有保存，未產生
未驗證結論。最新稽核為 `source_documents=666`、`research_documents=14,298`、
`research_chunks=15,677`、`evidence_items=13,808`、`evidence_links=13,832`、
`claims=79,247`，恢復點為：
`data\backups\stock_20260920_121902_web-search-ask-hotpath-final.db`。

## 最終整合再驗收（2026-09-20 12:35～12:38）

完整 `stock-validate --shioaji --mis-seconds 3` 再次為 **49 OK、0 warning、0 failure**。
驗證後 `source_documents=670`、`research_documents=14,303`、`research_chunks=15,682`、
`evidence_items=13,810`、`claims=79,270`，raw `670/670` hash 相符、SQLite integrity=ok、
FTS 一致、orphan=0。恢復點為
`data\backups\stock_20260920_123602_post-source-revalidation.db`。

再執行 `stock-ask --live --no-llm` 後，最終恢復點為
`data\backups\stock_20260920_123745_post-ask-integration.db`；最終 raw `673/673`、
SQLite integrity=ok、FTS 一致、orphan=0。未配置 Google Sheets 時同步命令以 exit code 2
安全拒絕且 SHA-256 不變。新增整合 preflight 後完整回歸為 `744 passed, 1 warning`，
156 個 bot 模組 import sweep 全部成功。

同一輪也驗證了 30 個 CLI entry point 的 `--help`、外部 CWD 的 Dashboard health（`ok`／
首頁 HTTP 200），以及外部 CWD 的 `stock-scheduler --once --dry-run`（exit code 0）。
