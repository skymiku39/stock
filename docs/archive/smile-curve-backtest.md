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

## 策略邏輯（日 K 近似）

1. **標準價**：每週期起始價，或 `SMILE_REFERENCE_PRICES` 固定值
2. **買進**：收盤低於標準價時，依跌幅階梯加碼（`SMILE_BUY_TIERS=1:1,3:2,5:3,8:4`）
3. **底部 DCA**：觸及最深階後，每個交易日可再加碼 `SMILE_BASE_LOT`
4. **賣出**：收盤回到標準價以上時，**僅賣淨利 > 0 的 lot**（含手續費 + 一般證交稅 0.3%）
5. **重置**：全部 lot 賣清後，以當價重設標準價，開始新週期

## 實驗結果範例（2330，2023-01 ~ 2026-06）

| 設定 | 回合數 | 勝率 | 說明 |
|------|--------|------|------|
| 動態標準價 | 0 | — | 單邊牛市（453→2355），幾乎不回撤至標準價以下，無加碼 |
| 固定標準價 1000 | 9 | 100% | 模擬「高點套牢後微笑曲線」情境 |
| 固定標準價 1200 | 4 | 100% | 標準價愈高，加碼愈深、回合較少 |

> 回測使用日 K 收盤價，未模擬盤中高低點；實盤需分 K 或 Tick 驗證。

## 相關程式

- `src/bot/smile_curve.py` — 核心狀態機
- `src/bot/smile_curve_backtest.py` — 日 K 回測
- `src/bot/smile_curve_backtest_cli.py` — CLI
