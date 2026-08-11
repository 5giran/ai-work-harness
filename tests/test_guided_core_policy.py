from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from ai_work_harness.decision.domain import (
    AGENT_PRODUCER_KINDS,
    evaluation_cell_requires_review,
    final_decision_constraints,
    required_review_cell_ids,
    review_completion_state,
    validate_reviews,
)
from ai_work_harness.errors import HarnessError


def _criteria() -> dict[str, dict[str, str]]:
    return {
        "privacy": {"criterion_id": "privacy", "priority": "must"},
        "quality": {"criterion_id": "quality", "priority": "high"},
        "latency": {"criterion_id": "latency", "priority": "medium"},
    }


def _evaluation_cells() -> dict[tuple[str, str], dict[str, str]]:
    return {
        (candidate_id, criterion_id): {
            "candidate_id": candidate_id,
            "criterion_id": criterion_id,
        }
        for candidate_id in ("zeta", "alpha")
        for criterion_id in ("quality", "privacy", "latency")
    }


def _review(
    candidate_id: str,
    criterion_id: str,
    outcome: str = "concur",
) -> dict[str, str]:
    return {
        "candidate_id": candidate_id,
        "criterion_id": criterion_id,
        "outcome": outcome,
        "reason": "The operator reviewed this cell.",
    }


def test_agent_review_policy_is_centralized_and_required_ids_are_stable() -> None:
    assert {"agent_import", "fixture", "openai"} == AGENT_PRODUCER_KINDS
    for producer_kind in AGENT_PRODUCER_KINDS:
        assert evaluation_cell_requires_review(
            producer_kind=producer_kind,
            priority="must",
        )
        assert evaluation_cell_requires_review(
            producer_kind=producer_kind,
            priority="high",
        )
        assert not evaluation_cell_requires_review(
            producer_kind=producer_kind,
            priority="medium",
        )

    assert not evaluation_cell_requires_review(
        producer_kind="local_operator",
        priority="must",
    )
    assert required_review_cell_ids(
        evaluation_cells=_evaluation_cells(),
        criteria=_criteria(),
        producer_kind="fixture",
    ) == (
        ("alpha", "privacy"),
        ("alpha", "quality"),
        ("zeta", "privacy"),
        ("zeta", "quality"),
    )
    assert (
        required_review_cell_ids(
            evaluation_cells=_evaluation_cells(),
            criteria=_criteria(),
            producer_kind="local_operator",
        )
        == ()
    )


def test_review_completion_state_separates_missing_and_revision_cells() -> None:
    state = review_completion_state(
        evaluation_cells=_evaluation_cells(),
        criteria=_criteria(),
        producer_kind="openai",
        reviews=[
            _review("zeta", "quality"),
            _review("alpha", "privacy", "request_revision"),
        ],
    )

    assert state.required_cell_ids == (
        ("alpha", "privacy"),
        ("alpha", "quality"),
        ("zeta", "privacy"),
        ("zeta", "quality"),
    )
    assert state.completed_cell_ids == (("zeta", "quality"),)
    assert state.missing_cell_ids == (("alpha", "quality"), ("zeta", "privacy"))
    assert state.pending_cell_ids == (
        ("alpha", "privacy"),
        ("alpha", "quality"),
        ("zeta", "privacy"),
    )
    assert state.revision_requested_cell_ids == (("alpha", "privacy"),)
    assert state.revision_required_cell_ids == state.revision_requested_cell_ids
    assert state.complete is False
    assert state.revision_pending is True
    assert state.can_store is True


def test_optional_revision_is_visible_without_making_required_reviews_incomplete() -> None:
    required_reviews = [
        _review(candidate_id, criterion_id)
        for candidate_id in ("alpha", "zeta")
        for criterion_id in ("privacy", "quality")
    ]
    state = review_completion_state(
        evaluation_cells=_evaluation_cells(),
        criteria=_criteria(),
        producer_kind="agent_import",
        reviews=[*required_reviews, _review("alpha", "latency", "request_revision")],
    )

    assert state.complete is True
    assert state.pending_cell_ids == ()
    assert state.revision_pending is True
    assert state.revision_requested_cell_ids == (("alpha", "latency"),)


def test_local_operator_reviews_have_no_required_cells() -> None:
    state = review_completion_state(
        evaluation_cells=_evaluation_cells(),
        criteria=_criteria(),
        producer_kind="local_operator",
        reviews=[],
    )

    assert state.required_cell_ids == ()
    assert state.pending_cell_ids == ()
    assert state.complete is True
    assert state.revision_pending is False
    assert state.can_store is True


def test_ordinary_incomplete_agent_review_still_rejects() -> None:
    with pytest.raises(HarnessError) as caught:
        validate_reviews(
            {"reviews": [_review("zeta", "quality")]},
            evaluation_cells=_evaluation_cells(),
            criteria=_criteria(),
            producer_kind="fixture",
            evidence_by_id={},
        )

    assert caught.value.code == "REVIEW_REQUIRED"
    assert caught.value.details == {
        "cells": [
            ["alpha", "privacy"],
            ["alpha", "quality"],
            ["zeta", "privacy"],
        ]
    }


def test_revision_request_allows_partial_review_artifact_to_be_stored() -> None:
    payload = {
        "reviews": [
            _review("zeta", "quality"),
            _review("alpha", "privacy", "request_revision"),
        ]
    }

    validated = validate_reviews(
        payload,
        evaluation_cells=_evaluation_cells(),
        criteria=_criteria(),
        producer_kind="fixture",
        evidence_by_id={},
    )

    assert validated["reviews"] == [
        _review("alpha", "privacy", "request_revision"),
        _review("zeta", "quality"),
    ]


def test_any_revision_request_marks_a_partial_artifact_as_storable() -> None:
    validated = validate_reviews(
        {"reviews": [_review("alpha", "latency", "request_revision")]},
        evaluation_cells=_evaluation_cells(),
        criteria=_criteria(),
        producer_kind="openai",
        evidence_by_id={},
    )

    assert validated["reviews"] == [_review("alpha", "latency", "request_revision")]


def _matrix_cell(
    candidate_id: str,
    criterion_id: str,
    priority: str,
    *,
    effective_assessment: str = "meets",
    review_status: str = "concur",
) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "criterion_id": criterion_id,
        "priority": priority,
        "assessment": effective_assessment,
        "effective_assessment": effective_assessment,
        "evidence_count": 1,
        "review_status": review_status,
    }


def _comparison() -> dict[str, Any]:
    return {
        "eligible_candidate_ids": ["two", "one"],
        "matrix": [
            _matrix_cell("three", "privacy", "must", effective_assessment="fails"),
            _matrix_cell("one", "privacy", "must"),
            _matrix_cell("two", "privacy", "must"),
            _matrix_cell("one", "quality", "high", effective_assessment="partial"),
            _matrix_cell("two", "quality", "high", effective_assessment="partial"),
            _matrix_cell("one", "latency", "medium", review_status="not_required"),
            _matrix_cell("two", "latency", "medium", review_status="override"),
            _matrix_cell("one", "cost", "medium", effective_assessment="partial"),
            _matrix_cell("two", "cost", "medium", effective_assessment="fails"),
            _matrix_cell("one", "speed", "low", review_status="request_revision"),
            _matrix_cell("two", "speed", "low", review_status="request_revision"),
        ],
    }


def test_final_constraints_expose_per_candidate_agent_risks_without_mutation() -> None:
    comparison = _comparison()
    original = deepcopy(comparison)

    constraints = final_decision_constraints(
        comparison=comparison,
        producer_kind="fixture",
        disposition="select",
        candidate_id="one",
    )

    assert comparison == original
    assert constraints.eligible_candidate_ids == ("one", "two")
    assert constraints.relevant_candidate_ids == ("one",)
    assert constraints.required_risk_acknowledgement_cell_ids_by_candidate == {
        "one": ("one/latency", "one/quality", "one/speed"),
        "two": ("two/quality", "two/speed"),
    }
    assert constraints.required_risk_acknowledgement_cell_ids == (
        "one/latency",
        "one/quality",
        "one/speed",
    )
    assert constraints.risk_cell_ids_by_candidate == {
        "one": ("one/cost", "one/latency", "one/quality", "one/speed"),
        "two": ("two/cost", "two/quality", "two/speed"),
    }
    assert constraints.reject_all_shared_risk_criterion_ids == (
        "cost",
        "quality",
        "speed",
    )
    assert constraints.reject_all_shared_risk_cell_ids_by_criterion["cost"] == (
        "one/cost",
        "two/cost",
    )
    assert constraints.reject_all_shared_risk_feasible is True
    assert "three/privacy" in constraints.valid_acknowledgement_cell_ids
    assert constraints.unknown_acknowledgement_cell_ids(
        ["one/quality", "not/a-cell"]
    ) == ("not/a-cell",)
    assert constraints.missing_required_risk_acknowledgement_cell_ids(
        ["one/quality"]
    ) == ("one/latency", "one/speed")


def test_reject_all_constraints_preserve_shared_risk_validation_details() -> None:
    constraints = final_decision_constraints(
        comparison=_comparison(),
        producer_kind="agent_import",
        disposition="reject_all",
        candidate_id=None,
    )

    assert constraints.relevant_candidate_ids == ("one", "two")
    assert constraints.required_risk_acknowledgement_cell_ids == (
        "one/latency",
        "one/quality",
        "one/speed",
        "two/quality",
        "two/speed",
    )
    mismatched = ["one/quality", "two/cost"]
    assert constraints.acknowledged_risk_criterion_ids_by_candidate(mismatched) == {
        "one": ("quality",),
        "two": ("cost",),
    }
    assert constraints.reject_all_shared_risk_satisfied(mismatched) is False
    assert constraints.reject_all_shared_risk_satisfied(["one/cost", "two/cost"]) is True


def test_local_operator_constraints_keep_optional_review_behavior_unchanged() -> None:
    constraints = final_decision_constraints(
        comparison=_comparison(),
        producer_kind="local_operator",
        disposition="select",
        candidate_id="one",
    )

    assert constraints.required_risk_acknowledgement_cell_ids_by_candidate == {
        "one": ("one/quality",),
        "two": ("two/quality",),
    }
    assert constraints.risk_cell_ids_by_candidate == {
        "one": ("one/cost", "one/quality"),
        "two": ("two/cost", "two/quality"),
    }
    assert constraints.reject_all_shared_risk_criterion_ids == ("cost", "quality")


def test_reject_all_reports_infeasible_when_eligible_candidates_share_no_risk() -> None:
    comparison = {
        "eligible_candidate_ids": ["one", "two"],
        "matrix": [
            _matrix_cell("one", "quality", "high", effective_assessment="partial"),
            _matrix_cell("one", "cost", "medium"),
            _matrix_cell("two", "quality", "high"),
            _matrix_cell("two", "cost", "medium", effective_assessment="partial"),
        ],
    }
    constraints = final_decision_constraints(
        comparison=comparison,
        producer_kind="local_operator",
        disposition="reject_all",
        candidate_id=None,
    )

    assert constraints.reject_all_shared_risk_criterion_ids == ()
    assert constraints.reject_all_shared_risk_feasible is False
    assert constraints.reject_all_shared_risk_satisfied(["one/quality", "two/cost"]) is False


def test_reject_all_without_eligible_candidates_needs_no_shared_risk() -> None:
    constraints = final_decision_constraints(
        comparison={
            "eligible_candidate_ids": [],
            "matrix": [
                _matrix_cell("one", "privacy", "must", effective_assessment="fails")
            ],
        },
        producer_kind="fixture",
        disposition="reject_all",
        candidate_id=None,
    )

    assert constraints.eligible_candidate_ids == ()
    assert constraints.relevant_candidate_ids == ()
    assert constraints.required_risk_acknowledgement_cell_ids == ()
    assert constraints.reject_all_shared_risk_criterion_ids == ()
    assert constraints.reject_all_shared_risk_feasible is True
    assert constraints.reject_all_shared_risk_satisfied([]) is True


@pytest.mark.parametrize("disposition", ["defer", "request_more_evidence"])
def test_noncommittal_dispositions_have_no_required_acknowledgements(
    disposition: str,
) -> None:
    constraints = final_decision_constraints(
        comparison=_comparison(),
        producer_kind="fixture",
        disposition=disposition,
        candidate_id=None,
    )

    assert constraints.relevant_candidate_ids == ()
    assert constraints.required_risk_acknowledgement_cell_ids == ()
