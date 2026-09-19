from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_work_harness.decision import store as store_module
from ai_work_harness.decision.service import DecisionService
from ai_work_harness.decision.store import DecisionStore
from ai_work_harness.errors import HarnessError


def test_crashed_writer_is_diagnosed_without_automatic_unlock(tmp_path: Path) -> None:
    store = DecisionStore(tmp_path, "crash", lock_timeout=0.01)
    initial = store.initialize()
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, sys; from pathlib import Path; "
            "from ai_work_harness.decision.store import DecisionStore; "
            "s = DecisionStore(Path(sys.argv[1]), 'crash'); "
            "lock = s._writer_lock(); lock.__enter__(); os._exit(0)",
            str(tmp_path),
        ],
        check=True,
        timeout=20,
    )
    before = store.paths.current_pointer.read_bytes()
    lock_bytes = store.paths.writer_lock.read_bytes()
    report = store.doctor().as_dict()
    assert report["ok"] is False
    assert report["integrity_ok"] is True
    assert report["issues"] == []
    assert report["writer_lock"]["state"] == "present"
    assert report["writer_lock"]["owner_status"] == (
        "pid_absent" if os.name == "posix" else "unknown"
    )
    service = DecisionService(tmp_path, "crash", store=store)
    with pytest.raises(HarnessError) as doctor:
        service.doctor()
    assert doctor.value.code == "WRITE_LOCK_PRESENT"
    assert doctor.value.exit_code == 3
    assert doctor.value.details["doctor"]["integrity_ok"] is True
    with pytest.raises(HarnessError) as blocked:
        store.commit(expected_parent=initial.sha256, refs={})
    assert blocked.value.code == "WRITE_LOCK_TIMEOUT"
    assert store.paths.writer_lock.read_bytes() == lock_bytes
    assert store.paths.current_pointer.read_bytes() == before

    # The child was joined, and no other writer exists in this isolated test.
    # Model the documented manual backup/removal step, never an automatic repair.
    store.paths.writer_lock.rename(tmp_path / "recovered-writer.lock")
    assert store.doctor().as_dict()["writer_lock"]["state"] == "absent"
    assert store.doctor().ok is True
    store.commit(expected_parent=initial.sha256, refs={}, operation="after-manual-recovery")
    assert store.verify().ok is True


def test_active_writer_lock_is_reported_and_preserved(tmp_path: Path) -> None:
    store = DecisionStore(tmp_path, "active")
    store.initialize()
    with store._writer_lock():
        before = store.paths.writer_lock.read_bytes()
        report = store.doctor().as_dict()
        assert report["ok"] is False
        assert report["integrity_ok"] is True
        assert report["writer_lock"]["pid"] == os.getpid()
        assert report["writer_lock"]["owner_status"] == (
            "pid_present" if os.name == "posix" else "unknown"
        )
        assert store.paths.writer_lock.read_bytes() == before
    assert store.doctor().ok is True


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"not-json",
        b'{"pid":true,"acquired_at":"2026-09-19T00:00:00Z"}',
        b'{"pid":-1,"acquired_at":"2026-09-19T00:00:00Z"}',
        b'{"pid":1,"acquired_at":"not-a-timestamp"}',
        b"x" * 4097,
    ],
)
def test_invalid_lock_metadata_is_reported_without_echoing_or_deleting_it(
    tmp_path: Path, content: bytes
) -> None:
    store = DecisionStore(tmp_path, "invalid")
    store.initialize()
    store.paths.writer_lock.write_bytes(content)
    report = store.doctor().as_dict()
    assert report["ok"] is False
    assert report["integrity_ok"] is True
    assert report["writer_lock"]["state"] == "invalid"
    assert report["writer_lock"]["owner_status"] == "unknown"
    assert store.paths.writer_lock.read_bytes() == content


def test_windows_pid_probe_never_uses_os_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    def unsafe_kill(_pid: int, _signal: int) -> None:
        pytest.fail("Windows must not use os.kill to probe a writer")

    monkeypatch.setattr(store_module, "os", SimpleNamespace(name="nt", kill=unsafe_kill))
    assert DecisionStore._probe_lock_pid(123) == "unknown"


def test_permission_denied_pid_probe_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(_pid: int, _signal: int) -> None:
        raise PermissionError("synthetic permission denial")

    monkeypatch.setattr(store_module, "os", SimpleNamespace(name="posix", kill=denied))
    assert DecisionStore._probe_lock_pid(123) == "unknown"


def test_lock_replaced_during_diagnosis_is_not_treated_as_an_abandoned_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DecisionStore(tmp_path, "changing")
    store.initialize()
    store.paths.writer_lock.write_bytes(b"old lock")
    read = store._read_regular_file

    def read_and_replace(path: Path, *, missing_code: str) -> bytes:
        result = read(path, missing_code=missing_code)
        if path == store.paths.writer_lock:
            path.write_bytes(b"a different writer's lock")
        return result

    monkeypatch.setattr(store, "_read_regular_file", read_and_replace)
    report = store.doctor().as_dict()
    assert report["ok"] is False
    assert report["writer_lock"]["state"] == "changed"
    assert report["writer_lock"]["owner_status"] == "unknown"
    assert store.paths.writer_lock.read_bytes() == b"a different writer's lock"


def test_lock_symlink_is_not_followed(tmp_path: Path) -> None:
    store = DecisionStore(tmp_path, "symlink")
    store.initialize()
    external = tmp_path / "external.txt"
    external.write_text("not lock metadata", encoding="utf-8")
    try:
        store.paths.writer_lock.symlink_to(external)
    except OSError:
        pytest.skip("This platform does not permit creating symbolic links")
    report = store.doctor().as_dict()
    assert report["writer_lock"]["state"] == "unreadable"
    assert store.paths.writer_lock.is_symlink()
    assert external.read_text(encoding="utf-8") == "not lock metadata"
