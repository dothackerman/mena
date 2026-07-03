"""Export command workflow orchestration."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from mena.artifacts import (
    ARTIFACT_MANIFEST_VERSION,
    ARTIFACT_STATUSES,
    ArtifactRecord,
    ArtifactStoreError,
    FileSystemArtifactStore,
    session_artifact_manifest_path,
)
from mena.export import export_summary_markdown
from mena.interaction import ConfirmationRequest, InteractionPort, WorkflowNotice
from mena.session_integrity import load_verified_session_bundle
from mena.sessions import SessionRecordError, resolve_session, session_id
from mena.settings import AppSettings
from mena.summarization import SUMMARY_ARTIFACT_TYPE, SUMMARY_SCHEMA_VERSION
from mena.timestamps import parse_utc_timestamp

_INVALID_SUMMARY_CREATED_AT = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True)
class ExportCommandInput:
    session_path: Path | None
    summary_artifact_id: str | None = None
    export_format: str | None = None


@dataclass(frozen=True)
class ExportWorkflowResult:
    status: Literal["success", "cancelled"]
    export_path: Path | None = None


@dataclass(frozen=True)
class _SummaryManifestRecord:
    id: str | None
    path: str | None
    metadata: Mapping[str, object]
    record: ArtifactRecord | None = None


def run_export_workflow(
    command: ExportCommandInput,
    *,
    settings: AppSettings,
    interaction: InteractionPort | None,
) -> ExportWorkflowResult:
    resolved_session_path = resolve_export_session_path(
        session_path=command.session_path,
        summary_artifact_id=command.summary_artifact_id,
        settings=settings,
    )
    verified = load_verified_session_bundle(resolved_session_path)
    if command.session_path is None and interaction is not None:
        if command.summary_artifact_id is not None:
            interaction.notify(
                WorkflowNotice(
                    topic="export.session_selected.summary_artifact",
                    context={"session_path": resolved_session_path},
                )
            )
        else:
            interaction.notify(
                WorkflowNotice(
                    topic="export.session_selected.latest",
                    context={"session_path": resolved_session_path},
                )
            )

    selected_format = resolve_export_format(
        export_format=command.export_format,
        interaction=interaction,
    )
    if selected_format is None:
        return ExportWorkflowResult(status="cancelled")

    session_record = dict(verified.record)
    session_id_value = session_id(session_record)

    export_result = export_summary_markdown(
        store=FileSystemArtifactStore(
            session_id=session_id_value,
            session_dir=verified.session_dir,
        ),
        summary_record_id=command.summary_artifact_id,
    )

    export_path = verified.session_dir / export_result.export_record.path
    if interaction is not None:
        interaction.notify(
            WorkflowNotice(
                topic="export.completed",
                context={"export_path": export_path},
            )
        )
    return ExportWorkflowResult(status="success", export_path=export_path)


def resolve_export_format(
    *,
    export_format: str | None,
    interaction: InteractionPort | None,
) -> str | None:
    if export_format is not None:
        return export_format
    if interaction is None:
        return "markdown"
    confirmed = interaction.confirm(
        ConfirmationRequest(topic="export.format.markdown", default=True)
    )
    return "markdown" if confirmed else None


def resolve_export_session_path(
    *,
    session_path: Path | None,
    summary_artifact_id: str | None,
    settings: AppSettings,
) -> Path:
    if session_path is not None:
        return session_path

    resolved = resolve_default_export_session(
        sessions_root=settings.paths.sessions_dir,
        summary_artifact_id=summary_artifact_id,
    )
    if resolved is None:
        if summary_artifact_id is not None:
            raise ValueError(f"Summary artifact not found: {summary_artifact_id}.")
        raise ValueError("No summarized sessions were found. Run `mena summarize` first.")

    return resolved


def resolve_default_export_session(
    *,
    sessions_root: Path,
    summary_artifact_id: str | None,
) -> Path | None:
    if not sessions_root.is_dir():
        return None

    if summary_artifact_id is not None:
        for session_dir in _iter_session_directories(sessions_root):
            records = _session_summary_records(
                session_dir=session_dir,
                requested_summary_artifact_id=summary_artifact_id,
            )
            if records is None:
                if _manifest_contains_summary_artifact_id(
                    session_dir=session_dir,
                    artifact_id=summary_artifact_id,
                ):
                    return session_dir
                if _session_id_matches_artifact_id(
                    session_dir=session_dir,
                    artifact_id=summary_artifact_id,
                ):
                    return session_dir
                continue
            if all(record.id != summary_artifact_id for record in records):
                continue
            return session_dir
        return None

    newest: tuple[datetime, Path] | None = None
    for session_dir in _iter_session_directories(sessions_root):
        summary_key = _session_summary_created_at(session_dir=session_dir)
        if summary_key is None:
            continue
        if newest is None or summary_key > newest:
            newest = summary_key

    return newest[1] if newest is not None else None


def _session_summary_created_at(session_dir: Path) -> tuple[datetime, Path] | None:
    records = _session_summary_records(session_dir=session_dir)
    if not records:
        return None

    newest: tuple[datetime, str, str] | None = None
    for record in records:
        summary_created_at = _summary_record_created_at(session_dir=session_dir, record=record)
        if summary_created_at is None:
            continue
        summary_key = (summary_created_at, record.path or "", record.id or "")
        if newest is None or summary_key > newest:
            newest = summary_key
    return (newest[0], session_dir) if newest is not None else None


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
        if data.get("version") != ARTIFACT_MANIFEST_VERSION:
            return None
        if data.get("session_id") != resolved_session_id:
            return None
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
                )
                if invalid_record is not None:
                    records.append(invalid_record)
                continue
            if record.session_id != resolved_session_id:
                if (
                    record.id == requested_summary_artifact_id
                    and record.artifact_type == SUMMARY_ARTIFACT_TYPE
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


def _invalid_summary_manifest_record(
    *,
    item: Mapping[str, object],
    resolved_session_id: str,
) -> _SummaryManifestRecord | None:
    if item.get("session_id") != resolved_session_id:
        return None
    if item.get("artifact_type") != SUMMARY_ARTIFACT_TYPE:
        return None
    raw_status = item.get("status")
    if isinstance(raw_status, str) and raw_status in ARTIFACT_STATUSES and raw_status != "sealed":
        return None
    metadata = item.get("metadata", {})
    artifact_id = item.get("id")
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
        try:
            data = json.loads((session_dir / record.path).read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return _summary_metadata_created_at(record) or _INVALID_SUMMARY_CREATED_AT
            if data.get("version") != SUMMARY_SCHEMA_VERSION:
                return _summary_metadata_created_at(record) or _INVALID_SUMMARY_CREATED_AT
            created_at = data.get("created_at")
            if not isinstance(created_at, str):
                return _INVALID_SUMMARY_CREATED_AT
            return _parse_iso_timestamp(created_at) or _INVALID_SUMMARY_CREATED_AT
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return _summary_metadata_created_at(record) or _INVALID_SUMMARY_CREATED_AT

    return _summary_metadata_created_at(record) or _INVALID_SUMMARY_CREATED_AT


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
        isinstance(item, dict)
        and item.get("id") == artifact_id
        and item.get("artifact_type") == SUMMARY_ARTIFACT_TYPE
        for item in artifacts
    )


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


def _parse_iso_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return parse_utc_timestamp(value)
    except ValueError:
        return None
