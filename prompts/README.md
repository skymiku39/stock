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
  - name: ticker
    required: true
  - name: text
    required: true
    max_chars: 30000                 # 自動截斷
output_format: json | markdown | text
template: |
  ... 字串模板，使用 Python `str.format` 變數替換 ...
```

## 目前的 Prompt 清單

| id                         | 用途                                       |
|----------------------------|--------------------------------------------|
| `analyze_presentation`     | 解析法說會逐字稿，輸出情緒/Capex/毛利率指引等 |
| `logic_check`              | 言行反查：法說語意 vs 籌碼面                   |
| `extract_etf_holdings`     | 從投信網頁 HTML/PDF 抽出 ETF 持股 JSON         |
| `daily_brief`              | 每日盤後簡報，整合所有分析結果                 |
| `classify_news`            | 重大訊息分類與影響面評分                       |
| `us_market_brief`          | 美股盤後 → 台股早盤影響評估 (Markdown 簡報)    |
| `supply_chain_impact`      | 美股財報/新聞 → 對應台股供應鏈影響 JSON         |
| `analyst_real_sentiment`   | 外資報告語意 vs 真實買賣超的反指標分析          |

## 如何新增

1. 在本目錄新增 `<id>.yaml`，填入欄位
2. 在 dashboard「Prompt 管理」頁按「重新載入」
3. 在程式碼或 dashboard 直接呼叫 `registry.render(<id>, **vars)`
