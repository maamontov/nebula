import pytest

from backend.core.state_machine import (
    InvalidStateTransitionError,
    can_accept_new_chunks,
    can_run_ai_jobs,
    transition_status,
)
from contracts.domain import InterviewStatus


def test_standard_happy_path_transitions():
    state = InterviewStatus.DRAFT
    state = transition_status(state, InterviewStatus.READY)
    assert state == InterviewStatus.READY

    state = transition_status(state, InterviewStatus.RECORDING)
    assert state == InterviewStatus.RECORDING

    state = transition_status(state, InterviewStatus.PAUSED)
    assert state == InterviewStatus.PAUSED

    state = transition_status(state, InterviewStatus.RECORDING)
    assert state == InterviewStatus.RECORDING

    state = transition_status(state, InterviewStatus.PROCESSING)
    assert state == InterviewStatus.PROCESSING

    state = transition_status(state, InterviewStatus.REVIEW)
    assert state == InterviewStatus.REVIEW

    state = transition_status(state, InterviewStatus.FINALIZED)
    assert state == InterviewStatus.FINALIZED


def test_terminal_deleted_from_any_state():
    for status in [
        InterviewStatus.DRAFT,
        InterviewStatus.READY,
        InterviewStatus.RECORDING,
        InterviewStatus.PAUSED,
        InterviewStatus.PROCESSING,
        InterviewStatus.REVIEW,
        InterviewStatus.FINALIZED,
    ]:
        next_s = transition_status(status, InterviewStatus.DELETED)
        assert next_s == InterviewStatus.DELETED

    # DELETED is strictly terminal
    with pytest.raises(InvalidStateTransitionError):
        transition_status(InterviewStatus.DELETED, InterviewStatus.DRAFT)


def test_forbidden_transitions():
    # Cannot jump from DRAFT to RECORDING directly without READY
    with pytest.raises(InvalidStateTransitionError):
        transition_status(InterviewStatus.DRAFT, InterviewStatus.RECORDING)

    # Cannot jump from DRAFT directly to FINALIZED
    with pytest.raises(InvalidStateTransitionError):
        transition_status(InterviewStatus.DRAFT, InterviewStatus.FINALIZED)

    # Cannot resume RECORDING from FINALIZED
    with pytest.raises(InvalidStateTransitionError):
        transition_status(InterviewStatus.FINALIZED, InterviewStatus.RECORDING)


def test_state_predicates():
    assert can_accept_new_chunks(InterviewStatus.RECORDING) is True
    assert can_accept_new_chunks(InterviewStatus.PAUSED) is True
    assert can_accept_new_chunks(InterviewStatus.PROCESSING) is False
    assert can_accept_new_chunks(InterviewStatus.FINALIZED) is False

    assert can_run_ai_jobs(InterviewStatus.PROCESSING) is True
    assert can_run_ai_jobs(InterviewStatus.REVIEW) is True
    assert can_run_ai_jobs(InterviewStatus.DRAFT) is False
    assert can_run_ai_jobs(InterviewStatus.FINALIZED) is False
