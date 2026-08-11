from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_work_harness.cli import main
from ai_work_harness.errors import HarnessError
from ai_work_harness.project import capture_input, init_project, write_frame
from ai_work_harness.routing import select_route


@pytest.mark.parametrize(
    ("input_source", "code"),
    [
        ("network", "UNSUPPORTED_NETWORK"),
        ("live_api", "UNSUPPORTED_LIVE_API"),
        ("retrieval", "UNSUPPORTED_RETRIEVAL"),
        ("unregistered", "UNREGISTERED_INPUT_SOURCE"),
    ],
)
def test_unsupported_input_sources_fail_closed(input_source: str, code: str) -> None:
    with pytest.raises(HarnessError) as caught:
        select_route(
            {
                "input_source": input_source,
                "modalities": ["text"],
                "capabilities": ["transform"],
            }
        )
    assert caught.value.code == code


@pytest.mark.parametrize(
    ("modality", "code"),
    [
        ("multimodal", "UNSUPPORTED_MULTIMODAL"),
        ("image", "UNSUPPORTED_MODALITY"),
    ],
)
def test_unsupported_modalities_fail_closed(modality: str, code: str) -> None:
    with pytest.raises(HarnessError) as caught:
        select_route(
            {
                "input_source": "local_files",
                "modalities": [modality],
                "capabilities": ["transform"],
            }
        )
    assert caught.value.code == code


def test_unregistered_capability_fails_closed() -> None:
    with pytest.raises(HarnessError) as caught:
        select_route(
            {
                "input_source": "local_files",
                "modalities": ["json"],
                "capabilities": ["generate"],
            }
        )
    assert caught.value.code == "UNREGISTERED_CAPABILITY"


def test_mixed_capability_families_fail_closed() -> None:
    with pytest.raises(HarnessError) as caught:
        select_route(
            {
                "input_source": "local_files",
                "modalities": ["csv"],
                "capabilities": ["aggregate", "validate"],
            }
        )
    assert caught.value.code == "MIXED_CAPABILITY_FAMILIES"


def test_cli_emits_structured_error_and_nonzero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    root = tmp_path / "project"
    source = tmp_path / "source.txt"
    source.write_text("data", encoding="utf-8")
    init_project(root)
    capture_input(root, logical_id="input.txt", source=source)
    write_frame(
        root,
        user_statement="Process the file.",
        ai_interpretation="A local route is expected.",
        user_confirmed=True,
        agreed_frame="Keep all work local.",
    )
    result = main(
        [
            "--root",
            str(root),
            "route",
            "--input-source",
            "network",
            "--modality",
            "text",
            "--capability",
            "transform",
            "--objective",
            "Create output.",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert result != 0
    assert captured.out == ""
    assert payload["ok"] is False
    assert payload["error"]["code"] == "UNSUPPORTED_NETWORK"
