from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ai_work_harness.errors import HarnessError
from ai_work_harness.validation import validate_all_schemas, validate_document

ROOT = Path(__file__).resolve().parents[1]


def test_all_packaged_schemas_are_valid() -> None:
    assert validate_all_schemas() == [
        "input-manifest",
        "frame",
        "task-profile",
        "route",
        "approval",
    ]


@pytest.mark.parametrize(
    ("kind", "document"),
    [
        (
            "frame",
            {
                "schema_version": 1,
                "input_manifest_sha256": "0" * 64,
                "user_statement": "statement",
                "ai_interpretation": "interpretation",
                "user_confirmed": True,
                "agreed_frame": "frame",
                "unexpected": "rejected",
            },
        ),
        (
            "input-manifest",
            {
                "schema_version": 1,
                "inputs": [
                    {
                        "logical_id": "input.txt",
                        "bytes": 1,
                        "sha256": "0" * 64,
                        "source_path": "must not be accepted",
                    }
                ],
            },
        ),
    ],
)
def test_extra_schema_fields_are_rejected(kind: str, document: dict) -> None:
    with pytest.raises(HarnessError) as caught:
        validate_document(kind, document)
    assert caught.value.code == "SCHEMA_VALIDATION_FAILED"


def test_synthetic_examples_and_route_fixtures_are_clean_checkout_assets() -> None:
    paths = [
        ROOT / "examples" / "synthetic" / "records.csv",
        ROOT / "examples" / "synthetic" / "frame.example.json",
        ROOT / "tests" / "data" / "route_cases.json",
    ]
    assert all(path.is_file() for path in paths)
    frame = json.loads(paths[1].read_text(encoding="utf-8"))
    validate_document("frame", frame)
    cases = json.loads(paths[2].read_text(encoding="utf-8"))
    assert {case["expected_route"] for case in cases} == {
        "batch_pipeline",
        "rule_decision",
    }
    if (ROOT / ".git").is_dir():
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", *[str(path.relative_to(ROOT)) for path in paths]],
            cwd=ROOT,
            check=False,
        )
        assert ignored.returncode != 0
