"""Summary markdown export service."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from mena.artifacts import ArtifactRecord, ArtifactStore
from mena.summarization import (
    SUMMARY_ARTIFACT_TYPE,
    SummarizationContractError,
    SummaryArtifact,
)
from mena.summary_markdown import format_summary_markdown

SUMMARY_MARKDOWN_EXPORT_ARTIFACT_TYPE = "summary_markdown_export"
SUMMARY_MARKDOWN_EXPORT_ARTIFACT_VERSION = 1
SUMMARY_MARKDOWN_EXPORT_CONTRACT = "summary-markdown.v1"


class SummaryMarkdownExportError(RuntimeError):
    """Raised when markdown export cannot be produced."""


@dataclass(frozen=True)
class SummaryMarkdownExportResult:
    """Result of a deterministic markdown export run."""

    summary_record: ArtifactRecord
    summary: SummaryArtifact
    export_record: ArtifactRecord


def export_summary_markdown(
    *,
    store: ArtifactStore,
    summary_record_id: str | None = None,
) -> SummaryMarkdownExportResult:
    """Export one summary artifact to markdown and persist it as an export artifact."""

    summary_record, summary = resolve_summary_markdown_input(
        store=store,
        summary_record_id=summary_record_id,
    )
    markdown = format_summary_markdown(summary)

    export_record = store.store(
        artifact_type=SUMMARY_MARKDOWN_EXPORT_ARTIFACT_TYPE,
        name=_export_artifact_name(summary_record=summary_record, summary=summary),
        data=markdown.encode("utf-8"),
        version=SUMMARY_MARKDOWN_EXPORT_ARTIFACT_VERSION,
        status="sealed",
        metadata=_export_metadata(summary_record=summary_record, summary=summary),
    )

    return SummaryMarkdownExportResult(
        summary_record=summary_record,
        summary=summary,
        export_record=export_record,
    )


def resolve_summary_markdown_input(
    *,
    store: ArtifactStore,
    summary_record_id: str | None = None,
) -> tuple[ArtifactRecord, SummaryArtifact]:
    """Return the latest valid summary artifact for export."""

    records = tuple(store.list(artifact_type=SUMMARY_ARTIFACT_TYPE, status="sealed"))

    if summary_record_id is not None:
        for record in records:
            if record.id == summary_record_id:
                return _load_summary_artifact(record=record, store=store)
        raise SummaryMarkdownExportError(f"Summary artifact not found: {summary_record_id}.")

    candidates: list[tuple[datetime, str, str, ArtifactRecord, SummaryArtifact]] = []
    for record in records:
        try:
            _, summary = _load_summary_artifact(record=record, store=store)
        except SummaryMarkdownExportError:
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
        raise SummaryMarkdownExportError("No valid summary.v1 artifact was found for export.")

    _, _, _, selected_record, selected_summary = max(candidates)
    return selected_record, selected_summary


def _load_summary_artifact(
    *,
    record: ArtifactRecord,
    store: ArtifactStore,
) -> tuple[ArtifactRecord, SummaryArtifact]:
    verification = store.verify(record.id)
    if not verification.ok:
        raise SummaryMarkdownExportError(
            f"Summary artifact {record.id} failed integrity verification."
        )

    try:
        payload_bytes = store.retrieve(record.id)
    except KeyError as exc:
        raise SummaryMarkdownExportError(
            f"Summary artifact payload is missing for {record.id}."
        ) from exc

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SummaryMarkdownExportError(
            f"Summary artifact {record.id} is not valid JSON."
        ) from exc

    try:
        summary = SummaryArtifact.from_dict(payload)
    except SummarizationContractError as exc:
        raise SummaryMarkdownExportError(
            f"Summary artifact {record.id} is not a valid summary.v1 payload."
        ) from exc

    if summary.session_id != record.session_id:
        raise SummaryMarkdownExportError(
            "Summary payload session id does not match artifact session id."
        )

    return record, summary


def _export_metadata(
    *,
    summary_record: ArtifactRecord,
    summary: SummaryArtifact,
) -> dict[str, object]:
    return {
        "category": "export",
        "contract": SUMMARY_MARKDOWN_EXPORT_CONTRACT,
        "summary_artifact_id": summary_record.id,
        "summary_session_id": summary_record.session_id,
        "summary_version": summary_record.version,
        "summary_created_at": summary.created_at,
        "merged_transcript_artifact_id": summary.merged_transcript_artifact.id,
        "manual_notes_artifact_id": (
            None if summary.manual_notes_artifact is None else summary.manual_notes_artifact.id
        ),
        "correction_overlay_artifact_id": _artifact_reference_metadata(
            key="correction_overlay_artifact_id",
            record=summary_record,
        ),
        "exported_at": _now_utc_iso8601(),
    }


def _artifact_reference_metadata(
    *,
    key: str,
    record: ArtifactRecord,
) -> str | None:
    value = record.metadata.get(key)
    if isinstance(value, str) and value:
        return value
    return None


def _export_artifact_name(*, summary_record: ArtifactRecord, summary: SummaryArtifact) -> Path:
    created_at_token = _format_timestamp(summary.created_at)
    normalized_path = Path(summary_record.path).as_posix().replace("/", "_")
    return Path("summary_markdown") / f"{created_at_token}-{normalized_path}.md"


def _summary_created_at(summary: SummaryArtifact) -> datetime:
    return _parse_iso_timestamp(summary.created_at) or datetime.min.replace(tzinfo=UTC)


def _parse_iso_timestamp(value: str) -> datetime | None:
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _format_timestamp(value: str) -> str:
    parsed = _parse_iso_timestamp(value)
    if parsed is None:
        raise SummaryMarkdownExportError(f"Invalid summary timestamp: {value}")
    return parsed.astimezone(UTC).replace(microsecond=0).isoformat().replace(":", "")


def _now_utc_iso8601() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
