# 當沖模組封存說明

**封存日期**：2026-06-11  
**狀態**：維護模式（僅修重大 bug；新功能轉向微笑曲線／指定標的波段策略）

## 封存範圍

以下功能標記為**已封存**，不再積極開發：

| 類別 | 模組 / 設定 | 說明 |
|------|-------------|------|
| 實單交易 | `RUN_MODE=trade` | 啟動 `stock-bot` 時預設阻擋；需 `DAY_TRADING_UNFREEZE=true` 才能解除 |
| 當沖策略 | `ConfigurableStrategy`、`EtfFollowStrategy` | 漲幅進場、移動停利、13:15 全出 |
| 風控（當沖） | `RiskGuard` 12 道閘門、`EXIT_TIME` | 仍保留程式碼，解除封存後可用 |
| 盤中 LLM 閘門 | `LLM_GATE_*`、`intraday_llm_advisor` | 當沖進出場輔助 |
| 文件 | `docs/trading-rules.md`、`docs/operations.md` 當沖章節 | 歷史參考 |

## 仍可使用（未封存）

| 功能 | 指令 / 模式 |
|------|-------------|
| 看盤訊號 | `RUN_MODE=watch` |
| 延遲報表 | `RUN_MODE=report` |
| 儀表板 | `stock-dashboard` |
| 法說 / ETF / 籌碼 / K 線研究 | `stock-auto-research`、dashboard 各頁 |
| 資料庫 / 雲端同步 | `stock_db`、`stock-cloud-sync` |
| 可行性檢查 | `stock-preflight`（會顯示封存狀態） |

## 如何暫時恢復當沖實單

僅供自行承擔風險之維護或回歸測試：

```env
DAY_TRADING_ARCHIVED=true
DAY_TRADING_UNFREEZE=true
RUN_MODE=trade
SIMULATION=true
```

建議先在模擬環境驗證，再考慮實單。

### 封存閘門實際的判斷（`src/bot/archive_status.py`）

`RUN_MODE=trade` 只有在下列條件**同時**成立時才會被阻擋：

1. `SIMULATION=false`（模擬委託到不了正式帳戶，所以模擬下不擋，上面那組設定其實不需要解除封存）。
2. `DAY_TRADING_ARCHIVED=true`（預設）。
3. `DAY_TRADING_UNFREEZE` 不是 `true`。

也就是說，**單獨把 `DAY_TRADING_ARCHIVED` 設成 `false` 就會解除這道閘門**，不需要 `DAY_TRADING_UNFREEZE`。
請把這兩個設定都當成會放行真實委託的開關；平常維持 `DAY_TRADING_ARCHIVED=true`、不設 `DAY_TRADING_UNFREEZE`。
是否要改成「一定要 `DAY_TRADING_UNFREEZE=true` 才放行」屬於 #38 的範圍，尚未決定。

這道閘門只管 `stock-bot` 啟動；微笑策略另外有自己寫死的檢查（見 `docs/broker-fill-acceptance-plan.md`）。

## 新方向（規劃中）

- 指定標的、可隔夜持股
- 微笑曲線／分批逢低加碼
- 回彈至標準價賣出；每筆賣出淨利須 > 0
- 週期結束後重置買賣邏輯

微笑曲線回測已可用：`uv run stock-smile-backtest`（見 [smile-curve-backtest.md](smile-curve-backtest.md)）。  
Live 實單 `STRATEGY_TYPE=smile_curve` 規劃中，與封存模組分離。

## 相關程式進入點

```
src/bot/main.py              # trade 封存閘門
src/bot/strategy.py          # BaseStrategy（當沖核心）
src/bot/strategy_configurable.py
src/bot/strategy_etf_follow.py
src/bot/risk_guard.py
```
