from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from test_decision_service import _build_approved_demo, _service

from ai_work_harness.decision.models import ArtifactEnvelope
from ai_work_harness.decision.service import DOWNSTREAM, DecisionService
from ai_work_harness.errors import HarnessError


@pytest.fixture(scope="module")
def approved_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("semantic-verification")
    source = root / "source.md"
    source.write_text("Synthetic triage evidence for every configured fixture cell.\n")
    _build_approved_demo(_service(root), source)
    return root


@pytest.fixture
def service(approved_project: Path, tmp_path: Path) -> DecisionService:
    shutil.copytree(approved_project, tmp_path, dirs_exist_ok=True)
    return _service(tmp_path)


def _replace_artifact(
    service: DecisionService,
    ref: str,
    change: Callable[[dict[str, Any]], None],
) -> str:
    current = service.store.get_current()
    refs = dict(current.refs)
    document = service.store.read_artifact(refs[ref]).to_document()
    change(document)
    refs[ref] = service.store.put_artifact(ArtifactEnvelope.from_document(document))
    for downstream in DOWNSTREAM.get(ref, set()):
        refs.pop(downstream, None)
    return service.store.commit(
        expected_parent=current.sha256, refs=refs, operation="test.invalid-domain-state"
    ).sha256


def _assert_rejected(service: DecisionService, snapshot: str, reason_code: str) -> None:
    before = service.store.paths.current_pointer.read_bytes()
    with pytest.raises(HarnessError) as caught:
        service.verify(snapshot)
    assert caught.value.code == "WORKFLOW_INTEGRITY_FAILED"
    assert caught.value.exit_code == 5
    assert caught.value.details["reason_code"] == reason_code
    status = service.status()
    assert status["verified"] is False
    assert status["ready"] is False
    for read in (
        lambda: service.read_operator_state(snapshot),
        lambda: service.export_view(service.project_root / "invalid-export.json"),
    ):
        with pytest.raises(HarnessError, match="workflow"):
            read()
    with pytest.raises(HarnessError) as approval:
        service.create_approval_challenge(disposition="approved", expected_parent=snapshot)
    assert approval.value.code == "WORKFLOW_INTEGRITY_FAILED"
    assert service.store.paths.current_pointer.read_bytes() == before
    assert not (service.project_root / "invalid-export.json").exists()


@pytest.mark.parametrize(
    ("changes", "reason_code"),
    [
        ({"candidate_id": "llm-assisted"}, "INELIGIBLE_FINAL_DECISION"),
        ({"risk_acknowledgements": []}, "RISK_ACKNOWLEDGEMENT_REQUIRED"),
        ({"recommendation_relation": "same"}, "RECOMMENDATION_RELATION_MISMATCH"),
    ],
)
def test_invalid_final_decision_cannot_be_verified_or_approved(
    service: DecisionService, changes: dict[str, Any], reason_code: str
) -> None:
    snapshot = _replace_artifact(
        service, "final_decision", lambda document: document["payload"].update(changes)
    )
    _assert_rejected(service, snapshot, reason_code)


@pytest.mark.parametrize(
    ("ref", "change", "reason_code"),
    [
        (
            "candidate_set",
            lambda doc: doc["parents"].pop("frame_confirmation"),
            "WORKFLOW_DEPENDENCY_MISSING",
        ),
        (
            "criteria_set",
            lambda doc: doc["payload"]["criteria"].append(doc["payload"]["criteria"][0]),
            "DUPLICATE_ID",
        ),
        (
            "evidence_set",
            lambda doc: doc["payload"]["evidence"][0]["source"].update(excerpt_sha256="f" * 64),
            "EVIDENCE_LOCATOR_STALE",
        ),
        (
            "evaluation_set",
            lambda doc: doc["payload"]["cells"].pop(),
            "INCOMPLETE_EVALUATION_MATRIX",
        ),
        (
            "evaluation_set",
            lambda doc: doc["payload"]["cells"][0].update(evidence_ids=["unknown"]),
            "UNKNOWN_REFERENCE",
        ),
        (
            "evaluation_set",
            lambda doc: doc["producer"].update(kind="unrecognized-agent"),
            "INVALID_PRODUCER",
        ),
        (
            "evaluation_review_set",
            lambda doc: doc["payload"]["reviews"].pop(),
            "REVIEW_REQUIRED",
        ),
        (
            "comparison",
            lambda doc: doc["payload"]["eligible_candidate_ids"].append("llm-assisted"),
            "COMPARISON_MISMATCH",
        ),
        (
            "comparison",
            lambda doc: doc["parents"].pop("evaluation_review_set"),
            "WORKFLOW_DEPENDENCY_MISSING",
        ),
        (
            "recommendation",
            lambda doc: doc["payload"].update(candidate_id="llm-assisted"),
            "INELIGIBLE_RECOMMENDATION",
        ),
        (
            "recommendation",
            lambda doc: doc["payload"].update(evidence_ids=["unknown"]),
            "UNKNOWN_REFERENCE",
        ),
    ],
)
def test_schema_valid_domain_errors_are_rejected_at_the_reached_stage(
    service: DecisionService,
    ref: str,
    change: Callable[[dict[str, Any]], None],
    reason_code: str,
) -> None:
    _assert_rejected(service, _replace_artifact(service, ref, change), reason_code)


def test_incomplete_frame_can_be_a_draft_but_cannot_be_confirmed(
    service: DecisionService,
) -> None:
    current = service.store.get_current()
    confirmation = service.store.read_artifact(current.refs["frame_confirmation"]).to_document()
    snapshot = _replace_artifact(
        service, "decision_frame", lambda doc: doc["payload"].update(problem_statement=None)
    )
    assert service.verify(snapshot)["verified"] is True
    refs = dict(service.store.get_current().refs)
    confirmation["parents"]["decision_frame"] = refs["decision_frame"]
    confirmation["payload"]["subject_sha256"] = refs["decision_frame"]
    refs["frame_confirmation"] = service.store.put_artifact(confirmation)
    confirmed = service.store.commit(expected_parent=snapshot, refs=refs)
    _assert_rejected(service, confirmed.sha256, "FRAME_INCOMPLETE")


def test_comparison_cannot_omit_required_reviews(service: DecisionService) -> None:
    snapshot = _replace_artifact(
        service, "comparison", lambda doc: doc["parents"].pop("evaluation_review_set")
    )
    refs = dict(service.store.get_current().refs)
    refs.pop("evaluation_review_set")
    without_reviews = service.store.commit(expected_parent=snapshot, refs=refs)
    _assert_rejected(service, without_reviews.sha256, "REVIEW_REQUIRED")


def test_revision_request_is_valid_progress_but_blocks_a_stored_comparison(
    service: DecisionService,
) -> None:
    current = service.store.get_current()
    comparison = service.store.read_artifact(current.refs["comparison"]).to_document()
    pending = _replace_artifact(
        service,
        "evaluation_review_set",
        lambda doc: doc["payload"]["reviews"][0].update(outcome="request_revision"),
    )
    assert service.verify(pending)["verified"] is True
    refs = dict(service.store.get_current().refs)
    comparison["parents"]["evaluation_review_set"] = refs["evaluation_review_set"]
    refs["comparison"] = service.store.put_artifact(comparison)
    compared = service.store.commit(expected_parent=pending, refs=refs)
    _assert_rejected(service, compared.sha256, "EVALUATION_REVISION_REQUIRED")


def test_bundle_must_cover_all_current_inputs(service: DecisionService) -> None:
    current = service.store.get_current()
    refs = dict(current.refs)
    bundle = service.store.read_artifact(refs["decision_bundle"]).to_document()
    bundle["payload"]["refs"].pop("evidence_set")
    refs["decision_bundle"] = service.store.put_artifact(bundle)
    refs.pop("human_approval")
    changed = service.store.commit(expected_parent=current.sha256, refs=refs)
    _assert_rejected(service, changed.sha256, "DECISION_BUNDLE_MISMATCH")


def test_each_valid_stage_and_approved_snapshot_remains_verifiable_without_writes(
    service: DecisionService,
) -> None:
    before = service.store.paths.current_pointer.read_bytes()
    files_before = sorted(service.store.paths.v2_root.rglob("*"))
    snapshot = service.store.get_current()
    assert service.status()["ready"] is True
    while True:
        assert service.verify(snapshot.sha256)["verified"] is True
        parent = snapshot.parent_snapshot_sha256
        if parent is None:
            break
        snapshot = service.store.load_snapshot(parent)
    assert service.store.paths.current_pointer.read_bytes() == before
    assert sorted(service.store.paths.v2_root.rglob("*")) == files_before


def test_invalid_choice_is_blocked_at_commit_and_never_reported_ready(
    service: DecisionService,
) -> None:
    current = service.store.get_current()
    approval = service.store.read_artifact(current.refs["human_approval"]).to_document()
    challenge = service.store.read_artifact(approval["payload"]["challenge_sha256"]).to_document()
    bundle = service.store.read_artifact(current.refs["decision_bundle"]).to_document()
    bad_final = _replace_artifact(
        service, "final_decision", lambda doc: doc["payload"].update(candidate_id="llm-assisted")
    )
    refs = dict(service.store.get_current().refs)
    bundle["parents"] = dict(refs)
    bundle["payload"]["refs"] = dict(refs)
    refs["decision_bundle"] = service.store.put_artifact(bundle)
    challenge["parents"] = {
        "decision_bundle": refs["decision_bundle"],
        "final_decision": refs["final_decision"],
    }
    challenge["payload"].update(
        decision_bundle_sha256=refs["decision_bundle"], parent_snapshot_sha256=bad_final
    )
    refs["approval_challenge"] = service.store.put_artifact(challenge)
    challenged = service.store.commit(expected_parent=bad_final, refs=refs)
    before = service.store.paths.current_pointer.read_bytes()
    with pytest.raises(HarnessError) as commit:
        service.commit_approval(
            challenge_id=challenge["payload"]["challenge_id"],
            nonce=challenge["payload"]["nonce"],
            expected_bundle_sha=refs["decision_bundle"],
            reason="Synthetic invalid choice must be refused.",
            expected_parent=challenged.sha256,
        )
    assert commit.value.code == "WORKFLOW_INTEGRITY_FAILED"
    assert service.store.paths.current_pointer.read_bytes() == before

    # Simulate an already stored approval from an older writer. Hash, schema,
    # parent and challenge bindings remain valid; decision policy alone is wrong.
    approval["parents"] = {
        "approval_challenge": refs["approval_challenge"],
        "decision_bundle": refs["decision_bundle"],
        "final_decision": refs["final_decision"],
    }
    approval["payload"].update(
        challenge_sha256=refs["approval_challenge"],
        decision_bundle_sha256=refs["decision_bundle"],
    )
    refs["human_approval"] = service.store.put_artifact(approval)
    refs.pop("approval_challenge")
    approved = service.store.commit(expected_parent=challenged.sha256, refs=refs)
    assert service.store.verify(approved.sha256).ok is True
    status = service.status()
    assert status["verified"] is False
    assert status["ready"] is False
    assert status["decision_complete"] is False
    assert status["verification_error"]["details"]["reason_code"] == "INELIGIBLE_FINAL_DECISION"
