"""
Secure, versioned storage for STT and LLM API credentials.
API keys are never stored in SQLite, returned to clients, or written to logs.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path

from contracts.settings import ApiKeyAction, CredentialStatus, ProviderPreset

logger = logging.getLogger("nebula.credentials")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
STT_KEY_NAME = "NEBULA_STT_API_KEY"
LLM_KEY_NAME = "NEBULA_LLM_API_KEY"
ALLOWED_KEYS = {STT_KEY_NAME, LLM_KEY_NAME}


def get_data_dir() -> Path:
    """Returns the canonical data directory resolved from NEBULA_DATA_DIR or project root."""
    env_dir = os.getenv("NEBULA_DATA_DIR")
    path = Path(env_dir).resolve() if env_dir else (PROJECT_ROOT / "data").resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


class CredentialStore:
    def __init__(self, data_dir: Path | str | None = None) -> None:
        if data_dir is not None:
            self.data_dir = Path(data_dir).resolve()
            self.data_dir.mkdir(parents=True, exist_ok=True)
        else:
            self.data_dir = get_data_dir()

    def _get_snapshot_path(self, revision: int) -> Path:
        return self.data_dir / f"provider-secrets-v{revision}.env"

    def read_snapshot(self, revision: int) -> dict[str, str]:
        """
        Reads non-empty key-value pairs from the versioned secrets snapshot.
        Only allowed keys (NEBULA_STT_API_KEY, NEBULA_LLM_API_KEY) are extracted.
        """
        if revision <= 0:
            return {}
        path = self._get_snapshot_path(revision)
        if not path.is_file():
            return {}

        secrets: dict[str, str] = {}
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip()
                    if k in ALLOWED_KEYS and v:
                        secrets[k] = v
        except OSError as err:
            logger.warning("Failed to read credentials snapshot %s: %s", path.name, err)
            return {}

        return secrets

    def resolve_credential(
        self,
        slot: str,
        revision: int,
        preset: ProviderPreset | str,
    ) -> tuple[str | None, CredentialStatus]:
        """
        Resolves the effective credential and status for the given slot ('stt' or 'llm')
        according to the strict priority rules:
        1. Explicit process environment (NEBULA_STT_API_KEY / NEBULA_LLM_API_KEY)
        2. UI-managed versioned snapshot (provider-secrets-v<revision>.env)
        3. Legacy provider-specific variable (ROUTERAI_API_KEY / PLUSVIBE_API_KEY) for built-in presets
        4. None
        """
        key_name = STT_KEY_NAME if slot == "stt" else LLM_KEY_NAME

        # 1. Process environment has top priority and is not editable via UI
        env_val = os.environ.get(key_name)
        if env_val is not None:
            clean_env = env_val.strip()
            if clean_env:
                return clean_env, CredentialStatus(
                    configured=True,
                    source="process_environment",
                    editable=False,
                )

        # 2. UI-managed snapshot
        snapshot = self.read_snapshot(revision)
        snapshot_val = snapshot.get(key_name)
        if snapshot_val:
            return snapshot_val, CredentialStatus(
                configured=True,
                source="runtime_file",
                editable=True,
            )

        # 3. Legacy environment variables for built-in presets
        preset_str = preset.value if isinstance(preset, ProviderPreset) else str(preset).lower()
        legacy_val: str | None = None
        if preset_str == ProviderPreset.ROUTERAI.value:
            legacy_val = os.environ.get("ROUTERAI_API_KEY")
        elif preset_str == ProviderPreset.PLUSVIBE.value:
            legacy_val = os.environ.get("PLUSVIBE_API_KEY")

        if legacy_val is not None:
            clean_legacy = legacy_val.strip()
            if clean_legacy:
                return clean_legacy, CredentialStatus(
                    configured=True,
                    source="legacy_environment",
                    editable=True,
                )

        # 4. No credentials configured
        return None, CredentialStatus(
            configured=False,
            source="none",
            editable=True,
        )

    def prepare_snapshot(
        self,
        expected_revision: int,
        stt_action: ApiKeyAction,
        stt_value: str | None,
        llm_action: ApiKeyAction,
        llm_value: str | None,
    ) -> Path:
        """
        Atomically prepares the snapshot file for expected_revision + 1.
        Validates that process-environment managed keys cannot be modified.
        Returns the path to the newly written snapshot.
        """
        # Validate that process environment keys are not being modified via UI
        if stt_action in (ApiKeyAction.REPLACE, ApiKeyAction.CLEAR):
            env_stt = os.environ.get(STT_KEY_NAME)
            if env_stt and env_stt.strip():
                raise ValueError("Cannot modify STT API key: it is managed by the process environment")

        if llm_action in (ApiKeyAction.REPLACE, ApiKeyAction.CLEAR):
            env_llm = os.environ.get(LLM_KEY_NAME)
            if env_llm and env_llm.strip():
                raise ValueError("Cannot modify LLM API key: it is managed by the process environment")

        # Read existing effective UI snapshot
        current_snapshot = self.read_snapshot(expected_revision)

        # Determine next values
        def _resolve_val(action: ApiKeyAction, val: str | None, key: str) -> str | None:
            if action == ApiKeyAction.PRESERVE:
                return current_snapshot.get(key)
            if action == ApiKeyAction.CLEAR:
                return None
            if action == ApiKeyAction.REPLACE:
                if not val or not val.strip():
                    raise ValueError("Replacement key must not be empty or whitespace")
                clean = val.strip()
                if "\n" in clean or "\r" in clean:
                    raise ValueError("API key must not contain newline characters")
                return clean
            raise ValueError(f"Unknown ApiKeyAction: {action}")

        next_stt = _resolve_val(stt_action, stt_value, STT_KEY_NAME)
        next_llm = _resolve_val(llm_action, llm_value, LLM_KEY_NAME)

        next_revision = expected_revision + 1
        target_path = self._get_snapshot_path(next_revision)

        # Write to temporary file in the same directory for atomic rename
        fd, temp_path_str = tempfile.mkstemp(
            prefix=f"provider-secrets-v{next_revision}-",
            suffix=".tmp",
            dir=self.data_dir,
            text=True,
        )
        temp_path = Path(temp_path_str)
        try:
            with open(fd, "w", encoding="utf-8") as f:
                if next_stt:
                    f.write(f"{STT_KEY_NAME}={next_stt}\n")
                if next_llm:
                    f.write(f"{LLM_KEY_NAME}={next_llm}\n")
                f.flush()
                os.fsync(f.fileno())

            # Set mode 0600 on POSIX
            if os.name != "nt":
                os.chmod(temp_path, 0o600)

            os.replace(temp_path, target_path)
            return target_path
        except Exception:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
            raise

    def cleanup_snapshot(self, revision: int) -> None:
        """Deletes a snapshot file (used when an update transaction fails)."""
        path = self._get_snapshot_path(revision)
        if path.exists():
            path.unlink(missing_ok=True)

    def prune_old_snapshots(self, current_revision: int) -> None:
        """
        Retains the current and previous revision snapshots for recovery/diagnostics,
        pruning anything older.
        """
        keep_revisions = {current_revision, current_revision - 1}
        pattern = re.compile(r"^provider-secrets-v(\d+)\.env$")
        for entry in self.data_dir.iterdir():
            if entry.is_file():
                match = pattern.match(entry.name)
                if match:
                    rev = int(match.group(1))
                    if rev not in keep_revisions and rev < current_revision:
                        entry.unlink(missing_ok=True)
