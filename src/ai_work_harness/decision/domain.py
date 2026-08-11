from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ai_work_harness.errors import HarnessError

from .models import validate_safe_id

ASSESSMENTS = {
    "meets",
    "partial",
    "fails",
    "insufficient_evidence",
    "not_applicable",
}
PRIORITIES = {"must", "high", "medium", "low"}
REVIEW_OUTCOMES = {"concur", "override", "request_revision"}
FACTUAL_ASSESSMENTS = ASSESSMENTS - {"insufficient_evidence"}


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    payload: dict[str, Any]
    effective: dict[tuple[str, str], str]
    eligible: frozenset[str]


def require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HarnessError("INVALID_PAYLOAD", f"{label} must be a JSON object")
    return value


def require_exact_fields(
    value: Mapping[str, Any],
    *,
    required: Iterable[str],
    optional: Iterable[str] = (),
    label: str,
) -> None:
    required_set = set(required)
    allowed = required_set | set(optional)
    missing = sorted(required_set - set(value))
    extra = sorted(set(value) - allowed)
    if missing or extra:
        raise HarnessError(
            "INVALID_PAYLOAD",
            f"{label} has missing or undeclared fields",
            details={"missing": missing, "undeclared": extra},
        )


def require_string(value: Any, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip():
        raise HarnessError(
            "INVALID_PAYLOAD",
            f"{field} must be a nonblank string",
            details={"field": field},
        )
    return value


def require_string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise HarnessError(
            "INVALID_PAYLOAD",
            f"{field} must be an array of nonblank strings",
            details={"field": field},
        )
    return value


def validate_frame(payload: Any) -> dict[str, Any]:
    document = require_object(payload, "decision frame")
    fields = (
        "user_statement_verbatim",
        "ai_initial_interpretation",
        "business_user",
        "blocked_decision",
        "problem_statement",
        "scope_in",
        "scope_out",
        "assumptions",
        "open_questions",
    )
    require_exact_fields(document, required=fields, label="decision frame")
    for field in fields[:2]:
        require_string(document[field], field)
    for field in fields[2:5]:
        require_string(document[field], field, nullable=True)
    for field in fields[5:]:
        require_string_list(document[field], field)
    return document


def frame_is_confirmable(payload: Mapping[str, Any]) -> bool:
    return all(
        isinstance(payload.get(field), str) and payload[field].strip()
        for field in (
            "business_user",
            "blocked_decision",
            "problem_statement",
        )
    )


def validate_candidates(payload: Any) -> dict[str, Any]:
    document = require_object(payload, "candidate set")
    require_exact_fields(document, required=("candidates",), label="candidate set")
    items = document["candidates"]
    if not isinstance(items, list) or len(items) < 2:
        raise HarnessError("CANDIDATES_REQUIRED", "At least two candidates are required")
    ids: set[str] = set()
    for index, candidate in enumerate(items):
        candidate = require_object(candidate, f"candidate[{index}]")
        require_exact_fields(
            candidate,
            required=(
                "candidate_id",
                "title",
                "summary",
                "proposed_by",
                "benefits",
                "drawbacks",
                "risks",
                "uncertainties",
            ),
            label=f"candidate[{index}]",
        )
        candidate_id = require_string(candidate["candidate_id"], "candidate_id")
        assert candidate_id is not None
        validate_safe_id(candidate_id, label="candidate_id")
        if candidate_id in ids:
            raise HarnessError("DUPLICATE_ID", "candidate_id must be unique")
        ids.add(candidate_id)
        for field in ("title", "summary", "proposed_by"):
            require_string(candidate[field], field)
        for field in ("benefits", "drawbacks", "risks", "uncertainties"):
            require_string_list(candidate[field], field)
    document["candidates"] = sorted(items, key=lambda item: item["candidate_id"])
    return document


def validate_criteria(payload: Any) -> dict[str, Any]:
    document = require_object(payload, "criteria set")
    require_exact_fields(document, required=("criteria",), label="criteria set")
    items = document["criteria"]
    if not isinstance(items, list) or not items:
        raise HarnessError("CRITERIA_REQUIRED", "At least one criterion is required")
    ids: set[str] = set()
    must_count = 0
    for index, criterion in enumerate(items):
        criterion = require_object(criterion, f"criterion[{index}]")
        require_exact_fields(
            criterion,
            required=("criterion_id", "title", "definition", "priority"),
            label=f"criterion[{index}]",
        )
        criterion_id = require_string(criterion["criterion_id"], "criterion_id")
        assert criterion_id is not None
        validate_safe_id(criterion_id, label="criterion_id")
        if criterion_id in ids:
            raise HarnessError("DUPLICATE_ID", "criterion_id must be unique")
        ids.add(criterion_id)
        require_string(criterion["title"], "title")
        require_string(criterion["definition"], "definition")
        if not isinstance(criterion["priority"], str) or criterion["priority"] not in PRIORITIES:
            raise HarnessError("INVALID_PRIORITY", "Unknown criterion priority")
        must_count += criterion["priority"] == "must"
    if not must_count:
        raise HarnessError("MUST_CRITERION_REQUIRED", "At least one must criterion is required")
    document["criteria"] = sorted(items, key=lambda item: item["criterion_id"])
    return document


def validate_evidence(
    payload: Any,
    *,
    source_ids: set[str],
    excerpt_hashes: Mapping[tuple[str, int, int], str],
) -> dict[str, Any]:
    document = require_object(payload, "evidence set")
    require_exact_fields(document, required=("evidence",), label="evidence set")
    items = document["evidence"]
    if not isinstance(items, list) or not items:
        raise HarnessError("EVIDENCE_REQUIRED", "At least one evidence item is required")
    ids: set[str] = set()
    for index, item in enumerate(items):
        item = require_object(item, f"evidence[{index}]")
        require_exact_fields(
            item,
            required=("evidence_id", "claim", "provenance", "source"),
            optional=("notes",),
            label=f"evidence[{index}]",
        )
        evidence_id = require_string(item["evidence_id"], "evidence_id")
        assert evidence_id is not None
        validate_safe_id(evidence_id, label="evidence_id")
        if evidence_id in ids:
            raise HarnessError("DUPLICATE_ID", "evidence_id must be unique")
        ids.add(evidence_id)
        require_string(item["claim"], "claim")
        if not isinstance(item["provenance"], str) or item["provenance"] not in {
            "source_observation",
            "user_assertion",
            "agent_inference",
        }:
            raise HarnessError("INVALID_PROVENANCE", "Unknown evidence provenance")
        source = item["source"]
        if item["provenance"] == "source_observation":
            source = require_object(source, "evidence source")
            require_exact_fields(
                source,
                required=("source_id", "start_line", "end_line", "excerpt_sha256"),
                label="evidence source",
            )
            source_id = require_string(source["source_id"], "source_id")
            assert source_id is not None
            validate_safe_id(source_id, label="source_id")
            if source_id not in source_ids:
                raise HarnessError("UNKNOWN_REFERENCE", "Evidence references an unknown source")
            start = source["start_line"]
            end = source["end_line"]
            if (
                not isinstance(start, int)
                or isinstance(start, bool)
                or not isinstance(end, int)
                or isinstance(end, bool)
                or start < 1
                or end < start
            ):
                raise HarnessError("INVALID_LOCATOR", "Evidence line range is invalid")
            expected = excerpt_hashes.get((source_id, start, end))
            if expected is None or source["excerpt_sha256"] != expected:
                raise HarnessError("EVIDENCE_LOCATOR_STALE", "Evidence excerpt hash is stale")
        elif source is not None:
            raise HarnessError("INVALID_PAYLOAD", "Non-source evidence must use source: null")
    document["evidence"] = sorted(items, key=lambda item: item["evidence_id"])
    return document


def validate_evaluations(
    payload: Any,
    *,
    candidate_ids: set[str],
    criterion_ids: set[str],
    evidence_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    document = require_object(payload, "evaluation set")
    require_exact_fields(document, required=("cells",), label="evaluation set")
    cells = document["cells"]
    if not isinstance(cells, list):
        raise HarnessError("INVALID_PAYLOAD", "cells must be an array")
    expected = {
        (candidate, criterion) for candidate in candidate_ids for criterion in criterion_ids
    }
    seen: set[tuple[str, str]] = set()
    for index, cell in enumerate(cells):
        cell = require_object(cell, f"cell[{index}]")
        require_exact_fields(
            cell,
            required=(
                "candidate_id",
                "criterion_id",
                "assessment",
                "rationale",
                "evidence_ids",
                "confidence",
                "uncertainties",
            ),
            label=f"cell[{index}]",
        )
        candidate_id = require_string(cell["candidate_id"], "candidate_id")
        criterion_id = require_string(cell["criterion_id"], "criterion_id")
        assert candidate_id is not None and criterion_id is not None
        validate_safe_id(candidate_id, label="candidate_id")
        validate_safe_id(criterion_id, label="criterion_id")
        key = (candidate_id, criterion_id)
        if key not in expected:
            raise HarnessError("UNKNOWN_REFERENCE", "Evaluation references an unknown ID")
        if key in seen:
            raise HarnessError("DUPLICATE_EVALUATION", "Each candidate/criterion needs one cell")
        seen.add(key)
        assessment = cell["assessment"]
        if not isinstance(assessment, str) or assessment not in ASSESSMENTS:
            raise HarnessError("INVALID_ASSESSMENT", "Unknown evaluation assessment")
        require_string(cell["rationale"], "rationale")
        evidence_ids = require_string_list(cell["evidence_ids"], "evidence_ids")
        if len(set(evidence_ids)) != len(evidence_ids):
            raise HarnessError(
                "DUPLICATE_REFERENCE",
                "Evaluation evidence_ids must not contain duplicates",
            )
        unknown = sorted(set(evidence_ids) - set(evidence_by_id))
        if unknown:
            raise HarnessError(
                "UNKNOWN_REFERENCE",
                "Evaluation references unknown evidence",
                details={"evidence_ids": unknown},
            )
        factual = [
            evidence_id
            for evidence_id in evidence_ids
            if evidence_by_id[evidence_id]["provenance"] == "source_observation"
        ]
        if assessment in FACTUAL_ASSESSMENTS and not factual:
            raise HarnessError("FACTUAL_EVIDENCE_REQUIRED", "Assessment requires source evidence")
        uncertainties = require_string_list(cell["uncertainties"], "uncertainties")
        if assessment == "insufficient_evidence":
            if evidence_ids:
                raise HarnessError(
                    "EVIDENCE_NOT_ALLOWED",
                    "insufficient_evidence must describe uncertainty instead of citing evidence",
                )
            if not uncertainties:
                raise HarnessError(
                    "UNCERTAINTY_REQUIRED",
                    "insufficient_evidence requires an uncertainty explanation",
                )
        if not isinstance(cell["confidence"], str) or cell["confidence"] not in {
            "low",
            "medium",
            "high",
        }:
            raise HarnessError("INVALID_CONFIDENCE", "Unknown evaluation confidence")
    missing = sorted(expected - seen)
    if missing:
        raise HarnessError(
            "INCOMPLETE_EVALUATION_MATRIX",
            "Every candidate/criterion pair must be evaluated",
            details={"missing": [list(item) for item in missing]},
        )
    document["cells"] = sorted(
        cells,
        key=lambda item: (item["candidate_id"], item["criterion_id"]),
    )
    return document


def validate_reviews(
    payload: Any,
    *,
    evaluation_cells: Mapping[tuple[str, str], Mapping[str, Any]],
    criteria: Mapping[str, Mapping[str, Any]],
    producer_kind: str,
    evidence_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    document = require_object(payload, "evaluation review set")
    require_exact_fields(document, required=("reviews",), label="evaluation review set")
    reviews = document["reviews"]
    if not isinstance(reviews, list):
        raise HarnessError("INVALID_PAYLOAD", "reviews must be an array")
    by_key: dict[tuple[str, str], Mapping[str, Any]] = {}
    for index, review in enumerate(reviews):
        review = require_object(review, f"review[{index}]")
        require_exact_fields(
            review,
            required=("candidate_id", "criterion_id", "outcome", "reason"),
            optional=("replacement_assessment", "replacement_evidence_ids"),
            label=f"review[{index}]",
        )
        candidate_id = require_string(review["candidate_id"], "candidate_id")
        criterion_id = require_string(review["criterion_id"], "criterion_id")
        assert candidate_id is not None and criterion_id is not None
        validate_safe_id(candidate_id, label="candidate_id")
        validate_safe_id(criterion_id, label="criterion_id")
        key = (candidate_id, criterion_id)
        if key not in evaluation_cells:
            raise HarnessError("UNKNOWN_REFERENCE", "Review references an unknown cell")
        if key in by_key:
            raise HarnessError("DUPLICATE_REVIEW", "Each evaluation cell may be reviewed once")
        by_key[key] = review
        if not isinstance(review["outcome"], str) or review["outcome"] not in REVIEW_OUTCOMES:
            raise HarnessError("INVALID_REVIEW", "Unknown review outcome")
        require_string(review["reason"], "reason")
        if review["outcome"] == "override":
            replacement = review.get("replacement_assessment")
            if not isinstance(replacement, str) or replacement not in ASSESSMENTS:
                raise HarnessError("INVALID_REVIEW", "Override requires a valid assessment")
            evidence_ids = require_string_list(
                review.get("replacement_evidence_ids", []),
                "replacement_evidence_ids",
            )
            if len(set(evidence_ids)) != len(evidence_ids):
                raise HarnessError(
                    "DUPLICATE_REFERENCE",
                    "Override evidence IDs must not contain duplicates",
                )
            if any(item not in evidence_by_id for item in evidence_ids):
                raise HarnessError("UNKNOWN_REFERENCE", "Override evidence is invalid")
            if replacement in FACTUAL_ASSESSMENTS and not any(
                evidence_by_id[item]["provenance"] == "source_observation" for item in evidence_ids
            ):
                raise HarnessError("FACTUAL_EVIDENCE_REQUIRED", "Override requires source evidence")
            if replacement == "insufficient_evidence" and evidence_ids:
                raise HarnessError(
                    "EVIDENCE_NOT_ALLOWED",
                    "An insufficient-evidence override must not cite evidence",
                )
        elif "replacement_assessment" in review or "replacement_evidence_ids" in review:
            raise HarnessError(
                "INVALID_REVIEW",
                "Only override reviews may include replacement fields",
            )
    if producer_kind in {"fixture", "openai", "agent_import"}:
        required = {
            key for key in evaluation_cells if criteria[key[1]]["priority"] in {"must", "high"}
        }
        incomplete = sorted(
            key
            for key in required
            if key not in by_key or by_key[key]["outcome"] == "request_revision"
        )
        if incomplete:
            raise HarnessError(
                "REVIEW_REQUIRED",
                "All agent-authored must/high cells require completed review",
                details={"cells": [list(item) for item in incomplete]},
            )
    document["reviews"] = sorted(
        reviews,
        key=lambda item: (item["candidate_id"], item["criterion_id"]),
    )
    return document


def derive_comparison(
    *,
    candidates: Sequence[Mapping[str, Any]],
    criteria: Sequence[Mapping[str, Any]],
    cells: Sequence[Mapping[str, Any]],
    reviews: Sequence[Mapping[str, Any]],
    producer_kind: str,
) -> ComparisonResult:
    review_by_key = {(item["candidate_id"], item["criterion_id"]): item for item in reviews}
    criteria_by_id = {item["criterion_id"]: item for item in criteria}
    effective: dict[tuple[str, str], str] = {}
    matrix: list[dict[str, Any]] = []
    for cell in cells:
        key = (cell["candidate_id"], cell["criterion_id"])
        review = review_by_key.get(key)
        assessment = cell["assessment"]
        evidence_count = len(cell["evidence_ids"])
        review_status = review["outcome"] if review is not None else "not_required"
        if (
            review is None
            and producer_kind in {"fixture", "openai", "agent_import"}
            and criteria_by_id[key[1]]["priority"] in {"must", "high"}
        ):
            review_status = "missing"
        if review is not None and review["outcome"] == "override":
            assessment = review["replacement_assessment"]
            evidence_count = len(review["replacement_evidence_ids"])
        effective[key] = assessment
        matrix.append(
            {
                "candidate_id": key[0],
                "criterion_id": key[1],
                "priority": criteria_by_id[key[1]]["priority"],
                "assessment": cell["assessment"],
                "effective_assessment": assessment,
                "evidence_count": evidence_count,
                "review_status": review_status,
            }
        )
    eligible = {
        candidate["candidate_id"]
        for candidate in candidates
        if all(
            effective[(candidate["candidate_id"], criterion["criterion_id"])] == "meets"
            for criterion in criteria
            if criterion["priority"] == "must"
        )
    }
    payload = {
        "candidate_order": [item["candidate_id"] for item in candidates],
        "criterion_order": [item["criterion_id"] for item in criteria],
        "matrix": matrix,
        "eligible_candidate_ids": sorted(eligible),
        "ineligible_candidate_ids": sorted(
            {item["candidate_id"] for item in candidates} - eligible
        ),
        "unresolved_risks": [
            {
                "candidate_id": item["candidate_id"],
                "criterion_id": item["criterion_id"],
                "priority": item["priority"],
                "effective_assessment": item["effective_assessment"],
            }
            for item in matrix
            if item["effective_assessment"] != "meets"
        ],
        "scoring": None,
    }
    return ComparisonResult(payload=payload, effective=effective, eligible=frozenset(eligible))
