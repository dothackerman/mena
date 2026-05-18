import json
from dataclasses import replace
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from mena.artifacts import ArtifactRecord, FileSystemArtifactStore, InMemoryArtifactStore
from mena.merge_timing import (
    MERGED_TRANSCRIPT_SCHEMA_VERSION,
    MergedTranscriptArtifact,
    MergedTranscriptSegment,
    MergedTranscriptSource,
    MergePolicy,
    SegmentConfidence,
    SpeakerIdentity,
    persist_merged_transcript_artifact,
)
from mena.processing import ArtifactRef, ProcessingStageLedger
from mena.review_flagging import (
    CONFIDENCE_FLAGGING_STAGE,
    REVIEW_QUEUE_ARTIFACT_TYPE,
    REVIEW_QUEUE_SCHEMA_VERSION,
    ConfidenceFlaggingPolicy,
    ReviewFlag,
    ReviewFlaggingContractError,
    ReviewFlaggingError,
    ReviewQueueArtifact,
    persist_review_queue_artifact,
    run_confidence_flagging_stage,
)

SESSION_ID = "00000000-0000-0000-0000-000000000001"
RUN_ID = "run-20260517-review"
NOW = datetime(2026, 5, 17, 15, 0, 0, tzinfo=UTC)


def test_confidence_flagging_stage_persists_review_queue_and_completed_checkpoint(
    tmp_path: Path,
) -> None:
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    merged_record = _persist_merged_transcript(
        store=store,
        artifact=_merged_transcript_fixture(),
    )
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    result = run_confidence_flagging_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merged_record,
        now=NOW,
    )

    assert result.skipped is False
    assert result.state.status == "completed"
    assert result.review_queue_record.artifact_type == REVIEW_QUEUE_ARTIFACT_TYPE
    assert result.review_queue_record.metadata["contract"] == "review-queue.v1"
    payload = json.loads(store.retrieve(result.review_queue_record.id))
    Draft202012Validator(
        _load_review_queue_schema(),
        format_checker=FormatChecker(),
    ).validate(payload)
    artifact = ReviewQueueArtifact.from_dict(payload)
    assert artifact.version == REVIEW_QUEUE_SCHEMA_VERSION
    assert isinstance(result.review_queue.policy, ConfidenceFlaggingPolicy)
    assert isinstance(artifact.policy, ConfidenceFlaggingPolicy)
    assert {flag.reason_code for flag in artifact.flags} == {
        "low_transcription_confidence",
        "low_speaker_confidence",
        "speaker_overlap",
    }
    assert [state.status for state in ledger.states(stage=CONFIDENCE_FLAGGING_STAGE)] == [
        "running",
        "completed",
    ]


def test_confidence_flagging_stage_reuses_verified_completed_output() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    merged_record = _persist_merged_transcript(
        store=store,
        artifact=_merged_transcript_fixture(),
    )
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    first = run_confidence_flagging_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merged_record,
        now=NOW,
    )
    second = run_confidence_flagging_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merged_record,
        now=NOW,
    )

    assert second.skipped is True
    assert second.skip_reason == "completed_output_reused"
    assert second.review_queue_record == first.review_queue_record


def test_confidence_flagging_stage_reuse_is_scoped_to_policy() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    merged_record = _persist_merged_transcript(
        store=store,
        artifact=_merged_transcript_fixture(),
    )
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    run_confidence_flagging_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merged_record,
        now=NOW,
    )
    second = run_confidence_flagging_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merged_record,
        policy=ConfidenceFlaggingPolicy(min_word_probability=0.6),
        attempt=2,
        now=NOW,
    )

    assert second.skipped is False


def test_confidence_flagging_stage_fails_closed_when_reused_output_payload_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    merged_record = _persist_merged_transcript(
        store=store,
        artifact=_merged_transcript_fixture(),
    )
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)
    first = run_confidence_flagging_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merged_record,
        now=NOW,
    )
    original_retrieve = store.retrieve

    def retrieve_with_invalid_payload(artifact_id: str) -> bytes:
        if artifact_id == first.review_queue_record.id:
            return b"{not-json"
        return original_retrieve(artifact_id)

    monkeypatch.setattr(store, "retrieve", retrieve_with_invalid_payload)

    second = run_confidence_flagging_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merged_record,
        attempt=2,
        now=NOW,
    )

    assert second.skipped is False
    assert [state.status for state in ledger.states(stage=CONFIDENCE_FLAGGING_STAGE)] == [
        "running",
        "completed",
        "running",
        "completed",
    ]


def test_persist_review_queue_artifact_validates_contract_before_writing() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    merged_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/merged_transcript/merged.json",
        artifact_type="merged_transcript",
        version=1,
        sha256="0" * 64,
    )
    artifact = ReviewQueueArtifact(
        version=REVIEW_QUEUE_SCHEMA_VERSION,
        session_id=SESSION_ID,
        merged_transcript_artifact=merged_ref,
        policy=ConfidenceFlaggingPolicy(),
        flags=(
            ReviewFlag(
                id="flag_1",
                segment_id="segment_1",
                reason_code="bad_reason",
                start_seconds=0.0,
                end_seconds=1.0,
                source_id="system_audio",
                speaker_id="speaker_1",
                metadata={},
            ),
        ),
        created_at="2026-05-17T15:00:00Z",
    )

    with pytest.raises(ReviewFlaggingContractError, match="reason_code"):
        persist_review_queue_artifact(store=store, artifact=artifact, name="bad.json")

    assert store.list(artifact_type=REVIEW_QUEUE_ARTIFACT_TYPE) == ()


def test_review_queue_schema_rejects_incomplete_policy() -> None:
    payload = _review_queue_payload().to_dict()
    del cast(dict[str, object], payload["policy"])["min_word_probability"]
    validator = Draft202012Validator(
        _load_review_queue_schema(),
        format_checker=FormatChecker(),
    )

    with pytest.raises(ValidationError, match="not valid under any of the given schemas"):
        validator.validate(payload)


def test_review_queue_schema_is_valid() -> None:
    Draft202012Validator.check_schema(_load_review_queue_schema())


def test_confidence_flagging_policy_from_dict_rejects_bad_version_and_probability() -> None:
    with pytest.raises(ReviewFlaggingContractError, match="Unsupported confidence flagging"):
        ConfidenceFlaggingPolicy.from_dict(
            {
                "policy_version": 99,
                "min_word_probability": 0.85,
                "min_avg_logprob": -0.6,
                "min_speaker_overlap_ratio": 0.85,
                "flag_unavailable_speaker_confidence": True,
            }
        )

    with pytest.raises(ReviewFlaggingContractError, match="min_word_probability must be within"):
        ConfidenceFlaggingPolicy.from_dict(
            {
                "policy_version": 1,
                "min_word_probability": 1.2,
                "min_avg_logprob": -0.6,
                "min_speaker_overlap_ratio": 0.85,
                "flag_unavailable_speaker_confidence": True,
            }
        )


def test_review_flag_from_dict_rejects_invalid_reason_code_and_span() -> None:
    with pytest.raises(ReviewFlaggingContractError, match="reason_code is invalid"):
        ReviewFlag.from_dict(
            {
                "id": "flag_1",
                "segment_id": "segment_1",
                "reason_code": "bad_reason",
                "start_seconds": 0.0,
                "end_seconds": 1.0,
                "source_id": "system_audio",
                "speaker_id": "speaker_1",
                "metadata": {},
            }
        )

    with pytest.raises(ReviewFlaggingContractError, match="timestamps must be non-negative"):
        ReviewFlag.from_dict(
            {
                "id": "flag_1",
                "segment_id": "segment_1",
                "reason_code": "speaker_overlap",
                "start_seconds": 1.0,
                "end_seconds": 1.0,
                "source_id": "system_audio",
                "speaker_id": "speaker_1",
                "metadata": {},
            }
        )


def test_review_queue_artifact_from_dict_rejects_unsupported_version() -> None:
    payload = _review_queue_payload().to_dict()
    payload["version"] = 99

    with pytest.raises(ReviewFlaggingContractError, match="Unsupported review queue artifact"):
        ReviewQueueArtifact.from_dict(payload)


def test_review_queue_artifact_from_dict_rejects_invalid_semantic_policy() -> None:
    payload = _review_queue_payload().to_dict()
    payload["policy"] = {
        "policy_version": 1,
        "context_window_segments": -1,
        "min_flag_confidence": 0.8,
        "prompt_policy": "semantic-coherence-json-v1",
        "provider_config_id": "provider",
    }

    with pytest.raises(ReviewFlaggingContractError, match="must be non-negative"):
        ReviewQueueArtifact.from_dict(payload)


def test_review_queue_artifact_from_dict_rejects_unknown_policy_shape() -> None:
    payload = _review_queue_payload().to_dict()
    cast(dict[str, object], payload["policy"])["extra"] = "forbidden"

    with pytest.raises(ReviewFlaggingContractError, match="must match confidence or semantic"):
        ReviewQueueArtifact.from_dict(payload)


def test_review_queue_artifact_from_dict_rejects_unsupported_semantic_prompt_policy() -> None:
    payload = _review_queue_payload().to_dict()
    payload["policy"] = {
        "policy_version": 1,
        "context_window_segments": 1,
        "min_flag_confidence": 0.8,
        "prompt_policy": "other-prompt",
        "provider_config_id": "provider",
    }

    with pytest.raises(ReviewFlaggingContractError, match="Unsupported semantic review prompt"):
        ReviewQueueArtifact.from_dict(payload)


def test_confidence_flagging_stage_marks_failed_checkpoint_on_corrupt_input_artifact(
    tmp_path: Path,
) -> None:
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    merged_record = _persist_merged_transcript(
        store=store,
        artifact=_merged_transcript_fixture(),
    )
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)
    (tmp_path / merged_record.path).write_bytes(b"corrupt")

    with pytest.raises(ReviewFlaggingError, match="match metadata"):
        run_confidence_flagging_stage(
            store=store,
            ledger=ledger,
            merged_transcript_record=merged_record,
            now=NOW,
        )

    failed_state = ledger.states(stage=CONFIDENCE_FLAGGING_STAGE)[-1]
    assert failed_state.status == "failed"
    assert failed_state.error is not None
    assert failed_state.error.code == "confidence_flagging_failed"


def test_confidence_flagging_stage_can_skip_unavailable_speaker_confidence_flags() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    merged_record = _persist_merged_transcript(
        store=store,
        artifact=_merged_transcript_fixture(),
    )
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    result = run_confidence_flagging_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merged_record,
        policy=ConfidenceFlaggingPolicy(flag_unavailable_speaker_confidence=False),
        now=NOW,
    )

    low_speaker_flags = [
        flag for flag in result.review_queue.flags if flag.reason_code == "low_speaker_confidence"
    ]
    assert len(low_speaker_flags) == 1
    assert low_speaker_flags[0].segment_id == "merged_segment_000002"


def test_confidence_flagging_stage_flags_low_avg_logprob_segments() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    fixture = _merged_transcript_fixture()
    merged_record = _persist_merged_transcript(
        store=store,
        artifact=replace(
            fixture,
            text=f"{fixture.text} uncertain remote",
            segments=fixture.segments
            + (
                MergedTranscriptSegment(
                    id="merged_segment_000004",
                    text="uncertain remote",
                    start_seconds=1.7,
                    end_seconds=2.1,
                    source_id="system_audio",
                    channel="remote",
                    speaker=SpeakerIdentity(
                        speaker_id="speaker_3",
                        label="Remote speaker 3",
                        kind="diarized_remote",
                        confidence=SegmentConfidence(
                            status="available",
                            metric="temporal_overlap_ratio",
                            value=0.95,
                            reason="diarization_temporal_alignment",
                        ),
                    ),
                    transcription_confidence=SegmentConfidence(
                        status="available",
                        metric="avg_logprob",
                        value=-0.9,
                        reason="provider_avg_logprob",
                    ),
                    overlap_detected=False,
                    transcript_segment_id="remote_segment_3",
                    metadata={},
                ),
            ),
        ),
    )
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    result = run_confidence_flagging_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merged_record,
        now=NOW,
    )

    avg_logprob_flags = [
        flag
        for flag in result.review_queue.flags
        if flag.segment_id == "merged_segment_000004"
        and flag.reason_code == "low_transcription_confidence"
    ]
    assert len(avg_logprob_flags) == 1
    assert avg_logprob_flags[0].metadata["metric"] == "avg_logprob"
    assert avg_logprob_flags[0].metadata["threshold"] == pytest.approx(-0.6)


def _persist_merged_transcript(
    *,
    store: FileSystemArtifactStore | InMemoryArtifactStore,
    artifact: MergedTranscriptArtifact,
) -> ArtifactRecord:
    return persist_merged_transcript_artifact(store=store, artifact=artifact, name="merged.json")


def _merged_transcript_fixture() -> MergedTranscriptArtifact:
    transcript_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/transcript/segment.json",
        artifact_type="transcript",
        version=1,
        sha256="1" * 64,
    )
    return MergedTranscriptArtifact(
        version=MERGED_TRANSCRIPT_SCHEMA_VERSION,
        session_id=SESSION_ID,
        policy=MergePolicy(),
        sources=(
            MergedTranscriptSource(
                source_id="local_mic",
                channel="local",
                transcript_artifact=transcript_ref,
                diarization_status="not_applicable",
                diarization_reason="local_user_structural_attribution",
            ),
            MergedTranscriptSource(
                source_id="system_audio",
                channel="remote",
                transcript_artifact=transcript_ref,
                diarization_status="used",
                regular_diarization_artifact=ArtifactRef(
                    id=f"{SESSION_ID}:artifacts/diarization/regular.json",
                    artifact_type="diarization",
                    version=1,
                    sha256="2" * 64,
                ),
                exclusive_diarization_artifact=ArtifactRef(
                    id=f"{SESSION_ID}:artifacts/diarization/exclusive.json",
                    artifact_type="diarization",
                    version=1,
                    sha256="3" * 64,
                ),
            ),
        ),
        text="hello remote reply",
        segments=(
            MergedTranscriptSegment(
                id="merged_segment_000001",
                text="hello",
                start_seconds=0.0,
                end_seconds=0.5,
                source_id="local_mic",
                channel="local",
                speaker=SpeakerIdentity(
                    speaker_id="you",
                    label="You",
                    kind="local_user",
                    confidence=SegmentConfidence(
                        status="available",
                        metric="structural",
                        value=1.0,
                        reason="local_microphone_structural_attribution",
                    ),
                ),
                transcription_confidence=SegmentConfidence(
                    status="available",
                    metric="word_probability",
                    value=0.99,
                    reason="mean_word_probability",
                ),
                overlap_detected=False,
                transcript_segment_id="local_segment_1",
                metadata={},
            ),
            MergedTranscriptSegment(
                id="merged_segment_000002",
                text="remote reply",
                start_seconds=0.2,
                end_seconds=1.0,
                source_id="system_audio",
                channel="remote",
                speaker=SpeakerIdentity(
                    speaker_id="speaker_2",
                    label="Remote speaker 2",
                    kind="diarized_remote",
                    confidence=SegmentConfidence(
                        status="available",
                        metric="temporal_overlap_ratio",
                        value=0.55,
                        reason="diarization_temporal_alignment",
                    ),
                ),
                transcription_confidence=SegmentConfidence(
                    status="available",
                    metric="word_probability",
                    value=0.4,
                    reason="mean_word_probability",
                ),
                overlap_detected=True,
                transcript_segment_id="remote_segment_1",
                diarization_segment_ids=("turn_1", "turn_2", "exclusive_2"),
                metadata={},
            ),
            MergedTranscriptSegment(
                id="merged_segment_000003",
                text="fallback remote",
                start_seconds=1.1,
                end_seconds=1.6,
                source_id="system_audio",
                channel="remote",
                speaker=SpeakerIdentity(
                    speaker_id="remote",
                    label="Remote",
                    kind="fallback_remote",
                    confidence=SegmentConfidence(
                        status="unavailable",
                        reason="diarization_disabled",
                    ),
                ),
                transcription_confidence=SegmentConfidence(
                    status="available",
                    metric="avg_logprob",
                    value=-0.2,
                    reason="provider_avg_logprob",
                ),
                overlap_detected=False,
                transcript_segment_id="remote_segment_2",
                metadata={},
            ),
        ),
        created_at="2026-05-17T15:00:00Z",
    )


def _review_queue_payload() -> ReviewQueueArtifact:
    merged_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/merged_transcript/merged.json",
        artifact_type="merged_transcript",
        version=1,
        sha256="0" * 64,
    )
    return ReviewQueueArtifact(
        version=REVIEW_QUEUE_SCHEMA_VERSION,
        session_id=SESSION_ID,
        merged_transcript_artifact=merged_ref,
        policy=ConfidenceFlaggingPolicy(),
        flags=(),
        created_at="2026-05-17T15:00:00Z",
    )


def _load_review_queue_schema() -> dict[str, object]:
    schema_path = files("mena.schemas").joinpath("review-queue.v1.schema.json")
    return cast(dict[str, object], json.loads(schema_path.read_text(encoding="utf-8")))
