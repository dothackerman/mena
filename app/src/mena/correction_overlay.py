"""Correction overlay contract and pure transcript application."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Self

from mena.artifacts import ArtifactRecord, ArtifactStore
from mena.fields import ContractReader
from mena.merge_timing import (
    MERGED_TRANSCRIPT_ARTIFACT_TYPE,
    MergedTranscriptArtifact,
    MergedTranscriptSegment,
    SegmentConfidence,
    SpeakerIdentity,
)
from mena.processing import ArtifactRef, ProcessingStateError
from mena.timestamps import NaiveTimestampError, parse_utc_timestamp, utc_timestamp

CORRECTION_OVERLAY_ARTIFACT_TYPE = "correction_overlay"
CORRECTION_OVERLAY_ARTIFACT_VERSION = 1
CORRECTION_OVERLAY_SCHEMA_VERSION = 1
CORRECTION_TYPES = frozenset(
    {
        "speaker_reassignment",
        "text_edit",
        "skip_mark",
        "overlap_mark",
    }
)
HUMAN_CORRECTED_SPEAKER_KIND = "human_corrected"
CORRECTION_ENTRY_KEYS = frozenset(
    {
        "id",
        "segment_id",
        "correction_type",
        "original_flag_id",
        "user_action_at",
        "speaker_id",
        "speaker_label",
        "text",
        "overlap_detected",
        "metadata",
    }
)
CORRECTION_OVERLAY_KEYS = frozenset(
    {
        "version",
        "session_id",
        "source_transcript_artifact",
        "review_queue_artifact",
        "entries",
        "created_at",
    }
)
_STABLE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_:-]*$")


class CorrectionOverlayError(RuntimeError):
    """Base class for correction overlay failures."""


class CorrectionOverlayContractError(CorrectionOverlayError):
    """Raised when a correction overlay payload violates the v1 contract."""


@dataclass(frozen=True)
class CorrectionEntry:
    """One user correction applied to a stable source transcript segment."""

    id: str
    segment_id: str
    correction_type: str
    user_action_at: str
    original_flag_id: str | None = None
    speaker_id: str | None = None
    speaker_label: str | None = None
    text: str | None = None
    overlap_detected: bool | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: object) -> Self:
        item = _mapping(data, "Correction entry")
        _reject_unknown_keys(item, CORRECTION_ENTRY_KEYS, "Correction entry")
        reader = ContractReader("Correction entry", CorrectionOverlayContractError)
        entry = cls(
            id=reader.required_str(item, "id"),
            segment_id=reader.required_str(item, "segment_id"),
            correction_type=reader.required_str(item, "correction_type"),
            user_action_at=reader.required_str(item, "user_action_at"),
            original_flag_id=reader.required_nullable_str(item, "original_flag_id"),
            speaker_id=reader.required_nullable_str(item, "speaker_id"),
            speaker_label=reader.required_nullable_str(item, "speaker_label"),
            text=_read_required_nullable_str(item, "text", "Correction entry", allow_empty=True),
            overlap_detected=reader.required_nullable_bool(item, "overlap_detected"),
            metadata=_read_json_object(item, "metadata", "Correction entry"),
        )
        for value, label in (
            (entry.id, "Correction entry id"),
            (entry.segment_id, "Correction entry segment_id"),
        ):
            _validate_stable_id(value, label)
        if entry.original_flag_id is not None:
            _validate_stable_id(entry.original_flag_id, "Correction entry original_flag_id")
        if entry.speaker_id is not None:
            _validate_stable_id(entry.speaker_id, "Correction entry speaker_id")
        if entry.correction_type not in CORRECTION_TYPES:
            raise CorrectionOverlayContractError("Correction entry correction_type is invalid.")
        _validate_timestamp(entry.user_action_at, "Correction entry user_action_at")
        _validate_entry_payload(entry)
        return entry

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "segment_id": self.segment_id,
            "correction_type": self.correction_type,
            "original_flag_id": self.original_flag_id,
            "user_action_at": self.user_action_at,
            "speaker_id": self.speaker_id,
            "speaker_label": self.speaker_label,
            "text": self.text,
            "overlap_detected": self.overlap_detected,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class CorrectionOverlayArtifact:
    """Persisted `correction-overlay.v1` artifact."""

    version: int
    session_id: str
    source_transcript_artifact: ArtifactRef
    entries: tuple[CorrectionEntry, ...]
    created_at: str
    review_queue_artifact: ArtifactRef | None = None

    @classmethod
    def from_dict(cls, data: object) -> Self:
        item = _mapping(data, "Correction overlay artifact")
        _reject_unknown_keys(item, CORRECTION_OVERLAY_KEYS, "Correction overlay artifact")
        reader = ContractReader("Correction overlay artifact", CorrectionOverlayContractError)
        artifact = cls(
            version=reader.required_int(item, "version"),
            session_id=reader.required_str(item, "session_id"),
            source_transcript_artifact=_read_artifact_ref(
                item, "source_transcript_artifact", "Correction overlay artifact"
            ),
            review_queue_artifact=_read_nullable_artifact_ref(
                item, "review_queue_artifact", "Correction overlay artifact"
            ),
            entries=tuple(
                CorrectionEntry.from_dict(entry) for entry in reader.required_list(item, "entries")
            ),
            created_at=reader.required_str(item, "created_at"),
        )
        if artifact.version != CORRECTION_OVERLAY_SCHEMA_VERSION:
            raise CorrectionOverlayContractError("Unsupported correction overlay artifact version.")
        _validate_uuid(artifact.session_id, "Correction overlay artifact session_id")
        _validate_timestamp(artifact.created_at, "Correction overlay artifact created_at")
        _validate_source_transcript_ref(artifact.source_transcript_artifact)
        _validate_unique_entry_ids(artifact.entries)
        return artifact

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "session_id": self.session_id,
            "source_transcript_artifact": self.source_transcript_artifact.to_dict(),
            "review_queue_artifact": (
                None if self.review_queue_artifact is None else self.review_queue_artifact.to_dict()
            ),
            "entries": [entry.to_dict() for entry in self.entries],
            "created_at": self.created_at,
        }

    def to_json_bytes(self) -> bytes:
        return (json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n").encode("utf-8")


def create_correction_overlay(
    *,
    session_id: str,
    source_transcript_artifact: ArtifactRef,
    entries: tuple[CorrectionEntry, ...],
    created_at: str | None = None,
    review_queue_artifact: ArtifactRef | None = None,
    now: datetime | None = None,
) -> CorrectionOverlayArtifact:
    """Create and validate a correction overlay artifact."""

    timestamp = created_at or _timestamp(now)
    artifact = CorrectionOverlayArtifact(
        version=CORRECTION_OVERLAY_SCHEMA_VERSION,
        session_id=session_id,
        source_transcript_artifact=source_transcript_artifact,
        review_queue_artifact=review_queue_artifact,
        entries=entries,
        created_at=timestamp,
    )
    return CorrectionOverlayArtifact.from_dict(artifact.to_dict())


def persist_correction_overlay_artifact(
    *,
    store: ArtifactStore,
    artifact: CorrectionOverlayArtifact,
    name: str | Path,
    status: str = "sealed",
    metadata: Mapping[str, object] | None = None,
) -> ArtifactRecord:
    """Persist one correction overlay payload through the artifact store contract."""

    CorrectionOverlayArtifact.from_dict(artifact.to_dict())
    artifact_metadata: dict[str, object] = {
        "category": "review",
        "contract": "correction-overlay.v1",
        "source_transcript_artifact_id": artifact.source_transcript_artifact.id,
        "review_queue_artifact_id": (
            None if artifact.review_queue_artifact is None else artifact.review_queue_artifact.id
        ),
    }
    if metadata is not None:
        artifact_metadata.update(metadata)
    return store.store(
        artifact_type=CORRECTION_OVERLAY_ARTIFACT_TYPE,
        name=name,
        data=artifact.to_json_bytes(),
        version=CORRECTION_OVERLAY_ARTIFACT_VERSION,
        status=status,
        metadata=artifact_metadata,
    )


def resolve_active_correction_overlay(
    *,
    store: ArtifactStore,
    source_transcript_record: ArtifactRecord | None = None,
) -> tuple[ArtifactRecord, CorrectionOverlayArtifact] | None:
    """Return the active completed correction overlay for a session or source transcript.

    Selection is deterministic:
    - uses completed_at metadata when present,
    - falls back to created_at for legacy completed overlays,
    - prefers highest completed_at timestamp,
    - then stable tie-breakers (created_at, source transcript id, record sha256, id).
    """

    source_ref = (
        ArtifactRef.from_record(source_transcript_record)
        if source_transcript_record is not None
        else None
    )
    best: (
        tuple[tuple[datetime, datetime, str, str, str], ArtifactRecord, CorrectionOverlayArtifact]
        | None
    ) = None
    for record in store.list(artifact_type=CORRECTION_OVERLAY_ARTIFACT_TYPE):
        if record.status != "sealed":
            continue
        if record.metadata.get("workflow_status") != "completed":
            continue
        if (
            source_ref is not None
            and isinstance(record.metadata.get("source_transcript_artifact_id"), str)
            and record.metadata["source_transcript_artifact_id"] != source_ref.id
        ):
            continue
        verification = store.verify(record.id)
        if not verification.ok:
            raise CorrectionOverlayContractError(
                "Completed correction overlay artifact failed verification."
            )

        try:
            overlay_data = json.loads(store.retrieve(record.id))
            overlay = CorrectionOverlayArtifact.from_dict(overlay_data)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CorrectionOverlayContractError(
                "Completed correction overlay artifact could not be decoded."
            ) from exc

        if source_ref is not None and overlay.source_transcript_artifact != source_ref:
            continue

        key = _overlay_recency_key(record=record, overlay=overlay)
        if best is None or key > best[0]:
            best = (key, record, overlay)

    if best is None:
        return None
    return best[1], best[2]


def apply_correction_overlay(
    *,
    source_transcript: MergedTranscriptArtifact,
    source_transcript_record: ArtifactRecord,
    overlay: CorrectionOverlayArtifact,
) -> MergedTranscriptArtifact:
    """Apply an overlay without mutating the source transcript artifact."""

    overlay = CorrectionOverlayArtifact.from_dict(overlay.to_dict())
    if ArtifactRef.from_record(source_transcript_record) != overlay.source_transcript_artifact:
        raise CorrectionOverlayContractError(
            "Correction overlay source_transcript_artifact must match the source record."
        )
    _validate_source_transcript_ref(ArtifactRef.from_record(source_transcript_record))
    if source_transcript_record.session_id != source_transcript.session_id:
        raise CorrectionOverlayContractError(
            "Correction overlay source record session_id must match the source transcript."
        )
    _validate_source_transcript_content(
        source_transcript=source_transcript,
        source_transcript_record=source_transcript_record,
    )
    if overlay.session_id != source_transcript.session_id:
        raise CorrectionOverlayContractError(
            "Correction overlay session_id must match the source transcript."
        )
    _validate_unique_segment_ids(source_transcript.segments)
    segment_ids = {segment.id for segment in source_transcript.segments}
    unknown_ids = [
        entry.segment_id for entry in overlay.entries if entry.segment_id not in segment_ids
    ]
    if unknown_ids:
        raise CorrectionOverlayContractError(
            "Correction overlay entry references an unknown segment_id."
        )

    segments = list(source_transcript.segments)
    positions = {segment.id: index for index, segment in enumerate(segments)}
    for entry in overlay.entries:
        index = positions[entry.segment_id]
        segments[index] = _apply_entry(segment=segments[index], entry=entry)

    corrected = replace(
        source_transcript,
        text=" ".join(segment.text for segment in segments).strip(),
        segments=tuple(segments),
    )
    return MergedTranscriptArtifact.from_dict(corrected.to_dict())


def _apply_entry(
    *,
    segment: MergedTranscriptSegment,
    entry: CorrectionEntry,
) -> MergedTranscriptSegment:
    if entry.correction_type == "speaker_reassignment":
        return replace(
            segment,
            speaker=SpeakerIdentity(
                speaker_id=entry.speaker_id or "",
                label=entry.speaker_label or "",
                kind=HUMAN_CORRECTED_SPEAKER_KIND,
                confidence=SegmentConfidence(
                    status="available",
                    metric="human_correction",
                    value=1.0,
                    reason=entry.id,
                ),
            ),
        )
    if entry.correction_type == "text_edit":
        return replace(segment, text=entry.text or "")
    if entry.correction_type == "overlap_mark":
        return replace(segment, overlap_detected=bool(entry.overlap_detected))
    if entry.correction_type == "skip_mark":
        return segment
    raise CorrectionOverlayContractError("Correction entry correction_type is invalid.")


def _validate_entry_payload(entry: CorrectionEntry) -> None:
    if entry.correction_type == "speaker_reassignment":
        if entry.speaker_id is None or entry.speaker_label is None:
            raise CorrectionOverlayContractError(
                "Speaker reassignment must include speaker_id and speaker_label."
            )
        if entry.text is not None or entry.overlap_detected is not None:
            raise CorrectionOverlayContractError(
                "Speaker reassignment cannot include text or overlap_detected."
            )
    elif entry.correction_type == "text_edit":
        if entry.text is None:
            raise CorrectionOverlayContractError("Text edit must include text.")
        if entry.speaker_id is not None or entry.speaker_label is not None:
            raise CorrectionOverlayContractError(
                "Text edit cannot include speaker_id or speaker_label."
            )
        if entry.overlap_detected is not None:
            raise CorrectionOverlayContractError("Text edit cannot include overlap_detected.")
    elif entry.correction_type == "overlap_mark":
        if entry.overlap_detected is None:
            raise CorrectionOverlayContractError("Overlap mark must include overlap_detected.")
        if (
            entry.speaker_id is not None
            or entry.speaker_label is not None
            or entry.text is not None
        ):
            raise CorrectionOverlayContractError(
                "Overlap mark cannot include speaker_id, speaker_label, or text."
            )
    elif (
        entry.speaker_id is not None
        or entry.speaker_label is not None
        or entry.text is not None
        or entry.overlap_detected is not None
    ):
        raise CorrectionOverlayContractError("Skip mark cannot include correction payload fields.")


def _validate_unique_entry_ids(entries: tuple[CorrectionEntry, ...]) -> None:
    ids = [entry.id for entry in entries]
    if len(ids) != len(set(ids)):
        raise CorrectionOverlayContractError("Correction overlay entry ids must be unique.")


def _validate_unique_segment_ids(segments: tuple[MergedTranscriptSegment, ...]) -> None:
    ids = [segment.id for segment in segments]
    if len(ids) != len(set(ids)):
        raise CorrectionOverlayContractError("Source transcript segment ids must be unique.")


def _validate_source_transcript_ref(artifact_ref: ArtifactRef) -> None:
    if artifact_ref.artifact_type != MERGED_TRANSCRIPT_ARTIFACT_TYPE:
        raise CorrectionOverlayContractError(
            "Correction overlay source_transcript_artifact must reference a merged transcript."
        )


def _validate_source_transcript_content(
    *,
    source_transcript: MergedTranscriptArtifact,
    source_transcript_record: ArtifactRecord,
) -> None:
    data = source_transcript.to_json_bytes()
    if len(data) != source_transcript_record.byte_count:
        raise CorrectionOverlayContractError(
            "Source transcript content does not match the source record byte_count."
        )
    if sha256(data).hexdigest() != source_transcript_record.sha256:
        raise CorrectionOverlayContractError(
            "Source transcript content does not match the source record sha256."
        )


def _mapping(data: object, label: str) -> Mapping[str, object]:
    if not isinstance(data, Mapping):
        raise CorrectionOverlayContractError(f"{label} must be an object.")
    return data


def _reject_unknown_keys(data: Mapping[str, object], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise CorrectionOverlayContractError(f"{label} contains unknown fields: {unknown}.")


def _read_required_nullable_str(
    data: Mapping[str, object],
    key: str,
    label: str,
    *,
    allow_empty: bool = False,
) -> str | None:
    if allow_empty:
        value = _require_key(data, key, label)
        if value is None:
            return None
        if not isinstance(value, str):
            raise CorrectionOverlayContractError(f"{label} field '{key}' must be a string or null.")
        return value
    return ContractReader(label, CorrectionOverlayContractError).required_nullable_str(data, key)


def _read_nullable_artifact_ref(
    data: Mapping[str, object],
    key: str,
    label: str,
) -> ArtifactRef | None:
    value = _require_key(data, key, label)
    if value is None:
        return None
    return _artifact_ref_from_dict(value, f"{label} {key}")


def _read_artifact_ref(
    data: Mapping[str, object],
    key: str,
    label: str,
) -> ArtifactRef:
    return _artifact_ref_from_dict(_require_key(data, key, label), f"{label} {key}")


def _artifact_ref_from_dict(data: object, label: str) -> ArtifactRef:
    try:
        return ArtifactRef.from_dict(data)
    except ProcessingStateError as exc:
        raise CorrectionOverlayContractError(f"{label} is invalid: {exc}") from exc


def _read_json_object(data: Mapping[str, object], key: str, label: str) -> Mapping[str, object]:
    value = ContractReader(label, CorrectionOverlayContractError).required_mapping(data, key)
    for item_key in value:
        if not isinstance(item_key, str):
            raise CorrectionOverlayContractError(f"{label} field '{key}' keys must be strings.")
    return value


def _require_key(data: Mapping[str, object], key: str, label: str) -> object:
    if key not in data:
        raise CorrectionOverlayContractError(f"{label} field '{key}' is required.")
    return data[key]


def _overlay_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise CorrectionOverlayContractError(f"{label} must be a timestamp string.")
    try:
        return parse_utc_timestamp(value)
    except NaiveTimestampError as exc:
        raise CorrectionOverlayContractError(f"{label} must include a timezone.") from exc
    except ValueError as exc:
        raise CorrectionOverlayContractError(f"{label} must be a RFC 3339 date-time.") from exc


def _overlay_record_timestamp(
    *, record: ArtifactRecord, overlay: CorrectionOverlayArtifact
) -> datetime:
    completed_at = record.metadata.get("completed_at")
    if isinstance(completed_at, str):
        return _overlay_timestamp(completed_at, "Correction overlay completed_at metadata")
    if completed_at is not None:
        raise CorrectionOverlayContractError(
            "Correction overlay completed_at metadata must be a timestamp string."
        )
    return _overlay_timestamp(overlay.created_at, "Correction overlay created_at")


def _overlay_recency_key(
    *, record: ArtifactRecord, overlay: CorrectionOverlayArtifact
) -> tuple[datetime, datetime, str, str, str]:
    completed_at = _overlay_record_timestamp(record=record, overlay=overlay)
    created_at = _overlay_timestamp(overlay.created_at, "Correction overlay created_at")
    return (
        completed_at,
        created_at,
        overlay.source_transcript_artifact.id,
        record.sha256,
        record.id,
    )


def _validate_stable_id(value: str, label: str) -> None:
    if not _STABLE_ID_PATTERN.fullmatch(value):
        raise CorrectionOverlayContractError(f"{label} must be a stable id.")


def _validate_uuid(value: str, label: str) -> None:
    try:
        uuid.UUID(value)
    except ValueError as exc:
        raise CorrectionOverlayContractError(f"{label} must be a UUID.") from exc


def _validate_timestamp(value: str, label: str) -> None:
    try:
        parse_utc_timestamp(value)
    except NaiveTimestampError as exc:
        raise CorrectionOverlayContractError(f"{label} must include a timezone.") from exc
    except ValueError as exc:
        raise CorrectionOverlayContractError(f"{label} must be an RFC 3339 date-time.") from exc


def _timestamp(now: datetime | None) -> str:
    current = now or datetime.now(tz=UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return utc_timestamp(current)
