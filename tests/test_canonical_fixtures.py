from pathlib import Path

from contracts.followups import FollowUpsStateResponse


def test_canonical_followups_state_fixture():
    fixture_path = Path(__file__).parent / "fixtures" / "canonical_followups_state.json"
    assert fixture_path.exists(), "Canonical followups fixture must exist"
    data = fixture_path.read_text(encoding="utf-8")
    resp = FollowUpsStateResponse.model_validate_json(data)
    assert resp.interview_id == "int-test-1"
    assert len(resp.suggestions) == 1
    sug = resp.suggestions[0]
    assert sug.question_text == "Как именно вы обеспечиваете идемпотентность компенсирующих транзакций?"
    assert sug.purpose == "Проверить понимание идемпотентности при сбоях сети"
    assert len(sug.source_refs) == 1
    assert sug.source_refs[0].segment_id == "seg-101"
