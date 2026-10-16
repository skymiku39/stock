# 本機啟動入口

專用資料夾：`C:\start_server\台股研究與微笑曲線`。

- `啟動_台股儀表板_PORT8501.cmd`：執行 `scripts/start_stock.ps1 -Action dashboard`，開啟既有台股研究儀表板，網址 `http://127.0.0.1:8501`。僅監聽本機；port 被占用時拒絕啟動，不關閉其他服務。Ctrl+C 停止。
- `重跑_微笑曲線回測_無需PORT.cmd`：執行 `scripts/start_stock.ps1 -Action replay`，讀取固定的上輪輸入快照，重跑理想/滑價/基準/壓力情境，存至 `data/reports/smile_friction_執行時間/comparison.json`。此命令無網頁服務、不需要 port。
- `使用說明.txt`：使用方式與模型限制。

CMD 只呼叫專案內共用 PowerShell 啟動程式，避免維護兩份執行邏輯。使用 `uv run --no-sync`，不自動安裝或變更依賴。PowerShell 的 Bypass 僅作用於該次啟動，不更改全機政策。

驗證：兩種 Action 的 `-CheckOnly` 通過；replay 入口完成實際重跑；dashboard 使用 `-Headless` 啟動後 `/_stcore/health` 回傳 `ok`。測試程序已停止，8501 已釋放。未執行瀏覽器互動或逐頁驗收。
