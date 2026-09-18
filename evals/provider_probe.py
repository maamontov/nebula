"""
Provider Probe for Nebula.
Validates any OpenAI-compatible provider and model without GPT dependencies.
Adheres strictly to docs/implementation-plan.md Section 4.4 & Section 13.
"""
import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.adapters.llm import OpenAICompatibleAdapter, extract_clean_json_content
from contracts.provider import (
    ModelProfile,
    ProviderProfile,
    StructuredOutputMode,
)

TEST_SCHEMA = {
    "type": "object",
    "properties": {
        "candidate_summary": {"type": "string"},
        "technical_score": {"type": "number", "minimum": 1.0, "maximum": 5.0},
        "evidence_quotes": {
            "type": "array",
            "items": {"type": "string"}
        },
        "requires_review": {"type": "boolean"}
    },
    "required": ["candidate_summary", "technical_score", "evidence_quotes", "requires_review"],
    "additionalProperties": False
}


async def run_probe(
    base_url: str,
    model_id: str,
    api_key_env: str,
    mode: StructuredOutputMode,
    thinking_disable_payload: dict | None = None,
) -> int:
    print("==================================================")
    print("Nebula Provider Probe")
    print(f"Upstream Base URL: {base_url}")
    print(f"Upstream Model ID: {model_id}")
    print(f"Structured Output Mode: {mode.value}")
    print(f"API Key Env: {api_key_env} ({'SET' if os.getenv(api_key_env) else 'EMPTY/NOT SET'})")
    print("==================================================\n")

    provider = ProviderProfile(
        id="probe-provider",
        name="Probe Provider",
        base_url=base_url,
        api_key_env=api_key_env,
        timeout_seconds=45.0,
    )

    model = ModelProfile(
        id="probe-model",
        provider_id=provider.id,
        upstream_model_id=model_id,
        structured_output_mode=mode,
        max_output_tokens=1024,
        thinking_disable_payload=thinking_disable_payload,
    )

    adapter = OpenAICompatibleAdapter(provider=provider, model=model)

    # 1. Test Unicode & Russian capability
    print("[1/3] Testing Basic Connectivity & Russian Unicode...")
    t0 = time.perf_counter()
    try:
        res1 = await adapter.execute_request(
            messages=[
                {"role": "system", "content": "Отвечай кратко на русском языке."},
                {"role": "user", "content": "Назови два ключевых преимущества шардирования баз данных."}
            ],
            max_retries=2,
        )
        elapsed1 = time.perf_counter() - t0
        content1 = res1.get("content", "")
        print(f"  ✓ Success ({elapsed1:.2f}s)")
        print(f"  Response sample: {content1[:120].strip()}...\n")
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ FAILED: {e}\n")
        return 1

    # 2. Test Structured JSON Output
    print(f"[2/3] Testing Structured Output ({mode.value})...")
    sample_transcript = (
        "Кандидат: 'Мы использовали PostgreSQL с партиционированием по дате "
        "и Redis для кеширования сессий. Это дало нам задержку в 15 миллисекунд.'"
    )
    t0 = time.perf_counter()
    try:
        res2 = await adapter.execute_request(
            messages=[
                {
                    "role": "system",
                    "content": "Ты ассистент оценки собеседований. Оцени технический фрагмент и заполни схему."
                },
                {
                    "role": "user",
                    "content": f"Фрагмент транскрипта:\n{sample_transcript}\n\nСформируй структурированную оценку."
                }
            ],
            json_schema=TEST_SCHEMA,
            schema_name="candidate_assessment_probe",
            max_retries=2,
        )
        elapsed2 = time.perf_counter() - t0
        parsed_data = res2.get("data")
        print(f"  ✓ Success ({elapsed2:.2f}s)")
        print(f"  Parsed JSON: {json.dumps(parsed_data, ensure_ascii=False, indent=2)}")
        usage = res2.get("usage", {})
        reasoning_tokens = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        print(f"  Usage: {usage}")
        print(f"  reasoning_tokens: {reasoning_tokens}\n")

        # A gateway silently drops unknown parameters instead of returning 4xx, so a wrong
        # switch looks like success while costing latency (or even adding reasoning tokens).
        # Never record a thinking_disable_payload capability without this check passing.
        if thinking_disable_payload is not None:
            print("[2b] Verifying reasoning is actually disabled...")
            print(f"  Sent fragment: {json.dumps(thinking_disable_payload, ensure_ascii=False)}")
            if reasoning_tokens:
                print(
                    f"  ✗ FAILED: model still emitted {reasoning_tokens} reasoning tokens. "
                    "The upstream gateway ignored the fragment. Do NOT record this capability "
                    "for this provider/model pair.\n"
                )
                return 3
            print("  ✓ Zero/absent reasoning tokens. Capability verified for this provider/model.\n")
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ FAILED: {e}\n")
        return 2

    # 3. Test Reasoning Cleaning
    print("[3/3] Checking Reasoning Tag Sanitation...")
    raw_with_think = "<think>Внутренние рассуждения модели, которые нельзя показывать кандидату</think>{\"valid\": true}"
    cleaned = extract_clean_json_content(raw_with_think)
    assert "<think>" not in cleaned and "</think>" not in cleaned, "Sanitation failed"
    print("  ✓ Reasoning stripped cleanly.\n")

    print("==================================================")
    print("PROBE PASSED: Provider and Model are compatible with Nebula core contracts.")
    print("==================================================")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Nebula Provider Probe CLI")
    parser.add_argument("--base-url", default=os.getenv("NEBULA_LLM_BASE_URL", "http://localhost:11434/v1"))
    parser.add_argument("--model", default=os.getenv("NEBULA_LLM_MODEL", "qwen2.5:7b"))
    parser.add_argument("--api-key-env", default="LLM_API_KEY")
    parser.add_argument(
        "--mode",
        choices=["json_schema", "json_object", "prompt_instruction"],
        default="json_object",
    )
    parser.add_argument(
        "--thinking-disable-json",
        default=None,
        help=(
            "Exact JSON object merged into the request to disable reasoning, e.g. "
            "'{\"enable_thinking\": false}' for Qwen or '{\"reasoning_effort\": \"none\"}' "
            "for DeepSeek. When set, the probe fails unless the model emits zero reasoning tokens."
        ),
    )
    args = parser.parse_args()

    thinking_payload: dict | None = None
    if args.thinking_disable_json:
        try:
            thinking_payload = json.loads(args.thinking_disable_json)
        except json.JSONDecodeError as exc:
            print(f"Invalid --thinking-disable-json: {exc}")
            sys.exit(2)
        if not isinstance(thinking_payload, dict):
            print("--thinking-disable-json must be a JSON object")
            sys.exit(2)

    mode_enum = StructuredOutputMode(args.mode)
    code = asyncio.run(
        run_probe(
            base_url=args.base_url,
            model_id=args.model,
            api_key_env=args.api_key_env,
            mode=mode_enum,
            thinking_disable_payload=thinking_payload,
        )
    )
    sys.exit(code)


if __name__ == "__main__":
    main()
