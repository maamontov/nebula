"""
Interview session state machine.
Enforces transition rules defined in docs/implementation-plan.md Section 6.
"""
from contracts.domain import InterviewStatus


class InvalidStateTransitionError(Exception):
    """Raised when an illegal status change is attempted."""
    def __init__(self, current_status: InterviewStatus, target_status: InterviewStatus, reason: str = ""):
        self.current_status = current_status
        self.target_status = target_status
        super().__init__(
            f"Cannot transition from {current_status.value} to {target_status.value}"
            + (f": {reason}" if reason else ".")
        )


ALLOWED_TRANSITIONS: dict[InterviewStatus, set[InterviewStatus]] = {
    InterviewStatus.DRAFT: {InterviewStatus.READY, InterviewStatus.DELETED},
    InterviewStatus.READY: {InterviewStatus.RECORDING, InterviewStatus.DRAFT, InterviewStatus.DELETED},
    InterviewStatus.RECORDING: {InterviewStatus.PAUSED, InterviewStatus.PROCESSING, InterviewStatus.DELETED},
    InterviewStatus.PAUSED: {InterviewStatus.RECORDING, InterviewStatus.PROCESSING, InterviewStatus.DELETED},
    InterviewStatus.PROCESSING: {InterviewStatus.REVIEW, InterviewStatus.RECORDING, InterviewStatus.DELETED},
    InterviewStatus.REVIEW: {InterviewStatus.FINALIZED, InterviewStatus.PROCESSING, InterviewStatus.DELETED},
    InterviewStatus.FINALIZED: {InterviewStatus.DELETED},
    InterviewStatus.DELETED: set(),  # Terminal state
}


def transition_status(
    current_status: InterviewStatus,
    target_status: InterviewStatus,
    allow_idempotent: bool = True,
) -> InterviewStatus:
    """
    Validates and performs transition between interview lifecycle states.
    """
    if current_status == target_status:
        if allow_idempotent:
            return current_status
        raise InvalidStateTransitionError(
            current_status, target_status, "Idempotent transition not allowed"
        )

    allowed_targets = ALLOWED_TRANSITIONS.get(current_status, set())
    if target_status not in allowed_targets:
        raise InvalidStateTransitionError(
            current_status,
            target_status,
            f"Permitted next states from '{current_status.value}' are: "
            f"{[s.value for s in allowed_targets] or 'none (terminal)'}",
        )

    return target_status


def can_accept_new_chunks(status: InterviewStatus) -> bool:
    """Only active recording or paused sessions accept audio chunks for upload."""
    return status in {InterviewStatus.RECORDING, InterviewStatus.PAUSED}


def can_run_ai_jobs(status: InterviewStatus) -> bool:
    """AI assessment and extraction jobs can only run during recording, processing, or review."""
    return status in {
        InterviewStatus.RECORDING,
        InterviewStatus.PROCESSING,
        InterviewStatus.REVIEW,
    }
