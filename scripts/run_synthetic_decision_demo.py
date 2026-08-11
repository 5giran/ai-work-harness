#!/usr/bin/env python3
"""Run the synthetic v2 workflow; fixture mode makes no external request."""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Any


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cli",
        default="ai-work-harness",
        help="CLI command, for example '.venv/bin/ai-work-harness'",
    )
    parser.add_argument("--root", type=Path, help="Demo project root (default: a new temp dir)")
    parser.add_argument("--session-id", default="triage-demo")
    parser.add_argument(
        "--test-operator",
        action="store_true",
        help="Auto-enter digest challenges for CI only; this is not human identity evidence",
    )
    parser.add_argument(
        "--skip-stale",
        action="store_true",
        help="Stop after the ready snapshot instead of changing criteria",
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


class Demo:
    def __init__(self, *, cli: str, root: Path, session_id: str, test_operator: bool) -> None:
        self.command = shlex.split(cli)
        self.root = root
        self.session_id = session_id
        self.test_operator = test_operator
        self.parent = ""

    def run(self, *arguments: str) -> dict[str, Any]:
        command = [*self.command, "--root", str(self.root), *arguments]
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        serialized = completed.stdout if completed.returncode == 0 else completed.stderr
        try:
            payload = json.loads(serialized)
        except json.JSONDecodeError as exc:
            raise SystemExit(
                f"Command did not return JSON (exit {completed.returncode}): "
                f"{' '.join(command)}\n{serialized}"
            ) from exc
        if completed.returncode != 0:
            raise SystemExit(json.dumps(payload, ensure_ascii=False, indent=2))
        snapshot = payload.get("snapshot_sha256")
        if isinstance(snapshot, str):
            self.parent = snapshot
        return payload

    def mutate(self, *arguments: str) -> dict[str, Any]:
        return self.run(
            *arguments,
            "--session-id",
            self.session_id,
            "--expected-parent",
            self.parent,
        )

    def read(self, *arguments: str) -> dict[str, Any]:
        return self.run(*arguments, "--session-id", self.session_id)

    def exact(self, label: str, expected: str) -> str:
        print(f"\n{label}\n{expected}")
        if self.test_operator:
            print("[CI test operator injected the exact value]")
            return expected
        actual = input("위 값을 그대로 입력하세요: ").strip()
        if actual != expected:
            raise SystemExit(f"{label} 불일치: 데모를 중단합니다.")
        return actual


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


def main() -> int:
    args = _parser().parse_args()
    repository = Path(__file__).resolve().parents[1]
    example = repository / "examples" / "decision-triage"
    root = args.root or Path(tempfile.mkdtemp(prefix="ai-work-harness-demo-"))
    root.mkdir(parents=True, exist_ok=True)
    demo = Demo(
        cli=args.cli,
        root=root.resolve(),
        session_id=args.session_id,
        test_operator=args.test_operator,
    )

    initialized = demo.read("decision", "init")
    demo.parent = initialized["snapshot_sha256"]
    demo.mutate(
        "decision",
        "source",
        "capture",
        "--source-id",
        "triage-source",
        "--file",
        str(example / "source.md"),
        "--media-type",
        "text/markdown",
    )

    for command, artifact_ref, payload_file in (
        ("frame", "decision_frame", "frame.json"),
        ("candidates", "candidate_set", "candidates.json"),
        ("criteria", "criteria_set", "criteria.json"),
    ):
        demo.mutate(
            "decision",
            command,
            "import",
            "--from",
            str(example / payload_file),
        )
        status = demo.read("decision", "status")
        artifact_sha = status["result"]["refs"][artifact_ref]
        entered = demo.exact(f"{command} artifact SHA-256", artifact_sha)
        demo.mutate(
            "decision",
            command,
            "confirm",
            "--expected-artifact-sha",
            entered,
        )

    evidence_file = root / "demo-input" / "evidence.json"
    _write_json(evidence_file, _evidence_payload(example / "source.md"))
    demo.mutate("decision", "evidence", "import", "--from", str(evidence_file))
    if args.evaluation_provider == "openai":
        preview = demo.read(
            "decision",
            "agent",
            "preview",
            "--operation",
            "evaluations",
            "--provider",
            "openai",
        )
        manifest_sha = demo.exact(
            "outbound manifest SHA-256",
            preview["result"]["outbound_manifest_sha256"],
        )
        demo.mutate(
            "decision",
            "agent",
            "consent",
            "--operation",
            "evaluations",
            "--provider",
            "openai",
            "--expected-manifest-sha",
            manifest_sha,
        )
        generated = demo.mutate(
            "decision",
            "evaluations",
            "generate",
            "--provider",
            "openai",
        )
        demo.read("decision", "agent", "replay", "--mode", "validate")
        print(f"\nOpenAI synthetic evaluation snapshot: {generated['snapshot_sha256']}")
        print("One consent-bound draft generation completed; no decision was approved.")
        print(f"Demo root preserved at: {root}")
        return 0

    demo.mutate("decision", "evaluations", "generate", "--provider", "fixture")
    demo.mutate(
        "decision",
        "evaluations",
        "review",
        "--from",
        str(example / "reviews.json"),
    )
    comparison = demo.mutate("decision", "compare")
    assert comparison["result"]["eligible_candidate_ids"] == ["classical-ml", "rules"]
    demo.mutate("decision", "recommend", "--provider", "fixture")
    final = demo.mutate(
        "decision",
        "final",
        "import",
        "--from",
        str(example / "final.json"),
    )
    assert final["result"]["recommendation_relation"] == "different"

    challenge = demo.mutate(
        "decision",
        "approval",
        "challenge",
        "--disposition",
        "approved",
    )["result"]
    challenge_id = demo.exact("approval challenge ID", challenge["challenge_id"])
    nonce = demo.exact("approval nonce", challenge["nonce"])
    bundle_sha = demo.exact("decision bundle SHA-256", challenge["decision_bundle_sha256"])
    demo.mutate(
        "decision",
        "approval",
        "commit",
        "--challenge-id",
        challenge_id,
        "--nonce",
        nonce,
        "--expected-bundle-sha",
        bundle_sha,
        "--reason-file",
        str(example / "approval-reason.txt"),
    )

    ready = demo.read("decision", "status")
    if ready["result"]["ready"] is not True:
        raise SystemExit("Expected the approved select snapshot to be ready")
    demo.read("decision", "verify", "--snapshot", demo.parent)
    export_file = root / "approved-decision-view.v1.json"
    demo.read(
        "decision",
        "export-view",
        "--snapshot",
        demo.parent,
        "--output",
        str(export_file),
    )
    print(f"\nREADY snapshot: {demo.parent}")
    print(f"Offline viewer bundle: {export_file}")

    if not args.skip_stale:
        demo.mutate(
            "decision",
            "criteria",
            "import",
            "--from",
            str(example / "criteria.changed.json"),
        )
        stale = demo.read("decision", "status")
        if stale["result"]["ready"] is not False:
            raise SystemExit("Expected changed criteria to make the previous approval stale")
        if stale["result"]["verified"] is not True:
            raise SystemExit("Expected the stale snapshot graph to remain internally valid")
        print("\nAfter criteria change:")
        print(json.dumps(stale["result"], ensure_ascii=False, indent=2, sort_keys=True))

    print(f"\nDemo root preserved at: {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
