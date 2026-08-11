from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from types import ModuleType
from typing import Any

import pytest

from ai_work_harness.decision.openai_provider import (
    OpenAIProvider,
    OpenAIProviderError,
    candidate_lookup_tool,
    criterion_lookup_tool,
    evidence_lookup_tool,
    validate_tool_transcript,
)
from ai_work_harness.decision.providers import (
    EvaluationCell,
    EvaluationDraft,
    FixtureCellSpec,
    FixtureProvider,
    FrozenDecisionContext,
    ProviderContractError,
    RecommendationDraft,
)


def _context(*, evidence_count: int = 2) -> FrozenDecisionContext:
    evidence = tuple(
        {
            "evidence_id": f"evidence-{index}",
            "candidate_id": "one" if index % 2 == 0 else "two",
            "criterion_id": "must",
            "provenance": "source_observation",
        }
        for index in range(evidence_count)
    )
    return FrozenDecisionContext(
        session_id="demo",
        snapshot_sha256="a" * 64,
        candidates=({"candidate_id": "one"}, {"candidate_id": "two"}),
        criteria=({"criterion_id": "must", "priority": "must"},),
        evidence=evidence,
        comparison={"must_eligibility": {"one": True, "two": False}},
    )


def _evaluations() -> dict[str, Any]:
    return {
        "cells": [
            {
                "candidate_id": candidate,
                "criterion_id": "must",
                "assessment": "meets",
                "rationale": "Cited source observation.",
                "evidence_ids": [f"evidence-{index}"],
                "confidence": "high",
                "uncertainties": [],
            }
            for index, candidate in enumerate(("one", "two"))
        ]
    }


def _recommendation() -> dict[str, Any]:
    return {
        "disposition": "select",
        "candidate_id": "one",
        "rationale": "Must eligible.",
        "evidence_ids": ["evidence-0"],
        "risks": [],
        "uncertainties": [],
    }


def _response(name: str, payload: Any, **extra: Any) -> dict[str, Any]:
    return {
        "id": "response-id",
        "output": [{"type": "function_call", "name": name, "arguments": payload}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
        **extra,
    }


def _lookup_response(
    name: str,
    arguments: dict[str, Any],
    *,
    call_id: str = "lookup-call",
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    return {
        "id": f"response-{call_id}",
        "output": [
            {
                "type": "function_call",
                "name": name,
                "call_id": call_id,
                "arguments": json.dumps(arguments),
            }
        ],
        "usage": (usage if usage is not None else {"input_tokens": 1, "output_tokens": 1}),
    }


class _Responses:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls = 0
        self.requests: list[dict[str, Any]] = []

    def create(self, **request: Any) -> Any:
        self.calls += 1
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _Client:
    def __init__(self, outcomes: list[Any]) -> None:
        self.responses = _Responses(outcomes)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"client": _Client([]), "client_factory": lambda: _Client([])},
        {"client": _Client([]), "max_retries": -1},
        {"client": _Client([]), "max_retries": 3},
        {"client": _Client([]), "max_tool_calls": 0},
        {"client": _Client([]), "max_tool_calls": 13},
        {"client": _Client([]), "max_output_tokens": 8001},
        {"client": _Client([]), "timeout_seconds": 61},
        {"client": _Client([]), "reasoning_effort": "unbounded"},
        {"client": _Client([]), "max_context_bytes": 0},
        {"client": _Client([]), "max_lookup_bytes": 1000001},
    ],
)
def test_openai_configuration_limits_fail_before_network(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        OpenAIProvider(**kwargs)


def test_openai_client_factory_is_lazy_and_adapters_delegate() -> None:
    created: list[_Client] = []
    client = _Client([_response("submit_evaluations", _evaluations())])

    def factory() -> _Client:
        created.append(client)
        return client

    provider = OpenAIProvider(client_factory=factory)
    assert created == []
    assert len(provider.evaluation_provider().generate(_context()).cells) == 2
    assert created == [client]

    recommendation_client = _Client([_response("submit_recommendation", _recommendation())])
    recommendation_provider = OpenAIProvider(client=recommendation_client)
    draft = recommendation_provider.recommendation_provider().generate(_context())
    assert draft.candidate_id == "one"


def test_missing_openai_sdk_is_a_structured_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "openai", None)
    with pytest.raises(OpenAIProviderError) as caught:
        OpenAIProvider().generate_evaluations(_context())
    assert caught.value.code == "MODEL_SDK_UNAVAILABLE"
    assert caught.value.exit_code == 4


def test_client_factory_failure_is_structured_and_never_retried() -> None:
    factory_calls: list[str] = []
    sleeps: list[float] = []

    def fail_factory() -> Any:
        factory_calls.append("called")
        raise TimeoutError("credential lookup must not leak")

    with pytest.raises(OpenAIProviderError) as caught:
        OpenAIProvider(
            client_factory=fail_factory,
            sleeper=sleeps.append,
        ).generate_evaluations(_context())

    assert caught.value.code == "MODEL_UNAVAILABLE"
    assert caught.value.exit_code == 4
    assert caught.value.retryable is False
    assert caught.value.details == {
        "retryable": False,
        "attempts": 1,
        "exception_type": "TimeoutError",
        "phase": "client_initialization",
    }
    assert factory_calls == ["called"]
    assert sleeps == []


def test_sdk_client_construction_failure_is_structured_as_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = ModuleType("openai")

    class MissingCredentials(RuntimeError):
        pass

    class OpenAI:
        def __init__(self, **_kwargs: Any) -> None:
            raise MissingCredentials("missing API key must not leak")

    module.OpenAI = OpenAI  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", module)

    with pytest.raises(OpenAIProviderError) as caught:
        OpenAIProvider().generate_evaluations(_context())

    assert caught.value.code == "MODEL_UNAVAILABLE"
    assert caught.value.exit_code == 4
    assert caught.value.retryable is False
    assert caught.value.details["phase"] == "client_initialization"
    assert caught.value.details["exception_type"] == "MissingCredentials"
    assert "missing API key" not in str(caught.value.as_dict())


def test_openai_enforces_evidence_and_tool_call_bounds_without_network() -> None:
    client = _Client([])
    with pytest.raises(OpenAIProviderError) as too_much_evidence:
        OpenAIProvider(client=client).generate_evaluations(_context(evidence_count=51))
    assert too_much_evidence.value.code == "OUTBOUND_LIMIT_EXCEEDED"
    assert client.responses.calls == 0

    calls = [
        {"type": "function_call", "name": "submit_evaluations", "arguments": _evaluations()}
        for _ in range(13)
    ]
    with pytest.raises(OpenAIProviderError) as too_many_tools:
        OpenAIProvider(client=_Client([{"output": calls}])).generate_evaluations(_context())
    assert too_many_tools.value.code == "MODEL_INVALID_OUTPUT"


def test_openai_lookup_loop_exposes_only_bounded_read_tools_and_records_transcript() -> None:
    context = _context()
    client = _Client(
        [
            _lookup_response(
                "get_candidates",
                {"candidate_ids": []},
                call_id="candidates",
                usage={"input_tokens": 3, "output_tokens": 2},
            ),
            _lookup_response(
                "get_evidence",
                {"evidence_ids": ["evidence-0"]},
                call_id="evidence",
                usage={"input_tokens": 2, "output_tokens": 1},
            ),
            _response(
                "submit_evaluations",
                json.dumps(_evaluations()),
                usage={"input_tokens": 4, "output_tokens": 3},
            ),
        ]
    )
    provider = OpenAIProvider(client=client)
    assert len(provider.generate_evaluations(context).cells) == 2
    assert client.responses.calls == 3
    first_request = client.responses.requests[0]
    assert [tool["name"] for tool in first_request["tools"]] == [
        "get_candidates",
        "get_criteria",
        "get_evidence",
        "submit_evaluations",
    ]
    assert first_request["tool_choice"] == "required"
    assert first_request["include"] == ["reasoning.encrypted_content"]
    assert provider.last_run is not None
    assert provider.last_run.usage == {
        "input_tokens": 9,
        "output_tokens": 6,
        "total_tokens": 15,
    }
    transcript = [dict(event) for event in provider.last_run.transcript]
    assert [event["tool_name"] for event in transcript] == [
        "get_candidates",
        "get_evidence",
        "submit_evaluations",
    ]
    validate_tool_transcript(
        transcript,
        context=context,
        expected_submission="submit_evaluations",
        result_payload=_evaluations(),
    )


@pytest.mark.parametrize(
    ("tool_factory", "name", "ids_name"),
    [
        (candidate_lookup_tool, "get_candidates", "candidate_ids"),
        (criterion_lookup_tool, "get_criteria", "criterion_ids"),
        (evidence_lookup_tool, "get_evidence", "evidence_ids"),
    ],
)
def test_lookup_tool_contracts_are_strict_and_read_only_shaped(
    tool_factory: Any,
    name: str,
    ids_name: str,
) -> None:
    tool = tool_factory()
    assert tool["name"] == name
    assert tool["strict"] is True
    assert tool["parameters"]["required"] == [ids_name]
    assert tool["parameters"]["additionalProperties"] is False


def test_openai_round_output_and_excerpt_limits_are_enforced() -> None:
    lookup = _lookup_response("get_candidates", {"candidate_ids": []})
    client = _Client([lookup, lookup])
    with pytest.raises(OpenAIProviderError) as rounds:
        OpenAIProvider(client=client, max_tool_calls=2).generate_evaluations(_context())
    assert rounds.value.code == "MODEL_TOOL_CALL_LIMIT"

    output_client = _Client(
        [
            _lookup_response(
                "get_candidates",
                {"candidate_ids": []},
                usage={"input_tokens": 0, "output_tokens": 5},
            )
        ]
    )
    with pytest.raises(OpenAIProviderError) as output:
        OpenAIProvider(client=output_client, max_output_tokens=5).generate_evaluations(_context())
    assert output.value.code == "MODEL_OUTPUT_LIMIT"

    for excerpt in (1, "x" * 100_001):
        base = _context().as_dict()
        base["evidence"][0]["cited_excerpt"] = excerpt
        context = FrozenDecisionContext.from_mapping(base)
        client = _Client([])
        with pytest.raises(OpenAIProviderError) as limit:
            OpenAIProvider(client=client).generate_evaluations(context)
        assert limit.value.code == "OUTBOUND_LIMIT_EXCEEDED"
        assert client.responses.calls == 0

    with pytest.raises(OpenAIProviderError) as context_limit:
        OpenAIProvider(client=_Client([]), max_context_bytes=1).generate_evaluations(_context())
    assert context_limit.value.code == "OUTBOUND_LIMIT_EXCEEDED"

    with pytest.raises(OpenAIProviderError) as lookup_limit:
        OpenAIProvider(
            client=_Client([_lookup_response("get_candidates", {"candidate_ids": []})]),
            max_lookup_bytes=1,
        ).generate_evaluations(_context())
    assert lookup_limit.value.code == "MODEL_LOOKUP_LIMIT"


def test_openai_requires_usage_and_rejects_malformed_nested_output() -> None:
    with pytest.raises(OpenAIProviderError) as usage:
        OpenAIProvider(
            client=_Client([_response("submit_evaluations", _evaluations(), usage={})])
        ).generate_evaluations(_context())
    assert usage.value.code == "MODEL_USAGE_MISSING"

    with pytest.raises(OpenAIProviderError) as nested:
        OpenAIProvider(
            client=_Client([{"output": [{"type": "message", "content": 1}]}])
        ).generate_evaluations(_context())
    assert nested.value.code == "MODEL_INVALID_OUTPUT"


def test_openai_rejects_non_object_cells_and_duplicate_evidence_ids() -> None:
    duplicate = _evaluations()
    duplicate["cells"][0]["evidence_ids"] = ["evidence-0", "evidence-0"]
    for payload in ({"cells": [1]}, duplicate):
        with pytest.raises(OpenAIProviderError) as caught:
            OpenAIProvider(
                client=_Client([_response("submit_evaluations", payload)])
            ).generate_evaluations(_context())
        assert caught.value.code == "MODEL_INVALID_OUTPUT"


@pytest.mark.parametrize(
    "response",
    [
        {"output": []},
        _lookup_response("unknown_tool", {"ids": []}),
        _lookup_response("get_candidates", {"candidate_ids": []}, call_id=""),
        _lookup_response("get_candidates", {"wrong": []}),
        _lookup_response("get_candidates", {"candidate_ids": ["one", "one"]}),
        _lookup_response("get_candidates", {"candidate_ids": ["absent"]}),
        {
            "output": [
                {
                    "type": "function_call",
                    "name": "get_candidates",
                    "call_id": "lookup",
                    "arguments": {"candidate_ids": []},
                },
                {
                    "type": "function_call",
                    "name": "submit_evaluations",
                    "arguments": _evaluations(),
                },
            ]
        },
    ],
)
def test_openai_lookup_loop_rejects_unknown_or_invalid_calls(response: dict[str, Any]) -> None:
    with pytest.raises(OpenAIProviderError) as caught:
        OpenAIProvider(client=_Client([response])).generate_evaluations(_context())
    assert caught.value.code == "MODEL_INVALID_OUTPUT"


def test_transcript_validator_rejects_tampering_and_bad_event_order() -> None:
    context = _context()
    result = _evaluations()
    lookup_output = {"candidates": [{"candidate_id": "one"}]}
    valid_lookup = {
        "round": 1,
        "call_id": "lookup",
        "tool_name": "get_candidates",
        "arguments": {"candidate_ids": ["one"]},
        "output": lookup_output,
    }
    submission = {
        "round": 2,
        "call_id": None,
        "tool_name": "submit_evaluations",
        "arguments": result,
    }
    validate_tool_transcript(
        [valid_lookup, submission],
        context=context,
        expected_submission="submit_evaluations",
        result_payload=result,
    )

    invalid_transcripts: list[Any] = [
        [],
        ["not-an-object"],
        [{**valid_lookup, "extra": True}, submission],
        [{**valid_lookup, "round": 0}, submission],
        [{**valid_lookup, "call_id": 1}, submission],
        [{**valid_lookup, "call_id": None}, submission],
        [{**valid_lookup, "output": {"candidates": []}}, submission],
        [submission, valid_lookup],
        [{**valid_lookup, "round": 2}, {**submission, "round": 3}],
        [valid_lookup, {**submission, "round": 1}],
        [valid_lookup, {**valid_lookup, "round": 2}, {**submission, "round": 3}],
        [{**submission, "arguments": {"cells": []}}],
        [valid_lookup],
    ]
    for transcript in invalid_transcripts:
        with pytest.raises(ValueError):
            validate_tool_transcript(
                transcript,
                context=context,
                expected_submission="submit_evaluations",
                result_payload=result,
            )


@pytest.mark.parametrize(
    "output",
    [
        [],
        [{"type": "function_call", "name": "wrong", "arguments": {}}],
        [
            {"type": "function_call", "name": "submit_evaluations", "arguments": {}},
            {"type": "function_call", "name": "wrong", "arguments": {}},
        ],
    ],
)
def test_openai_requires_exactly_one_expected_submission(output: list[dict[str, Any]]) -> None:
    with pytest.raises(OpenAIProviderError) as caught:
        OpenAIProvider(client=_Client([{"output": output}])).generate_evaluations(_context())
    assert caught.value.code == "MODEL_INVALID_OUTPUT"


@pytest.mark.parametrize(
    "arguments",
    [
        3,
        "[]",
        "{",
        '{"cells":[],"cells":[]}',
        '{"n":NaN}',
    ],
)
def test_openai_rejects_malformed_or_ambiguous_tool_arguments(arguments: Any) -> None:
    with pytest.raises(OpenAIProviderError) as caught:
        OpenAIProvider(
            client=_Client([_response("submit_evaluations", arguments)])
        ).generate_evaluations(_context())
    assert caught.value.code == "MODEL_INVALID_OUTPUT"


@pytest.mark.parametrize(
    ("error", "code", "retryable"),
    [
        (TimeoutError("timeout"), "MODEL_TIMEOUT", True),
        (ConnectionError("offline"), "MODEL_UNAVAILABLE", True),
        (RuntimeError("bad request"), "MODEL_UNAVAILABLE", False),
    ],
)
def test_openai_transport_failures_are_bounded_and_classified(
    error: BaseException,
    code: str,
    retryable: bool,
) -> None:
    client = _Client([error, error, error])
    sleeps: list[float] = []
    with pytest.raises(OpenAIProviderError) as caught:
        OpenAIProvider(client=client, sleeper=sleeps.append).generate_evaluations(_context())
    assert caught.value.code == code
    assert caught.value.retryable is retryable
    assert client.responses.calls == (3 if retryable else 1)
    assert sleeps == ([0.25, 0.5] if retryable else [])


def test_openai_refusal_falls_back_to_text_and_usage_objects_are_normalized() -> None:
    refusal = {
        "output": [
            {
                "type": "message",
                "content": [{"type": "refusal", "text": "policy refusal"}],
            }
        ]
    }
    with pytest.raises(OpenAIProviderError) as caught:
        OpenAIProvider(client=_Client([refusal])).generate_evaluations(_context())
    assert caught.value.code == "MODEL_REFUSAL"
    assert caught.value.details["refusal"] == "policy refusal"

    @dataclass
    class Usage:
        input_tokens: int = 4
        output_tokens: int = 0

        def model_dump(self) -> dict[str, int]:
            return {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
            }

    class Response:
        def __init__(self) -> None:
            self.id = "object-response"
            self.usage = Usage()
            self.output = [
                {
                    "type": "function_call",
                    "name": "submit_evaluations",
                    "arguments": json.dumps(_evaluations()),
                }
            ]

    provider = OpenAIProvider(client=_Client([Response()]))
    provider.generate_evaluations(_context())
    assert provider.last_run is not None
    assert provider.last_run.response_id == "object-response"
    assert provider.last_run.usage == {
        "input_tokens": 4,
        "output_tokens": 0,
        "total_tokens": 4,
    }


@pytest.mark.parametrize(
    "context_kwargs",
    [
        {"session_id": ""},
        {"snapshot_sha256": ""},
        {"candidates": ({"candidate_id": "one"}, {"candidate_id": "one"})},
        {"criteria": ({"criterion_id": "must"}, {"criterion_id": "must"})},
        {"candidates": ({"title": "missing id"},)},
    ],
)
def test_frozen_context_rejects_missing_and_duplicate_ids(context_kwargs: dict[str, Any]) -> None:
    base: dict[str, Any] = {
        "session_id": "demo",
        "snapshot_sha256": "a" * 64,
        "candidates": ({"candidate_id": "one"}, {"candidate_id": "two"}),
        "criteria": ({"criterion_id": "must"},),
    }
    base.update(context_kwargs)
    with pytest.raises(ProviderContractError):
        FrozenDecisionContext(**base)


@pytest.mark.parametrize(
    "patch",
    [
        {"candidate_id": ""},
        {"assessment": "unknown"},
        {"confidence": "certain"},
        {"rationale": "   "},
        {"assessment": "insufficient_evidence", "uncertainties": []},
    ],
)
def test_evaluation_cell_contract_rejects_unsafe_drafts(patch: dict[str, Any]) -> None:
    values: dict[str, Any] = {
        "candidate_id": "one",
        "criterion_id": "must",
        "assessment": "meets",
        "rationale": "Reason",
        "evidence_ids": (),
        "confidence": "medium",
        "uncertainties": (),
    }
    values.update(patch)
    with pytest.raises(ProviderContractError):
        EvaluationCell(**values)


def test_provider_payload_parsers_and_matrix_validation_edges() -> None:
    with pytest.raises(ProviderContractError):
        EvaluationDraft.from_payload({"cells": "not-an-array"})
    with pytest.raises(ProviderContractError):
        EvaluationCell.from_mapping({"candidate_id": "one"})
    invalid = _evaluations()["cells"][0]
    with pytest.raises(ProviderContractError):
        EvaluationCell.from_mapping({**invalid, "evidence_ids": [1]})
    with pytest.raises(ProviderContractError):
        EvaluationCell.from_mapping({**invalid, "uncertainties": [1]})
    with pytest.raises(ProviderContractError):
        EvaluationCell.from_mapping({**invalid, "confidence": 1})

    duplicate = EvaluationDraft.from_payload(
        {"cells": [_evaluations()["cells"][0], _evaluations()["cells"][0]]}
    )
    with pytest.raises(ProviderContractError, match="duplicate"):
        duplicate.validate_against(_context())

    incomplete = EvaluationDraft.from_payload({"cells": [_evaluations()["cells"][0]]})
    with pytest.raises(ProviderContractError, match="full"):
        incomplete.validate_against(_context())

    unknown_evidence = _evaluations()
    unknown_evidence["cells"][1]["evidence_ids"] = ["absent"]
    with pytest.raises(ProviderContractError, match="unknown evidence"):
        EvaluationDraft.from_payload(unknown_evidence).validate_against(_context())


def test_recommendation_and_fixture_validation_edges() -> None:
    for payload in (
        {"disposition": "select"},
        {**_recommendation(), "candidate_id": 1},
        {**_recommendation(), "risks": [1]},
    ):
        with pytest.raises(ProviderContractError):
            RecommendationDraft.from_payload(payload)

    with pytest.raises(ProviderContractError):
        RecommendationDraft("other", None, "Reason", (), (), ())  # type: ignore[arg-type]
    with pytest.raises(ProviderContractError):
        RecommendationDraft("select", None, "Reason", (), (), ())
    with pytest.raises(ProviderContractError):
        RecommendationDraft("abstain", "one", "Reason", (), (), ())
    with pytest.raises(ProviderContractError):
        RecommendationDraft("abstain", None, " ", (), (), ())
    with pytest.raises(ProviderContractError, match="unknown candidate"):
        RecommendationDraft("select", "absent", "Reason", (), (), ()).validate_against(_context())
    with pytest.raises(ProviderContractError, match="unknown evidence"):
        RecommendationDraft("select", "one", "Reason", ("absent",), (), ()).validate_against(
            _context()
        )
    with pytest.raises(ProviderContractError, match="must-eligible"):
        RecommendationDraft("select", "two", "Reason", (), (), ()).validate_against(_context())

    with pytest.raises(ProviderContractError):
        FixtureCellSpec("unknown", ())  # type: ignore[arg-type]
    with pytest.raises(ProviderContractError):
        FixtureCellSpec("meets", (), confidence="certain")  # type: ignore[arg-type]
    with pytest.raises(ProviderContractError):
        FixtureCellSpec("meets", (), rationale=" ")
    with pytest.raises(ProviderContractError):
        FixtureCellSpec("insufficient_evidence", ())
    with pytest.raises(ProviderContractError):
        FixtureProvider(evaluation_map={"not-a-pair": FixtureCellSpec("meets", ())})  # type: ignore[dict-item]
    with pytest.raises(ProviderContractError):
        FixtureProvider(evaluation_map={("one", "must"): object()})  # type: ignore[dict-item]


def test_context_mapping_and_alternate_comparison_shapes() -> None:
    mapped = FrozenDecisionContext.from_mapping(
        {
            "session_id": "mapped",
            "snapshot_sha256": "b" * 64,
            "candidates": [{"id": "one"}, {"id": "two"}],
            "criteria": [{"id": "must"}],
            "comparison": {
                "candidate_results": [
                    {"candidate_id": "one", "must_eligible": True},
                    {"candidate_id": "two", "must_eligible": False},
                ]
            },
        }
    )
    assert mapped.candidate_ids == ("one", "two")
    recommendation = FixtureProvider(recommended_candidate_id=None).generate_recommendation(mapped)
    assert recommendation.candidate_id == "one"
    empty = FrozenDecisionContext.from_mapping(
        {
            "session_id": "mapped",
            "snapshot_sha256": "b" * 64,
            "candidates": [{"id": "one"}, {"id": "two"}],
            "criteria": [{"id": "must"}],
        }
    )
    assert FixtureProvider().generate_recommendation(empty).disposition == "abstain"
