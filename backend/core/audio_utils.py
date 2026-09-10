"""
Audio utility functions for Nebula.
Provides standard RIFF/WAVE container encapsulation and audio metadata verification.
"""
from __future__ import annotations

import io
import wave


def pcm_s16le_to_wav_bytes(
    pcm_bytes: bytes,
    sample_rate: int = 16000,
    channels: int = 1,
) -> bytes:
    """
    Encapsulates raw PCM S16LE bytes into a fully compliant RIFF/WAVE container
    with standard 44-byte header and valid frame counts.
    """
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()
