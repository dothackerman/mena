"""Correction overlay contract and pure transcript application."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from mena.artifacts import ArtifactRecord, ArtifactStore
from mena.merge_timing import (
    MergedTranscriptArtifact,
    MergedTranscriptSegment,
    SegmentConfidence,
    SpeakerIdentity,
)
from mena.processing import ArtifactRef

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
        entry = cls(
            id=_required_str(item, "id", "Correction entry"),
            segment_id=_required_str(item, "segment_id", "Correction entry"),
            correction_type=_required_str(item, "correction_type", "Correction entry"),
            user_action_at=_required_str(item, "user_action_at", "Correction entry"),
            original_flag_id=_optional_str(item, "original_flag_id", "Correction entry"),
            speaker_id=_optional_str(item, "speaker_id", "Correction entry"),
            speaker_label=_optional_str(item, "speaker_label", "Correction entry"),
            text=_optional_str(item, "text", "Correction entry"),
            overlap_detected=_optional_bool(item, "overlap_detected", "Correction entry"),
            metadata=_required_json_object(item, "metadata", "Correction entry metadata"),
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
        artifact = cls(
            version=_required_int(item, "version", "Correction overlay artifact"),
            session_id=_required_str(item, "session_id", "Correction overlay artifact"),
            source_transcript_artifact=ArtifactRef.from_dict(
                item.get("source_transcript_artifact")
            ),
            review_queue_artifact=(
                None
                if item.get("review_queue_artifact") is None
                else ArtifactRef.from_dict(item.get("review_queue_artifact"))
            ),
            entries=tuple(CorrectionEntry.from_dict(entry) for entry in _list(item, "entries")),
            created_at=_required_str(item, "created_at", "Correction overlay artifact"),
        )
        if artifact.version != CORRECTION_OVERLAY_SCHEMA_VERSION:
            raise CorrectionOverlayContractError("Unsupported correction overlay artifact version.")
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
) -> ArtifactRecord:
    """Persist one correction overlay payload through the artifact store contract."""

    CorrectionOverlayArtifact.from_dict(artifact.to_dict())
    return store.store(
        artifact_type=CORRECTION_OVERLAY_ARTIFACT_TYPE,
        name=name,
        data=artifact.to_json_bytes(),
        version=CORRECTION_OVERLAY_ARTIFACT_VERSION,
        metadata={
            "category": "review",
            "contract": "correction-overlay.v1",
            "source_transcript_artifact_id": artifact.source_transcript_artifact.id,
            "review_queue_artifact_id": (
                None
                if artifact.review_queue_artifact is None
                else artifact.review_queue_artifact.id
            ),
        },
    )


def apply_correction_overlay(
    *,
    source_transcript: MergedTranscriptArtifact,
    source_transcript_record: ArtifactRecord,
    overlay: CorrectionOverlayArtifact,
) -> MergedTranscriptArtifact:
    """Apply an overlay without mutating the source transcript artifact."""

    if ArtifactRef.from_record(source_transcript_record) != overlay.source_transcript_artifact:
        raise CorrectionOverlayContractError(
            "Correction overlay source_transcript_artifact must match the source record."
        )
    if overlay.session_id != source_transcript.session_id:
        raise CorrectionOverlayContractError(
            "Correction overlay session_id must match the source transcript."
        )
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
    return segment


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


def _mapping(data: object, label: str) -> Mapping[str, object]:
    if not isinstance(data, Mapping):
        raise CorrectionOverlayContractError(f"{label} must be an object.")
    return data


def _list(data: Mapping[str, object], key: str) -> list[object]:
    value = data.get(key)
    if not isinstance(value, list):
        raise CorrectionOverlayContractError(f"{key} must be an array.")
    return value


def _required_str(data: Mapping[str, object], key: str, label: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or value == "":
        raise CorrectionOverlayContractError(f"{label} {key} must be a non-empty string.")
    return value


def _optional_str(data: Mapping[str, object], key: str, label: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or value == "":
        raise CorrectionOverlayContractError(f"{label} {key} must be a non-empty string or null.")
    return value


def _required_int(data: Mapping[str, object], key: str, label: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise CorrectionOverlayContractError(f"{label} {key} must be an integer.")
    return value


def _optional_bool(data: Mapping[str, object], key: str, label: str) -> bool | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise CorrectionOverlayContractError(f"{label} {key} must be a boolean or null.")
    return value


def _required_json_object(data: Mapping[str, object], key: str, label: str) -> Mapping[str, object]:
    value = data.get(key)
    if not isinstance(value, Mapping):
        raise CorrectionOverlayContractError(f"{label} must be an object.")
    for item_key in value:
        if not isinstance(item_key, str):
            raise CorrectionOverlayContractError(f"{label} keys must be strings.")
    return value


def _validate_stable_id(value: str, label: str) -> None:
    if not _STABLE_ID_PATTERN.fullmatch(value):
        raise CorrectionOverlayContractError(f"{label} must be a stable id.")


def _timestamp(now: datetime | None) -> str:
    current = now or datetime.now(tz=UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).isoformat().replace("+00:00", "Z")
