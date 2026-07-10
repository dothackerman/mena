"""Shared exportable summary discovery and selection."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from mena.artifacts import (
    ARTIFACT_MANIFEST_VERSION,
    ARTIFACT_STATUSES,
    ArtifactRecord,
    ArtifactStore,
    ArtifactStoreError,
    session_artifact_manifest_path,
)
from mena.processing import ProcessingStateError
from mena.sessions import SessionRecordError, resolve_session, session_id
from mena.summarization import (
    SUMMARY_ARTIFACT_CONTRACT,
    SUMMARY_ARTIFACT_TYPE,
    SUMMARY_ARTIFACT_VERSION,
    SummarizationContractError,
    SummaryArtifact,
)
from mena.timestamps import parse_utc_timestamp

_INVALID_SUMMARY_CREATED_AT = datetime.min.replace(tzinfo=UTC)


class ExportableSummaryError(RuntimeError):
    """Raised when exportable summary selection cannot be completed."""


class IncompatibleExportableSummary(ExportableSummaryError):
    """Raised when a sealed summary payload is not compatible with export."""


@dataclass(frozen=True)
class ExportableSummarySelection:
    """A selected summary artifact and parsed note-package payload."""

    record: ArtifactRecord
    summary: SummaryArtifact


@dataclass(frozen=True)
class ExportableSummarySessionSelection:
    """A selected session plus the summary artifact that caused selection."""

    session_dir: Path
    summary_artifact_id: str | None


@dataclass(frozen=True)
class _SummaryManifestRecord:
    id: str | None
    path: str | None
    metadata: Mapping[str, object]
    record: ArtifactRecord | None = None


def resolve_exportable_summary(
    *,
    store: ArtifactStore,
    summary_record_id: str | None = None,
) -> ExportableSummarySelection:
    """Select the summary artifact that Markdown export should render."""

    records = tuple(store.list(artifact_type=SUMMARY_ARTIFACT_TYPE, status="sealed"))

    if summary_record_id is not None:
        for record in records:
            if record.id == summary_record_id:
                record, summary = load_exportable_summary(record=record, store=store)
                return ExportableSummarySelection(record=record, summary=summary)
        raise ExportableSummaryError(f"Summary artifact not found: {summary_record_id}.")

    candidates: list[tuple[datetime, str, str, ArtifactRecord, SummaryArtifact]] = []
    for record in records:
        if record.version != SUMMARY_ARTIFACT_VERSION:
            continue
        try:
            _, summary = load_exportable_summary(record=record, store=store)
        except IncompatibleExportableSummary:
            # Older summary artifacts, and readable payloads that no longer satisfy
            # the current strict note-package contract, remain sealed by design.
            # Auto-selection skips them so a valid current rerun can still win.
            continue
        candidates.append(
            (
                _summary_created_at(summary),
                record.path,
                record.id,
                record,
                summary,
            )
        )

    if not candidates:
        raise ExportableSummaryError(
            f"No valid {SUMMARY_ARTIFACT_CONTRACT} artifact was found for export."
        )

    _, _, _, selected_record, selected_summary = max(candidates)
    return ExportableSummarySelection(record=selected_record, summary=selected_summary)


def load_exportable_summary(
    *,
    record: ArtifactRecord,
    store: ArtifactStore,
) -> tuple[ArtifactRecord, SummaryArtifact]:
    """Load one sealed summary payload and validate it for export."""

    verification = store.verify(record.id)
    if not verification.ok:
        raise ExportableSummaryError(f"Summary artifact {record.id} failed integrity verification.")

    try:
        payload_bytes = store.retrieve(record.id)
    except KeyError as exc:
        raise ExportableSummaryError(
            f"Summary artifact payload is missing for {record.id}."
        ) from exc

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExportableSummaryError(f"Summary artifact {record.id} is not valid JSON.") from exc

    try:
        summary = SummaryArtifact.from_dict(payload)
    except (SummarizationContractError, ProcessingStateError) as exc:
        raise IncompatibleExportableSummary(
            f"Summary artifact {record.id} is not a valid {SUMMARY_ARTIFACT_CONTRACT} payload."
        ) from exc

    if summary.session_id != record.session_id:
        raise ExportableSummaryError(
            "Summary payload session id does not match artifact session id."
        )

    return record, summary


def resolve_exportable_summary_session(
    *,
    sessions_root: Path,
    summary_artifact_id: str | None = None,
) -> ExportableSummarySessionSelection | None:
    """Select the session that owns the exportable summary input."""

    if not sessions_root.is_dir():
        return None

    if summary_artifact_id is not None:
        for session_dir in _iter_session_directories(sessions_root):
            if not _session_can_own_artifact_id(
                session_dir=session_dir,
                artifact_id=summary_artifact_id,
            ):
                continue
            records = _session_summary_records(
                session_dir=session_dir,
                requested_summary_artifact_id=summary_artifact_id,
            )
            if records is None:
                if _manifest_contains_summary_artifact_id(
                    session_dir=session_dir,
                    artifact_id=summary_artifact_id,
                ):
                    return ExportableSummarySessionSelection(
                        session_dir=session_dir,
                        summary_artifact_id=summary_artifact_id,
                    )
                if _session_id_matches_artifact_id(
                    session_dir=session_dir,
                    artifact_id=summary_artifact_id,
                ):
                    return ExportableSummarySessionSelection(
                        session_dir=session_dir,
                        summary_artifact_id=summary_artifact_id,
                    )
                continue
            if all(record.id != summary_artifact_id for record in records):
                continue
            return ExportableSummarySessionSelection(
                session_dir=session_dir,
                summary_artifact_id=summary_artifact_id,
            )
        return None

    newest: tuple[datetime, str, str, str, Path, str | None] | None = None
    for session_dir in _iter_session_directories(sessions_root):
        summary_key = _session_summary_created_at(session_dir=session_dir)
        if summary_key is None:
            continue
        if newest is None or summary_key > newest:
            newest = summary_key

    if newest is None:
        return None
    return ExportableSummarySessionSelection(session_dir=newest[4], summary_artifact_id=newest[5])


def _session_summary_created_at(
    session_dir: Path,
) -> tuple[datetime, str, str, str, Path, str | None] | None:
    records = _session_summary_records(session_dir=session_dir)
    if not records:
        return None

    newest: tuple[datetime, str, str, str, Path, str | None] | None = None
    for record in records:
        summary_created_at = _summary_record_created_at(session_dir=session_dir, record=record)
        if summary_created_at is None:
            continue
        summary_key = (
            summary_created_at,
            record.path or "",
            record.id or "",
            session_dir.as_posix(),
            session_dir,
            record.id,
        )
        if newest is None or summary_key > newest:
            newest = summary_key
    return newest


def _session_summary_records(
    *,
    session_dir: Path,
    requested_summary_artifact_id: str | None = None,
) -> tuple[_SummaryManifestRecord, ...] | None:
    try:
        resolved = resolve_session(session_dir)
        resolved_session_id = session_id(dict(resolved.record))
        data = json.loads(
            session_artifact_manifest_path(resolved.session_dir).read_text(encoding="utf-8")
        )
        if not isinstance(data, dict):
            return None
        manifest_trusted = (
            data.get("version") == ARTIFACT_MANIFEST_VERSION
            and data.get("session_id") == resolved_session_id
        )
        artifacts = data.get("artifacts")
        if not isinstance(artifacts, list):
            return None
        records: list[_SummaryManifestRecord] = []
        for item in artifacts:
            if not isinstance(item, dict):
                continue
            raw_record = cast(Mapping[str, object], item)
            try:
                record = ArtifactRecord.from_dict(raw_record)
            except ArtifactStoreError:
                invalid_record = _invalid_summary_manifest_record(
                    item=raw_record,
                    resolved_session_id=resolved_session_id,
                    requested_summary_artifact_id=requested_summary_artifact_id,
                )
                if invalid_record is not None:
                    records.append(invalid_record)
                continue
            if not manifest_trusted or record.session_id != resolved_session_id:
                if (
                    record.artifact_type == SUMMARY_ARTIFACT_TYPE
                    and record.status == "sealed"
                    and (
                        requested_summary_artifact_id is None
                        or record.id == requested_summary_artifact_id
                    )
                ):
                    records.append(
                        _SummaryManifestRecord(
                            id=record.id,
                            path=record.path,
                            metadata=record.metadata,
                        )
                    )
                continue
            if record.artifact_type == SUMMARY_ARTIFACT_TYPE and record.status == "sealed":
                records.append(
                    _SummaryManifestRecord(
                        id=record.id,
                        path=record.path,
                        metadata=record.metadata,
                        record=record,
                    )
                )
        return tuple(sorted(records, key=lambda record: record.path or ""))
    except (
        ArtifactStoreError,
        FileNotFoundError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ):
        return None


def _session_id_matches_artifact_id(*, session_dir: Path, artifact_id: str) -> bool:
    try:
        resolved = resolve_session(session_dir)
        resolved_session_id = session_id(dict(resolved.record))
    except (
        ArtifactStoreError,
        FileNotFoundError,
        OSError,
        UnicodeDecodeError,
        ValueError,
        SessionRecordError,
    ):
        return False
    return artifact_id.startswith(f"{resolved_session_id}:")


def _session_can_own_artifact_id(*, session_dir: Path, artifact_id: str) -> bool:
    artifact_session_id = _artifact_id_session_id(artifact_id)
    if artifact_session_id is None:
        return True
    if _session_id_matches_artifact_id(session_dir=session_dir, artifact_id=artifact_id):
        return True
    return _manifest_session_id_matches(
        session_dir=session_dir,
        session_id_value=artifact_session_id,
    )


def _artifact_id_session_id(artifact_id: str) -> str | None:
    session_id_value, separator, _relative_path = artifact_id.partition(":")
    if not separator or not session_id_value:
        return None
    return session_id_value


def _manifest_session_id_matches(*, session_dir: Path, session_id_value: str) -> bool:
    try:
        data = json.loads(session_artifact_manifest_path(session_dir).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and data.get("session_id") == session_id_value


def _invalid_summary_manifest_record(
    *,
    item: Mapping[str, object],
    resolved_session_id: str,
    requested_summary_artifact_id: str | None,
) -> _SummaryManifestRecord | None:
    artifact_id = item.get("id")
    preserves_requested_id = (
        requested_summary_artifact_id is not None
        and isinstance(artifact_id, str)
        and artifact_id == requested_summary_artifact_id
    )
    if not preserves_requested_id:
        if item.get("session_id") != resolved_session_id:
            return None
        if item.get("artifact_type") != SUMMARY_ARTIFACT_TYPE:
            return None
    elif _raw_artifact_type_is_known_non_summary(item):
        return None
    raw_status = item.get("status")
    if isinstance(raw_status, str) and raw_status in ARTIFACT_STATUSES and raw_status != "sealed":
        return None
    metadata = item.get("metadata", {})
    path = item.get("path")
    return _SummaryManifestRecord(
        id=artifact_id if isinstance(artifact_id, str) else None,
        path=path if isinstance(path, str) else None,
        metadata=metadata if isinstance(metadata, Mapping) else {},
    )


def _summary_record_created_at(
    *,
    session_dir: Path,
    record: _SummaryManifestRecord,
) -> datetime | None:
    if record.record is not None and record.path is not None:
        if record.record.version != SUMMARY_ARTIFACT_VERSION:
            return _INVALID_SUMMARY_CREATED_AT
        try:
            data = json.loads((session_dir / record.path).read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return _INVALID_SUMMARY_CREATED_AT
            created_at = data.get("created_at")
            if not isinstance(created_at, str):
                return _INVALID_SUMMARY_CREATED_AT
            if not _summary_payload_is_export_compatible(data):
                return _INVALID_SUMMARY_CREATED_AT
            return _parse_iso_timestamp(created_at) or _INVALID_SUMMARY_CREATED_AT
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return _summary_metadata_created_at(record) or _INVALID_SUMMARY_CREATED_AT

    return _summary_metadata_created_at(record) or _INVALID_SUMMARY_CREATED_AT


def _summary_payload_is_export_compatible(payload: Mapping[str, object]) -> bool:
    try:
        SummaryArtifact.from_dict(payload)
    except (SummarizationContractError, ProcessingStateError):
        return False
    return True


def _manifest_contains_summary_artifact_id(*, session_dir: Path, artifact_id: str) -> bool:
    try:
        data = json.loads(session_artifact_manifest_path(session_dir).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    artifacts = data.get("artifacts")
    if not isinstance(artifacts, list):
        return False
    return any(
        isinstance(item, dict) and _raw_manifest_record_matches_summary_request(item, artifact_id)
        for item in artifacts
    )


def _raw_manifest_record_matches_summary_request(
    item: Mapping[str, object],
    artifact_id: str,
) -> bool:
    if item.get("id") != artifact_id:
        return False
    return not _raw_artifact_type_is_known_non_summary(item)


def _raw_artifact_type_is_known_non_summary(item: Mapping[str, object]) -> bool:
    value = item.get("artifact_type")
    return (
        isinstance(value, str)
        and value != SUMMARY_ARTIFACT_TYPE
        and not _raw_record_path_looks_like_summary(item)
    )


def _raw_record_path_looks_like_summary(item: Mapping[str, object]) -> bool:
    path = item.get("path")
    if isinstance(path, str) and path.startswith(f"artifacts/{SUMMARY_ARTIFACT_TYPE}/"):
        return True
    artifact_id = item.get("id")
    return isinstance(artifact_id, str) and f":artifacts/{SUMMARY_ARTIFACT_TYPE}/" in artifact_id


def _summary_metadata_created_at(record: _SummaryManifestRecord) -> datetime | None:
    metadata_created_at = record.metadata.get("summary_created_at")
    if not isinstance(metadata_created_at, str):
        return None
    return _parse_iso_timestamp(metadata_created_at)


def _iter_session_directories(sessions_root: Path) -> tuple[Path, ...]:
    try:
        return tuple(
            sorted(session_dir for session_dir in sessions_root.iterdir() if session_dir.is_dir())
        )
    except OSError:
        return ()


def _summary_created_at(summary: SummaryArtifact) -> datetime:
    return _parse_iso_timestamp(summary.created_at) or _INVALID_SUMMARY_CREATED_AT


def _parse_iso_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return parse_utc_timestamp(value)
    except ValueError:
        return None
