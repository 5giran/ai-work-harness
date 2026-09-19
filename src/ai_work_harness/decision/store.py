from __future__ import annotations

import os
import stat
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ai_work_harness.errors import HarnessError

from .canonical import canonical_json_bytes, parse_json_bytes, sha256_bytes
from .models import (
    REF_ARTIFACT_TYPES,
    ArtifactEnvelope,
    Clock,
    CurrentPointer,
    DoctorReport,
    IdSource,
    IntegrityIssue,
    OperationRecord,
    SnapshotRecord,
    StoredSnapshot,
    SystemClock,
    UUID4IdSource,
    VerificationReport,
    WriterLockReport,
    format_timestamp,
    validate_digest,
    validate_event_id,
    validate_ref_key,
    validate_safe_id,
)


@dataclass(frozen=True)
class DecisionStorePaths:
    project_root: Path
    session_id: str

    @property
    def v2_root(self) -> Path:
        return self.project_root / ".ai-work-harness" / "v2"

    @property
    def objects_root(self) -> Path:
        return self.v2_root / "objects" / "sha256"

    @property
    def session_root(self) -> Path:
        return self.v2_root / "sessions" / self.session_id

    @property
    def snapshots_root(self) -> Path:
        return self.session_root / "snapshots"

    @property
    def current_pointer(self) -> Path:
        return self.session_root / "current.json"

    @property
    def writer_lock(self) -> Path:
        return self.session_root / "writer.lock"

    def object_path(self, digest: str) -> Path:
        validate_digest(digest)
        return self.objects_root / digest[:2] / digest

    def snapshot_path(self, digest: str) -> Path:
        validate_digest(digest)
        return self.snapshots_root / digest[:2] / f"{digest}.json"


class DecisionStore:
    def __init__(
        self,
        project_root: Path,
        session_id: str,
        *,
        clock: Clock | None = None,
        id_source: IdSource | None = None,
        lock_timeout: float = 5.0,
    ) -> None:
        if lock_timeout <= 0:
            raise ValueError("lock_timeout must be positive")
        self.project_root = Path(project_root).resolve()
        self.session_id = validate_safe_id(session_id, label="session_id")
        self.paths = DecisionStorePaths(self.project_root, self.session_id)
        self.clock = clock or SystemClock()
        self.id_source = id_source or UUID4IdSource()
        self.lock_timeout = lock_timeout
        self._transaction_owner: int | None = None

    def new_event_id(self) -> str:
        return validate_event_id(self.id_source.new_id())

    def find_idempotent_result(self, operation: OperationRecord) -> StoredSnapshot | None:
        """Return the original committed snapshot for an exact request, if present."""

        return self._find_idempotent_result(self.get_current(), operation)

    @contextmanager
    def mutation(
        self,
        expected_parent: str,
        operation: OperationRecord | None = None,
    ) -> Iterator[StoredSnapshot | None]:
        """Serialize validation, object writes, and pointer commit for one mutation."""

        validate_digest(expected_parent, label="expected_parent")
        owner = threading.get_ident()
        if self._transaction_owner == owner:
            yield None
            return
        self._ensure_store_directories()
        with self._writer_lock():
            current = self.get_current()
            if current.sha256 != expected_parent:
                replay = self._find_idempotent_result(current, operation)
                if replay is None:
                    raise HarnessError(
                        "WRITE_CONFLICT",
                        "Current snapshot changed since the caller read it",
                        details={
                            "expected_parent": expected_parent,
                            "actual_parent": current.sha256,
                        },
                    )
                yield replay
                return
            self._transaction_owner = owner
            try:
                yield None
            finally:
                self._transaction_owner = None

    def initialize(
        self,
        operation: OperationRecord | str | None = "initialize",
    ) -> StoredSnapshot:
        normalized_operation = (
            OperationRecord(name=operation) if isinstance(operation, str) else operation
        )
        self._ensure_store_directories()
        with self._writer_lock():
            pointer = self.paths.current_pointer
            if pointer.exists() or pointer.is_symlink():
                current = self.get_current()
                replay = self._find_idempotent_result(current, normalized_operation)
                if replay is not None:
                    return replay
                raise HarnessError(
                    "SESSION_EXISTS",
                    f"Decision session already exists: {self.session_id}",
                    details={"session_id": self.session_id},
                )
            snapshot = SnapshotRecord(
                session_id=self.session_id,
                generation=0,
                parent_snapshot_sha256=None,
                created_at=format_timestamp(self.clock.now()),
                refs={},
                operation=normalized_operation,
            )
            stored = self._store_snapshot(snapshot)
            self._write_pointer(stored)
            return stored

    def read_pointer(self) -> CurrentPointer:
        path = self.paths.current_pointer
        raw = self._read_regular_file(path, missing_code="SESSION_NOT_FOUND")
        try:
            document = parse_json_bytes(raw, label="current.json")
            pointer = CurrentPointer.from_document(document)
        except HarnessError as exc:
            raise HarnessError(
                "POINTER_INTEGRITY_FAILED",
                "Current pointer is malformed",
                details={"reason_code": exc.code, "reason": exc.message},
            ) from exc
        if raw != canonical_json_bytes(pointer.to_document()):
            raise HarnessError(
                "POINTER_INTEGRITY_FAILED",
                "Current pointer is not canonical JSON",
            )
        if pointer.session_id != self.session_id:
            raise HarnessError(
                "POINTER_INTEGRITY_FAILED",
                "Current pointer belongs to a different session",
                details={
                    "expected_session_id": self.session_id,
                    "actual_session_id": pointer.session_id,
                },
            )
        return pointer

    def get_current(self) -> StoredSnapshot:
        pointer = self.read_pointer()
        stored = self.load_snapshot(pointer.snapshot_sha256)
        if stored.snapshot.generation != pointer.generation:
            raise HarnessError(
                "POINTER_INTEGRITY_FAILED",
                "Current pointer generation does not match its snapshot",
                details={
                    "pointer_generation": pointer.generation,
                    "snapshot_generation": stored.snapshot.generation,
                },
            )
        return stored

    def put_blob(self, value: bytes) -> str:
        if not isinstance(value, bytes):
            raise TypeError("value must be bytes")
        digest = sha256_bytes(value)
        self._write_immutable(self.paths.object_path(digest), value)
        return digest

    def put_artifact(self, envelope: ArtifactEnvelope | Mapping[str, Any]) -> str:
        artifact = (
            envelope
            if isinstance(envelope, ArtifactEnvelope)
            else ArtifactEnvelope.from_document(envelope)
        )
        if artifact.session_id != self.session_id:
            raise HarnessError(
                "SESSION_MISMATCH",
                "Artifact belongs to a different decision session",
                details={
                    "expected_session_id": self.session_id,
                    "actual_session_id": artifact.session_id,
                },
            )
        for parent_name, parent_digest in self._artifact_object_dependencies(artifact).items():
            try:
                self.read_object(parent_digest)
            except HarnessError as exc:
                raise HarnessError(
                    "ARTIFACT_PARENT_INVALID",
                    f"Artifact parent is unavailable: {parent_name}",
                    details={
                        "parent": parent_name,
                        "sha256": parent_digest,
                        "reason_code": exc.code,
                    },
                ) from exc
        encoded = canonical_json_bytes(artifact.to_document())
        digest = sha256_bytes(encoded)
        self._write_immutable(self.paths.object_path(digest), encoded)
        return digest

    def read_object(self, digest: str) -> bytes:
        validate_digest(digest)
        path = self.paths.object_path(digest)
        raw = self._read_regular_file(path, missing_code="OBJECT_MISSING")
        actual = sha256_bytes(raw)
        if actual != digest:
            raise HarnessError(
                "OBJECT_INTEGRITY_FAILED",
                "Content-addressed object does not match its filename",
                details={"expected_sha256": digest, "actual_sha256": actual},
            )
        return raw

    def read_artifact(self, digest: str) -> ArtifactEnvelope:
        raw = self.read_object(digest)
        try:
            document = parse_json_bytes(raw, label=f"object {digest}")
            artifact = ArtifactEnvelope.from_document(document)
        except HarnessError as exc:
            raise HarnessError(
                "ARTIFACT_INTEGRITY_FAILED",
                "Referenced object is not a valid artifact envelope",
                details={"sha256": digest, "reason_code": exc.code, "reason": exc.message},
            ) from exc
        if raw != canonical_json_bytes(artifact.to_document()):
            raise HarnessError(
                "ARTIFACT_INTEGRITY_FAILED",
                "Artifact envelope is not canonical JSON",
                details={"sha256": digest},
            )
        return artifact

    def load_snapshot(self, snapshot_sha256: str) -> StoredSnapshot:
        validate_digest(snapshot_sha256, label="snapshot_sha256")
        path = self.paths.snapshot_path(snapshot_sha256)
        raw = self._read_regular_file(path, missing_code="SNAPSHOT_MISSING")
        actual = sha256_bytes(raw)
        if actual != snapshot_sha256:
            raise HarnessError(
                "SNAPSHOT_INTEGRITY_FAILED",
                "Snapshot content does not match its filename",
                details={"expected_sha256": snapshot_sha256, "actual_sha256": actual},
            )
        try:
            document = parse_json_bytes(raw, label=f"snapshot {snapshot_sha256}")
            snapshot = SnapshotRecord.from_document(document)
        except HarnessError as exc:
            raise HarnessError(
                "SNAPSHOT_INTEGRITY_FAILED",
                "Snapshot is malformed",
                details={
                    "snapshot_sha256": snapshot_sha256,
                    "reason_code": exc.code,
                    "reason": exc.message,
                },
            ) from exc
        if raw != canonical_json_bytes(snapshot.to_document()):
            raise HarnessError(
                "SNAPSHOT_INTEGRITY_FAILED",
                "Snapshot is not canonical JSON",
                details={"snapshot_sha256": snapshot_sha256},
            )
        if snapshot.session_id != self.session_id:
            raise HarnessError(
                "SNAPSHOT_INTEGRITY_FAILED",
                "Snapshot belongs to a different session",
                details={
                    "expected_session_id": self.session_id,
                    "actual_session_id": snapshot.session_id,
                },
            )
        return StoredSnapshot(sha256=snapshot_sha256, snapshot=snapshot)

    def commit(
        self,
        *,
        expected_parent: str,
        refs: Mapping[str, str],
        operation: OperationRecord | str | None = None,
    ) -> StoredSnapshot:
        validate_digest(expected_parent, label="expected_parent")
        normalized_refs = self._validate_complete_refs(refs)
        normalized_operation = (
            OperationRecord(name=operation) if isinstance(operation, str) else operation
        )
        if self._transaction_owner == threading.get_ident():
            return self._commit_locked(
                expected_parent=expected_parent,
                refs=normalized_refs,
                operation=normalized_operation,
            )
        self._ensure_store_directories()
        with self._writer_lock():
            return self._commit_locked(
                expected_parent=expected_parent,
                refs=normalized_refs,
                operation=normalized_operation,
            )

    def _commit_locked(
        self,
        *,
        expected_parent: str,
        refs: Mapping[str, str],
        operation: OperationRecord | None,
    ) -> StoredSnapshot:
        current = self.get_current()
        replay = self._find_idempotent_result(current, operation)
        if replay is not None:
            return replay
        if current.sha256 != expected_parent:
            raise HarnessError(
                "WRITE_CONFLICT",
                "Current snapshot changed since the caller read it",
                details={
                    "expected_parent": expected_parent,
                    "actual_parent": current.sha256,
                },
            )
        self._verify_refs(refs)
        snapshot = SnapshotRecord(
            session_id=self.session_id,
            generation=current.snapshot.generation + 1,
            parent_snapshot_sha256=current.sha256,
            created_at=format_timestamp(self.clock.now()),
            refs=refs,
            operation=operation,
        )
        stored = self._store_snapshot(snapshot)
        self._write_pointer(stored)
        return stored

    def verify(self, snapshot_sha256: str | None = None) -> VerificationReport:
        issues: list[IntegrityIssue] = []
        pointer: CurrentPointer | None = None
        try:
            if snapshot_sha256 is None:
                pointer = self.read_pointer()
                snapshot_sha256 = pointer.snapshot_sha256
            else:
                validate_digest(snapshot_sha256, label="snapshot_sha256")
            stored = self.load_snapshot(snapshot_sha256)
        except HarnessError as exc:
            issues.append(IntegrityIssue(exc.code, exc.message))
            return VerificationReport(False, snapshot_sha256, None, tuple(issues))

        if pointer is not None and pointer.generation != stored.snapshot.generation:
            issues.append(
                IntegrityIssue(
                    "POINTER_INTEGRITY_FAILED",
                    "Current pointer generation does not match its snapshot",
                    str(self.paths.current_pointer),
                )
            )

        seen_snapshots: set[str] = set()
        cursor = stored
        expected_generation = stored.snapshot.generation
        while True:
            if cursor.sha256 in seen_snapshots:
                issues.append(
                    IntegrityIssue(
                        "SNAPSHOT_CYCLE",
                        "Snapshot parent chain contains a cycle",
                        str(self.paths.snapshot_path(cursor.sha256)),
                    )
                )
                break
            seen_snapshots.add(cursor.sha256)
            if cursor.snapshot.generation != expected_generation:
                issues.append(
                    IntegrityIssue(
                        "SNAPSHOT_GENERATION_MISMATCH",
                        "Snapshot generations are not contiguous",
                        str(self.paths.snapshot_path(cursor.sha256)),
                    )
                )
            self._verify_snapshot_refs(cursor.snapshot, issues)
            parent = cursor.snapshot.parent_snapshot_sha256
            if parent is None:
                if cursor.snapshot.generation != 0:
                    issues.append(
                        IntegrityIssue(
                            "SNAPSHOT_ROOT_INVALID",
                            "Only generation zero may omit a parent snapshot",
                            str(self.paths.snapshot_path(cursor.sha256)),
                        )
                    )
                break
            try:
                cursor = self.load_snapshot(parent)
            except HarnessError as exc:
                issues.append(IntegrityIssue(exc.code, exc.message))
                break
            expected_generation -= 1

        return VerificationReport(
            ok=not issues,
            snapshot_sha256=stored.sha256,
            generation=stored.snapshot.generation,
            issues=tuple(issues),
        )

    def doctor(self) -> DoctorReport:
        session_ids, session_path_issues = self._discover_session_ids()
        if self.session_id in session_ids:
            verification = self.verify()
        else:
            selected_issue = next(
                (
                    issue
                    for issue in session_path_issues
                    if issue.path == str(self.paths.session_root)
                ),
                IntegrityIssue(
                    "SESSION_NOT_FOUND",
                    f"Decision session does not exist: {self.session_id}",
                    str(self.paths.session_root),
                ),
            )
            verification = VerificationReport(False, None, None, (selected_issue,))
        issues = list(verification.issues)
        issues.extend(issue for issue in session_path_issues if issue not in issues)
        reachable_snapshots: set[str] = set()
        selected_objects: set[str] = set()
        current_sha = verification.snapshot_sha256

        if current_sha is not None:
            reachable_snapshots, selected_objects = self._collect_session_reachability(current_sha)

        # Objects are shared by every v2 session in the project, while snapshots are
        # session-local.  Only integrity-valid current chains make objects globally live.
        referenced_objects: set[str] = set()
        if verification.ok:
            referenced_objects.update(selected_objects)
        for session_id in sorted(session_ids - {self.session_id}):
            other_store = DecisionStore(
                self.project_root,
                session_id,
                clock=self.clock,
                id_source=self.id_source,
                lock_timeout=self.lock_timeout,
            )
            other_verification = other_store.verify()
            if not other_verification.ok or other_verification.snapshot_sha256 is None:
                issues.extend(
                    IntegrityIssue(
                        issue.code,
                        f"Session {session_id}: {issue.message}",
                        issue.path or str(other_store.paths.session_root),
                    )
                    for issue in other_verification.issues
                )
                continue
            _, other_objects = other_store._collect_session_reachability(
                other_verification.snapshot_sha256
            )
            referenced_objects.update(other_objects)

        all_snapshots, snapshot_path_issues = self._discover_digests(
            self.paths.snapshots_root,
            suffix=".json",
        )
        all_objects, object_path_issues = self._discover_digests(self.paths.objects_root)
        issues.extend(snapshot_path_issues)
        issues.extend(object_path_issues)
        writer_lock = self._inspect_writer_lock()
        return DoctorReport(
            ok=not issues and writer_lock.state == "absent",
            current_snapshot_sha256=current_sha,
            reachable_snapshots=tuple(sorted(reachable_snapshots)),
            orphan_snapshots=tuple(sorted(all_snapshots - reachable_snapshots)),
            referenced_objects=tuple(sorted(referenced_objects)),
            orphan_objects=tuple(sorted(all_objects - referenced_objects)),
            issues=tuple(issues),
            writer_lock=writer_lock,
        )

    def _inspect_writer_lock(self) -> WriterLockReport:
        """Observe a lock without acquiring, removing, or claiming ownership of it."""

        path = self.paths.writer_lock
        try:
            self._assert_safe_read_path(path)
            before = path.lstat()
        except FileNotFoundError:
            return WriterLockReport(state="absent")
        except (HarnessError, OSError):
            return WriterLockReport(state="unreadable", issue="LOCK_PATH_UNREADABLE")
        if not stat.S_ISREG(before.st_mode) or before.st_size > 4096:
            return WriterLockReport(state="invalid", issue="INVALID_LOCK_METADATA")
        try:
            raw = self._read_regular_file(path, missing_code="LOCK_MISSING")
            after = path.lstat()
        except FileNotFoundError:
            return WriterLockReport(state="changed", issue="LOCK_CHANGED_DURING_READ")
        except HarnessError as exc:
            if exc.code == "LOCK_MISSING":
                return WriterLockReport(state="changed", issue="LOCK_CHANGED_DURING_READ")
            return WriterLockReport(state="unreadable", issue="LOCK_PATH_UNREADABLE")
        except OSError:
            return WriterLockReport(state="unreadable", issue="LOCK_PATH_UNREADABLE")
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            return WriterLockReport(state="changed", issue="LOCK_CHANGED_DURING_READ")
        try:
            metadata = parse_json_bytes(raw, label="writer lock")
            if not isinstance(metadata, dict) or set(metadata) != {"pid", "acquired_at"}:
                raise ValueError("Invalid lock fields")
            pid = metadata["pid"]
            acquired_at = metadata["acquired_at"]
            if (
                not isinstance(pid, int)
                or isinstance(pid, bool)
                or not 0 < pid <= 2**31 - 1
                or not isinstance(acquired_at, str)
                or not acquired_at.endswith("Z")
            ):
                raise ValueError("Invalid lock metadata")
            datetime.fromisoformat(acquired_at)
        except (HarnessError, ValueError):
            return WriterLockReport(state="invalid", issue="INVALID_LOCK_METADATA")
        return WriterLockReport(
            state="present",
            pid=pid,
            acquired_at=acquired_at,
            owner_status=self._probe_lock_pid(pid),
        )

    @staticmethod
    def _probe_lock_pid(pid: int) -> str:
        # Signal zero is a read-only existence probe on POSIX. Do not use os.kill
        # on Windows, where signals have different semantics. PID presence never
        # establishes lock ownership (PID reuse); neither result permits deletion.
        if os.name != "posix":
            return "unknown"
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return "pid_absent"
        except (OSError, OverflowError):
            return "unknown"
        return "pid_present"

    def _collect_session_reachability(self, current_sha: str) -> tuple[set[str], set[str]]:
        reachable_snapshots: set[str] = set()
        referenced_objects: set[str] = set()
        cursor_sha: str | None = current_sha
        while cursor_sha is not None and cursor_sha not in reachable_snapshots:
            reachable_snapshots.add(cursor_sha)
            try:
                cursor = self.load_snapshot(cursor_sha)
            except HarnessError:
                break
            for artifact_sha in cursor.snapshot.refs.values():
                self._collect_object_graph(artifact_sha, referenced_objects)
            cursor_sha = cursor.snapshot.parent_snapshot_sha256
        return reachable_snapshots, referenced_objects

    def _discover_session_ids(self) -> tuple[set[str], list[IntegrityIssue]]:
        sessions_root = self.paths.v2_root / "sessions"
        try:
            self._assert_safe_read_path(sessions_root)
        except HarnessError as exc:
            return set(), [IntegrityIssue(exc.code, exc.message, str(sessions_root))]
        if not sessions_root.exists():
            return set(), []
        if not sessions_root.is_dir():
            return set(), [
                IntegrityIssue(
                    "UNSAFE_STORAGE_PATH",
                    "The v2 sessions path must be a directory",
                    str(sessions_root),
                )
            ]

        session_ids: set[str] = set()
        issues: list[IntegrityIssue] = []
        try:
            entries = sorted(sessions_root.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            return set(), [
                IntegrityIssue(
                    "STORAGE_SCAN_FAILED",
                    f"Could not scan v2 sessions: {exc}",
                    str(sessions_root),
                )
            ]
        for path in entries:
            if path.is_symlink() or not path.is_dir():
                issues.append(
                    IntegrityIssue(
                        "UNSAFE_STORAGE_PATH",
                        "Every v2 session entry must be a real directory",
                        str(path),
                    )
                )
                continue
            try:
                session_ids.add(validate_safe_id(path.name, label="session_id"))
            except HarnessError as exc:
                issues.append(IntegrityIssue(exc.code, exc.message, str(path)))
        return session_ids, issues

    def _validate_complete_refs(self, refs: Mapping[str, str]) -> dict[str, str]:
        if not isinstance(refs, Mapping):
            raise HarnessError("INVALID_REFS", "refs must be a mapping")
        result: dict[str, str] = {}
        for ref_name, digest in refs.items():
            validate_ref_key(ref_name, label="refs key")
            if ref_name not in REF_ARTIFACT_TYPES:
                raise HarnessError(
                    "UNKNOWN_ARTIFACT_REF",
                    "Snapshot contains an undeclared artifact reference",
                    details={"ref_name": ref_name},
                )
            validate_digest(digest, label=f"refs.{ref_name}")
            result[ref_name] = digest
        return result

    def _verify_refs(self, refs: Mapping[str, str]) -> None:
        issues: list[IntegrityIssue] = []
        self._verify_snapshot_refs(
            SnapshotRecord(
                session_id=self.session_id,
                generation=0,
                parent_snapshot_sha256=None,
                created_at=format_timestamp(self.clock.now()),
                refs=refs,
            ),
            issues,
        )
        if issues:
            first = issues[0]
            raise HarnessError(
                "INVALID_SNAPSHOT_REFS",
                "Snapshot refs do not resolve to valid session artifacts",
                details={
                    "issues": [
                        {"code": issue.code, "message": issue.message, "path": issue.path}
                        for issue in issues
                    ],
                    "first_issue": first.code,
                },
            )

    def _verify_snapshot_refs(
        self,
        snapshot: SnapshotRecord,
        issues: list[IntegrityIssue],
    ) -> None:
        checked: set[str] = set()
        for ref_name, digest in snapshot.refs.items():
            expected_type = REF_ARTIFACT_TYPES.get(ref_name)
            if expected_type is None:
                issues.append(
                    IntegrityIssue(
                        "UNKNOWN_ARTIFACT_REF",
                        f"Snapshot contains an undeclared artifact reference: {ref_name}",
                    )
                )
            self._verify_artifact_graph(
                digest,
                checked,
                issues,
                expected_type=expected_type,
                ref_name=ref_name,
            )

    def _verify_artifact_graph(
        self,
        digest: str,
        checked: set[str],
        issues: list[IntegrityIssue],
        *,
        expected_type: str | None = None,
        ref_name: str | None = None,
    ) -> None:
        try:
            artifact = self.read_artifact(digest)
        except HarnessError as exc:
            issues.append(
                IntegrityIssue(exc.code, exc.message, str(self.paths.object_path(digest)))
            )
            return
        if expected_type is not None and artifact.artifact_type != expected_type:
            issues.append(
                IntegrityIssue(
                    "ARTIFACT_TYPE_MISMATCH",
                    (
                        f"Reference {ref_name} requires {expected_type}, "
                        f"not {artifact.artifact_type}"
                    ),
                    str(self.paths.object_path(digest)),
                )
            )
        if digest in checked:
            return
        checked.add(digest)
        if artifact.session_id != self.session_id:
            issues.append(
                IntegrityIssue(
                    "SESSION_MISMATCH",
                    "Artifact graph crosses decision session boundaries",
                    str(self.paths.object_path(digest)),
                )
            )
        for parent_name, parent_digest in artifact.parents.items():
            parent_type = REF_ARTIFACT_TYPES.get(parent_name)
            if parent_type is None:
                issues.append(
                    IntegrityIssue(
                        "UNKNOWN_ARTIFACT_REF",
                        f"Artifact contains an undeclared parent reference: {parent_name}",
                        str(self.paths.object_path(digest)),
                    )
                )
            self._verify_artifact_graph(
                parent_digest,
                checked,
                issues,
                expected_type=parent_type,
                ref_name=parent_name,
            )
        expected_blob_bytes = self._artifact_blob_byte_counts(artifact)
        for dependency_name, blob_digest in self._artifact_blob_dependencies(artifact).items():
            try:
                raw = self.read_object(blob_digest)
            except HarnessError as exc:
                issues.append(
                    IntegrityIssue(
                        exc.code,
                        exc.message,
                        str(self.paths.object_path(blob_digest)),
                    )
                )
                continue
            expected_bytes = expected_blob_bytes.get(dependency_name)
            if expected_bytes is not None and len(raw) != expected_bytes:
                issues.append(
                    IntegrityIssue(
                        "BLOB_SIZE_MISMATCH",
                        (
                            f"{dependency_name} declares {expected_bytes} bytes, "
                            f"but the bound blob has {len(raw)}"
                        ),
                        str(self.paths.object_path(blob_digest)),
                    )
                )
            if artifact.artifact_type in {"source-manifest", "migration-report"}:
                try:
                    raw.decode("utf-8")
                except UnicodeDecodeError:
                    issues.append(
                        IntegrityIssue(
                            "BLOB_ENCODING_INVALID",
                            f"{dependency_name} is not valid UTF-8 text",
                            str(self.paths.object_path(blob_digest)),
                        )
                    )
        duplicate_ids = self._artifact_duplicate_source_ids(artifact)
        if duplicate_ids:
            issues.append(
                IntegrityIssue(
                    "DUPLICATE_SOURCE_ID",
                    "Artifact contains duplicate source identifiers: " + ", ".join(duplicate_ids),
                    str(self.paths.object_path(digest)),
                )
            )

    @staticmethod
    def _possible_envelope(raw: bytes) -> Mapping[str, Any] | None:
        try:
            value = parse_json_bytes(raw, label="parent object")
        except HarnessError:
            return None
        required = {
            "schema_version",
            "artifact_type",
            "session_id",
            "producer",
            "parents",
            "payload",
        }
        return value if isinstance(value, Mapping) and set(value) == required else None

    def _find_idempotent_result(
        self,
        current: StoredSnapshot,
        operation: OperationRecord | None,
    ) -> StoredSnapshot | None:
        if operation is None or operation.idempotency_key is None:
            return None
        cursor = current
        visited: set[str] = set()
        while cursor.sha256 not in visited:
            visited.add(cursor.sha256)
            recorded = cursor.snapshot.operation
            if recorded is not None and recorded.idempotency_key == operation.idempotency_key:
                if (
                    recorded.name != operation.name
                    or recorded.request_sha256 != operation.request_sha256
                ):
                    raise HarnessError(
                        "IDEMPOTENCY_KEY_REUSE",
                        "Idempotency key was already used for a different request",
                        details={"idempotency_key": operation.idempotency_key},
                    )
                return cursor
            parent = cursor.snapshot.parent_snapshot_sha256
            if parent is None:
                return None
            cursor = self.load_snapshot(parent)
        raise HarnessError("SNAPSHOT_CYCLE", "Snapshot parent chain contains a cycle")

    def _collect_object_graph(self, digest: str, found: set[str]) -> None:
        if digest in found:
            return
        found.add(digest)
        try:
            artifact = self.read_artifact(digest)
        except HarnessError:
            return
        for parent_digest in artifact.parents.values():
            self._collect_object_graph(parent_digest, found)
        for blob_digest in self._artifact_blob_dependencies(artifact).values():
            found.add(blob_digest)

    @staticmethod
    def _artifact_object_dependencies(artifact: ArtifactEnvelope) -> dict[str, str]:
        dependencies = dict(artifact.parents)
        dependencies.update(DecisionStore._artifact_blob_dependencies(artifact))
        return dependencies

    @staticmethod
    def _artifact_blob_dependencies(artifact: ArtifactEnvelope) -> dict[str, str]:
        dependencies: dict[str, str] = {}
        if artifact.artifact_type == "source-manifest":
            for index, source in enumerate(artifact.payload["sources"]):
                dependencies[f"source:{source['source_id']}:{index}"] = source["blob_sha256"]
        elif artifact.artifact_type == "migration-report":
            for index, source in enumerate(artifact.payload["migrated_sources"]):
                dependencies[f"migrated-source:{source['source_id']}:{index}"] = source[
                    "blob_sha256"
                ]
        elif artifact.artifact_type == "agent-run":
            dependencies["transcript"] = artifact.payload["transcript_sha256"]
        return dependencies

    @staticmethod
    def _artifact_blob_byte_counts(artifact: ArtifactEnvelope) -> dict[str, int]:
        expected: dict[str, int] = {}
        if artifact.artifact_type == "source-manifest":
            for index, source in enumerate(artifact.payload["sources"]):
                expected[f"source:{source['source_id']}:{index}"] = source["bytes"]
        elif artifact.artifact_type == "migration-report":
            for index, source in enumerate(artifact.payload["migrated_sources"]):
                expected[f"migrated-source:{source['source_id']}:{index}"] = source["bytes"]
        return expected

    @staticmethod
    def _artifact_duplicate_source_ids(artifact: ArtifactEnvelope) -> list[str]:
        if artifact.artifact_type == "source-manifest":
            sources = artifact.payload["sources"]
        elif artifact.artifact_type == "migration-report":
            sources = artifact.payload["migrated_sources"]
        else:
            return []
        seen: set[str] = set()
        duplicates: set[str] = set()
        for source in sources:
            source_id = source["source_id"]
            if source_id in seen:
                duplicates.add(source_id)
            seen.add(source_id)
        return sorted(duplicates)

    def _store_snapshot(self, snapshot: SnapshotRecord) -> StoredSnapshot:
        encoded = canonical_json_bytes(snapshot.to_document())
        digest = sha256_bytes(encoded)
        self._write_immutable(self.paths.snapshot_path(digest), encoded)
        return StoredSnapshot(sha256=digest, snapshot=snapshot)

    def _write_pointer(self, stored: StoredSnapshot) -> None:
        pointer = CurrentPointer(
            session_id=self.session_id,
            generation=stored.snapshot.generation,
            snapshot_sha256=stored.sha256,
        )
        self._atomic_replace(
            self.paths.current_pointer,
            canonical_json_bytes(pointer.to_document()),
        )

    def _ensure_store_directories(self) -> None:
        self._safe_mkdir(self.paths.objects_root)
        self._safe_mkdir(self.paths.snapshots_root)

    def _safe_mkdir(self, path: Path) -> None:
        try:
            relative = path.relative_to(self.project_root)
        except ValueError as exc:  # pragma: no cover - paths are constructed internally.
            raise HarnessError("UNSAFE_STORAGE_PATH", "Storage path escapes project root") from exc
        cursor = self.project_root
        for part in relative.parts:
            cursor /= part
            if cursor.is_symlink():
                raise HarnessError(
                    "UNSAFE_STORAGE_PATH",
                    "Storage directories must not be symbolic links",
                    details={"path": str(cursor)},
                )
            try:
                cursor.mkdir()
            except FileExistsError:
                if not cursor.is_dir():
                    raise HarnessError(
                        "UNSAFE_STORAGE_PATH",
                        "Storage path component is not a directory",
                        details={"path": str(cursor)},
                    ) from None

    @contextmanager
    def _writer_lock(self) -> Iterator[None]:
        self._safe_mkdir(self.paths.session_root)
        deadline = time.monotonic() + self.lock_timeout
        descriptor: int | None = None
        while descriptor is None:
            try:
                descriptor = os.open(
                    self.paths.writer_lock,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise HarnessError(
                        "WRITE_LOCK_TIMEOUT",
                        "Timed out waiting for the decision session writer lock",
                        details={"timeout_seconds": self.lock_timeout},
                    ) from None
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        try:
            metadata = canonical_json_bytes(
                {"acquired_at": format_timestamp(self.clock.now()), "pid": os.getpid()}
            )
            os.write(descriptor, metadata)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            yield
        finally:
            if descriptor is not None:
                os.close(descriptor)
            with suppress(FileNotFoundError):
                self.paths.writer_lock.unlink()
            self._fsync_directory(self.paths.session_root)

    def _write_immutable(self, path: Path, value: bytes) -> None:
        self._safe_mkdir(path.parent)
        descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temp_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                existing = self._read_regular_file(path, missing_code="CAS_WRITE_FAILED")
                if existing != value:
                    raise HarnessError(
                        "CAS_COLLISION",
                        "Immutable content-addressed path already contains different bytes",
                        details={"path": str(path)},
                    ) from None
            self._fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def _atomic_replace(self, path: Path, value: bytes) -> None:
        self._safe_mkdir(path.parent)
        if path.is_symlink():
            raise HarnessError(
                "UNSAFE_STORAGE_PATH",
                "Current pointer must not be a symbolic link",
                details={"path": str(path)},
            )
        descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temp_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            self._fsync_directory(path.parent)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def _read_regular_file(self, path: Path, *, missing_code: str) -> bytes:
        self._assert_safe_read_path(path)
        if not path.is_file() or path.is_symlink():
            raise HarnessError(
                missing_code,
                f"Required regular file is missing: {path.name}",
                details={"path": str(path)},
            )
        try:
            return path.read_bytes()
        except OSError as exc:
            raise HarnessError(
                "ARTIFACT_READ_FAILED",
                f"Could not read artifact: {path.name}",
                details={"path": str(path), "reason": str(exc)},
            ) from exc

    def _assert_safe_read_path(self, path: Path) -> None:
        try:
            relative = path.relative_to(self.project_root)
        except ValueError as exc:
            raise HarnessError(
                "UNSAFE_STORAGE_PATH",
                "Storage read path escapes project root",
                details={"path": str(path)},
            ) from exc
        cursor = self.project_root
        for part in relative.parts:
            cursor /= part
            if cursor.is_symlink():
                raise HarnessError(
                    "UNSAFE_STORAGE_PATH",
                    "Storage read paths must not traverse symbolic links",
                    details={"path": str(cursor)},
                )

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":  # Windows has no portable directory fsync operation.
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _discover_digests(
        self,
        root: Path,
        *,
        suffix: str = "",
    ) -> tuple[set[str], list[IntegrityIssue]]:
        found: set[str] = set()
        issues: list[IntegrityIssue] = []
        try:
            self._assert_safe_read_path(root)
        except HarnessError as exc:
            return found, [IntegrityIssue(exc.code, exc.message, str(root))]
        if not root.is_dir() or root.is_symlink():
            return found, issues
        for path in root.glob("*/*"):
            if not path.is_file() or path.is_symlink() or path.name.startswith("."):
                continue
            name = path.name.removesuffix(suffix) if suffix else path.name
            try:
                validate_digest(name)
            except HarnessError:
                issues.append(
                    IntegrityIssue(
                        "UNEXPECTED_STORAGE_FILE",
                        "Storage contains a file without a digest name",
                        str(path),
                    )
                )
                continue
            if path.parent.name != name[:2]:
                issues.append(
                    IntegrityIssue(
                        "MISPLACED_STORAGE_FILE",
                        "Digest file is stored under the wrong prefix directory",
                        str(path),
                    )
                )
            found.add(name)
        return found, issues
