import ipaddress
import urllib.parse
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

from contracts.provider import StructuredOutputMode


class ProviderPreset(str, Enum):
    ROUTERAI = "routerai"
    PLUSVIBE = "plusvibe"
    CUSTOM = "custom"


class AuthMode(str, Enum):
    BEARER = "bearer"
    NONE = "none"


class ReasoningPolicy(str, Enum):
    PROVIDER_DEFAULT = "provider_default"
    DISABLE_ENABLE_THINKING = "disable_enable_thinking"
    DISABLE_REASONING_EFFORT = "disable_reasoning_effort"


class ApiKeyAction(str, Enum):
    PRESERVE = "preserve"
    REPLACE = "replace"
    CLEAR = "clear"


def is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    host_clean = host.strip("[]").lower()
    if host_clean in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        ip = ipaddress.ip_address(host_clean)
        return ip.is_loopback
    except ValueError:
        return False


def validate_network_url(url: str, auth_mode: AuthMode, is_base_url: bool = False) -> str:
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"URL scheme must be http or https, got '{parsed.scheme}'")
    if parsed.username or parsed.password:
        raise ValueError("URL must not contain user credentials (username or password)")
    if parsed.fragment:
        raise ValueError("URL must not contain a fragment")
    if not parsed.netloc:
        raise ValueError("URL must include a valid host")

    if is_base_url:
        path = parsed.path.rstrip("/")
        if path.endswith("/chat/completions"):
            raise ValueError("base_url must not end with /chat/completions")
    else:
        if not parsed.path or parsed.path == "/":
            raise ValueError("Endpoint URL must include a path")

    if (
        auth_mode == AuthMode.BEARER
        and parsed.scheme == "http"
        and not is_loopback_host(parsed.hostname)
    ):
        raise ValueError("Bearer authentication is forbidden over remote plain HTTP; use HTTPS or localhost/127.0.0.1")

    return url.strip()


class TranscriptionSettings(BaseModel):
    preset: ProviderPreset
    provider_id: str
    provider_name: str
    endpoint_url: str
    model_id: str
    auth_mode: AuthMode = AuthMode.BEARER
    language: str = "ru"
    timeout_seconds: float = Field(default=60.0, ge=1.0, le=300.0)
    max_concurrency: int = Field(default=2, ge=1, le=20)

    @field_validator("model_id")
    @classmethod
    def validate_model_id(cls, v: str) -> str:
        v_clean = v.strip()
        if not v_clean:
            raise ValueError("model_id must not be empty")
        return v_clean

    @model_validator(mode="after")
    def validate_transcription_url(self) -> "TranscriptionSettings":
        validate_network_url(self.endpoint_url, self.auth_mode, is_base_url=False)
        return self


class AnalysisModelSettings(BaseModel):
    model_id: str
    structured_output_mode: StructuredOutputMode = StructuredOutputMode.JSON_OBJECT
    reasoning_policy: ReasoningPolicy = ReasoningPolicy.PROVIDER_DEFAULT
    supports_temperature: bool = True
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_output_tokens: int = Field(default=4096, ge=256, le=65536)
    context_window_tokens: int = Field(default=32768, ge=2048)

    @field_validator("model_id")
    @classmethod
    def validate_model_id(cls, v: str) -> str:
        v_clean = v.strip()
        if not v_clean:
            raise ValueError("model_id must not be empty")
        return v_clean


class TextAnalysisSettings(BaseModel):
    preset: ProviderPreset
    provider_id: str
    provider_name: str
    base_url: str
    auth_mode: AuthMode = AuthMode.BEARER
    timeout_seconds: float = Field(default=60.0, ge=1.0, le=300.0)
    max_concurrency: int = Field(default=10, ge=1, le=100)
    primary_model: AnalysisModelSettings
    fallback_models: list[AnalysisModelSettings] = Field(default_factory=list, max_length=2)

    @model_validator(mode="after")
    def validate_analysis_url(self) -> "TextAnalysisSettings":
        validate_network_url(self.base_url, self.auth_mode, is_base_url=True)
        return self


class ApiKeyUpdate(BaseModel):
    action: ApiKeyAction = ApiKeyAction.PRESERVE
    value: SecretStr | None = None

    @model_validator(mode="after")
    def validate_api_key_update(self) -> "ApiKeyUpdate":
        if self.action in (ApiKeyAction.PRESERVE, ApiKeyAction.CLEAR):
            if self.value is not None:
                raise ValueError(f"value must be None when action is {self.action.value}")
        elif self.action == ApiKeyAction.REPLACE:
            if self.value is None:
                raise ValueError("value is required when action is replace")
            raw = self.value.get_secret_value().strip()
            if not raw:
                raise ValueError("value must not be empty or whitespace when action is replace")
            if "\n" in raw or "\r" in raw:
                raise ValueError("API key must not contain newline characters")
        return self


class UpdateAiSettingsRequest(BaseModel):
    expected_revision: int = Field(..., ge=0)
    transcription: TranscriptionSettings
    text_analysis: TextAnalysisSettings
    stt_api_key: ApiKeyUpdate = Field(default_factory=ApiKeyUpdate)
    llm_api_key: ApiKeyUpdate = Field(default_factory=ApiKeyUpdate)


class CredentialStatus(BaseModel):
    configured: bool
    source: Literal["process_environment", "runtime_file", "legacy_environment", "none"]
    editable: bool


class AiSettingsResponse(BaseModel):
    revision: int = Field(..., ge=0)
    source: Literal["environment", "database"]
    transcription: TranscriptionSettings
    text_analysis: TextAnalysisSettings
    stt_credentials: CredentialStatus
    llm_credentials: CredentialStatus
    can_update: bool
    update_blocker: str | None = None


class TestTarget(str, Enum):
    __test__ = False
    STT = "stt"
    LLM = "llm"


class TestAiSettingsRequest(BaseModel):
    __test__ = False
    target: TestTarget
    transcription: TranscriptionSettings | None = None
    text_analysis: TextAnalysisSettings | None = None
    api_key: SecretStr | None = None

    @model_validator(mode="after")
    def validate_request(self) -> "TestAiSettingsRequest":
        if self.target == TestTarget.STT and self.transcription is None:
            raise ValueError("transcription must be provided when target is stt")
        if self.target == TestTarget.LLM and self.text_analysis is None:
            raise ValueError("text_analysis must be provided when target is llm")
        if self.api_key is not None:
            raw = self.api_key.get_secret_value().strip()
            if not raw:
                raise ValueError("api_key must not be empty if provided")
            if "\n" in raw or "\r" in raw:
                raise ValueError("api_key must not contain newline characters")
        return self


class TestAiSettingsResponse(BaseModel):
    __test__ = False
    target: TestTarget
    success: bool
    provider_id: str
    model_id: str
    latency_ms: float
    message: str
