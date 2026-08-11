from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .errors import HarnessError
from .project import (
    capture_input,
    create_approval,
    create_route,
    init_project,
    project_status,
    verify_project,
    write_frame,
)


def _emit(value: dict[str, Any], *, stream: Any | None = None) -> None:
    target = sys.stdout if stream is None else stream
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), file=target)


def _add_session_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--session-id", required=True, help="Explicit v2 decision session ID")


def _add_mutation_arguments(parser: argparse.ArgumentParser) -> None:
    _add_session_argument(parser)
    parser.add_argument(
        "--expected-parent",
        required=True,
        help="Full SHA-256 digest of the current snapshot",
    )


def _decision_leaf(
    subparsers: Any,
    name: str,
    *,
    operation: str,
    help: str,
    mutating: bool = False,
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(name, help=help)
    parser.set_defaults(decision_operation=operation)
    if mutating:
        _add_mutation_arguments(parser)
    else:
        _add_session_argument(parser)
    return parser


def _build_decision_parser(commands: Any) -> None:
    decision = commands.add_parser(
        "decision",
        help="Use the explicit v2 evidence-backed decision workflow",
    )
    decision_commands = decision.add_subparsers(dest="decision_group", required=True)

    _decision_leaf(
        decision_commands,
        "init",
        operation="decision init",
        help="Create a new v2 decision session",
    )

    source = decision_commands.add_parser("source", help="Manage captured source material")
    source_commands = source.add_subparsers(dest="source_command", required=True)
    source_capture = _decision_leaf(
        source_commands,
        "capture",
        operation="decision source capture",
        help="Capture one UTF-8 source into the content-addressed store",
        mutating=True,
    )
    source_capture.add_argument("--source-id", required=True)
    source_capture.add_argument("--file", required=True, type=Path, dest="source")
    source_capture.add_argument("--media-type")

    for group_name, artifact_name in (
        ("frame", "decision-frame"),
        ("candidates", "candidate-set"),
        ("criteria", "criteria-set"),
    ):
        group = decision_commands.add_parser(group_name, help=f"Manage the {artifact_name}")
        group_commands = group.add_subparsers(
            dest=f"{group_name}_command",
            required=True,
        )
        import_command = _decision_leaf(
            group_commands,
            "import",
            operation=f"decision {group_name} import",
            help=f"Import a user-authored {artifact_name} payload",
            mutating=True,
        )
        import_command.add_argument("--from", required=True, type=Path, dest="input_file")
        confirm_command = _decision_leaf(
            group_commands,
            "confirm",
            operation=f"decision {group_name} confirm",
            help=f"Confirm the current {artifact_name} by its full digest",
            mutating=True,
        )
        confirm_command.add_argument("--expected-artifact-sha", required=True)
        confirm_command.set_defaults(confirmation_subject=artifact_name)

    evidence = decision_commands.add_parser("evidence", help="Manage evidence")
    evidence_commands = evidence.add_subparsers(dest="evidence_command", required=True)
    evidence_import = _decision_leaf(
        evidence_commands,
        "import",
        operation="decision evidence import",
        help="Import source-bound evidence",
        mutating=True,
    )
    evidence_import.add_argument("--from", required=True, type=Path, dest="input_file")

    evaluations = decision_commands.add_parser("evaluations", help="Manage evaluations")
    evaluation_commands = evaluations.add_subparsers(
        dest="evaluations_command",
        required=True,
    )
    evaluations_import = _decision_leaf(
        evaluation_commands,
        "import",
        operation="decision evaluations import",
        help="Import a complete candidate-by-criterion matrix",
        mutating=True,
    )
    evaluations_import.add_argument("--from", required=True, type=Path, dest="input_file")
    evaluations_import.add_argument(
        "--producer",
        choices=("local_operator", "agent_import"),
        default="local_operator",
    )
    evaluations_generate = _decision_leaf(
        evaluation_commands,
        "generate",
        operation="decision evaluations generate",
        help="Generate a draft evaluation matrix",
        mutating=True,
    )
    evaluations_generate.add_argument("--provider", default="fixture")
    evaluations_review = _decision_leaf(
        evaluation_commands,
        "review",
        operation="decision evaluations review",
        help="Record human reviews for agent-authored Must and High cells",
        mutating=True,
    )
    evaluations_review.add_argument("--from", required=True, type=Path, dest="input_file")

    _decision_leaf(
        decision_commands,
        "compare",
        operation="decision compare",
        help="Derive the deterministic qualitative comparison",
        mutating=True,
    )

    recommend = _decision_leaf(
        decision_commands,
        "recommend",
        operation="decision recommend",
        help="Record or generate a non-binding recommendation",
        mutating=True,
    )
    recommendation_source = recommend.add_mutually_exclusive_group()
    recommendation_source.add_argument("--from", type=Path, dest="input_file")
    recommendation_source.add_argument("--provider")

    final = decision_commands.add_parser("final", help="Manage the human final decision")
    final_commands = final.add_subparsers(dest="final_command", required=True)
    final_import = _decision_leaf(
        final_commands,
        "import",
        operation="decision final import",
        help="Import the human final decision",
        mutating=True,
    )
    final_import.add_argument("--from", required=True, type=Path, dest="input_file")

    approval = decision_commands.add_parser("approval", help="Manage the approval challenge")
    approval_commands = approval.add_subparsers(dest="approval_command", required=True)
    approval_challenge = _decision_leaf(
        approval_commands,
        "challenge",
        operation="decision approval challenge",
        help="Bind the current decision bundle into a short-lived challenge",
        mutating=True,
    )
    approval_challenge.add_argument(
        "--disposition",
        required=True,
        choices=("approved", "rejected", "changes_requested"),
    )
    approval_commit = _decision_leaf(
        approval_commands,
        "commit",
        operation="decision approval commit",
        help="Commit an exact challenge response",
        mutating=True,
    )
    approval_commit.add_argument("--challenge-id", required=True)
    approval_commit.add_argument("--nonce", required=True)
    approval_commit.add_argument("--expected-bundle-sha", required=True)
    approval_commit.add_argument("--reason-file", required=True, type=Path)

    _decision_leaf(
        decision_commands,
        "status",
        operation="decision status",
        help="Report the current pinned v2 decision state",
    )
    _decision_leaf(
        decision_commands,
        "next",
        operation="decision next",
        help="Return the verified next-action plan for an operator or UI",
    )
    decision_verify = _decision_leaf(
        decision_commands,
        "verify",
        operation="decision verify",
        help="Verify the current or named snapshot",
    )
    decision_verify.add_argument("--snapshot")
    _decision_leaf(
        decision_commands,
        "doctor",
        operation="decision doctor",
        help="Report integrity issues and harmless orphan objects",
    )
    _decision_leaf(
        decision_commands,
        "migrate-v1",
        operation="decision migrate-v1",
        help="Copy a validated legacy v1 tree into a separate v2 session",
    )

    agent = decision_commands.add_parser("agent", help="Manage explicit outbound consent")
    agent_commands = agent.add_subparsers(dest="agent_command", required=True)
    agent_preview = _decision_leaf(
        agent_commands,
        "preview",
        operation="decision agent preview",
        help="Compute the exact outbound manifest without making a network request",
    )
    agent_preview.add_argument(
        "--operation",
        required=True,
        choices=("evaluations", "recommendation"),
    )
    agent_preview.add_argument("--provider", default="openai")
    agent_preview.add_argument("--model")
    agent_consent = _decision_leaf(
        agent_commands,
        "consent",
        operation="decision agent consent",
        help="Bind local operator consent to an exact outbound manifest digest",
        mutating=True,
    )
    agent_consent.add_argument(
        "--operation",
        required=True,
        choices=("evaluations", "recommendation"),
    )
    agent_consent.add_argument("--provider", default="openai")
    agent_consent.add_argument("--model")
    agent_consent.add_argument("--expected-manifest-sha", required=True)
    agent_replay = _decision_leaf(
        agent_commands,
        "replay",
        operation="decision agent replay",
        help="Validate a recorded agent run without making a network request",
    )
    agent_replay.add_argument("--mode", choices=("validate",), default="validate")
    agent_replay.add_argument("--agent-run-sha")
    export_view = _decision_leaf(
        decision_commands,
        "export-view",
        operation="decision export-view",
        help="Export a self-contained read-only decision view",
    )
    export_view.add_argument(
        "--snapshot",
        help="Snapshot to export. Defaults to the verified current snapshot.",
    )
    export_view.add_argument("--output", required=True, type=Path)
    export_view.add_argument(
        "--include-cited-excerpts",
        action="store_true",
        help="Include cited text only after confirming it is synthetic or safe to disclose",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-work-harness",
        description="Human-gated artifact and routing checks for a small AI build workflow.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Project root. Defaults to the current directory.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="Create the local harness state directory")

    capture = commands.add_parser("capture", help="Copy and hash one local input")
    capture.add_argument("--id", required=True, dest="logical_id", help="Safe logical input ID")
    capture.add_argument("--file", required=True, type=Path, dest="source")

    frame = commands.add_parser("frame", help="Record separate user and AI framing fields")
    frame.add_argument("--user-statement", required=True)
    frame.add_argument("--ai-interpretation", required=True)
    frame.add_argument("--confirmed", action="store_true", dest="user_confirmed")
    frame.add_argument("--agreed-frame", required=True)

    route = commands.add_parser("route", help="Create a deterministic task profile and route")
    route.add_argument(
        "--input-source",
        required=True,
        help="Input source; only local_files is registered.",
    )
    route.add_argument(
        "--modality",
        required=True,
        action="append",
        dest="modalities",
        help="Repeat for each local modality: text, json, or csv.",
    )
    route.add_argument(
        "--capability",
        required=True,
        action="append",
        dest="capabilities",
        help="Repeat for each requested capability.",
    )
    route.add_argument("--objective", required=True)

    approve = commands.add_parser("approve", help="Bind a decision to current artifacts")
    approve.add_argument("--decision", required=True)
    approve.add_argument("--reason", required=True)

    commands.add_parser("status", help="Report current artifacts and approval freshness")
    commands.add_parser("verify", help="Validate schemas, bindings, route, and approvals")
    _build_decision_parser(commands)
    return parser


def _run_v1(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "init":
        return init_project(args.root)
    if args.command == "capture":
        return capture_input(args.root, logical_id=args.logical_id, source=args.source)
    if args.command == "frame":
        return write_frame(
            args.root,
            user_statement=args.user_statement,
            ai_interpretation=args.ai_interpretation,
            user_confirmed=args.user_confirmed,
            agreed_frame=args.agreed_frame,
        )
    if args.command == "route":
        return create_route(
            args.root,
            input_source=args.input_source,
            modalities=args.modalities,
            capabilities=args.capabilities,
            objective=args.objective,
        )
    if args.command == "approve":
        return create_approval(args.root, decision=args.decision, reason=args.reason)
    if args.command == "status":
        return project_status(args.root)
    if args.command == "verify":
        return verify_project(args.root)
    raise HarnessError("UNKNOWN_COMMAND", f"Unknown command: {args.command}")


def _load_reason(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise HarnessError(
            "ARTIFACT_MISSING",
            "Approval reason must be a regular non-symlink file",
            details={"artifact": str(path)},
        )
    try:
        reason = path.read_text(encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise HarnessError(
            "INVALID_REASON_FILE",
            "Approval reason file must be UTF-8 text",
            details={"artifact": str(path)},
        ) from exc
    except OSError as exc:
        raise HarnessError(
            "ARTIFACT_READ_FAILED",
            "Could not read the approval reason file",
            details={"artifact": str(path), "reason": str(exc)},
        ) from exc
    return reason.rstrip("\r\n")


def _provider_for(kind: str, *, purpose: str) -> tuple[Any, str]:
    if kind == "fixture":
        from .decision.providers import FixtureProvider

        fixture = FixtureProvider()
        provider = (
            fixture.evaluation_provider()
            if purpose == "evaluations"
            else fixture.recommendation_provider()
        )
        return provider, "fixture"
    if kind == "openai":
        raise HarnessError(
            "OUTBOUND_CONSENT_REQUIRED",
            "OpenAI generation requires a bound outbound manifest and explicit consent",
            details={"provider": kind},
            exit_code=4,
        )
    raise HarnessError(
        "UNSUPPORTED_PROVIDER",
        f"Unsupported decision provider: {kind}",
        details={"provider": kind},
        exit_code=4,
    )


def _execute_decision(args: argparse.Namespace, service: Any) -> dict[str, Any]:
    from .decision.service import load_payload

    operation = args.decision_operation
    if operation == "decision init":
        return service.initialize()
    if operation == "decision source capture":
        return service.capture_source(
            source_id=args.source_id,
            source=args.source,
            media_type=args.media_type,
            expected_parent=args.expected_parent,
        )
    if operation in {
        "decision frame import",
        "decision candidates import",
        "decision criteria import",
    }:
        payload = load_payload(args.input_file)
        method_name = {
            "decision frame import": "import_frame",
            "decision candidates import": "import_candidates",
            "decision criteria import": "import_criteria",
        }[operation]
        return getattr(service, method_name)(payload, expected_parent=args.expected_parent)
    if operation in {
        "decision frame confirm",
        "decision candidates confirm",
        "decision criteria confirm",
    }:
        return service.confirm(
            args.confirmation_subject,
            expected_artifact_sha=args.expected_artifact_sha,
            expected_parent=args.expected_parent,
        )
    if operation == "decision evidence import":
        return service.import_evidence(
            load_payload(args.input_file),
            expected_parent=args.expected_parent,
        )
    if operation == "decision evaluations import":
        return service.import_evaluations(
            load_payload(args.input_file),
            expected_parent=args.expected_parent,
            producer_kind=args.producer,
        )
    if operation == "decision evaluations generate":
        if args.provider == "openai":
            return service.run_openai_agent(
                operation="evaluations",
                expected_parent=args.expected_parent,
            )
        provider, producer = _provider_for(args.provider, purpose="evaluations")
        return service.generate_evaluations(
            provider,
            expected_parent=args.expected_parent,
            producer_kind=producer,
        )
    if operation == "decision evaluations review":
        return service.import_reviews(
            load_payload(args.input_file),
            expected_parent=args.expected_parent,
        )
    if operation == "decision compare":
        return service.compare(expected_parent=args.expected_parent)
    if operation == "decision recommend":
        if args.input_file is not None:
            return service.record_recommendation(
                load_payload(args.input_file),
                expected_parent=args.expected_parent,
                producer_kind="local_operator",
            )
        selected_provider = args.provider or "fixture"
        if selected_provider == "openai":
            return service.run_openai_agent(
                operation="recommendation",
                expected_parent=args.expected_parent,
            )
        provider, producer = _provider_for(
            selected_provider,
            purpose="recommendation",
        )
        return service.generate_recommendation(
            provider,
            expected_parent=args.expected_parent,
            producer_kind=producer,
        )
    if operation == "decision final import":
        return service.import_final_decision(
            load_payload(args.input_file),
            expected_parent=args.expected_parent,
        )
    if operation == "decision approval challenge":
        return service.create_approval_challenge(
            disposition=args.disposition,
            expected_parent=args.expected_parent,
        )
    if operation == "decision approval commit":
        return service.commit_approval(
            challenge_id=args.challenge_id,
            nonce=args.nonce,
            expected_bundle_sha=args.expected_bundle_sha,
            reason=_load_reason(args.reason_file),
            expected_parent=args.expected_parent,
        )
    if operation == "decision status":
        return service.status()
    if operation == "decision next":
        return service.operator_plan()
    if operation == "decision verify":
        return service.verify(args.snapshot)
    if operation == "decision doctor":
        return service.doctor()
    if operation == "decision migrate-v1":
        from .decision.migration import migrate_v1

        return migrate_v1(args.root, args.session_id)
    if operation == "decision agent preview":
        return service.preview_agent(
            operation=args.operation,
            provider=args.provider,
            model=args.model,
        )
    if operation == "decision agent consent":
        return service.consent_agent(
            operation=args.operation,
            provider=args.provider,
            model=args.model,
            expected_manifest_sha=args.expected_manifest_sha,
            expected_parent=args.expected_parent,
        )
    if operation == "decision agent replay":
        return service.replay_agent_validate(agent_run_sha=args.agent_run_sha)
    if operation == "decision export-view":
        return service.export_view(
            args.output,
            snapshot_sha256=args.snapshot,
            include_cited_excerpts=args.include_cited_excerpts,
        )
    raise HarnessError("UNKNOWN_COMMAND", f"Unknown command: {operation}")


def _decision_position(
    service: Any,
    snapshot_sha256: str | None = None,
) -> tuple[int | None, str | None]:
    try:
        snapshot = (
            service.store.load_snapshot(snapshot_sha256)
            if snapshot_sha256 is not None
            else service.store.get_current()
        )
    except (HarnessError, OSError):
        return None, snapshot_sha256
    return snapshot.generation, snapshot.snapshot_sha256


def _decision_success(
    *,
    operation: str,
    session_id: str,
    service: Any,
    value: dict[str, Any],
) -> dict[str, Any]:
    result = dict(value)
    result.pop("ok", None)
    result.pop("session_id", None)
    generation = result.pop("generation", None)
    snapshot_sha256 = result.pop("snapshot_sha256", None)
    if generation is None:
        generation, resolved_sha = _decision_position(service, snapshot_sha256)
        snapshot_sha256 = resolved_sha
    return {
        "ok": True,
        "command": operation,
        "session_id": session_id,
        "generation": generation,
        "snapshot_sha256": snapshot_sha256,
        "result": result,
    }


def _decision_error(
    *,
    operation: str,
    session_id: str,
    service: Any | None,
    error: HarnessError,
) -> dict[str, Any]:
    generation: int | None = None
    snapshot_sha256: str | None = None
    if service is not None:
        generation, snapshot_sha256 = _decision_position(service)
    return {
        "ok": False,
        "command": operation,
        "session_id": session_id,
        "generation": generation,
        "snapshot_sha256": snapshot_sha256,
        "error": error.as_dict()["error"],
    }


def _run_decision(args: argparse.Namespace) -> int:
    from .decision.service import DecisionService

    operation = args.decision_operation
    service: Any | None = None
    try:
        service = DecisionService(args.root, args.session_id)
        value = _execute_decision(args, service)
        payload = _decision_success(
            operation=operation,
            session_id=args.session_id,
            service=service,
            value=value,
        )
    except HarnessError as exc:
        payload = _decision_error(
            operation=operation,
            session_id=args.session_id,
            service=service,
            error=exc,
        )
        _emit(payload, stream=sys.stderr)
        return exc.exit_code
    except OSError as exc:
        error = HarnessError(
            "FILESYSTEM_ERROR",
            "A filesystem operation failed",
            details={"reason": str(exc)},
        )
        payload = _decision_error(
            operation=operation,
            session_id=args.session_id,
            service=service,
            error=error,
        )
        _emit(payload, stream=sys.stderr)
        return 2
    except Exception:
        error = HarnessError(
            "INTERNAL_ERROR",
            "An unexpected internal error occurred",
            exit_code=1,
        )
        payload = _decision_error(
            operation=operation,
            session_id=args.session_id,
            service=service,
            error=error,
        )
        _emit(payload, stream=sys.stderr)
        return 1
    _emit(payload)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "decision":
        return _run_decision(args)
    try:
        result = _run_v1(args)
    except HarnessError as exc:
        _emit(exc.as_dict(), stream=sys.stderr)
        return 2
    except OSError as exc:
        error = HarnessError(
            "FILESYSTEM_ERROR",
            "A filesystem operation failed",
            details={"reason": str(exc)},
        )
        _emit(error.as_dict(), stream=sys.stderr)
        return 2
    _emit(result)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
