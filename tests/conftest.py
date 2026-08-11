from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from ai_work_harness.project import (
    ProjectPaths,
    capture_input,
    create_approval,
    create_route,
    init_project,
    write_frame,
)


@dataclass
class PreparedProject:
    root: Path
    source: Path
    paths: ProjectPaths
    approval_id: str


@pytest.fixture
def prepared_project(tmp_path: Path) -> PreparedProject:
    root = tmp_path / "project"
    source = tmp_path / "source.csv"
    source.write_bytes(b"id,value\n1,4\n")
    init_project(root)
    capture_input(root, logical_id="records.csv", source=source)
    write_frame(
        root,
        user_statement="Group the records.",
        ai_interpretation="A local batch transformation fits.",
        user_confirmed=True,
        agreed_frame="Create a deterministic local artifact.",
    )
    create_route(
        root,
        input_source="local_files",
        modalities=["csv"],
        capabilities=["aggregate", "transform"],
        objective="Create grouped output.",
    )
    approval = create_approval(
        root,
        decision="Use the registered batch route.",
        reason="The confirmed frame uses local files.",
    )
    return PreparedProject(
        root=root,
        source=source,
        paths=ProjectPaths.from_root(root),
        approval_id=approval["approval_id"],
    )
