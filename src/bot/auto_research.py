"""auto_research -- 一鍵跑完整自動化研究流程的 CLI 進入點。

用法:
    uv run stock-auto-research               # 用 .env 預設參數
    uv run stock-auto-research --no-etf      # 跳過 ETF 抓取
    uv run stock-auto-research --no-chips    # 跳過籌碼面
    uv run stock-auto-research --no-brief    # 不產出每日簡報
    uv run stock-auto-research --pdf <path>  # 額外送一份法說會 PDF 進去
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from bot.config import Settings
from bot.data_pipeline import (
    PipelineConfig,
    PresentationInput,
    run_full_pipeline,
)
from bot.utils import get_logger


def _read_pdf_text(path: Path) -> Optional[str]:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        return None
    try:
        reader = PdfReader(str(path))
        parts: List[str] = []
        for p in reader.pages[:80]:
            try:
                parts.append(p.extract_text() or "")
            except Exception:
                continue
        return "\n\n".join(parts)
    except Exception:
        return None


def _read_text_or_pdf(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    if path.suffix.lower() == ".pdf":
        return _read_pdf_text(path)
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return path.read_bytes().decode("utf-8", errors="replace")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stock-auto-research",
        description="自動化股票研究管線 (ETF + 籌碼 + 法說 + LLM 每日簡報)",
    )
    parser.add_argument("--no-etf", action="store_true", help="跳過 ETF 持股抓取")
    parser.add_argument("--no-chips", action="store_true", help="跳過籌碼面抓取")
    parser.add_argument("--no-llm", action="store_true", help="跳過 LLM 法說分析")
    parser.add_argument("--no-brief", action="store_true", help="不產出每日簡報")
    parser.add_argument(
        "--pdf", action="append", default=[],
        help="附加一份法說會 PDF/TXT，格式 <ticker>=<path>，可重複",
    )
    parser.add_argument(
        "--focus", action="append", default=[],
        help="額外指定焦點個股 (可重複；單一參數內可用逗號分隔，例如 --focus 2330,2317)",
    )
    parser.add_argument(
        "--days", type=int, default=5,
        help="籌碼面回顧天數 (預設 5)",
    )
    parser.add_argument(
        "--min-consensus", type=int, default=2,
        help="共識焦點門檻 (ETF 持有檔數 >=N，預設 2)",
    )
    args = parser.parse_args(argv)

    logger = get_logger("auto-research")
    project_root = Path.cwd()
    settings = Settings()

    presentations: List[PresentationInput] = []
    for spec in args.pdf:
        if "=" not in spec:
            logger.warning("--pdf 參數需為 <ticker>=<path> 格式，已略過: %s", spec)
            continue
        ticker, path_str = spec.split("=", 1)
        p = Path(path_str)
        text = _read_text_or_pdf(p)
        if text is None or len(text) < 50:
            logger.warning("讀不到 %s 的內容", p)
            continue
        presentations.append(PresentationInput(
            ticker=ticker.strip(), text=text,
            source="pdf_path" if p.suffix.lower() == ".pdf" else "file",
            label=p.name,
        ))

    config = PipelineConfig(
        project_root=project_root,
        gemini_api_key=settings.gemini_api_key,
        gemini_model=settings.gemini_model,
        focus_tickers=[
            t.strip()
            for raw in args.focus
            for t in str(raw).split(",")
            if t.strip()
        ],
        chip_lookback_days=args.days,
        fetch_etf_holdings=not args.no_etf,
        fetch_chips=not args.no_chips,
        run_llm_analysis=(not args.no_llm) and bool(settings.gemini_api_key),
        generate_brief=(not args.no_brief) and bool(settings.gemini_api_key),
        min_consensus_for_focus=args.min_consensus,
        presentation_inputs=presentations,
    )

    logger.info("=== 自動化研究啟動 ===")
    run = run_full_pipeline(config, logger=logger)
    logger.info(
        "=== 完成: run_id=%s, 焦點 %d, 錯誤 %d, 輸出 %s ===",
        run.run_id, len(run.focus_tickers), len(run.errors), run.output_dir,
    )
    return 0 if not run.errors else 2


if __name__ == "__main__":
    sys.exit(main())
