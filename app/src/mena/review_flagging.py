"""Confidence-based review flagging contract and processing stage."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Self

from mena.artifacts import ArtifactRecord, ArtifactStore
from mena.merge_timing import (
    MergedTranscriptArtifact,
    MergedTranscriptSegment,
    MergeTimingContractError,
)
from mena.processing import (
    ArtifactRef,
    ProcessingErrorInfo,
    ProcessingStageLedger,
    ProcessingStageState,
)

REVIEW_QUEUE_ARTIFACT_TYPE = "review_queue"
REVIEW_QUEUE_ARTIFACT_VERSION = 1
REVIEW_QUEUE_SCHEMA_VERSION = 1
CONFIDENCE_FLAGGING_STAGE = "confidence_flagging"
FLAGGING_POLICY_VERSION = 1
REASON_CODES = frozenset(
    {
        "low_transcription_confidence",
        "low_speaker_confidence",
        "speaker_overlap",
    }
)
_STABLE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_:-]*$")


class ReviewFlaggingError(RuntimeError):
    """Base class for review flagging failures."""


class ReviewFlaggingContractError(ReviewFlaggingError):
    """Raised when a review queue payload violates the v1 contract."""


@dataclass(frozen=True)
class ConfidenceFlaggingPolicy:
    """Deterministic thresholds and behavior for review flag generation."""

    policy_version: int = FLAGGING_POLICY_VERSION
    min_word_probability: float = 0.85
    min_avg_logprob: float = -0.6
    min_speaker_overlap_ratio: float = 0.85
    flag_unavailable_speaker_confidence: bool = True

    @classmethod
    def from_dict(cls, data: object) -> Self:
        item = _mapping(data, "Confidence flagging policy")
        policy = cls(
            policy_version=_required_int(item, "policy_version", "Confidence flagging policy"),
            min_word_probability=_required_number(
                item,
                "min_word_probability",
                "Confidence flagging policy",
            ),
            min_avg_logprob=_required_number(
                item,
                "min_avg_logprob",
                "Confidence flagging policy",
            ),
            min_speaker_overlap_ratio=_required_number(
                item,
                "min_speaker_overlap_ratio",
                "Confidence flagging policy",
            ),
            flag_unavailable_speaker_confidence=_required_bool(
                item,
                "flag_unavailable_speaker_confidence",
                "Confidence flagging policy",
            ),
        )
        if policy.policy_version != FLAGGING_POLICY_VERSION:
            raise ReviewFlaggingContractError("Unsupported confidence flagging policy version.")
        _validate_probability(policy.min_word_probability, "min_word_probability")
        _validate_probability(policy.min_speaker_overlap_ratio, "min_speaker_overlap_ratio")
        return policy

    @property
    def config_id(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def to_dict(self) -> dict[str, object]:
        return {
            "policy_version": self.policy_version,
            "min_word_probability": self.min_word_probability,
            "min_avg_logprob": self.min_avg_logprob,
            "min_speaker_overlap_ratio": self.min_speaker_overlap_ratio,
            "flag_unavailable_speaker_confidence": self.flag_unavailable_speaker_confidence,
        }


@dataclass(frozen=True)
class ReviewFlag:
    """One structured review flag emitted by confidence-based flagging."""

    id: str
    segment_id: str
    reason_code: str
    start_seconds: float
    end_seconds: float
    source_id: str
    speaker_id: str | None
    metadata: Mapping[str, object] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: object) -> Self:
        item = _mapping(data, "Review flag")
        flag = cls(
            id=_required_str(item, "id", "Review flag"),
            segment_id=_required_str(item, "segment_id", "Review flag"),
            reason_code=_required_str(item, "reason_code", "Review flag"),
            start_seconds=_required_number(item, "start_seconds", "Review flag"),
            end_seconds=_required_number(item, "end_seconds", "Review flag"),
            source_id=_required_str(item, "source_id", "Review flag"),
            speaker_id=_optional_str(item, "speaker_id", "Review flag"),
            metadata=_required_json_object(item, "metadata", "Review flag metadata"),
        )
        for value, label in (
            (flag.id, "Review flag id"),
            (flag.segment_id, "Review flag segment_id"),
            (flag.source_id, "Review flag source_id"),
        ):
            _validate_stable_id(value, label)
        if flag.speaker_id is not None:
            _validate_stable_id(flag.speaker_id, "Review flag speaker_id")
        if flag.reason_code not in REASON_CODES:
            raise ReviewFlaggingContractError("Review flag reason_code is invalid.")
        _validate_span(flag.start_seconds, flag.end_seconds, "Review flag")
        return flag

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "segment_id": self.segment_id,
            "reason_code": self.reason_code,
            "start_seconds": self.start_seconds,
            "end_seconds": self.end_seconds,
            "source_id": self.source_id,
            "speaker_id": self.speaker_id,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class ReviewQueueArtifact:
    """Persisted `review-queue.v1` artifact."""

    version: int
    session_id: str
    merged_transcript_artifact: ArtifactRef
    policy: ConfidenceFlaggingPolicy
    flags: tuple[ReviewFlag, ...]
    created_at: str

    @classmethod
    def from_dict(cls, data: object) -> Self:
        item = _mapping(data, "Review queue artifact")
        artifact = cls(
            version=_required_int(item, "version", "Review queue artifact"),
            session_id=_required_str(item, "session_id", "Review queue artifact"),
            merged_transcript_artifact=ArtifactRef.from_dict(
                item.get("merged_transcript_artifact")
            ),
            policy=ConfidenceFlaggingPolicy.from_dict(item.get("policy")),
            flags=tuple(ReviewFlag.from_dict(flag) for flag in _list(item, "flags")),
            created_at=_required_str(item, "created_at", "Review queue artifact"),
        )
        if artifact.version != REVIEW_QUEUE_SCHEMA_VERSION:
            raise ReviewFlaggingContractError("Unsupported review queue artifact version.")
        return artifact

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "session_id": self.session_id,
            "merged_transcript_artifact": self.merged_transcript_artifact.to_dict(),
            "policy": self.policy.to_dict(),
            "flags": [flag.to_dict() for flag in self.flags],
            "created_at": self.created_at,
        }

    def to_json_bytes(self) -> bytes:
        return (json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n").encode("utf-8")


@dataclass(frozen=True)
class ConfidenceFlaggingStageResult:
    """Result of running or reusing the confidence-based flagging stage."""

    state: ProcessingStageState
    review_queue: ReviewQueueArtifact
    review_queue_record: ArtifactRecord
    skipped: bool = False
    skip_reason: str | None = None


def persist_review_queue_artifact(
    *,
    store: ArtifactStore,
    artifact: ReviewQueueArtifact,
    name: str | Path,
) -> ArtifactRecord:
    """Persist one review-queue payload through the artifact store contract."""

    ReviewQueueArtifact.from_dict(artifact.to_dict())
    return store.store(
        artifact_type=REVIEW_QUEUE_ARTIFACT_TYPE,
        name=name,
        data=artifact.to_json_bytes(),
        version=REVIEW_QUEUE_ARTIFACT_VERSION,
        metadata={
            "category": "review",
            "contract": "review-queue.v1",
            "policy_config_id": artifact.policy.config_id,
            "merged_transcript_artifact_id": artifact.merged_transcript_artifact.id,
        },
    )


def run_confidence_flagging_stage(
    *,
    store: ArtifactStore,
    ledger: ProcessingStageLedger,
    merged_transcript_record: ArtifactRecord,
    policy: ConfidenceFlaggingPolicy | None = None,
    stage: str = CONFIDENCE_FLAGGING_STAGE,
    attempt: int = 1,
    skip_completed: bool = True,
    now: datetime | None = None,
) -> ConfidenceFlaggingStageResult:
    """Generate review flags from a merged transcript artifact."""

    input_ref = ArtifactRef.from_record(merged_transcript_record)
    effective_policy = policy or ConfidenceFlaggingPolicy()

    if skip_completed:
        completed = _completed_state_for_inputs(
            store=store,
            ledger=ledger,
            stage=stage,
            input_artifacts=(input_ref,),
            policy_config_id=effective_policy.config_id,
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
                        input_artifacts=(input_ref,),
                        output_artifacts=completed.output_artifacts,
                        started_at=timestamp,
                        completed_at=timestamp,
                        error=None,
                        attempt=attempt,
                    )
                )
                return ConfidenceFlaggingStageResult(
                    state=state,
                    review_queue=artifact,
                    review_queue_record=record,
                    skipped=True,
                    skip_reason="completed_output_reused",
                )

    started = ledger.mark_running(
        stage=stage,
        input_artifacts=(input_ref,),
        attempt=attempt,
        now=now,
    )
    try:
        verification = store.verify(merged_transcript_record.id)
        if not verification.ok:
            raise ReviewFlaggingContractError(
                verification.error or "merged transcript artifact failed verification"
            )
        merged_transcript = MergedTranscriptArtifact.from_dict(
            json.loads(store.retrieve(merged_transcript_record.id))
        )
        review_queue = ReviewQueueArtifact(
            version=REVIEW_QUEUE_SCHEMA_VERSION,
            session_id=merged_transcript.session_id,
            merged_transcript_artifact=input_ref,
            policy=effective_policy,
            flags=_build_flags(merged_transcript=merged_transcript, policy=effective_policy),
            created_at=_timestamp(now),
        )
        review_record = persist_review_queue_artifact(
            store=store,
            artifact=review_queue,
            name=_review_queue_artifact_name(
                run_id=ledger.run_id,
                merged_transcript_sha256=merged_transcript_record.sha256,
                policy=effective_policy,
                attempt=attempt,
            ),
        )
    except (
        ReviewFlaggingError,
        MergeTimingContractError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as exc:
        failed = _mark_flagging_failed(
            ledger=ledger,
            stage=stage,
            input_ref=input_ref,
            attempt=attempt,
            started_at=started.started_at,
            now=now,
            message=str(exc),
            retryable=False,
            code="confidence_flagging_failed",
        )
        raise ReviewFlaggingError(failed.error.message if failed.error else str(exc)) from exc

    completed = ledger.mark_completed(
        stage=stage,
        input_artifacts=(input_ref,),
        output_artifacts=(ArtifactRef.from_record(review_record),),
        attempt=attempt,
        started_at=started.started_at,
        now=now,
    )
    return ConfidenceFlaggingStageResult(
        state=completed,
        review_queue=review_queue,
        review_queue_record=review_record,
    )


def _build_flags(
    *,
    merged_transcript: MergedTranscriptArtifact,
    policy: ConfidenceFlaggingPolicy,
) -> tuple[ReviewFlag, ...]:
    flags: list[ReviewFlag] = []
    for segment in merged_transcript.segments:
        flags.extend(_segment_flags(segment=segment, policy=policy))
    return tuple(flags)


def _segment_flags(
    *,
    segment: MergedTranscriptSegment,
    policy: ConfidenceFlaggingPolicy,
) -> tuple[ReviewFlag, ...]:
    flags: list[ReviewFlag] = []
    confidence = segment.transcription_confidence
    if confidence.status == "available" and confidence.value is not None:
        if (
            confidence.metric == "word_probability"
            and confidence.value < policy.min_word_probability
        ):
            flags.append(
                _flag(
                    segment=segment,
                    reason_code="low_transcription_confidence",
                    metadata={
                        "metric": confidence.metric,
                        "observed_value": confidence.value,
                        "threshold": policy.min_word_probability,
                        "confidence_reason": confidence.reason,
                    },
                )
            )
        if confidence.metric == "avg_logprob" and confidence.value < policy.min_avg_logprob:
            flags.append(
                _flag(
                    segment=segment,
                    reason_code="low_transcription_confidence",
                    metadata={
                        "metric": confidence.metric,
                        "observed_value": confidence.value,
                        "threshold": policy.min_avg_logprob,
                        "confidence_reason": confidence.reason,
                    },
                )
            )
    speaker_confidence = segment.speaker.confidence
    if speaker_confidence.status == "unavailable" and policy.flag_unavailable_speaker_confidence:
        flags.append(
            _flag(
                segment=segment,
                reason_code="low_speaker_confidence",
                metadata={
                    "status": speaker_confidence.status,
                    "metric": speaker_confidence.metric,
                    "observed_value": speaker_confidence.value,
                    "threshold": policy.min_speaker_overlap_ratio,
                    "confidence_reason": speaker_confidence.reason,
                },
            )
        )
    if (
        speaker_confidence.status == "available"
        and speaker_confidence.value is not None
        and speaker_confidence.metric == "temporal_overlap_ratio"
        and speaker_confidence.value < policy.min_speaker_overlap_ratio
    ):
        flags.append(
            _flag(
                segment=segment,
                reason_code="low_speaker_confidence",
                metadata={
                    "status": speaker_confidence.status,
                    "metric": speaker_confidence.metric,
                    "observed_value": speaker_confidence.value,
                    "threshold": policy.min_speaker_overlap_ratio,
                    "confidence_reason": speaker_confidence.reason,
                },
            )
        )
    if segment.overlap_detected:
        flags.append(
            _flag(
                segment=segment,
                reason_code="speaker_overlap",
                metadata={
                    "diarization_segment_ids": list(segment.diarization_segment_ids),
                    "speaker_kind": segment.speaker.kind,
                },
            )
        )
    return tuple(flags)


def _flag(
    *,
    segment: MergedTranscriptSegment,
    reason_code: str,
    metadata: Mapping[str, object],
) -> ReviewFlag:
    return ReviewFlag(
        id=f"{segment.id}:{reason_code}",
        segment_id=segment.id,
        reason_code=reason_code,
        start_seconds=segment.start_seconds,
        end_seconds=segment.end_seconds,
        source_id=segment.source_id,
        speaker_id=segment.speaker.speaker_id,
        metadata=metadata,
    )


def _completed_state_for_inputs(
    *,
    store: ArtifactStore,
    ledger: ProcessingStageLedger,
    stage: str,
    input_artifacts: tuple[ArtifactRef, ...],
    policy_config_id: str,
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
        if record.metadata.get("policy_config_id") == policy_config_id:
            return state
    return None


def _load_reusable_output(
    *,
    store: ArtifactStore,
    completed: ProcessingStageState,
) -> tuple[ArtifactRecord, ReviewQueueArtifact] | None:
    output_ref = completed.output_artifacts[0]
    try:
        record = _record_for_ref(store=store, artifact_ref=output_ref)
        artifact = ReviewQueueArtifact.from_dict(json.loads(store.retrieve(record.id)))
    except (ReviewFlaggingError, json.JSONDecodeError, KeyError, UnicodeDecodeError):
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
    raise ReviewFlaggingContractError("Review queue output ref does not match a known artifact.")


def _artifact_ref_verifies(*, store: ArtifactStore, artifact_ref: ArtifactRef) -> bool:
    try:
        record = _record_for_ref(store=store, artifact_ref=artifact_ref)
        verification = store.verify(record.id)
    except (KeyError, ReviewFlaggingContractError):
        return False
    return verification.ok


def _mark_flagging_failed(
    *,
    ledger: ProcessingStageLedger,
    stage: str,
    input_ref: ArtifactRef,
    attempt: int,
    started_at: str | None,
    now: datetime | None,
    message: str,
    retryable: bool,
    code: str,
) -> ProcessingStageState:
    return ledger.mark_failed(
        stage=stage,
        input_artifacts=(input_ref,),
        error=ProcessingErrorInfo(message=message, retryable=retryable, code=code),
        attempt=attempt,
        started_at=started_at,
        now=now,
    )


def _review_queue_artifact_name(
    *,
    run_id: str,
    merged_transcript_sha256: str,
    policy: ConfidenceFlaggingPolicy,
    attempt: int,
) -> Path:
    scope = sha256(policy.config_id.encode("utf-8")).hexdigest()[:12]
    return Path(run_id) / merged_transcript_sha256[:12] / scope / f"attempt-{attempt:03d}.json"


def _mapping(data: object, label: str) -> Mapping[str, object]:
    if not isinstance(data, Mapping):
        raise ReviewFlaggingContractError(f"{label} must be a JSON object.")
    return data


def _list(data: Mapping[str, object], field_name: str) -> list[object]:
    value = data.get(field_name)
    if not isinstance(value, list):
        raise ReviewFlaggingContractError(f"Field '{field_name}' must be an array.")
    return value


def _required_str(data: Mapping[str, object], field_name: str, label: str) -> str:
    value = data.get(field_name)
    if not isinstance(value, str) or not value:
        raise ReviewFlaggingContractError(
            f"Field '{field_name}' in {label} must be a non-empty string."
        )
    return value


def _optional_str(data: Mapping[str, object], field_name: str, label: str) -> str | None:
    value = data.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ReviewFlaggingContractError(
            f"Field '{field_name}' in {label} must be null or a non-empty string."
        )
    return value


def _required_int(data: Mapping[str, object], field_name: str, label: str) -> int:
    value = data.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReviewFlaggingContractError(f"Field '{field_name}' in {label} must be an integer.")
    return value


def _required_number(data: Mapping[str, object], field_name: str, label: str) -> float:
    value = data.get(field_name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReviewFlaggingContractError(f"Field '{field_name}' in {label} must be a number.")
    return float(value)


def _required_bool(data: Mapping[str, object], field_name: str, label: str) -> bool:
    value = data.get(field_name)
    if not isinstance(value, bool):
        raise ReviewFlaggingContractError(f"Field '{field_name}' in {label} must be a boolean.")
    return value


def _required_json_object(
    data: Mapping[str, object],
    field_name: str,
    label: str,
) -> Mapping[str, object]:
    value = data.get(field_name)
    if not isinstance(value, Mapping):
        raise ReviewFlaggingContractError(f"{label} must be a JSON object.")
    return value


def _validate_stable_id(value: str, label: str) -> None:
    if not _STABLE_ID_PATTERN.fullmatch(value):
        raise ReviewFlaggingContractError(f"{label} must be a stable identifier.")


def _validate_probability(value: float, label: str) -> None:
    if not 0.0 <= value <= 1.0:
        raise ReviewFlaggingContractError(f"{label} must be within [0, 1].")


def _validate_span(start_seconds: float, end_seconds: float, label: str) -> None:
    if start_seconds < 0 or end_seconds <= start_seconds:
        raise ReviewFlaggingContractError(
            f"{label} timestamps must be non-negative and increasing."
        )


def _timestamp(now: datetime | None) -> str:
    current = now or datetime.now(UTC)
    return current.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
