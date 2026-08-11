from __future__ import annotations

import pytest

from ai_work_harness.decision.domain import (
    derive_comparison,
    validate_candidates,
    validate_criteria,
    validate_evaluations,
    validate_reviews,
)
from ai_work_harness.errors import HarnessError


def candidate_payload() -> dict:
    return {
        "candidates": [
            {
                "candidate_id": candidate_id,
                "title": candidate_id,
                "summary": f"Use {candidate_id}",
                "proposed_by": "local_operator",
                "benefits": ["benefit"],
                "drawbacks": [],
                "risks": [],
                "uncertainties": [],
            }
            for candidate_id in ("rules", "classical-ml")
        ]
    }


def criteria_payload() -> dict:
    return {
        "criteria": [
            {
                "criterion_id": "privacy",
                "title": "Privacy",
                "definition": "Keep data local.",
                "priority": "must",
            },
            {
                "criterion_id": "quality",
                "title": "Quality",
                "definition": "Classify accurately.",
                "priority": "high",
            },
        ]
    }


def evidence() -> dict[str, dict]:
    return {
        "local": {"evidence_id": "local", "provenance": "source_observation"},
        "quality": {"evidence_id": "quality", "provenance": "source_observation"},
    }


def evaluation_payload() -> dict:
    return {
        "cells": [
            {
                "candidate_id": candidate,
                "criterion_id": criterion,
                "assessment": (
                    "meets" if candidate == "rules" or criterion == "privacy" else "partial"
                ),
                "rationale": "Synthetic evidence supports this assessment.",
                "evidence_ids": ["local" if criterion == "privacy" else "quality"],
                "confidence": "medium",
                "uncertainties": [],
            }
            for candidate in ("rules", "classical-ml")
            for criterion in ("privacy", "quality")
        ]
    }


def reviews_payload() -> dict:
    return {
        "reviews": [
            {
                "candidate_id": candidate,
                "criterion_id": criterion,
                "outcome": "concur",
                "reason": "Reviewed against the captured synthetic source.",
            }
            for candidate in ("rules", "classical-ml")
            for criterion in ("privacy", "quality")
        ]
    }


def test_candidates_require_two_items() -> None:
    payload = candidate_payload()
    payload["candidates"].pop()
    with pytest.raises(HarnessError) as caught:
        validate_candidates(payload)
    assert caught.value.code == "CANDIDATES_REQUIRED"


def test_candidate_ids_follow_the_public_safe_id_contract() -> None:
    payload = candidate_payload()
    payload["candidates"][0]["candidate_id"] = "../rules"

    with pytest.raises(HarnessError) as caught:
        validate_candidates(payload)

    assert caught.value.code == "UNSAFE_ID"


def test_criteria_reject_weights_and_require_must() -> None:
    payload = criteria_payload()
    payload["criteria"][0]["weight"] = 1
    with pytest.raises(HarnessError) as caught:
        validate_criteria(payload)
    assert caught.value.code == "INVALID_PAYLOAD"

    payload = criteria_payload()
    for item in payload["criteria"]:
        item["priority"] = "high"
    with pytest.raises(HarnessError) as caught:
        validate_criteria(payload)
    assert caught.value.code == "MUST_CRITERION_REQUIRED"


def test_evaluations_require_complete_matrix_and_source_evidence() -> None:
    payload = evaluation_payload()
    payload["cells"].pop()
    with pytest.raises(HarnessError) as caught:
        validate_evaluations(
            payload,
            candidate_ids={"rules", "classical-ml"},
            criterion_ids={"privacy", "quality"},
            evidence_by_id=evidence(),
        )
    assert caught.value.code == "INCOMPLETE_EVALUATION_MATRIX"

    payload = evaluation_payload()
    payload["cells"][0]["evidence_ids"] = []
    with pytest.raises(HarnessError) as caught:
        validate_evaluations(
            payload,
            candidate_ids={"rules", "classical-ml"},
            criterion_ids={"privacy", "quality"},
            evidence_by_id=evidence(),
        )
    assert caught.value.code == "FACTUAL_EVIDENCE_REQUIRED"

    payload = evaluation_payload()
    payload["cells"][0].update(
        assessment="insufficient_evidence",
        uncertainties=["The captured source is inconclusive."],
    )
    with pytest.raises(HarnessError) as caught:
        validate_evaluations(
            payload,
            candidate_ids={"rules", "classical-ml"},
            criterion_ids={"privacy", "quality"},
            evidence_by_id=evidence(),
        )
    assert caught.value.code == "EVIDENCE_NOT_ALLOWED"

    payload = evaluation_payload()
    payload["cells"][0].update(
        assessment="insufficient_evidence",
        evidence_ids=[],
        uncertainties=["   "],
    )
    with pytest.raises(HarnessError) as blank_uncertainty:
        validate_evaluations(
            payload,
            candidate_ids={"rules", "classical-ml"},
            criterion_ids={"privacy", "quality"},
            evidence_by_id=evidence(),
        )
    assert blank_uncertainty.value.code == "INVALID_PAYLOAD"


def test_fixture_reviews_are_required_for_must_and_high() -> None:
    cells = {
        (cell["candidate_id"], cell["criterion_id"]): cell for cell in evaluation_payload()["cells"]
    }
    criteria = {item["criterion_id"]: item for item in criteria_payload()["criteria"]}
    payload = reviews_payload()
    payload["reviews"].pop()
    with pytest.raises(HarnessError) as caught:
        validate_reviews(
            payload,
            evaluation_cells=cells,
            criteria=criteria,
            producer_kind="fixture",
            evidence_by_id=evidence(),
        )
    assert caught.value.code == "REVIEW_REQUIRED"

    payload = reviews_payload()
    payload["reviews"][0]["replacement_assessment"] = "meets"
    with pytest.raises(HarnessError) as replacement:
        validate_reviews(
            payload,
            evaluation_cells=cells,
            criteria=criteria,
            producer_kind="fixture",
            evidence_by_id=evidence(),
        )
    assert replacement.value.code == "INVALID_REVIEW"


def test_qualitative_comparison_has_no_score_or_winner() -> None:
    result = derive_comparison(
        candidates=candidate_payload()["candidates"],
        criteria=criteria_payload()["criteria"],
        cells=evaluation_payload()["cells"],
        reviews=reviews_payload()["reviews"],
        producer_kind="fixture",
    )
    assert result.eligible == {"rules", "classical-ml"}
    assert result.payload["scoring"] is None
    assert "winner" not in result.payload
    assert result.payload["unresolved_risks"] == [
        {
            "candidate_id": "classical-ml",
            "criterion_id": "quality",
            "priority": "high",
            "effective_assessment": "partial",
        }
    ]


def test_override_uses_replacement_evidence_and_reports_optional_review() -> None:
    criteria = criteria_payload()["criteria"] + [
        {
            "criterion_id": "latency",
            "title": "Latency",
            "definition": "Respond promptly.",
            "priority": "medium",
        }
    ]
    cells = evaluation_payload()["cells"] + [
        {
            "candidate_id": candidate,
            "criterion_id": "latency",
            "assessment": "partial",
            "rationale": "Initial assessment.",
            "evidence_ids": ["local", "quality"],
            "confidence": "medium",
            "uncertainties": [],
        }
        for candidate in ("rules", "classical-ml")
    ]
    reviews = reviews_payload()["reviews"] + [
        {
            "candidate_id": "rules",
            "criterion_id": "latency",
            "outcome": "override",
            "reason": "The cited observation supports the replacement.",
            "replacement_assessment": "meets",
            "replacement_evidence_ids": ["local"],
        }
    ]

    result = derive_comparison(
        candidates=candidate_payload()["candidates"],
        criteria=criteria,
        cells=cells,
        reviews=reviews,
        producer_kind="fixture",
    )

    overridden = next(
        item
        for item in result.payload["matrix"]
        if item["candidate_id"] == "rules" and item["criterion_id"] == "latency"
    )
    assert overridden["effective_assessment"] == "meets"
    assert overridden["evidence_count"] == 1
    assert overridden["review_status"] == "override"


def test_insufficient_evidence_override_rejects_evidence() -> None:
    cells = {
        (cell["candidate_id"], cell["criterion_id"]): cell for cell in evaluation_payload()["cells"]
    }
    payload = reviews_payload()
    payload["reviews"][0].update(
        outcome="override",
        replacement_assessment="insufficient_evidence",
        replacement_evidence_ids=["local"],
    )

    with pytest.raises(HarnessError) as caught:
        validate_reviews(
            payload,
            evaluation_cells=cells,
            criteria={item["criterion_id"]: item for item in criteria_payload()["criteria"]},
            producer_kind="fixture",
            evidence_by_id=evidence(),
        )

    assert caught.value.code == "EVIDENCE_NOT_ALLOWED"
