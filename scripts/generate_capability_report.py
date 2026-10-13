"""產生系統能力盤點 HTML 報告（可離線開啟）。

用法:
    uv run python scripts/generate_capability_report.py
    uv run python scripts/generate_capability_report.py --out docs/reports/system_capability_report.html
    uv run python scripts/generate_capability_report.py --audit-json path/to/stock-data-audit.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]


# ----------------------------------------------------------------------
# 掃描程式碼
# ----------------------------------------------------------------------


def _load_toml_scripts(pyproject: Path) -> List[Tuple[str, str]]:
    text = pyproject.read_text(encoding="utf-8")
    # 不依賴 tomllib 以外的套件；手解析 [project.scripts] 區塊即可
    m = re.search(r"\[project\.scripts\]\s*\n(.*?)(?:\n\[|\Z)", text, re.S)
    if not m:
        return []
    block = m.group(1)
    out: List[Tuple[str, str]] = []
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, rest = line.partition("=")
        name = name.strip()
        target = rest.strip().strip('"').strip("'")
        out.append((name, target))
    return out


CLI_META: Dict[str, Tuple[str, str]] = {
    # name -> (category, purpose)
    "stock-bot": ("trade", "依 RUN_MODE 啟動 trade／watch／report 策略生命週期"),
    "stock-dashboard": ("dashboard", "啟動 Streamlit 儀表板"),
    "stock-auto-research": ("research", "一鍵研究管線：ETF／籌碼／法說／LLM／每日簡報"),
    "stock-macro-update": ("data", "抓取美股／台指期／ADR 等跨市場 macro 快照"),
    "stock-fundamentals-refresh": ("data", "處理基本面慢速補抓佇列"),
    "stock-intraday": ("research", "今日當沖戰情室管線（題材＋評分＋簡報）"),
    "stock-nextday": ("research", "明日當沖關注名單（draft／update）"),
    "stock-preflight": ("validate", "交易可行性七區檢查（含封存／憑證／時段）"),
    "stock-system-preflight": ("validate", "系統完整性、資料稽核、記憶容量與模組 import 檢查"),
    "stock-test": ("validate", "執行 unit／regression 測試，傳遞 pytest 參數與退出碼"),
    "stock-validate": ("validate", "公開資料源只讀健康度（link／http／parse）"),
    "stock-oddlot-test": ("trade", "零股下單煙測（僅模擬環境）"),
    "stock-scheduler": ("other", "背景排程：macro／研究／公司／雲端／歷史／監控"),
    "stock-company-update": ("data", "補齊上市櫃公司基本資料至 SQLite"),
    "stock-gemini-test": ("validate", "驗證 GEMINI_API_KEY 與最小呼叫"),
    "stock-calendar-update": ("data", "更新 MOPS 法說會與全球科技事件行事曆"),
    "stock-cloud-sync": ("data", "Google Sheets ↔ 本地 SQLite 雙向同步"),
    "stock-intraday-fetch": ("data", "以 Shioaji 抓 1 分／tick 寫入 SQLite"),
    "stock-intraday-backtest": ("research", "當沖／日 K 量化回測（LLM 名單）"),
    "stock-history-fetch": ("data", "慢速補日 K（顧及 TWSE 限流）"),
    "stock-smile-backtest": ("research", "微笑曲線日 K 回測"),
    "stock-smile-screen": ("research", "微笑曲線綜合選股（ETF 共識＋回撤＋負擔力）"),
    "stock-smile-audit": ("research", "微笑曲線多情境稽核回測"),
    "stock-watch-snapshot": ("data", "只看不買：熱度／熱門個股期貨快照"),
    "stock-watch-data-fetch": ("data", "只看不買：公司／日K／基本面／籌碼／技術／macro 補齊"),
    "stock-db-backup": ("data", "stock.db 線上備份（保留策略／標籤）"),
    "stock-data-audit": ("validate", "只讀稽核 SQLite、raw archive、FTS 與 provenance 缺口"),
    "stock-provenance-backfill": ("data", "只對可證明 exact source 的歷史資料補 provenance"),
    "stock-evidence-link-repair": ("data", "只依 exact provenance 修復 evidence 的歷史來源連結"),
    "stock-research-quarantine": ("validate", "以明確 source_document_id 隔離錯誤研究鏈；不刪 raw、不猜 provenance"),
    "stock-calibrate": ("research", "評分訊號與前瞻報酬校準（只讀，不改權重）"),
    "stock-memory": ("research", "短期／長期記憶查詢、回填、軟刪、提案"),
    "stock-ask": ("research", "QueryPlan＋EvidencePack 本地問答（可棄權）"),
    "stock-sync-conflicts": ("validate", "查詢與人工裁決 row-level cloud sync conflict"),
}


def _parse_nav_pages(nav_path: Path) -> Tuple[List[str], Dict[str, List[str]]]:
    text = nav_path.read_text(encoding="utf-8")
    pages_block = re.search(r"PAGES\s*=\s*\{(.*?)\n\}", text, re.S)
    pages: List[str] = []
    if pages_block:
        for m in re.finditer(r'"([^"]+)"\s*:', pages_block.group(1)):
            pages.append(m.group(1))
    # late insert
    if 'PAGES["台股行事曆"]' in text or "PAGES['台股行事曆']" in text:
        if "台股行事曆" not in pages:
            pages.append("台股行事曆")

    groups: Dict[str, List[str]] = {}
    groups_block = re.search(r"NAV_GROUPS\s*=\s*\{(.*?)\n\}", text, re.S)
    if groups_block:
        for gm in re.finditer(
            r'"([^"]+)"\s*:\s*\[(.*?)\]',
            groups_block.group(1),
            re.S,
        ):
            gname = gm.group(1)
            items = re.findall(r'"([^"]+)"', gm.group(2))
            groups[gname] = items
    # 台股行事曆插入監控組（與 nav 邏輯一致）
    for gname, items in groups.items():
        if "LLM 法說分析" in items and "台股行事曆" not in items:
            idx = items.index("LLM 法說分析") + 1
            items.insert(idx, "台股行事曆")
            break
    return pages, groups


def _read_prompt_meta(prompts_dir: Path) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for path in sorted(prompts_dir.glob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        pid = _yaml_scalar(text, "id") or path.stem
        version = _yaml_scalar(text, "version") or ""
        desc = _yaml_scalar(text, "description") or ""
        rows.append({
            "id": pid,
            "version": version.strip('"'),
            "description": desc,
            "file": path.name,
        })
    return rows


def _yaml_scalar(text: str, key: str) -> Optional[str]:
    m = re.search(rf"^{re.escape(key)}:\s*(.+)$", text, re.M)
    if not m:
        return None
    return m.group(1).strip().strip('"').strip("'")


def _list_scripts_helpers(scripts_dir: Path) -> List[Tuple[str, str]]:
    rows: List[Tuple[str, str]] = []
    purpose = {
        "start-watch-only.ps1": "只看不買一鍵：watch-data-fetch → snapshot → scheduler → dashboard",
        "scan_price_10.py": "從本地 DB 掃描約 10 元價位流動股",
        "scan_price_10_mis.py": "TWSE MIS 掃描 8–13 元流動非 ETF",
        "scan_price_10_debug.py": "MIS 報價解析除錯",
        "show_price_band_heat.py": "列印約 10 元價位熱度排行",
        "probe_taifex_mis.py": "探測 TAIFEX MIS 熱門個股期貨端點",
        "probe_taifex_sockjs.py": "探測 TAIFEX SockJS rtCore",
    }
    for p in sorted(scripts_dir.iterdir()):
        if not p.is_file():
            continue
        if p.name.startswith("probe_taifex_mis") and p.name != "probe_taifex_mis.py":
            rows.append((p.name, "TAIFEX MIS 端點／bundle 探測（開發用）"))
        else:
            rows.append((p.name, purpose.get(p.name, "輔助腳本")))
    return rows


# ----------------------------------------------------------------------
# HTML helpers
# ----------------------------------------------------------------------


def esc(s: Any) -> str:
    return html.escape(str(s), quote=True)


def table(headers: List[str], rows: List[List[Any]]) -> str:
    th = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = []
    for row in rows:
        tds = "".join(f"<td>{cell}</td>" for cell in row)
        body.append(f"<tr>{tds}</tr>")
    return f"<table><thead><tr>{th}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def mermaid(code: str) -> str:
    return f'<pre class="mermaid">{esc(code)}</pre>'


# ----------------------------------------------------------------------
# 靜態章節（分析／流程／缺口）
# ----------------------------------------------------------------------


PLANNED_PROMPTS = {
    # 空集合：未接線設計稿改放 prompts/archive/
}

ACTIVE_EXTRA = {
    "query_plan",
    "ask_answer",
    "classify_news",
}  # 程式已接線，README 可能滯後


def section_analysis() -> str:
    return f"""
<section id="analysis">
  <h2>3. 分析能力</h2>

  <h3>3.1 決策主軸</h3>
  {mermaid('''flowchart TB
  subgraph ingest [Ingestion]
    FF[fundamentals]
    CF[chips]
    CD[distribution]
    TC[technicals]
    EC[etf_consensus]
    AL[auto_llm]
  end
  subgraph hub [Hub]
    TV[ticker_view.build_snapshot]
  end
  subgraph decide [Decide]
    SC[scoring.compute_scorecard]
    LG[llm_gate]
    ASK[ask + evidence]
  end
  FF --> TV
  CF --> TV
  CD --> TV
  TC --> TV
  EC --> TV
  AL --> TV
  TV --> SC
  SC --> LG
  TV --> ASK
''')}

  <h3>3.2 量化評分（scoring）</h3>
  <p>四時間框架：<code>day_trade</code>／<code>short_term</code>／<code>mid_term</code>／<code>long_term</code>。
  九因子：llm_sentiment、logic、etf_consensus、chips、technical、fundamental、distribution、us_market、risk。
  缺資料因子會從加權剔除（不以中性 50 稀釋）。</p>
  {table(
      ["分數門檻", "動作"],
      [
          ["≥ 78", "STRONG_BUY"],
          ["≥ 62", "BUY"],
          ["≥ 45", "HOLD"],
          ["≥ 30", "REDUCE"],
          ["&lt; 30", "SELL"],
      ],
  )}
  <p>覆蓋率 &lt; 50% 強制 HOLD；&lt; 80% 時 STRONG_BUY 降為 BUY。進場／停損／停利為建議價，非 bot 環境參數。</p>

  <h3>3.3 技術／基本面／籌碼</h3>
  <ul>
    <li><strong>technicals</strong>：日 K、MA／MACD／RSI／KD／布林、technical_score、16 種單根 K 型態</li>
    <li><strong>fundamentals</strong>：月營收 YoY／MoM、PER／PBR／殖利率、股利、季報 EPS／三率</li>
    <li><strong>chips</strong>：三大法人／融資券／借券／鉅額 N 日彙總</li>
    <li><strong>chip_distribution</strong>：TDCC 大戶 vs 散戶結構分數</li>
    <li><strong>etf_consensus</strong>：主動 ETF 共識持有、新建倉、加碼訊號</li>
  </ul>

  <h3>3.4 LLM 與 Prompt</h3>
  <p>主路徑 <code>research_ticker</code> 已含 presentation_block（法說原文）與 quant_block（基本面／籌碼／ETF）。
  言行反查走 <code>logic_check</code>。Ask 另用 <code>query_plan</code>／<code>ask_answer</code>。</p>

  <h3>3.5 敘事管線</h3>
  <ul>
    <li><strong>intraday</strong>：theme_radar → 候選池 → day_trade 評分 → intraday_brief</li>
    <li><strong>next_day</strong>：催化＋強度 → next_day_radar → 合成分數 → next_day_brief</li>
    <li><strong>data_pipeline</strong>：ETF → 共識 → 籌碼 → 法說 LLM → daily_brief／us_market_brief</li>
    <li><strong>portfolio_analysis</strong>：持股證據包 → LLM 操作建議（不下單）</li>
    <li><strong>smile_curve*</strong>：階梯加碼策略、選股、回測／稽核</li>
  </ul>

  <h3>3.6 Ask／記憶</h3>
  <p>流程：QueryPlan → 本地 EvidencePack →（明確啟用時）bounded live adapters → ask_answer →
  evidence_id 驗證 → 通過才回答；失敗則「我不確定」。僅寫入短期記憶；長期需品質通過或管理者核准。
  live 預設關閉，只有 QueryPlan 要求新聞／網搜且使用者明確啟用時才抓外網。</p>
</section>
"""


def section_flows() -> str:
    return f"""
<section id="flows">
  <h2>4. 流程與判斷</h2>

  <h3>4.1 Scheduler</h3>
  {mermaid('''flowchart TB
  T[stock-scheduler tick] --> J{{Due jobs}}
  J --> M[macro-update]
  J --> F[fundamentals-refresh]
  J --> R[auto-research]
  J --> C[company-update]
  J --> CS[cloud-sync]
  J --> H[history-fetch]
  J --> I[intraday]
  J --> N[nextday]
  J --> W[watch-snapshot]
  T --> SM{{Supervise monitor}}
  SM -->|盤中| BOT[stock-bot]
  SM -->|盤後| STOP[stop bot]
''')}

  <h3>4.2 自動研究管線</h3>
  {mermaid('''flowchart LR
  A[stock-auto-research] --> B[calendar]
  B --> C[ETF holdings]
  C --> D[consensus signals]
  D --> E[chips]
  E --> F[LLM + logic_check]
  F --> G[auto_llm per focus]
  G --> H[macro + us_brief]
  H --> I[daily_brief]
  I --> O[pipeline_runs]
''')}

  <h3>4.3 Bot 進出場判斷</h3>
  {mermaid('''flowchart TB
  MAIN[main.py] --> ARCH{{day_trading archived?}}
  ARCH -->|trade 且未解凍| ERR[拒絕啟動]
  ARCH -->|ok| ST{{STRATEGY_TYPE}}
  ST -->|configurable| CS[ConfigurableStrategy]
  ST -->|etf_follow| EF[EtfFollowStrategy]
  CS --> ER[entry_rules 漲幅區間]
  ER --> LG[LlmGate.allow_entry]
  LG --> RG[RiskGuard]
  EF --> SIG[ETF follow 訊號]
  SIG --> LG2[LlmGate.allow_entry 可選]
  LG2 --> RG[RiskGuard]
  RG --> ORD[下單或虛擬 would-buy]
  CS --> EX[出場: 停損/移動停利/目標/收盤]
  EX --> LGX[LlmGate.allow_exit 可選]
''')}
  <p><strong>重要差異</strong>：<code>ConfigurableStrategy</code> 進場必經 <code>LlmGate</code>；
  <code>EtfFollowStrategy</code> 預設略過 LLM 進場閘（共識訊號為主），設
  <code>ETF_FOLLOW_LLM_GATE=true</code> 可對齊。</p>

  {table(
      ["層級", "判斷點", "結果"],
      [
          ["產品安全", "day_trading_archived 且 RUN_MODE=trade", "拒絕實單（需 DAY_TRADING_UNFREEZE）"],
          ["模式", "trade / watch / report", "實單 vs 虛擬訊號"],
          ["進場價格", "in_entry_range / 回落買回", "是否觸發進場嘗試"],
          ["LLM 進場", "sentiment、day_trade 分數門檻", "允許或封鎖（configurable；EtfFollow 可選）"],
          ["風控", "資金／張數／黑名單／日損／冷卻／漲幅上限", "調整數量或封鎖"],
          ["出場", "停損、移動停利、目標賣、收盤清倉", "觸發賣出"],
          ["LLM 出場閘", "llm_sell_gate_enabled", "可能擋賣（停損／收盤可 bypass）"],
          ["評分", "四框架 scorecard", "儀表板與當沖排序；非直接下單"],
          ["Ask", "evidence_ids 驗證失敗", "棄權「我不確定」"],
      ],
  )}

  <h3>4.4 Ask 流程</h3>
  {mermaid('''flowchart TB
  Q[使用者問題] --> P[plan_query]
  P --> E[收集本地證據]
  E --> PACK[EvidencePack + freshness]
  PACK --> A{{可用證據且有 Gemini?}}
  A -->|是| ANS[ask_answer]
  ANS --> V[answer_verifier]
  V -->|通過| OK[回答 + 短期記憶]
  V -->|失敗| NO[我不確定]
  A -->|否| NO
''')}
</section>
"""


def load_audit_json(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("audit JSON 必須是物件")
    return data


def derive_audit_gap_metrics(audit: Dict[str, Any]) -> Dict[str, Any]:
    """自 stock-data-audit JSON 衍生缺口計數；不硬編碼歷史數字。"""
    db = audit.get("database") if isinstance(audit.get("database"), dict) else {}

    source_runs: Optional[int] = None
    for gap in audit.get("gaps") or []:
        if not isinstance(gap, dict):
            continue
        if gap.get("code") != "source_runs_without_raw_manifest":
            continue
        details = gap.get("details") if isinstance(gap.get("details"), dict) else {}
        unresolved = details.get("unresolved_source_runs")
        if isinstance(unresolved, list):
            source_runs = len(unresolved)
        break
    if source_runs is None and "source_runs_without_documents" in db:
        source_runs = int(db.get("source_runs_without_documents") or 0)

    legacy_llm: Optional[int] = None
    for item in audit.get("known_limits") or []:
        if not isinstance(item, dict):
            continue
        if item.get("code") != "legacy_llm_without_provable_evidence":
            continue
        if "llm_analysis_legacy_unresolved" in db:
            legacy_llm = int(db.get("llm_analysis_legacy_unresolved") or 0)
        else:
            legacy_llm = None
        break
    if legacy_llm is None and "llm_analysis_legacy_unresolved" in db:
        legacy_llm = int(db.get("llm_analysis_legacy_unresolved") or 0)

    return {
        "supplied": True,
        "source_runs_without_raw_manifest": source_runs,
        "legacy_llm_without_provable_evidence": legacy_llm,
    }


def _fmt_audit_count(value: Optional[int]) -> str:
    if value is None:
        return "（此 audit JSON 未提供該計數）"
    return f"{value} 筆"


def section_gaps(audit_metrics: Optional[Dict[str, Any]] = None) -> str:
    if audit_metrics and audit_metrics.get("supplied"):
        source_txt = _fmt_audit_count(
            audit_metrics.get("source_runs_without_raw_manifest")
        )
        legacy_txt = _fmt_audit_count(
            audit_metrics.get("legacy_llm_without_provable_evidence")
        )
        audit_note = (
            f"本報告已讀取 <code>--audit-json</code>："
            f"<code>source_runs_without_raw_manifest</code> = {esc(source_txt)}；"
            f"<code>legacy_llm_without_provable_evidence</code> = {esc(legacy_txt)}。"
            " 計數來自該次 audit 產物，不是文件內硬編碼歷史快照。"
        )
        legacy_priority = (
            f"以 exact manifest 逐步處理目前 audit 顯示的 "
            f"{esc(source_txt)} legacy source run 與 {esc(legacy_txt)} legacy LLM 分析"
            "（僅在有可證明原始檔時）"
        )
    else:
        audit_note = (
            "未提供 <code>--audit-json</code>：目前 "
            "<code>source_runs_without_raw_manifest</code>／"
            "<code>legacy_llm_without_provable_evidence</code> "
            "計數不可用。請執行 <code>uv run stock-data-audit --json</code> "
            "後帶入產物再生報告；勿沿用歷史文件中的 4／43 等過期數字。"
        )
        legacy_priority = (
            "以最新 <code>stock-data-audit --json</code> 確認 "
            "source_runs_without_raw_manifest 與 legacy_llm_without_provable_evidence "
            "後，再以 exact manifest 逐步處理（目前報告未附 audit，計數不可用）"
        )

    return f"""
<section id="gaps">
  <h2>5. 缺少／缺口</h2>
  <p class="note">{audit_note}</p>
  {table(
      ["嚴重度", "缺口", "影響"],
      [
          ["Done (P0)", "Ask snapshot adapters（technical／chips／etf／macro）",
           "已由 bot.ask_adapters 只讀接通；仍需本地先有快取"],
          ["Done (P0)", "ETF 評分遮蔽 fundamental／etf_consensus",
           "compute_scorecard(instrument_type=etf) 已分流"],
          ["Done (P1)", "Ask news／web_search／MOPS／ticker live adapters",
           "預設關閉；--live 或 STOCK_ASK_LIVE=1 時限時抓取並保存 provenance；ticker 仍受延遲與 freshness gate 約束"],
          ["Done (P1)", "classify_news 接線；其餘 planned 歸檔",
           "auto_llm → classified_materials；supply_chain／analyst 移 archive"],
          ["Done (P1)", "EtfFollow 可選 LLM 進場閘",
           "ETF_FOLLOW_LLM_GATE=true 可對齊 ConfigurableStrategy"],
          ["High", "DATA_ARCHITECTURE_REDESIGN 已部分落地",
           "大規模歷史回填、來源專用日期／衝突驗證仍分階段補齊"],
          ["High", "當沖 trade 模式預設封存",
           "易誤以為 bot 會實單，實際僅訊號"],
          ["Done (P1)", "canonical SQLite semantic audit",
           "只讀檢查 OHLC、期間、財務數值與空表狀態；不把保存誤當正確"],
          ["Medium", "來源專用 PDF／事件衝突驗證仍需擴充",
           "共用品質與 raw provenance 已有，域專用規則仍需實際格式案例"],
          ["Low", "professional_knowledge 僅接受 allowlist 權威網域",
           "已接入專用 adapter；頁面原文優先，僅摘要時標 partial，一般搜尋結果不列為專業證據"],
          ["Out of scope", "RiskPostTradeHandler／下單後流程",
           "本次研究、蒐集、問答與同步驗收不改動交易／下單路徑"],
          ["Low", "EVENT_CHAIN_SMILE_SCREEN 預設關閉",
           "量化補齊後不會自動選股"],
          ["Low", "新掛牌主動 ETF 可能 no_data_yet",
           "共識持股暫時低估"],
      ],
  )}

  <h3>6. 建議優先順序（後續）</h3>
  <ol>
    <li>補齊 MOPS／ETF／新聞／PDF 的來源專用日期、單位與衝突驗證</li>
    <li>{legacy_priority}</li>
    <li>累積真實歷史樣本後再做 scoring calibration；不以少量結果宣稱投資正確率</li>
  </ol>
</section>
"""


# ----------------------------------------------------------------------
# 組裝報告
# ----------------------------------------------------------------------


def build_html(
    *,
    generated_at: str,
    scripts: List[Tuple[str, str]],
    pages: List[str],
    groups: Dict[str, List[str]],
    prompts: List[Dict[str, str]],
    helpers: List[Tuple[str, str]],
    audit_metrics: Optional[Dict[str, Any]] = None,
) -> str:
    cli_rows = []
    for name, target in scripts:
        cat, purpose = CLI_META.get(name, ("other", target))
        cli_rows.append([
            f"<code>{esc(name)}</code>",
            f"<code>{esc(target)}</code>",
            esc(purpose),
            f"<span class='tag tag-{esc(cat)}'>{esc(cat)}</span>",
        ])

    page_rows = []
    page_to_group = {}
    for g, items in groups.items():
        for it in items:
            page_to_group[it] = g
    for p in pages:
        page_rows.append([esc(p), esc(page_to_group.get(p, "（未分組）"))])

    prompt_rows = []
    for pr in prompts:
        status = "planned" if pr["id"] in PLANNED_PROMPTS else "active"
        note = ""
        if pr["id"] in ACTIVE_EXTRA:
            note = "程式已接線（README 可能滯後）"
        prompt_rows.append([
            f"<code>{esc(pr['id'])}</code>",
            esc(pr["version"]),
            esc(pr["description"]),
            f"<span class='tag tag-{status}'>{status}</span>",
            esc(note),
        ])

    helper_rows = [[f"<code>{esc(n)}</code>", esc(p)] for n, p in helpers]

    n_cli = len(scripts)
    n_pages = len(pages)
    n_prompts = len(prompts)
    n_planned = sum(1 for p in prompts if p["id"] in PLANNED_PROMPTS)

    css = """
:root {
  --bg: #0f1419;
  --panel: #1a2332;
  --text: #e7ecf3;
  --muted: #9aa8bc;
  --accent: #3d9cf0;
  --border: #2a3a4f;
  --ok: #3ecf8e;
  --warn: #f0b429;
  --bad: #f07178;
}
* { box-sizing: border-box; }
body {
  margin: 0; font-family: "Segoe UI", "Noto Sans TC", "Microsoft JhengHei", sans-serif;
  background: var(--bg); color: var(--text); line-height: 1.55;
}
header {
  padding: 2rem 1.5rem 1rem; border-bottom: 1px solid var(--border);
  background: linear-gradient(180deg, #152033 0%, var(--bg) 100%);
}
header h1 { margin: 0 0 .4rem; font-size: 1.75rem; }
header p { margin: .2rem 0; color: var(--muted); }
nav.toc {
  position: sticky; top: 0; z-index: 5;
  background: rgba(15,20,25,.92); backdrop-filter: blur(6px);
  border-bottom: 1px solid var(--border);
  padding: .6rem 1.5rem; display: flex; flex-wrap: wrap; gap: .6rem 1rem;
}
nav.toc a { color: var(--accent); text-decoration: none; font-size: .92rem; }
main { max-width: 1100px; margin: 0 auto; padding: 1.5rem; }
section { margin: 2rem 0; padding: 1.25rem; background: var(--panel); border: 1px solid var(--border); border-radius: 10px; }
h2 { margin-top: 0; border-bottom: 1px solid var(--border); padding-bottom: .4rem; }
h3 { color: #c5d4e8; }
table { width: 100%; border-collapse: collapse; font-size: .9rem; margin: .8rem 0; }
th, td { border: 1px solid var(--border); padding: .45rem .55rem; text-align: left; vertical-align: top; }
th { background: #243247; }
code { background: #0c1017; padding: .1rem .35rem; border-radius: 4px; font-size: .86em; }
.tag { display: inline-block; padding: .1rem .45rem; border-radius: 999px; font-size: .75rem; }
.tag-trade { background: #5a2d2d; }
.tag-research { background: #2d4a5a; }
.tag-data { background: #2d5a3d; }
.tag-validate { background: #5a4a2d; }
.tag-dashboard { background: #3d2d5a; }
.tag-other { background: #444; }
.tag-active { background: #1f5a3a; }
.tag-planned { background: #5a4a1f; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: .75rem; }
.card { background: #121a26; border: 1px solid var(--border); border-radius: 8px; padding: .9rem; }
.card .n { font-size: 1.6rem; font-weight: 700; color: var(--accent); }
.card .l { color: var(--muted); font-size: .85rem; }
pre.mermaid { background: #0c1017; padding: 1rem; border-radius: 8px; overflow-x: auto; }
.note { color: var(--muted); font-size: .9rem; }
footer { text-align: center; color: var(--muted); padding: 2rem; font-size: .85rem; }
ul { padding-left: 1.2rem; }
"""

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Stock Bot 系統能力盤點報告</title>
<style>{css}</style>
</head>
<body>
<header>
  <h1>Stock Bot 系統能力盤點報告</h1>
  <p>產生時間：{esc(generated_at)}（Asia/Taipei）</p>
  <p class="note">本報告由 <code>scripts/generate_capability_report.py</code> 自程式碼掃描＋靜態盤點產生；不含任何 API 金鑰。可離線開啟（流程圖為可讀 preformatted 文字，不載入外部 CDN）。</p>
</header>
<nav class="toc">
  <a href="#summary">1. 摘要</a>
  <a href="#tools">2. 可用工具</a>
  <a href="#analysis">3. 分析能力</a>
  <a href="#flows">4. 流程與判斷</a>
  <a href="#gaps">5. 缺口</a>
</nav>
<main>
<section id="summary">
  <h2>1. 摘要</h2>
  <div class="cards">
    <div class="card"><div class="n">{n_cli}</div><div class="l">stock-* CLI</div></div>
    <div class="card"><div class="n">{n_pages}</div><div class="l">Dashboard 頁面</div></div>
    <div class="card"><div class="n">{n_prompts}</div><div class="l">Prompt YAML</div></div>
    <div class="card"><div class="n">{n_planned}</div><div class="l">planned 未接線</div></div>
  </div>
  <ul>
    <li>分析主軸：<strong>TickerSnapshot → scorecard</strong>、LLM 研究／簡報、<strong>Ask 證據問答</strong>、微笑曲線選股／回測。</li>
    <li>執行模式：trade／watch／report；當沖實單預設<strong>封存</strong>（需明確解凍）。</li>
    <li>資料健康可用 <code>uv run stock-validate</code>；研究入口 <code>stock-auto-research</code>／Dashboard。</li>
  </ul>
</section>

<section id="tools">
  <h2>2. 可用工具</h2>
  <h3>2.1 CLI（pyproject [project.scripts]）</h3>
  {table(["CLI", "模組", "用途", "分類"], cli_rows)}
  <p class="note">共 {n_cli} 個已註冊 console script。</p>

  <h3>2.2 Dashboard 頁面</h3>
  {table(["頁面", "導覽分組"], page_rows)}

  <h3>2.3 scripts/ 輔助</h3>
  {table(["檔名", "用途"], helper_rows)}
</section>

<section id="prompts-live">
  <h2>2.4 Prompt 清單（掃描 prompts/*.yaml）</h2>
  {table(["id", "version", "description", "狀態", "備註"], prompt_rows)}
</section>

{section_analysis()}
{section_flows()}
{section_gaps(audit_metrics)}

</main>
<footer>
  重新產生：<code>uv run python scripts/generate_capability_report.py</code>
  （可選 <code>--audit-json</code> 帶入 <code>stock-data-audit --json</code> 產物）
</footer>
</body>
</html>
"""


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="產生系統能力盤點 HTML 報告")
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "docs" / "reports" / "system_capability_report.html",
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--audit-json",
        type=Path,
        default=None,
        help="可選：stock-data-audit --json 產物；用於衍生 provenance 缺口計數",
    )
    args = parser.parse_args(argv)

    root: Path = args.root
    scripts = _load_toml_scripts(root / "pyproject.toml")
    missing_meta = sorted(name for name, _target in scripts if name not in CLI_META)
    if missing_meta:
        print(
            "error: CLI metadata missing for " + ", ".join(missing_meta),
            file=sys.stderr,
        )
        return 2

    pages, groups = _parse_nav_pages(root / "src" / "bot" / "dashboard" / "nav.py")
    prompts = _read_prompt_meta(root / "prompts")
    helpers = _list_scripts_helpers(root / "scripts")

    audit_metrics: Optional[Dict[str, Any]] = None
    if args.audit_json is not None:
        try:
            audit_metrics = derive_audit_gap_metrics(load_audit_json(args.audit_json))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"error: failed to read --audit-json: {exc}", file=sys.stderr)
            return 2

    # 台北時間近似：本機若已是 TW 則用本地；報告標註字串
    now = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    html_doc = build_html(
        generated_at=now,
        scripts=scripts,
        pages=pages,
        groups=groups,
        prompts=prompts,
        helpers=helpers,
        audit_metrics=audit_metrics,
    )

    out: Path = args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_doc, encoding="utf-8")
    audit_note = "audit=none"
    if audit_metrics and audit_metrics.get("supplied"):
        audit_note = (
            "audit="
            f"source_runs={audit_metrics.get('source_runs_without_raw_manifest')};"
            f"legacy_llm={audit_metrics.get('legacy_llm_without_provable_evidence')}"
        )
    print(
        f"ok wrote {out} cli={len(scripts)} pages={len(pages)} "
        f"prompts={len(prompts)} {audit_note}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
