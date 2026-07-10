"""Summary markdown export service."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from mena.artifacts import ArtifactRecord, ArtifactStore
from mena.exportable_summary import ExportableSummaryError, resolve_exportable_summary
from mena.summarization import (
    SummaryArtifact,
)
from mena.summary_markdown import format_summary_markdown
from mena.timestamps import parse_utc_timestamp, utc_timestamp

SUMMARY_MARKDOWN_EXPORT_ARTIFACT_TYPE = "summary_markdown_export"
SUMMARY_MARKDOWN_EXPORT_ARTIFACT_VERSION = 3
SUMMARY_MARKDOWN_EXPORT_CONTRACT = "summary-markdown-note-package-v3"


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
    export_name = _export_artifact_name(summary_record=summary_record, summary=summary)
    metadata = _export_metadata(summary_record=summary_record, summary=summary)

    existing_export = _matching_existing_export(
        store=store,
        export_name=export_name,
        markdown=markdown,
        metadata=metadata,
    )
    if existing_export is not None:
        return SummaryMarkdownExportResult(
            summary_record=summary_record,
            summary=summary,
            export_record=existing_export,
        )

    export_record = store.store(
        artifact_type=SUMMARY_MARKDOWN_EXPORT_ARTIFACT_TYPE,
        name=export_name,
        data=markdown.encode("utf-8"),
        version=SUMMARY_MARKDOWN_EXPORT_ARTIFACT_VERSION,
        status="sealed",
        metadata=metadata,
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

    try:
        selection = resolve_exportable_summary(
            store=store,
            summary_record_id=summary_record_id,
        )
    except ExportableSummaryError as exc:
        raise SummaryMarkdownExportError(str(exc)) from exc

    return selection.record, selection.summary


def _matching_existing_export(
    *,
    store: ArtifactStore,
    export_name: Path,
    markdown: str,
    metadata: dict[str, object],
) -> ArtifactRecord | None:
    expected_path = Path("artifacts") / SUMMARY_MARKDOWN_EXPORT_ARTIFACT_TYPE / export_name
    expected_payload = markdown.encode("utf-8")
    expected_sha = _sha256(expected_payload)
    for record in store.list(artifact_type=SUMMARY_MARKDOWN_EXPORT_ARTIFACT_TYPE):
        if record.path != expected_path.as_posix():
            continue
        verification = store.verify(record.id)
        if not verification.ok or record.sha256 != expected_sha:
            raise SummaryMarkdownExportError(
                f"Existing markdown export artifact {record.id} failed integrity verification."
            )
        if store.retrieve(record.id) != expected_payload:
            raise SummaryMarkdownExportError(
                f"Existing markdown export artifact {record.id} content does not match."
            )
        for key, value in metadata.items():
            if key == "exported_at":
                continue
            if record.metadata.get(key) != value:
                raise SummaryMarkdownExportError(
                    f"Existing markdown export artifact {record.id} metadata does not match."
                )
        return record
    return None


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
    return (
        Path("summary_markdown")
        / SUMMARY_MARKDOWN_EXPORT_CONTRACT
        / f"{created_at_token}-{normalized_path}.md"
    )


def _parse_iso_timestamp(value: str) -> datetime | None:
    try:
        return parse_utc_timestamp(value)
    except ValueError:
        return None


def _format_timestamp(value: str) -> str:
    parsed = _parse_iso_timestamp(value)
    if parsed is None:
        raise SummaryMarkdownExportError(f"Invalid summary timestamp: {value}")
    return parsed.astimezone(UTC).replace(microsecond=0).isoformat().replace(":", "")


def _now_utc_iso8601() -> str:
    return utc_timestamp()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
