"""
Interviewer question marks: explicit "I am asking question N now" boundaries on the capture
timeline. They must win over keyword matching, feed evaluation and follow-ups, and be exposed
to the live screen together with a non-binding switch hint.
"""
import pytest
from fastapi.testclient import TestClient

from backend.api.app import app, get_repository
from backend.core.matcher import MARK_LEAD_MS, QuestionMatcher
from backend.db.database import Database
from backend.db.repository import Repository

QUESTIONS = [
    {
        "id": "q1",
        "title": "Индексы",
        "prompt": "Как устроены индексы в PostgreSQL и когда B-tree не помогает?",
        "criteria": [{"id": "c1", "title": "Индексы", "description": "B-tree, составные индексы", "min_score": 1, "max_score": 5, "weight": 1.0}],
    },
    {
        "id": "q2",
        "title": "Транзакции",
        "prompt": "Какие уровни изоляции транзакций вы знаете?",
        "criteria": [{"id": "c2", "title": "Изоляция", "description": "Уровни изоляции транзакций, аномалии", "min_score": 1, "max_score": 5, "weight": 1.0}],
    },
    {
        "id": "q3",
        "title": "Очереди",
        "prompt": "Как вы проектируете обработку сообщений из очереди Kafka?",
        "criteria": [{"id": "c3", "title": "Очереди", "description": "Kafka, идемпотентность, ретраи", "min_score": 1, "max_score": 5, "weight": 1.0}],
    },
]


def _seg(seg_id, track, start, end, text, role=None):
    return {
        "id": seg_id,
        "track_id": track,
        "speaker_role": role or track,
        "start_time_ms": start,
        "end_time_ms": end,
        "text": text,
    }


# ---------------------------------------------------------------------------
# Matcher
# ---------------------------------------------------------------------------

def test_marks_override_keyword_matching():
    """Question asked in the interviewer's own words still belongs to the marked question."""
    segments = [
        _seg("i1", "interviewer", 0, 3000, "Как устроены индексы в PostgreSQL?"),
        _seg("c1", "candidate", 3500, 9000, "B-tree индекс хранит ключи упорядоченно"),
        # Paraphrased question 2: shares no keywords with the plan text.
        _seg("i2", "interviewer", 20000, 23000, "Давайте поговорим, что будет если два клиента одновременно пишут"),
        _seg("c2", "candidate", 24000, 30000, "Будет гонка, поможет serializable"),
    ]
    marks = [{"question_id": "q1", "at_ms": 0}, {"question_id": "q2", "at_ms": 20000}]

    without_marks = {a.segment_id: a.question_id for a in QuestionMatcher().associate_segments(QUESTIONS, segments)}
    assert without_marks["c2"] == "q1"  # keyword matching alone keeps the old question

    result = {a.segment_id: a for a in QuestionMatcher().associate_segments(QUESTIONS, segments, question_marks=marks)}
    assert result["i2"].question_id == "q2"
    assert result["c2"].question_id == "q2"
    assert result["c2"].is_ambiguous is False
    assert result["c1"].question_id == "q1"


def test_interviewer_speech_just_before_mark_belongs_to_new_question():
    segments = [
        _seg("c1", "candidate", 0, 9000, "B-tree индекс хранит ключи"),
        _seg("i2", "interviewer", 17000, 19500, "Следующий вопрос про изоляцию"),
        _seg("c-late", "candidate", 18000, 19000, "да, и ещё hash-индексы"),
    ]
    marks = [{"question_id": "q1", "at_ms": 0}, {"question_id": "q2", "at_ms": 17000 + MARK_LEAD_MS - 1000}]
    result = {a.segment_id: a.question_id for a in QuestionMatcher().associate_segments(QUESTIONS, segments, question_marks=marks)}
    assert result["i2"] == "q2"
    # Candidate speech that started before the mark still finishes the previous answer.
    assert result["c-late"] == "q1"


def test_manual_association_beats_marks():
    segments = [_seg("c2", "candidate", 24000, 30000, "ответ")]
    marks = [{"question_id": "q2", "at_ms": 20000}]
    existing = [{"segment_id": "c2", "question_id": "q3", "is_manually_adjusted": 1, "confidence": 1.0}]
    result = QuestionMatcher().associate_segments(QUESTIONS, segments, existing_associations=existing, question_marks=marks)
    assert result[0].question_id == "q3"
    assert result[0].is_manually_adjusted is True


def test_marks_for_unknown_questions_are_ignored():
    segments = [_seg("c1", "candidate", 1000, 2000, "B-tree индекс")]
    result = QuestionMatcher().associate_segments(QUESTIONS, segments, question_marks=[{"question_id": "q-missing", "at_ms": 0}])
    assert result[0].question_id == "q1"


def test_switch_hint_suggests_unmarked_question():
    segments = [
        _seg("i1", "interviewer", 0, 3000, "Как устроены индексы в PostgreSQL?"),
        _seg("i3", "interviewer", 40000, 44000, "Как вы проектируете обработку сообщений из очереди Kafka?"),
    ]
    hint = QuestionMatcher().suggest_question_switch(QUESTIONS, segments, [{"question_id": "q1", "at_ms": 0}])
    assert hint is not None
    assert hint.question_id == "q3"
    assert hint.segment_id == "i3"


def test_switch_hint_silent_when_on_topic_or_without_marks():
    segments = [_seg("i1", "interviewer", 0, 3000, "Как устроены индексы в PostgreSQL?")]
    matcher = QuestionMatcher()
    assert matcher.suggest_question_switch(QUESTIONS, segments, [{"question_id": "q1", "at_ms": 0}]) is None
    assert matcher.suggest_question_switch(QUESTIONS, segments, []) is None


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    db = Database(str(tmp_path / "question_marks.db"))
    db.init_schema()
    repo = Repository(db)
    app.dependency_overrides[get_repository] = lambda: repo
    with TestClient(app) as test_client:
        yield test_client, repo
    app.dependency_overrides.clear()


def _start_recording(client: TestClient, interview_id: str = "inv-marks") -> str:
    res = client.post(
        "/api/v1/interviews",
        json={
            "id": interview_id,
            "title": "Backend",
            "candidate_name": "Анна",
            "role": "Backend Middle",
            "plan": {"title": "Backend", "questions": QUESTIONS},
        },
    )
    assert res.status_code == 200
    assert client.post(
        f"/api/v1/interviews/{interview_id}/status",
        json={"current_status": "draft", "target_status": "ready"},
    ).status_code == 200
    assert client.post(
        f"/api/v1/interviews/{interview_id}/status",
        json={
            "current_status": "ready",
            "target_status": "recording",
            "consent_confirmed_at": "2026-09-28T00:00:00Z",
            "consent_version": "consent-v1",
        },
    ).status_code == 200
    return interview_id


def test_question_mark_endpoint_and_live_state(client):
    test_client, repo = client
    inv = _start_recording(test_client)

    res = test_client.post(f"/api/v1/interviews/{inv}/question-marks", json={"question_id": "q1", "at_ms": 0})
    assert res.status_code == 200
    assert res.json()["created"] is True

    # Marking the same current question again is idempotent.
    again = test_client.post(f"/api/v1/interviews/{inv}/question-marks", json={"question_id": "q1", "at_ms": 500})
    assert again.json()["created"] is False

    repo.add_transcript_segment(
        segment_id="i2", interview_id=inv, track_id="interviewer", start_time_ms=20000, end_time_ms=22000,
        text="Что будет, если два клиента одновременно пишут одну строку?", is_final=True, speaker_role="interviewer",
    )
    repo.add_transcript_segment(
        segment_id="c2", interview_id=inv, track_id="candidate", start_time_ms=23000, end_time_ms=28000,
        text="Будет гонка", is_final=True, speaker_role="candidate",
    )
    res = test_client.post(f"/api/v1/interviews/{inv}/question-marks", json={"question_id": "q2", "at_ms": 20000})
    assert res.status_code == 200

    state = test_client.get(f"/api/v1/interviews/{inv}/live-state").json()
    assert state["current_question_id"] == "q2"
    assert [m["question_id"] for m in state["question_marks"]] == ["q1", "q2"]
    assert state["segment_questions"]["c2"]["question_id"] == "q2"
    assert len(state["transcript_segments"]) == 2
    assert "scoring" not in state

    full = test_client.get(f"/api/v1/interviews/{inv}").json()
    assert len(full["question_marks"]) == 2


def test_question_mark_validation(client):
    test_client, _repo = client
    inv = _start_recording(test_client)
    assert test_client.post(f"/api/v1/interviews/{inv}/question-marks", json={"question_id": "nope", "at_ms": 0}).status_code == 400
    assert test_client.post(f"/api/v1/interviews/{inv}/question-marks", json={"question_id": "q1", "at_ms": -5}).status_code == 422
    assert test_client.post("/api/v1/interviews/missing/question-marks", json={"question_id": "q1", "at_ms": 0}).status_code == 404


def test_question_mark_rejected_outside_recording(client):
    test_client, _repo = client
    res = test_client.post(
        "/api/v1/interviews",
        json={"id": "inv-draft", "title": "x", "candidate_name": "x", "role": "x", "plan": {"title": "x", "questions": QUESTIONS}},
    )
    assert res.status_code == 200
    assert test_client.post("/api/v1/interviews/inv-draft/question-marks", json={"question_id": "q1", "at_ms": 0}).status_code == 409


def test_refresh_associations_picks_up_late_answer_segments(client):
    """Re-evaluating a question must see answer segments that arrived after the first evaluation."""
    test_client, repo = client
    inv = _start_recording(test_client)
    repo.add_question_mark(inv, "q1", 0)
    repo.add_transcript_segment(
        segment_id="c1", interview_id=inv, track_id="candidate", start_time_ms=1000, end_time_ms=4000,
        text="B-tree индекс", is_final=True, speaker_role="candidate",
    )
    first = repo.refresh_associations(inv, QUESTIONS)
    assert {a["segment_id"] for a in first if a["question_id"] == "q1"} == {"c1"}

    repo.add_transcript_segment(
        segment_id="c1b", interview_id=inv, track_id="candidate", start_time_ms=5000, end_time_ms=9000,
        text="и составные индексы", is_final=True, speaker_role="candidate",
    )
    second = repo.refresh_associations(inv, QUESTIONS)
    assert {a["segment_id"] for a in second if a["question_id"] == "q1"} == {"c1", "c1b"}
