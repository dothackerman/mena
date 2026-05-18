import json
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from mena.artifacts import ArtifactRecord, FileSystemArtifactStore, InMemoryArtifactStore
from mena.diarization import (
    DIARIZATION_SCHEMA_VERSION,
    DiarizationArtifact,
    DiarizationContext,
    DiarizationProviderMetadata,
    DiarizationSegment,
    DiarizedSpeaker,
    SpeakerEmbeddingRef,
    persist_diarization_artifact,
)
from mena.merge_timing import (
    MERGE_TIMING_STAGE,
    MERGED_TRANSCRIPT_ARTIFACT_TYPE,
    MERGED_TRANSCRIPT_SCHEMA_VERSION,
    MergedTranscriptArtifact,
    MergedTranscriptSegment,
    MergedTranscriptSource,
    MergePolicy,
    MergeTimingContractError,
    MergeTimingError,
    MergeTimingInputError,
    SegmentConfidence,
    SpeakerIdentity,
    persist_merged_transcript_artifact,
    run_merge_timing_stage,
)
from mena.processing import ArtifactRef, ProcessingStageLedger
from mena.transcription import (
    TRANSCRIPT_SCHEMA_VERSION,
    TranscriptArtifact,
    TranscriptLanguage,
    TranscriptProviderMetadata,
    TranscriptSegment,
    TranscriptWord,
    persist_transcript_artifact,
)

SESSION_ID = "00000000-0000-0000-0000-000000000001"
RUN_ID = "run-20260517"
NOW = datetime(2026, 5, 17, 14, 0, 0, tzinfo=UTC)


def test_merge_timing_stage_persists_merged_artifact_and_completed_checkpoint(
    tmp_path: Path,
) -> None:
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    local_source = store.store(artifact_type="source_audio", name="local.wav", data=b"local")
    remote_source = store.store(artifact_type="source_audio", name="remote.wav", data=b"remote")
    local_transcript_record = _persist_transcript(
        store=store,
        source_record=local_source,
        source_id="local_mic",
        segments=(
            TranscriptSegment(
                id="segment_local_1",
                text="hello there",
                start_seconds=0.0,
                end_seconds=0.8,
                words=(
                    TranscriptWord(
                        text="hello",
                        start_seconds=0.0,
                        end_seconds=0.4,
                        probability=0.99,
                    ),
                    TranscriptWord(
                        text="there",
                        start_seconds=0.4,
                        end_seconds=0.8,
                        probability=0.98,
                    ),
                ),
            ),
        ),
    )
    remote_transcript_record = _persist_transcript(
        store=store,
        source_record=remote_source,
        source_id="system_audio",
        segments=(
            TranscriptSegment(
                id="segment_remote_1",
                text="remote reply",
                start_seconds=0.2,
                end_seconds=1.0,
                words=(
                    TranscriptWord(
                        text="remote",
                        start_seconds=0.2,
                        end_seconds=0.55,
                        probability=0.7,
                    ),
                    TranscriptWord(
                        text="reply",
                        start_seconds=0.55,
                        end_seconds=1.0,
                        probability=0.65,
                    ),
                ),
            ),
        ),
    )
    regular_record, exclusive_record = _persist_remote_diarization_pair(
        store=store,
        source_record=remote_source,
    )
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    result = run_merge_timing_stage(
        store=store,
        ledger=ledger,
        local_transcript_record=local_transcript_record,
        remote_transcript_record=remote_transcript_record,
        remote_regular_diarization_record=regular_record,
        remote_exclusive_diarization_record=exclusive_record,
        now=NOW,
    )

    assert result.skipped is False
    assert result.state.status == "completed"
    assert result.merged_transcript_record.artifact_type == MERGED_TRANSCRIPT_ARTIFACT_TYPE
    assert result.merged_transcript_record.metadata["contract"] == "merged-transcript.v1"
    payload = json.loads(store.retrieve(result.merged_transcript_record.id))
    Draft202012Validator(
        _load_merged_transcript_schema(),
        format_checker=FormatChecker(),
    ).validate(payload)
    artifact = MergedTranscriptArtifact.from_dict(payload)
    assert artifact.version == MERGED_TRANSCRIPT_SCHEMA_VERSION
    assert [source.diarization_status for source in artifact.sources] == [
        "not_applicable",
        "used",
    ]
    assert [segment.source_id for segment in artifact.segments] == ["local_mic", "system_audio"]
    assert artifact.segments[0].speaker.kind == "local_user"
    assert artifact.segments[1].speaker.kind == "diarized_remote"
    assert artifact.segments[1].speaker.speaker_id == "speaker_2"
    assert artifact.segments[1].speaker.confidence.metric == "temporal_overlap_ratio"
    assert artifact.segments[1].overlap_detected is True
    assert [state.status for state in ledger.states(stage=MERGE_TIMING_STAGE)] == [
        "running",
        "completed",
    ]


def test_merge_timing_stage_reuses_verified_completed_output() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    local_source = store.store(artifact_type="source_audio", name="local.wav", data=b"local")
    remote_source = store.store(artifact_type="source_audio", name="remote.wav", data=b"remote")
    local_transcript_record = _persist_transcript(
        store=store,
        source_record=local_source,
        source_id="local_mic",
    )
    remote_transcript_record = _persist_transcript(
        store=store,
        source_record=remote_source,
        source_id="system_audio",
    )
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    first = run_merge_timing_stage(
        store=store,
        ledger=ledger,
        local_transcript_record=local_transcript_record,
        remote_transcript_record=remote_transcript_record,
        remote_diarization_status="skipped",
        remote_diarization_reason="diarization_disabled",
        now=NOW,
    )
    second = run_merge_timing_stage(
        store=store,
        ledger=ledger,
        local_transcript_record=local_transcript_record,
        remote_transcript_record=remote_transcript_record,
        remote_diarization_status="skipped",
        remote_diarization_reason="diarization_disabled",
        now=NOW,
    )

    assert second.skipped is True
    assert second.skip_reason == "completed_output_reused"
    assert second.merged_transcript_record == first.merged_transcript_record


def test_merge_timing_stage_reuse_is_scoped_to_matching_policy_and_inputs() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    local_source = store.store(artifact_type="source_audio", name="local.wav", data=b"local")
    remote_source = store.store(artifact_type="source_audio", name="remote.wav", data=b"remote")
    local_transcript_record = _persist_transcript(
        store=store,
        source_record=local_source,
        source_id="local_mic",
    )
    remote_transcript_record = _persist_transcript(
        store=store,
        source_record=remote_source,
        source_id="system_audio",
    )
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    run_merge_timing_stage(
        store=store,
        ledger=ledger,
        local_transcript_record=local_transcript_record,
        remote_transcript_record=remote_transcript_record,
        remote_diarization_status="skipped",
        remote_diarization_reason="diarization_disabled",
        now=NOW,
    )
    changed_policy = MergePolicy(remote_fallback_speaker_label="Remote participant")
    second = run_merge_timing_stage(
        store=store,
        ledger=ledger,
        local_transcript_record=local_transcript_record,
        remote_transcript_record=remote_transcript_record,
        remote_diarization_status="skipped",
        remote_diarization_reason="diarization_disabled",
        policy=changed_policy,
        attempt=2,
        now=NOW,
    )
    other_remote_source = store.store(
        artifact_type="source_audio", name="remote-2.wav", data=b"remote-2"
    )
    other_remote_transcript = _persist_transcript(
        store=store,
        source_record=other_remote_source,
        source_id="system_audio",
    )
    third = run_merge_timing_stage(
        store=store,
        ledger=ledger,
        local_transcript_record=local_transcript_record,
        remote_transcript_record=other_remote_transcript,
        remote_diarization_status="skipped",
        remote_diarization_reason="diarization_disabled",
        policy=changed_policy,
        attempt=3,
        now=NOW,
    )

    assert second.skipped is False
    assert third.skipped is False


def test_merge_timing_stage_reuses_inferred_policy_for_custom_source_ids() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    local_source = store.store(artifact_type="source_audio", name="host.wav", data=b"host")
    remote_source = store.store(artifact_type="source_audio", name="call.wav", data=b"call")
    local_transcript_record = _persist_transcript(
        store=store,
        source_record=local_source,
        source_id="host_mic",
    )
    remote_transcript_record = _persist_transcript(
        store=store,
        source_record=remote_source,
        source_id="call_audio",
    )
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    first = run_merge_timing_stage(
        store=store,
        ledger=ledger,
        local_transcript_record=local_transcript_record,
        remote_transcript_record=remote_transcript_record,
        remote_diarization_status="skipped",
        remote_diarization_reason="diarization_disabled",
        now=NOW,
    )
    second = run_merge_timing_stage(
        store=store,
        ledger=ledger,
        local_transcript_record=local_transcript_record,
        remote_transcript_record=remote_transcript_record,
        remote_diarization_status="skipped",
        remote_diarization_reason="diarization_disabled",
        now=NOW,
    )

    assert first.merged_transcript.policy.local_source_id == "host_mic"
    assert first.merged_transcript.policy.remote_source_id == "call_audio"
    assert second.skipped is True
    assert second.merged_transcript_record == first.merged_transcript_record


def test_merge_timing_stage_records_explicit_skipped_diarization_status() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    local_source = store.store(artifact_type="source_audio", name="local.wav", data=b"local")
    remote_source = store.store(artifact_type="source_audio", name="remote.wav", data=b"remote")
    local_transcript_record = _persist_transcript(
        store=store,
        source_record=local_source,
        source_id="local_mic",
    )
    remote_transcript_record = _persist_transcript(
        store=store,
        source_record=remote_source,
        source_id="system_audio",
    )
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    result = run_merge_timing_stage(
        store=store,
        ledger=ledger,
        local_transcript_record=local_transcript_record,
        remote_transcript_record=remote_transcript_record,
        remote_diarization_status="skipped",
        remote_diarization_reason="diarization_disabled",
        now=NOW,
    )

    remote_source_info = next(
        source for source in result.merged_transcript.sources if source.channel == "remote"
    )
    remote_segment = next(
        segment for segment in result.merged_transcript.segments if segment.channel == "remote"
    )
    assert remote_source_info.diarization_status == "skipped"
    assert remote_source_info.diarization_reason == "diarization_disabled"
    assert remote_segment.speaker.kind == "fallback_remote"
    assert remote_segment.speaker.confidence.status == "unavailable"


def test_merge_timing_stage_fails_closed_when_reused_output_payload_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    local_source = store.store(artifact_type="source_audio", name="local.wav", data=b"local")
    remote_source = store.store(artifact_type="source_audio", name="remote.wav", data=b"remote")
    local_transcript_record = _persist_transcript(
        store=store,
        source_record=local_source,
        source_id="local_mic",
    )
    remote_transcript_record = _persist_transcript(
        store=store,
        source_record=remote_source,
        source_id="system_audio",
    )
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)
    first = run_merge_timing_stage(
        store=store,
        ledger=ledger,
        local_transcript_record=local_transcript_record,
        remote_transcript_record=remote_transcript_record,
        remote_diarization_status="skipped",
        remote_diarization_reason="diarization_disabled",
        now=NOW,
    )
    original_retrieve = store.retrieve

    def retrieve_with_invalid_payload(artifact_id: str) -> bytes:
        if artifact_id == first.merged_transcript_record.id:
            return b"{not-json"
        return original_retrieve(artifact_id)

    monkeypatch.setattr(store, "retrieve", retrieve_with_invalid_payload)

    second = run_merge_timing_stage(
        store=store,
        ledger=ledger,
        local_transcript_record=local_transcript_record,
        remote_transcript_record=remote_transcript_record,
        remote_diarization_status="skipped",
        remote_diarization_reason="diarization_disabled",
        attempt=2,
        now=NOW,
    )

    assert second.skipped is False
    assert [state.status for state in ledger.states(stage=MERGE_TIMING_STAGE)] == [
        "running",
        "completed",
        "running",
        "completed",
    ]


def test_persist_merged_transcript_artifact_validates_contract_before_writing() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    source = store.store(artifact_type="source_audio", name="local.wav", data=b"local")
    transcript_record = _persist_transcript(
        store=store,
        source_record=source,
        source_id="local_mic",
    )
    artifact = MergedTranscriptArtifact(
        version=MERGED_TRANSCRIPT_SCHEMA_VERSION,
        session_id=SESSION_ID,
        policy=MergePolicy(),
        sources=(
            _source_from_transcript_record(
                transcript_record=transcript_record,
                source_id="local_mic",
                channel="local",
                diarization_status="not_applicable",
                diarization_reason="local_user_structural_attribution",
            ),
            _source_from_transcript_record(
                transcript_record=transcript_record,
                source_id="system_audio",
                channel="remote",
                diarization_status="skipped",
                diarization_reason="diarization_disabled",
            ),
        ),
        text="bad",
        segments=(),
        created_at="2026-05-17T14:00:00Z",
    )

    with pytest.raises(MergeTimingContractError, match="at least one segment"):
        persist_merged_transcript_artifact(store=store, artifact=artifact, name="bad.json")

    assert store.list(artifact_type=MERGED_TRANSCRIPT_ARTIFACT_TYPE) == ()


def test_merged_transcript_schema_rejects_incomplete_policy() -> None:
    payload = _merged_transcript_payload().to_dict()
    del cast(dict[str, object], payload["policy"])["local_source_id"]
    validator = Draft202012Validator(
        _load_merged_transcript_schema(),
        format_checker=FormatChecker(),
    )

    with pytest.raises(ValidationError, match="'local_source_id' is a required property"):
        validator.validate(payload)


def test_merged_transcript_schema_is_valid() -> None:
    Draft202012Validator.check_schema(_load_merged_transcript_schema())


def test_segment_confidence_from_dict_validates_status_and_required_fields() -> None:
    with pytest.raises(MergeTimingContractError, match="status is invalid"):
        SegmentConfidence.from_dict({"status": "bad"}, label="Speaker confidence")

    with pytest.raises(
        MergeTimingContractError,
        match="available confidence must include metric and value",
    ):
        SegmentConfidence.from_dict({"status": "available"}, label="Speaker confidence")

    with pytest.raises(
        MergeTimingContractError,
        match="unavailable confidence must include a reason",
    ):
        SegmentConfidence.from_dict({"status": "unavailable"}, label="Speaker confidence")


def test_speaker_identity_from_dict_rejects_invalid_kind() -> None:
    with pytest.raises(MergeTimingContractError, match="kind is invalid"):
        SpeakerIdentity.from_dict(
            {
                "speaker_id": "speaker_1",
                "label": "Speaker 1",
                "kind": "mystery",
                "confidence": {
                    "status": "available",
                    "metric": "structural",
                    "value": 1.0,
                    "reason": "fixture",
                },
            }
        )


def test_merged_transcript_source_from_dict_validates_channel_status_and_used_refs() -> None:
    payload = _merged_transcript_payload().to_dict()
    sources = cast(list[object], payload["sources"])
    remote_source = cast(dict[str, object], sources[1])

    invalid_channel = dict(remote_source)
    invalid_channel["channel"] = "side"
    with pytest.raises(MergeTimingContractError, match="channel is invalid"):
        MergedTranscriptSource.from_dict(invalid_channel)

    invalid_status = dict(remote_source)
    invalid_status["diarization_status"] = "mystery"
    with pytest.raises(MergeTimingContractError, match="diarization_status is invalid"):
        MergedTranscriptSource.from_dict(invalid_status)

    used_without_refs = dict(remote_source)
    used_without_refs["diarization_status"] = "used"
    used_without_refs["diarization_reason"] = None
    used_without_refs["regular_diarization_artifact"] = None
    used_without_refs["exclusive_diarization_artifact"] = None
    with pytest.raises(
        MergeTimingContractError,
        match="regular and exclusive artifacts",
    ):
        MergedTranscriptSource.from_dict(used_without_refs)


def test_merged_transcript_segment_from_dict_rejects_invalid_span() -> None:
    payload = _merged_transcript_payload().to_dict()
    segments = cast(list[object], payload["segments"])
    segment = cast(dict[str, object], segments[0])
    segment["end_seconds"] = 0.0

    with pytest.raises(MergeTimingContractError, match="timestamps must be non-negative"):
        MergedTranscriptSegment.from_dict(segment)


def test_merged_transcript_artifact_from_dict_rejects_unsupported_version_and_source_mismatch() -> (
    None
):
    payload = _merged_transcript_payload().to_dict()
    invalid_version = dict(payload)
    invalid_version["version"] = 99
    with pytest.raises(MergeTimingContractError, match="Unsupported merged transcript artifact"):
        MergedTranscriptArtifact.from_dict(invalid_version)

    mismatched_sources = _merged_transcript_payload().to_dict()
    cast(dict[str, object], mismatched_sources["policy"])["remote_source_id"] = "other_remote"
    with pytest.raises(MergeTimingContractError, match="must match the merge policy source ids"):
        MergedTranscriptArtifact.from_dict(mismatched_sources)


def test_merge_timing_stage_requires_regular_and_exclusive_diarization_together() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    local_source = store.store(artifact_type="source_audio", name="local.wav", data=b"local")
    remote_source = store.store(artifact_type="source_audio", name="remote.wav", data=b"remote")
    local_transcript_record = _persist_transcript(
        store=store,
        source_record=local_source,
        source_id="local_mic",
    )
    remote_transcript_record = _persist_transcript(
        store=store,
        source_record=remote_source,
        source_id="system_audio",
    )
    regular_record, _ = _persist_remote_diarization_pair(
        store=store,
        source_record=remote_source,
    )
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    with pytest.raises(MergeTimingInputError, match="both regular and exclusive"):
        run_merge_timing_stage(
            store=store,
            ledger=ledger,
            local_transcript_record=local_transcript_record,
            remote_transcript_record=remote_transcript_record,
            remote_regular_diarization_record=regular_record,
            now=NOW,
        )


def test_merge_timing_stage_rejects_invalid_remote_diarization_status() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    local_source = store.store(artifact_type="source_audio", name="local.wav", data=b"local")
    remote_source = store.store(artifact_type="source_audio", name="remote.wav", data=b"remote")
    local_transcript_record = _persist_transcript(
        store=store,
        source_record=local_source,
        source_id="local_mic",
    )
    remote_transcript_record = _persist_transcript(
        store=store,
        source_record=remote_source,
        source_id="system_audio",
    )
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    with pytest.raises(MergeTimingInputError, match="status is invalid"):
        run_merge_timing_stage(
            store=store,
            ledger=ledger,
            local_transcript_record=local_transcript_record,
            remote_transcript_record=remote_transcript_record,
            remote_diarization_status="bad",
            now=NOW,
        )


def test_merge_timing_stage_marks_failed_checkpoint_on_policy_mismatch() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    local_source = store.store(artifact_type="source_audio", name="local.wav", data=b"local")
    remote_source = store.store(artifact_type="source_audio", name="remote.wav", data=b"remote")
    local_transcript_record = _persist_transcript(
        store=store,
        source_record=local_source,
        source_id="local_mic",
    )
    remote_transcript_record = _persist_transcript(
        store=store,
        source_record=remote_source,
        source_id="system_audio",
    )
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    with pytest.raises(MergeTimingError, match="Local transcript source_id must match"):
        run_merge_timing_stage(
            store=store,
            ledger=ledger,
            local_transcript_record=local_transcript_record,
            remote_transcript_record=remote_transcript_record,
            policy=MergePolicy(local_source_id="wrong_local"),
            now=NOW,
        )

    failed_state = ledger.states(stage=MERGE_TIMING_STAGE)[-1]
    assert failed_state.status == "failed"
    assert failed_state.error is not None
    assert failed_state.error.code == "merge_timing_failed"


def test_merge_timing_stage_marks_failed_checkpoint_on_corrupt_input_artifact(
    tmp_path: Path,
) -> None:
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    local_source = store.store(artifact_type="source_audio", name="local.wav", data=b"local")
    remote_source = store.store(artifact_type="source_audio", name="remote.wav", data=b"remote")
    local_transcript_record = _persist_transcript(
        store=store,
        source_record=local_source,
        source_id="local_mic",
    )
    remote_transcript_record = _persist_transcript(
        store=store,
        source_record=remote_source,
        source_id="system_audio",
    )
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)
    (tmp_path / remote_transcript_record.path).write_bytes(b"corrupt")

    with pytest.raises(MergeTimingError, match="match metadata"):
        run_merge_timing_stage(
            store=store,
            ledger=ledger,
            local_transcript_record=local_transcript_record,
            remote_transcript_record=remote_transcript_record,
            remote_diarization_status="skipped",
            remote_diarization_reason="diarization_disabled",
            now=NOW,
        )

    failed_state = ledger.states(stage=MERGE_TIMING_STAGE)[-1]
    assert failed_state.status == "failed"
    assert failed_state.error is not None
    assert failed_state.error.code == "merge_timing_failed"


def test_merge_timing_stage_handles_avg_logprob_and_missing_exclusive_diarization_match() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    local_source = store.store(artifact_type="source_audio", name="local.wav", data=b"local")
    remote_source = store.store(artifact_type="source_audio", name="remote.wav", data=b"remote")
    local_transcript_record = _persist_transcript(
        store=store,
        source_record=local_source,
        source_id="local_mic",
    )
    remote_transcript_record = _persist_transcript(
        store=store,
        source_record=remote_source,
        source_id="system_audio",
        segments=(
            TranscriptSegment(
                id="remote_segment_avg_logprob",
                text="first remote",
                start_seconds=0.2,
                end_seconds=0.7,
                avg_logprob=-0.45,
                words=(
                    TranscriptWord(
                        text="first",
                        start_seconds=0.2,
                        end_seconds=0.45,
                        probability=None,
                    ),
                    TranscriptWord(
                        text="remote",
                        start_seconds=0.45,
                        end_seconds=0.7,
                        probability=None,
                    ),
                ),
            ),
            TranscriptSegment(
                id="remote_segment_no_match",
                text="second remote",
                start_seconds=1.1,
                end_seconds=1.5,
                words=(
                    TranscriptWord(
                        text="second",
                        start_seconds=1.1,
                        end_seconds=1.3,
                        probability=None,
                    ),
                    TranscriptWord(
                        text="remote",
                        start_seconds=1.3,
                        end_seconds=1.5,
                        probability=None,
                    ),
                ),
            ),
        ),
    )
    source_ref = ArtifactRef.from_record(remote_source)
    speakers = (
        DiarizedSpeaker(
            id="speaker_1",
            label="Remote speaker 1",
            embedding=SpeakerEmbeddingRef.unavailable("fixture"),
        ),
    )
    regular_record = persist_diarization_artifact(
        store=store,
        artifact=DiarizationArtifact(
            version=DIARIZATION_SCHEMA_VERSION,
            kind="regular_timeline",
            session_id=SESSION_ID,
            source_artifact=source_ref,
            source_id="system_audio",
            context=DiarizationContext(
                capture_profile="online_dual_stream",
                source_id="system_audio",
                remote_speaker_count=1,
            ),
            provider=DiarizationProviderMetadata(
                name="fake",
                checkpoint="fixture",
                device="test",
                parameters={},
                metadata={},
            ),
            speakers=speakers,
            segments=(
                DiarizationSegment(
                    id="regular_1",
                    speaker_id="speaker_1",
                    start_seconds=0.2,
                    end_seconds=0.7,
                    overlapped=False,
                ),
                DiarizationSegment(
                    id="regular_2",
                    speaker_id="speaker_1",
                    start_seconds=1.1,
                    end_seconds=1.5,
                    overlapped=False,
                ),
            ),
            base_artifacts=(),
            overlay_entries=(),
            created_at="2026-05-17T14:00:00Z",
        ),
        name="regular-custom.json",
    )
    exclusive_record = persist_diarization_artifact(
        store=store,
        artifact=DiarizationArtifact(
            version=DIARIZATION_SCHEMA_VERSION,
            kind="exclusive_projection",
            session_id=SESSION_ID,
            source_artifact=source_ref,
            source_id="system_audio",
            context=DiarizationContext(
                capture_profile="online_dual_stream",
                source_id="system_audio",
                remote_speaker_count=1,
            ),
            provider=DiarizationProviderMetadata(
                name="fake",
                checkpoint="fixture",
                device="test",
                parameters={},
                metadata={},
            ),
            speakers=speakers,
            segments=(
                DiarizationSegment(
                    id="exclusive_1",
                    speaker_id="speaker_1",
                    start_seconds=0.2,
                    end_seconds=0.7,
                    overlapped=False,
                ),
            ),
            base_artifacts=(ArtifactRef.from_record(regular_record),),
            overlay_entries=(),
            created_at="2026-05-17T14:00:00Z",
        ),
        name="exclusive-custom.json",
    )
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    result = run_merge_timing_stage(
        store=store,
        ledger=ledger,
        local_transcript_record=local_transcript_record,
        remote_transcript_record=remote_transcript_record,
        remote_regular_diarization_record=regular_record,
        remote_exclusive_diarization_record=exclusive_record,
        now=NOW,
    )

    avg_logprob_segment = next(
        segment
        for segment in result.merged_transcript.segments
        if segment.transcript_segment_id == "remote_segment_avg_logprob"
    )
    no_match_segment = next(
        segment
        for segment in result.merged_transcript.segments
        if segment.transcript_segment_id == "remote_segment_no_match"
    )
    assert avg_logprob_segment.transcription_confidence.metric == "avg_logprob"
    assert avg_logprob_segment.transcription_confidence.value == pytest.approx(-0.45)
    assert no_match_segment.speaker.kind == "fallback_remote"
    assert no_match_segment.speaker.confidence.reason == "no_matching_diarization_segment"
    assert no_match_segment.transcription_confidence.status == "unavailable"
    assert no_match_segment.overlap_detected is False


def test_merge_timing_stage_marks_cross_stream_overlap_after_chronological_sort() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    local_source = store.store(artifact_type="source_audio", name="local.wav", data=b"local")
    remote_source = store.store(artifact_type="source_audio", name="remote.wav", data=b"remote")
    local_transcript_record = _persist_transcript(
        store=store,
        source_record=local_source,
        source_id="local_mic",
        segments=(
            TranscriptSegment(
                id="local_early",
                text="early local",
                start_seconds=0.0,
                end_seconds=0.6,
                words=(),
            ),
            TranscriptSegment(
                id="local_later",
                text="later local",
                start_seconds=1.0,
                end_seconds=1.5,
                words=(),
            ),
        ),
    )
    remote_transcript_record = _persist_transcript(
        store=store,
        source_record=remote_source,
        source_id="system_audio",
        segments=(
            TranscriptSegment(
                id="remote_between",
                text="between remote",
                start_seconds=0.2,
                end_seconds=0.4,
                words=(),
            ),
        ),
    )
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    result = run_merge_timing_stage(
        store=store,
        ledger=ledger,
        local_transcript_record=local_transcript_record,
        remote_transcript_record=remote_transcript_record,
        remote_diarization_status="skipped",
        remote_diarization_reason="diarization_disabled",
        now=NOW,
    )

    segments = {
        segment.transcript_segment_id: segment for segment in result.merged_transcript.segments
    }
    assert segments["local_early"].overlap_detected is True
    assert segments["remote_between"].overlap_detected is True
    assert segments["local_later"].overlap_detected is False


def test_merge_timing_stage_rejects_transcripts_from_different_sessions() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    local_source = store.store(artifact_type="source_audio", name="local.wav", data=b"local")
    remote_source = store.store(artifact_type="source_audio", name="remote.wav", data=b"remote")
    local_transcript_record = _persist_transcript(
        store=store,
        source_record=local_source,
        source_id="local_mic",
    )
    remote_transcript_record = _persist_transcript(
        store=store,
        source_record=remote_source,
        source_id="system_audio",
        session_id="00000000-0000-0000-0000-000000000099",
    )
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    with pytest.raises(MergeTimingError, match="Remote transcript session_id"):
        run_merge_timing_stage(
            store=store,
            ledger=ledger,
            local_transcript_record=local_transcript_record,
            remote_transcript_record=remote_transcript_record,
            remote_diarization_status="skipped",
            remote_diarization_reason="diarization_disabled",
            now=NOW,
        )


def _persist_transcript(
    *,
    store: FileSystemArtifactStore | InMemoryArtifactStore,
    source_record: ArtifactRecord,
    source_id: str,
    segments: tuple[TranscriptSegment, ...] | None = None,
    session_id: str = SESSION_ID,
) -> ArtifactRecord:
    transcript = TranscriptArtifact(
        version=TRANSCRIPT_SCHEMA_VERSION,
        session_id=session_id,
        source_artifact=ArtifactRef.from_record(source_record),
        source_id=source_id,
        provider=TranscriptProviderMetadata(
            name="fake",
            checkpoint="fixture",
            quantization="none",
            device="test",
            timestamp_mode="word",
            decode_parameters={},
            metadata={},
        ),
        language=TranscriptLanguage(requested="en", detected="en", detection_probability=1.0),
        text=" ".join(segment.text for segment in (segments or _default_segments())).strip(),
        segments=segments or _default_segments(),
        created_at="2026-05-17T14:00:00Z",
    )
    return persist_transcript_artifact(
        store=store,
        transcript=transcript,
        name=f"{source_id}-{source_record.sha256[:8]}.json",
    )


def _persist_remote_diarization_pair(
    *,
    store: FileSystemArtifactStore | InMemoryArtifactStore,
    source_record: ArtifactRecord,
) -> tuple[ArtifactRecord, ArtifactRecord]:
    source_ref = ArtifactRef.from_record(source_record)
    speakers = (
        DiarizedSpeaker(
            id="speaker_1",
            label="Remote speaker 1",
            embedding=SpeakerEmbeddingRef.unavailable("fixture"),
        ),
        DiarizedSpeaker(
            id="speaker_2",
            label="Remote speaker 2",
            embedding=SpeakerEmbeddingRef.unavailable("fixture"),
        ),
    )
    regular = DiarizationArtifact(
        version=DIARIZATION_SCHEMA_VERSION,
        kind="regular_timeline",
        session_id=SESSION_ID,
        source_artifact=source_ref,
        source_id="system_audio",
        context=DiarizationContext(
            capture_profile="online_dual_stream",
            source_id="system_audio",
            remote_speaker_count=2,
        ),
        provider=DiarizationProviderMetadata(
            name="fake",
            checkpoint="fixture",
            device="test",
            parameters={},
            metadata={},
        ),
        speakers=speakers,
        segments=(
            DiarizationSegment(
                id="turn_1",
                speaker_id="speaker_1",
                start_seconds=0.2,
                end_seconds=0.6,
                overlapped=False,
            ),
            DiarizationSegment(
                id="turn_2",
                speaker_id="speaker_2",
                start_seconds=0.5,
                end_seconds=1.0,
                overlapped=True,
            ),
        ),
        base_artifacts=(),
        overlay_entries=(),
        created_at="2026-05-17T14:00:00Z",
    )
    regular_record = persist_diarization_artifact(
        store=store,
        artifact=regular,
        name="regular.json",
    )
    exclusive = DiarizationArtifact(
        version=DIARIZATION_SCHEMA_VERSION,
        kind="exclusive_projection",
        session_id=SESSION_ID,
        source_artifact=source_ref,
        source_id="system_audio",
        context=regular.context,
        provider=regular.provider,
        speakers=speakers,
        segments=(
            DiarizationSegment(
                id="exclusive_1",
                speaker_id="speaker_1",
                start_seconds=0.2,
                end_seconds=0.55,
                overlapped=False,
            ),
            DiarizationSegment(
                id="exclusive_2",
                speaker_id="speaker_2",
                start_seconds=0.55,
                end_seconds=1.0,
                overlapped=True,
            ),
        ),
        base_artifacts=(ArtifactRef.from_record(regular_record),),
        overlay_entries=(),
        created_at="2026-05-17T14:00:00Z",
    )
    exclusive_record = persist_diarization_artifact(
        store=store,
        artifact=exclusive,
        name="exclusive.json",
    )
    return regular_record, exclusive_record


def _source_from_transcript_record(
    *,
    transcript_record: ArtifactRecord,
    source_id: str,
    channel: str,
    diarization_status: str,
    diarization_reason: str,
) -> MergedTranscriptSource:
    return MergedTranscriptSource(
        source_id=source_id,
        channel=channel,
        transcript_artifact=ArtifactRef.from_record(transcript_record),
        diarization_status=diarization_status,
        diarization_reason=diarization_reason,
    )


def _merged_transcript_payload() -> MergedTranscriptArtifact:
    source_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/transcript/local.json",
        artifact_type="transcript",
        version=1,
        sha256="0" * 64,
    )
    from mena.merge_timing import MergedTranscriptSegment, SegmentConfidence, SpeakerIdentity

    return MergedTranscriptArtifact(
        version=MERGED_TRANSCRIPT_SCHEMA_VERSION,
        session_id=SESSION_ID,
        policy=MergePolicy(),
        sources=(
            _source_from_transcript_record_for_schema(
                source_ref=source_ref,
                source_id="local_mic",
                channel="local",
                diarization_status="not_applicable",
                diarization_reason="local_user_structural_attribution",
            ),
            _source_from_transcript_record_for_schema(
                source_ref=source_ref,
                source_id="system_audio",
                channel="remote",
                diarization_status="skipped",
                diarization_reason="diarization_disabled",
            ),
        ),
        text="hello",
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
                transcript_segment_id="segment_local_1",
                metadata={},
            ),
        ),
        created_at="2026-05-17T14:00:00Z",
    )


def _source_from_transcript_record_for_schema(
    *,
    source_ref: ArtifactRef,
    source_id: str,
    channel: str,
    diarization_status: str,
    diarization_reason: str,
) -> MergedTranscriptSource:
    return MergedTranscriptSource(
        source_id=source_id,
        channel=channel,
        transcript_artifact=source_ref,
        diarization_status=diarization_status,
        diarization_reason=diarization_reason,
    )


def _default_segments() -> tuple[TranscriptSegment, ...]:
    return (
        TranscriptSegment(
            id="segment_1",
            text="hello",
            start_seconds=0.0,
            end_seconds=0.6,
            avg_logprob=-0.05,
            words=(
                TranscriptWord(
                    text="hello",
                    start_seconds=0.0,
                    end_seconds=0.6,
                    probability=0.99,
                ),
            ),
        ),
    )


def _load_merged_transcript_schema() -> dict[str, object]:
    schema_path = files("mena.schemas").joinpath("merged-transcript.v1.schema.json")
    return cast(dict[str, object], json.loads(schema_path.read_text(encoding="utf-8")))
