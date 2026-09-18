"""
Tests for PipelineWorker AI settings runtime bundle and reload (Step 7).
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.adapters.llm import OpenAICompatibleAdapter
from backend.adapters.resilient_llm import ResilientLLMAdapter
from backend.adapters.stt import OpenAICompatibleSTTAdapter
from backend.core.credential_store import CredentialStore
from backend.db.database import Database
from backend.db.repository import Repository
from backend.workers.pipeline import PipelineWorker, WorkerAiBundle
from contracts.domain import InterviewStatus


@pytest.fixture
def repo(tmp_path):
    db_file = tmp_path / "test_worker.db"
    db = Database(str(db_file))
    db.init_schema()
    return Repository(db)


@pytest.fixture
def cred_store(tmp_path):
    secrets_dir = tmp_path / "secrets"
    return CredentialStore(data_dir=secrets_dir)


@pytest.mark.asyncio
async def test_worker_builds_bundle_from_db(repo, cred_store):
    worker = PipelineWorker(repository=repo, credential_store=cred_store)
    bundle = await worker._get_current_bundle()

    assert isinstance(bundle, WorkerAiBundle)
    assert bundle.revision == 0
    assert bundle.default_language == "ru"
    assert isinstance(bundle.stt_adapter, OpenAICompatibleSTTAdapter)
    assert isinstance(bundle.llm_adapter, ResilientLLMAdapter)
    assert isinstance(bundle.followup_llm_adapter, OpenAICompatibleAdapter)
    assert bundle.stt_semaphore is not bundle.llm_semaphore


@pytest.mark.asyncio
async def test_worker_runtime_reload_on_revision_bump(repo, cred_store):
    worker = PipelineWorker(repository=repo, credential_store=cred_store)
    initial_bundle = await worker._get_current_bundle()
    assert initial_bundle.revision == 0

    # Simulate settings update in DB to revision 1
    t_cfg = {
        "preset": "custom",
        "provider_id": "custom-stt",
        "provider_name": "Custom STT",
        "endpoint_url": "http://127.0.0.1:8080/v1/audio/transcriptions",
        "model_id": "custom-whisper-v1",
        "auth_mode": "none",
        "language": "en",
        "timeout_seconds": 60.0,
        "max_concurrency": 2,
    }
    llm_cfg = {
        "preset": "custom",
        "provider_id": "custom-llm",
        "provider_name": "Custom LLM",
        "base_url": "http://127.0.0.1:8080/v1",
        "auth_mode": "none",
        "timeout_seconds": 60.0,
        "max_concurrency": 10,
        "primary_model": {
            "model_id": "custom-primary-v1",
            "structured_output_mode": "json_object",
            "reasoning_policy": "disable_enable_thinking",
            "supports_temperature": True,
            "temperature": 0.0,
            "max_output_tokens": 4096,
            "context_window_tokens": 32768,
        },
        "fallback_models": [],
    }
    repo.update_ai_settings(
        expected_revision=0,
        transcription_config=t_cfg,
        text_analysis_config=llm_cfg,
    )

    # Next call to _get_current_bundle should detect revision 1 and reload
    new_bundle = await worker._get_current_bundle()
    assert new_bundle.revision == 1
    assert new_bundle.default_language == "en"
    assert new_bundle.stt_profile.model_id == "custom-whisper-v1"
    assert new_bundle.llm_adapter.primary_adapter.model.upstream_model_id == "custom-primary-v1"
    assert new_bundle is not initial_bundle


@pytest.mark.asyncio
async def test_worker_fixed_adapters_not_reloaded_from_db(repo, cred_store):
    mock_stt = MagicMock(spec=OpenAICompatibleSTTAdapter)
    mock_llm = MagicMock(spec=OpenAICompatibleAdapter)

    worker = PipelineWorker(
        repository=repo,
        stt_adapter=mock_stt,
        llm_adapter=mock_llm,
        credential_store=cred_store,
    )
    assert worker._fixed_adapters is True

    bundle1 = await worker._get_current_bundle()
    assert bundle1.stt_adapter is mock_stt
    assert bundle1.llm_adapter is mock_llm

    # Bump revision in DB with valid configs
    t_cfg = {
        "preset": "routerai",
        "provider_id": "routerai",
        "provider_name": "RouterAI",
        "endpoint_url": "https://router.ai/api/v1/audio/transcriptions",
        "model_id": "whisper-large-v3",
        "auth_mode": "bearer",
        "language": "ru",
        "timeout_seconds": 60.0,
        "max_concurrency": 2,
    }
    llm_cfg = {
        "preset": "routerai",
        "provider_id": "routerai",
        "provider_name": "RouterAI",
        "base_url": "https://router.ai/api/v1",
        "auth_mode": "bearer",
        "timeout_seconds": 60.0,
        "max_concurrency": 10,
        "primary_model": {
            "model_id": "openai/gpt-4o",
            "structured_output_mode": "json_object",
            "reasoning_policy": "provider_default",
            "supports_temperature": True,
            "temperature": 0.0,
            "max_output_tokens": 4096,
            "context_window_tokens": 32768,
        },
        "fallback_models": [],
    }
    repo.update_ai_settings(
        expected_revision=0,
        transcription_config=t_cfg,
        text_analysis_config=llm_cfg,
    )

    bundle2 = await worker._get_current_bundle()
    assert bundle2.stt_adapter is mock_stt
    assert bundle2.llm_adapter is mock_llm
    assert bundle2 is bundle1


@pytest.mark.asyncio
async def test_worker_handles_stt_with_bundle_semaphore_and_adapter(repo, cred_store):
    mock_stt = MagicMock()
    mock_stt.transcribe_audio = AsyncMock(
        return_value=MagicMock(
            text="Добрый день, начинаем интервью.",
            model_id="mock-stt-v1",
            latency_seconds=0.1,
        )
    )

    worker = PipelineWorker(repository=repo, stt_adapter=mock_stt, credential_store=cred_store)
    repo.create_interview("inv-stt-1", "Go Dev", "Иван", "Go", status=InterviewStatus.RECORDING)

    repo.get_turn_audio_pcm = MagicMock(return_value=b"\x00" * 32000)

    repo.enqueue_job(
        job_id="job-stt-1",
        job_type="TRANSCRIBE_TURN",
        interview_id="inv-stt-1",
        payload={
            "track_id": "candidate",
            "capture_epoch": 1,
            "first_sequence": 0,
            "first_sample_offset": 0,
            "last_sequence": 0,
            "last_sample_offset": 32000,
            "start_ms": 0,
            "end_ms": 2000,
        },
    )

    processed = await worker.process_one_job()
    assert processed is True
    assert mock_stt.transcribe_audio.await_count == 1

    # Check segment saved with model_id
    segs = repo.get_transcript_segments("inv-stt-1")
    assert len(segs) == 1
    assert segs[0]["text"] == "Добрый день, начинаем интервью."
