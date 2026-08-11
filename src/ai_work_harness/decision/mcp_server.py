"""Draft-only MCP surface for the decision workflow.

The registry is the security boundary.  MCP annotations are emitted as client
hints, but tool availability and input validation are enforced here before a
backend can be invoked.  The optional MCP SDK is imported only by the stdio seam.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator

from ai_work_harness.errors import HarnessError

from .canonical import digest_json, sha256_bytes
from .models import OperationRecord

_SAFE_ID = {"type": "string", "pattern": "^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"}
_DIGEST = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
_IDEMPOTENCY_KEY = {
    "type": "string",
    "minLength": 1,
    "maxLength": 128,
    "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
}
_STRING_ARRAY = {"type": "array", "items": {"type": "string"}}


def _closed(
    properties: Mapping[str, Any], required: tuple[str, ...] | None = None
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required or tuple(properties)),
        "additionalProperties": False,
    }


def _mutation(properties: Mapping[str, Any]) -> dict[str, Any]:
    return _closed(
        {
            "session_id": _SAFE_ID,
            "expected_parent": _DIGEST,
            "idempotency_key": _IDEMPOTENCY_KEY,
            **properties,
        }
    )


_FRAME = _closed(
    {
        "user_statement_verbatim": {"type": "string", "minLength": 1},
        "ai_initial_interpretation": {"type": "string", "minLength": 1},
        "business_user": {"type": ["string", "null"]},
        "blocked_decision": {"type": ["string", "null"]},
        "problem_statement": {"type": ["string", "null"]},
        "scope_in": _STRING_ARRAY,
        "scope_out": _STRING_ARRAY,
        "assumptions": _STRING_ARRAY,
        "open_questions": _STRING_ARRAY,
    }
)

_CANDIDATE = _closed(
    {
        "candidate_id": _SAFE_ID,
        "title": {"type": "string", "minLength": 1},
        "summary": {"type": "string", "minLength": 1},
        "proposed_by": {"type": "string", "const": "agent"},
        "benefits": _STRING_ARRAY,
        "drawbacks": _STRING_ARRAY,
        "risks": _STRING_ARRAY,
        "uncertainties": _STRING_ARRAY,
    }
)

_CRITERION = _closed(
    {
        "criterion_id": _SAFE_ID,
        "title": {"type": "string", "minLength": 1},
        "definition": {"type": "string", "minLength": 1},
        "priority": {"type": "string", "enum": ["must", "high", "medium", "low"]},
    }
)

_EVALUATION_CELL = _closed(
    {
        "candidate_id": _SAFE_ID,
        "criterion_id": _SAFE_ID,
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
        "rationale": {"type": "string", "minLength": 1},
        "evidence_ids": {"type": "array", "items": _SAFE_ID},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "uncertainties": _STRING_ARRAY,
    }
)

_RECOMMENDATION = _closed(
    {
        "disposition": {"type": "string", "enum": ["select", "abstain"]},
        "candidate_id": {"anyOf": [_SAFE_ID, {"type": "null"}]},
        "rationale": {"type": "string", "minLength": 1},
        "evidence_ids": {"type": "array", "items": _SAFE_ID},
        "risks": _STRING_ARRAY,
        "uncertainties": _STRING_ARRAY,
    }
)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    read_only: bool
    idempotent: bool
    destructive: bool = False

    @property
    def annotations(self) -> dict[str, bool]:
        return {
            "readOnlyHint": self.read_only,
            "destructiveHint": self.destructive,
            "idempotentHint": self.idempotent,
            "openWorldHint": False,
        }


PUBLIC_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "decision_create_session",
        "Create an empty local decision session. This does not confirm or approve anything.",
        _closed(
            {
                "session_id": _SAFE_ID,
                "expected_parent": {"type": "null"},
                "idempotency_key": _IDEMPOTENCY_KEY,
            }
        ),
        False,
        True,
    ),
    ToolSpec(
        "decision_get_status",
        "Read the status of a pinned local decision session.",
        _closed({"session_id": _SAFE_ID}),
        True,
        True,
    ),
    ToolSpec(
        "decision_list_sources",
        "List captured source metadata without exposing source paths.",
        _closed({"session_id": _SAFE_ID}),
        True,
        True,
    ),
    ToolSpec(
        "decision_get_source_excerpt",
        "Read a bounded UTF-8 source excerpt by source ID and inclusive line range.",
        _closed(
            {
                "session_id": _SAFE_ID,
                "source_id": _SAFE_ID,
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
            }
        ),
        True,
        True,
    ),
    ToolSpec(
        "decision_draft_frame",
        "Record an agent-authored decision frame draft; a human must confirm it separately.",
        _mutation({"frame": _FRAME}),
        False,
        True,
        destructive=True,
    ),
    ToolSpec(
        "decision_draft_candidates",
        "Record agent-authored candidate drafts; a human must confirm them separately.",
        _mutation({"candidates": {"type": "array", "minItems": 2, "items": _CANDIDATE}}),
        False,
        True,
        destructive=True,
    ),
    ToolSpec(
        "decision_draft_criteria",
        "Record agent-authored criteria drafts; a human must confirm them separately.",
        _mutation({"criteria": {"type": "array", "minItems": 1, "items": _CRITERION}}),
        False,
        True,
        destructive=True,
    ),
    ToolSpec(
        "decision_draft_evaluations",
        "Record draft evaluation cells; required human reviews remain separate.",
        _mutation({"cells": {"type": "array", "minItems": 1, "items": _EVALUATION_CELL}}),
        False,
        True,
        destructive=True,
    ),
    ToolSpec(
        "decision_compare",
        "Run the deterministic qualitative comparison over reviewed inputs.",
        _mutation({}),
        False,
        True,
        destructive=True,
    ),
    ToolSpec(
        "decision_record_recommendation",
        "Record an agent recommendation draft; this is not a final decision or approval.",
        _mutation({"recommendation": _RECOMMENDATION}),
        False,
        True,
        destructive=True,
    ),
    ToolSpec(
        "decision_verify",
        "Verify one pinned decision snapshot without changing it.",
        _closed({"session_id": _SAFE_ID, "snapshot_sha256": _DIGEST}),
        True,
        True,
    ),
)

MCP_TOOL_NAMES: tuple[str, ...] = tuple(spec.name for spec in PUBLIC_TOOL_SPECS)
MUTATING_TOOL_NAMES: frozenset[str] = frozenset(
    spec.name for spec in PUBLIC_TOOL_SPECS if not spec.read_only
)
FORBIDDEN_HUMAN_GATE_TERMS: frozenset[str] = frozenset(
    {"confirm", "review", "final", "challenge", "approval", "approve"}
)


class McpSurfaceError(HarnessError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(code, message, details=details)


class DecisionToolBackend(Protocol):
    def invoke(
        self, name: str, arguments: Mapping[str, Any]
    ) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]:
        """Invoke one already-allowlisted decision tool."""


class _IdempotentStore:
    def __init__(self, store: Any, operation: OperationRecord) -> None:
        self._store = store
        self._operation = operation

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def initialize(self) -> Any:
        return self._store.initialize(self._operation)

    def mutation(self, expected_parent: str) -> Any:
        return self._store.mutation(expected_parent, self._operation)

    def render_idempotent_result(self, snapshot: Any) -> dict[str, Any]:
        refs = dict(snapshot.refs)
        result: dict[str, Any] = {
            "ok": True,
            "session_id": self._store.session_id,
            "generation": snapshot.generation,
            "snapshot_sha256": snapshot.snapshot_sha256,
        }
        result_ref = {
            "decision_draft_frame": "decision_frame",
            "decision_draft_candidates": "candidate_set",
            "decision_draft_criteria": "criteria_set",
            "decision_draft_evaluations": "evaluation_set",
            "decision_compare": "comparison",
            "decision_record_recommendation": "recommendation",
        }.get(self._operation.name)
        if result_ref is None or result_ref not in refs:
            raise McpSurfaceError(
                "MCP_IDEMPOTENCY_RESULT_INVALID",
                "The recorded idempotent result cannot be reconstructed",
                details={"tool": self._operation.name},
            )
        result.update(artifact=result_ref, artifact_sha256=refs[result_ref])
        if self._operation.name == "decision_compare":
            comparison = self._store.read_artifact(refs[result_ref]).payload
            result["eligible_candidate_ids"] = list(comparison["eligible_candidate_ids"])
        return result

    def commit(self, *, expected_parent: str, refs: Mapping[str, str], operation: Any) -> Any:
        return self._store.commit(
            expected_parent=expected_parent,
            refs=refs,
            operation=self._operation,
        )


class LocalDecisionBackend:
    """Application-owned local backend for the allowlisted stdio MCP surface."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()

    @staticmethod
    def _service(
        project_root: Path,
        session_id: str,
        operation: OperationRecord | None = None,
    ) -> Any:
        from .service import DecisionService
        from .store import DecisionStore

        store: Any = DecisionStore(project_root, session_id)
        if operation is not None:
            store = _IdempotentStore(store, operation)
        return DecisionService(project_root, session_id, store=store)

    @staticmethod
    def _operation(name: str, arguments: Mapping[str, Any]) -> OperationRecord:
        return OperationRecord(
            name=name,
            idempotency_key=arguments["idempotency_key"],
            request_sha256=digest_json({"tool": name, "arguments": dict(arguments)}),
        )

    def _source_manifest(self, session_id: str) -> tuple[Any, dict[str, Any]]:
        service = self._service(self.project_root, session_id)
        current = service.store.get_current()
        try:
            digest = current.refs["source_manifest"]
        except KeyError as exc:
            raise McpSurfaceError(
                "MCP_SOURCE_REQUIRED",
                "The decision session has no captured source manifest",
            ) from exc
        payload = service.store.read_artifact(digest).to_document()["payload"]
        return service, payload

    @staticmethod
    def _replay_result(service: Any, name: str, snapshot: Any) -> dict[str, Any]:
        refs = dict(snapshot.refs)
        result: dict[str, Any] = {
            "ok": True,
            "session_id": service.session_id,
            "generation": snapshot.generation,
            "snapshot_sha256": snapshot.snapshot_sha256,
        }
        result_ref = {
            "decision_draft_frame": "decision_frame",
            "decision_draft_candidates": "candidate_set",
            "decision_draft_criteria": "criteria_set",
            "decision_draft_evaluations": "evaluation_set",
            "decision_compare": "comparison",
            "decision_record_recommendation": "recommendation",
        }.get(name)
        if result_ref is None or result_ref not in refs:
            raise McpSurfaceError(
                "MCP_IDEMPOTENCY_RESULT_INVALID",
                "The recorded idempotent result cannot be reconstructed",
                details={"tool": name},
            )
        result.update(artifact=result_ref, artifact_sha256=refs[result_ref])
        if name == "decision_compare":
            comparison = service.store.read_artifact(refs[result_ref]).payload
            result["eligible_candidate_ids"] = list(comparison["eligible_candidate_ids"])
        return result

    def invoke(self, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        session_id = arguments["session_id"]
        if name == "decision_draft_candidates":
            candidates = arguments.get("candidates")
            if not isinstance(candidates, list) or any(
                not isinstance(candidate, Mapping) or candidate.get("proposed_by") != "agent"
                for candidate in candidates
            ):
                raise McpSurfaceError(
                    "MCP_AGENT_PROVENANCE_REQUIRED",
                    "MCP candidate drafts must be explicitly agent-authored",
                )
        if name == "decision_get_status":
            return self._service(self.project_root, session_id).status()
        if name == "decision_list_sources":
            _service, manifest = self._source_manifest(session_id)
            return {"session_id": session_id, "sources": manifest["sources"]}
        if name == "decision_get_source_excerpt":
            service, manifest = self._source_manifest(session_id)
            by_id = {item["source_id"]: item for item in manifest["sources"]}
            try:
                source = by_id[arguments["source_id"]]
            except KeyError as exc:
                raise McpSurfaceError(
                    "MCP_SOURCE_NOT_FOUND",
                    "The requested source ID is not active",
                    details={"source_id": arguments["source_id"]},
                ) from exc
            raw = service.store.read_object(source["blob_sha256"])
            lines = raw.decode("utf-8", errors="strict").splitlines(keepends=True)
            start = arguments["start_line"]
            end = arguments["end_line"]
            if end < start or end > len(lines):
                raise McpSurfaceError(
                    "MCP_INVALID_SOURCE_RANGE",
                    "The requested inclusive line range is outside the source",
                    details={"start_line": start, "end_line": end, "line_count": len(lines)},
                )
            excerpt_bytes = "".join(lines[start - 1 : end]).encode("utf-8")
            if len(excerpt_bytes) > 100_000:
                raise McpSurfaceError(
                    "MCP_SOURCE_EXCERPT_TOO_LARGE",
                    "A source excerpt is limited to 100,000 UTF-8 bytes",
                    details={"bytes": len(excerpt_bytes), "limit": 100_000},
                )
            return {
                "session_id": session_id,
                "source_id": arguments["source_id"],
                "start_line": start,
                "end_line": end,
                "excerpt": excerpt_bytes.decode("utf-8"),
                "excerpt_sha256": sha256_bytes(excerpt_bytes),
            }
        if name == "decision_verify":
            return self._service(self.project_root, session_id).verify(arguments["snapshot_sha256"])

        operation = self._operation(name, arguments)
        if name != "decision_create_session":
            replay_service = self._service(self.project_root, session_id)
            replay = replay_service.store.find_idempotent_result(operation)
            if replay is not None:
                return self._replay_result(replay_service, name, replay)
        service = self._service(self.project_root, session_id, operation)
        expected_parent = arguments.get("expected_parent")
        if name == "decision_create_session":
            return service.initialize()
        if name == "decision_draft_frame":
            return service.import_frame(
                arguments["frame"],
                expected_parent=expected_parent,
                producer_kind="agent_import",
            )
        if name == "decision_draft_candidates":
            return service.import_candidates(
                {"candidates": arguments["candidates"]},
                expected_parent=expected_parent,
                producer_kind="agent_import",
            )
        if name == "decision_draft_criteria":
            return service.import_criteria(
                {"criteria": arguments["criteria"]},
                expected_parent=expected_parent,
                producer_kind="agent_import",
            )
        if name == "decision_draft_evaluations":
            return service.import_evaluations(
                {"cells": arguments["cells"]},
                expected_parent=expected_parent,
                producer_kind="agent_import",
            )
        if name == "decision_compare":
            return service.compare(expected_parent=expected_parent)
        if name == "decision_record_recommendation":
            return service.record_recommendation(
                arguments["recommendation"],
                expected_parent=expected_parent,
                producer_kind="agent_import",
            )
        raise McpSurfaceError(
            "MCP_TOOL_NOT_ALLOWED",
            "The requested MCP tool has no application backend",
            details={"tool": name},
        )


class ToolRegistry:
    def __init__(self, specs: tuple[ToolSpec, ...] = PUBLIC_TOOL_SPECS) -> None:
        by_name = {spec.name: spec for spec in specs}
        if len(by_name) != len(specs):
            raise ValueError("MCP tool names must be unique")
        forbidden = sorted(
            name for name in by_name if any(term in name for term in FORBIDDEN_HUMAN_GATE_TERMS)
        )
        if forbidden:
            raise ValueError(f"Human gate tools are forbidden on MCP: {forbidden}")
        self._by_name = by_name

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._by_name)

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._by_name.values())

    def get(self, name: str) -> ToolSpec:
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise McpSurfaceError(
                "MCP_TOOL_NOT_ALLOWED",
                "The requested MCP tool is not registered",
                details={"tool": name},
            ) from exc


class McpDispatcher:
    def __init__(
        self,
        backend: DecisionToolBackend,
        registry: ToolRegistry | None = None,
    ) -> None:
        self.backend = backend
        self.registry = registry or ToolRegistry()

    def validate(self, name: str, arguments: Mapping[str, Any]) -> ToolSpec:
        spec = self.registry.get(name)
        errors = sorted(
            Draft202012Validator(spec.input_schema).iter_errors(arguments),
            key=lambda item: (list(item.absolute_path), item.message),
        )
        if errors:
            first = errors[0]
            raise McpSurfaceError(
                "MCP_INVALID_ARGUMENTS",
                "MCP tool arguments failed the closed input schema",
                details={
                    "tool": name,
                    "path": [str(item) for item in first.absolute_path],
                    "reason": first.message,
                },
            )
        return spec

    def dispatch(self, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        self.validate(name, arguments)
        result = self.backend.invoke(name, dict(arguments))
        if inspect.isawaitable(result):
            raise McpSurfaceError(
                "MCP_ASYNC_BACKEND_REQUIRED",
                "Use dispatch_async with an asynchronous backend",
                details={"tool": name},
            )
        if not isinstance(result, Mapping):
            raise McpSurfaceError(
                "MCP_BACKEND_INVALID_RESULT",
                "MCP backend results must be JSON objects",
                details={"tool": name},
            )
        return dict(result)

    async def dispatch_async(self, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        self.validate(name, arguments)
        result = self.backend.invoke(name, dict(arguments))
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, Mapping):
            raise McpSurfaceError(
                "MCP_BACKEND_INVALID_RESULT",
                "MCP backend results must be JSON objects",
                details={"tool": name},
            )
        return dict(result)


def create_stdio_server(backend: DecisionToolBackend) -> Any:
    """Build an SDK Server without opening stdio; useful for embedding and tests."""

    try:
        from mcp import types
        from mcp.server import Server
    except ImportError as exc:  # pragma: no cover - optional integration dependency
        raise McpSurfaceError(
            "MCP_SDK_UNAVAILABLE",
            "The optional MCP Python SDK is not installed",
        ) from exc

    dispatcher = McpDispatcher(backend)
    server = Server("ai-work-harness")

    @server.list_tools()
    async def list_tools() -> list[Any]:
        return [
            types.Tool(
                name=spec.name,
                description=spec.description,
                inputSchema=dict(spec.input_schema),
                annotations=types.ToolAnnotations(
                    readOnlyHint=spec.read_only,
                    destructiveHint=spec.destructive,
                    idempotentHint=spec.idempotent,
                    openWorldHint=False,
                ),
            )
            for spec in dispatcher.registry.specs
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> Any:
        is_error = False
        try:
            result = await dispatcher.dispatch_async(name, arguments or {})
            value = {"ok": True, "result": result}
        except HarnessError as exc:
            value = exc.as_dict()
            is_error = True
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text",
                    text=json.dumps(
                        value,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
            ],
            isError=is_error,
        )

    return server


async def run_stdio(backend: DecisionToolBackend) -> None:
    """Run the optional official MCP SDK server over local stdio."""

    try:
        from mcp.server.stdio import stdio_server
    except ImportError as exc:  # pragma: no cover - optional integration dependency
        raise McpSurfaceError(
            "MCP_SDK_UNAVAILABLE",
            "The optional MCP Python SDK is not installed",
        ) from exc
    server = create_stdio_server(backend)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def serve_stdio(backend_factory: Callable[[], DecisionToolBackend]) -> None:
    """Synchronous console-entry seam; backend construction remains application-owned."""

    asyncio.run(run_stdio(backend_factory()))


def main() -> None:
    """Run the local stdio server rooted at the current or explicitly configured project."""

    project_root = Path(os.environ.get("AI_WORK_HARNESS_ROOT", Path.cwd()))
    try:
        serve_stdio(lambda: LocalDecisionBackend(project_root))
    except HarnessError as exc:
        print(
            json.dumps(exc.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            file=sys.stderr,
        )
        raise SystemExit(4) from exc


__all__ = [
    "FORBIDDEN_HUMAN_GATE_TERMS",
    "MCP_TOOL_NAMES",
    "MUTATING_TOOL_NAMES",
    "PUBLIC_TOOL_SPECS",
    "DecisionToolBackend",
    "LocalDecisionBackend",
    "McpDispatcher",
    "McpSurfaceError",
    "ToolRegistry",
    "ToolSpec",
    "create_stdio_server",
    "main",
    "run_stdio",
    "serve_stdio",
]
