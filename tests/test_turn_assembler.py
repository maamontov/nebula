"""
Unit tests for Pure TurnAssembler.
Тесты чистого сборщика реплик (VAD и границы реплик).
"""
import math
import struct

from backend.core.turn_assembler import (
    BYTES_PER_SAMPLE,
    SAMPLE_RATE,
    AudioChunkRef,
    TurnAssembler,
    TurnAssemblyCursor,
    calculate_frame_rms_dbfs,
)


def make_pcm_sine(duration_ms: int, freq_hz: float = 440.0, amplitude: float = 10000.0) -> bytes:
    """Generates synthetic mono 16-bit 16kHz sine wave audio."""
    sample_count = int(SAMPLE_RATE * duration_ms / 1000)
    samples = []
    for i in range(sample_count):
        val = int(amplitude * math.sin(2.0 * math.pi * freq_hz * i / SAMPLE_RATE))
        samples.append(max(-32768, min(32767, val)))
    return struct.pack(f"<{sample_count}h", *samples)


def make_pcm_silence(duration_ms: int) -> bytes:
    """Generates synthetic digital silence."""
    sample_count = int(SAMPLE_RATE * duration_ms / 1000)
    return b"\x00" * (sample_count * BYTES_PER_SAMPLE)


def make_pcm_noise(duration_ms: int, amplitude: float = 100.0) -> bytes:
    """Generates quiet ambient noise (e.g. amplitude 100, dBFS ~ -50)."""
    sample_count = int(SAMPLE_RATE * duration_ms / 1000)
    samples = []
    for i in range(sample_count):
        # Deterministic pseudo-noise
        val = int(amplitude * math.sin(i * 0.17) * math.cos(i * 0.31))
        samples.append(max(-32768, min(32767, val)))
    return struct.pack(f"<{sample_count}h", *samples)


def create_chunks_from_audio(
    pcm_bytes: bytes,
    chunk_duration_ms: int = 1000,
    start_seq: int = 0,
    start_time_ms: int = 0,
) -> list[AudioChunkRef]:
    """Splits raw PCM audio into sequential AudioChunkRefs."""
    bytes_per_chunk = int(SAMPLE_RATE * chunk_duration_ms / 1000) * BYTES_PER_SAMPLE
    chunks = []
    seq = start_seq
    current_time = start_time_ms

    for offset in range(0, len(pcm_bytes), bytes_per_chunk):
        chunk_slice = pcm_bytes[offset : offset + bytes_per_chunk]
        actual_dur_ms = int(len(chunk_slice) / (BYTES_PER_SAMPLE * SAMPLE_RATE) * 1000)
        chunks.append(
            AudioChunkRef(
                sequence=seq,
                start_time_ms=current_time,
                end_time_ms=current_time + actual_dur_ms,
                sample_rate=SAMPLE_RATE,
                channels=1,
                format="pcm_s16le",
                pcm_bytes=chunk_slice,
            )
        )
        seq += 1
        current_time += actual_dur_ms

    return chunks


def test_frame_rms_and_dbfs_calculation():
    silence = make_pcm_silence(20)
    rms, dbfs = calculate_frame_rms_dbfs(silence)
    assert rms == 0.0
    assert dbfs == -100.0

    sine = make_pcm_sine(20, amplitude=16384.0)
    rms, dbfs = calculate_frame_rms_dbfs(sine)
    assert rms > 10000.0
    assert -10.0 < dbfs < -3.0


def test_pure_silence_produces_no_turns():
    assembler = TurnAssembler()
    # 5 seconds of silence
    pcm = make_pcm_silence(5000)
    chunks = create_chunks_from_audio(pcm, chunk_duration_ms=1000)

    out = assembler.assemble(chunks, TurnAssemblyCursor(0, 0), is_flush=False)
    assert len(out.turns) == 0
    assert not out.has_open_tail
    # Cursor advances through silence
    assert out.next_sequence >= 4


def test_ten_seconds_speech_followed_by_silence_forms_one_turn():
    assembler = TurnAssembler()
    # 500ms silence, 10s speech, 1s silence
    pcm = (
        make_pcm_silence(500)
        + make_pcm_sine(10000, amplitude=8000.0)
        + make_pcm_silence(1000)
    )
    chunks = create_chunks_from_audio(pcm, chunk_duration_ms=1000)

    out = assembler.assemble(chunks, TurnAssemblyCursor(0, 0), is_flush=False)
    assert len(out.turns) == 1
    turn = out.turns[0]
    # Speech duration should be approximately 10 seconds (plus pre-roll / post-roll)
    turn_dur_ms = turn.end_ms - turn.start_ms
    assert 9500 <= turn_dur_ms <= 11000
    assert turn.first_sequence == 0
    assert turn.last_sequence >= 9


def test_speech_with_300ms_pause_forms_one_turn():
    assembler = TurnAssembler()
    # 2s speech + 300ms silence + 2s speech + 1s silence
    pcm = (
        make_pcm_sine(2000, amplitude=8000.0)
        + make_pcm_silence(300)
        + make_pcm_sine(2000, amplitude=8000.0)
        + make_pcm_silence(1000)
    )
    chunks = create_chunks_from_audio(pcm, chunk_duration_ms=1000)

    out = assembler.assemble(chunks, TurnAssemblyCursor(0, 0), is_flush=False)
    # 300ms pause should NOT split the speech into two turns
    assert len(out.turns) == 1
    turn = out.turns[0]
    turn_dur_ms = turn.end_ms - turn.start_ms
    assert 4000 <= turn_dur_ms <= 5000


def test_speech_with_900ms_pause_forms_two_turns():
    assembler = TurnAssembler()
    # 2s speech + 900ms silence + 2s speech + 1s silence
    pcm = (
        make_pcm_sine(2000, amplitude=8000.0)
        + make_pcm_silence(900)
        + make_pcm_sine(2000, amplitude=8000.0)
        + make_pcm_silence(1000)
    )
    chunks = create_chunks_from_audio(pcm, chunk_duration_ms=1000)

    out = assembler.assemble(chunks, TurnAssemblyCursor(0, 0), is_flush=False)
    # 900ms silence (> 800ms) should split speech into two turns
    assert len(out.turns) == 2
    turn1, turn2 = out.turns
    assert turn1.end_ms < turn2.start_ms
    assert 1800 <= (turn1.end_ms - turn1.start_ms) <= 2500
    assert 1800 <= (turn2.end_ms - turn2.start_ms) <= 2500


def test_twenty_five_seconds_speech_forced_max_split():
    assembler = TurnAssembler(max_turn_duration_ms=12000)
    # 25 seconds of continuous speech + 1s silence
    pcm = make_pcm_sine(25000, amplitude=8000.0) + make_pcm_silence(1000)
    chunks = create_chunks_from_audio(pcm, chunk_duration_ms=1000)

    out = assembler.assemble(chunks, TurnAssemblyCursor(0, 0), is_flush=False)
    # 25s continuous speech should be cut into:
    # Turn 1: 12s
    # Turn 2: 12s
    # Turn 3: 1s
    assert len(out.turns) == 3
    t1, t2, t3 = out.turns
    assert (t1.end_ms - t1.start_ms) == 12000
    assert (t2.end_ms - t2.start_ms) == 12000
    assert 900 <= (t3.end_ms - t3.start_ms) <= 1500
    # Continuous without overlap
    assert t1.end_ms == t2.start_ms
    assert t2.end_ms == t3.start_ms


def test_open_speech_tail_flushed_on_stop():
    assembler = TurnAssembler()
    # 3 seconds of speech, stream ends abruptly (no trailing silence)
    pcm = make_pcm_sine(3000, amplitude=8000.0)
    chunks = create_chunks_from_audio(pcm, chunk_duration_ms=1000)

    # Without flush: turn is held open
    out_pending = assembler.assemble(chunks, TurnAssemblyCursor(0, 0), is_flush=False)
    assert len(out_pending.turns) == 0
    assert out_pending.has_open_tail
    assert out_pending.next_sequence == 0

    # With flush: turn is emitted
    out_flushed = assembler.assemble(chunks, TurnAssemblyCursor(0, 0), is_flush=True)
    assert len(out_flushed.turns) == 1
    assert not out_flushed.has_open_tail
    turn = out_flushed.turns[0]
    assert 2800 <= (turn.end_ms - turn.start_ms) <= 3200


def test_sequence_gap_flushes_accumulated_turn():
    assembler = TurnAssembler()
    # 2 seconds speech in seq 0..1, then gap (seq 2, 3 missing), then 2 seconds speech in seq 4..5
    pcm1 = make_pcm_sine(2000, amplitude=8000.0)
    chunks1 = create_chunks_from_audio(pcm1, chunk_duration_ms=1000, start_seq=0, start_time_ms=0)

    pcm2 = make_pcm_sine(2000, amplitude=8000.0) + make_pcm_silence(1000)
    chunks2 = create_chunks_from_audio(pcm2, chunk_duration_ms=1000, start_seq=4, start_time_ms=4000)

    all_chunks = chunks1 + chunks2

    out = assembler.assemble(all_chunks, TurnAssemblyCursor(0, 0), is_flush=False)
    # Both speech parts should be emitted as separate turns due to gap
    assert len(out.turns) == 2
    assert out.turns[0].last_sequence <= 1
    assert out.turns[1].first_sequence >= 4


def test_background_noise_adapts_and_does_not_hold_turn_open():
    assembler = TurnAssembler(initial_noise_floor_dbfs=-60.0)
    # Ambient noise (amp=80, ~ -52 dBFS)
    # 2 seconds ambient noise, then 2 seconds speech, then 2 seconds ambient noise
    pcm = (
        make_pcm_noise(2000, amplitude=80.0)
        + make_pcm_sine(2000, amplitude=8000.0)
        + make_pcm_noise(2000, amplitude=80.0)
    )
    chunks = create_chunks_from_audio(pcm, chunk_duration_ms=1000)

    out = assembler.assemble(chunks, TurnAssemblyCursor(0, 0), is_flush=False)
    # Should identify speech in the middle and close it after silence/noise timeout
    assert len(out.turns) == 1
    turn = out.turns[0]
    turn_dur_ms = turn.end_ms - turn.start_ms
    assert 1800 <= turn_dur_ms <= 2600
