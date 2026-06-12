# Prompt Library

本目錄收錄所有由系統呼叫 LLM 時使用的 Prompt 模板，每一份檔案對應一個唯一的 `id`。
所有檔案皆為 **版本化的人類可讀 YAML**，可直接編輯也可在儀表板「Prompt 管理」頁修改。

## 為什麼要這樣做

1. **可追溯** — 每筆 LLM 呼叫會同時寫入 prompt id 與 version 到 `log/llm_calls_*.jsonl`，
   日後可重現任何一次分析結果。
2. **可優化** — Prompt 與程式邏輯解耦，調整提示工程不必動程式碼。
3. **可審計** — 全部 prompt 都在 Git 控管，模型輸出也都會被記錄。

## 標準欄位

```yaml
id: <唯一識別碼，等同檔名>
version: "<語意化版本，每次調整必加>"
description: 一句話說明
model_hint: gemini-2.5-flash         # 預設模型，可被覆寫
max_output_tokens: 2048
temperature: 0.2
inputs:                              # 必填參數列表
  - name: ticker                       # ≡ 程式中的 symbol（代號）
    required: true
  - name: text
    required: true
    max_chars: 30000                 # 自動截斷
output_format: json | markdown | text
template: |
  ... 字串模板，使用 Python `str.format` 變數替換 ...
```

> 術語對照（`ticker` / `symbol`、`停損停利三層` 等）見 [docs/glossary.md](../docs/glossary.md)。

## 狀態說明

| status | 意義 |
|--------|------|
| `active` | 程式碼有 `gemini_call()` 或等效呼叫 |
| `planned` | 已設計 YAML，管線尚未接線 |
| `archived` | 已移至 `prompts/archive/`，不再載入 |

## Active Prompt 清單（12 份）

| id | status | 用途 |
|----|--------|------|
| `analyze_presentation` | active | 解析法說會逐字稿，輸出情緒/Capex/毛利率指引等 |
| `logic_check` | active | 言行反查：法說語意 vs 籌碼面（`verdict` 欄位） |
| `extract_etf_holdings` | active | 從投信網頁 HTML/PDF 抽出 ETF 持股 JSON |
| `daily_brief` | active | 每日盤後簡報，整合所有分析結果 |
| `us_market_brief` | active | 美股盤後 → 台股早盤影響評估 (Markdown 簡報) |
| `research_ticker` | active | 全自動個股研究（行事曆+搜尋+新聞 → 結構化 JSON） |
| `portfolio_analysis` | active | 投組持倉分析與操作建議 |
| `theme_radar` | active | 盤中題材雷達 |
| `intraday_brief` | active | 當沖戰情簡報（停損停利為人工參考，非 bot 參數） |
| `intraday_live_review` | active | 盤中即時檢討與推演 |
| `next_day_radar` | active | 明日當沖關注雷達 |
| `next_day_brief` | active | 明日當沖戰情簡報 |

## Planned Prompt 清單（3 份）

| id | status | 用途 | 備註 |
|----|--------|------|------|
| `classify_news` | planned | 重大訊息分類與影響面評分 | `mops_scraper` 已就緒，待接 `gemini_call` |
| `supply_chain_impact` | planned | 美股財報/新聞 → 對應台股供應鏈影響 JSON | `supply_chain.json` 已就緒 |
| `analyst_real_sentiment` | planned | 外資報告語意 vs 實際買賣超（`discrepancy` 欄位） | 見 glossary 獨立流程說明 |

## Archived Prompt

見 [archive/README.md](archive/README.md)。

## 如何新增

1. 在本目錄新增 `<id>.yaml`，填入欄位
2. 在 dashboard「Prompt 管理」頁按「重新載入」
3. 在程式碼或 dashboard 直接呼叫 `registry.render(<id>, **vars)`
