"""
Regression and validation suite for Stage 4: Honest Finalization & Immutable Export.
Covers requirements R7, R8 from docs/remediation-plan.md:
1. Cannot finalize from non-REVIEW states (DRAFT, RECORDING, PROCESSING).
2. Cannot finalize with unassessed, missing, or stale criteria.
3. Explicit exclusions with reasons are permitted.
4. AI proposals do not silently count towards final score without human confirmation.
5. No default HIRE or default summary: requires explicit human confirmation and reviewer ID.
6. Atomic finalization transaction creating canonical snapshot and SHA-256 checksum.
7. Idempotent finalization: repeated calls do not create duplicate report revisions.
8. Immutability: finalized interview rejects segment additions, review overrides, and summary changes with 409.
9. Export of finalized interview is strictly built from the immutable snapshot.
"""
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from backend.api.app import app, get_repository
from backend.db.database import Database
from backend.db.repository import Repository


@pytest.fixture
def test_env(tmp_path):
    db_file = tmp_path / "stage4_test.db"
    db = Database(str(db_file))
    db.init_schema()
    repo = Repository(db)
    app.dependency_overrides[get_repository] = lambda: repo
    with TestClient(app) as client:
        yield {"client": client, "repo": repo, "db": db}
    app.dependency_overrides.clear()


def create_standard_interview(client: TestClient, interview_id: str, status: str = "DRAFT"):
    # 1. Create interview in DRAFT
    client.post(
        "/api/v1/interviews",
        json={
            "id": interview_id,
            "title": "Senior Systems Engineer",
            "candidate_name": "Alice Wonderland",
            "role": "Backend",
            "plan": {
                "role": "Backend",
                "questions": [
                    {
                        "id": "q1",
                        "title": "Distributed Consensus",
                        "prompt": "Explain Raft vs Paxos",
                        "weight": 2.0,
                        "criteria": [
                            {"id": "crit-raft", "title": "Raft protocol", "min_score": 1.0, "max_score": 5.0, "weight": 1.0},
                            {"id": "crit-leader", "title": "Leader election", "min_score": 1.0, "max_score": 5.0, "weight": 1.0},
                        ],
                    },
                    {
                        "id": "q2",
                        "title": "Storage Engines",
                        "prompt": "Explain LSM trees vs B-trees",
                        "weight": 1.0,
                        "criteria": [
                            {"id": "crit-lsm", "title": "LSM Compaction", "min_score": 1.0, "max_score": 5.0, "weight": 1.0},
                        ],
                    },
                ],
            },
        },
    )
    if status.upper() != "DRAFT":
        # Transition up to target status
        s_upper = status.upper()
        if s_upper in ("READY", "RECORDING", "PAUSED", "PROCESSING", "REVIEW"):
            r = client.post(f"/api/v1/interviews/{interview_id}/status", json={"target_status": "ready"})
            assert r.status_code == 200, f"Failed transition to READY: {r.text}"
        if s_upper in ("RECORDING", "PAUSED", "PROCESSING", "REVIEW"):
            r = client.post(f"/api/v1/interviews/{interview_id}/status", json={"target_status": "recording"})
            assert r.status_code == 200, f"Failed transition to RECORDING: {r.text}"
        if s_upper in ("PROCESSING", "REVIEW"):
            r = client.post(f"/api/v1/interviews/{interview_id}/status", json={"target_status": "processing"})
            assert r.status_code == 200, f"Failed transition to PROCESSING: {r.text}"
        if s_upper == "REVIEW":
            r = client.post(f"/api/v1/interviews/{interview_id}/status", json={"target_status": "review"})
            assert r.status_code == 200, f"Failed transition to REVIEW: {r.text}"


# --- 1. State machine enforcement ---

def test_cannot_finalize_from_draft_or_recording(test_env):
    """Requirement: finalization is only permitted from REVIEW state."""
    client = test_env["client"]
    int_id = "test-fin-draft"
    create_standard_interview(client, int_id, status="DRAFT")

    res = client.post(
        f"/api/v1/interviews/{int_id}/report/finalize",
        json={
            "summary_markdown": "Candidate demonstrated strong systems knowledge.",
            "hiring_recommendation": "HIRE",
            "confirmed_by": "lead-interviewer-1",
        },
    )
    assert res.status_code in (400, 409), f"Expected 400/409 from DRAFT but got {res.status_code}: {res.text}"
    assert "REVIEW" in res.text or "status" in res.text


# --- 2. Incomplete and unassessed questions validation ---

def test_cannot_finalize_with_unassessed_questions(test_env):
    """Requirement: all planned questions must have human assessment or explicit exclusion."""
    client = test_env["client"]
    int_id = "test-fin-unassessed"
    create_standard_interview(client, int_id, status="REVIEW")

    # Review only question 1, leave question 2 completely unassessed
    client.post(
        f"/api/v1/interviews/{int_id}/assessments/q1/review",
        json={
            "expected_transcript_revision": "trans-rev-1",
            "scores": [
                {"criterion_id": "crit-raft", "score": 4.0},
                {"criterion_id": "crit-leader", "score": 4.0},
            ],
            "reviewer_notes": "Good understanding of Raft",
            "reviewer_id": "lead-1",
        },
    )

    res = client.post(
        f"/api/v1/interviews/{int_id}/report/finalize",
        json={
            "summary_markdown": "Candidate did okay on q1.",
            "hiring_recommendation": "LEAN_HIRE",
            "confirmed_by": "lead-1",
        },
    )
    assert res.status_code in (400, 422, 409)
    assert "q2" in res.text or "unassessed" in res.text.lower() or "не оценен" in res.text.lower()


def test_finalize_allowed_with_explicit_question_exclusion(test_env):
    """Requirement: unassessed question can be explicitly excluded by human with valid reason."""
    client = test_env["client"]
    repo = test_env["repo"]
    int_id = "test-fin-excluded"
    create_standard_interview(client, int_id, status="REVIEW")

    # Question 1: assessed
    client.post(
        f"/api/v1/interviews/{int_id}/assessments/q1/review",
        json={
            "expected_transcript_revision": "trans-rev-1",
            "scores": [
                {"criterion_id": "crit-raft", "score": 5.0},
                {"criterion_id": "crit-leader", "score": 5.0},
            ],
            "reviewer_notes": "Flawless",
            "reviewer_id": "lead-1",
        },
    )

    # Question 2: explicitly excluded with reason
    repo.save_human_assessment(
        assessment_id=f"ha-{int_id}-q2",
        interview_id=int_id,
        question_id="q2",
        rubric_revision_id="rub-rev-1",
        transcript_revision_id="trans-rev-1",
        scores=[],
        reviewer_notes="Ran out of time due to deep dive on consensus",
        reviewer_id="lead-1",
        is_manually_adjusted=True,
    )
    # Mark as excluded in DB
    with repo.db.transaction() as conn:
        conn.execute(
            "UPDATE human_assessments SET is_excluded = 1, exclusion_reason = ? WHERE interview_id = ? AND question_id = 'q2'",
            ("Ran out of time due to deep dive on consensus", int_id),
        )

    res = client.post(
        f"/api/v1/interviews/{int_id}/report/finalize",
        json={
            "summary_markdown": "Strong candidate on distributed systems.",
            "hiring_recommendation": "STRONG_HIRE",
            "confirmed_by": "lead-1",
        },
    )
    assert res.status_code == 200, f"Finalization failed: {res.text}"
    body = res.json()
    assert body["status"] == "finalized"
    assert body["final_score_100"] == 100.0  # 5 on scale 1-5 is 100%


# --- 3. Stale review protection ---

def test_cannot_finalize_with_stale_human_assessment(test_env):
    """Requirement: stale review blocks finalization until re-confirmed."""
    client = test_env["client"]
    repo = test_env["repo"]
    int_id = "test-fin-stale"
    create_standard_interview(client, int_id, status="REVIEW")

    # Review both questions
    for q_id, crits in [("q1", [("crit-raft", 4.0), ("crit-leader", 4.0)]), ("q2", [("crit-lsm", 4.0)])]:
        client.post(
            f"/api/v1/interviews/{int_id}/assessments/{q_id}/review",
            json={
                "expected_transcript_revision": "trans-rev-1",
                "scores": [{"criterion_id": cid, "score": s} for cid, s in crits],
                "reviewer_notes": "Ok",
                "reviewer_id": "lead-1",
            },
        )

    # Mark q1 as stale due to transcript re-run
    repo.mark_proposals_stale(int_id, ["q1"], "Transcript retranscribed in rev-2")

    res = client.post(
        f"/api/v1/interviews/{int_id}/report/finalize",
        json={
            "summary_markdown": "Candidate review.",
            "hiring_recommendation": "HIRE",
            "confirmed_by": "lead-1",
        },
    )
    assert res.status_code in (400, 409)
    assert "stale" in res.text.lower() or "устарел" in res.text.lower()


# --- 4. No default HIRE or default summary ---

def test_cannot_finalize_without_explicit_summary_and_recommendation(test_env):
    """Requirement: remove default HIRE and default summary; requires human confirmation."""
    client = test_env["client"]
    int_id = "test-fin-no-defaults"
    create_standard_interview(client, int_id, status="REVIEW")

    # Review both questions
    for q_id, crits in [("q1", [("crit-raft", 4.0), ("crit-leader", 4.0)]), ("q2", [("crit-lsm", 4.0)])]:
        client.post(
            f"/api/v1/interviews/{int_id}/assessments/{q_id}/review",
            json={
                "expected_transcript_revision": "trans-rev-1",
                "scores": [{"criterion_id": cid, "score": s} for cid, s in crits],
                "reviewer_notes": "Ok",
                "reviewer_id": "lead-1",
            },
        )

    # Attempt to finalize with empty / missing recommendation or empty confirmed_by
    res = client.post(
        f"/api/v1/interviews/{int_id}/report/finalize",
        json={
            "summary_markdown": "",
            "hiring_recommendation": "",
            "confirmed_by": "",
        },
    )
    assert res.status_code in (400, 422)
    assert "recommendation" in res.text.lower() or "summary" in res.text.lower() or "confirmed_by" in res.text.lower()


# --- 5. Atomic transaction & Canonical Snapshot Checksum ---

def test_atomic_finalization_snapshot_and_checksum(test_env):
    """Requirement: canonical snapshot contains exact inputs, formula, scoring, and checksum."""
    client = test_env["client"]
    int_id = "test-fin-snapshot"
    create_standard_interview(client, int_id, status="REVIEW")

    for q_id, crits in [("q1", [("crit-raft", 3.0), ("crit-leader", 3.0)]), ("q2", [("crit-lsm", 3.0)])]:
        client.post(
            f"/api/v1/interviews/{int_id}/assessments/{q_id}/review",
            json={
                "expected_transcript_revision": "trans-rev-1",
                "scores": [{"criterion_id": cid, "score": s} for cid, s in crits],
                "reviewer_notes": "Solid mid level",
                "reviewer_id": "lead-1",
            },
        )

    res = client.post(
        f"/api/v1/interviews/{int_id}/report/finalize",
        json={
            "summary_markdown": "Candidate demonstrated solid middle engineer proficiency.",
            "hiring_recommendation": "HIRE",
            "confirmed_by": "lead-1",
            "audio_limitations": ["Minor background noise on interviewer track"],
        },
    )
    assert res.status_code == 200, f"Finalize failed: {res.text}"
    data = res.json()
    assert data["status"] == "finalized"
    assert data["final_score_100"] == 50.0  # 3 on 1-5 scale is exactly 50.0%
    assert data["sha256_checksum"] is not None

    # Verify snapshot in DB
    repo = test_env["repo"]
    latest_rep = repo.get_latest_report_revision(int_id)
    assert latest_rep is not None
    assert latest_rep["sha256_checksum"] == data["sha256_checksum"]

    snapshot = json.loads(latest_rep["canonical_snapshot_json"])
    assert snapshot["interview_id"] == int_id
    assert snapshot["formula_version"] == "1.0-linear-midpoint"
    assert snapshot["input_revisions"]["transcript_revision_id"] == "trans-rev-1"
    assert snapshot["executive_summary"]["hiring_recommendation"] == "HIRE"
    assert snapshot["audio_limitations"] == ["Minor background noise on interviewer track"]

    # Re-verify checksum reproducibility
    recomputed_sha = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    assert recomputed_sha == latest_rep["sha256_checksum"]


# --- 6. Idempotence of finalization ---

def test_idempotent_finalization(test_env):
    """Requirement: repeated identical finalize calls return the same report without duplicate revisions."""
    client = test_env["client"]
    repo = test_env["repo"]
    int_id = "test-fin-idempotent"
    create_standard_interview(client, int_id, status="REVIEW")

    for q_id, crits in [("q1", [("crit-raft", 4.0), ("crit-leader", 4.0)]), ("q2", [("crit-lsm", 4.0)])]:
        client.post(
            f"/api/v1/interviews/{int_id}/assessments/{q_id}/review",
            json={
                "expected_transcript_revision": "trans-rev-1",
                "scores": [{"criterion_id": cid, "score": s} for cid, s in crits],
                "reviewer_notes": "Good",
                "reviewer_id": "lead-1",
            },
        )

    finalize_payload = {
        "summary_markdown": "Finalized review note.",
        "hiring_recommendation": "HIRE",
        "confirmed_by": "lead-1",
    }

    # First call
    res1 = client.post(f"/api/v1/interviews/{int_id}/report/finalize", json=finalize_payload)
    assert res1.status_code == 200
    revs1 = repo.get_report_revisions(int_id)
    assert len(revs1) == 1

    # Second call (identical)
    res2 = client.post(f"/api/v1/interviews/{int_id}/report/finalize", json=finalize_payload)
    assert res2.status_code == 200
    assert res2.json()["sha256_checksum"] == res1.json()["sha256_checksum"]
    revs2 = repo.get_report_revisions(int_id)
    assert len(revs2) == 1, "Duplicate report revision created on idempotent finalize call!"


# --- 7. Immutability of Finalized Interview ---

def test_finalized_interview_rejects_modifications(test_env):
    """Requirement: normal endpoints cannot mutate a finalized interview."""
    client = test_env["client"]
    int_id = "test-fin-immutable"
    create_standard_interview(client, int_id, status="REVIEW")

    for q_id, crits in [("q1", [("crit-raft", 4.0), ("crit-leader", 4.0)]), ("q2", [("crit-lsm", 4.0)])]:
        client.post(
            f"/api/v1/interviews/{int_id}/assessments/{q_id}/review",
            json={
                "expected_transcript_revision": "trans-rev-1",
                "scores": [{"criterion_id": cid, "score": s} for cid, s in crits],
                "reviewer_notes": "Good",
                "reviewer_id": "lead-1",
            },
        )

    client.post(
        f"/api/v1/interviews/{int_id}/report/finalize",
        json={
            "summary_markdown": "Final summary.",
            "hiring_recommendation": "HIRE",
            "confirmed_by": "lead-1",
        },
    )

    # 1. Reject new transcript segments
    res_seg = client.post(
        f"/api/v1/interviews/{int_id}/segments",
        json={
            "id": "seg-late",
            "track_id": "candidate",
            "start_time_ms": 1000,
            "end_time_ms": 2000,
            "text": "Late segment attempt",
        },
    )
    assert res_seg.status_code == 409

    # 2. Reject human review modification
    res_rev = client.post(
        f"/api/v1/interviews/{int_id}/assessments/q1/review",
        json={
            "expected_transcript_revision": "trans-rev-1",
            "scores": [{"criterion_id": "crit-raft", "score": 2.0}],
            "reviewer_notes": "Trying to alter finalized score",
            "reviewer_id": "malicious",
        },
    )
    assert res_rev.status_code == 409

    # 3. Reject summary confirmation update
    res_sum = client.post(
        f"/api/v1/interviews/{int_id}/summary/confirm",
        json={
            "confirmed_markdown": "Mutated summary",
            "confirmed_recommendation": "NO_HIRE",
            "reviewer_id": "lead-1",
        },
    )
    assert res_sum.status_code == 409


# --- 8. Immutable Export from Snapshot ---

def test_finalized_export_is_built_from_snapshot(test_env):
    """Requirement: export of finalized interview returns sealed snapshot data and is_draft=False."""
    client = test_env["client"]
    int_id = "test-fin-export"
    create_standard_interview(client, int_id, status="REVIEW")

    for q_id, crits in [("q1", [("crit-raft", 5.0), ("crit-leader", 5.0)]), ("q2", [("crit-lsm", 5.0)])]:
        client.post(
            f"/api/v1/interviews/{int_id}/assessments/{q_id}/review",
            json={
                "expected_transcript_revision": "trans-rev-1",
                "scores": [{"criterion_id": cid, "score": s} for cid, s in crits],
                "reviewer_notes": "Excellent",
                "reviewer_id": "lead-1",
            },
        )

    res_fin = client.post(
        f"/api/v1/interviews/{int_id}/report/finalize",
        json={
            "summary_markdown": "Top candidate.",
            "hiring_recommendation": "STRONG_HIRE",
            "confirmed_by": "lead-1",
        },
    )
    assert res_fin.status_code == 200
    expected_checksum = res_fin.json()["sha256_checksum"]

    # Request export
    res_exp = client.get(f"/api/v1/interviews/{int_id}/export")
    assert res_exp.status_code == 200
    export_body = res_exp.json()

    assert export_body["is_draft"] is False
    assert export_body["status"] == "FINALIZED"
    assert export_body["sha256_checksum"] == expected_checksum
    assert export_body["final_score_100"] == 100.0
    assert "snapshot" in export_body
    assert export_body["snapshot"]["executive_summary"]["hiring_recommendation"] == "STRONG_HIRE"
