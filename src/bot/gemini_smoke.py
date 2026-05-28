"""Small CLI for verifying the Gemini API setup."""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from bot.config import Settings
from bot.llm_analyzer import DEFAULT_MODEL, GeminiClient
from bot.utils import get_logger


DEFAULT_PROMPT = (
    "Reply in Traditional Chinese with one concise sentence: "
    "Gemini API connection is working."
)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stock-gemini-test",
        description="Verify GEMINI_API_KEY, model selection, and a basic Gemini API call.",
    )
    parser.add_argument(
        "--model",
        default="",
        help=f"Override GEMINI_MODEL for this check (default: .env or {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--prompt",
        default=DEFAULT_PROMPT,
        help="Prompt used for the smoke test.",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=128,
        help="Maximum output tokens for the smoke test.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help="Generation temperature for the smoke test.",
    )
    args = parser.parse_args(argv)

    settings = Settings()
    api_key = settings.gemini_api_key.strip()
    model = args.model.strip() or settings.gemini_model.strip() or DEFAULT_MODEL

    if not api_key:
        print("GEMINI_API_KEY is not set. Add it to .env or your shell environment.")
        print("Example: GEMINI_API_KEY=your_google_ai_studio_key")
        return 2

    logger = get_logger("gemini-smoke")
    client = GeminiClient(api_key=api_key, model=model, logger=logger)
    if not client.enabled:
        print("Gemini client is disabled. Check google-genai installation and API key.")
        return 1

    text, meta = client.generate_raw(
        args.prompt,
        max_output_tokens=args.max_output_tokens,
        temperature=args.temperature,
    )
    if not text:
        print("Gemini API request failed.")
        if meta.get("error"):
            print(f"Error: {meta['error']}")
        return 1

    latency = meta.get("latency_ms", 0)
    tokens_in = meta.get("tokens_in", "?")
    tokens_out = meta.get("tokens_out", "?")
    print(f"Gemini API OK (model={model}, latency_ms={latency})")
    print(f"tokens_in={tokens_in}, tokens_out={tokens_out}")
    print(text.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
