from enum import Enum

from pydantic import BaseModel, Field


class TrackType(str, Enum):
    """Audio track identification."""
    INTERVIEWER = "interviewer"
    CANDIDATE = "candidate"
    SHARED = "shared"


class AudioFormat(str, Enum):
    """Raw or compressed audio chunk format."""
    PCM_S16LE = "pcm_s16le"
    PCM_S16_LE = "pcm_s16_le"
    PCM_F32LE = "pcm_f32le"
    PCM_F32_LE = "pcm_f32_le"
    OPUS = "opus"
    WAV = "wav"


class AudioChunkMetadata(BaseModel):
    """
    Immutable metadata descriptor of an audio slice captured on the desktop.
    Sent with the chunk payload for verification before persisting.
    """
    interview_id: str
    track_id: TrackType
    capture_epoch: int = Field(ge=0, description="Monotonic capture epoch; increments on resume/reconnect")
    sequence: int = Field(ge=0, description="Zero-based sequence number within this epoch")
    start_time_ms: int = Field(ge=0, description="Start timestamp relative to interview monotonic clock")
    end_time_ms: int = Field(ge=0, description="End timestamp relative to interview monotonic clock")
    sample_rate: int = Field(default=16000, ge=8000, le=48000)
    channels: int = Field(default=1, ge=1, le=2)
    sample_count: int = Field(ge=0)
    format: AudioFormat = Field(default=AudioFormat.PCM_S16LE)
    checksum_sha256: str = Field(..., pattern=r"^[a-fA-F0-9]{64}$", description="SHA-256 hex digest of payload")
    size_bytes: int = Field(gt=0)


class AudioGap(BaseModel):
    """Represents a recognized audio loss / device drop / silence gap."""
    track_id: TrackType
    start_time_ms: int
    end_time_ms: int
    reason: str


class TrackManifest(BaseModel):
    """
    Manifest sealed at the end of recording to verify all chunks were received.
    """
    model_config = {"extra": "allow"}

    interview_id: str
    track_id: TrackType
    capture_epoch: int = Field(default=0, ge=0)
    total_chunks: int = Field(ge=0)
    total_duration_ms: int = Field(ge=0)
    is_sealed: bool = Field(default=False, description="True once Stop is called and upload is complete")
    gaps: list[AudioGap] = Field(default_factory=list)
    total_samples: int = Field(default=0, ge=0)
    dropped_samples: int = Field(default=0, ge=0)
