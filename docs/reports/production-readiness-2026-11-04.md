# 正式運作檢視與修復（2026-11-04）

目前已實測通過新聞／行情蒐集、原始資料保存與雜湊核對、SQLite 讀回、
本地證據檢索，以及模型問答的引用驗證與保存。尚不能宣告全市場研究、
完整歷史比較與無人登入的開機運作全部驗收完成。

## 本次修復

- `ff7787d fix(model): cancel timed out turns and retain final answers`
  - 每批模型工作原本固定在 300 秒逾時；實際全量執行在 57 份完成後，
    其餘 378 檔因逾時被封鎖。新增 `CODEX_APP_SERVER_TIMEOUT_SECONDS`，
    預設 900 秒，可設定 1–3600 秒，模型與推理強度沿用原設定。
  - 逾時時，以該次工作的 thread／turn ID 請求中斷，記錄是否收到確認。
    中斷失敗仍回報失敗，不能把部分文字當作成功。
  - 使用完成的最終回答，避免串流中的說明文字污染 JSON；隔離其他 turn 的事件。
    協定依據：[官方 App Server 文件](https://learn.chatgpt.com/docs/app-server)。
- `3fb754c fix(scheduler): propagate blocked research retry deadlines`
  - 將模型受阻原因與重試期限傳回每日狀態，供排程器尊重服務冷卻時間。
  - 每日摘要會顯示受阻原因；恢復成功後清除封鎖與舊的重試期限。
- `ed596c4 perf(audit): bound the cache for full integrity checks`
  - 稽核連線使用上限 256 MiB、按需配置的 SQLite page cache。
    快取僅在該次唯讀連線生效，關閉後不保留；完整 integrity_check 規則不變。
  - 同一工作目錄的完整稽核由原設定約 677 秒，降為調整後約 57 秒。
    這是本機觀察值，包含作業系統快取與同時收集資料的影響。
    快取語意依據：[SQLite 官方 PRAGMA 說明](https://www.sqlite.org/pragma.html#pragma_cache_size)。
  - 測試確認寫入仍被拒絕、資料庫檔案雜湊不變，新連線不繼承快取設定。

## 實測證據

- 最終離線回歸：**1216 passed**，88.07 秒。
  詳見 `work/production-readiness-20261104/tests-final.log`。
  唯一警告為第三方 Shioaji
  的 Pydantic 棄用訊息。先前沙箱內 6 項子程序終止測試因權限失敗；
  在本機權限下已全數重測通過。
- 正式來源檢查：49 項中 **47 通過、1 警告、1 失敗**。
  券商登入、2330 contract 與 snapshot 通過，為現有模擬設定下的行情驗收。
  警告是本次未啟用盤中 MIS 連續輪詢；失敗是 DuckDuckGo 的 HTTP 202 驗證頁。
- 2026-11-04 02:32–02:33（台北）重新抓取 120 則新聞、0050 與 2330 日 K。
  行情原始觀測日為 11 月 3 日；擷取時間與觀測時間分開保存。
  從 SQLite 重新讀回與抓取結果一致，3 份對應原始檔的 SHA-256 與 manifest 一致。
- 實際模型問答：`trace:e038bde235ad4942a956430470f5d61a`。
  關閉原連線、重新開啟 SQLite 後，答案一致、狀態 verified、prompt 版本 1.2、
  2 個證據引用與 2 筆主張，無保存警告。此次沒有重播模型輸出或補寫驗收 metadata。
- 排程器於 02:33 安全重啟；更新後 PID 40592、載入 commit `3fb754c`，
  啟動時記錄的程式雜湊與目前程式一致。02:34 已自動續跑未完成研究，
  該輪仍在執行中，尚未驗收全量完成。
  續跑首批新增 8 份通過驗證的報告，該次模型呼叫 289703 ms、turn 狀態 completed。
- 完整稽核已通過 SQLite integrity、40245 份 raw 檔案雜湊、provenance 關聯、
  FTS 計數與 196 個模組載入。無缺檔或孤立來源連結。
  整體仍為 partial：保留 3 類舊資料品質警告；並行收集期間另觀測到
  `unregistered_raw_files` 警告；稍後重新核對時，未登錄的 6 份皆為既有
  雜湊完全相符的歷史排除項，活動中的未登錄原始檔為 0。詳細結果見
  `raw-registration-recheck.json`。短期記憶容量為 99.8%，仍在 200 萬字上限內，
  但已超過 80% 警戒；不能將此狀態宣告為全綠。

機器可讀證據位於 `data/audits/production-readiness-20261104/`；
測試與操作記錄位於 `work/production-readiness-20261104/`。
使用 `uv run --offline --no-sync` 執行，未更換依賴或鎖檔。

## 仍有限制

- 季報只有 2026Q2（1985 筆）與部分 Q3（95 筆）；月營收只有 2026-08
  （1978 筆）與 2026-09（1981 筆）。這些是資料庫涵蓋範圍，不是完整歷史。
  跨年度／完整季度比較不能假定已齊備，缺值不可補零或由模型猜造。
- 部分外國交易所／發行人的官方來源 adapter 尚未配置，會明確排除。
  報告產生成功與研究條件齊備是兩個不同狀態。
- DuckDuckGo 被供應商驗證頁阻擋，不能宣告網頁搜尋全部可用；
  官方來源與新聞來源的成功不能代替該供應商驗收。
- 目前 Windows 模型與排程任務為 Interactive，需使用者登入；
  watchdog 尚未安裝。開機前登入、重啟恢復與重開機驗收仍待完成。
  安裝預覽／原設定備份在 `data/deployment/production-readiness-20261104/`。
  真正安裝需要 Windows 本機 UAC 與帳戶密碼，密碼不應送進聊天。
- 本次沒有執行實際交易、雲端寫入、UI 操作、登出或重開機。

## 保留與驗證邊界

既有未提交變更與資料保留。本次在已有修改的模型檔案只暫存必要修復，
其餘既有修改仍未提交；沒有推送遠端。
既有修復前資料庫備份為
`data/audits/production-20261104/stock_20261104_005238_before-repair.db`。
完整 SQLite／raw／provenance 稽核結果見同次驗收目錄的 `preflight-final.json`。
排程器重啟後僅另修復獨立稽核 CLI 的快取；模型、設定、每日研究執行檔的
啟動時雜湊仍可個別核對。稽核修復另以新 CLI 程序驗收，未打斷正在執行的研究。
