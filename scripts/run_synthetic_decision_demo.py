#!/usr/bin/env python3
"""Run the synthetic triage decision through one guided CLI invocation."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ai_work_harness.decision.service import DecisionService
from ai_work_harness.guided_cli import run_guided_cli
from ai_work_harness.guided_io import ConsolePort

Response = str | Callable[[str], str]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cli",
        help="Deprecated compatibility option; the demo now calls the packaged guide directly",
    )
    parser.add_argument("--root", type=Path, help="Demo project root (default: a new temp dir)")
    parser.add_argument("--session-id", default="triage-demo")
    parser.add_argument("--lang", choices=("ko", "en"), default="ko")
    parser.add_argument(
        "--test-operator",
        action="store_true",
        help=(
            "Inject deterministic prompt answers for CI; this is not human identity "
            "or human-review evidence"
        ),
    )
    parser.add_argument(
        "--skip-stale",
        action="store_true",
        help="Stop after exporting the ready snapshot instead of changing criteria",
    )
    parser.add_argument(
        "--evaluation-provider",
        choices=("fixture", "openai"),
        default="fixture",
        help="OpenAI performs one consent-bound synthetic evaluation and then stops",
    )
    return parser


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _evidence_payload(source: Path) -> dict[str, Any]:
    excerpt_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    candidates = ("rules", "classical-ml", "llm-assisted")
    criteria = (
        "privacy",
        "auditability",
        "classification-quality",
        "latency",
        "operating-cost",
    )
    return {
        "evidence": [
            {
                "evidence_id": f"ev-{candidate}-{criterion}",
                "claim": f"Synthetic observation for {candidate} against {criterion}.",
                "provenance": "source_observation",
                "source": {
                    "source_id": "triage-source",
                    "start_line": 1,
                    "end_line": 1,
                    "excerpt_sha256": excerpt_sha256,
                },
            }
            for candidate in candidates
            for criterion in criteria
        ]
    }


def _exact_phrase(prompt: str) -> str:
    match = re.search(
        r"(?:SEND OPENAI|APPROVE rules) [0-9a-f]{12}",
        prompt,
    )
    if match is None:
        raise RuntimeError(f"exact consent phrase was not present in prompt: {prompt!r}")
    return match.group(0)


class TestOperatorConsole(ConsolePort):
    """Prompt-aware CI driver; deliberately not evidence of a human identity."""

    def __init__(self, responses: list[Response]) -> None:
        self.responses = deque(responses)

    def is_interactive(self) -> bool:
        return True

    def write(self, text: str) -> None:
        print(text)

    def read(self, prompt: str = "") -> str:
        if not self.responses:
            raise EOFError("synthetic test operator exhausted its prompt script")
        response = self.responses.popleft()
        value = response(prompt) if callable(response) else response
        rendered = value if value else "<Enter>"
        print(f"{prompt}{rendered}")
        return value


def _guided_prefix(example: Path, evidence_file: Path) -> list[Response]:
    return [
        "yes",  # create the missing session
        "",  # continue: source
        "triage-source",
        str(example / "source.md"),
        "text/markdown",
        "",  # continue: frame
        "json",
        str(example / "frame.json"),
        "",  # do not save another draft
        "",  # continue: confirm frame
        "confirm",
        "",  # continue: candidates
        "json",
        str(example / "candidates.json"),
        "",  # do not save another draft
        "",  # continue: confirm candidates
        "confirm",
        "",  # continue: criteria
        "json",
        str(example / "criteria.json"),
        "",  # do not save another draft
        "",  # continue: confirm criteria
        "confirm",
        "",  # continue: evidence
        "json",
        str(evidence_file),
        "",  # do not save another draft
        "",  # continue: evaluation
    ]


def _fixture_responses(
    example: Path,
    evidence_file: Path,
    export_file: Path,
    *,
    skip_stale: bool,
) -> list[Response]:
    responses = [*_guided_prefix(example, evidence_file), "fixture", ""]
    for index in range(9):
        responses.extend(
            (
                "concur",
                f"Synthetic test operator reviewed Must/High cell {index + 1}.",
            )
        )
    responses.extend(
        (
            "yes",  # commit all Must/High reviews
            "",  # continue: recommendation
            "fixture",
            "",  # continue: final decision
            "select",
            "rules",  # intentionally differ from Fixture recommendation
            "Rules are the simplest reviewed operating choice for this small team.",
            "yes",  # classification-quality risk
            "yes",  # latency risk
            "yes",  # operating-cost risk
            "yes",  # record final decision
            "",  # continue: create challenge
            "approved",
            "",  # continue: commit challenge
            _exact_phrase,
            "Reviewed the complete synthetic decision bundle.",
            "export",
            str(export_file),
        )
    )
    if skip_stale:
        responses.append("finish")
    else:
        responses.extend(
            (
                "change",
                "criteria_set",
                "yes",
                "json",
                str(example / "criteria.changed.json"),
                "",  # do not save another draft
                "quit",
            )
        )
    return responses


def _openai_responses(example: Path, evidence_file: Path) -> list[Response]:
    return [
        *_guided_prefix(example, evidence_file),
        "openai",
        _exact_phrase,
        "quit",  # one external draft only; no decision or approval
    ]


def _assert_fixture_outcome(
    service: DecisionService,
    *,
    export_file: Path,
    skip_stale: bool,
) -> None:
    status = service.status()
    if skip_stale:
        if status["ready"] is not True or status["decision_complete"] is not True:
            raise SystemExit("Expected the approved select snapshot to be ready")
    else:
        if status["ready"] is not False or status["verified"] is not True:
            raise SystemExit("Expected changed criteria to stale a still-valid graph")
        if "criteria_set_changed" not in status["stale_reasons"]:
            raise SystemExit("Expected the stale reason to identify the criteria change")
    exported = json.loads(export_file.read_text(encoding="utf-8"))
    if exported["status"]["ready"] is not True:
        raise SystemExit("Expected the pre-change viewer export to preserve the ready decision")
    final = exported["artifacts"]["final_decision"]["payload"]
    if final["recommendation_relation"] != "different" or final["candidate_id"] != "rules":
        raise SystemExit("Expected the human decision to differ from the Fixture recommendation")


def main() -> int:
    args = _parser().parse_args()
    repository = Path(__file__).resolve().parents[1]
    example = repository / "examples" / "decision-triage"
    root = (args.root or Path(tempfile.mkdtemp(prefix="ai-work-harness-demo-"))).resolve()
    root.mkdir(parents=True, exist_ok=True)
    session_dir = root / ".ai-work-harness" / "v2" / "sessions" / args.session_id
    if session_dir.exists():
        raise SystemExit(f"Demo session already exists: {session_dir}")

    evidence_file = root / "demo-input" / "evidence.json"
    export_file = root / "approved-decision-view.v1.json"
    _write_json(evidence_file, _evidence_payload(example / "source.md"))

    print("Synthetic data only. No real customer data is used.")
    print(f"Demo root: {root}")
    print(f"Session: {args.session_id}")
    print(
        "The injected test operator is deterministic CI input, not human identity "
        "or human-review evidence."
        if args.test_operator
        else (
            "Follow the guided prompts; no snapshot, artifact, nonce, or challenge value is copied."
        )
    )

    console: ConsolePort | None = None
    if args.test_operator:
        responses = (
            _openai_responses(example, evidence_file)
            if args.evaluation_provider == "openai"
            else _fixture_responses(
                example,
                evidence_file,
                export_file,
                skip_stale=args.skip_stale,
            )
        )
        console = TestOperatorConsole(responses)

    exit_code = run_guided_cli(
        root,
        args.session_id,
        language=args.lang,
        console=console,
    )
    if exit_code != 0:
        return exit_code

    service = DecisionService(root, args.session_id)
    if args.evaluation_provider == "openai":
        status = service.status()
        if args.test_operator and (
            "evaluation_set" not in status["refs"] or "agent_run" not in status["refs"]
        ):
            raise SystemExit("Expected one consent-bound OpenAI evaluation result")
        print("One consent-bound synthetic OpenAI draft completed; no decision was approved.")
    elif args.test_operator:
        _assert_fixture_outcome(
            service,
            export_file=export_file,
            skip_stale=args.skip_stale,
        )
        print(f"Ready decision export: {export_file}")
        if not args.skip_stale:
            print("The same guide invocation then changed criteria and recorded stale state.")

    print(f"Demo root preserved at: {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
