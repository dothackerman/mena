"""Artifact store port and file-system adapter."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Self
from uuid import uuid4

from mena.fields import required_int, required_str

ARTIFACT_MANIFEST_FILENAME = "artifact-manifest.v1.json"
ARTIFACT_MANIFEST_VERSION = 1
ARTIFACT_RECORD_VERSION = 1
ARTIFACT_STATUSES = frozenset({"sealed", "staged", "failed"})
_ARTIFACT_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_:-]*$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ArtifactStoreError(RuntimeError):
    """Raised when the artifact store contract cannot be fulfilled."""


@dataclass(frozen=True)
class ArtifactRecord:
    """Versioned metadata for one session-scoped artifact."""

    id: str
    session_id: str
    artifact_type: str
    version: int
    path: str
    byte_count: int
    sha256: str
    status: str
    metadata: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "artifact_type": self.artifact_type,
            "version": self.version,
            "path": self.path,
            "byte_count": self.byte_count,
            "sha256": self.sha256,
            "status": self.status,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Self:
        artifact_id = _required_str(data, "id")
        session_id = _required_str(data, "session_id")
        artifact_type = _required_str(data, "artifact_type")
        version = _required_int(data, "version")
        path = _required_str(data, "path")
        byte_count = _required_int(data, "byte_count")
        sha256 = _required_str(data, "sha256")
        status = _required_str(data, "status")
        metadata = data.get("metadata", {})
        metadata = _metadata_dict(metadata)
        _validate_artifact_type(artifact_type)
        if version < 1:
            raise ArtifactStoreError("Artifact version must be positive.")
        if byte_count < 0:
            raise ArtifactStoreError("Artifact byte_count must not be negative.")
        if not _SHA256_PATTERN.fullmatch(sha256):
            raise ArtifactStoreError("Artifact sha256 must be a lowercase hex digest.")
        _validate_status(status)
        relative_path = _validate_manifest_path(path)
        expected_id = _artifact_id(session_id, relative_path)
        if artifact_id != expected_id:
            raise ArtifactStoreError("Artifact id must match session_id and path.")
        return cls(
            id=artifact_id,
            session_id=session_id,
            artifact_type=artifact_type,
            version=version,
            path=path,
            byte_count=byte_count,
            sha256=sha256,
            status=status,
            metadata=metadata,
        )


@dataclass(frozen=True)
class ArtifactVerification:
    """Machine-readable integrity verification result."""

    artifact_id: str
    path: str
    ok: bool
    expected_sha256: str
    actual_sha256: str | None
    expected_byte_count: int
    actual_byte_count: int | None
    status: str
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "path": self.path,
            "ok": self.ok,
            "expected_sha256": self.expected_sha256,
            "actual_sha256": self.actual_sha256,
            "expected_byte_count": self.expected_byte_count,
            "actual_byte_count": self.actual_byte_count,
            "status": self.status,
            "error": self.error,
        }


class ArtifactStore(Protocol):
    """Port for durable, session-scoped artifact persistence."""

    def store(
        self,
        *,
        artifact_type: str,
        name: str | Path,
        data: bytes,
        version: int = ARTIFACT_RECORD_VERSION,
        status: str = "sealed",
        metadata: Mapping[str, object] | None = None,
    ) -> ArtifactRecord:
        """Store one complete artifact and return its metadata record."""

    def store_file(
        self,
        *,
        artifact_type: str,
        name: str | Path,
        source_path: Path,
        version: int = ARTIFACT_RECORD_VERSION,
        status: str = "sealed",
        metadata: Mapping[str, object] | None = None,
    ) -> ArtifactRecord:
        """Store one complete artifact from an existing file path."""

    def retrieve(self, artifact_id: str) -> bytes:
        """Return artifact bytes by artifact id."""

    def list(
        self,
        *,
        artifact_type: str | None = None,
        status: str | None = None,
    ) -> tuple[ArtifactRecord, ...]:
        """List known artifact records in deterministic order."""

    def verify(self, artifact_id: str) -> ArtifactVerification:
        """Verify artifact bytes against tracked metadata."""


class FileSystemArtifactStore:
    """File-system artifact adapter rooted at one session bundle."""

    def __init__(self, *, session_id: str, session_dir: Path) -> None:
        self.session_id = session_id
        self.session_dir = session_dir
        self.artifact_root = session_artifact_root(session_dir)
        self.manifest_path = session_artifact_manifest_path(session_dir)
        self.artifact_root.mkdir(parents=True, exist_ok=True)

    def store(
        self,
        *,
        artifact_type: str,
        name: str | Path,
        data: bytes,
        version: int = ARTIFACT_RECORD_VERSION,
        status: str = "sealed",
        metadata: Mapping[str, object] | None = None,
    ) -> ArtifactRecord:
        relative_path = _artifact_relative_path(artifact_type=artifact_type, name=name)
        artifact_id = _artifact_id(self.session_id, relative_path)
        records = self._load_records()
        if artifact_id in records:
            raise FileExistsError(f"Artifact already exists: {artifact_id}")

        record = _record_for_bytes(
            session_id=self.session_id,
            artifact_type=artifact_type,
            relative_path=relative_path,
            data=data,
            version=version,
            status=status,
            metadata=metadata,
        )
        next_records = {**records, record.id: record}
        self._manifest_bytes(next_records)

        path = self.session_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_bytes_exclusive(path, data)
        try:
            self._write_records(next_records)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return record

    def store_file(
        self,
        *,
        artifact_type: str,
        name: str | Path,
        source_path: Path,
        version: int = ARTIFACT_RECORD_VERSION,
        status: str = "sealed",
        metadata: Mapping[str, object] | None = None,
    ) -> ArtifactRecord:
        relative_path = _artifact_relative_path(artifact_type=artifact_type, name=name)
        artifact_id = _artifact_id(self.session_id, relative_path)
        records = self._load_records()
        if artifact_id in records:
            raise FileExistsError(f"Artifact already exists: {artifact_id}")
        if not source_path.is_file():
            raise ArtifactStoreError("Artifact source path must be a file.")

        record = _record_for_file(
            session_id=self.session_id,
            artifact_type=artifact_type,
            relative_path=relative_path,
            source_path=source_path,
            version=version,
            status=status,
            metadata=metadata,
        )
        next_records = {**records, record.id: record}
        self._manifest_bytes(next_records)

        path = self.session_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        _copy_file_exclusive(source_path, path)
        try:
            self._write_records(next_records)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return record

    def retrieve(self, artifact_id: str) -> bytes:
        record = self._record_by_id(artifact_id)
        return (self.session_dir / record.path).read_bytes()

    def list(
        self,
        *,
        artifact_type: str | None = None,
        status: str | None = None,
    ) -> tuple[ArtifactRecord, ...]:
        records = self._load_records().values()
        filtered = (
            record
            for record in records
            if (artifact_type is None or record.artifact_type == artifact_type)
            and (status is None or record.status == status)
        )
        return tuple(sorted(filtered, key=lambda record: record.path))

    def verify(self, artifact_id: str) -> ArtifactVerification:
        record = self._record_by_id(artifact_id)
        path = self.session_dir / record.path
        if not path.exists():
            return _verification(
                record,
                ok=False,
                actual_sha256=None,
                actual_byte_count=None,
                error="artifact file is missing",
            )
        if not path.is_file():
            return _verification(
                record,
                ok=False,
                actual_sha256=None,
                actual_byte_count=None,
                error="artifact path is not a file",
            )
        actual_sha256, actual_byte_count = _sha256_file(path)
        ok = actual_sha256 == record.sha256 and actual_byte_count == record.byte_count
        return _verification(
            record,
            ok=ok,
            actual_sha256=actual_sha256,
            actual_byte_count=actual_byte_count,
            error=None if ok else "artifact content does not match metadata",
        )

    def _record_by_id(self, artifact_id: str) -> ArtifactRecord:
        records = self._load_records()
        try:
            return records[artifact_id]
        except KeyError as exc:
            raise KeyError(f"Unknown artifact id: {artifact_id}") from exc

    def _load_records(self) -> dict[str, ArtifactRecord]:
        if not self.manifest_path.exists():
            return {}
        data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ArtifactStoreError("Artifact manifest must be a JSON object.")
        if data.get("version") != ARTIFACT_MANIFEST_VERSION:
            raise ArtifactStoreError("Unsupported artifact manifest version.")
        if data.get("session_id") != self.session_id:
            raise ArtifactStoreError("Artifact manifest session_id does not match store.")
        artifacts = data.get("artifacts")
        if not isinstance(artifacts, list):
            raise ArtifactStoreError("Artifact manifest artifacts must be an array.")
        records: dict[str, ArtifactRecord] = {}
        for item in artifacts:
            record = ArtifactRecord.from_dict(_mapping(item))
            if record.session_id != self.session_id:
                raise ArtifactStoreError("Artifact record session_id does not match store.")
            if record.id in records:
                raise ArtifactStoreError(f"Duplicate artifact id in manifest: {record.id}")
            records[record.id] = record
        return records

    def _write_records(self, records: Mapping[str, ArtifactRecord]) -> None:
        encoded = self._manifest_bytes(records)
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        temp_path = self.manifest_path.with_name(f".{self.manifest_path.name}.tmp")
        temp_path.write_bytes(encoded)
        temp_path.replace(self.manifest_path)

    def _manifest_bytes(self, records: Mapping[str, ArtifactRecord]) -> bytes:
        manifest = {
            "version": ARTIFACT_MANIFEST_VERSION,
            "session_id": self.session_id,
            "artifacts": [
                record.to_dict() for record in sorted(records.values(), key=lambda item: item.path)
            ],
        }
        return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")


class InMemoryArtifactStore:
    """Deterministic fake adapter for artifact store contract tests."""

    def __init__(self, *, session_id: str = "00000000-0000-0000-0000-000000000001") -> None:
        self.session_id = session_id
        self._records: dict[str, ArtifactRecord] = {}
        self._data: dict[str, bytes] = {}

    def store(
        self,
        *,
        artifact_type: str,
        name: str | Path,
        data: bytes,
        version: int = ARTIFACT_RECORD_VERSION,
        status: str = "sealed",
        metadata: Mapping[str, object] | None = None,
    ) -> ArtifactRecord:
        relative_path = _artifact_relative_path(artifact_type=artifact_type, name=name)
        artifact_id = _artifact_id(self.session_id, relative_path)
        if artifact_id in self._records:
            raise FileExistsError(f"Artifact already exists: {artifact_id}")
        record = _record_for_bytes(
            session_id=self.session_id,
            artifact_type=artifact_type,
            relative_path=relative_path,
            data=data,
            version=version,
            status=status,
            metadata=metadata,
        )
        self._records[record.id] = record
        self._data[record.id] = data
        return record

    def store_file(
        self,
        *,
        artifact_type: str,
        name: str | Path,
        source_path: Path,
        version: int = ARTIFACT_RECORD_VERSION,
        status: str = "sealed",
        metadata: Mapping[str, object] | None = None,
    ) -> ArtifactRecord:
        return self.store(
            artifact_type=artifact_type,
            name=name,
            data=source_path.read_bytes(),
            version=version,
            status=status,
            metadata=metadata,
        )

    def retrieve(self, artifact_id: str) -> bytes:
        return self._data[artifact_id]

    def list(
        self,
        *,
        artifact_type: str | None = None,
        status: str | None = None,
    ) -> tuple[ArtifactRecord, ...]:
        records = (
            record
            for record in self._records.values()
            if (artifact_type is None or record.artifact_type == artifact_type)
            and (status is None or record.status == status)
        )
        return tuple(sorted(records, key=lambda record: record.path))

    def verify(self, artifact_id: str) -> ArtifactVerification:
        record = self._records[artifact_id]
        data = self._data.get(artifact_id)
        if data is None:
            return _verification(
                record,
                ok=False,
                actual_sha256=None,
                actual_byte_count=None,
                error="artifact bytes are missing",
            )
        actual_sha256 = _sha256(data)
        actual_byte_count = len(data)
        ok = actual_sha256 == record.sha256 and actual_byte_count == record.byte_count
        return _verification(
            record,
            ok=ok,
            actual_sha256=actual_sha256,
            actual_byte_count=actual_byte_count,
            error=None if ok else "artifact content does not match metadata",
        )


def _record_for_bytes(
    *,
    session_id: str,
    artifact_type: str,
    relative_path: Path,
    data: bytes,
    version: int,
    status: str,
    metadata: Mapping[str, object] | None,
) -> ArtifactRecord:
    _validate_artifact_type(artifact_type)
    _validate_status(status)
    if type(version) is not int or version < 1:
        raise ArtifactStoreError("Artifact version must be positive.")
    metadata_dict = _metadata_dict({} if metadata is None else metadata)
    path = relative_path.as_posix()
    return ArtifactRecord(
        id=_artifact_id(session_id, relative_path),
        session_id=session_id,
        artifact_type=artifact_type,
        version=version,
        path=path,
        byte_count=len(data),
        sha256=_sha256(data),
        status=status,
        metadata=metadata_dict,
    )


def _record_for_file(
    *,
    session_id: str,
    artifact_type: str,
    relative_path: Path,
    source_path: Path,
    version: int,
    status: str,
    metadata: Mapping[str, object] | None,
) -> ArtifactRecord:
    _validate_artifact_type(artifact_type)
    _validate_status(status)
    if type(version) is not int or version < 1:
        raise ArtifactStoreError("Artifact version must be positive.")
    metadata_dict = _metadata_dict({} if metadata is None else metadata)
    sha256, byte_count = _sha256_file(source_path)
    path = relative_path.as_posix()
    return ArtifactRecord(
        id=_artifact_id(session_id, relative_path),
        session_id=session_id,
        artifact_type=artifact_type,
        version=version,
        path=path,
        byte_count=byte_count,
        sha256=sha256,
        status=status,
        metadata=metadata_dict,
    )


def _artifact_relative_path(*, artifact_type: str, name: str | Path) -> Path:
    _validate_artifact_type(artifact_type)
    artifact_name = Path(name)
    if artifact_name.is_absolute():
        raise ArtifactStoreError("Artifact name must be session-relative.")
    if not artifact_name.parts or any(part in {"", ".", ".."} for part in artifact_name.parts):
        raise ArtifactStoreError("Artifact name must not contain empty or parent path parts.")
    return Path("artifacts") / artifact_type / artifact_name


def session_artifact_root(session_dir: Path) -> Path:
    return session_dir / "artifacts"


def session_artifact_manifest_path(session_dir: Path) -> Path:
    return session_artifact_root(session_dir) / ARTIFACT_MANIFEST_FILENAME


def _validate_manifest_path(path: str) -> Path:
    relative_path = Path(path)
    if relative_path.is_absolute():
        raise ArtifactStoreError("Artifact path must be session-relative.")
    if (
        not relative_path.parts
        or relative_path.parts[0] != "artifacts"
        or any(part in {"", ".", ".."} for part in relative_path.parts)
    ):
        raise ArtifactStoreError("Artifact path must stay under the session artifacts directory.")
    return relative_path


def _artifact_id(session_id: str, relative_path: Path) -> str:
    return f"{session_id}:{relative_path.as_posix()}"


def _validate_artifact_type(artifact_type: str) -> None:
    if not _ARTIFACT_TYPE_PATTERN.fullmatch(artifact_type):
        raise ArtifactStoreError("Artifact type must be a non-empty stable identifier.")


def _validate_status(status: str) -> None:
    if status not in ARTIFACT_STATUSES:
        valid = ", ".join(sorted(ARTIFACT_STATUSES))
        raise ArtifactStoreError(f"Artifact status must be one of: {valid}.")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            byte_count += len(chunk)
    return digest.hexdigest(), byte_count


def _write_bytes_exclusive(path: Path, data: bytes) -> None:
    temp_path = path.with_name(f".{path.name}.{uuid4()}.tmp")
    try:
        temp_path.write_bytes(data)
        try:
            os.link(temp_path, path)
        except FileExistsError as exc:
            raise FileExistsError(f"Artifact path already exists: {path}") from exc
    finally:
        temp_path.unlink(missing_ok=True)


def _copy_file_exclusive(source_path: Path, path: Path) -> None:
    temp_path = path.with_name(f".{path.name}.{uuid4()}.tmp")
    try:
        with source_path.open("rb") as source, temp_path.open("xb") as destination:
            shutil.copyfileobj(source, destination, length=1024 * 1024)
        try:
            os.link(temp_path, path)
        except FileExistsError as exc:
            raise FileExistsError(f"Artifact path already exists: {path}") from exc
    finally:
        temp_path.unlink(missing_ok=True)


def _metadata_dict(metadata: object) -> dict[str, object]:
    if not isinstance(metadata, dict):
        raise ArtifactStoreError("Artifact metadata must be a JSON object.")
    if any(not isinstance(key, str) for key in metadata):
        raise ArtifactStoreError("Artifact metadata keys must be strings.")
    try:
        json.dumps(metadata)
    except TypeError as exc:
        raise ArtifactStoreError("Artifact metadata must be JSON serializable.") from exc
    return metadata


def _verification(
    record: ArtifactRecord,
    *,
    ok: bool,
    actual_sha256: str | None,
    actual_byte_count: int | None,
    error: str | None,
) -> ArtifactVerification:
    return ArtifactVerification(
        artifact_id=record.id,
        path=record.path,
        ok=ok,
        expected_sha256=record.sha256,
        actual_sha256=actual_sha256,
        expected_byte_count=record.byte_count,
        actual_byte_count=actual_byte_count,
        status=record.status,
        error=error,
    )


def _required_str(data: Mapping[str, object], key: str) -> str:
    return required_str(
        data,
        key,
        error=ArtifactStoreError,
        message=f"Artifact field '{key}' must be a non-empty string.",
    )


def _required_int(data: Mapping[str, object], key: str) -> int:
    return required_int(
        data,
        key,
        error=ArtifactStoreError,
        message=f"Artifact field '{key}' must be an integer.",
    )


def _mapping(data: object) -> Mapping[str, object]:
    if not isinstance(data, dict):
        raise ArtifactStoreError("Artifact record must be a JSON object.")
    return data
