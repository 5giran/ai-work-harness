from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from ai_work_harness.decision.canonical import sha256_bytes
from ai_work_harness.decision.guidance import OperatorPlan
from ai_work_harness.decision.operator import GuidedOperator, OperatorCursor
from ai_work_harness.decision.service import DecisionService
from ai_work_harness.errors import HarnessError


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "operator-source.md"
    source.write_text("Both options keep the captured data local.\n", encoding="utf-8")
    return source


def _frame() -> dict:
    return {
        "user_statement_verbatim": "Choose one local option.",
        "ai_initial_interpretation": "Compare two local options.",
        "business_user": "support operator",
        "blocked_decision": "which local option to use",
        "problem_statement": "Choose one local operating option.",
        "scope_in": ["captured local evidence"],
        "scope_out": [],
        "assumptions": [],
        "open_questions": [],
    }


def _candidates() -> dict:
    return {
        "candidates": [
            {
                "candidate_id": candidate_id,
                "title": candidate_id.title(),
                "summary": f"Use {candidate_id}.",
                "proposed_by": "local_operator",
                "benefits": [],
                "drawbacks": [],
                "risks": [],
                "uncertainties": [],
            }
            for candidate_id in ("one", "two")
        ]
    }


def _criteria() -> dict:
    return {
        "criteria": [
            {
                "criterion_id": "privacy",
                "title": "Privacy",
                "definition": "Keep captured data local.",
                "priority": "must",
            }
        ]
    }


def _build_operator_with_final_decision(
    tmp_path: Path,
) -> tuple[DecisionService, GuidedOperator]:
    service = DecisionService(tmp_path, "guided-approval")
    service.initialize()
    operator = GuidedOperator(service)
    source = _source(tmp_path)
    operator.capture_source(source_id="brief", source=source)
    operator.import_frame(_frame())
    operator.confirm("decision-frame")
    operator.import_candidates(_candidates())
    operator.confirm("candidate-set")
    operator.import_criteria(_criteria())
    operator.confirm("criteria-set")
    operator.import_evidence(
        {
            "evidence": [
                {
                    "evidence_id": "local",
                    "claim": "Both options keep the captured data local.",
                    "provenance": "source_observation",
                    "source": {
                        "source_id": "brief",
                        "start_line": 1,
                        "end_line": 1,
                        "excerpt_sha256": sha256_bytes(source.read_bytes()),
                    },
                }
            ]
        }
    )
    operator.import_evaluations(
        {
            "cells": [
                {
                    "candidate_id": candidate_id,
                    "criterion_id": "privacy",
                    "assessment": "meets",
                    "rationale": "The captured observation supports local processing.",
                    "evidence_ids": ["local"],
                    "confidence": "high",
                    "uncertainties": [],
                }
                for candidate_id in ("one", "two")
            ]
        }
    )
    operator.compare()
    operator.import_final_decision(
        {
            "disposition": "select",
            "candidate_id": "one",
            "reason": "One satisfies the reviewed must criterion.",
            "risk_acknowledgements": [],
        }
    )
    return service, operator


def _parent_snapshot_sha(service: DecisionService, snapshot_sha256: str) -> str | None:
    stored = service.store.load_snapshot(snapshot_sha256)
    return stored.snapshot.parent_snapshot_sha256


def test_cursor_advances_only_after_successful_mutation(tmp_path: Path) -> None:
    service = DecisionService(tmp_path, "guided-success")
    initialized = service.initialize()
    operator = GuidedOperator(service)
    initial_cursor = operator.cursor
    source = _source(tmp_path)

    result = operator.capture_source(source_id="brief", source=source)

    assert isinstance(result, OperatorPlan)
    assert operator.cursor.generation == initial_cursor.generation + 1
    assert operator.cursor.pinned_snapshot_sha256 != initial_cursor.pinned_snapshot_sha256
    assert operator.cursor.pinned_snapshot_sha256 == service.status()["snapshot_sha256"]
    assert initial_cursor.pinned_snapshot_sha256 == initialized["snapshot_sha256"]

    before_noop = operator.cursor
    noop_plan = operator.capture_source(source_id="brief", source=source)
    assert isinstance(noop_plan, OperatorPlan)
    assert operator.cursor == before_noop
    assert operator.state.pinned_snapshot_sha256 == before_noop.pinned_snapshot_sha256

    before_cursor = operator.cursor
    before_state = operator.state
    before_plan = operator.plan
    with pytest.raises(HarnessError) as caught:
        operator.import_frame({})

    assert caught.value.code == "INVALID_PAYLOAD"
    assert operator.cursor == before_cursor
    assert operator.state is before_state
    assert operator.plan is before_plan
    assert service.status()["snapshot_sha256"] == before_cursor.pinned_snapshot_sha256

    with pytest.raises(TypeError, match="owns expected_parent"):
        operator.mutate(
            service.capture_source,
            source_id="brief",
            source=source,
            expected_parent=before_cursor.pinned_snapshot_sha256,
        )
    assert operator.cursor == before_cursor


@pytest.mark.parametrize(
    ("generation", "snapshot_sha256"),
    [
        (2, "b" * 64),
        (3, "a" * 64),
        (4, "b" * 64),
    ],
)
def test_cursor_rejects_mixed_or_skipped_result_states(
    generation: int,
    snapshot_sha256: str,
) -> None:
    cursor = OperatorCursor(
        session_id="guided",
        generation=2,
        pinned_snapshot_sha256="a" * 64,
    )

    with pytest.raises(ValueError):
        cursor.advance(
            {
                "ok": True,
                "session_id": "guided",
                "generation": generation,
                "snapshot_sha256": snapshot_sha256,
            }
        )


def test_guided_import_and_semantic_confirm_are_separate_snapshots(tmp_path: Path) -> None:
    service = DecisionService(tmp_path, "guided-confirm")
    service.initialize()
    operator = GuidedOperator(service)
    source = _source(tmp_path)
    operator.capture_source(source_id="brief", source=source)

    import_plan = operator.import_frame(_frame())
    import_cursor = operator.cursor
    frame_sha256 = operator.state.refs["decision_frame"]

    assert import_plan.recommended_action == "confirm_frame"
    confirm_plan = operator.confirm("decision-frame")
    confirm_cursor = operator.cursor

    assert confirm_plan.recommended_action == "import_candidates"
    assert confirm_cursor.generation == import_cursor.generation + 1
    assert confirm_cursor.pinned_snapshot_sha256 != import_cursor.pinned_snapshot_sha256
    assert (
        _parent_snapshot_sha(service, confirm_cursor.pinned_snapshot_sha256)
        == import_cursor.pinned_snapshot_sha256
    )
    assert operator.state.refs["decision_frame"] == frame_sha256
    assert "frame_confirmation" in operator.state.refs
    confirmation = operator.semantic_payload("frame_confirmation")
    assert confirmation is not None
    assert confirmation["subject_sha256"] == frame_sha256
    assert confirmation["method"] == "guided_semantic_review"


def test_guided_challenge_and_commit_use_separate_secret_bound_snapshots(
    tmp_path: Path,
) -> None:
    service, operator = _build_operator_with_final_decision(tmp_path)
    final_cursor = operator.cursor

    challenge_plan = operator.create_approval_challenge(disposition="approved")
    challenge_cursor = operator.cursor
    binding = operator.state.active_challenge

    assert binding is not None
    assert challenge_cursor.generation == final_cursor.generation + 1
    assert (
        _parent_snapshot_sha(service, challenge_cursor.pinned_snapshot_sha256)
        == final_cursor.pinned_snapshot_sha256
    )
    assert operator.semantic_artifact("approval_challenge") is None
    public_plan = json.dumps(operator.public_plan(), sort_keys=True)
    assert binding.nonce not in public_plan
    assert binding.challenge_id not in public_plan
    assert binding.decision_bundle_sha256 not in public_plan
    assert challenge_plan.public_challenge is not None
    assert set(inspect.signature(GuidedOperator.commit_approval).parameters) == {
        "self",
        "reason",
    }

    complete_plan = operator.commit_approval(reason="Reviewed the selected option.")
    approval_cursor = operator.cursor

    assert approval_cursor.generation == challenge_cursor.generation + 1
    assert (
        _parent_snapshot_sha(service, approval_cursor.pinned_snapshot_sha256)
        == challenge_cursor.pinned_snapshot_sha256
    )
    assert complete_plan.ready is True
    assert "human_approval" in operator.state.refs
    assert "approval_challenge" not in operator.state.refs
    assert operator.state.active_challenge is None


def test_guided_outbound_preview_and_exact_consent_keep_full_binding_internal(
    tmp_path: Path,
) -> None:
    service, operator = _build_operator_with_final_decision(tmp_path)
    before = operator.cursor

    preview = operator.preview_agent(operation="recommendation")

    assert operator.cursor == before
    consent_plan = operator.consent_agent(operation="recommendation", preview=preview)
    assert operator.cursor.generation == before.generation + 1
    assert consent_plan.recommended_action == "retry_recommendation"
    consent = operator.semantic_payload("agent_consent")
    assert consent is not None
    assert consent["method"] == "guided_exact_phrase"
    public = json.dumps(operator.public_plan(), sort_keys=True)
    assert consent["subject_sha256"] not in public
    assert consent["outbound_manifest"]["input_sha256"] not in public

    with pytest.raises(HarnessError) as invalid_method:
        service.consent_agent(
            operation="recommendation",
            expected_manifest_sha=str(preview["outbound_manifest_sha256"]),
            expected_parent=operator.cursor.pinned_snapshot_sha256,
            method="guided_semantic_review",
        )
    assert invalid_method.value.code == "INVALID_CONFIRMATION_METHOD"


def test_write_conflict_preserves_pinned_cursor_until_explicit_reload(
    tmp_path: Path,
) -> None:
    service = DecisionService(tmp_path, "guided-conflict")
    service.initialize()
    operator = GuidedOperator(service)
    before_cursor = operator.cursor
    before_state = operator.state
    before_plan = operator.plan
    moved = service.store.commit(
        expected_parent=before_cursor.pinned_snapshot_sha256,
        refs={},
        operation="test.concurrent-writer",
    )

    with pytest.raises(HarnessError) as caught:
        operator.capture_source(source_id="brief", source=_source(tmp_path))

    assert caught.value.code == "WRITE_CONFLICT"
    assert operator.cursor == before_cursor
    assert operator.state is before_state
    assert operator.plan is before_plan
    assert service.store.get_current().snapshot_sha256 == moved.snapshot_sha256

    operator.reload()
    assert operator.cursor.pinned_snapshot_sha256 == moved.snapshot_sha256
    assert operator.cursor.generation == before_cursor.generation + 1
