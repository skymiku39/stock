# PROGRESS

最後更新：2026-11-09（分支 `codex/evidence-and-raw-recovery`）

## 專案目標
Stock Lab（`stock-bot`）：本機台股研究工作台。把公開行情、財務、籌碼、ETF 持股、新聞與法說資料存入 SQLite，保留原始來源與引用，提供查詢、AI 研究問答、圖表、回測與模擬交易。目前 `RUN_MODE=watch`、`SIMULATION=true`；當沖實單模組已封存（2026-06-11）。

## 已定決策
- 預設不下真實委託，維持 `SIMULATION=true`。為什麼：實單模組已封存，專案定位為研究與教學。
- 資料驗收須核對數值、日期、來源與保存讀回一致；「頁面載入成功」或「回報缺資料」不算資料正確。為什麼：2026-11-06 驗收報告採用的標準。
- 不改寫原始稽核結果（如 `source_left_running`）為通過，保留原報告並另附後續查詢。為什麼：保持證據可追溯。
- 測試用 `uv run --offline --no-sync pytest`，不安裝套件、不動 `uv.lock`。為什麼：避免依賴漂移。
- 模型額度封鎖後每 5 分鐘最多一次健康查詢，恢復即解除等待。為什麼：提前恢復時不浪費等待（commit `ea17ee4`）。

## 目前計畫（優先順序）
1. [P0] 審視並整理未提交修改，分組提交（見「進行中」）。
2. [P1] 補跨年歷史財務資料（月營收目前僅 2026-08、09；季報僅 2026Q2 與部分 Q3）。
3. [P1] DuckDuckGo 搜尋失效（HTTP 202 驗證頁）：找替代來源或明確回報缺來源。
4. [P2] Windows 工作排程 S4U 登入回 1909（帳戶鎖定）：修復後驗證登出／重開機自動啟動。
5. [P2] 短期記憶容量約 88.6%，處理警戒。
6. [P3] 瀏覽器畫面排版驗收、券商實際成交驗收（需使用者決定是否做）。

## 已完成
- 2026-11-06 本機模組驗收（見 `docs/reports/local-module-check-2026-11-06.md`，證據在 `work/local-acceptance-20261106/`），修復：設定頁重複 key、模型額度恢復、日 K 問答不再強制 web_search。
- 近期提交：模擬交易跨盤中續抱證據化部位、真實 tick 回測對帳、發行商歷史比較匯入。
- 2026-11-09 完整離線測試 `1379 passed`（約 142 秒）。

## 進行中 / 下一步
- 工作區有 12 個已修改檔 + 新檔 `src/bot/brief_cache.py`、`src/bot/model_availability.py`、`tests/test_model_availability.py`，未提交。
- 下一步：
  1. （已檢視 2026-11-09）`scheduler.py`：exit 4=busy 60 秒後重試、exit 3=blocked/partial 依 `next_retry_epoch`；移除「失敗數超過上限就歸零」後，重試次數用完就只照一般間隔執行，直到成功才歸零（不再每輪重啟快速重試）。判斷為合理，已補 `test_busy_exit_waits_a_minute_without_counting_as_failure`（17 passed）；尚無 rc=3 的 `next_retry_epoch` 專屬測試。
  2. 確認 `docs/reports/local-module-check-2026-11-06.md` 的改動屬於本批。
  3. 分組提交（排程重試 / 模型可用性與簡報快取 / 文件）。分支領先 origin 47 個提交，推送前先問使用者。

## 已知問題與注意事項
- `work/` 未被 `.gitignore` 排除，提交時不要 `git add -A`。
- `Sinopac.pfx`、`shioaji.log`、`data/`、`log/` 已被忽略；勿提交憑證。
- 部分檔案有 LF→CRLF 警告，屬行尾差異，非內容問題。
- 測試有一個 Shioaji 上游 Pydantic 棄用警告，可忽略。
- 全套測試約 2.5 分鐘，需背景或調高 timeout 執行。
- 入口：`stock-bot`（`bot.main`）、`stock-dashboard`；文件入口 `README.md`、`docs/architecture.md`、`docs/operations.md`。
