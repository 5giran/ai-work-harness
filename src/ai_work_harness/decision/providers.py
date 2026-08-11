"""Provider-neutral contracts for draft evaluations and recommendations.

Providers receive a frozen, repository-free view of a decision session.  They
return draft payloads only; persistence, confirmations, reviews, final decisions,
and approvals remain responsibilities of the decision core.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Protocol, cast, runtime_checkable

from ai_work_harness.errors import HarnessError

Assessment = Literal[
    "meets",
    "partial",
    "fails",
    "insufficient_evidence",
    "not_applicable",
]
Confidence = Literal["low", "medium", "high"]
RecommendationDisposition = Literal["select", "abstain"]

ASSESSMENTS: frozenset[str] = frozenset(
    {"meets", "partial", "fails", "insufficient_evidence", "not_applicable"}
)
CONFIDENCES: frozenset[str] = frozenset({"low", "medium", "high"})


class ProviderContractError(HarnessError):
    """A provider input or draft violated the provider-neutral contract."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__("PROVIDER_CONTRACT_INVALID", message, details=details)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted((_freeze(item) for item in value), key=repr))
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _record_id(record: Mapping[str, Any], kind: str) -> str:
    value = record.get(f"{kind}_id", record.get("id"))
    if not isinstance(value, str) or not value:
        raise ProviderContractError(
            f"Every {kind} must have a non-empty '{kind}_id' or 'id'",
            details={"record": _thaw(record)},
        )
    return value


@dataclass(frozen=True, slots=True)
class FrozenDecisionContext:
    """Immutable, serializable context passed across the provider boundary."""

    session_id: str
    snapshot_sha256: str
    candidates: tuple[Mapping[str, Any], ...]
    criteria: tuple[Mapping[str, Any], ...]
    evidence: tuple[Mapping[str, Any], ...] = ()
    evaluations: tuple[Mapping[str, Any], ...] = ()
    comparison: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ProviderContractError("session_id must not be empty")
        if not self.snapshot_sha256:
            raise ProviderContractError("snapshot_sha256 must not be empty")
        object.__setattr__(self, "candidates", tuple(_freeze(item) for item in self.candidates))
        object.__setattr__(self, "criteria", tuple(_freeze(item) for item in self.criteria))
        object.__setattr__(self, "evidence", tuple(_freeze(item) for item in self.evidence))
        object.__setattr__(self, "evaluations", tuple(_freeze(item) for item in self.evaluations))
        object.__setattr__(self, "comparison", _freeze(self.comparison))

        candidate_ids = tuple(_record_id(item, "candidate") for item in self.candidates)
        criterion_ids = tuple(_record_id(item, "criterion") for item in self.criteria)
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ProviderContractError("candidate IDs must be unique")
        if len(set(criterion_ids)) != len(criterion_ids):
            raise ProviderContractError("criterion IDs must be unique")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> FrozenDecisionContext:
        return cls(
            session_id=str(value.get("session_id", "")),
            snapshot_sha256=str(value.get("snapshot_sha256", "")),
            candidates=tuple(value.get("candidates", ())),
            criteria=tuple(value.get("criteria", ())),
            evidence=tuple(value.get("evidence", ())),
            evaluations=tuple(value.get("evaluations", ())),
            comparison=value.get("comparison", {}),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "snapshot_sha256": self.snapshot_sha256,
            "candidates": _thaw(self.candidates),
            "criteria": _thaw(self.criteria),
            "evidence": _thaw(self.evidence),
            "evaluations": _thaw(self.evaluations),
            "comparison": _thaw(self.comparison),
        }

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        return tuple(sorted(_record_id(item, "candidate") for item in self.candidates))

    @property
    def criterion_ids(self) -> tuple[str, ...]:
        return tuple(sorted(_record_id(item, "criterion") for item in self.criteria))


@dataclass(frozen=True, slots=True)
class EvaluationCell:
    candidate_id: str
    criterion_id: str
    assessment: Assessment
    rationale: str
    evidence_ids: tuple[str, ...]
    confidence: Confidence
    uncertainties: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        object.__setattr__(self, "uncertainties", tuple(self.uncertainties))
        if not self.candidate_id or not self.criterion_id:
            raise ProviderContractError("evaluation candidate_id and criterion_id are required")
        if self.assessment not in ASSESSMENTS:
            raise ProviderContractError(
                "unsupported evaluation assessment",
                details={"assessment": self.assessment},
            )
        if self.confidence not in CONFIDENCES:
            raise ProviderContractError(
                "unsupported evaluation confidence",
                details={"confidence": self.confidence},
            )
        if not self.rationale.strip():
            raise ProviderContractError("evaluation rationale must not be blank")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ProviderContractError("evaluation evidence_ids must not contain duplicates")
        if any(not isinstance(item, str) or not item.strip() for item in self.uncertainties):
            raise ProviderContractError("evaluation uncertainty descriptions must not be blank")
        if self.assessment == "insufficient_evidence":
            if self.evidence_ids:
                raise ProviderContractError(
                    "insufficient_evidence evaluations must not cite evidence"
                )
            if not self.uncertainties:
                raise ProviderContractError(
                    "insufficient_evidence evaluations must describe an uncertainty"
                )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> EvaluationCell:
        if not isinstance(value, Mapping):
            raise ProviderContractError("every evaluation cell must be an object")
        expected = {
            "candidate_id",
            "criterion_id",
            "assessment",
            "rationale",
            "evidence_ids",
            "confidence",
            "uncertainties",
        }
        if set(value) != expected:
            raise ProviderContractError(
                "evaluation cell has missing or undeclared fields",
                details={"expected": sorted(expected), "actual": sorted(value)},
            )
        evidence_ids = value["evidence_ids"]
        uncertainties = value["uncertainties"]
        if not isinstance(evidence_ids, list) or not all(
            isinstance(item, str) for item in evidence_ids
        ):
            raise ProviderContractError("evaluation evidence_ids must be an array of strings")
        if not isinstance(uncertainties, list) or not all(
            isinstance(item, str) for item in uncertainties
        ):
            raise ProviderContractError("evaluation uncertainties must be an array of strings")
        scalar_fields = ("candidate_id", "criterion_id", "assessment", "rationale", "confidence")
        if not all(isinstance(value[field_name], str) for field_name in scalar_fields):
            raise ProviderContractError("evaluation scalar fields must be strings")
        return cls(
            candidate_id=value["candidate_id"],
            criterion_id=value["criterion_id"],
            assessment=value["assessment"],
            rationale=value["rationale"],
            evidence_ids=tuple(evidence_ids),
            confidence=value["confidence"],
            uncertainties=tuple(uncertainties),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "criterion_id": self.criterion_id,
            "assessment": self.assessment,
            "rationale": self.rationale,
            "evidence_ids": list(self.evidence_ids),
            "confidence": self.confidence,
            "uncertainties": list(self.uncertainties),
        }


@dataclass(frozen=True, slots=True)
class EvaluationDraft:
    cells: tuple[EvaluationCell, ...]

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.cells, key=lambda item: (item.candidate_id, item.criterion_id)))
        object.__setattr__(self, "cells", ordered)

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> EvaluationDraft:
        if set(value) != {"cells"} or not isinstance(value.get("cells"), list):
            raise ProviderContractError("evaluation draft must contain only a cells array")
        return cls(tuple(EvaluationCell.from_mapping(item) for item in value["cells"]))

    def as_payload(self) -> dict[str, Any]:
        return {"cells": [cell.as_dict() for cell in self.cells]}

    def validate_against(self, context: FrozenDecisionContext) -> None:
        expected_pairs = {
            (candidate_id, criterion_id)
            for candidate_id in context.candidate_ids
            for criterion_id in context.criterion_ids
        }
        actual_pairs = {(cell.candidate_id, cell.criterion_id) for cell in self.cells}
        if len(actual_pairs) != len(self.cells):
            raise ProviderContractError(
                "evaluation draft contains duplicate candidate/criterion cells"
            )
        if actual_pairs != expected_pairs:
            raise ProviderContractError(
                "evaluation draft must cover the full candidate/criterion matrix",
                details={
                    "missing": sorted(expected_pairs - actual_pairs),
                    "unknown": sorted(actual_pairs - expected_pairs),
                },
            )

        evidence_by_id = {_record_id(item, "evidence"): item for item in context.evidence}
        for cell in self.cells:
            unknown = sorted(set(cell.evidence_ids) - evidence_by_id.keys())
            if unknown:
                raise ProviderContractError(
                    "evaluation draft references unknown evidence",
                    details={"evidence_ids": unknown},
                )
            if cell.assessment == "insufficient_evidence":
                continue
            factual = [
                evidence_by_id[evidence_id]
                for evidence_id in cell.evidence_ids
                if evidence_by_id[evidence_id].get("provenance") == "source_observation"
            ]
            if not factual:
                raise ProviderContractError(
                    "non-insufficient evaluations require source_observation evidence",
                    details={
                        "candidate_id": cell.candidate_id,
                        "criterion_id": cell.criterion_id,
                    },
                )


@dataclass(frozen=True, slots=True)
class RecommendationDraft:
    disposition: RecommendationDisposition
    candidate_id: str | None
    rationale: str
    evidence_ids: tuple[str, ...]
    risks: tuple[str, ...]
    uncertainties: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        object.__setattr__(self, "risks", tuple(self.risks))
        object.__setattr__(self, "uncertainties", tuple(self.uncertainties))
        if self.disposition not in {"select", "abstain"}:
            raise ProviderContractError(
                "recommendation disposition must be select or abstain",
                details={"disposition": self.disposition},
            )
        if self.disposition == "select" and not self.candidate_id:
            raise ProviderContractError("select recommendations require candidate_id")
        if self.disposition == "abstain" and self.candidate_id is not None:
            raise ProviderContractError("abstain recommendations require a null candidate_id")
        if not self.rationale.strip():
            raise ProviderContractError("recommendation rationale must not be blank")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ProviderContractError("recommendation evidence_ids must not contain duplicates")
        for field_name, values in (
            ("evidence_ids", self.evidence_ids),
            ("risks", self.risks),
            ("uncertainties", self.uncertainties),
        ):
            if any(not isinstance(item, str) or not item.strip() for item in values):
                raise ProviderContractError(f"recommendation {field_name} values must not be blank")

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> RecommendationDraft:
        expected = {
            "disposition",
            "candidate_id",
            "rationale",
            "evidence_ids",
            "risks",
            "uncertainties",
        }
        if set(value) != expected:
            raise ProviderContractError(
                "recommendation draft has missing or undeclared fields",
                details={"expected": sorted(expected), "actual": sorted(value)},
            )
        if not isinstance(value["disposition"], str) or not isinstance(value["rationale"], str):
            raise ProviderContractError("recommendation disposition and rationale must be strings")
        if value["candidate_id"] is not None and not isinstance(value["candidate_id"], str):
            raise ProviderContractError("recommendation candidate_id must be a string or null")
        for key in ("evidence_ids", "risks", "uncertainties"):
            if not isinstance(value[key], list) or not all(
                isinstance(item, str) for item in value[key]
            ):
                raise ProviderContractError(f"recommendation {key} must be an array of strings")
        return cls(
            disposition=cast(RecommendationDisposition, value["disposition"]),
            candidate_id=value["candidate_id"],
            rationale=value["rationale"],
            evidence_ids=tuple(value["evidence_ids"]),
            risks=tuple(value["risks"]),
            uncertainties=tuple(value["uncertainties"]),
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "disposition": self.disposition,
            "candidate_id": self.candidate_id,
            "rationale": self.rationale,
            "evidence_ids": list(self.evidence_ids),
            "risks": list(self.risks),
            "uncertainties": list(self.uncertainties),
        }

    def validate_against(self, context: FrozenDecisionContext) -> None:
        if self.candidate_id is not None and self.candidate_id not in context.candidate_ids:
            raise ProviderContractError(
                "recommendation references an unknown candidate",
                details={"candidate_id": self.candidate_id},
            )
        evidence_ids = {_record_id(item, "evidence") for item in context.evidence}
        unknown = sorted(set(self.evidence_ids) - evidence_ids)
        if unknown:
            raise ProviderContractError(
                "recommendation references unknown evidence",
                details={"evidence_ids": unknown},
            )
        eligible = _eligible_candidate_ids(context.comparison)
        if self.disposition == "select" and self.candidate_id not in eligible:
            raise ProviderContractError(
                "recommendation may select only a must-eligible candidate",
                details={"candidate_id": self.candidate_id, "eligible_candidate_ids": eligible},
            )


@runtime_checkable
class EvaluationProvider(Protocol):
    def generate(self, context: FrozenDecisionContext) -> EvaluationDraft:
        """Generate an evaluation draft without changing repository state."""


@runtime_checkable
class RecommendationProvider(Protocol):
    def generate(self, context: FrozenDecisionContext) -> RecommendationDraft:
        """Generate a recommendation draft without changing repository state."""


@dataclass(frozen=True, slots=True)
class FixtureCellSpec:
    """Explicit synthetic result and the real evidence IDs it is allowed to cite."""

    assessment: Assessment
    evidence_ids: tuple[str, ...]
    rationale: str = "Deterministic fixture result derived from configured source evidence."
    confidence: Confidence = "medium"
    uncertainties: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        object.__setattr__(self, "uncertainties", tuple(self.uncertainties))
        if self.assessment not in ASSESSMENTS:
            raise ProviderContractError(
                "fixture cell uses an unsupported assessment",
                details={"assessment": self.assessment},
            )
        if self.confidence not in CONFIDENCES:
            raise ProviderContractError(
                "fixture cell uses an unsupported confidence",
                details={"confidence": self.confidence},
            )
        if not self.rationale.strip():
            raise ProviderContractError("fixture cell rationale must not be blank")
        if self.assessment == "insufficient_evidence" and not self.uncertainties:
            raise ProviderContractError(
                "insufficient_evidence fixture cells must describe an uncertainty"
            )


_TRIAGE_ASSESSMENTS: Mapping[tuple[str, str], Assessment] = MappingProxyType(
    {
        ("rules", "privacy"): "meets",
        ("rules", "auditability"): "meets",
        ("rules", "classification-quality"): "partial",
        ("rules", "latency"): "meets",
        ("rules", "operating-cost"): "meets",
        ("classical-ml", "privacy"): "meets",
        ("classical-ml", "auditability"): "meets",
        ("classical-ml", "classification-quality"): "meets",
        ("classical-ml", "latency"): "partial",
        ("classical-ml", "operating-cost"): "partial",
        ("llm-assisted", "privacy"): "fails",
        ("llm-assisted", "auditability"): "partial",
        ("llm-assisted", "classification-quality"): "meets",
        ("llm-assisted", "latency"): "partial",
        ("llm-assisted", "operating-cost"): "fails",
    }
)


def triage_fixture_map() -> dict[tuple[str, str], FixtureCellSpec]:
    """Return a fresh, explicit cell map for the public synthetic triage demo."""

    return {
        pair: FixtureCellSpec(
            assessment=assessment,
            evidence_ids=(f"ev-{pair[0]}-{pair[1]}",),
            confidence="high" if assessment in {"meets", "fails"} else "medium",
        )
        for pair, assessment in _TRIAGE_ASSESSMENTS.items()
    }


def _eligible_candidate_ids(comparison: Mapping[str, Any]) -> list[str]:
    direct = comparison.get("eligible_candidate_ids")
    if isinstance(direct, (list, tuple)):
        return sorted(item for item in direct if isinstance(item, str))
    eligibility = comparison.get("must_eligibility")
    if isinstance(eligibility, Mapping):
        return sorted(
            candidate_id
            for candidate_id, is_eligible in eligibility.items()
            if isinstance(candidate_id, str) and is_eligible is True
        )
    results = comparison.get("candidate_results")
    if isinstance(results, (list, tuple)):
        return sorted(
            item["candidate_id"]
            for item in results
            if isinstance(item, Mapping)
            and isinstance(item.get("candidate_id"), str)
            and item.get("must_eligible") is True
        )
    return []


class _FixtureEvaluationAdapter:
    def __init__(self, fixture: FixtureProvider) -> None:
        self._fixture = fixture

    def generate(self, context: FrozenDecisionContext) -> EvaluationDraft:
        return self._fixture.generate_evaluations(context)


class _FixtureRecommendationAdapter:
    def __init__(self, fixture: FixtureProvider) -> None:
        self._fixture = fixture

    def generate(self, context: FrozenDecisionContext) -> RecommendationDraft:
        return self._fixture.generate_recommendation(context)


@dataclass(frozen=True, slots=True)
class FixtureProvider:
    """Transparent deterministic provider for tests and the synthetic demo."""

    evaluation_map: Mapping[tuple[str, str], FixtureCellSpec] = field(
        default_factory=triage_fixture_map
    )
    recommended_candidate_id: str | None = "classical-ml"

    def __post_init__(self) -> None:
        normalized: dict[tuple[str, str], FixtureCellSpec] = {}
        for pair, spec in self.evaluation_map.items():
            if (
                not isinstance(pair, tuple)
                or len(pair) != 2
                or not all(isinstance(item, str) and item for item in pair)
            ):
                raise ProviderContractError(
                    "fixture evaluation_map keys must be (candidate_id, criterion_id) tuples"
                )
            if not isinstance(spec, FixtureCellSpec):
                raise ProviderContractError(
                    "fixture evaluation_map values must be FixtureCellSpec instances",
                    details={"candidate_id": pair[0], "criterion_id": pair[1]},
                )
            normalized[pair] = spec
        object.__setattr__(self, "evaluation_map", MappingProxyType(normalized))

    def evaluation_provider(self) -> EvaluationProvider:
        return _FixtureEvaluationAdapter(self)

    def recommendation_provider(self) -> RecommendationProvider:
        return _FixtureRecommendationAdapter(self)

    def generate_evaluations(self, context: FrozenDecisionContext) -> EvaluationDraft:
        evidence_by_id = {_record_id(item, "evidence"): item for item in context.evidence}

        cells: list[EvaluationCell] = []
        for candidate_id in context.candidate_ids:
            for criterion_id in context.criterion_ids:
                pair = (candidate_id, criterion_id)
                spec = self.evaluation_map.get(pair)
                if spec is None:
                    cells.append(
                        EvaluationCell(
                            candidate_id=candidate_id,
                            criterion_id=criterion_id,
                            assessment="insufficient_evidence",
                            rationale="The fixture found no source observation for this cell.",
                            evidence_ids=(),
                            confidence="low",
                            uncertainties=("No matching source observation is available.",),
                        )
                    )
                    continue
                missing = sorted(set(spec.evidence_ids) - evidence_by_id.keys())
                if missing:
                    raise ProviderContractError(
                        "fixture cell references evidence that is absent from the context",
                        details={
                            "candidate_id": candidate_id,
                            "criterion_id": criterion_id,
                            "evidence_ids": missing,
                        },
                    )
                cells.append(
                    EvaluationCell(
                        candidate_id=candidate_id,
                        criterion_id=criterion_id,
                        assessment=spec.assessment,
                        rationale=spec.rationale,
                        evidence_ids=spec.evidence_ids,
                        confidence=spec.confidence,
                        uncertainties=spec.uncertainties,
                    )
                )
        draft = EvaluationDraft(tuple(cells))
        draft.validate_against(context)
        return draft

    def generate_recommendation(self, context: FrozenDecisionContext) -> RecommendationDraft:
        eligible = _eligible_candidate_ids(context.comparison)
        configured = self.recommended_candidate_id
        preferred = configured or ("classical-ml" if "classical-ml" in eligible else None)
        selected = preferred if preferred in eligible else (eligible[0] if eligible else None)

        evidence_ids = tuple(
            sorted(
                _record_id(item, "evidence")
                for item in context.evidence
                if item.get("provenance") == "source_observation"
                and (selected is None or item.get("candidate_id") == selected)
            )
        )
        if selected is None:
            draft = RecommendationDraft(
                disposition="abstain",
                candidate_id=None,
                rationale="The fixture found no candidate that passed all must criteria.",
                evidence_ids=evidence_ids,
                risks=("No must-eligible candidate is available.",),
                uncertainties=("More evidence or revised candidates are required.",),
            )
        else:
            draft = RecommendationDraft(
                disposition="select",
                candidate_id=selected,
                rationale="The deterministic fixture selected a must-eligible candidate.",
                evidence_ids=evidence_ids,
                risks=(),
                uncertainties=(),
            )
        draft.validate_against(context)
        return draft


__all__ = [
    "ASSESSMENTS",
    "CONFIDENCES",
    "EvaluationCell",
    "EvaluationDraft",
    "EvaluationProvider",
    "FixtureCellSpec",
    "FixtureProvider",
    "FrozenDecisionContext",
    "ProviderContractError",
    "RecommendationDraft",
    "RecommendationProvider",
    "triage_fixture_map",
]
