import pytest
from pydantic import ValidationError

from contracts.audio import (
    AudioChunkMetadata,
    TrackType,
)
from contracts.domain import (
    ReportRevision,
)
from contracts.provider import (
    ModelProfile,
    ProviderProfile,
    StructuredOutputMode,
    TaskRoute,
    TaskType,
)


def test_provider_and_model_contracts():
    provider = ProviderProfile(
        id="test-vllm",
        name="Local vLLM",
        base_url="http://localhost:8000/v1",
        api_key_env="VLLM_API_KEY",
    )
    assert provider.id == "test-vllm"

    model = ModelProfile(
        id="qwen-assess",
        provider_id=provider.id,
        upstream_model_id="Qwen/Qwen2.5-72B-Instruct",
        structured_output_mode=StructuredOutputMode.JSON_SCHEMA,
    )
    assert model.upstream_model_id == "Qwen/Qwen2.5-72B-Instruct"

    route = TaskRoute(
        task=TaskType.ANSWER_ASSESSMENT,
        model_profile_id=model.id,
        prompt_version="1.0.0",
        schema_version="1.0.0",
    )
    assert route.task == TaskType.ANSWER_ASSESSMENT


def test_audio_chunk_metadata_validation():
    valid_sha256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    chunk = AudioChunkMetadata(
        interview_id="inv-123",
        track_id=TrackType.CANDIDATE,
        capture_epoch=0,
        sequence=1,
        start_time_ms=0,
        end_time_ms=5000,
        sample_count=80000,
        checksum_sha256=valid_sha256,
        size_bytes=160000,
    )
    assert chunk.sequence == 1

    # Invalid sha256 should raise validation error
    with pytest.raises(ValidationError):
        AudioChunkMetadata(
            interview_id="inv-123",
            track_id=TrackType.CANDIDATE,
            capture_epoch=0,
            sequence=1,
            start_time_ms=0,
            end_time_ms=5000,
            sample_count=80000,
            checksum_sha256="invalid-hash",
            size_bytes=160000,
        )


def test_domain_report_revision():
    report = ReportRevision(
        revision_id="rep-1",
        interview_id="inv-123",
        final_score=85.5,
        coverage_percentage=100.0,
        summary_markdown="Strong candidate with solid architecture knowledge.",
    )
    assert report.final_score == 85.5
    assert report.coverage_percentage == 100.0
