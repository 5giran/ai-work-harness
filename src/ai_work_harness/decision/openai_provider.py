"""Optional OpenAI Responses API adapter for draft-only provider operations.

Importing this module never imports the OpenAI SDK and never performs network I/O.
The SDK is loaded only when a provider without an injected client makes a request.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ai_work_harness.errors import HarnessError

from .prompts import EVALUATION_PROMPT, RECOMMENDATION_PROMPT, PromptTemplate
from .providers import (
    EvaluationDraft,
    EvaluationProvider,
    FrozenDecisionContext,
    RecommendationDraft,
    RecommendationProvider,
)

DEFAULT_MODEL = "gpt-5.6-luna"
DEFAULT_REASONING_EFFORT = "medium"
DEFAULT_MAX_OUTPUT_TOKENS = 8_000
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_MAX_TOOL_CALLS = 12
DEFAULT_MAX_CONTEXT_BYTES = 1_000_000
DEFAULT_MAX_LOOKUP_BYTES = 1_000_000
SUPPORTED_REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})


class OpenAIProviderError(HarnessError):
    """A bounded OpenAI provider failure suitable for CLI exit-code mapping."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        merged = {"retryable": retryable, **(details or {})}
        super().__init__(code, message, details=merged, exit_code=4)
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ProviderRun:
    provider: str
    model: str
    prompt_id: str
    prompt_sha256: str
    tool_name: str
    response_id: str | None
    usage: Mapping[str, Any]
    transcript: tuple[Mapping[str, Any], ...]


_STRING_ARRAY = {"type": "array", "items": {"type": "string"}}
_UNIQUE_STRING_ARRAY = {
    "type": "array",
    "uniqueItems": True,
    "items": {"type": "string"},
}
_NONBLANK_STRING_ARRAY = {
    "type": "array",
    "items": {"type": "string", "minLength": 1, "pattern": ".*\\S.*"},
}


def _lookup_tool(
    *,
    name: str,
    ids_name: str,
    description: str,
) -> dict[str, Any]:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {ids_name: dict(_STRING_ARRAY)},
            "required": [ids_name],
            "additionalProperties": False,
        },
    }


def candidate_lookup_tool() -> dict[str, Any]:
    """Return the read-only candidate lookup tool contract."""

    return _lookup_tool(
        name="get_candidates",
        ids_name="candidate_ids",
        description="Read candidate records by ID. Pass an empty array to read all candidates.",
    )


def criterion_lookup_tool() -> dict[str, Any]:
    """Return the read-only criterion lookup tool contract."""

    return _lookup_tool(
        name="get_criteria",
        ids_name="criterion_ids",
        description="Read criterion records by ID. Pass an empty array to read all criteria.",
    )


def evidence_lookup_tool() -> dict[str, Any]:
    """Return the read-only evidence lookup tool contract."""

    return _lookup_tool(
        name="get_evidence",
        ids_name="evidence_ids",
        description="Read evidence records by ID. Pass an empty array to read all evidence.",
    )


def evaluation_submission_tool() -> dict[str, Any]:
    """Return a fresh strict tool schema for an evaluation draft submission."""

    return {
        "type": "function",
        "name": "submit_evaluations",
        "description": "Submit draft evaluation cells. This does not review or approve them.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "cells": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "candidate_id": {"type": "string"},
                            "criterion_id": {"type": "string"},
                            "assessment": {
                                "type": "string",
                                "enum": [
                                    "meets",
                                    "partial",
                                    "fails",
                                    "insufficient_evidence",
                                    "not_applicable",
                                ],
                            },
                            "rationale": {"type": "string"},
                            "evidence_ids": dict(_UNIQUE_STRING_ARRAY),
                            "confidence": {
                                "type": "string",
                                "enum": ["low", "medium", "high"],
                            },
                            "uncertainties": dict(_NONBLANK_STRING_ARRAY),
                        },
                        "required": [
                            "candidate_id",
                            "criterion_id",
                            "assessment",
                            "rationale",
                            "evidence_ids",
                            "confidence",
                            "uncertainties",
                        ],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["cells"],
            "additionalProperties": False,
        },
    }


def recommendation_submission_tool() -> dict[str, Any]:
    """Return a fresh strict tool schema for a recommendation draft submission."""

    return {
        "type": "function",
        "name": "submit_recommendation",
        "description": "Submit a draft recommendation. This is not a final decision or approval.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "disposition": {"type": "string", "enum": ["select", "abstain"]},
                "candidate_id": {"type": ["string", "null"]},
                "rationale": {"type": "string"},
                "evidence_ids": dict(_UNIQUE_STRING_ARRAY),
                "risks": dict(_NONBLANK_STRING_ARRAY),
                "uncertainties": dict(_NONBLANK_STRING_ARRAY),
            },
            "required": [
                "disposition",
                "candidate_id",
                "rationale",
                "evidence_ids",
                "risks",
                "uncertainties",
            ],
            "additionalProperties": False,
        },
    }


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _strict_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_arguments(arguments: Any) -> Mapping[str, Any]:
    if isinstance(arguments, Mapping):
        return dict(arguments)
    if not isinstance(arguments, str):
        raise ValueError("tool arguments must be a JSON object or JSON string")
    value = json.loads(
        arguments,
        object_pairs_hook=_strict_object_pairs,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"non-finite JSON number: {value}")
        ),
    )
    if not isinstance(value, Mapping):
        raise ValueError("tool arguments must decode to an object")
    return value


def _response_items(response: Any) -> list[Any]:
    output = _get(response, "output", None)
    if not isinstance(output, (list, tuple)):
        raise OpenAIProviderError(
            "MODEL_INVALID_OUTPUT",
            "The model response output must be an array",
        )
    return list(output)


def _response_refusal(response: Any) -> str | None:
    for item in _response_items(response):
        if _get(item, "type") != "message":
            continue
        contents = _get(item, "content", None)
        if not isinstance(contents, (list, tuple)):
            raise OpenAIProviderError(
                "MODEL_INVALID_OUTPUT",
                "A model message content field must be an array",
            )
        for content in contents:
            if _get(content, "type") == "refusal":
                refusal = _get(content, "refusal", _get(content, "text", "Model refused"))
                return str(refusal)
    return None


def _tool_arguments(response: Any, expected_name: str, max_tool_calls: int) -> Mapping[str, Any]:
    output = _get(response, "output", ()) or ()
    calls = [item for item in output if _get(item, "type") == "function_call"]
    if len(calls) > max_tool_calls:
        raise OpenAIProviderError(
            "MODEL_TOOL_CALL_LIMIT",
            "The model exceeded the bounded tool-call limit",
            details={"limit": max_tool_calls, "actual": len(calls)},
        )
    matching = [item for item in calls if _get(item, "name") == expected_name]
    if len(matching) != 1 or len(calls) != 1:
        raise OpenAIProviderError(
            "MODEL_INVALID_OUTPUT",
            "The model must make exactly one expected draft submission tool call",
            details={
                "expected_tool": expected_name,
                "observed_tools": [str(_get(item, "name", "")) for item in calls],
            },
        )
    try:
        return _load_arguments(_get(matching[0], "arguments"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OpenAIProviderError(
            "MODEL_INVALID_OUTPUT",
            "The model returned malformed draft tool arguments",
            details={"expected_tool": expected_name, "reason": str(exc)},
        ) from exc


_LOOKUP_SPECS: dict[str, tuple[str, str, str]] = {
    "get_candidates": ("candidate_ids", "candidate_id", "candidates"),
    "get_criteria": ("criterion_ids", "criterion_id", "criteria"),
    "get_evidence": ("evidence_ids", "evidence_id", "evidence"),
}


def _lookup_output(
    tool_name: str,
    arguments: Mapping[str, Any],
    context_document: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        ids_name, record_id_name, records_name = _LOOKUP_SPECS[tool_name]
    except KeyError as exc:
        raise ValueError(f"unknown lookup tool: {tool_name}") from exc
    if set(arguments) != {ids_name}:
        raise ValueError(f"{tool_name} arguments must contain only {ids_name}")
    requested = arguments[ids_name]
    if not isinstance(requested, list) or not all(isinstance(item, str) for item in requested):
        raise ValueError(f"{ids_name} must be an array of strings")
    if len(set(requested)) != len(requested):
        raise ValueError(f"{ids_name} must not contain duplicates")

    records = context_document.get(records_name)
    if not isinstance(records, list):
        raise ValueError(f"context {records_name} must be an array")
    by_id: dict[str, Mapping[str, Any]] = {}
    for record in records:
        if not isinstance(record, Mapping) or not isinstance(record.get(record_id_name), str):
            raise ValueError(f"context {records_name} contains an invalid record")
        by_id[record[record_id_name]] = record
    selected_ids = sorted(by_id) if not requested else sorted(requested)
    unknown = sorted(set(selected_ids) - by_id.keys())
    if unknown:
        raise ValueError(f"{tool_name} requested unknown IDs: {unknown}")
    return {records_name: [dict(by_id[item]) for item in selected_ids]}


def _response_calls(response: Any) -> list[Any]:
    return [item for item in _response_items(response) if _get(item, "type") == "function_call"]


def _usage_mapping(response: Any) -> dict[str, int]:
    usage = _get(response, "usage", {}) or {}
    if hasattr(usage, "model_dump"):
        usage = usage.model_dump()
    if not isinstance(usage, Mapping):
        return {}
    return {
        key: value
        for key, value in usage.items()
        if key in {"input_tokens", "output_tokens", "total_tokens"}
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    }


def _required_usage(response: Any) -> dict[str, int]:
    usage = _usage_mapping(response)
    missing = sorted({"input_tokens", "output_tokens"} - usage.keys())
    if missing:
        raise OpenAIProviderError(
            "MODEL_USAGE_MISSING",
            "The model response omitted required token usage",
            details={"missing": missing},
        )
    return usage


def _merge_usage(total: dict[str, int], usage: Mapping[str, int]) -> None:
    for key, value in usage.items():
        if key == "total_tokens":
            continue
        total[key] = total.get(key, 0) + value


def validate_tool_transcript(
    transcript: Any,
    *,
    context: FrozenDecisionContext,
    expected_submission: str,
    result_payload: Mapping[str, Any],
) -> None:
    """Revalidate a stored provider transcript without making an external call."""

    if not isinstance(transcript, list) or not transcript:
        raise ValueError("tool transcript must be a non-empty array")
    context_document = context.as_dict()
    submissions = 0
    previous_round = 0
    seen_call_ids: set[str] = set()
    for index, event in enumerate(transcript):
        if not isinstance(event, Mapping):
            raise ValueError("tool transcript events must be objects")
        expected_fields = {"round", "call_id", "tool_name", "arguments"}
        if event.get("tool_name") in _LOOKUP_SPECS:
            expected_fields.add("output")
        if set(event) != expected_fields:
            raise ValueError("tool transcript event has missing or undeclared fields")
        round_number = event["round"]
        if (
            not isinstance(round_number, int)
            or isinstance(round_number, bool)
            or round_number < 1
            or round_number > DEFAULT_MAX_TOOL_CALLS
            or (previous_round == 0 and round_number != 1)
            or (previous_round > 0 and round_number not in {previous_round, previous_round + 1})
        ):
            raise ValueError("tool transcript round is invalid")
        previous_round = round_number
        call_id = event["call_id"]
        if call_id is not None and not isinstance(call_id, str):
            raise ValueError("tool transcript call_id must be a string or null")
        if isinstance(call_id, str):
            if not call_id or call_id in seen_call_ids:
                raise ValueError("tool transcript call_id must be non-empty and unique")
            seen_call_ids.add(call_id)
        tool_name = event["tool_name"]
        arguments = event["arguments"]
        if not isinstance(tool_name, str) or not isinstance(arguments, Mapping):
            raise ValueError("tool transcript name and arguments are invalid")
        if tool_name in _LOOKUP_SPECS:
            if not call_id:
                raise ValueError("lookup transcript events require call_id")
            expected_output = _lookup_output(tool_name, arguments, context_document)
            if event["output"] != expected_output:
                raise ValueError("lookup transcript output does not match the frozen context")
            continue
        if tool_name != expected_submission or index != len(transcript) - 1:
            raise ValueError("tool transcript contains an unknown or misplaced submission")
        if index > 0 and transcript[index - 1].get("round") == round_number:
            raise ValueError("draft submission cannot share a round with lookup calls")
        submissions += 1
        if dict(arguments) != dict(result_payload):
            raise ValueError("tool transcript submission does not match the stored result")
    if submissions != 1:
        raise ValueError("tool transcript must end in exactly one submission")


def _is_retryable(exc: BaseException) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code == 429 or (isinstance(status_code, int) and status_code >= 500):
        return True
    return isinstance(exc, (ConnectionError, TimeoutError, OSError)) or exc.__class__.__name__ in {
        "APIConnectionError",
        "APITimeoutError",
        "RateLimitError",
    }


def _error_code(exc: BaseException) -> str:
    status_code = getattr(exc, "status_code", None)
    if status_code == 429 or exc.__class__.__name__ == "RateLimitError":
        return "MODEL_RATE_LIMIT"
    if isinstance(exc, TimeoutError) or exc.__class__.__name__ == "APITimeoutError":
        return "MODEL_TIMEOUT"
    return "MODEL_UNAVAILABLE"


class _OpenAIEvaluationAdapter:
    def __init__(self, provider: OpenAIProvider) -> None:
        self._provider = provider

    def generate(self, context: FrozenDecisionContext) -> EvaluationDraft:
        return self._provider.generate_evaluations(context)


class _OpenAIRecommendationAdapter:
    def __init__(self, provider: OpenAIProvider) -> None:
        self._provider = provider

    def generate(self, context: FrozenDecisionContext) -> RecommendationDraft:
        return self._provider.generate_recommendation(context)


class OpenAIProvider:
    """Responses API adapter with explicit limits and no persistence authority."""

    def __init__(
        self,
        *,
        client: Any | None = None,
        client_factory: Callable[[], Any] | None = None,
        model: str | None = None,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
        max_context_bytes: int = DEFAULT_MAX_CONTEXT_BYTES,
        max_lookup_bytes: int = DEFAULT_MAX_LOOKUP_BYTES,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if client is not None and client_factory is not None:
            raise ValueError("Pass either client or client_factory, not both")
        if max_retries < 0 or max_retries > DEFAULT_MAX_RETRIES:
            raise ValueError(f"max_retries must be between 0 and {DEFAULT_MAX_RETRIES}")
        if max_tool_calls < 1 or max_tool_calls > DEFAULT_MAX_TOOL_CALLS:
            raise ValueError(f"max_tool_calls must be between 1 and {DEFAULT_MAX_TOOL_CALLS}")
        if max_output_tokens < 1 or max_output_tokens > DEFAULT_MAX_OUTPUT_TOKENS:
            raise ValueError(f"max_output_tokens must be between 1 and {DEFAULT_MAX_OUTPUT_TOKENS}")
        if timeout_seconds <= 0 or timeout_seconds > DEFAULT_TIMEOUT_SECONDS:
            raise ValueError(
                f"timeout_seconds must be greater than 0 and at most {DEFAULT_TIMEOUT_SECONDS}"
            )
        if reasoning_effort not in SUPPORTED_REASONING_EFFORTS:
            raise ValueError("reasoning_effort is not supported")
        if max_context_bytes < 1 or max_context_bytes > DEFAULT_MAX_CONTEXT_BYTES:
            raise ValueError(f"max_context_bytes must be between 1 and {DEFAULT_MAX_CONTEXT_BYTES}")
        if max_lookup_bytes < 1 or max_lookup_bytes > DEFAULT_MAX_LOOKUP_BYTES:
            raise ValueError(f"max_lookup_bytes must be between 1 and {DEFAULT_MAX_LOOKUP_BYTES}")
        self.model = model or os.environ.get("AI_WORK_HARNESS_OPENAI_MODEL", DEFAULT_MODEL)
        self.reasoning_effort = reasoning_effort
        self.max_output_tokens = max_output_tokens
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.max_tool_calls = max_tool_calls
        self.max_context_bytes = max_context_bytes
        self.max_lookup_bytes = max_lookup_bytes
        self._client = client
        self._client_factory = client_factory
        self._sleeper = sleeper
        self.last_run: ProviderRun | None = None

    def evaluation_provider(self) -> EvaluationProvider:
        return _OpenAIEvaluationAdapter(self)

    def recommendation_provider(self) -> RecommendationProvider:
        return _OpenAIRecommendationAdapter(self)

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if self._client_factory is not None:
            self._client = self._client_factory()
            return self._client
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - depends on optional installation
            raise OpenAIProviderError(
                "MODEL_SDK_UNAVAILABLE",
                "The optional OpenAI SDK is not installed",
                details={"install_extra": "openai"},
            ) from exc
        self._client = OpenAI(timeout=self.timeout_seconds, max_retries=0)
        return self._client

    def _validate_outbound_limits(self, context: FrozenDecisionContext) -> None:
        if len(context.evidence) > 50:
            raise OpenAIProviderError(
                "OUTBOUND_LIMIT_EXCEEDED",
                "OpenAI runs are limited to 50 evidence records",
                details={"evidence_count": len(context.evidence), "limit": 50},
            )
        excerpt_bytes = 0
        for evidence in context.evidence:
            excerpt = evidence.get("cited_excerpt", "")
            if not isinstance(excerpt, str):
                raise OpenAIProviderError(
                    "OUTBOUND_LIMIT_EXCEEDED",
                    "Cited source excerpts must be UTF-8 text",
                )
            excerpt_bytes += len(excerpt.encode("utf-8"))
        if excerpt_bytes > 100_000:
            raise OpenAIProviderError(
                "OUTBOUND_LIMIT_EXCEEDED",
                "OpenAI runs are limited to 100,000 bytes of cited source excerpts",
                details={"source_excerpt_bytes": excerpt_bytes, "limit": 100_000},
            )
        context_bytes = len(
            json.dumps(
                context.as_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
        if context_bytes > self.max_context_bytes:
            raise OpenAIProviderError(
                "OUTBOUND_LIMIT_EXCEEDED",
                "The frozen decision context exceeds the outbound byte limit",
                details={"context_bytes": context_bytes, "limit": self.max_context_bytes},
            )

    def _request(self, request: Mapping[str, Any]) -> Any:
        try:
            client = self._get_client()
        except OpenAIProviderError:
            raise
        except Exception as exc:
            raise OpenAIProviderError(
                "MODEL_UNAVAILABLE",
                "The OpenAI client could not be initialized",
                retryable=False,
                details={
                    "attempts": 1,
                    "exception_type": exc.__class__.__name__,
                    "phase": "client_initialization",
                },
            ) from exc
        for attempt in range(self.max_retries + 1):
            try:
                return client.responses.create(**dict(request))
            except Exception as exc:
                retryable = _is_retryable(exc)
                if retryable and attempt < self.max_retries:
                    self._sleeper(0.25 * (2**attempt))
                    continue
                raise OpenAIProviderError(
                    _error_code(exc),
                    "The OpenAI Responses request failed",
                    retryable=retryable,
                    details={
                        "attempts": attempt + 1,
                        "status_code": getattr(exc, "status_code", None),
                        "exception_type": exc.__class__.__name__,
                    },
                ) from exc
        raise AssertionError("bounded retry loop did not return or raise")

    def _record_run(
        self,
        response: Any,
        *,
        prompt: PromptTemplate,
        tool_name: str,
        usage: Mapping[str, int],
        transcript: list[dict[str, Any]],
    ) -> None:
        self.last_run = ProviderRun(
            provider="openai",
            model=self.model,
            prompt_id=prompt.prompt_id,
            prompt_sha256=prompt.sha256,
            tool_name=tool_name,
            response_id=_get(response, "id"),
            usage=dict(usage),
            transcript=tuple(dict(event) for event in transcript),
        )

    def _ensure_not_refused(self, response: Any) -> None:
        refusal = _response_refusal(response)
        if refusal is not None:
            raise OpenAIProviderError(
                "MODEL_REFUSAL",
                "The model refused to produce a draft",
                details={"refusal": refusal},
            )

    def _run_tool_loop(
        self,
        context: FrozenDecisionContext,
        *,
        prompt: PromptTemplate,
        submission_tool: dict[str, Any],
    ) -> tuple[Mapping[str, Any], Any]:
        self.last_run = None
        self._validate_outbound_limits(context)
        context_document = context.as_dict()
        initial_context = {
            "session_id": context.session_id,
            "snapshot_sha256": context.snapshot_sha256,
            "candidate_ids": context.candidate_ids,
            "criterion_ids": context.criterion_ids,
            "evidence_ids": sorted(
                str(item["evidence_id"])
                for item in context_document["evidence"]
                if isinstance(item, Mapping) and "evidence_id" in item
            ),
            "comparison": context_document["comparison"],
        }
        initial_json = json.dumps(
            initial_context,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        input_items: list[Any] = [{"role": "user", "content": initial_json}]
        tools = [
            candidate_lookup_tool(),
            criterion_lookup_tool(),
            evidence_lookup_tool(),
            submission_tool,
        ]
        transcript: list[dict[str, Any]] = []
        usage: dict[str, int] = {}
        output_tokens_used = 0
        lookup_bytes_used = 0

        for round_number in range(1, self.max_tool_calls + 1):
            remaining_output_tokens = self.max_output_tokens - output_tokens_used
            if remaining_output_tokens < 1:
                raise OpenAIProviderError(
                    "MODEL_OUTPUT_LIMIT",
                    "The model exhausted the bounded output-token budget before submission",
                    details={"limit": self.max_output_tokens},
                )
            request = {
                "model": self.model,
                "instructions": prompt.text,
                "input": input_items,
                "reasoning": {"effort": self.reasoning_effort},
                "store": False,
                "include": ["reasoning.encrypted_content"],
                "max_output_tokens": remaining_output_tokens,
                "tools": tools,
                "tool_choice": "required",
                "timeout": self.timeout_seconds,
            }
            response = self._request(request)
            self._ensure_not_refused(response)
            calls = _response_calls(response)
            if not calls:
                raise OpenAIProviderError(
                    "MODEL_INVALID_OUTPUT",
                    "The model returned no lookup or draft submission tool call",
                )

            submission_calls = [
                call for call in calls if _get(call, "name") == submission_tool["name"]
            ]
            if submission_calls:
                if len(calls) != 1 or len(submission_calls) != 1:
                    raise OpenAIProviderError(
                        "MODEL_INVALID_OUTPUT",
                        "A draft submission cannot be mixed with other tool calls",
                        details={"observed_tools": [str(_get(call, "name", "")) for call in calls]},
                    )
                try:
                    payload = _load_arguments(_get(submission_calls[0], "arguments"))
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise OpenAIProviderError(
                        "MODEL_INVALID_OUTPUT",
                        "The model returned malformed draft tool arguments",
                        details={"expected_tool": submission_tool["name"], "reason": str(exc)},
                    ) from exc
                response_usage = _required_usage(response)
                if response_usage["output_tokens"] > remaining_output_tokens:
                    raise OpenAIProviderError(
                        "MODEL_OUTPUT_LIMIT",
                        "The model exceeded the bounded output-token budget",
                        details={
                            "limit": self.max_output_tokens,
                            "observed": output_tokens_used + response_usage["output_tokens"],
                        },
                    )
                _merge_usage(usage, response_usage)
                output_tokens_used += response_usage["output_tokens"]
                transcript.append(
                    {
                        "round": round_number,
                        "call_id": _get(submission_calls[0], "call_id"),
                        "tool_name": submission_tool["name"],
                        "arguments": dict(payload),
                    }
                )
                usage["total_tokens"] = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                self._record_run(
                    response,
                    prompt=prompt,
                    tool_name=submission_tool["name"],
                    usage=usage,
                    transcript=transcript,
                )
                return payload, response

            function_outputs: list[dict[str, Any]] = []
            for call in calls:
                tool_name = _get(call, "name")
                call_id = _get(call, "call_id")
                if tool_name not in _LOOKUP_SPECS or not isinstance(call_id, str) or not call_id:
                    raise OpenAIProviderError(
                        "MODEL_INVALID_OUTPUT",
                        "The model called an unknown tool or omitted its call ID",
                        details={"tool_name": tool_name},
                    )
                try:
                    arguments = _load_arguments(_get(call, "arguments"))
                    output = _lookup_output(tool_name, arguments, context_document)
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise OpenAIProviderError(
                        "MODEL_INVALID_OUTPUT",
                        "The model returned invalid lookup arguments",
                        details={"tool_name": tool_name, "reason": str(exc)},
                    ) from exc
                output_json = json.dumps(
                    output,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                output_bytes = len(output_json.encode("utf-8"))
                if lookup_bytes_used + output_bytes > self.max_lookup_bytes:
                    raise OpenAIProviderError(
                        "MODEL_LOOKUP_LIMIT",
                        "The model exceeded the cumulative lookup-output byte limit",
                        details={
                            "limit": self.max_lookup_bytes,
                            "observed": lookup_bytes_used + output_bytes,
                        },
                    )
                lookup_bytes_used += output_bytes
                transcript.append(
                    {
                        "round": round_number,
                        "call_id": call_id,
                        "tool_name": tool_name,
                        "arguments": dict(arguments),
                        "output": output,
                    }
                )
                function_outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": output_json,
                    }
                )
            response_usage = _required_usage(response)
            if response_usage["output_tokens"] > remaining_output_tokens:
                raise OpenAIProviderError(
                    "MODEL_OUTPUT_LIMIT",
                    "The model exceeded the bounded output-token budget",
                    details={
                        "limit": self.max_output_tokens,
                        "observed": output_tokens_used + response_usage["output_tokens"],
                    },
                )
            _merge_usage(usage, response_usage)
            output_tokens_used += response_usage["output_tokens"]
            input_items.extend(_response_items(response))
            input_items.extend(function_outputs)

        raise OpenAIProviderError(
            "MODEL_TOOL_CALL_LIMIT",
            "The model exceeded the bounded tool-round limit before submission",
            details={"limit": self.max_tool_calls},
        )

    def generate_evaluations(self, context: FrozenDecisionContext) -> EvaluationDraft:
        tool = evaluation_submission_tool()
        payload, _ = self._run_tool_loop(
            context,
            prompt=EVALUATION_PROMPT,
            submission_tool=tool,
        )
        try:
            draft = EvaluationDraft.from_payload(payload)
            draft.validate_against(context)
        except HarnessError as exc:
            raise OpenAIProviderError(
                "MODEL_INVALID_OUTPUT",
                "The model evaluation draft failed the provider contract",
                details={"reason": exc.message, "contract_code": exc.code},
            ) from exc
        return draft

    def generate_recommendation(self, context: FrozenDecisionContext) -> RecommendationDraft:
        tool = recommendation_submission_tool()
        payload, _ = self._run_tool_loop(
            context,
            prompt=RECOMMENDATION_PROMPT,
            submission_tool=tool,
        )
        try:
            draft = RecommendationDraft.from_payload(payload)
            draft.validate_against(context)
        except HarnessError as exc:
            raise OpenAIProviderError(
                "MODEL_INVALID_OUTPUT",
                "The model recommendation draft failed the provider contract",
                details={"reason": exc.message, "contract_code": exc.code},
            ) from exc
        return draft


__all__ = [
    "DEFAULT_MAX_CONTEXT_BYTES",
    "DEFAULT_MAX_LOOKUP_BYTES",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_MAX_TOOL_CALLS",
    "DEFAULT_MODEL",
    "DEFAULT_REASONING_EFFORT",
    "DEFAULT_TIMEOUT_SECONDS",
    "OpenAIProvider",
    "OpenAIProviderError",
    "ProviderRun",
    "candidate_lookup_tool",
    "criterion_lookup_tool",
    "evaluation_submission_tool",
    "evidence_lookup_tool",
    "recommendation_submission_tool",
    "validate_tool_transcript",
]
