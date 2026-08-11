#!/usr/bin/env python3
"""Portable clean-wheel smoke used by Linux, macOS, and Windows CI."""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path


def _executable(venv: Path, name: str) -> Path:
    suffix = ".exe" if (venv / "Scripts").is_dir() else ""
    directory = venv / ("Scripts" if suffix else "bin")
    return directory / f"{name}{suffix}"


def _run(command: list[str]) -> str:
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        raise SystemExit(
            f"smoke command failed ({completed.returncode}): {command}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venv", type=Path, required=True)
    args = parser.parse_args()
    venv = args.venv.resolve()
    python = _executable(venv, "python")
    cli = _executable(venv, "ai-work-harness")

    version = _run([str(cli), "--version"])
    if "0.4.0" not in version:
        raise SystemExit(f"unexpected wheel version: {version.strip()}")

    resource_check = """
from importlib.resources import files
from importlib.metadata import entry_points
from ai_work_harness.decision.prompts import EVALUATION_PROMPT, RECOMMENDATION_PROMPT
schemas = list(files('ai_work_harness.schemas.v2').iterdir())
assert len([item for item in schemas if item.name.endswith('.json')]) >= 20
assert EVALUATION_PROMPT.prompt_id == 'evaluation-v1'
assert RECOMMENDATION_PROMPT.prompt_id == 'recommendation-v1'
scripts = {item.name for item in entry_points(group='console_scripts')}
assert {'ai-work-harness', 'ai-work-harness-mcp'} <= scripts
"""
    _run([str(python), "-c", resource_check])
    guide_help = _run([str(cli), "decision", "guide", "--help"])
    if "session_id" not in guide_help or "--lang {ko,en}" not in guide_help:
        raise SystemExit("guided decision command is missing from the clean wheel")

    with tempfile.TemporaryDirectory(prefix="ai-work-harness-wheel-smoke-") as directory:
        root = Path(directory)
        legacy = json.loads(_run([str(cli), "--root", str(root), "init"]))
        if legacy.get("ok") is not True:
            raise SystemExit("legacy v1 init did not succeed")

        initialized = json.loads(
            _run(
                [
                    str(cli),
                    "--root",
                    str(root),
                    "decision",
                    "init",
                    "--session-id",
                    "wheel-smoke",
                ]
            )
        )
        if initialized.get("generation") != 0:
            raise SystemExit("v2 init did not create generation zero")
        snapshot = initialized.get("snapshot_sha256")

        source = root / "wheel-smoke-source.txt"
        source.write_text("synthetic cross-platform CAS smoke\n", encoding="utf-8")
        captured = json.loads(
            _run(
                [
                    str(cli),
                    "--root",
                    str(root),
                    "decision",
                    "source",
                    "capture",
                    "--session-id",
                    "wheel-smoke",
                    "--expected-parent",
                    str(snapshot),
                    "--source-id",
                    "wheel-source",
                    "--file",
                    str(source),
                    "--media-type",
                    "text/plain",
                ]
            )
        )
        if captured.get("generation") != 1:
            raise SystemExit("v2 source capture did not advance the snapshot")
        snapshot = captured.get("snapshot_sha256")
        object_files = [
            item
            for item in (root / ".ai-work-harness" / "v2" / "objects" / "sha256").rglob("*")
            if item.is_file()
        ]
        if len(object_files) < 2:
            raise SystemExit("v2 source capture did not populate the content-addressed store")
        writer_lock = root / ".ai-work-harness" / "v2" / "sessions" / "wheel-smoke" / "writer.lock"
        if writer_lock.exists():
            raise SystemExit("v2 mutation left the cross-platform writer lock behind")

        verified = json.loads(
            _run(
                [
                    str(cli),
                    "--root",
                    str(root),
                    "decision",
                    "verify",
                    "--session-id",
                    "wheel-smoke",
                    "--snapshot",
                    str(snapshot),
                ]
            )
        )
        if verified.get("result", {}).get("verified") is not True:
            raise SystemExit("v2 initialized snapshot did not verify")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
