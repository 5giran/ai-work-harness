from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from ai_work_harness.decision.canonical import sha256_bytes
from ai_work_harness.decision.mcp_server import (
    MCP_TOOL_NAMES,
    LocalDecisionBackend,
    McpDispatcher,
    McpSurfaceError,
    ToolRegistry,
    ToolSpec,
    create_stdio_server,
    run_stdio,
    serve_stdio,
)
from ai_work_harness.decision.service import DecisionService
from ai_work_harness.decision.store import DecisionStore
from ai_work_harness.errors import HarnessError


def _mutating(session_id: str, parent: str, key: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "expected_parent": parent,
        "idempotency_key": key,
    }


def _frame(statement: str = "Choose a bounded local workflow.") -> dict[str, Any]:
    return {
        "user_statement_verbatim": statement,
        "ai_initial_interpretation": "Compare two operating choices.",
        "business_user": "operator",
        "blocked_decision": "which workflow to use",
        "problem_statement": "Choose one local workflow.",
        "scope_in": ["synthetic evidence"],
        "scope_out": ["production data"],
        "assumptions": [],
        "open_questions": [],
    }


def _candidates(proposed_by: str = "agent") -> list[dict[str, Any]]:
    return [
        {
            "candidate_id": candidate,
            "title": candidate.title(),
            "summary": f"Use {candidate}.",
            "proposed_by": proposed_by,
            "benefits": ["bounded"],
            "drawbacks": [],
            "risks": [],
            "uncertainties": [],
        }
        for candidate in ("one", "two")
    ]


@pytest.mark.parametrize("proposed_by", ["human", "fixture"])
def test_local_backend_rejects_non_agent_candidate_provenance_before_state_access(
    tmp_path: Path,
    proposed_by: str,
) -> None:
    backend = LocalDecisionBackend(tmp_path)

    with pytest.raises(McpSurfaceError) as caught:
        backend.invoke(
            "decision_draft_candidates",
            {
                **_mutating("missing-session", "a" * 64, "agent-only"),
                "candidates": _candidates(proposed_by),
            },
        )

    assert caught.value.code == "MCP_AGENT_PROVENANCE_REQUIRED"
    assert not (tmp_path / ".ai-work-harness").exists()


def _criterion() -> list[dict[str, Any]]:
    return [
        {
            "criterion_id": "privacy",
            "title": "Privacy",
            "definition": "Keep synthetic text local.",
            "priority": "must",
        }
    ]


def _reviews() -> dict[str, Any]:
    return {
        "reviews": [
            {
                "candidate_id": candidate,
                "criterion_id": "privacy",
                "outcome": "concur",
                "reason": "The cited source line was reviewed.",
            }
            for candidate in ("one", "two")
        ]
    }


def test_local_mcp_backend_executes_only_draft_and_read_operations(tmp_path: Path) -> None:
    session_id = "mcp-flow"
    source = tmp_path / "facts.md"
    source.write_text("Both options keep this synthetic text local.\n", encoding="utf-8")
    service = DecisionService(tmp_path, session_id)
    parent = service.initialize()["snapshot_sha256"]
    parent = service.capture_source(
        source_id="facts",
        source=source,
        expected_parent=parent,
        media_type="text/markdown",
    )["snapshot_sha256"]
    dispatcher = McpDispatcher(LocalDecisionBackend(tmp_path))

    listed = dispatcher.dispatch("decision_list_sources", {"session_id": session_id})
    assert listed["sources"] == [
        {
            "source_id": "facts",
            "media_type": "text/markdown",
            "bytes": source.stat().st_size,
            "blob_sha256": sha256_bytes(source.read_bytes()),
        }
    ]
    excerpt = dispatcher.dispatch(
        "decision_get_source_excerpt",
        {"session_id": session_id, "source_id": "facts", "start_line": 1, "end_line": 1},
    )
    assert excerpt["excerpt"] == source.read_text(encoding="utf-8")
    assert excerpt["excerpt_sha256"] == sha256_bytes(source.read_bytes())

    frame_args = {**_mutating(session_id, parent, "frame-once"), "frame": _frame()}
    first = dispatcher.dispatch("decision_draft_frame", frame_args)
    replay = dispatcher.dispatch("decision_draft_frame", frame_args)
    assert replay["snapshot_sha256"] == first["snapshot_sha256"]
    parent = first["snapshot_sha256"]

    with pytest.raises(HarnessError) as conflict:
        dispatcher.dispatch(
            "decision_draft_frame",
            {
                **_mutating(session_id, frame_args["expected_parent"], "frame-once"),
                "frame": _frame("A different request under the same key."),
            },
        )
    assert conflict.value.code == "IDEMPOTENCY_KEY_REUSE"

    frame_sha = DecisionService(tmp_path, session_id).status()["refs"]["decision_frame"]
    parent = DecisionService(tmp_path, session_id).confirm(
        "decision-frame",
        expected_artifact_sha=frame_sha,
        expected_parent=parent,
    )["snapshot_sha256"]
    historical_replay = dispatcher.dispatch("decision_draft_frame", frame_args)
    assert historical_replay["snapshot_sha256"] == first["snapshot_sha256"]
    assert DecisionService(tmp_path, session_id).status()["snapshot_sha256"] == parent

    candidate_result = dispatcher.dispatch(
        "decision_draft_candidates",
        {**_mutating(session_id, parent, "candidate-once"), "candidates": _candidates()},
    )
    parent = candidate_result["snapshot_sha256"]
    candidate_sha = DecisionService(tmp_path, session_id).status()["refs"]["candidate_set"]
    parent = DecisionService(tmp_path, session_id).confirm(
        "candidate-set",
        expected_artifact_sha=candidate_sha,
        expected_parent=parent,
    )["snapshot_sha256"]

    criteria_result = dispatcher.dispatch(
        "decision_draft_criteria",
        {**_mutating(session_id, parent, "criteria-once"), "criteria": _criterion()},
    )
    parent = criteria_result["snapshot_sha256"]
    criteria_sha = DecisionService(tmp_path, session_id).status()["refs"]["criteria_set"]
    parent = DecisionService(tmp_path, session_id).confirm(
        "criteria-set",
        expected_artifact_sha=criteria_sha,
        expected_parent=parent,
    )["snapshot_sha256"]

    evidence = {
        "evidence": [
            {
                "evidence_id": f"evidence-{candidate}",
                "claim": f"{candidate} keeps the source text local.",
                "provenance": "source_observation",
                "source": {
                    "source_id": "facts",
                    "start_line": 1,
                    "end_line": 1,
                    "excerpt_sha256": sha256_bytes(source.read_bytes()),
                },
            }
            for candidate in ("one", "two")
        ]
    }
    parent = DecisionService(tmp_path, session_id).import_evidence(
        evidence,
        expected_parent=parent,
    )["snapshot_sha256"]
    cells = [
        {
            "candidate_id": candidate,
            "criterion_id": "privacy",
            "assessment": "meets",
            "rationale": "The local source supports this draft.",
            "evidence_ids": [f"evidence-{candidate}"],
            "confidence": "high",
            "uncertainties": [],
        }
        for candidate in ("one", "two")
    ]
    evaluation_result = dispatcher.dispatch(
        "decision_draft_evaluations",
        {**_mutating(session_id, parent, "evaluation-once"), "cells": cells},
    )
    parent = evaluation_result["snapshot_sha256"]
    parent = DecisionService(tmp_path, session_id).import_reviews(
        _reviews(),
        expected_parent=parent,
    )["snapshot_sha256"]

    comparison = dispatcher.dispatch(
        "decision_compare",
        _mutating(session_id, parent, "compare-once"),
    )
    assert comparison["eligible_candidate_ids"] == ["one", "two"]
    parent = comparison["snapshot_sha256"]
    recommendation = dispatcher.dispatch(
        "decision_record_recommendation",
        {
            **_mutating(session_id, parent, "recommend-once"),
            "recommendation": {
                "disposition": "select",
                "candidate_id": "one",
                "rationale": "One is must eligible.",
                "evidence_ids": ["evidence-one"],
                "risks": [],
                "uncertainties": [],
            },
        },
    )
    parent = recommendation["snapshot_sha256"]
    status = dispatcher.dispatch("decision_get_status", {"session_id": session_id})
    assert status["snapshot_sha256"] == parent
    verified = dispatcher.dispatch(
        "decision_verify",
        {"session_id": session_id, "snapshot_sha256": parent},
    )
    assert verified["verified"] is True


def test_concurrent_exact_idempotent_replay_returns_the_original_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "mcp-idempotency-race"
    source = tmp_path / "race-source.md"
    source.write_text("Synthetic source for a concurrent draft.\n", encoding="utf-8")
    service = DecisionService(tmp_path, session_id)
    parent = service.initialize()["snapshot_sha256"]
    parent = service.capture_source(
        source_id="facts",
        source=source,
        expected_parent=parent,
    )["snapshot_sha256"]
    arguments = {**_mutating(session_id, parent, "same-race-key"), "frame": _frame()}
    barrier = threading.Barrier(2)
    original = DecisionStore.find_idempotent_result

    def synchronize_early_check(self: DecisionStore, operation: Any) -> Any:
        barrier.wait()
        return original(self, operation)

    monkeypatch.setattr(DecisionStore, "find_idempotent_result", synchronize_early_check)

    def invoke() -> dict[str, Any]:
        return McpDispatcher(LocalDecisionBackend(tmp_path)).dispatch(
            "decision_draft_frame",
            arguments,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: invoke(), range(2)))

    assert results[0]["snapshot_sha256"] == results[1]["snapshot_sha256"]
    assert results[0]["artifact_sha256"] == results[1]["artifact_sha256"]
    status = DecisionService(tmp_path, session_id).status()
    assert status["refs"]["decision_frame"] == results[0]["artifact_sha256"]


def test_local_source_reads_fail_closed_for_missing_unknown_and_oversized_ranges(
    tmp_path: Path,
) -> None:
    dispatcher = McpDispatcher(LocalDecisionBackend(tmp_path))
    dispatcher.dispatch(
        "decision_create_session",
        {"session_id": "empty", "expected_parent": None, "idempotency_key": "create"},
    )
    with pytest.raises(McpSurfaceError) as missing:
        dispatcher.dispatch("decision_list_sources", {"session_id": "empty"})
    assert missing.value.code == "MCP_SOURCE_REQUIRED"

    source = tmp_path / "large.txt"
    source.write_text("x" * 100_001 + "\n", encoding="utf-8")
    service = DecisionService(tmp_path, "large")
    parent = service.initialize()["snapshot_sha256"]
    service.capture_source(source_id="large", source=source, expected_parent=parent)

    with pytest.raises(McpSurfaceError) as unknown:
        dispatcher.dispatch(
            "decision_get_source_excerpt",
            {"session_id": "large", "source_id": "absent", "start_line": 1, "end_line": 1},
        )
    assert unknown.value.code == "MCP_SOURCE_NOT_FOUND"
    for start, end in ((2, 1), (1, 2)):
        with pytest.raises(McpSurfaceError) as invalid:
            dispatcher.dispatch(
                "decision_get_source_excerpt",
                {
                    "session_id": "large",
                    "source_id": "large",
                    "start_line": start,
                    "end_line": end,
                },
            )
        assert invalid.value.code == "MCP_INVALID_SOURCE_RANGE"
    with pytest.raises(McpSurfaceError) as too_large:
        dispatcher.dispatch(
            "decision_get_source_excerpt",
            {"session_id": "large", "source_id": "large", "start_line": 1, "end_line": 1},
        )
    assert too_large.value.code == "MCP_SOURCE_EXCERPT_TOO_LARGE"


def test_dispatcher_fails_closed_for_async_misuse_and_non_object_results() -> None:
    class AwaitableResult:
        def __await__(self):
            yield
            return {"ok": True}

    class AsyncBackend:
        def invoke(self, _name: str, _arguments: dict[str, Any]) -> AwaitableResult:
            return AwaitableResult()

    with pytest.raises(McpSurfaceError) as async_error:
        McpDispatcher(AsyncBackend()).dispatch("decision_get_status", {"session_id": "demo"})
    assert async_error.value.code == "MCP_ASYNC_BACKEND_REQUIRED"

    class InvalidBackend:
        def invoke(self, _name: str, _arguments: dict[str, Any]) -> list[Any]:
            return []

    dispatcher = McpDispatcher(InvalidBackend())
    with pytest.raises(McpSurfaceError) as sync_error:
        dispatcher.dispatch("decision_get_status", {"session_id": "demo"})
    assert sync_error.value.code == "MCP_BACKEND_INVALID_RESULT"
    with pytest.raises(McpSurfaceError) as async_result_error:
        asyncio.run(dispatcher.dispatch_async("decision_get_status", {"session_id": "demo"}))
    assert async_result_error.value.code == "MCP_BACKEND_INVALID_RESULT"


def test_registry_rejects_duplicate_names_and_unknown_get() -> None:
    spec = ToolSpec(
        "safe",
        "safe",
        {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        True,
        True,
    )
    with pytest.raises(ValueError, match="unique"):
        ToolRegistry((spec, spec))
    registry = ToolRegistry((spec,))
    assert registry.names == ("safe",)
    assert registry.specs == (spec,)
    with pytest.raises(McpSurfaceError) as caught:
        registry.get("absent")
    assert caught.value.code == "MCP_TOOL_NOT_ALLOWED"


def test_official_mcp_sdk_server_lists_and_wraps_allowlisted_calls() -> None:
    mcp_types = pytest.importorskip("mcp.types")

    class Backend:
        def invoke(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            if arguments["session_id"] == "demo-error":
                raise McpSurfaceError("MCP_BACKEND_FAILED", "backend failed")
            return {"name": name, "session_id": arguments["session_id"]}

    server = create_stdio_server(Backend())
    list_handler = server.request_handlers[mcp_types.ListToolsRequest]
    listed = asyncio.run(list_handler(mcp_types.ListToolsRequest())).root
    assert [tool.name for tool in listed.tools] == list(MCP_TOOL_NAMES)
    assert all(tool.annotations.openWorldHint is False for tool in listed.tools)
    annotations = {tool.name: tool.annotations for tool in listed.tools}
    assert annotations["decision_create_session"].destructiveHint is False
    assert annotations["decision_get_status"].destructiveHint is False
    for name in (
        "decision_draft_frame",
        "decision_draft_candidates",
        "decision_draft_criteria",
        "decision_draft_evaluations",
        "decision_compare",
        "decision_record_recommendation",
    ):
        assert annotations[name].destructiveHint is True

    call_handler = server.request_handlers[mcp_types.CallToolRequest]
    request = mcp_types.CallToolRequest(
        params=mcp_types.CallToolRequestParams(
            name="decision_get_status",
            arguments={"session_id": "demo"},
        )
    )
    called = asyncio.run(call_handler(request)).root
    assert called.isError is False
    assert json.loads(called.content[0].text) == {
        "ok": True,
        "result": {"name": "decision_get_status", "session_id": "demo"},
    }

    invalid_request = mcp_types.CallToolRequest(
        params=mcp_types.CallToolRequestParams(
            name="decision_get_status",
            arguments={},
        )
    )
    invalid = asyncio.run(call_handler(invalid_request)).root
    assert "Input validation error" in invalid.content[0].text

    backend_error_request = mcp_types.CallToolRequest(
        params=mcp_types.CallToolRequestParams(
            name="decision_get_status",
            arguments={"session_id": "demo-error"},
        )
    )
    backend_error = asyncio.run(call_handler(backend_error_request)).root
    assert backend_error.isError is True
    assert json.loads(backend_error.content[0].text)["error"]["code"] == "MCP_BACKEND_FAILED"


def test_stdio_runner_and_console_error_seams(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    import mcp.server.stdio as stdio_module

    import ai_work_harness.decision.mcp_server as module

    events: list[Any] = []

    class Server:
        def create_initialization_options(self) -> str:
            return "options"

        async def run(self, read: str, write: str, options: str) -> None:
            events.append((read, write, options))

    @asynccontextmanager
    async def fake_stdio():
        yield "read", "write"

    monkeypatch.setattr(stdio_module, "stdio_server", fake_stdio)
    monkeypatch.setattr(module, "create_stdio_server", lambda _backend: Server())
    asyncio.run(run_stdio(object()))
    assert events == [("read", "write", "options")]

    async def fake_run(_backend: Any) -> None:
        events.append("served")

    monkeypatch.setattr(module, "run_stdio", fake_run)
    serve_stdio(lambda: object())
    assert events[-1] == "served"

    def fail(_factory: Any) -> None:
        raise McpSurfaceError("MCP_SDK_UNAVAILABLE", "missing")

    monkeypatch.setattr(module, "serve_stdio", fail)
    monkeypatch.setenv("AI_WORK_HARNESS_ROOT", str(tmp_path))
    with pytest.raises(SystemExit) as exited:
        module.main()
    assert exited.value.code == 4
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "MCP_SDK_UNAVAILABLE"
