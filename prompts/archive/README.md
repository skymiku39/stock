# 封存 Prompt

此目錄的 YAML **不會**被 `PromptRegistry` 載入（僅掃描 `prompts/*.yaml`）。

| id | 封存原因 |
|----|----------|
| `brokerage_commission_guide` | 內容已內嵌至 `intraday_brief`、`daily_brief`、`portfolio_analysis` 等 active prompt；權威交易成本說明見 [docs/glossary.md](../../docs/glossary.md) |
| `supply_chain_impact` | 設計稿；尚無美股→台股供應鏈管線接線，暫不維護為 planned |
| `analyst_real_sentiment` | 設計稿；外資報告語意 vs 買賣超流程未建置，暫不維護為 planned |
