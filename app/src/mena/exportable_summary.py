"""Shared exportable summary discovery and selection."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

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
SummarySelectionReason = Literal["exportable", "verify_selected_session"]


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
    reason: SummarySelectionReason = "exportable"


@dataclass(frozen=True)
class _SummaryManifestRecord:
    id: str | None
    path: str | None
    metadata: Mapping[str, object]
    record: ArtifactRecord | None = None


@dataclass(frozen=True, order=True)
class _SummaryCandidate:
    created_at: datetime
    path: str
    artifact_id: str
    record: ArtifactRecord = field(compare=False)
    summary: SummaryArtifact = field(compare=False)


@dataclass(frozen=True, order=True)
class _SessionSummaryCandidate:
    created_at: datetime
    path: str
    artifact_id: str
    session_path: str
    session_dir: Path = field(compare=False)
    summary_artifact_id: str | None = field(compare=False)
    reason: SummarySelectionReason = field(compare=False)


@dataclass(frozen=True)
class _SummaryRecordRank:
    created_at: datetime
    reason: SummarySelectionReason


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

    candidates: list[_SummaryCandidate] = []
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
            _SummaryCandidate(
                created_at=_summary_created_at(summary),
                path=record.path,
                artifact_id=record.id,
                record=record,
                summary=summary,
            )
        )

    if not candidates:
        raise ExportableSummaryError(
            f"No valid {SUMMARY_ARTIFACT_CONTRACT} artifact was found for export."
        )

    selected = max(candidates)
    return ExportableSummarySelection(record=selected.record, summary=selected.summary)


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
                        reason="verify_selected_session",
                    )
                if _requested_summary_artifact_file_exists(
                    session_dir=session_dir,
                    artifact_id=summary_artifact_id,
                ):
                    return ExportableSummarySessionSelection(
                        session_dir=session_dir,
                        summary_artifact_id=summary_artifact_id,
                        reason="verify_selected_session",
                    )
                continue
            matching_record = next(
                (record for record in records if record.id == summary_artifact_id),
                None,
            )
            if matching_record is None:
                continue
            return ExportableSummarySessionSelection(
                session_dir=session_dir,
                summary_artifact_id=summary_artifact_id,
                reason=_summary_record_rank(
                    session_dir=session_dir,
                    record=matching_record,
                ).reason,
            )
        return None

    newest: _SessionSummaryCandidate | None = None
    for session_dir in _iter_session_directories(sessions_root):
        candidate = _session_summary_candidate(session_dir=session_dir)
        if candidate is None:
            continue
        if newest is None or candidate > newest:
            newest = candidate

    if newest is None:
        return None
    return ExportableSummarySessionSelection(
        session_dir=newest.session_dir,
        summary_artifact_id=newest.summary_artifact_id,
        reason=newest.reason,
    )


def _session_summary_candidate(
    session_dir: Path,
) -> _SessionSummaryCandidate | None:
    records = _session_summary_records(session_dir=session_dir)
    if not records:
        return None

    newest: _SessionSummaryCandidate | None = None
    for record in records:
        rank = _summary_record_rank(session_dir=session_dir, record=record)
        candidate = _SessionSummaryCandidate(
            created_at=rank.created_at,
            path=record.path or "",
            artifact_id=record.id or "",
            session_path=session_dir.as_posix(),
            session_dir=session_dir,
            summary_artifact_id=record.id,
            reason=rank.reason,
        )
        if newest is None or candidate > newest:
            newest = candidate
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


def _summary_record_rank(
    *,
    session_dir: Path,
    record: _SummaryManifestRecord,
) -> _SummaryRecordRank:
    if record.record is not None and record.path is not None:
        if record.record.version != SUMMARY_ARTIFACT_VERSION:
            return _SummaryRecordRank(
                created_at=_INVALID_SUMMARY_CREATED_AT,
                reason="verify_selected_session",
            )
        try:
            data = json.loads((session_dir / record.path).read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return _SummaryRecordRank(
                    created_at=_INVALID_SUMMARY_CREATED_AT,
                    reason="verify_selected_session",
                )
            created_at = data.get("created_at")
            if not isinstance(created_at, str):
                return _SummaryRecordRank(
                    created_at=_INVALID_SUMMARY_CREATED_AT,
                    reason="verify_selected_session",
                )
            if not _summary_payload_is_export_compatible(data):
                return _SummaryRecordRank(
                    created_at=_INVALID_SUMMARY_CREATED_AT,
                    reason="verify_selected_session",
                )
            parsed_created_at = _parse_iso_timestamp(created_at)
            return _SummaryRecordRank(
                created_at=parsed_created_at or _INVALID_SUMMARY_CREATED_AT,
                reason="exportable" if parsed_created_at is not None else "verify_selected_session",
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return _SummaryRecordRank(
                created_at=_summary_metadata_created_at(record) or _INVALID_SUMMARY_CREATED_AT,
                reason="verify_selected_session",
            )

    return _SummaryRecordRank(
        created_at=_summary_metadata_created_at(record) or _INVALID_SUMMARY_CREATED_AT,
        reason="verify_selected_session",
    )


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


def _requested_summary_artifact_file_exists(*, session_dir: Path, artifact_id: str) -> bool:
    relative_path = _requested_summary_artifact_path(artifact_id)
    if relative_path is None:
        return False
    if not _session_id_matches_artifact_id(session_dir=session_dir, artifact_id=artifact_id):
        return False
    return (session_dir / relative_path).is_file()


def _requested_summary_artifact_path(artifact_id: str) -> Path | None:
    _session_id_value, separator, raw_path = artifact_id.partition(":")
    if not separator or not raw_path:
        return None
    relative_path = Path(raw_path)
    if relative_path.is_absolute() or any(part in {"", ".", ".."} for part in relative_path.parts):
        return None
    if len(relative_path.parts) < 3:
        return None
    if relative_path.parts[0] != "artifacts" or relative_path.parts[1] != SUMMARY_ARTIFACT_TYPE:
        return None
    return relative_path


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
