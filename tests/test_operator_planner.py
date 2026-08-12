from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from ai_work_harness.decision.guidance import (
    OPERATOR_PLAN_SCHEMA_VERSION,
    ApprovalChallengeBinding,
    OperatorAction,
    OperatorStage,
    PendingReviewStatus,
    ValidatedOperatorState,
    plan_operator_next,
)

OBSERVED_AT = datetime(2026, 8, 12, 3, 0, tzinfo=UTC)
SNAPSHOT_SHA = "a" * 64


def _digest(label: str) -> str:
    return sha256(label.encode()).hexdigest()


def _artifact(
    ref_name: str,
    payload: dict | None = None,
    *,
    producer: str = "local_operator",
) -> dict:
    return {
        "artifact_type": ref_name.replace("_", "-"),
        "producer": {"kind": producer},
        "payload": payload or {},
    }


def _base_artifacts() -> dict[str, dict]:
    candidates = [
        {"candidate_id": "alpha", "title": "Alpha"},
        {"candidate_id": "zeta", "title": "Zeta"},
    ]
    criteria = [
        {"criterion_id": "privacy", "title": "Privacy", "priority": "must"},
        {"criterion_id": "quality", "title": "Quality", "priority": "high"},
    ]
    cells = [
        {
            "candidate_id": candidate_id,
            "criterion_id": criterion_id,
            "assessment": "meets",
            "rationale": f"{candidate_id}/{criterion_id} rationale",
            "evidence_ids": [f"ev-{candidate_id}-{criterion_id}"],
            "confidence": "medium",
            "uncertainties": [],
        }
        for candidate_id in ("zeta", "alpha")
        for criterion_id in ("quality", "privacy")
    ]
    return {
        "source_manifest": _artifact(
            "source_manifest",
            {
                "sources": [
                    {
                        "source_id": "brief",
                        "media_type": "text/markdown",
                        "bytes": 42,
                        "blob_sha256": "b" * 64,
                        "raw_source": "must never be public",
                    }
                ]
            },
        ),
        "decision_frame": _artifact("decision_frame"),
        "frame_confirmation": _artifact("frame_confirmation", producer="core"),
        "candidate_set": _artifact("candidate_set", {"candidates": candidates}),
        "candidate_confirmation": _artifact("candidate_confirmation", producer="core"),
        "criteria_set": _artifact("criteria_set", {"criteria": criteria}),
        "criteria_confirmation": _artifact("criteria_confirmation", producer="core"),
        "evidence_set": _artifact("evidence_set", {"evidence": []}),
        "evaluation_set": _artifact(
            "evaluation_set",
            {"cells": cells},
            producer="local_operator",
        ),
        "evaluation_review_set": _artifact(
            "evaluation_review_set",
            {"reviews": []},
        ),
        "comparison": _artifact(
            "comparison",
            {
                "eligible_candidate_ids": ["alpha", "zeta"],
                "matrix": [],
            },
            producer="core",
        ),
        "recommendation": _artifact("recommendation", producer="fixture"),
        "final_decision": _artifact("final_decision"),
        "decision_bundle": _artifact("decision_bundle", producer="core"),
        "approval_challenge": _artifact("approval_challenge", producer="core"),
        "human_approval": _artifact("human_approval", producer="core"),
        "agent_consent": _artifact("agent_consent", producer="core"),
    }


def _state(
    ref_names: tuple[str, ...],
    *,
    artifacts: dict[str, dict] | None = None,
    challenge: ApprovalChallengeBinding | None = None,
    outbound_operation: str | None = None,
    decision_complete: bool = False,
    ready: bool = False,
) -> ValidatedOperatorState:
    available = artifacts or _base_artifacts()
    return ValidatedOperatorState(
        session_id="triage",
        generation=len(ref_names),
        pinned_snapshot_sha256=SNAPSHOT_SHA,
        lifecycle_state="test_state",
        refs={name: _digest(name) for name in ref_names},
        artifacts={name: available[name] for name in ref_names},
        active_challenge=challenge,
        outbound_consent_operation=outbound_operation,
        decision_complete=decision_complete,
        ready=ready,
        stale_reasons=("source_changed",),
    )


@pytest.mark.parametrize(
    ("refs", "stage", "recommended", "available"),
    [
        ((), "source", "capture_source", ["capture_source"]),
        (
            ("source_manifest",),
            "frame",
            "import_frame",
            ["import_frame", "capture_source"],
        ),
        (
            ("source_manifest", "decision_frame"),
            "frame",
            "confirm_frame",
            ["confirm_frame", "import_frame"],
        ),
        (
            ("source_manifest", "decision_frame", "frame_confirmation"),
            "candidates",
            "import_candidates",
            ["import_candidates"],
        ),
        (
            (
                "source_manifest",
                "decision_frame",
                "frame_confirmation",
                "candidate_set",
            ),
            "candidates",
            "confirm_candidates",
            ["confirm_candidates", "import_candidates"],
        ),
        (
            (
                "source_manifest",
                "decision_frame",
                "frame_confirmation",
                "candidate_set",
                "candidate_confirmation",
            ),
            "criteria",
            "import_criteria",
            ["import_criteria"],
        ),
        (
            (
                "source_manifest",
                "decision_frame",
                "frame_confirmation",
                "candidate_set",
                "candidate_confirmation",
                "criteria_set",
            ),
            "criteria",
            "confirm_criteria",
            ["confirm_criteria", "import_criteria"],
        ),
        (
            (
                "source_manifest",
                "decision_frame",
                "frame_confirmation",
                "candidate_set",
                "candidate_confirmation",
                "criteria_set",
                "criteria_confirmation",
            ),
            "evidence",
            "import_evidence",
            ["import_evidence"],
        ),
    ],
)
def test_planner_progression_table(
    refs: tuple[str, ...],
    stage: str,
    recommended: str,
    available: list[str],
) -> None:
    public = plan_operator_next(_state(refs), observed_at=OBSERVED_AT).to_public_dict()

    assert public["stage"] == stage
    assert public["recommended_action"] == recommended
    assert public["available_actions"] == available


def test_evaluation_comparison_recommendation_final_and_terminal_priority() -> None:
    prefix = (
        "source_manifest",
        "decision_frame",
        "frame_confirmation",
        "candidate_set",
        "candidate_confirmation",
        "criteria_set",
        "criteria_confirmation",
        "evidence_set",
    )
    cases = [
        (prefix, OperatorStage.EVALUATION, OperatorAction.IMPORT_EVALUATIONS),
        (
            (*prefix, "evaluation_set"),
            OperatorStage.COMPARISON,
            OperatorAction.DERIVE_COMPARISON,
        ),
        (
            (*prefix, "evaluation_set", "comparison"),
            OperatorStage.RECOMMENDATION,
            OperatorAction.GENERATE_RECOMMENDATION,
        ),
        (
            (*prefix, "evaluation_set", "comparison", "recommendation"),
            OperatorStage.FINAL_DECISION,
            OperatorAction.RECORD_FINAL_DECISION,
        ),
        (
            (*prefix, "evaluation_set", "comparison", "recommendation", "final_decision"),
            OperatorStage.APPROVAL,
            OperatorAction.CREATE_APPROVAL_CHALLENGE,
        ),
        (
            (
                *prefix,
                "evaluation_set",
                "comparison",
                "recommendation",
                "final_decision",
                "human_approval",
            ),
            OperatorStage.COMPLETE,
            OperatorAction.EXPORT_DECISION,
        ),
    ]

    for refs, stage, action in cases:
        plan = plan_operator_next(_state(refs), observed_at=OBSERVED_AT)
        assert (plan.stage, plan.recommended_action) == (stage, action)

    comparison_plan = plan_operator_next(
        _state((*prefix, "evaluation_set", "comparison")), observed_at=OBSERVED_AT
    )
    assert comparison_plan.available_actions == (
        OperatorAction.GENERATE_RECOMMENDATION,
        OperatorAction.IMPORT_RECOMMENDATION,
        OperatorAction.RECORD_FINAL_DECISION,
    )


def test_active_outbound_consent_prioritizes_retry_then_local_progression() -> None:
    evidence_refs = (
        "source_manifest",
        "candidate_set",
        "criteria_set",
        "evidence_set",
        "agent_consent",
    )
    evaluation = plan_operator_next(
        _state(evidence_refs, outbound_operation="evaluations"),
        observed_at=OBSERVED_AT,
    )
    assert evaluation.available_actions == (
        OperatorAction.RETRY_EVALUATIONS,
        OperatorAction.IMPORT_EVALUATIONS,
    )

    recommendation = plan_operator_next(
        _state(
            (*evidence_refs, "evaluation_set", "comparison"),
            outbound_operation="recommendation",
        ),
        observed_at=OBSERVED_AT,
    )
    assert recommendation.available_actions == (
        OperatorAction.RETRY_RECOMMENDATION,
        OperatorAction.IMPORT_RECOMMENDATION,
        OperatorAction.RECORD_FINAL_DECISION,
    )

    after_final = plan_operator_next(
        _state(
            (*evidence_refs, "evaluation_set", "comparison", "final_decision"),
            outbound_operation="recommendation",
        ),
        observed_at=OBSERVED_AT,
    )
    assert after_final.stage is OperatorStage.RECOMMENDATION
    assert after_final.recommended_action is OperatorAction.RETRY_RECOMMENDATION


def test_pending_reviews_are_stable_and_revision_stops_comparison() -> None:
    artifacts = _base_artifacts()
    artifacts["evaluation_set"] = {
        **artifacts["evaluation_set"],
        "producer": {"kind": "fixture"},
    }
    refs = ("candidate_set", "criteria_set", "evaluation_set")
    pending = plan_operator_next(
        _state(refs, artifacts=artifacts),
        observed_at=OBSERVED_AT,
    )
    assert pending.stage is OperatorStage.REVIEW
    assert [item.cell_id for item in pending.pending_reviews] == [
        "alpha/privacy",
        "alpha/quality",
        "zeta/privacy",
        "zeta/quality",
    ]
    assert {item.status for item in pending.pending_reviews} == {PendingReviewStatus.PENDING}

    artifacts["evaluation_review_set"] = _artifact(
        "evaluation_review_set",
        {
            "reviews": [
                {
                    "candidate_id": "zeta",
                    "criterion_id": "quality",
                    "outcome": "request_revision",
                    "reason": "The evidence is stale.",
                }
            ]
        },
    )
    revised = plan_operator_next(
        _state((*refs, "evaluation_review_set"), artifacts=artifacts),
        observed_at=OBSERVED_AT,
    )
    assert revised.recommended_action is OperatorAction.REVISE_EVALUATIONS
    assert revised.pending_reviews[-1].cell_id == "zeta/quality"
    assert revised.pending_reviews[-1].status is PendingReviewStatus.REVISION_REQUIRED


def _challenge() -> ApprovalChallengeBinding:
    return ApprovalChallengeBinding(
        challenge_id="00000000-0000-4000-8000-000000000001",
        nonce="nonce-must-stay-internal",
        decision_bundle_sha256="d" * 64,
        challenge_sha256="e" * 64,
        challenge_snapshot_sha256=SNAPSHOT_SHA,
        parent_snapshot_sha256="f" * 64,
        proposed_disposition="approved",
        issued_at=OBSERVED_AT,
        expires_at=OBSERVED_AT + timedelta(minutes=10),
    )


@pytest.mark.parametrize(
    ("offset", "status", "action"),
    [
        (timedelta(minutes=10, microseconds=-1), "active", "commit_approval"),
        (timedelta(minutes=10), "expired", "reissue_approval_challenge"),
        (timedelta(minutes=11), "expired", "reissue_approval_challenge"),
    ],
)
def test_challenge_boundary_is_explicit_and_public_value_is_sanitized(
    offset: timedelta,
    status: str,
    action: str,
) -> None:
    challenge = _challenge()
    refs = ("source_manifest", "final_decision", "decision_bundle", "approval_challenge")
    plan = plan_operator_next(
        _state(refs, challenge=challenge),
        observed_at=OBSERVED_AT + offset,
    )
    public = plan.to_public_dict()

    assert public["challenge"]["status"] == status
    assert public["recommended_action"] == action
    serialized = json.dumps(public, sort_keys=True)
    for secret in (
        challenge.challenge_id,
        challenge.nonce,
        challenge.decision_bundle_sha256,
        challenge.challenge_sha256,
        challenge.challenge_snapshot_sha256,
        challenge.parent_snapshot_sha256,
        "must never be public",
    ):
        assert secret not in serialized
    assert plan.challenge_binding is challenge


def test_public_contract_and_nested_inputs_are_immutable() -> None:
    artifacts = _base_artifacts()
    state = _state(("source_manifest",), artifacts=artifacts)
    plan = plan_operator_next(state, observed_at=OBSERVED_AT)
    public = plan.to_public_dict()

    assert set(public) == {
        "schema_version",
        "observed_at",
        "stage",
        "recommended_action",
        "available_actions",
        "summary",
        "pending_reviews",
        "final_decision_constraints",
        "challenge",
        "decision_complete",
        "ready",
        "stale_reasons",
    }
    assert public["schema_version"] == OPERATOR_PLAN_SCHEMA_VERSION
    assert "session_id" not in public
    assert "pinned_snapshot_sha256" not in public
    assert public["summary"]["sources"] == [
        {"source_id": "brief", "media_type": "text/markdown", "bytes": 42}
    ]
    with pytest.raises(TypeError):
        state.artifacts["source_manifest"]["payload"]["sources"][0]["bytes"] = 99
    with pytest.raises(TypeError):
        plan.summary["artifacts"]["source_manifest"]["fingerprint"] = "changed"


def test_final_constraints_share_domain_risk_policy_and_reject_all_condition() -> None:
    artifacts = _base_artifacts()
    artifacts["evaluation_set"] = {
        **artifacts["evaluation_set"],
        "producer": {"kind": "fixture"},
    }
    artifacts["comparison"] = _artifact(
        "comparison",
        {
            "eligible_candidate_ids": ["zeta", "alpha"],
            "matrix": [
                {
                    "candidate_id": candidate,
                    "criterion_id": criterion,
                    "priority": priority,
                    "effective_assessment": assessment,
                    "review_status": review_status,
                }
                for candidate in ("zeta", "alpha")
                for criterion, priority, assessment, review_status in (
                    ("privacy", "must", "meets", "concur"),
                    (
                        "quality",
                        "high",
                        "partial" if candidate == "alpha" else "meets",
                        "concur",
                    ),
                    ("latency", "medium", "partial", "not_required"),
                )
            ],
        },
        producer="core",
    )
    plan = plan_operator_next(
        _state(
            ("candidate_set", "criteria_set", "evaluation_set", "comparison"),
            artifacts=artifacts,
        ),
        observed_at=OBSERVED_AT,
    )
    constraints = plan.to_public_dict()["final_decision_constraints"]

    assert constraints["eligible_candidate_ids"] == ["alpha", "zeta"]
    assert constraints["select"]["required_risk_acknowledgements_by_candidate"] == {
        "alpha": ["alpha/latency", "alpha/quality"],
        "zeta": ["zeta/latency"],
    }
    assert constraints["reject_all"]["shared_risk_condition"] == {
        "required": True,
        "feasible": True,
        "criterion_ids": ["latency"],
    }
