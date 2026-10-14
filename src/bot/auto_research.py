"""auto_research -- 一鍵跑完整自動化研究流程的 CLI 進入點。

用法:
    uv run stock-auto-research               # 用 .env 預設參數
    uv run stock-auto-research --llm-only    # 僅 LLM 個股研究
    uv run stock-auto-research --llm-only 2330,2317 --upcoming --refresh
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

from bot.app_bootstrap import get_or_create_bus
from bot.auto_llm import run_llm_research_batch
from bot.cli_encoding import configure_console
from bot.config import Settings
from bot.data_pipeline import (
    PipelineConfig,
    PresentationInput,
    run_full_pipeline,
)
from bot.utils import get_logger, log_root


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
    configure_console()
    parser = argparse.ArgumentParser(
        prog="stock-auto-research",
        description="自動化股票研究管線 (ETF + 籌碼 + 法說 + LLM 每日簡報)",
    )
    parser.add_argument("--no-etf", action="store_true", help="跳過 ETF 持股抓取")
    parser.add_argument("--no-chips", action="store_true", help="跳過籌碼面抓取")
    parser.add_argument("--no-llm", action="store_true", help="跳過 LLM 法說分析")
    parser.add_argument("--no-brief", action="store_true", help="不產出每日簡報")
    parser.add_argument(
        "--no-calendar", action="store_true",
        help="不自動更新法說會行事曆 (預設會自動更新)",
    )
    parser.add_argument(
        "--no-auto-research", action="store_true",
        help="不對焦點個股自動跑 LLM 研究 (預設啟用)",
    )
    parser.add_argument(
        "--auto-research-limit", type=int, default=None,
        help="完整管線模式最多研究幾檔缺少/過期快取的個股；未指定或 <=0 表示不設上限",
    )
    parser.add_argument(
        "--upcoming-days", type=int, default=14,
        help="把未來 N 天有法說會的個股自動納入焦點 (預設 14)",
    )
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
    parser.add_argument(
        "--llm-only", action="store_true",
        help="僅跑 LLM 個股研究（跳過 ETF/籌碼/簡報管線）",
    )
    parser.add_argument(
        "llm_tickers", nargs="*",
        help="--llm-only 時的個股代號（可逗號分隔；無參數則 watchlist）",
    )
    parser.add_argument(
        "--upcoming", action="store_true",
        help="--llm-only：納入未來有法說會的個股",
    )
    parser.add_argument(
        "--refresh", action="store_true",
        help="--llm-only：略過快取強制重跑",
    )
    parser.add_argument(
        "--root", type=Path, default=None,
        help="專案資料／設定根目錄（未指定時使用目前工作目錄）",
    )
    args = parser.parse_args(argv)

    project_root = args.root.resolve() if args.root else Path.cwd()
    logger = get_logger("auto-research", log_dir=project_root / "log")

    if args.llm_only:
        tickers = [
            t.strip()
            for raw in args.llm_tickers
            for t in str(raw).split(",")
            if t.strip()
        ]
        with log_root(project_root):
            return run_llm_research_batch(
                tickers=tickers,
                root=project_root,
                upcoming=args.upcoming,
                upcoming_days=args.upcoming_days,
                no_calendar=args.no_calendar,
                refresh=args.refresh,
                chip_days=args.days,
                logger=logger,
            )

    settings = Settings(_env_file=str(project_root / ".env"))  # type: ignore[call-arg]

    presentations: List[PresentationInput] = []
    for spec in args.pdf:
        if "=" not in spec:
            logger.warning("--pdf 參數需為 <ticker>=<path> 格式，已略過: %s", spec)
            continue
        ticker, path_str = spec.split("=", 1)
        p = Path(path_str)
        if not p.is_absolute():
            p = project_root / p
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
        llm_settings=settings,
        focus_tickers=[
            t.strip()
            for raw in args.focus
            for t in str(raw).split(",")
            if t.strip()
        ],
        chip_lookback_days=args.days,
        fetch_etf_holdings=not args.no_etf,
        fetch_chips=not args.no_chips,
        update_calendar=not args.no_calendar,
        auto_focus_upcoming_days=args.upcoming_days,
        auto_research_focus=(not args.no_auto_research) and settings.llm_enabled,
        auto_research_limit=args.auto_research_limit,
        run_llm_analysis=(not args.no_llm) and settings.llm_enabled,
        generate_brief=(not args.no_brief) and settings.llm_enabled,
        generate_us_brief=(not args.no_brief) and settings.llm_enabled,
        min_consensus_for_focus=args.min_consensus,
        presentation_inputs=presentations,
    )

    logger.info("=== 自動化研究啟動 ===")
    with log_root(project_root):
        run = run_full_pipeline(
            config,
            logger=logger,
            publisher=get_or_create_bus(project_root=project_root),
        )
    logger.info(
        "=== 完成: run_id=%s, 狀態 %s, 焦點 %d, 錯誤 %d, 警告 %d, 輸出 %s ===",
        run.run_id,
        "failed" if run.errors else "partial" if run.warnings else "ok",
        len(run.focus_tickers), len(run.errors), len(run.warnings), run.output_dir,
    )
    return 0 if not run.errors else 2


if __name__ == "__main__":
    sys.exit(main())
