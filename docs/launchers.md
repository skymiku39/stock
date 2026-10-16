# 本機啟動入口

專用資料夾：`C:\start_server\台股研究與微笑曲線`。

- `啟動_台股儀表板_PORT8501.cmd`：執行 `scripts/start_stock.ps1 -Action dashboard`，開啟既有台股研究儀表板，網址 `http://127.0.0.1:8501`。僅監聽本機；port 被占用時拒絕啟動，不關閉其他服務。Ctrl+C 停止。
- `重跑_微笑曲線回測_無需PORT.cmd`：執行 `scripts/start_stock.ps1 -Action replay`，讀取固定的上輪輸入快照，重跑理想/滑價/基準/壓力情境，存至 `data/reports/smile_friction_執行時間/comparison.json`。此命令無網頁服務、不需要 port。
- `使用說明.txt`：使用方式與模型限制。
- `啟動_微笑曲線回測_PORT8502.cmd`：執行 `scripts/start_stock.ps1 -Action smile`，獨立回測頁面 `http://127.0.0.1:8502`。可調共用本金、滑價百分比、前日成交量參與率、股票及日期。原台股研究頁面仍使用 8501。

CMD 只呼叫專案內共用 PowerShell 啟動程式，避免維護兩份執行邏輯。使用 `uv run --no-sync`，不自動安裝或變更依賴。PowerShell 的 Bypass 僅作用於該次啟動，不更改全機政策。

驗證：兩種 Action 的 `-CheckOnly` 通過；replay 入口完成實際重跑；dashboard 使用 `-Headless` 啟動後 `/_stcore/health` 回傳 `ok`。測試程序已停止，8501 已釋放。未執行瀏覽器互動或逐頁驗收。

## 獨立微笑曲線頁面

只使用既有、經雜湊及來源驗證的 `data/reports/smile_execution_20261016` 快照，不在調參時更新正式資料庫。所選股票的共同區間會顯示在側欄，日期超出任何一檔的實際覆蓋即拒絕回測。

按「執行回測」才更新結果；結果上方保留當次執行參數及日期，避免把未提交的側欄修改誤認為已生效。理想成交、目前設定與壓力情境使用相同本金與標的，顯示已實現/未實現/總損益、報酬、最大日終回撤、每日權益、委託取消原因及平倉明細。壓力情境可以獨立調參或停用比較。

每次結果原子保存於 `data/reports/smile_dashboard/時間_唯一碼.json`，不覆寫舊結果。重新啟動後可在「已保存的回測」載入最近 100 份紀錄，也能下載完整 JSON。

單一快照讀取實作 `bot.smile_snapshot` 同時供 CLI 與網頁使用；獨立頁面不啟動下單、LLM 或排程服務。無需新增依賴。

啟動使用 `uv run --no-sync python -m streamlit`，避開本機 `streamlit` console shim 的 canonicalize 錯誤。8502 啟動及健康檢查通過；Streamlit AppTest 已驗證提交、改參數、保存、重新載入和快照缺失阻擋。使用真實快照從頁面重跑 50 萬及 300 萬本金，結果與上一輪 CLI 一致。完整單元測試 1,116 項通過；最後頁面測試 5 項通過。尚未做實際瀏覽器視覺驗收。
