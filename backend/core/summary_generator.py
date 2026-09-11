"""
Executive Summary Generator.
Adheres strictly to docs/implementation-plan.md Section 7.3 & 10:
- Uses ResilientLLMAdapter (non-GPT models: Gemini 3.8 Flash -> Qwen 3.7 Plus -> DeepSeek v4 Flash).
- Generates structured executive summary based exclusively on confirmed evidence.
- Restricts assessments strictly to professional qualifications (no accent, gender, speed of speech).
- Emits objective hiring recommendation with evidence quotes.
"""

import json
import logging
from typing import Any

from backend.adapters.resilient_llm import ResilientLLMAdapter

logger = logging.getLogger("nebula.summary_generator")


class ExecutiveSummaryGenerator:
    def __init__(self, llm_adapter: ResilientLLMAdapter | None = None):
        self.llm_adapter = llm_adapter or ResilientLLMAdapter()

    async def generate_summary(
        self,
        candidate_name: str,
        role: str,
        decisions: list[dict[str, Any]],
        audio_health_summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Generates structured executive summary from confirmed human/AI decisions.
        """
        # Prepare context for prompt
        questions_context = []
        for d in decisions:
            q_id = d.get("question_id", "")
            scores = d.get("scores", [])
            notes = d.get("reviewer_notes") or d.get("explanation") or ""
            evidence_quotes = []
            for s in scores:
                for ev in s.get("evidence", []):
                    if ev.get("exact_quote"):
                        evidence_quotes.append(ev["exact_quote"])

            questions_context.append({
                "question_id": q_id,
                "scores": scores,
                "reviewer_notes": notes,
                "evidence_quotes": evidence_quotes,
            })

        system_prompt = (
            "Ты главный технический архитектор и эксперт по найму инженерных лидеров. "
            "Твоя задача — составить объективное, строго обоснованное итоговое резюме собеседования (Executive Summary). "
            "Правила формирования:\n"
            "1. Базируйся ИСКЛЮЧИТЕЛЬНО на предоставленных доказательствах и ответах кандидата.\n"
            "2. Никаких личных характеристик (пол, возраст, акцент, тембр, эмоции). Оценка строго профессиональная.\n"
            "3. Каждое утверждение в сильных сторонах и зонах роста должно сопровождаться дословной цитатой (evidence_quote).\n"
            "4. Рекомендация по найму должна быть одной из: STRONG_HIRE, HIRE, LEAN_HIRE, LEAN_NO_HIRE, NO_HIRE.\n"
            "5. Ответ должен быть СТРОГО в формате JSON по заданной схеме."
        )

        user_prompt = (
            f"Кандидат: {candidate_name}\n"
            f"Позиция: {role}\n"
            f"Оцененные вопросы и доказательства:\n"
            f"{json.dumps(questions_context, ensure_ascii=False, indent=2)}\n"
            f"Статус качества звука: {json.dumps(audio_health_summary or {}, ensure_ascii=False)}\n"
        )

        json_schema = {
            "type": "object",
            "properties": {
                "key_strengths": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                            "evidence_quote": {"type": "string"},
                        },
                        "required": ["title", "description"],
                    },
                },
                "growth_areas": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                            "evidence_quote": {"type": "string"},
                        },
                        "required": ["title", "description"],
                    },
                },
                "hiring_recommendation": {
                    "type": "string",
                    "enum": ["STRONG_HIRE", "HIRE", "LEAN_HIRE", "LEAN_NO_HIRE", "NO_HIRE"],
                },
                "recommendation_rationale": {"type": "string"},
                "session_limitations": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "summary_markdown": {"type": "string"},
            },
            "required": [
                "key_strengths",
                "growth_areas",
                "hiring_recommendation",
                "recommendation_rationale",
                "summary_markdown",
            ],
        }

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        res_dict, actual_model = await self.llm_adapter.execute_request(
            messages=messages,
            json_schema=json_schema,
            schema_name="executive_summary_schema",
        )

        parsed = res_dict.get("data", {})
        parsed["model_profile_id"] = actual_model
        return parsed
