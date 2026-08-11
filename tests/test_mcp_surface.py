from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from ai_work_harness.decision.mcp_server import (
    FORBIDDEN_HUMAN_GATE_TERMS,
    MCP_TOOL_NAMES,
    MUTATING_TOOL_NAMES,
    PUBLIC_TOOL_SPECS,
    LocalDecisionBackend,
    McpDispatcher,
    McpSurfaceError,
    ToolRegistry,
    ToolSpec,
)

EXPECTED_TOOLS = {
    "decision_create_session",
    "decision_get_status",
    "decision_list_sources",
    "decision_get_source_excerpt",
    "decision_draft_frame",
    "decision_draft_candidates",
    "decision_draft_criteria",
    "decision_draft_evaluations",
    "decision_compare",
    "decision_record_recommendation",
    "decision_verify",
}


class _Backend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def invoke(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        return {"tool": name, "arguments": arguments}


def test_mcp_registry_is_an_exact_draft_read_allowlist() -> None:
    assert set(MCP_TOOL_NAMES) == EXPECTED_TOOLS
    assert len(MCP_TOOL_NAMES) == len(set(MCP_TOOL_NAMES))
    assert all(
        not any(term in name for term in FORBIDDEN_HUMAN_GATE_TERMS) for name in MCP_TOOL_NAMES
    )
    assert not {
        "decision_confirm",
        "decision_review",
        "decision_final",
        "decision_challenge",
        "decision_approval",
        "decision_approve",
    }.intersection(MCP_TOOL_NAMES)


def test_all_mutations_require_cas_and_idempotency() -> None:
    for spec in PUBLIC_TOOL_SPECS:
        properties = spec.input_schema["properties"]
        required = set(spec.input_schema["required"])
        assert spec.input_schema["additionalProperties"] is False
        if spec.name in MUTATING_TOOL_NAMES:
            assert {"expected_parent", "idempotency_key"} <= required
            assert {"expected_parent", "idempotency_key"} <= properties.keys()
            assert spec.annotations["readOnlyHint"] is False
        else:
            assert spec.annotations["readOnlyHint"] is True
        assert spec.annotations["destructiveHint"] is (
            spec.name in MUTATING_TOOL_NAMES and spec.name != "decision_create_session"
        )
        assert spec.annotations["openWorldHint"] is False


def test_idempotency_key_schema_matches_the_core_operation_contract() -> None:
    for spec in PUBLIC_TOOL_SPECS:
        if spec.name not in MUTATING_TOOL_NAMES:
            continue
        assert spec.input_schema["properties"]["idempotency_key"] == {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
        }


def test_candidate_draft_schema_requires_agent_provenance() -> None:
    spec = next(item for item in PUBLIC_TOOL_SPECS if item.name == "decision_draft_candidates")
    candidate = spec.input_schema["properties"]["candidates"]["items"]

    assert candidate["properties"]["proposed_by"] == {
        "type": "string",
        "const": "agent",
    }

    backend = _Backend()
    dispatcher = McpDispatcher(backend)
    human_candidate = {
        "candidate_id": "one",
        "title": "One",
        "summary": "Human-proposed option.",
        "proposed_by": "human",
        "benefits": [],
        "drawbacks": [],
        "risks": [],
        "uncertainties": [],
    }
    with pytest.raises(McpSurfaceError) as caught:
        dispatcher.dispatch(
            "decision_draft_candidates",
            {
                "session_id": "demo",
                "expected_parent": "a" * 64,
                "idempotency_key": "candidate-agent-only",
                "candidates": [human_candidate, {**human_candidate, "candidate_id": "two"}],
            },
        )

    assert caught.value.code == "MCP_INVALID_ARGUMENTS"
    assert backend.calls == []


def test_dispatcher_validates_before_backend_invocation() -> None:
    backend = _Backend()
    dispatcher = McpDispatcher(backend)

    with pytest.raises(McpSurfaceError) as unknown:
        dispatcher.dispatch("decision_approval", {})
    assert unknown.value.code == "MCP_TOOL_NOT_ALLOWED"

    with pytest.raises(McpSurfaceError) as extra:
        dispatcher.dispatch("decision_get_status", {"session_id": "demo", "approve": True})
    assert extra.value.code == "MCP_INVALID_ARGUMENTS"
    assert backend.calls == []


def test_dispatcher_passes_only_validated_arguments() -> None:
    backend = _Backend()
    dispatcher = McpDispatcher(backend)

    result = dispatcher.dispatch("decision_get_status", {"session_id": "demo"})

    assert result["tool"] == "decision_get_status"
    assert backend.calls == [("decision_get_status", {"session_id": "demo"})]


def test_mutation_rejects_missing_or_malformed_cas_fields() -> None:
    backend = _Backend()
    dispatcher = McpDispatcher(backend)
    valid = {
        "session_id": "demo",
        "expected_parent": "a" * 64,
        "idempotency_key": "request-1",
    }

    result = dispatcher.dispatch("decision_compare", valid)
    assert result["tool"] == "decision_compare"

    for key in ("expected_parent", "idempotency_key"):
        invalid = dict(valid)
        del invalid[key]
        with pytest.raises(McpSurfaceError, match="closed input schema"):
            dispatcher.dispatch("decision_compare", invalid)

    invalid_digest = {**valid, "expected_parent": "ABC"}
    with pytest.raises(McpSurfaceError, match="closed input schema"):
        dispatcher.dispatch("decision_compare", invalid_digest)

    invalid_key = {**valid, "idempotency_key": "contains spaces"}
    with pytest.raises(McpSurfaceError, match="closed input schema"):
        dispatcher.dispatch("decision_compare", invalid_key)


def test_create_session_explicitly_binds_null_parent() -> None:
    dispatcher = McpDispatcher(_Backend())
    result = dispatcher.dispatch(
        "decision_create_session",
        {"session_id": "demo", "expected_parent": None, "idempotency_key": "create-1"},
    )
    assert result["arguments"]["expected_parent"] is None

    with pytest.raises(McpSurfaceError):
        dispatcher.dispatch(
            "decision_create_session",
            {
                "session_id": "demo",
                "expected_parent": "a" * 64,
                "idempotency_key": "create-2",
            },
        )


def test_registry_refuses_human_gate_tool_even_if_custom_supplied() -> None:
    bad = ToolSpec(
        name="decision_final_approve",
        description="forbidden",
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        read_only=False,
        idempotent=True,
    )
    with pytest.raises(ValueError, match="forbidden"):
        ToolRegistry((bad,))


def test_async_backend_is_supported_by_async_dispatch() -> None:
    class _AsyncBackend:
        async def invoke(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            return {"tool": name, "session_id": arguments["session_id"]}

    result = asyncio.run(
        McpDispatcher(_AsyncBackend()).dispatch_async("decision_get_status", {"session_id": "demo"})
    )
    assert result == {"tool": "decision_get_status", "session_id": "demo"}


def test_local_backend_replays_idempotent_session_creation(tmp_path: Path) -> None:
    dispatcher = McpDispatcher(LocalDecisionBackend(tmp_path))
    arguments = {
        "session_id": "mcp-demo",
        "expected_parent": None,
        "idempotency_key": "create-once",
    }

    first = dispatcher.dispatch("decision_create_session", arguments)
    second = dispatcher.dispatch("decision_create_session", arguments)

    assert first["snapshot_sha256"] == second["snapshot_sha256"]
    assert first["generation"] == second["generation"] == 0
