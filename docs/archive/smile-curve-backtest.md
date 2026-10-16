# 微笑曲線回測

## 指令

```bash
# 基本回測（建議股票代號加引號，避免 PowerShell 把 0050 變成 50）
uv run stock-smile-backtest --symbols "2330,0050" --start 2023-01-01 --end 2026-06-01 --fetch

# 固定標準價（高於現價時才會觸發逢低加碼）
uv run stock-smile-backtest --symbols "2330" --start 2023-01-01 --reference 2330:1000

# 自訂階梯與資金
uv run stock-smile-backtest --symbols "2330" --tiers "1:1,3:2,5:3,10:5" --max-fund 2000000 --json

# 匯出明細 CSV
uv run stock-smile-backtest --symbols "2330" --reference 2330:1000 --output data/reports/smile_2330.csv
```

## 現行回測：收盤訊號、次日開盤成交（2026-10-16 修正）

1. **標準價**：每週期起始價，或 `SMILE_REFERENCE_PRICES` 固定值
2. **買進訊號**：收盤低於標準價時，依跌幅階梯產生委託意圖（`SMILE_BUY_TIERS=1:1,3:2,5:3,8:4`），下一筆日 K 的開盤成交。首次日 K 只初始化標準價。
3. **底部 DCA**：收盤跌幅仍達最深階時，每日另產生一筆 `SMILE_BASE_LOT` 買單。跨多階可同時產生多筆委託；成交後才消耗階梯，部分成交視為該階已觸發。
4. **賣出訊號**：收盤回到標準價以上，對預估淨利 > 0 的批次產生賣單。次日開盤重新計算費稅後損益；若不再獲利則取消，保留部位。次日不重新要求回到標準價。此為條件開盤成交近似，不保證真實開盤撮合。
5. **重置**：全部批次賣清後，以成交價重設標準價；手動指定固定價則繼續沿用固定值。
6. **多檔共用資金**：`run_many` 使用一個帳戶，同時限制現金及持倉成本總額。開盤買單只使用當日賣出前可用額度；代號排序、再按階梯排序分配。已實現獲利回到現金，持倉成本仍不得超出原設定上限。零股也受單檔張數換算的股數上限限制。
7. **缺資料**：缺少某檔交易日不以其他檔價格代替，委託保留到下一筆可見日 K；若該筆開盤無效，取消舊單。無效收盤不發訊號；既有部位以最近有效收盤估值並標記 stale。期末訊號沒有下一筆開盤就不成交。

## 損益與回撤

- `realized_pnl_twd`：已平倉淨損益；舊 `total_pnl_twd` 名稱保留同義，不能當作全帳戶損益。
- `unrealized_pnl_twd`：期末以收盤估算出清、扣除買賣費稅後的未平倉損益。
- `net_pnl_twd`：已實現 + 未實現；`return_pct` 使用整個帳戶初始本金為分母。
- `equity_curve`：初始本金 + 累計已實現 + 當日日終未實現；記錄現金、成本曝險與過時估值。
- `max_drawdown_pct` / `max_drawdown_twd`：從初始本金起算的日終權益高點回落最大值，百分比及金額各取其最大值。未模擬盤中回撤。
- 勝率仍僅為已平倉交易的勝率；只賣獲利單會自然造成 100%，不能用來證明策略有效。

`stock-smile-audit` 預設同樣使用次日開盤與多檔共用資金；每檔顯示的 portfolio 指標是同一帳戶，不能重複加總。`--include-legacy-comparison` 才會額外列出有偏誤的舊 OHLC 情境。

## 重跑比較

```powershell
uv run python scripts/compare_smile_execution.py --output data/reports/smile_execution_20261016
```

腳本唯讀 SQLite，驗證原始檔 SHA-256、來源代號/月份與每列 OHLC 完全相符，保留 input snapshot、來源索引、委託記錄及日終權益。任何來源缺失或不符均停止，不悄悄刪除日 K 再跨日成交。

本輪結果見 [驗證報告](../reports/smile-backtest-validation.md)。舊的固定標準價「100% 勝率」範例不再當作有效績效證據。

限制：尚未模擬滑價、成交量、漲跌停無法成交、股利及完整公司行動；原始價格回測不是含息總報酬，更不是實盤績效。

## 相關程式

- `src/bot/smile_curve.py` — 核心狀態機
- `src/bot/smile_curve_backtest.py` — 日 K 回測
- `src/bot/smile_curve_backtest_cli.py` — CLI
