"""
Speech Turn Assembler for Nebula (VAD and Turn Boundary Detection).
Сборщик реплик стенограммы для Nebula (VAD и детектор границ речевых блоков).

Extracts coherent speech turns from continuous audio chunks based on energy VAD,
adaptive noise floor, silence thresholds, pre-roll/post-roll margins, and max duration cuts.
Извлекает цельные реплики из потока аудиочанков на основе энергетического VAD,
адаптивного уровня шума, порогов тишины, отступов pre-roll/post-roll и принудительного
разреза по максимальной длительности.
"""
from __future__ import annotations

import bisect
import math
import struct
from dataclasses import dataclass, field
from typing import Any

# VAD and Segmentation Constants / Константы VAD и сегментации
FRAME_MS: int = 20
SAMPLE_RATE: int = 16000
BYTES_PER_SAMPLE: int = 2
SAMPLES_PER_FRAME: int = (SAMPLE_RATE * FRAME_MS) // 1000  # 320 samples
BYTES_PER_FRAME: int = SAMPLES_PER_FRAME * BYTES_PER_SAMPLE  # 640 bytes

SILENCE_TIMEOUT_MS: int = 800
MAX_TURN_DURATION_MS: int = 12000
PRE_ROLL_MS: int = 240
POST_ROLL_MS: int = 160
MIN_SPEECH_DURATION_MS: int = 40

DEFAULT_INITIAL_NOISE_FLOOR_DBFS: float = -55.0
MIN_NOISE_FLOOR_DBFS: float = -70.0
MAX_NOISE_FLOOR_DBFS: float = -35.0
SPEECH_TRIGGER_DELTA_DB: float = 10.0
SPEECH_CONTINUE_DELTA_DB: float = 5.0
MIN_SPEECH_TRIGGER_DBFS: float = -42.0
MIN_SPEECH_CONTINUE_DBFS: float = -48.0


@dataclass(frozen=True)
class AudioChunkRef:
    """Reference to an audio chunk in the assembly pipeline."""
    sequence: int
    start_time_ms: int
    end_time_ms: int
    sample_rate: int
    channels: int
    format: str
    pcm_bytes: bytes


@dataclass(frozen=True, slots=True)
class _ChunkSpan:
    sequence: int
    stream_sample_start: int
    stream_sample_end: int
    chunk_sample_start: int
    start_time_ms: int
    sample_rate: int


@dataclass(frozen=True)
class TurnAssemblyCursor:
    """Cursor representing the first uncommitted sample in the stream."""
    sequence: int = 0
    sample_offset: int = 0


@dataclass(frozen=True)
class AssembledTurn:
    """
    Completed speech turn ready for transcription.
    Завершенный речевой блок, готовый к отправке на распознавание (STT).
    """
    track_id: str
    capture_epoch: int
    first_sequence: int
    first_sample_offset: int
    last_sequence: int
    last_sample_offset: int
    start_ms: int
    end_ms: int
    sample_rate: int = SAMPLE_RATE
    channels: int = 1
    language: str = "ru"

    @property
    def deterministic_job_id(self) -> str:
        return (
            f"stt-turn-{self.track_id}-{self.capture_epoch}-"
            f"{self.first_sequence}-{self.first_sample_offset}-"
            f"{self.last_sequence}-{self.last_sample_offset}"
        )

    @property
    def deterministic_segment_id(self) -> str:
        return (
            f"seg-{self.track_id}-{self.capture_epoch}-"
            f"{self.first_sequence}-{self.first_sample_offset}-"
            f"{self.last_sequence}-{self.last_sample_offset}"
        )

    def to_payload_dict(self) -> dict[str, Any]:
        return {
            "track_id": self.track_id,
            "capture_epoch": self.capture_epoch,
            "first_sequence": self.first_sequence,
            "first_sample_offset": self.first_sample_offset,
            "last_sequence": self.last_sequence,
            "last_sample_offset": self.last_sample_offset,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "language": self.language,
        }


@dataclass
class TurnAssemblerOutput:
    """Output of turn assembly processing."""
    turns: list[AssembledTurn] = field(default_factory=list)
    next_sequence: int = 0
    next_sample_offset: int = 0
    has_open_tail: bool = False
    open_tail_duration_ms: int = 0


def calculate_frame_rms_dbfs(frame_bytes: bytes) -> tuple[float, float]:
    """
    Calculates RMS amplitude and dBFS for a 16-bit mono PCM audio frame.
    Вычисляет среднеквадратичную амплитуду (RMS) и dBFS для 16-битного моно PCM фрейма.
    """
    sample_count = len(frame_bytes) // 2
    if sample_count == 0:
        return 0.0, -100.0
    samples = struct.unpack(f"<{sample_count}h", frame_bytes)
    sum_sq = sum(s * s for s in samples)
    rms = math.sqrt(sum_sq / sample_count)
    dbfs = -100.0 if rms < 1.0 else 20.0 * math.log10(rms / 32768.0)
    return rms, dbfs


@dataclass
class _FrameMeta:
    index: int
    seq: int
    sample_offset: int
    time_ms: int
    dbfs: float
    is_speech_trigger: bool
    is_speech_continue: bool


class TurnAssembler:
    """
    Pure turn assembler for turning contiguous PCM chunks into coherent speech turns.
    Чистый сборщик реплик для преобразования непрерывных PCM чанков в речевые блоки.
    """

    def __init__(
        self,
        frame_ms: int = FRAME_MS,
        silence_timeout_ms: int = SILENCE_TIMEOUT_MS,
        max_turn_duration_ms: int = MAX_TURN_DURATION_MS,
        pre_roll_ms: int = PRE_ROLL_MS,
        post_roll_ms: int = POST_ROLL_MS,
        min_speech_duration_ms: int = MIN_SPEECH_DURATION_MS,
        initial_noise_floor_dbfs: float = DEFAULT_INITIAL_NOISE_FLOOR_DBFS,
    ) -> None:
        self.frame_ms = frame_ms
        self.samples_per_frame = (SAMPLE_RATE * frame_ms) // 1000
        self.bytes_per_frame = self.samples_per_frame * BYTES_PER_SAMPLE
        self.silence_timeout_ms = silence_timeout_ms
        self.max_turn_duration_ms = max_turn_duration_ms
        self.pre_roll_ms = pre_roll_ms
        self.post_roll_ms = post_roll_ms
        self.min_speech_duration_ms = min_speech_duration_ms
        self.noise_floor_dbfs = initial_noise_floor_dbfs

    def assemble(
        self,
        chunks: list[AudioChunkRef],
        cursor: TurnAssemblyCursor,
        is_flush: bool = False,
        track_id: str = "shared",
        capture_epoch: int = 1,
        language: str = "ru",
    ) -> TurnAssemblerOutput:
        """
        Processes a list of audio chunks starting from cursor, segmenting into turns.
        Обрабатывает список аудиочанков начиная с курсора, разделяя их на реплики.
        """
        if not chunks:
            return TurnAssemblerOutput(
                turns=[],
                next_sequence=cursor.sequence,
                next_sample_offset=cursor.sample_offset,
                has_open_tail=False,
                open_tail_duration_ms=0,
            )

        # Filter out chunks strictly before cursor
        relevant_chunks = [c for c in chunks if c.sequence >= cursor.sequence]
        if not relevant_chunks:
            return TurnAssemblerOutput(
                turns=[],
                next_sequence=cursor.sequence,
                next_sample_offset=cursor.sample_offset,
                has_open_tail=False,
                open_tail_duration_ms=0,
            )

        # Sort by sequence
        relevant_chunks.sort(key=lambda c: c.sequence)

        # Check for sequence continuity and split at gaps
        chunk_groups: list[list[AudioChunkRef]] = []
        current_group: list[AudioChunkRef] = [relevant_chunks[0]]

        for c in relevant_chunks[1:]:
            prev = current_group[-1]
            if c.sequence != prev.sequence + 1 or abs(c.start_time_ms - prev.end_time_ms) > 100:
                # Gap detected: cut current group
                chunk_groups.append(current_group)
                current_group = [c]
            else:
                current_group.append(c)
        chunk_groups.append(current_group)

        all_turns: list[AssembledTurn] = []
        current_cursor = cursor

        for idx, group in enumerate(chunk_groups):
            is_last_group = (idx == len(chunk_groups) - 1)
            # If gap occurs before last group, force flush the group before the gap
            group_flush = is_flush if is_last_group else True
            group_start_cursor = current_cursor if group[0].sequence == current_cursor.sequence else TurnAssemblyCursor(group[0].sequence, 0)

            group_out = self._assemble_contiguous_group(
                group=group,
                cursor=group_start_cursor,
                is_flush=group_flush,
                track_id=track_id,
                capture_epoch=capture_epoch,
                language=language,
            )
            all_turns.extend(group_out.turns)
            current_cursor = TurnAssemblyCursor(group_out.next_sequence, group_out.next_sample_offset)
            if not is_last_group:
                # Advance cursor past the gap to the start of the next group
                next_group = chunk_groups[idx + 1]
                current_cursor = TurnAssemblyCursor(next_group[0].sequence, 0)

        last_out = group_out
        return TurnAssemblerOutput(
            turns=all_turns,
            next_sequence=current_cursor.sequence,
            next_sample_offset=current_cursor.sample_offset,
            has_open_tail=last_out.has_open_tail if chunk_groups else False,
            open_tail_duration_ms=last_out.open_tail_duration_ms if chunk_groups else 0,
        )

    def _assemble_contiguous_group(
        self,
        group: list[AudioChunkRef],
        cursor: TurnAssemblyCursor,
        is_flush: bool,
        track_id: str,
        capture_epoch: int,
        language: str,
    ) -> TurnAssemblerOutput:
        """Assembles turns within a strictly contiguous group of chunks."""
        # Slice frames from the chunks starting from cursor
        frames: list[_FrameMeta] = []
        frame_bytes_list: list[bytes] = []

        total_samples = 0
        spans: list[_ChunkSpan] = []
        span_ends: list[int] = []
        pcm_stream = bytearray()

        for chunk in group:
            chunk_samples = len(chunk.pcm_bytes) // BYTES_PER_SAMPLE
            start_sample = cursor.sample_offset if chunk.sequence == cursor.sequence else 0
            if start_sample >= chunk_samples:
                continue

            active_bytes = chunk.pcm_bytes[start_sample * BYTES_PER_SAMPLE :]
            pcm_stream.extend(active_bytes)
            num_samples = chunk_samples - start_sample

            spans.append(
                _ChunkSpan(
                    sequence=chunk.sequence,
                    stream_sample_start=total_samples,
                    stream_sample_end=total_samples + num_samples,
                    chunk_sample_start=start_sample,
                    start_time_ms=chunk.start_time_ms,
                    sample_rate=chunk.sample_rate,
                )
            )
            total_samples += num_samples
            span_ends.append(total_samples)

        if not spans or total_samples == 0:
            return TurnAssemblerOutput(
                turns=[],
                next_sequence=cursor.sequence,
                next_sample_offset=cursor.sample_offset,
                has_open_tail=False,
                open_tail_duration_ms=0,
            )

        def resolve_sample(stream_sample_idx: int) -> tuple[int, int, int]:
            """Returns (sequence, sample_offset_in_chunk, time_ms) for a sample index in pcm_stream."""
            idx = bisect.bisect_right(span_ends, stream_sample_idx)
            if idx >= len(spans):
                idx = len(spans) - 1
            span = spans[idx]
            offset_in_span = stream_sample_idx - span.stream_sample_start
            chunk_off = span.chunk_sample_start + offset_in_span
            t_ms = span.start_time_ms + int(chunk_off * 1000 / span.sample_rate)
            return span.sequence, chunk_off, t_ms

        # Segment pcm_stream into frames
        num_frames = len(pcm_stream) // self.bytes_per_frame
        for f_idx in range(num_frames):
            b_start = f_idx * self.bytes_per_frame
            b_end = b_start + self.bytes_per_frame
            f_bytes = bytes(pcm_stream[b_start:b_end])
            frame_bytes_list.append(f_bytes)

            sample_start_idx = f_idx * self.samples_per_frame
            s_seq, s_off, s_time = resolve_sample(sample_start_idx)

            rms, dbfs = calculate_frame_rms_dbfs(f_bytes)

            # Adaptive noise floor update
            speech_trig_thresh = max(self.noise_floor_dbfs + SPEECH_TRIGGER_DELTA_DB, MIN_SPEECH_TRIGGER_DBFS)
            speech_cont_thresh = max(self.noise_floor_dbfs + SPEECH_CONTINUE_DELTA_DB, MIN_SPEECH_CONTINUE_DBFS)

            is_trigger = dbfs >= speech_trig_thresh
            is_continue = dbfs >= speech_cont_thresh

            if not is_continue:
                # Update noise floor towards current dbfs
                if dbfs < self.noise_floor_dbfs:
                    self.noise_floor_dbfs = 0.9 * self.noise_floor_dbfs + 0.1 * dbfs
                else:
                    self.noise_floor_dbfs = 0.99 * self.noise_floor_dbfs + 0.01 * dbfs
                self.noise_floor_dbfs = max(MIN_NOISE_FLOOR_DBFS, min(MAX_NOISE_FLOOR_DBFS, self.noise_floor_dbfs))

            frames.append(
                _FrameMeta(
                    index=f_idx,
                    seq=s_seq,
                    sample_offset=s_off,
                    time_ms=s_time,
                    dbfs=dbfs,
                    is_speech_trigger=is_trigger,
                    is_speech_continue=is_continue,
                )
            )

        turns: list[AssembledTurn] = []
        pre_roll_frames = self.pre_roll_ms // self.frame_ms
        post_roll_frames = self.post_roll_ms // self.frame_ms
        silence_timeout_frames = self.silence_timeout_ms // self.frame_ms
        max_turn_frames = self.max_turn_duration_ms // self.frame_ms

        in_speech = False
        speech_start_frame = 0
        last_speech_frame = 0
        speech_frames_count = 0
        silence_after_speech_frames = 0
        last_committed_frame_end = 0

        def emit_turn(start_f: int, end_f: int) -> None:
            start_sample_idx = start_f * self.samples_per_frame
            end_sample_idx = min((end_f + 1) * self.samples_per_frame, total_samples)

            first_seq, first_off, start_ms = resolve_sample(start_sample_idx)
            last_seq, last_off, _ = resolve_sample(end_sample_idx - 1)
            end_sample_offset = last_off + 1

            # Find the chunk containing the last sample to calculate exact end_ms
            last_chunk = next(c for c in group if c.sequence == last_seq)
            end_ms = last_chunk.start_time_ms + int(end_sample_offset * 1000 / last_chunk.sample_rate)

            turn = AssembledTurn(
                track_id=track_id,
                capture_epoch=capture_epoch,
                first_sequence=first_seq,
                first_sample_offset=first_off,
                last_sequence=last_seq,
                last_sample_offset=end_sample_offset,
                start_ms=start_ms,
                end_ms=end_ms,
                sample_rate=SAMPLE_RATE,
                channels=1,
                language=language,
            )
            turns.append(turn)

        for i, frame in enumerate(frames):
            if not in_speech:
                if frame.is_speech_trigger:
                    in_speech = True
                    speech_start_frame = max(last_committed_frame_end, i - pre_roll_frames)
                    last_speech_frame = i
                    speech_frames_count = 1
                    silence_after_speech_frames = 0
            else:
                current_duration_frames = i - speech_start_frame + 1

                # Check max duration cut
                if current_duration_frames >= max_turn_frames:
                    emit_turn(speech_start_frame, i)
                    last_committed_frame_end = i + 1
                    # Forced cut without overlap
                    speech_start_frame = i + 1
                    last_speech_frame = i + 1
                    speech_frames_count = 1 if frame.is_speech_continue else 0
                    silence_after_speech_frames = 0 if frame.is_speech_continue else 1
                    in_speech = frame.is_speech_continue
                    continue

                if frame.is_speech_continue:
                    speech_frames_count += 1
                    last_speech_frame = i
                    silence_after_speech_frames = 0
                else:
                    silence_after_speech_frames += 1
                    if silence_after_speech_frames >= silence_timeout_frames:
                        # Turn complete
                        actual_speech_ms = speech_frames_count * self.frame_ms
                        if actual_speech_ms >= self.min_speech_duration_ms:
                            turn_end_frame = min(last_speech_frame + post_roll_frames, i)
                            emit_turn(speech_start_frame, turn_end_frame)
                            last_committed_frame_end = turn_end_frame + 1

                        in_speech = False
                        speech_frames_count = 0
                        silence_after_speech_frames = 0

        # Handle stream completion / tail
        has_open_tail = False
        open_tail_duration_ms = 0

        if in_speech:
            if is_flush:
                actual_speech_ms = speech_frames_count * self.frame_ms
                if actual_speech_ms >= self.min_speech_duration_ms:
                    turn_end_frame = min(last_speech_frame + post_roll_frames, len(frames) - 1)
                    emit_turn(speech_start_frame, turn_end_frame)
                    last_committed_frame_end = turn_end_frame + 1
                in_speech = False
            else:
                has_open_tail = True
                open_tail_duration_ms = (len(frames) - speech_start_frame) * self.frame_ms
                start_sample_idx = speech_start_frame * self.samples_per_frame
                next_seq, next_off, _ = resolve_sample(start_sample_idx)
                return TurnAssemblerOutput(
                    turns=turns,
                    next_sequence=next_seq,
                    next_sample_offset=next_off,
                    has_open_tail=has_open_tail,
                    open_tail_duration_ms=open_tail_duration_ms,
                )

        if is_flush or not frames:
            last_chunk = group[-1]
            next_seq = last_chunk.sequence + 1
            next_off = 0
        else:
            discard_until_frame = max(last_committed_frame_end, len(frames) - pre_roll_frames)
            discard_sample_idx = min(discard_until_frame * self.samples_per_frame, total_samples - 1)
            next_seq, next_off, _ = resolve_sample(discard_sample_idx)

        return TurnAssemblerOutput(
            turns=turns,
            next_sequence=next_seq,
            next_sample_offset=next_off,
            has_open_tail=False,
            open_tail_duration_ms=0,
        )
