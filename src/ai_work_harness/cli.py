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


def _emit(value: dict[str, Any], *, stream: Any = sys.stdout) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), file=stream)


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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            result = init_project(args.root)
        elif args.command == "capture":
            result = capture_input(args.root, logical_id=args.logical_id, source=args.source)
        elif args.command == "frame":
            result = write_frame(
                args.root,
                user_statement=args.user_statement,
                ai_interpretation=args.ai_interpretation,
                user_confirmed=args.user_confirmed,
                agreed_frame=args.agreed_frame,
            )
        elif args.command == "route":
            result = create_route(
                args.root,
                input_source=args.input_source,
                modalities=args.modalities,
                capabilities=args.capabilities,
                objective=args.objective,
            )
        elif args.command == "approve":
            result = create_approval(args.root, decision=args.decision, reason=args.reason)
        elif args.command == "status":
            result = project_status(args.root)
        elif args.command == "verify":
            result = verify_project(args.root)
        else:  # pragma: no cover - argparse requires a known command
            raise HarnessError("UNKNOWN_COMMAND", f"Unknown command: {args.command}")
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
