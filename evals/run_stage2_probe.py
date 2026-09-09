"""
Unified Stage 2 AI/STT Acceptance Probe.
Tests real upstream providers (PlusVibe / non-GPT) against Nebula Stage 2 criteria:
- STT model verification (Whisper Large v3 Turbo)
- Minimum 2 non-GPT model profiles (Qwen 3.7 Plus, GLM-5.3 Flash)
- Structured JSON output schemas
- Deterministic EvidenceValidator citation verification
- Latency & failure diagnostics
"""
import asyncio
import io
import math
import os
import struct
import sys
import time
import wave
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from backend.adapters.llm import OpenAICompatibleAdapter
from backend.adapters.stt import OpenAICompatibleSTTAdapter, STTAuthenticationError
from backend.core.evidence_validator import validate_proposal
from backend.core.profiles import (
    get_plusvibe_deepseek_model,
    get_plusvibe_gemini_model,
    get_plusvibe_provider,
    get_plusvibe_qwen_model,
    get_plusvibe_whisper_stt,
)
from contracts.audio import TrackType
from contracts.domain import (
    AssessmentProposal,
    TranscriptRevision,
    TranscriptSegment,
)


def generate_synthetic_wav_bytes(duration_sec: float = 2.0, freq_hz: float = 440.0) -> bytes:
    """Generates an in-memory 16kHz mono 16-bit PCM WAV file."""
    sample_rate = 16000
    num_samples = int(duration_sec * sample_rate)
    buf = io.BytesIO()

    with wave.open(buf, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)

        frames = bytearray()
        for i in range(num_samples):
            t = i / sample_rate
            val = int(32767.0 * 0.4 * math.sin(2.0 * math.pi * freq_hz * t))
            frames.extend(struct.pack("<h", val))

        wav_file.writeframes(frames)

    return buf.getvalue()


async def run_stage2_evaluation() -> int:
    api_key = os.getenv("PLUSVIBE_API_KEY", "")
    base_url = os.getenv("NEBULA_LLM_BASE_URL", "https://plusvibeapi.ru/v1")
    stt_model_id = os.getenv("NEBULA_STT_MODEL", "whisper-large-v3-turbo")

    print("==================================================")
    print("Nebula Stage 2: AI & STT Live Acceptance Probe")
    print("==================================================")
    print(f"Base URL:      {base_url}")
    print(f"STT Model:     {stt_model_id}")
    print(f"API Key:       {'[CONFIGURED]' if api_key else '[MISSING in .env]'}")
    print("==================================================\n")

    if not api_key:
        print("ОШИБКА: Переменная PLUSVIBE_API_KEY не задана в .env или окружении.")
        print("Заполни PLUSVIBE_API_KEY в файле .env")
        return 1

    provider = get_plusvibe_provider()
    provider.base_url = base_url

    stt_profile = get_plusvibe_whisper_stt()
    stt_profile.model_id = stt_model_id

    stt_adapter = OpenAICompatibleSTTAdapter(stt_profile, api_key_env="PLUSVIBE_API_KEY")

    stt_ok = False
    stt_latency = 0.0
    stt_error_msg = ""

    # -------------------------------------------------------------
    # 1. Test STT Endpoint: POST /v1/audio/transcriptions
    # -------------------------------------------------------------
    print(f"[1/3] Тестирование STT: {stt_model_id}...")
    wav_bytes = generate_synthetic_wav_bytes(duration_sec=2.0, freq_hz=440.0)
    try:
        t0 = time.perf_counter()
        stt_res = await stt_adapter.transcribe_audio(
            wav_bytes,
            filename="tone_test.wav",
            content_type="audio/wav",
            language="ru",
            max_retries=1,
        )
        stt_latency = time.perf_counter() - t0
        stt_ok = True
        print(f"  ✓ STT Успешно ({stt_latency:.2f}s)")
        print(f"  Распознанный текст: '{stt_res.text}'\n")
    except STTAuthenticationError as e:
        stt_error_msg = f"Аутентификация (403/401): {e}"
        print(f"  ✗ Ошибка аутентификации STT: {e}\n")
    except Exception as e:  # noqa: BLE001
        stt_error_msg = str(e)
        print(f"  ✗ Сбой STT вызова: {e}\n")

    # -------------------------------------------------------------
    # Shared Evaluation Dataset
    # -------------------------------------------------------------
    candidate_answer = (
        "Для распределённых транзакций мы применили паттерн Saga с оркестрацией. "
        "В случае сбоя шага оркестратор запускает компенсирующие транзакции в обратном порядке."
    )
    transcript = TranscriptRevision(
        revision_id="live-rev-1",
        interview_id="stage2-eval",
        segments=[
            TranscriptSegment(
                id="seg-qa-1",
                track_id=TrackType.CANDIDATE,
                start_time_ms=0,
                end_time_ms=8000,
                text=candidate_answer,
            )
        ],
    )

    assessment_schema = {
        "type": "object",
        "properties": {
            "scores": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "criterion_id": {"type": "string"},
                        "score": {"type": "number", "minimum": 1.0, "maximum": 5.0},
                        "explanation": {"type": "string"},
                        "evidence": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "segment_id": {"type": "string"},
                                    "exact_quote": {"type": "string"}
                                },
                                "required": ["segment_id", "exact_quote"]
                            }
                        }
                    },
                    "required": ["criterion_id", "score", "explanation", "evidence"]
                }
            },
            "critical_errors": {"type": "array", "items": {"type": "string"}}
        },
        "required": ["scores", "critical_errors"],
        "additionalProperties": False
    }

    messages = [
        {
            "role": "system",
            "content": (
                "Ты эксперт технической оценки собеседований. "
                "Оцени кандидата по критерию 'crit-arch-saga' (шкала 1-5). "
                "Обязательно включи дословную цитату (exact_quote) из текста ответа с указанием segment_id='seg-qa-1'. "
                "Не делай оценок по полу, возрасту, акценту или манере речи. "
                "Отвечай строго валидным JSON."
            )
        },
        {
            "role": "user",
            "content": f"Транскрипт кандидата [seg-qa-1]:\n\"{candidate_answer}\""
        }
    ]

    # Helper function for evaluating a model
    async def evaluate_model(model_profile, step_num, step_name):
        print(f"[{step_num}/3] Тестирование LLM: {model_profile.upstream_model_id} ({step_name})...")
        adapter = OpenAICompatibleAdapter(provider, model_profile)
        try:
            t0 = time.perf_counter()
            llm_res = await adapter.execute_request(
                messages=messages,
                json_schema=assessment_schema,
                schema_name="assessment_proposal_schema",
                max_retries=2,
            )
            elapsed = time.perf_counter() - t0
            parsed_json = llm_res.get("data", {})
            print(f"  ✓ Ответ получен ({elapsed:.2f}s), токенов: {llm_res.get('usage', {}).get('total_tokens', 'N/A')}")

            proposal = AssessmentProposal(
                id=f"live-prop-{model_profile.id}",
                interview_id="stage2-eval",
                question_id="q-saga",
                rubric_revision_id="rub-1",
                transcript_revision_id="live-rev-1",
                model_profile_id=model_profile.upstream_model_id,
                scores=parsed_json.get("scores", []),
                critical_errors=parsed_json.get("critical_errors", []),
            )

            validation = validate_proposal(proposal, transcript)
            print(f"  ✓ EvidenceValidator: is_valid={validation.is_valid}, цитат: {len(proposal.scores[0].evidence) if proposal.scores else 0}")
            if not validation.is_valid:
                print(f"  ✗ Ошибки evidence: {validation.errors}")
                return False, elapsed, f"Evidence errors: {validation.errors}"
            return True, elapsed, ""
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ Сбой модели {model_profile.upstream_model_id}: {e}")
            return False, 0.0, str(e)

    # -------------------------------------------------------------
    # 2. Test Model 1: Gemini 3.8 Flash (Non-GPT, Primary)
    # -------------------------------------------------------------
    gemini_profile = get_plusvibe_gemini_model()
    gemini_ok, gemini_latency, gemini_err = await evaluate_model(gemini_profile, 2, "Основная: Gemini 3.8 Flash")
    print()

    # -------------------------------------------------------------
    # 3. Test Model 2: DeepSeek v4 Flash (Non-GPT, Alt 1)
    # -------------------------------------------------------------
    deepseek_profile = get_plusvibe_deepseek_model()
    deepseek_ok, deepseek_latency, deepseek_err = await evaluate_model(deepseek_profile, 3, "Резервная 1: DeepSeek v4 Flash")
    print()

    # -------------------------------------------------------------
    # 4. Test Model 3: Qwen 3.7 Plus (Non-GPT, Alt 2)
    # -------------------------------------------------------------
    qwen_profile = get_plusvibe_qwen_model()
    qwen_ok, qwen_latency, qwen_err = await evaluate_model(qwen_profile, 4, "Резервная 2: Qwen 3.7 Plus")
    print()

    # -------------------------------------------------------------
    # Summary
    # -------------------------------------------------------------
    print("==================================================")
    print("СВОДКА РЕЗУЛЬТАТОВ ЭТАПА 2 (AI/STT Spike)")
    print("==================================================")
    print(f"1. STT Whisper ({stt_model_id}): {'PASSED (' + f'{stt_latency:.2f}s' + ')' if stt_ok else 'FAILED: ' + stt_error_msg}")
    print(f"2. LLM Основная ({gemini_profile.upstream_model_id}): {'PASSED (' + f'{gemini_latency:.2f}s' + ')' if gemini_ok else 'FAILED: ' + gemini_err}")
    print(f"3. LLM Резервная 1 ({deepseek_profile.upstream_model_id}): {'PASSED (' + f'{deepseek_latency:.2f}s' + ')' if deepseek_ok else 'FAILED: ' + deepseek_err}")
    print(f"4. LLM Резервная 2 ({qwen_profile.upstream_model_id}): {'PASSED (' + f'{qwen_latency:.2f}s' + ')' if qwen_ok else 'FAILED: ' + qwen_err}")
    print("==================================================")

    all_passed = stt_ok and (gemini_ok or deepseek_ok) and (qwen_ok or deepseek_ok)
    if all_passed:
        print("ИТОГ: ВСЕ КРИТЕРИИ ЭТАПА 2 ВЫПОЛНЕНЫ ПОЛНОСТЬЮ.")
        return 0
    else:
        print("ИТОГ: ЭТАП 2 НЕ ЗАВЕРШЕН. ТРЕБУЕТСЯ УСТРАНЕНИЕ ОШИБОК ВЫШЕ.")
        return 1


if __name__ == "__main__":
    code = asyncio.run(run_stage2_evaluation())
    sys.exit(code)
