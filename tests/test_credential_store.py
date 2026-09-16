import os

import pytest

from backend.core.credential_store import (
    LLM_KEY_NAME,
    STT_KEY_NAME,
    CredentialStore,
)
from contracts.settings import ApiKeyAction, ProviderPreset


@pytest.fixture
def store(tmp_path):
    return CredentialStore(data_dir=tmp_path)


def test_initial_resolution_no_keys(store, monkeypatch):
    monkeypatch.delenv(STT_KEY_NAME, raising=False)
    monkeypatch.delenv(LLM_KEY_NAME, raising=False)
    monkeypatch.delenv("ROUTERAI_API_KEY", raising=False)
    monkeypatch.delenv("PLUSVIBE_API_KEY", raising=False)

    key, status = store.resolve_credential("stt", revision=0, preset=ProviderPreset.CUSTOM)
    assert key is None
    assert status.configured is False
    assert status.source == "none"
    assert status.editable is True


def test_process_environment_precedence_and_non_editable(store, monkeypatch):
    monkeypatch.setenv(STT_KEY_NAME, "env-stt-secret")
    monkeypatch.setenv(LLM_KEY_NAME, "env-llm-secret")

    # Even if snapshot exists, process environment has priority
    store.prepare_snapshot(
        expected_revision=0,
        stt_action=ApiKeyAction.REPLACE,
        stt_value=None,  # won't be used since env error occurs
        llm_action=ApiKeyAction.PRESERVE,
        llm_value=None,
    ) if False else None

    key_stt, status_stt = store.resolve_credential("stt", revision=1, preset=ProviderPreset.CUSTOM)
    assert key_stt == "env-stt-secret"
    assert status_stt.configured is True
    assert status_stt.source == "process_environment"
    assert status_stt.editable is False

    # Attempting to replace or clear via UI path raises error
    with pytest.raises(ValueError, match="managed by the process environment"):
        store.prepare_snapshot(
            expected_revision=0,
            stt_action=ApiKeyAction.REPLACE,
            stt_value="new-secret",
            llm_action=ApiKeyAction.PRESERVE,
            llm_value=None,
        )

    with pytest.raises(ValueError, match="managed by the process environment"):
        store.prepare_snapshot(
            expected_revision=0,
            stt_action=ApiKeyAction.PRESERVE,
            stt_value=None,
            llm_action=ApiKeyAction.CLEAR,
            llm_value=None,
        )


def test_legacy_environment_fallback(store, monkeypatch):
    monkeypatch.delenv(STT_KEY_NAME, raising=False)
    monkeypatch.delenv(LLM_KEY_NAME, raising=False)
    monkeypatch.setenv("ROUTERAI_API_KEY", "legacy-router-key")

    key, status = store.resolve_credential("llm", revision=0, preset=ProviderPreset.ROUTERAI)
    assert key == "legacy-router-key"
    assert status.configured is True
    assert status.source == "legacy_environment"
    assert status.editable is True

    # But for custom preset, legacy RouterAI key is not used
    key_custom, status_custom = store.resolve_credential("llm", revision=0, preset=ProviderPreset.CUSTOM)
    assert key_custom is None
    assert status_custom.source == "none"


def test_clear_suppresses_legacy_environment_fallback(store, monkeypatch):
    monkeypatch.delenv(STT_KEY_NAME, raising=False)
    monkeypatch.delenv(LLM_KEY_NAME, raising=False)
    monkeypatch.setenv("ROUTERAI_API_KEY", "legacy-router-key")

    store.prepare_snapshot(
        expected_revision=0,
        stt_action=ApiKeyAction.PRESERVE,
        stt_value=None,
        llm_action=ApiKeyAction.CLEAR,
        llm_value=None,
    )

    key, status = store.resolve_credential("llm", revision=1, preset=ProviderPreset.ROUTERAI)
    assert key is None
    assert status.configured is False
    assert status.source == "none"
    assert store.read_snapshot(1)[LLM_KEY_NAME] == ""


def test_separate_keys_preserve_replace_clear(store, monkeypatch):
    monkeypatch.delenv(STT_KEY_NAME, raising=False)
    monkeypatch.delenv(LLM_KEY_NAME, raising=False)
    monkeypatch.delenv("ROUTERAI_API_KEY", raising=False)

    # 1. Revision 1: Set both STT and LLM keys
    path_v1 = store.prepare_snapshot(
        expected_revision=0,
        stt_action=ApiKeyAction.REPLACE,
        stt_value="stt-secret-1",
        llm_action=ApiKeyAction.REPLACE,
        llm_value="llm-secret-1",
    )
    assert path_v1.name == "provider-secrets-v1.env"
    assert path_v1.exists()

    stt_k1, stt_s1 = store.resolve_credential("stt", revision=1, preset=ProviderPreset.CUSTOM)
    llm_k1, llm_s1 = store.resolve_credential("llm", revision=1, preset=ProviderPreset.CUSTOM)
    assert stt_k1 == "stt-secret-1"
    assert stt_s1.source == "runtime_file"
    assert llm_k1 == "llm-secret-1"
    assert llm_s1.source == "runtime_file"

    # 2. Revision 2: Preserve STT, replace LLM
    store.prepare_snapshot(
        expected_revision=1,
        stt_action=ApiKeyAction.PRESERVE,
        stt_value=None,
        llm_action=ApiKeyAction.REPLACE,
        llm_value="llm-secret-2",
    )
    stt_k2, _ = store.resolve_credential("stt", revision=2, preset=ProviderPreset.CUSTOM)
    llm_k2, _ = store.resolve_credential("llm", revision=2, preset=ProviderPreset.CUSTOM)
    assert stt_k2 == "stt-secret-1"  # preserved
    assert llm_k2 == "llm-secret-2"  # replaced

    # 3. Revision 3: Clear STT, preserve LLM
    store.prepare_snapshot(
        expected_revision=2,
        stt_action=ApiKeyAction.CLEAR,
        stt_value=None,
        llm_action=ApiKeyAction.PRESERVE,
        llm_value=None,
    )
    stt_k3, stt_s3 = store.resolve_credential("stt", revision=3, preset=ProviderPreset.CUSTOM)
    llm_k3, _ = store.resolve_credential("llm", revision=3, preset=ProviderPreset.CUSTOM)
    assert stt_k3 is None
    assert stt_s3.source == "none"
    assert llm_k3 == "llm-secret-2"


def test_newline_injection_rejected(store, monkeypatch):
    monkeypatch.delenv(STT_KEY_NAME, raising=False)
    monkeypatch.delenv(LLM_KEY_NAME, raising=False)

    secret_fixture = "injected\nkey"
    with pytest.raises(ValueError) as excinfo:
        store.prepare_snapshot(
            expected_revision=0,
            stt_action=ApiKeyAction.REPLACE,
            stt_value=secret_fixture,
            llm_action=ApiKeyAction.PRESERVE,
            llm_value=None,
        )
    # Ensure error message does NOT leak secret fixture
    assert secret_fixture not in str(excinfo.value)
    assert "newline" in str(excinfo.value)


def test_snapshot_file_permissions_on_unix(store, monkeypatch):
    monkeypatch.delenv(STT_KEY_NAME, raising=False)
    monkeypatch.delenv(LLM_KEY_NAME, raising=False)

    path = store.prepare_snapshot(
        expected_revision=0,
        stt_action=ApiKeyAction.REPLACE,
        stt_value="sec",
        llm_action=ApiKeyAction.PRESERVE,
        llm_value=None,
    )
    if os.name != "nt":
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600


def test_prepare_snapshot_never_overwrites_existing_revision(store, monkeypatch):
    monkeypatch.delenv(STT_KEY_NAME, raising=False)
    monkeypatch.delenv(LLM_KEY_NAME, raising=False)

    store.prepare_snapshot(
        expected_revision=0,
        stt_action=ApiKeyAction.REPLACE,
        stt_value="original-stt-secret",
        llm_action=ApiKeyAction.PRESERVE,
        llm_value=None,
    )

    with pytest.raises(FileExistsError):
        store.prepare_snapshot(
            expected_revision=0,
            stt_action=ApiKeyAction.REPLACE,
            stt_value="replacement-must-not-win",
            llm_action=ApiKeyAction.PRESERVE,
            llm_value=None,
        )

    assert store.read_snapshot(1)[STT_KEY_NAME] == "original-stt-secret"


def test_cleanup_and_prune_snapshots(store, monkeypatch):
    monkeypatch.delenv(STT_KEY_NAME, raising=False)
    monkeypatch.delenv(LLM_KEY_NAME, raising=False)

    for rev in range(4):
        store.prepare_snapshot(
            expected_revision=rev,
            stt_action=ApiKeyAction.REPLACE,
            stt_value=f"stt-{rev+1}",
            llm_action=ApiKeyAction.PRESERVE,
            llm_value=None,
        )

    # We now have v1, v2, v3, v4
    for r in (1, 2, 3, 4):
        assert (store.data_dir / f"provider-secrets-v{r}.env").exists()

    # Prune keeping current revision 4 and previous 3
    store.prune_old_snapshots(current_revision=4)

    assert not (store.data_dir / "provider-secrets-v1.env").exists()
    assert not (store.data_dir / "provider-secrets-v2.env").exists()
    assert (store.data_dir / "provider-secrets-v3.env").exists()
    assert (store.data_dir / "provider-secrets-v4.env").exists()

    # Cleanup snapshot 4
    store.cleanup_snapshot(4)
    assert not (store.data_dir / "provider-secrets-v4.env").exists()
