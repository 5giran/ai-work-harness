from __future__ import annotations

import json
from typing import Any

import pytest

from ai_work_harness.decision.openai_provider import (
    DEFAULT_MODEL,
    OpenAIProvider,
    OpenAIProviderError,
    evaluation_submission_tool,
    recommendation_submission_tool,
)
from ai_work_harness.decision.providers import (
    EvaluationProvider,
    FixtureCellSpec,
    FixtureProvider,
    FrozenDecisionContext,
    ProviderContractError,
    RecommendationProvider,
)


def _context() -> FrozenDecisionContext:
    candidates = (
        {"candidate_id": "rules", "title": "Rules"},
        {"candidate_id": "classical-ml", "title": "Classical ML"},
    )
    criteria = (
        {"criterion_id": "privacy", "priority": "must"},
        {"criterion_id": "classification-quality", "priority": "high"},
    )
    evidence = tuple(
        {
            "evidence_id": f"ev-{candidate_id}-{criterion_id}",
            "candidate_id": candidate_id,
            "criterion_id": criterion_id,
            "provenance": "source_observation",
        }
        for candidate_id in ("rules", "classical-ml")
        for criterion_id in ("privacy", "classification-quality")
    )
    return FrozenDecisionContext(
        session_id="triage",
        snapshot_sha256="a" * 64,
        candidates=candidates,
        criteria=criteria,
        evidence=evidence,
        comparison={"eligible_candidate_ids": ["rules", "classical-ml"]},
    )


def _evaluation_payload(context: FrozenDecisionContext) -> dict[str, Any]:
    return FixtureProvider().generate_evaluations(context).as_payload()


def _function_response(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "resp_test",
        "output": [
            {
                "type": "function_call",
                "name": name,
                "arguments": json.dumps(payload),
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }


def _lookup_response(name: str, ids_name: str, ids: list[str], call_id: str) -> dict[str, Any]:
    return {
        "id": f"resp_{call_id}",
        "output": [
            {
                "type": "function_call",
                "name": name,
                "call_id": call_id,
                "arguments": json.dumps({ids_name: ids}),
            }
        ],
        "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
    }


class _FakeResponses:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _FakeClient:
    base_url = "https://api.openai.com/v1/"

    def __init__(self, outcomes: list[Any]) -> None:
        self.responses = _FakeResponses(outcomes)


class _HttpError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def _assert_strict_objects(schema: dict[str, Any]) -> None:
    if schema.get("type") == "object":
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        for child in schema["properties"].values():
            _assert_strict_objects(child)
    if schema.get("type") == "array":
        _assert_strict_objects(schema["items"])


def test_fixture_is_deterministic_sorted_and_protocol_adapted() -> None:
    context = _context()
    fixture = FixtureProvider(recommended_candidate_id="classical-ml")

    first = fixture.generate_evaluations(context)
    second = fixture.generate_evaluations(context)

    assert first.as_payload() == second.as_payload()
    assert [(cell.candidate_id, cell.criterion_id) for cell in first.cells] == sorted(
        (cell.candidate_id, cell.criterion_id) for cell in first.cells
    )
    assert isinstance(fixture.evaluation_provider(), EvaluationProvider)
    assert isinstance(fixture.recommendation_provider(), RecommendationProvider)
    recommendation = fixture.generate_recommendation(context)
    assert recommendation.as_payload()["candidate_id"] == "classical-ml"


def test_fixture_fails_closed_to_insufficient_evidence() -> None:
    context = FrozenDecisionContext(
        session_id="empty-evidence",
        snapshot_sha256="b" * 64,
        candidates=({"candidate_id": "one"}, {"candidate_id": "two"}),
        criteria=({"criterion_id": "privacy", "priority": "must"},),
    )

    draft = FixtureProvider().generate_evaluations(context)

    assert {cell.assessment for cell in draft.cells} == {"insufficient_evidence"}
    assert all(cell.uncertainties for cell in draft.cells)


def test_triage_fixture_links_service_style_evidence_by_stable_id() -> None:
    context = FrozenDecisionContext(
        session_id="triage",
        snapshot_sha256="d" * 64,
        candidates=({"candidate_id": "rules"}, {"candidate_id": "classical-ml"}),
        criteria=({"criterion_id": "privacy", "priority": "must"},),
        evidence=(
            {
                "evidence_id": "ev-rules-privacy",
                "claim": "Synthetic rules privacy observation",
                "provenance": "source_observation",
                "source": {
                    "source_id": "triage-facts",
                    "start_line": 1,
                    "end_line": 1,
                    "excerpt_sha256": "e" * 64,
                },
            },
            {
                "evidence_id": "ev-classical-ml-privacy",
                "claim": "Synthetic ML privacy observation",
                "provenance": "source_observation",
                "source": {
                    "source_id": "triage-facts",
                    "start_line": 2,
                    "end_line": 2,
                    "excerpt_sha256": "f" * 64,
                },
            },
        ),
    )

    draft = FixtureProvider().generate_evaluations(context)

    assert {cell.assessment for cell in draft.cells} == {"meets"}
    assert all(cell.evidence_ids for cell in draft.cells)


def test_default_triage_fixture_reproduces_planned_failure_and_recommendation() -> None:
    context = FrozenDecisionContext(
        session_id="triage",
        snapshot_sha256="1" * 64,
        candidates=(
            {"candidate_id": "rules"},
            {"candidate_id": "classical-ml"},
            {"candidate_id": "llm-assisted"},
        ),
        criteria=({"criterion_id": "privacy", "priority": "must"},),
        evidence=tuple(
            {
                "evidence_id": f"ev-{candidate_id}-privacy",
                "provenance": "source_observation",
            }
            for candidate_id in ("rules", "classical-ml", "llm-assisted")
        ),
        comparison={"eligible_candidate_ids": ["rules", "classical-ml"]},
    )
    fixture = FixtureProvider()

    draft = fixture.generate_evaluations(context)
    by_candidate = {cell.candidate_id: cell.assessment for cell in draft.cells}

    assert by_candidate["llm-assisted"] == "fails"
    assert fixture.generate_recommendation(context).candidate_id == "classical-ml"


def test_custom_fixture_map_is_explicit_and_missing_evidence_fails_closed() -> None:
    context = FrozenDecisionContext(
        session_id="custom",
        snapshot_sha256="2" * 64,
        candidates=({"candidate_id": "one"}, {"candidate_id": "two"}),
        criteria=({"criterion_id": "privacy", "priority": "must"},),
        evidence=(
            {"evidence_id": "ev-one", "provenance": "source_observation"},
            {"evidence_id": "ev-two", "provenance": "source_observation"},
        ),
    )
    fixture = FixtureProvider(
        evaluation_map={
            ("one", "privacy"): FixtureCellSpec("meets", ("ev-one",)),
            ("two", "privacy"): FixtureCellSpec("fails", ("ev-two",)),
        },
        recommended_candidate_id="one",
    )
    assert [cell.assessment for cell in fixture.generate_evaluations(context).cells] == [
        "meets",
        "fails",
    ]

    missing = FixtureProvider(
        evaluation_map={
            ("one", "privacy"): FixtureCellSpec("meets", ("not-captured",)),
        }
    )
    with pytest.raises(ProviderContractError, match="absent"):
        missing.generate_evaluations(context)


def test_draft_rejects_non_factual_support() -> None:
    context = FrozenDecisionContext(
        session_id="claims",
        snapshot_sha256="c" * 64,
        candidates=({"candidate_id": "one"}, {"candidate_id": "two"}),
        criteria=({"criterion_id": "privacy", "priority": "must"},),
        evidence=(
            {
                "evidence_id": "claim-one",
                "candidate_id": "one",
                "criterion_id": "privacy",
                "provenance": "user_assertion",
            },
        ),
    )
    payload = {
        "cells": [
            {
                "candidate_id": "one",
                "criterion_id": "privacy",
                "assessment": "meets",
                "rationale": "Unsupported claim",
                "evidence_ids": ["claim-one"],
                "confidence": "high",
                "uncertainties": [],
            },
            {
                "candidate_id": "two",
                "criterion_id": "privacy",
                "assessment": "insufficient_evidence",
                "rationale": "No evidence",
                "evidence_ids": [],
                "confidence": "low",
                "uncertainties": ["Missing source observation"],
            },
        ]
    }

    from ai_work_harness.decision.providers import EvaluationDraft

    with pytest.raises(ProviderContractError, match="source_observation"):
        EvaluationDraft.from_payload(payload).validate_against(context)


@pytest.mark.parametrize(
    "tool_factory", [evaluation_submission_tool, recommendation_submission_tool]
)
def test_openai_submission_tools_are_strict_and_draft_only(tool_factory: Any) -> None:
    tool = tool_factory()
    assert tool["strict"] is True
    assert not any(term in tool["name"] for term in ("confirm", "review", "final", "approve"))
    _assert_strict_objects(tool["parameters"])


def test_openai_evaluation_request_uses_bounded_responses_defaults() -> None:
    context = _context()
    client = _FakeClient([_function_response("submit_evaluations", _evaluation_payload(context))])
    provider = OpenAIProvider(client=client, sleeper=lambda _: None)

    draft = provider.generate_evaluations(context)

    assert len(draft.cells) == 4
    request = client.responses.requests[0]
    assert request["model"] == DEFAULT_MODEL
    assert request["reasoning"] == {"effort": "medium"}
    assert request["store"] is False
    assert request["max_output_tokens"] == 8_000
    assert request["timeout"] == 60.0
    assert {tool["name"] for tool in request["tools"]} == {
        "get_candidates",
        "get_criteria",
        "get_evidence",
        "submit_evaluations",
    }
    assert all(tool["strict"] is True for tool in request["tools"])
    assert request["tool_choice"] == "required"
    assert provider.last_run is not None
    assert provider.last_run.prompt_id == "evaluation-v1"
    assert len(provider.last_run.prompt_sha256) == 64


def test_openai_lookup_loop_is_stateless_bounded_and_traced() -> None:
    context = _context()
    client = _FakeClient(
        [
            _lookup_response("get_candidates", "candidate_ids", [], "call_candidates"),
            _lookup_response(
                "get_evidence",
                "evidence_ids",
                ["ev-classical-ml-privacy"],
                "call_evidence",
            ),
            _function_response("submit_evaluations", _evaluation_payload(context)),
        ]
    )
    provider = OpenAIProvider(client=client)

    provider.generate_evaluations(context)

    assert len(client.responses.requests) == 3
    second_input = client.responses.requests[1]["input"]
    assert any(item.get("type") == "function_call_output" for item in second_input)
    assert all(request["store"] is False for request in client.responses.requests)
    assert provider.last_run is not None
    assert [event["tool_name"] for event in provider.last_run.transcript] == [
        "get_candidates",
        "get_evidence",
        "submit_evaluations",
    ]
    assert provider.last_run.usage == {
        "input_tokens": 16,
        "output_tokens": 24,
        "total_tokens": 40,
    }


def test_openai_recommendation_can_only_select_eligible_candidate() -> None:
    context = _context()
    payload = {
        "disposition": "select",
        "candidate_id": "classical-ml",
        "rationale": "Eligible and supported",
        "evidence_ids": ["ev-classical-ml-privacy"],
        "risks": [],
        "uncertainties": [],
    }
    client = _FakeClient([_function_response("submit_recommendation", payload)])

    draft = OpenAIProvider(client=client).generate_recommendation(context)

    assert draft.candidate_id == "classical-ml"


def test_openai_retries_only_retryable_failures_twice() -> None:
    context = _context()
    client = _FakeClient(
        [
            _HttpError(429),
            _HttpError(503),
            _function_response("submit_evaluations", _evaluation_payload(context)),
        ]
    )
    sleeps: list[float] = []

    OpenAIProvider(client=client, sleeper=sleeps.append).generate_evaluations(context)

    assert len(client.responses.requests) == 3
    assert sleeps == [0.25, 0.5]


def test_openai_does_not_retry_invalid_output_or_400() -> None:
    context = _context()
    invalid_client = _FakeClient(
        [_function_response("submit_evaluations", {"cells": [], "extra": True})]
    )
    with pytest.raises(OpenAIProviderError) as invalid:
        OpenAIProvider(client=invalid_client).generate_evaluations(context)
    assert invalid.value.code == "MODEL_INVALID_OUTPUT"
    assert len(invalid_client.responses.requests) == 1

    bad_request_client = _FakeClient([_HttpError(400)])
    with pytest.raises(OpenAIProviderError) as bad_request:
        OpenAIProvider(client=bad_request_client).generate_evaluations(context)
    assert bad_request.value.code == "MODEL_UNAVAILABLE"
    assert bad_request.value.retryable is False
    assert len(bad_request_client.responses.requests) == 1


def test_openai_refusal_is_not_promoted_to_a_draft() -> None:
    client = _FakeClient(
        [
            {
                "id": "resp_refusal",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "refusal", "refusal": "Cannot comply"}],
                    }
                ],
            }
        ]
    )

    with pytest.raises(OpenAIProviderError) as raised:
        OpenAIProvider(client=client).generate_evaluations(_context())

    assert raised.value.code == "MODEL_REFUSAL"


def test_openai_model_environment_override_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_WORK_HARNESS_OPENAI_MODEL", "gpt-5.6-test")
    assert OpenAIProvider(client=_FakeClient([])).model == "gpt-5.6-test"
