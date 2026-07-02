"""Session bundle integrity verification and rebuildable index projection."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from mena.artifacts import (
    ARTIFACT_MANIFEST_FILENAME,
    ARTIFACT_MANIFEST_VERSION,
    ArtifactRecord,
    ArtifactStoreError,
    session_artifact_manifest_path,
    session_artifact_root,
)
from mena.processing import ArtifactRef, ProcessingStageState, ProcessingStateError
from mena.sessions import SESSION_RECORD_FILENAME, read_session_record


class SessionIntegrityError(RuntimeError):
    """Raised when a session bundle cannot be trusted for downstream use."""


@dataclass(frozen=True)
class SessionIntegrityFinding:
    """One machine-readable session integrity diagnostic."""

    severity: str
    code: str
    path: str
    message: str

    def to_dict(self) -> dict[str, object]:
        return {
            "severity": self.severity,
            "code": self.code,
            "path": self.path,
            "message": self.message,
        }


@dataclass(frozen=True)
class SessionIntegrityReport:
    """Integrity result for one session bundle."""

    session_id: str | None
    session_dir: Path
    artifact_count: int
    capture_segment_count: int
    findings: tuple[SessionIntegrityFinding, ...]

    @property
    def ok(self) -> bool:
        return not any(finding.severity == "error" for finding in self.findings)

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "session_dir": str(self.session_dir),
            "artifact_count": self.artifact_count,
            "capture_segment_count": self.capture_segment_count,
            "ok": self.ok,
            "findings": [finding.to_dict() for finding in self.findings],
        }


@dataclass(frozen=True)
class VerifiedSessionBundle:
    """Session record plus an integrity report that has no error findings."""

    session_dir: Path
    record_path: Path
    record: Mapping[str, object]
    integrity: SessionIntegrityReport


@dataclass(frozen=True)
class SessionIndexEntry:
    """Rebuildable global-index projection for one session bundle."""

    session_id: str
    bundle_path: Path
    created_at: str
    status: str
    profile: str
    name: str | None
    integrity_ok: bool
    finding_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "bundle_path": str(self.bundle_path),
            "created_at": self.created_at,
            "status": self.status,
            "profile": self.profile,
            "name": self.name,
            "integrity_ok": self.integrity_ok,
            "finding_count": self.finding_count,
        }


def load_verified_session_bundle(session_path: Path) -> VerifiedSessionBundle:
    """Load a session only if its bundle passes integrity verification."""

    session_dir, record_path = _resolve_session_paths(session_path)
    report = verify_session_bundle(session_path)
    if not report.ok:
        details = "; ".join(f"{finding.code}: {finding.message}" for finding in report.findings)
        raise SessionIntegrityError(f"Session bundle failed integrity verification: {details}")
    return VerifiedSessionBundle(
        session_dir=session_dir,
        record_path=record_path,
        record=read_session_record(record_path),
        integrity=report,
    )


def verify_session_bundle(session_path: Path) -> SessionIntegrityReport:
    """Verify one session bundle without repairing or mutating it."""

    findings: list[SessionIntegrityFinding] = []
    session_dir, record_path = _resolve_session_paths(session_path)
    record = _read_record(record_path, findings)
    session_id = _optional_record_str(record, "id")

    if session_id is None:
        findings.append(
            _finding(
                code="session_id_missing",
                path=_display_path(record_path, session_dir=session_dir),
                message="Session record id must be a non-empty string.",
            )
        )

    expected_files: set[Path] = set()
    capture_segment_count = _verify_capture_metadata(
        session_dir=session_dir,
        record=record,
        expected_files=expected_files,
        findings=findings,
    )

    records: tuple[ArtifactRecord, ...] = ()
    record_by_id: dict[str, ArtifactRecord] = {}
    verified_artifact_ids: set[str] = set()
    if session_id is not None:
        records = _load_manifest_records(
            session_dir=session_dir,
            session_id=session_id,
            findings=findings,
        )
        record_by_id = {record.id: record for record in records}
        for record_item in records:
            expected_files.add(_normal_path(session_dir / record_item.path))
            if _verify_manifest_artifact(
                session_dir=session_dir,
                record=record_item,
                findings=findings,
            ):
                verified_artifact_ids.add(record_item.id)

        for record_item in records:
            if record_item.id not in verified_artifact_ids:
                continue
            _verify_artifact_payload_references(
                session_dir=session_dir,
                record=record_item,
                record_by_id=record_by_id,
                verified_artifact_ids=verified_artifact_ids,
                session_id=session_id,
                findings=findings,
            )

    _verify_orphaned_artifact_files(
        session_dir=session_dir,
        expected_files=expected_files,
        findings=findings,
    )
    return SessionIntegrityReport(
        session_id=session_id,
        session_dir=session_dir,
        artifact_count=len(records),
        capture_segment_count=capture_segment_count,
        findings=tuple(findings),
    )


def rebuild_session_index(sessions_root: Path) -> tuple[SessionIndexEntry, ...]:
    """Rebuild the narrow global session index projection from session bundles."""

    entries: list[SessionIndexEntry] = []
    if not sessions_root.exists():
        return ()
    for bundle_path in sorted(path for path in sessions_root.iterdir() if path.is_dir()):
        record_path = bundle_path / SESSION_RECORD_FILENAME
        if not record_path.exists():
            continue
        report = verify_session_bundle(bundle_path)
        try:
            record = read_session_record(record_path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        session_id = _optional_record_str(record, "id")
        created_at = _optional_record_str(record, "created_at")
        status = _optional_record_str(record, "status")
        profile = _optional_record_str(record, "profile")
        name = record.get("name")
        if (
            session_id is None
            or created_at is None
            or status is None
            or profile is None
            or (name is not None and not isinstance(name, str))
        ):
            continue
        entries.append(
            SessionIndexEntry(
                session_id=session_id,
                bundle_path=bundle_path,
                created_at=created_at,
                status=status,
                profile=profile,
                name=name,
                integrity_ok=report.ok,
                finding_count=len(report.findings),
            )
        )
    return tuple(entries)


def _verify_capture_metadata(
    *,
    session_dir: Path,
    record: Mapping[str, object],
    expected_files: set[Path],
    findings: list[SessionIntegrityFinding],
) -> int:
    capture_metadata = record.get("capture_metadata")
    if capture_metadata is None:
        return 0
    if not isinstance(capture_metadata, dict):
        findings.append(
            _finding(
                code="capture_metadata_invalid",
                path="session.json:capture_metadata",
                message="Capture metadata must be a JSON object.",
            )
        )
        return 0
    raw_segments = capture_metadata.get("segments")
    if not isinstance(raw_segments, list):
        findings.append(
            _finding(
                code="capture_segments_invalid",
                path="session.json:capture_metadata.segments",
                message="Capture metadata segments must be an array.",
            )
        )
        return 0
    segment_count = capture_metadata.get("segment_count")
    if type(segment_count) is int and segment_count != len(raw_segments):
        findings.append(
            _finding(
                code="capture_segment_count_mismatch",
                path="session.json:capture_metadata.segment_count",
                message=(
                    f"Capture metadata declares {segment_count} segments but "
                    f"contains {len(raw_segments)}."
                ),
            )
        )

    for index, raw_segment in enumerate(raw_segments, start=1):
        segment_path = f"session.json:capture_metadata.segments[{index - 1}]"
        if not isinstance(raw_segment, dict):
            findings.append(
                _finding(
                    code="capture_segment_invalid",
                    path=segment_path,
                    message="Capture segment metadata must be a JSON object.",
                )
            )
            continue
        path_value = raw_segment.get("path")
        if not isinstance(path_value, str) or not path_value:
            findings.append(
                _finding(
                    code="capture_segment_path_invalid",
                    path=f"{segment_path}.path",
                    message="Capture segment path must be a non-empty string.",
                )
            )
            continue
        resolved = _resolve_recorded_path(session_dir=session_dir, path_value=path_value)
        if _is_relative_to(resolved, session_dir):
            expected_files.add(_normal_path(resolved))
        _verify_file_bytes(
            file_path=resolved,
            display_path=_display_path(resolved, session_dir=session_dir),
            expected_sha=_optional_str(raw_segment, "sha256"),
            expected_byte_count=_optional_int(raw_segment, "byte_count"),
            code_prefix="capture_segment",
            findings=findings,
        )
    return len(raw_segments)


def _load_manifest_records(
    *,
    session_dir: Path,
    session_id: str,
    findings: list[SessionIntegrityFinding],
) -> tuple[ArtifactRecord, ...]:
    manifest_path = session_artifact_manifest_path(session_dir)
    if not manifest_path.exists():
        return ()
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        findings.append(
            _finding(
                code="artifact_manifest_invalid",
                path=_display_path(manifest_path, session_dir=session_dir),
                message=f"Artifact manifest is not readable JSON: {exc}",
            )
        )
        return ()
    if not isinstance(data, dict):
        findings.append(
            _finding(
                code="artifact_manifest_invalid",
                path=_display_path(manifest_path, session_dir=session_dir),
                message="Artifact manifest must be a JSON object.",
            )
        )
        return ()
    if data.get("version") != ARTIFACT_MANIFEST_VERSION:
        findings.append(
            _finding(
                code="artifact_manifest_version_unsupported",
                path=_display_path(manifest_path, session_dir=session_dir),
                message="Artifact manifest version is unsupported.",
            )
        )
    if data.get("session_id") != session_id:
        findings.append(
            _finding(
                code="artifact_manifest_session_mismatch",
                path=_display_path(manifest_path, session_dir=session_dir),
                message="Artifact manifest session_id does not match session record.",
            )
        )
    artifacts = data.get("artifacts")
    if not isinstance(artifacts, list):
        findings.append(
            _finding(
                code="artifact_manifest_records_invalid",
                path=_display_path(manifest_path, session_dir=session_dir),
                message="Artifact manifest artifacts must be an array.",
            )
        )
        return ()

    records: list[ArtifactRecord] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(artifacts):
        item_path = f"{_display_path(manifest_path, session_dir=session_dir)}:artifacts[{index}]"
        if not isinstance(item, dict):
            findings.append(
                _finding(
                    code="artifact_record_invalid",
                    path=item_path,
                    message="Artifact record must be a JSON object.",
                )
            )
            continue
        try:
            record = ArtifactRecord.from_dict(item)
        except ArtifactStoreError as exc:
            findings.append(
                _finding(
                    code="artifact_record_invalid",
                    path=item_path,
                    message=str(exc),
                )
            )
            continue
        if record.session_id != session_id:
            findings.append(
                _finding(
                    code="artifact_record_session_mismatch",
                    path=item_path,
                    message="Artifact record session_id does not match session record.",
                )
            )
            continue
        if record.id in seen_ids:
            findings.append(
                _finding(
                    code="artifact_record_duplicate",
                    path=item_path,
                    message=f"Duplicate artifact id in manifest: {record.id}",
                )
            )
            continue
        seen_ids.add(record.id)
        records.append(record)
    return tuple(sorted(records, key=lambda record: record.path))


def _verify_manifest_artifact(
    *,
    session_dir: Path,
    record: ArtifactRecord,
    findings: list[SessionIntegrityFinding],
) -> bool:
    return _verify_file_bytes(
        file_path=session_dir / record.path,
        display_path=record.path,
        expected_sha=record.sha256,
        expected_byte_count=record.byte_count,
        code_prefix="artifact",
        findings=findings,
    )


def _verify_artifact_payload_references(
    *,
    session_dir: Path,
    record: ArtifactRecord,
    record_by_id: Mapping[str, ArtifactRecord],
    verified_artifact_ids: set[str],
    session_id: str,
    findings: list[SessionIntegrityFinding],
) -> None:
    data = (session_dir / record.path).read_bytes()
    payload = _decode_json_payload(data)
    if payload is None:
        return
    if isinstance(payload, dict):
        payload_session_id = payload.get("session_id")
        if isinstance(payload_session_id, str) and payload_session_id != session_id:
            findings.append(
                _finding(
                    code="artifact_payload_session_mismatch",
                    path=record.path,
                    message="Artifact payload session_id does not match session record.",
                )
            )
    if record.artifact_type == "processing_state":
        try:
            state = ProcessingStageState.from_dict(payload)
        except ProcessingStateError as exc:
            findings.append(
                _finding(
                    code="processing_state_invalid",
                    path=record.path,
                    message=str(exc),
                )
            )
        else:
            if state.session_id != session_id:
                findings.append(
                    _finding(
                        code="processing_state_session_mismatch",
                        path=record.path,
                        message="Processing state session_id does not match session record.",
                    )
                )

    for ref_path, artifact_ref in _iter_artifact_refs(payload, root=record.path):
        referenced = record_by_id.get(artifact_ref.id)
        if referenced is None:
            findings.append(
                _finding(
                    code="artifact_ref_missing",
                    path=ref_path,
                    message=f"Artifact reference points to unknown artifact id: {artifact_ref.id}",
                )
            )
            continue
        if (
            referenced.artifact_type != artifact_ref.artifact_type
            or referenced.version != artifact_ref.version
            or referenced.sha256 != artifact_ref.sha256
        ):
            findings.append(
                _finding(
                    code="artifact_ref_metadata_mismatch",
                    path=ref_path,
                    message="Artifact reference metadata does not match the manifest record.",
                )
            )
        if referenced.id not in verified_artifact_ids:
            findings.append(
                _finding(
                    code="artifact_ref_invalid_target",
                    path=ref_path,
                    message="Artifact reference points to an artifact that failed verification.",
                )
            )

    for metadata_path, artifact_id in _iter_metadata_artifact_ids(record.metadata):
        if artifact_id not in record_by_id:
            findings.append(
                _finding(
                    code="artifact_metadata_ref_missing",
                    path=f"{record.path}:metadata.{metadata_path}",
                    message=f"Artifact metadata references unknown artifact id: {artifact_id}",
                )
            )


def _verify_orphaned_artifact_files(
    *,
    session_dir: Path,
    expected_files: set[Path],
    findings: list[SessionIntegrityFinding],
) -> None:
    artifact_root = session_artifact_root(session_dir)
    if not artifact_root.exists():
        return
    for path in sorted(item for item in artifact_root.rglob("*") if item.is_file()):
        if path.name == ARTIFACT_MANIFEST_FILENAME or path.name.startswith("."):
            continue
        if _normal_path(path) not in expected_files:
            findings.append(
                _finding(
                    code="orphaned_artifact_file",
                    path=_display_path(path, session_dir=session_dir),
                    message="Artifact file is not referenced by the manifest or capture metadata.",
                )
            )


def _verify_file_bytes(
    *,
    file_path: Path,
    display_path: str,
    expected_sha: str | None,
    expected_byte_count: int | None,
    code_prefix: str,
    findings: list[SessionIntegrityFinding],
) -> bool:
    if not file_path.exists():
        findings.append(
            _finding(
                code=f"{code_prefix}_missing",
                path=display_path,
                message="Referenced artifact file is missing.",
            )
        )
        return False
    if not file_path.is_file():
        findings.append(
            _finding(
                code=f"{code_prefix}_not_file",
                path=display_path,
                message="Referenced artifact path is not a file.",
            )
        )
        return False
    data = file_path.read_bytes()
    ok = True
    if expected_byte_count is not None and len(data) != expected_byte_count:
        findings.append(
            _finding(
                code=f"{code_prefix}_byte_count_mismatch",
                path=display_path,
                message=(f"Expected {expected_byte_count} bytes but found {len(data)} bytes."),
            )
        )
        ok = False
    if expected_sha is not None:
        actual_sha = hashlib.sha256(data).hexdigest()
        if actual_sha != expected_sha:
            findings.append(
                _finding(
                    code=f"{code_prefix}_sha256_mismatch",
                    path=display_path,
                    message="File sha256 does not match recorded metadata.",
                )
            )
            ok = False
    return ok


def _iter_artifact_refs(data: object, *, root: str) -> Iterable[tuple[str, ArtifactRef]]:
    if isinstance(data, dict):
        if {"id", "artifact_type", "version", "sha256"}.issubset(data):
            try:
                yield root, ArtifactRef.from_dict(data)
            except ProcessingStateError:
                return
        for key, value in data.items():
            yield from _iter_artifact_refs(value, root=f"{root}.{key}")
    elif isinstance(data, list):
        for index, item in enumerate(data):
            yield from _iter_artifact_refs(item, root=f"{root}[{index}]")


def _iter_metadata_artifact_ids(metadata: Mapping[str, object]) -> Iterable[tuple[str, str]]:
    for key, value in metadata.items():
        if key.endswith("_artifact_id") and isinstance(value, str):
            yield key, value


def _decode_json_payload(data: bytes) -> object | None:
    stripped = data.lstrip()
    if not stripped or stripped[:1] not in {b"{", b"["}:
        return None
    try:
        return cast(object, json.loads(data))
    except json.JSONDecodeError:
        return None


def _read_record(
    record_path: Path,
    findings: list[SessionIntegrityFinding],
) -> Mapping[str, object]:
    try:
        return read_session_record(record_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        findings.append(
            _finding(
                code="session_record_invalid",
                path=str(record_path),
                message=f"Session record is not readable JSON object: {exc}",
            )
        )
        return {}


def _resolve_session_paths(session_path: Path) -> tuple[Path, Path]:
    if session_path.name == SESSION_RECORD_FILENAME:
        return session_path.parent, session_path
    return session_path, session_path / SESSION_RECORD_FILENAME


def _resolve_recorded_path(*, session_dir: Path, path_value: str) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path
    return session_dir / path


def _optional_record_str(record: Mapping[str, object], key: str) -> str | None:
    value = record.get(key)
    if isinstance(value, str) and value:
        return value
    return None


def _optional_str(record: Mapping[str, object], key: str) -> str | None:
    value = record.get(key)
    if isinstance(value, str):
        return value
    return None


def _optional_int(record: Mapping[str, object], key: str) -> int | None:
    value = record.get(key)
    if type(value) is int:
        return value
    return None


def _normal_path(path: Path) -> Path:
    return path.resolve(strict=False)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        _normal_path(path).relative_to(_normal_path(parent))
    except ValueError:
        return False
    return True


def _display_path(path: Path, *, session_dir: Path) -> str:
    try:
        return _normal_path(path).relative_to(_normal_path(session_dir)).as_posix()
    except ValueError:
        return str(path)


def _finding(*, code: str, path: str, message: str) -> SessionIntegrityFinding:
    return SessionIntegrityFinding(
        severity="error",
        code=code,
        path=path,
        message=message,
    )
