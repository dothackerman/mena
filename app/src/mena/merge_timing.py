"""Merged transcript contract and deterministic merge/timing stage."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Self

from mena.artifacts import ArtifactRecord, ArtifactStore
from mena.diarization import DiarizationArtifact, DiarizationContractError, DiarizationSegment
from mena.processing import (
    ArtifactRef,
    ProcessingErrorInfo,
    ProcessingStageLedger,
    ProcessingStageState,
)
from mena.transcription import TranscriptArtifact, TranscriptionContractError, TranscriptSegment

MERGED_TRANSCRIPT_ARTIFACT_TYPE = "merged_transcript"
MERGED_TRANSCRIPT_ARTIFACT_VERSION = 1
MERGED_TRANSCRIPT_SCHEMA_VERSION = 1
MERGE_TIMING_STAGE = "merge_timing"
MERGE_POLICY_VERSION = 1
CHANNELS = frozenset({"local", "remote"})
DIARIZATION_STATUSES = frozenset({"used", "skipped", "unavailable", "not_applicable"})
CONFIDENCE_STATUSES = frozenset({"available", "unavailable"})
SPEAKER_KINDS = frozenset({"local_user", "diarized_remote", "fallback_remote"})
_STABLE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_:-]*$")


class MergeTimingError(RuntimeError):
    """Base class for merge/timing stage failures."""


class MergeTimingContractError(MergeTimingError):
    """Raised when a merged transcript payload violates the v1 contract."""


class MergeTimingInputError(MergeTimingError):
    """Raised when merge/timing inputs cannot be consumed safely."""


@dataclass(frozen=True)
class MergePolicy:
    """Deterministic merge policy for the PoC dual-stream transcript slice."""

    policy_version: int = MERGE_POLICY_VERSION
    capture_profile: str = "online_dual_stream"
    local_source_id: str = "local_mic"
    remote_source_id: str = "system_audio"
    local_speaker_id: str = "you"
    local_speaker_label: str = "You"
    remote_fallback_speaker_id: str = "remote"
    remote_fallback_speaker_label: str = "Remote"
    sort_order: str = "start_seconds,end_seconds,channel,source_id,transcript_segment_id"

    @classmethod
    def from_dict(cls, data: object) -> Self:
        item = _mapping(data, "Merge policy")
        policy = cls(
            policy_version=_required_int(item, "policy_version", "Merge policy"),
            capture_profile=_required_str(item, "capture_profile", "Merge policy"),
            local_source_id=_required_str(item, "local_source_id", "Merge policy"),
            remote_source_id=_required_str(item, "remote_source_id", "Merge policy"),
            local_speaker_id=_required_str(item, "local_speaker_id", "Merge policy"),
            local_speaker_label=_required_str(item, "local_speaker_label", "Merge policy"),
            remote_fallback_speaker_id=_required_str(
                item, "remote_fallback_speaker_id", "Merge policy"
            ),
            remote_fallback_speaker_label=_required_str(
                item, "remote_fallback_speaker_label", "Merge policy"
            ),
            sort_order=_required_str(item, "sort_order", "Merge policy"),
        )
        if policy.policy_version != MERGE_POLICY_VERSION:
            raise MergeTimingContractError("Unsupported merge policy version.")
        for value, label in (
            (policy.capture_profile, "Merge policy capture_profile"),
            (policy.local_source_id, "Merge policy local_source_id"),
            (policy.remote_source_id, "Merge policy remote_source_id"),
            (policy.local_speaker_id, "Merge policy local_speaker_id"),
            (policy.remote_fallback_speaker_id, "Merge policy remote_fallback_speaker_id"),
        ):
            _validate_stable_id(value, label)
        return policy

    @property
    def config_id(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def to_dict(self) -> dict[str, object]:
        return {
            "policy_version": self.policy_version,
            "capture_profile": self.capture_profile,
            "local_source_id": self.local_source_id,
            "remote_source_id": self.remote_source_id,
            "local_speaker_id": self.local_speaker_id,
            "local_speaker_label": self.local_speaker_label,
            "remote_fallback_speaker_id": self.remote_fallback_speaker_id,
            "remote_fallback_speaker_label": self.remote_fallback_speaker_label,
            "sort_order": self.sort_order,
        }


@dataclass(frozen=True)
class SegmentConfidence:
    """Deterministic confidence evidence preserved for downstream review."""

    status: str
    metric: str | None = None
    value: float | None = None
    reason: str | None = None

    @classmethod
    def from_dict(cls, data: object, *, label: str) -> Self:
        item = _mapping(data, label)
        confidence = cls(
            status=_required_str(item, "status", label),
            metric=_optional_str(item, "metric", label),
            value=_optional_number(item, "value", label),
            reason=_optional_str(item, "reason", label),
        )
        if confidence.status not in CONFIDENCE_STATUSES:
            raise MergeTimingContractError(f"{label} status is invalid.")
        if confidence.status == "available":
            if confidence.metric is None or confidence.value is None:
                raise MergeTimingContractError(
                    f"{label} available confidence must include metric and value."
                )
            if confidence.metric in {"word_probability", "temporal_overlap_ratio", "structural"}:
                _validate_probability(confidence.value, f"{label} value")
        if confidence.status == "unavailable" and not confidence.reason:
            raise MergeTimingContractError(f"{label} unavailable confidence must include a reason.")
        return confidence

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "metric": self.metric,
            "value": self.value,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SpeakerIdentity:
    """Merged speaker attribution for one transcript segment."""

    speaker_id: str
    label: str
    kind: str
    confidence: SegmentConfidence

    @classmethod
    def from_dict(cls, data: object) -> Self:
        item = _mapping(data, "Speaker identity")
        speaker = cls(
            speaker_id=_required_str(item, "speaker_id", "Speaker identity"),
            label=_required_str(item, "label", "Speaker identity"),
            kind=_required_str(item, "kind", "Speaker identity"),
            confidence=SegmentConfidence.from_dict(
                item.get("confidence"), label="Speaker confidence"
            ),
        )
        _validate_stable_id(speaker.speaker_id, "Speaker identity speaker_id")
        if speaker.kind not in SPEAKER_KINDS:
            raise MergeTimingContractError("Speaker identity kind is invalid.")
        return speaker

    def to_dict(self) -> dict[str, object]:
        return {
            "speaker_id": self.speaker_id,
            "label": self.label,
            "kind": self.kind,
            "confidence": self.confidence.to_dict(),
        }


@dataclass(frozen=True)
class MergedTranscriptSource:
    """Source-level provenance for one transcript stream consumed by merge/timing."""

    source_id: str
    channel: str
    transcript_artifact: ArtifactRef
    diarization_status: str
    diarization_reason: str | None = None
    regular_diarization_artifact: ArtifactRef | None = None
    exclusive_diarization_artifact: ArtifactRef | None = None

    @classmethod
    def from_dict(cls, data: object) -> Self:
        item = _mapping(data, "Merged transcript source")
        source = cls(
            source_id=_required_str(item, "source_id", "Merged transcript source"),
            channel=_required_str(item, "channel", "Merged transcript source"),
            transcript_artifact=ArtifactRef.from_dict(item.get("transcript_artifact")),
            diarization_status=_required_str(
                item, "diarization_status", "Merged transcript source"
            ),
            diarization_reason=_optional_str(
                item, "diarization_reason", "Merged transcript source"
            ),
            regular_diarization_artifact=(
                None
                if item.get("regular_diarization_artifact") is None
                else ArtifactRef.from_dict(item.get("regular_diarization_artifact"))
            ),
            exclusive_diarization_artifact=(
                None
                if item.get("exclusive_diarization_artifact") is None
                else ArtifactRef.from_dict(item.get("exclusive_diarization_artifact"))
            ),
        )
        _validate_stable_id(source.source_id, "Merged transcript source source_id")
        if source.channel not in CHANNELS:
            raise MergeTimingContractError("Merged transcript source channel is invalid.")
        if source.diarization_status not in DIARIZATION_STATUSES:
            raise MergeTimingContractError(
                "Merged transcript source diarization_status is invalid."
            )
        if source.diarization_status == "used":
            if (
                source.regular_diarization_artifact is None
                or source.exclusive_diarization_artifact is None
            ):
                raise MergeTimingContractError(
                    "Used diarization must reference regular and exclusive artifacts."
                )
        else:
            if source.diarization_reason is None:
                raise MergeTimingContractError(
                    "Skipped, unavailable, or not_applicable diarization must record a reason."
                )
        return source

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "channel": self.channel,
            "transcript_artifact": self.transcript_artifact.to_dict(),
            "diarization_status": self.diarization_status,
            "diarization_reason": self.diarization_reason,
            "regular_diarization_artifact": (
                None
                if self.regular_diarization_artifact is None
                else self.regular_diarization_artifact.to_dict()
            ),
            "exclusive_diarization_artifact": (
                None
                if self.exclusive_diarization_artifact is None
                else self.exclusive_diarization_artifact.to_dict()
            ),
        }


@dataclass(frozen=True)
class MergedTranscriptSegment:
    """Deterministic merged transcript segment with provenance and review evidence."""

    id: str
    text: str
    start_seconds: float
    end_seconds: float
    source_id: str
    channel: str
    speaker: SpeakerIdentity
    transcription_confidence: SegmentConfidence
    overlap_detected: bool
    transcript_segment_id: str
    diarization_segment_ids: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: object) -> Self:
        item = _mapping(data, "Merged transcript segment")
        segment = cls(
            id=_required_str(item, "id", "Merged transcript segment"),
            text=_required_str(item, "text", "Merged transcript segment"),
            start_seconds=_required_number(item, "start_seconds", "Merged transcript segment"),
            end_seconds=_required_number(item, "end_seconds", "Merged transcript segment"),
            source_id=_required_str(item, "source_id", "Merged transcript segment"),
            channel=_required_str(item, "channel", "Merged transcript segment"),
            speaker=SpeakerIdentity.from_dict(item.get("speaker")),
            transcription_confidence=SegmentConfidence.from_dict(
                item.get("transcription_confidence"),
                label="Transcription confidence",
            ),
            overlap_detected=_required_bool(item, "overlap_detected", "Merged transcript segment"),
            transcript_segment_id=_required_str(
                item,
                "transcript_segment_id",
                "Merged transcript segment",
            ),
            diarization_segment_ids=tuple(
                _required_str(value, "", "Diarization segment id")
                for value in _list(item, "diarization_segment_ids")
            ),
            metadata=_required_json_object(item, "metadata", "Merged transcript segment metadata"),
        )
        for value, label in (
            (segment.id, "Merged transcript segment id"),
            (segment.source_id, "Merged transcript segment source_id"),
            (segment.transcript_segment_id, "Merged transcript transcript_segment_id"),
        ):
            _validate_stable_id(value, label)
        if segment.channel not in CHANNELS:
            raise MergeTimingContractError("Merged transcript segment channel is invalid.")
        for diarization_segment_id in segment.diarization_segment_ids:
            _validate_stable_id(diarization_segment_id, "Merged transcript diarization_segment_id")
        _validate_span(
            segment.start_seconds,
            segment.end_seconds,
            "Merged transcript segment",
        )
        return segment

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "text": self.text,
            "start_seconds": self.start_seconds,
            "end_seconds": self.end_seconds,
            "source_id": self.source_id,
            "channel": self.channel,
            "speaker": self.speaker.to_dict(),
            "transcription_confidence": self.transcription_confidence.to_dict(),
            "overlap_detected": self.overlap_detected,
            "transcript_segment_id": self.transcript_segment_id,
            "diarization_segment_ids": list(self.diarization_segment_ids),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class MergedTranscriptArtifact:
    """Persisted `merged-transcript.v1` artifact."""

    version: int
    session_id: str
    policy: MergePolicy
    sources: tuple[MergedTranscriptSource, ...]
    text: str
    segments: tuple[MergedTranscriptSegment, ...]
    created_at: str

    @classmethod
    def from_dict(cls, data: object) -> Self:
        item = _mapping(data, "Merged transcript artifact")
        artifact = cls(
            version=_required_int(item, "version", "Merged transcript artifact"),
            session_id=_required_str(item, "session_id", "Merged transcript artifact"),
            policy=MergePolicy.from_dict(item.get("policy")),
            sources=tuple(
                MergedTranscriptSource.from_dict(source) for source in _list(item, "sources")
            ),
            text=_required_str(item, "text", "Merged transcript artifact"),
            segments=tuple(
                MergedTranscriptSegment.from_dict(segment) for segment in _list(item, "segments")
            ),
            created_at=_required_str(item, "created_at", "Merged transcript artifact"),
        )
        if artifact.version != MERGED_TRANSCRIPT_SCHEMA_VERSION:
            raise MergeTimingContractError("Unsupported merged transcript artifact version.")
        if len(artifact.sources) != 2:
            raise MergeTimingContractError("Merged transcript artifact must describe two sources.")
        if not artifact.segments:
            raise MergeTimingContractError(
                "Merged transcript artifact must contain at least one segment."
            )
        source_ids = {source.source_id for source in artifact.sources}
        if source_ids != {artifact.policy.local_source_id, artifact.policy.remote_source_id}:
            raise MergeTimingContractError(
                "Merged transcript sources must match the merge policy source ids."
            )
        return artifact

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "session_id": self.session_id,
            "policy": self.policy.to_dict(),
            "sources": [source.to_dict() for source in self.sources],
            "text": self.text,
            "segments": [segment.to_dict() for segment in self.segments],
            "created_at": self.created_at,
        }

    def to_json_bytes(self) -> bytes:
        return (json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n").encode("utf-8")


@dataclass(frozen=True)
class MergeTimingStageResult:
    """Result of running, reusing, or re-merging the dual-stream transcript slice."""

    state: ProcessingStageState
    merged_transcript: MergedTranscriptArtifact
    merged_transcript_record: ArtifactRecord
    skipped: bool = False
    skip_reason: str | None = None


def persist_merged_transcript_artifact(
    *,
    store: ArtifactStore,
    artifact: MergedTranscriptArtifact,
    name: str | Path,
) -> ArtifactRecord:
    """Persist one merged transcript payload through the artifact store contract."""

    MergedTranscriptArtifact.from_dict(artifact.to_dict())
    remote_source = next(source for source in artifact.sources if source.channel == "remote")
    return store.store(
        artifact_type=MERGED_TRANSCRIPT_ARTIFACT_TYPE,
        name=name,
        data=artifact.to_json_bytes(),
        version=MERGED_TRANSCRIPT_ARTIFACT_VERSION,
        metadata={
            "category": "derived",
            "contract": "merged-transcript.v1",
            "policy_config_id": artifact.policy.config_id,
            "local_source_id": artifact.policy.local_source_id,
            "remote_source_id": artifact.policy.remote_source_id,
            "remote_diarization_status": remote_source.diarization_status,
            "remote_diarization_reason": remote_source.diarization_reason,
        },
    )


def run_merge_timing_stage(
    *,
    store: ArtifactStore,
    ledger: ProcessingStageLedger,
    local_transcript_record: ArtifactRecord,
    remote_transcript_record: ArtifactRecord,
    policy: MergePolicy | None = None,
    remote_regular_diarization_record: ArtifactRecord | None = None,
    remote_exclusive_diarization_record: ArtifactRecord | None = None,
    remote_diarization_status: str | None = None,
    remote_diarization_reason: str | None = None,
    stage: str = MERGE_TIMING_STAGE,
    attempt: int = 1,
    skip_completed: bool = True,
    now: datetime | None = None,
) -> MergeTimingStageResult:
    """Merge local and remote transcript artifacts into a deterministic merged view."""

    local_ref = ArtifactRef.from_record(local_transcript_record)
    remote_ref = ArtifactRef.from_record(remote_transcript_record)
    input_refs = [local_ref, remote_ref]
    if (
        remote_regular_diarization_record is not None
        or remote_exclusive_diarization_record is not None
    ):
        if remote_regular_diarization_record is None or remote_exclusive_diarization_record is None:
            raise MergeTimingInputError(
                "Merge/timing requires both regular and exclusive diarization artifacts together."
            )
        input_refs.extend(
            (
                ArtifactRef.from_record(remote_regular_diarization_record),
                ArtifactRef.from_record(remote_exclusive_diarization_record),
            )
        )
        effective_remote_diarization_status = "used"
        effective_remote_diarization_reason = None
    else:
        effective_remote_diarization_status = remote_diarization_status or "unavailable"
        effective_remote_diarization_reason = (
            remote_diarization_reason or "remote_diarization_unavailable"
        )
    if effective_remote_diarization_status not in DIARIZATION_STATUSES:
        raise MergeTimingInputError("Merge/timing remote diarization status is invalid.")
    input_artifacts = tuple(input_refs)

    if skip_completed:
        completed = _completed_state_for_inputs(
            store=store,
            ledger=ledger,
            stage=stage,
            input_artifacts=input_artifacts,
            policy_config_id=_policy_for_reuse_lookup(
                policy=policy,
                local_transcript_record=local_transcript_record,
                remote_transcript_record=remote_transcript_record,
            ).config_id,
            remote_diarization_status=effective_remote_diarization_status,
            remote_diarization_reason=effective_remote_diarization_reason,
        )
        if completed is not None:
            reused = _load_reusable_output(store=store, completed=completed)
            if reused is not None:
                record, artifact = reused
                timestamp = _timestamp(now)
                state = ledger.record(
                    ProcessingStageState(
                        version=1,
                        session_id=ledger.session_id,
                        run_id=ledger.run_id,
                        stage=stage,
                        status="skipped",
                        input_artifacts=input_artifacts,
                        output_artifacts=completed.output_artifacts,
                        started_at=timestamp,
                        completed_at=timestamp,
                        error=None,
                        attempt=attempt,
                    )
                )
                return MergeTimingStageResult(
                    state=state,
                    merged_transcript=artifact,
                    merged_transcript_record=record,
                    skipped=True,
                    skip_reason="completed_output_reused",
                )

    started = ledger.mark_running(
        stage=stage,
        input_artifacts=input_artifacts,
        attempt=attempt,
        now=now,
    )
    try:
        local_transcript = _verify_and_load_transcript(store=store, record=local_transcript_record)
        remote_transcript = _verify_and_load_transcript(
            store=store, record=remote_transcript_record
        )
        effective_policy = policy or MergePolicy(
            local_source_id=local_transcript.source_id or "local_mic",
            remote_source_id=remote_transcript.source_id or "system_audio",
        )
        _validate_transcript_matches_policy(
            local_transcript=local_transcript,
            remote_transcript=remote_transcript,
            policy=effective_policy,
            ledger_session_id=ledger.session_id,
        )
        remote_regular = None
        remote_exclusive = None
        if (
            remote_regular_diarization_record is not None
            and remote_exclusive_diarization_record is not None
        ):
            remote_regular = _verify_and_load_diarization(
                store=store, record=remote_regular_diarization_record
            )
            remote_exclusive = _verify_and_load_diarization(
                store=store,
                record=remote_exclusive_diarization_record,
            )
            if (
                remote_regular.kind != "regular_timeline"
                or remote_exclusive.kind != "exclusive_projection"
            ):
                raise MergeTimingInputError(
                    "Merge/timing requires regular_timeline and "
                    "exclusive_projection diarization artifacts."
                )
            if remote_regular.source_id != effective_policy.remote_source_id:
                raise MergeTimingInputError(
                    "Remote diarization source_id must match the merge policy remote source."
                )
            if remote_exclusive.source_id != effective_policy.remote_source_id:
                raise MergeTimingInputError(
                    "Exclusive diarization source_id must match the merge policy remote source."
                )
            if remote_regular.session_id != ledger.session_id:
                raise MergeTimingInputError(
                    "Remote regular diarization session_id must match the processing ledger."
                )
            if remote_exclusive.session_id != ledger.session_id:
                raise MergeTimingInputError(
                    "Remote exclusive diarization session_id must match the processing ledger."
                )
        artifact = _build_merged_transcript(
            local_transcript=local_transcript,
            remote_transcript=remote_transcript,
            local_record=local_transcript_record,
            remote_record=remote_transcript_record,
            remote_regular_diarization=remote_regular,
            remote_exclusive_diarization=remote_exclusive,
            remote_regular_record=remote_regular_diarization_record,
            remote_exclusive_record=remote_exclusive_diarization_record,
            policy=effective_policy,
            remote_diarization_status=effective_remote_diarization_status,
            remote_diarization_reason=effective_remote_diarization_reason,
            created_at=_timestamp(now),
        )
        output_record = persist_merged_transcript_artifact(
            store=store,
            artifact=artifact,
            name=_merged_transcript_artifact_name(
                run_id=ledger.run_id,
                local_sha256=local_transcript_record.sha256,
                remote_sha256=remote_transcript_record.sha256,
                policy=artifact.policy,
                remote_diarization_status=effective_remote_diarization_status,
                remote_diarization_reason=effective_remote_diarization_reason,
                attempt=attempt,
            ),
        )
    except (
        MergeTimingError,
        DiarizationContractError,
        TranscriptionContractError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as exc:
        failed = _mark_merge_failed(
            ledger=ledger,
            stage=stage,
            input_artifacts=input_artifacts,
            attempt=attempt,
            started_at=started.started_at,
            now=now,
            message=str(exc),
            retryable=False,
            code="merge_timing_failed",
        )
        raise MergeTimingError(failed.error.message if failed.error else str(exc)) from exc

    completed = ledger.mark_completed(
        stage=stage,
        input_artifacts=input_artifacts,
        output_artifacts=(ArtifactRef.from_record(output_record),),
        attempt=attempt,
        started_at=started.started_at,
        now=now,
    )
    return MergeTimingStageResult(
        state=completed,
        merged_transcript=artifact,
        merged_transcript_record=output_record,
    )


def _default_policy() -> MergePolicy:
    return MergePolicy()


def _policy_for_reuse_lookup(
    *,
    policy: MergePolicy | None,
    local_transcript_record: ArtifactRecord,
    remote_transcript_record: ArtifactRecord,
) -> MergePolicy:
    if policy is not None:
        return policy
    return MergePolicy(
        local_source_id=_metadata_source_id(local_transcript_record) or "local_mic",
        remote_source_id=_metadata_source_id(remote_transcript_record) or "system_audio",
    )


def _metadata_source_id(record: ArtifactRecord) -> str | None:
    value = record.metadata.get("source_id")
    if isinstance(value, str) and value:
        return value
    return None


def _verify_and_load_transcript(
    *, store: ArtifactStore, record: ArtifactRecord
) -> TranscriptArtifact:
    verification = store.verify(record.id)
    if not verification.ok:
        raise MergeTimingInputError(verification.error or "transcript artifact failed verification")
    return TranscriptArtifact.from_dict(json.loads(store.retrieve(record.id)))


def _verify_and_load_diarization(
    *, store: ArtifactStore, record: ArtifactRecord
) -> DiarizationArtifact:
    verification = store.verify(record.id)
    if not verification.ok:
        raise MergeTimingInputError(
            verification.error or "diarization artifact failed verification"
        )
    return DiarizationArtifact.from_dict(json.loads(store.retrieve(record.id)))


def _validate_transcript_matches_policy(
    *,
    local_transcript: TranscriptArtifact,
    remote_transcript: TranscriptArtifact,
    policy: MergePolicy,
    ledger_session_id: str,
) -> None:
    if local_transcript.session_id != ledger_session_id:
        raise MergeTimingInputError("Local transcript session_id must match the processing ledger.")
    if remote_transcript.session_id != ledger_session_id:
        raise MergeTimingInputError(
            "Remote transcript session_id must match the processing ledger."
        )
    if local_transcript.session_id != remote_transcript.session_id:
        raise MergeTimingInputError("Local and remote transcript session_id values must match.")
    if local_transcript.source_id != policy.local_source_id:
        raise MergeTimingInputError("Local transcript source_id must match the merge policy.")
    if remote_transcript.source_id != policy.remote_source_id:
        raise MergeTimingInputError("Remote transcript source_id must match the merge policy.")


def _build_merged_transcript(
    *,
    local_transcript: TranscriptArtifact,
    remote_transcript: TranscriptArtifact,
    local_record: ArtifactRecord,
    remote_record: ArtifactRecord,
    remote_regular_diarization: DiarizationArtifact | None,
    remote_exclusive_diarization: DiarizationArtifact | None,
    remote_regular_record: ArtifactRecord | None,
    remote_exclusive_record: ArtifactRecord | None,
    policy: MergePolicy,
    remote_diarization_status: str,
    remote_diarization_reason: str | None,
    created_at: str,
) -> MergedTranscriptArtifact:
    local_source = MergedTranscriptSource(
        source_id=policy.local_source_id,
        channel="local",
        transcript_artifact=ArtifactRef.from_record(local_record),
        diarization_status="not_applicable",
        diarization_reason="local_user_structural_attribution",
    )
    remote_source = MergedTranscriptSource(
        source_id=policy.remote_source_id,
        channel="remote",
        transcript_artifact=ArtifactRef.from_record(remote_record),
        diarization_status=remote_diarization_status,
        diarization_reason=remote_diarization_reason
        if remote_diarization_status != "used"
        else None,
        regular_diarization_artifact=(
            None
            if remote_regular_record is None
            else ArtifactRef.from_record(remote_regular_record)
        ),
        exclusive_diarization_artifact=(
            None
            if remote_exclusive_record is None
            else ArtifactRef.from_record(remote_exclusive_record)
        ),
    )
    remote_speakers = {}
    remote_regular_segments: tuple[DiarizationSegment, ...] = ()
    remote_exclusive_segments: tuple[DiarizationSegment, ...] = ()
    if remote_regular_diarization is not None and remote_exclusive_diarization is not None:
        remote_speakers = {
            speaker.id: speaker.label for speaker in remote_regular_diarization.speakers
        }
        remote_regular_segments = remote_regular_diarization.segments
        remote_exclusive_segments = remote_exclusive_diarization.segments

    merged_segments = [
        _local_segment_to_merged(index=index, segment=segment, policy=policy)
        for index, segment in enumerate(local_transcript.segments, start=1)
    ]
    remote_offset = len(merged_segments)
    merged_segments.extend(
        _remote_segment_to_merged(
            index=remote_offset + index,
            segment=segment,
            policy=policy,
            diarization_status=remote_diarization_status,
            diarization_reason=remote_diarization_reason,
            remote_speakers=remote_speakers,
            remote_regular_segments=remote_regular_segments,
            remote_exclusive_segments=remote_exclusive_segments,
        )
        for index, segment in enumerate(remote_transcript.segments, start=1)
    )
    ordered_segments = _mark_cross_segment_overlap(
        tuple(
            sorted(
                merged_segments,
                key=lambda item: (
                    item.start_seconds,
                    item.end_seconds,
                    0 if item.channel == "local" else 1,
                    item.source_id,
                    item.transcript_segment_id,
                ),
            )
        )
    )
    return MergedTranscriptArtifact(
        version=MERGED_TRANSCRIPT_SCHEMA_VERSION,
        session_id=local_transcript.session_id,
        policy=policy,
        sources=(local_source, remote_source),
        text=" ".join(segment.text for segment in ordered_segments).strip(),
        segments=ordered_segments,
        created_at=created_at,
    )


def _local_segment_to_merged(
    *,
    index: int,
    segment: TranscriptSegment,
    policy: MergePolicy,
) -> MergedTranscriptSegment:
    return MergedTranscriptSegment(
        id=f"merged_segment_{index:06d}",
        text=segment.text,
        start_seconds=segment.start_seconds,
        end_seconds=segment.end_seconds,
        source_id=policy.local_source_id,
        channel="local",
        speaker=SpeakerIdentity(
            speaker_id=policy.local_speaker_id,
            label=policy.local_speaker_label,
            kind="local_user",
            confidence=SegmentConfidence(
                status="available",
                metric="structural",
                value=1.0,
                reason="local_microphone_structural_attribution",
            ),
        ),
        transcription_confidence=_transcription_confidence(segment),
        overlap_detected=False,
        transcript_segment_id=segment.id,
    )


def _remote_segment_to_merged(
    *,
    index: int,
    segment: TranscriptSegment,
    policy: MergePolicy,
    diarization_status: str,
    diarization_reason: str | None,
    remote_speakers: Mapping[str, str],
    remote_regular_segments: tuple[DiarizationSegment, ...],
    remote_exclusive_segments: tuple[DiarizationSegment, ...],
) -> MergedTranscriptSegment:
    if diarization_status != "used":
        return MergedTranscriptSegment(
            id=f"merged_segment_{index:06d}",
            text=segment.text,
            start_seconds=segment.start_seconds,
            end_seconds=segment.end_seconds,
            source_id=policy.remote_source_id,
            channel="remote",
            speaker=SpeakerIdentity(
                speaker_id=policy.remote_fallback_speaker_id,
                label=policy.remote_fallback_speaker_label,
                kind="fallback_remote",
                confidence=SegmentConfidence(
                    status="unavailable",
                    reason=diarization_reason or "remote_diarization_not_available",
                ),
            ),
            transcription_confidence=_transcription_confidence(segment),
            overlap_detected=False,
            transcript_segment_id=segment.id,
            metadata={"diarization_status": diarization_status},
        )

    regular_matches = tuple(
        diarization_segment
        for diarization_segment in remote_regular_segments
        if _overlap_duration(
            segment.start_seconds,
            segment.end_seconds,
            diarization_segment.start_seconds,
            diarization_segment.end_seconds,
        )
        > 0
    )
    exclusive_matches = tuple(
        diarization_segment
        for diarization_segment in remote_exclusive_segments
        if _overlap_duration(
            segment.start_seconds,
            segment.end_seconds,
            diarization_segment.start_seconds,
            diarization_segment.end_seconds,
        )
        > 0
    )
    overlap_detected = (
        any(match.overlapped for match in regular_matches)
        or len({match.speaker_id for match in regular_matches}) > 1
    )
    diarization_segment_ids = tuple(
        sorted({match.id for match in regular_matches + exclusive_matches})
    )
    durations: dict[str, float] = {}
    for match in exclusive_matches:
        durations[match.speaker_id] = durations.get(match.speaker_id, 0.0) + _overlap_duration(
            segment.start_seconds,
            segment.end_seconds,
            match.start_seconds,
            match.end_seconds,
        )
    if not durations:
        speaker = SpeakerIdentity(
            speaker_id=policy.remote_fallback_speaker_id,
            label=policy.remote_fallback_speaker_label,
            kind="fallback_remote",
            confidence=SegmentConfidence(
                status="unavailable",
                reason="no_matching_diarization_segment",
            ),
        )
    else:
        speaker_id, duration = sorted(durations.items(), key=lambda item: (-item[1], item[0]))[0]
        span = segment.end_seconds - segment.start_seconds
        speaker = SpeakerIdentity(
            speaker_id=speaker_id,
            label=remote_speakers.get(speaker_id, speaker_id),
            kind="diarized_remote",
            confidence=SegmentConfidence(
                status="available",
                metric="temporal_overlap_ratio",
                value=duration / span if span > 0 else 0.0,
                reason="diarization_temporal_alignment",
            ),
        )
    return MergedTranscriptSegment(
        id=f"merged_segment_{index:06d}",
        text=segment.text,
        start_seconds=segment.start_seconds,
        end_seconds=segment.end_seconds,
        source_id=policy.remote_source_id,
        channel="remote",
        speaker=speaker,
        transcription_confidence=_transcription_confidence(segment),
        overlap_detected=overlap_detected,
        transcript_segment_id=segment.id,
        diarization_segment_ids=diarization_segment_ids,
        metadata={"diarization_status": diarization_status},
    )


def _transcription_confidence(segment: TranscriptSegment) -> SegmentConfidence:
    probabilities = [word.probability for word in segment.words if word.probability is not None]
    if probabilities:
        return SegmentConfidence(
            status="available",
            metric="word_probability",
            value=sum(probabilities) / len(probabilities),
            reason="mean_word_probability",
        )
    if segment.avg_logprob is not None:
        return SegmentConfidence(
            status="available",
            metric="avg_logprob",
            value=segment.avg_logprob,
            reason="provider_avg_logprob",
        )
    return SegmentConfidence(
        status="unavailable",
        reason="provider_did_not_emit_transcription_confidence",
    )


def _mark_cross_segment_overlap(
    segments: tuple[MergedTranscriptSegment, ...],
) -> tuple[MergedTranscriptSegment, ...]:
    updated = list(segments)
    for index, segment in enumerate(segments):
        for other_index, other in enumerate(segments[index + 1 :], start=index + 1):
            if (
                other.start_seconds >= segment.end_seconds
                and segment.start_seconds <= other.start_seconds
            ):
                break
            if (
                _overlap_duration(
                    segment.start_seconds,
                    segment.end_seconds,
                    other.start_seconds,
                    other.end_seconds,
                )
                <= 0
            ):
                continue
            updated[index] = replace(updated[index], overlap_detected=True)
            updated[other_index] = replace(updated[other_index], overlap_detected=True)
    return tuple(updated)


def _completed_state_for_inputs(
    *,
    store: ArtifactStore,
    ledger: ProcessingStageLedger,
    stage: str,
    input_artifacts: tuple[ArtifactRef, ...],
    policy_config_id: str,
    remote_diarization_status: str,
    remote_diarization_reason: str | None,
) -> ProcessingStageState | None:
    for state in reversed(ledger.states(stage=stage)):
        if (
            state.status != "completed"
            or state.input_artifacts != input_artifacts
            or len(state.output_artifacts) != 1
        ):
            continue
        output_ref = state.output_artifacts[0]
        if not _artifact_ref_verifies(store=store, artifact_ref=output_ref):
            continue
        record = _record_for_ref(store=store, artifact_ref=output_ref)
        if (
            record.metadata.get("policy_config_id") == policy_config_id
            and record.metadata.get("remote_diarization_status") == remote_diarization_status
            and record.metadata.get("remote_diarization_reason") == remote_diarization_reason
        ):
            return state
    return None


def _load_reusable_output(
    *,
    store: ArtifactStore,
    completed: ProcessingStageState,
) -> tuple[ArtifactRecord, MergedTranscriptArtifact] | None:
    output_ref = completed.output_artifacts[0]
    try:
        record = _record_for_ref(store=store, artifact_ref=output_ref)
        artifact = MergedTranscriptArtifact.from_dict(json.loads(store.retrieve(record.id)))
    except (MergeTimingError, json.JSONDecodeError, KeyError, UnicodeDecodeError):
        return None
    return record, artifact


def _record_for_ref(*, store: ArtifactStore, artifact_ref: ArtifactRef) -> ArtifactRecord:
    for record in store.list(artifact_type=artifact_ref.artifact_type):
        if (
            record.id == artifact_ref.id
            and record.version == artifact_ref.version
            and record.sha256 == artifact_ref.sha256
        ):
            return record
    raise MergeTimingContractError("Merged transcript output ref does not match a known artifact.")


def _artifact_ref_verifies(*, store: ArtifactStore, artifact_ref: ArtifactRef) -> bool:
    try:
        record = _record_for_ref(store=store, artifact_ref=artifact_ref)
        verification = store.verify(record.id)
    except (KeyError, MergeTimingContractError):
        return False
    return verification.ok


def _mark_merge_failed(
    *,
    ledger: ProcessingStageLedger,
    stage: str,
    input_artifacts: tuple[ArtifactRef, ...],
    attempt: int,
    started_at: str | None,
    now: datetime | None,
    message: str,
    retryable: bool,
    code: str,
) -> ProcessingStageState:
    return ledger.mark_failed(
        stage=stage,
        input_artifacts=input_artifacts,
        error=ProcessingErrorInfo(message=message, retryable=retryable, code=code),
        attempt=attempt,
        started_at=started_at,
        now=now,
    )


def _merged_transcript_artifact_name(
    *,
    run_id: str,
    local_sha256: str,
    remote_sha256: str,
    policy: MergePolicy,
    remote_diarization_status: str,
    remote_diarization_reason: str | None,
    attempt: int,
) -> Path:
    scope = sha256(
        (
            f"{policy.config_id}:{remote_diarization_status}:{remote_diarization_reason or ''}"
        ).encode()
    ).hexdigest()[:12]
    return (
        Path(run_id)
        / f"{local_sha256[:8]}-{remote_sha256[:8]}"
        / scope
        / f"attempt-{attempt:03d}.json"
    )


def _overlap_duration(
    left_start: float,
    left_end: float,
    right_start: float,
    right_end: float,
) -> float:
    return max(0.0, min(left_end, right_end) - max(left_start, right_start))


def _mapping(data: object, label: str) -> Mapping[str, object]:
    if not isinstance(data, Mapping):
        raise MergeTimingContractError(f"{label} must be a JSON object.")
    return data


def _list(data: Mapping[str, object], field_name: str) -> list[object]:
    value = data.get(field_name)
    if not isinstance(value, list):
        raise MergeTimingContractError(f"Field '{field_name}' must be an array.")
    return value


def _required_str(data: Mapping[str, object] | object, field_name: str, label: str) -> str:
    if not isinstance(data, Mapping):
        if not isinstance(data, str) or not data:
            raise MergeTimingContractError(f"{label} must be a non-empty string.")
        return data
    value = data.get(field_name)
    if not isinstance(value, str) or not value:
        raise MergeTimingContractError(
            f"Field '{field_name}' in {label} must be a non-empty string."
        )
    return value


def _optional_str(data: Mapping[str, object], field_name: str, label: str) -> str | None:
    value = data.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise MergeTimingContractError(
            f"Field '{field_name}' in {label} must be null or a non-empty string."
        )
    return value


def _required_int(data: Mapping[str, object], field_name: str, label: str) -> int:
    value = data.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise MergeTimingContractError(f"Field '{field_name}' in {label} must be an integer.")
    return value


def _required_number(data: Mapping[str, object], field_name: str, label: str) -> float:
    value = data.get(field_name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MergeTimingContractError(f"Field '{field_name}' in {label} must be a number.")
    return float(value)


def _optional_number(data: Mapping[str, object], field_name: str, label: str) -> float | None:
    value = data.get(field_name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MergeTimingContractError(f"Field '{field_name}' in {label} must be null or a number.")
    return float(value)


def _required_bool(data: Mapping[str, object], field_name: str, label: str) -> bool:
    value = data.get(field_name)
    if not isinstance(value, bool):
        raise MergeTimingContractError(f"Field '{field_name}' in {label} must be a boolean.")
    return value


def _required_json_object(
    data: Mapping[str, object],
    field_name: str,
    label: str,
) -> Mapping[str, object]:
    value = data.get(field_name)
    if not isinstance(value, Mapping):
        raise MergeTimingContractError(f"{label} must be a JSON object.")
    return value


def _validate_stable_id(value: str, label: str) -> None:
    if not _STABLE_ID_PATTERN.fullmatch(value):
        raise MergeTimingContractError(f"{label} must be a stable identifier.")


def _validate_probability(value: float | None, label: str) -> None:
    if value is None:
        return
    if not 0.0 <= value <= 1.0:
        raise MergeTimingContractError(f"{label} must be within [0, 1].")


def _validate_span(start_seconds: float, end_seconds: float, label: str) -> None:
    if start_seconds < 0 or end_seconds <= start_seconds:
        raise MergeTimingContractError(f"{label} timestamps must be non-negative and increasing.")


def _timestamp(now: datetime | None) -> str:
    current = now or datetime.now(UTC)
    return current.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
