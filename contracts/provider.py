from enum import Enum

from pydantic import BaseModel, Field


class StructuredOutputMode(str, Enum):
    """
    Mode for enforcing structured responses from non-GPT OpenAI-compatible LLMs.
    Order of preference per docs/implementation-plan.md Section 4.3:
    1. JSON_SCHEMA (strict schema if supported upstream, e.g. vLLM guided decoding)
    2. JSON_OBJECT (standard json mode)
    3. PROMPT_INSTRUCTION (prompt-enforced JSON validation)
    """
    JSON_SCHEMA = "json_schema"
    JSON_OBJECT = "json_object"
    PROMPT_INSTRUCTION = "prompt_instruction"


class TaskType(str, Enum):
    """Pipeline tasks handled by LLM adapters."""
    QUESTION_EXTRACTION = "question_extraction"
    ANSWER_ASSESSMENT = "answer_assessment"
    FINAL_SUMMARY = "final_summary"


class ProviderProfile(BaseModel):
    """
    Configuration profile for an OpenAI-compatible upstream provider.
    No assumptions are made about GPT models or OpenAI proprietary endpoints.
    """
    id: str = Field(..., description="Unique internal provider identifier (e.g. 'vllm-local', 'deepseek-direct')")
    name: str = Field(..., description="Human-readable provider name")
    base_url: str = Field(..., description="OpenAI-compatible base URL (e.g. 'http://localhost:8000/v1')")
    api_key_env: str = Field(..., description="Name of environment variable storing secret key (never stored in DB)")
    allowed_endpoints: list[str] = Field(
        default_factory=lambda: ["/v1/chat/completions"],
        description="Allowed downstream endpoint paths to prevent credential exfiltration via redirects"
    )
    timeout_seconds: float = Field(default=30.0, ge=1.0, le=300.0)
    max_concurrency: int = Field(default=5, ge=1, le=100)
    rate_limit_rpm: int | None = Field(default=None, description="Requests per minute limit if known")


class ModelProfile(BaseModel):
    """
    Exact configuration for an upstream model.
    Model ID must match upstream identifier exactly, never masked as 'gpt-*'.
    """
    id: str = Field(..., description="Internal model profile ID (e.g. 'qwen2.5-72b-assess')")
    provider_id: str = Field(..., description="Reference to ProviderProfile.id")
    upstream_model_id: str = Field(..., description="Exact model ID passed in request body (e.g. 'Qwen/Qwen2.5-72B-Instruct')")
    structured_output_mode: StructuredOutputMode = Field(default=StructuredOutputMode.JSON_OBJECT)
    context_window_tokens: int = Field(default=32768, ge=2048)
    max_output_tokens: int = Field(default=4096, ge=256)
    supports_reasoning: bool = Field(
        default=False,
        description="Whether model produces internal reasoning tokens (e.g. <think>). Reasoning tokens are never shown as candidate explanation."
    )
    supports_temperature: bool = Field(default=True)
    default_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    supports_seed: bool = Field(default=False)


class TaskRoute(BaseModel):
    """Routing of a domain task to a verified model profile and prompt/schema versions."""
    task: TaskType
    model_profile_id: str
    prompt_version: str = Field(..., description="Semver of prompt template")
    schema_version: str = Field(..., description="Semver of output schema")
    request_budget_tokens: int = Field(default=4096, ge=512)


class STTProtocol(str, Enum):
    BATCH = "batch"
    STREAMING = "streaming"


class STTProfile(BaseModel):
    """Configuration for Speech-to-Text provider."""
    id: str
    name: str
    endpoint_url: str
    protocol: STTProtocol
    model_id: str
    supported_languages: list[str] = Field(default_factory=lambda: ["ru", "en"])
    supported_audio_formats: list[str] = Field(default_factory=lambda: ["pcm_s16le", "opus", "wav"])
    provides_timestamps: bool = True
    provides_partials: bool = False
    timeout_seconds: float = 60.0
