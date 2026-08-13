"""llm_smoke -- 統一 LLM 連線煙霧測試 CLI。

支援 chain / gemini_gateway / cursor / gemini 四種後端，
可逐一測試或依 LLM_PROVIDER 設定測試。
"""

from __future__ import annotations

import argparse
import sys

from bot.utils import get_logger

DEFAULT_PROMPT = (
    "Reply in Traditional Chinese with one concise sentence: "
    "LLM connection is working."
)


def _test_single_backend(client, prompt: str, max_tokens: int, temp: float, label: str) -> bool:
    """測試單一後端，回傳是否成功。"""
    print(f"\n--- 測試 {label} ---")
    if not client.enabled:
        print(f"  SKIP: {label} 未就緒 (enabled=False)")
        return False

    text, meta = client.generate_raw(prompt, max_output_tokens=max_tokens, temperature=temp)
    if text:
        latency = meta.get("latency_ms", "?")
        print(f"  OK (model={client.model}, latency_ms={latency})")
        print(f"  回覆: {text.strip()[:200]}")
        return True
    else:
        error = meta.get("error", "unknown")
        print(f"  FAIL: {error}")
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stock-llm-test",
        description="Verify LLM backend(s) connectivity.",
    )
    parser.add_argument(
        "--provider",
        choices=["chain", "gemini_gateway", "cursor", "gemini", "all"],
        default="",
        help="Override LLM_PROVIDER for this test (default: read from .env).",
    )
    parser.add_argument(
        "--health-only",
        action="store_true",
        help="Only check /health endpoint, do not send a prompt.",
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
        help="Maximum output tokens.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help="Generation temperature.",
    )
    args = parser.parse_args(argv)

    from bot.config import Settings
    settings = Settings()
    logger = get_logger("llm-smoke")

    provider = args.provider or settings.llm_provider
    results: list[tuple[str, bool]] = []

    if provider == "all":
        from bot.cursor_llm import CursorGatewayClient
        from bot.gemini_gateway import GeminiGatewayClient
        from bot.llm_analyzer import GeminiClient

        backends = [
            ("GeminiGateway", GeminiGatewayClient(
                base_url=settings.gemini_gateway_base_url,
                timeout=settings.gemini_gateway_timeout,
                model_label=settings.gemini_gateway_model_label,
            )),
            ("CursorGateway", CursorGatewayClient(
                base_url=settings.cursor_llm_base_url,
                timeout=settings.cursor_llm_timeout,
                model_label=settings.cursor_llm_model_label,
            )),
        ]
        if settings.gemini_api_key:
            backends.append(("GeminiSDK", GeminiClient(
                api_key=settings.gemini_api_key,
                model=settings.gemini_model,
            )))

        if args.health_only:
            for name, client in backends:
                ok = client.enabled
                status = "READY" if ok else "NOT READY"
                print(f"{name}: {status}")
                results.append((name, ok))
        else:
            for name, client in backends:
                ok = _test_single_backend(
                    client, args.prompt, args.max_output_tokens, args.temperature, name
                )
                results.append((name, ok))
    else:
        from bot.llm_analyzer import create_llm_client
        client = create_llm_client(settings)

        if args.health_only:
            ok = client.enabled
            status = "READY" if ok else "NOT READY"
            print(f"LLM ({provider}): {status}")
            results.append((provider, ok))
        else:
            ok = _test_single_backend(
                client, args.prompt, args.max_output_tokens, args.temperature, provider
            )
            results.append((provider, ok))

    print("\n=== 結果 ===")
    any_ok = False
    for name, ok in results:
        icon = "✓" if ok else "✗"
        print(f"  {icon} {name}")
        if ok:
            any_ok = True

    return 0 if any_ok else 1


if __name__ == "__main__":
    sys.exit(main())
