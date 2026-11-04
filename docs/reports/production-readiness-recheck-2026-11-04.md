# 正式運作複驗（2026-11-04 下午）

本次以現有工作目錄和正式資料目錄驗證蒐集、保存、讀回與問答。
核心流程實測可用；完整歷史財務與全市場研究仍有缺口，不能宣稱全功能驗收。
沒有下正式交易單，也沒有進行雲端寫入或重新開機。

## 本次修正

- `4df7a40 fix(news): bind cached articles to their exact source archive`
  新聞快取原本按日期從最近 200 份 manifest 選擇來源，可能把還原的舊版本
  掛到同日的新版本；快取內嵌的來源 ID 也未核對內容。
  現在以實際讀到的位元組計算 SHA-256，並核對來源類型與日期。
  找不到相同內容時保留可讀文章，但不冒用已登錄的來源。
  三項回歸案例在修正前失敗、修正後通過。
- `c35695b fix(collection): verify complete news and OHLCV readbacks`
  新聞入庫確認原本只比較 ID，行情只比較最新日期與收盤價。
  現在比較新聞完整欄位及最新日 K 的開、高、低、收、量，並拒絕非有限數值。
  模擬內容變動的十項案例在修正前被錯誤接受，修正後會明確回報失敗。

現有 24 天新聞快取沒有觀察到上述來源錯配；這是已重現的錯誤條件修復，
並非聲稱既有正式資料已遭污染。修正沒有重寫歷史新聞或原始來源檔案。

## 本次實測

- 基準離線測試：1,248 passed；修正後完整離線測試：**1,262 passed**，116.16 秒。
  唯一警告來自 Shioaji 的 Pydantic 棄用用法。
- 外部來源檢查：47 項中 44 通過、2 未執行、1 失敗。
  失敗是 DuckDuckGo HTTP 202 驗證頁；未執行的是券商登入與連續盤中報價。
  通過數含設定存在性檢查，不能解讀為 44 個服務均已完成端到端驗收。
- 17:04–17:05（台北）重新取得 120 則新聞，其中 70 則於當日發布；
  0050、2330 的日 K 來源日期均為 2026-11-04。
  新聞與資料庫讀回驗證通過，三份來源檔案的 SHA-256 與 manifest 相符。
  另直接解析 TWSE 原始回應，逐欄核對最新日 K 的開、高、低、收、量與 SQLite 一致。
- 24 天、2,460 則歷史新聞透過修正後的讀取路徑，皆對回快取內容的 exact manifest。
- 17:06 模型問答完成，trace 為 `trace:ed2fec8e54324123b7c3e65367f0b782`。
  實際 App Server turn 為 completed，模型呼叫耗時 34,344 ms。
  回答明確標示月營收資料期與來源。驗證通過，重新開啟 SQLite 後答案一致，
  保存狀態 verified、2 筆引用、3 筆主張，無保存警告。
- 背景排程、模型服務與 watchdog 持續運作；本次未為修正而中斷研究工作。
  蒐集工作由新的 Python 子程序載入程式，17:09:31 自動蒐集成功，
  17:10 watchdog 為健康。三個 Windows 工作均仍使用 S4U 背景登入。

- 17:21 完整稽核完成：SQLite `integrity_check=ok`、FTS 計數一致、
  canonical／research／evidence 無孤立連結，**40,688 份 raw 無缺檔或雜湊不符**，
  199 個模組可載入。結果為 partial，沒有 fail。
  掃描期間出現未登錄檔案警告；17:21:45 重新核對後，活動中未登錄 raw 為 0，
  六份既有歷史排除檔案的雜湊仍相符，沒有新增排除或刪除檔案。

完整稽核見 `data/audits/readiness-recheck-20261104/preflight.json`。
第一次無進度輸出的唯讀掃描被中止，加入堆疊取樣後重新執行相同完整檢查；
最終執行約 13 分 52 秒，主要等待原始檔讀取。本次沒有重現先前紀錄的較快耗時。
`preflight-profile.log` 的每 45 秒 `Timeout` 是診斷堆疊取樣，不是程序執行失敗；
最終 exit code 為 0。這是全量稽核耗時，不代表單次蒐集或查詢要等待相同時間。

## 仍有的限制

- 月營收目前只有 2026-08（1,978 筆）及 2026-09（1,981 筆）；
  季報只有 2026Q2（1,985 筆）及部分 2026Q3（95 筆）。完整跨年比較尚不具備條件。
- 本輪每日研究為 483 個標的、443 份摘要、20 份研究條件齊備，40 個標的保留缺口。
  這些是當次狀態快照；摘要完成不等於完整研究完成。
- 部分外國發行人來源尚無 adapter，系統保留排除與缺口，未以推測內容補值。
- DuckDuckGo 搜尋受外部驗證頁阻擋；不能以其他來源成功代替該供應商驗收。
- 稽核保留研究語意與既有模型紀錄的證據缺口警告，未臆造引用將其改為通過。
  短期記憶容量為 1,945,598／2,000,000 字（97.3%），未超限但已達警戒。
- 這次沒有驗收 UI 畫面、整機重開機、實際交易或雲端同步。

## 證據與保存範圍

機器可讀證據：`data/audits/readiness-recheck-20261104/`，包括
`sources-live.json`、`collection-readback.json`、`news-history-readback.json`、
`news-fix-impact.json`、`ask-live.json`、`ask-readback.json`、`coverage.json`、`background.json`、
`raw-registration-recheck.json`、`acceptance-summary.json`。
測試、修正前失敗重現與稽核執行紀錄：`work/readiness-recheck-20261104/`。

使用 `uv run --offline --no-sync` 執行 Python／pytest，沒有修改依賴或鎖檔。
分支為 `codex/evidence-and-raw-recovery`；只提交本次變更。
`daily_collection.py` 既有變更以局部暫存方式保留，其餘原有未提交內容未納入提交，未推送遠端。
