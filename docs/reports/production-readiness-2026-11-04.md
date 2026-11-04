# 正式運作檢視與修復（2026-11-04）

目前已實測通過新聞／行情蒐集、原始資料保存與雜湊核對、SQLite 讀回、
本地證據檢索，以及模型問答的引用驗證與保存。另已完成免密碼背景登入、
開機排程與 watchdog 自動恢復驗收。全市場研究、完整歷史比較及整機重開機
啟動仍未全面驗收。

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

## 免密碼開機與自動恢復（09:46–09:54 補驗收）

- `483f1fb feat(ops): support password-free unattended task logon`
  增加 `-LogonMode S4U`；Password 模式維持相容。S4U 不開啟密碼輸入視窗，
  不儲存 Windows 密碼，亦未重設使用者密碼。UAC 管理員同意仍是 Windows 安裝步驟。
- 09:43 的一次性 S4U 工作在 **Session 0** 成功讀取 SQLite、抓取證交所官方日 K，
  啟動全新的模型服務並以既有登入完成實際推論。模型為 gpt-6-sol，
  該次推論 2750 ms；測試服務已停止，臨時測試排程已移除。
- 模型、排程器、watchdog 三個工作已設為 ASUS 帳戶的 S4U／Limited 背景登入；
  開機觸發延遲 30 秒。Watchdog 每 2 分鐘檢查，維護暫停已解除，最近執行結果為 0。
- 受控恢復測試：於無研究子程序時停止排程器 PID 40592；watchdog 自動啟動
  新排程器 PID 51720，確認在 Session 0 以 ASUS 執行，心跳持續且狀態健康。
  首個互動式觀察程式無權讀取背景程序命令列而誤報失敗；後續以背景 watchdog、
  Windows 工作排程事件及提升權限的唯讀檢查，核對實際已成功恢復。
- 本輪完整離線回歸 **1248 passed**，89.39 秒。驗證的是當時工作樹，其他既有
  修改仍保留；記錄為 `work/production-readiness-20261104/tests-s4u-final.log`。
- 最終部署收據：`data/deployment/background-s4u-20261104/final-acceptance.json`，
  狀態 accepted；含原設定備份、已安裝／驗證 XML 與恢復程序身分。
  背景連線測試另見 `data/deployment/s4u-probe-20261104/probe.json`。

Microsoft 對 [S4U](https://learn.microsoft.com/en-us/windows/win32/api/taskschd/ne-taskschd-task_logon_type)
列有網路及加密檔限制。本次僅實測本機 SQLite、官方公開 HTTPS 與現有 Codex 登入；
未驗收 NAS／SMB、EFS 或其他依賴 Windows 網路憑證的來源。

## 仍有限制

- 季報只有 2026Q2（1985 筆）與部分 Q3（95 筆）；月營收只有 2026-08
  （1978 筆）與 2026-09（1981 筆）。這些是資料庫涵蓋範圍，不是完整歷史。
  跨年度／完整季度比較不能假定已齊備，缺值不可補零或由模型猜造。
- 部分外國交易所／發行人的官方來源 adapter 尚未配置，會明確排除。
  報告產生成功與研究條件齊備是兩個不同狀態。
- DuckDuckGo 被供應商驗證頁阻擋，不能宣告網頁搜尋全部可用；
  官方來源與新聞來源的成功不能代替該供應商驗收。
- 開機免登入設定與排程器自動恢復已驗收，仍未執行整機登出／重開機測試。
  先前 Password 憑證安裝已取消，由 S4U 部署取代；不需要知道 ASUS 帳戶密碼。
- 本次沒有執行實際交易、雲端寫入或 Dashboard 畫面驗收。

## 保留與驗證邊界

既有未提交變更與資料保留。本次在已有修改的模型檔案只暫存必要修復，
其餘既有修改仍未提交；沒有推送遠端。
既有修復前資料庫備份為
`data/audits/production-20261104/stock_20261104_005238_before-repair.db`。
完整 SQLite／raw／provenance 稽核結果見同次驗收目錄的 `preflight-final.json`。
核心驗收後另完成背景服務與免密碼部署修復；各次執行的程式身分及設定
保留於當次部署收據。受控恢復只在排程器空閒時進行。
