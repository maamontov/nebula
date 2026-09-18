"""
Audio Health and Channel Degradation Monitoring.
Adheres to docs/implementation-plan.md Section 8 and Section 11:
- Detects silence on microphone or loopback channel.
- Detects audio clipping (peak >= 0.98).
- Flags excessive clock drift (> 200 ms).
"""

from dataclasses import dataclass
from enum import Enum


class ChannelHealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    CRITICAL = "critical"


@dataclass
class ChannelMetrics:
    rms: float
    peak: float
    silence_duration_ms: int = 0
    drift_ms: int = 0


@dataclass
class AudioHealthReport:
    interviewer_status: ChannelHealthStatus
    candidate_status: ChannelHealthStatus
    interviewer_warnings: list[str]
    candidate_warnings: list[str]
    is_healthy: bool


class AudioHealthMonitor:
    def __init__(
        self,
        silence_threshold_rms: float = 0.005,
        clipping_threshold_peak: float = 0.98,
        max_acceptable_drift_ms: int = 200,
    ):
        self.silence_threshold_rms = silence_threshold_rms
        self.clipping_threshold_peak = clipping_threshold_peak
        self.max_acceptable_drift_ms = max_acceptable_drift_ms

    def evaluate_health(
        self,
        interviewer: ChannelMetrics,
        candidate: ChannelMetrics,
    ) -> AudioHealthReport:
        int_warnings = []
        cand_warnings = []

        int_status = ChannelHealthStatus.HEALTHY
        cand_status = ChannelHealthStatus.HEALTHY

        # 1. Clipping detection
        if interviewer.peak >= self.clipping_threshold_peak:
            int_warnings.append(f"Клиппинг микрофона: пик {interviewer.peak:.2f} >= {self.clipping_threshold_peak}")
            int_status = ChannelHealthStatus.DEGRADED

        if candidate.peak >= self.clipping_threshold_peak:
            cand_warnings.append(f"Клиппинг loopback: пик {candidate.peak:.2f} >= {self.clipping_threshold_peak}")
            cand_status = ChannelHealthStatus.DEGRADED

        # 2. Prolonged silence detection
        if interviewer.silence_duration_ms > 30000 and interviewer.rms < self.silence_threshold_rms:
            int_warnings.append("Канал микрофона неактивен более 30 секунд (проверьте подключение/разрешения)")
            int_status = ChannelHealthStatus.DEGRADED

        if candidate.silence_duration_ms > 60000 and candidate.rms < self.silence_threshold_rms:
            cand_warnings.append("Канал кандидата неактивен более 60 секунд")
            cand_status = ChannelHealthStatus.DEGRADED

        # 3. Clock drift check
        if abs(interviewer.drift_ms - candidate.drift_ms) > self.max_acceptable_drift_ms:
            diff = abs(interviewer.drift_ms - candidate.drift_ms)
            msg = f"Межканальный дрейф звука {diff} мс превышает допустимый порог {self.max_acceptable_drift_ms} мс"
            int_warnings.append(msg)
            cand_warnings.append(msg)
            int_status = ChannelHealthStatus.CRITICAL
            cand_status = ChannelHealthStatus.CRITICAL

        is_healthy = (int_status == ChannelHealthStatus.HEALTHY) and (cand_status == ChannelHealthStatus.HEALTHY)

        return AudioHealthReport(
            interviewer_status=int_status,
            candidate_status=cand_status,
            interviewer_warnings=int_warnings,
            candidate_warnings=cand_warnings,
            is_healthy=is_healthy,
        )
