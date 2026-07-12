import hashlib
import json
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import TypedDict, cast

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
    SourceDiarizationInput,
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
ONLINE_DUAL_STREAM_MERGED_TRANSCRIPT_GOLDEN_SHA256 = (
    "0fa7e3da930ff2114bf9db0e6618557579d02c651c2093e681c25c3301df4790"
)
IN_PERSON_SINGLE_MIC_MERGED_TRANSCRIPT_GOLDEN_SHA256 = (
    "265d5eaf4a6fdb30c642aafe2a684377d4bcae1f6333f8e9270f1d8d010d11e8"
)
ONLINE_DUAL_STREAM_MERGED_TRANSCRIPT_PATH = (
    "artifacts/merged_transcript/run-20260517/5b617fd6-1a65c54d/17cb058d1294/attempt-001.json"
)
IN_PERSON_SINGLE_MIC_MERGED_TRANSCRIPT_PATH = (
    "artifacts/merged_transcript/run-20260517/32985f34/1048b003c403/attempt-001.json"
)
ONLINE_DUAL_STREAM_POLICY_CONFIG_ID = (
    '{"capture_profile":"online_dual_stream","local_source_id":"local_mic",'
    '"local_speaker_id":"you","local_speaker_label":"You","policy_version":1,'
    '"remote_fallback_speaker_id":"remote","remote_fallback_speaker_label":"Remote",'
    '"remote_source_id":"system_audio","room_fallback_speaker_id":"room",'
    '"room_fallback_speaker_label":"Room","room_source_id":"room_mic",'
    '"sort_order":"start_seconds,end_seconds,channel,source_id,transcript_segment_id"}'
)
IN_PERSON_SINGLE_MIC_POLICY_CONFIG_ID = (
    '{"capture_profile":"in_person_single_mic","local_source_id":"local_mic",'
    '"local_speaker_id":"you","local_speaker_label":"You","policy_version":1,'
    '"remote_fallback_speaker_id":"remote","remote_fallback_speaker_label":"Remote",'
    '"remote_source_id":"system_audio","room_fallback_speaker_id":"room",'
    '"room_fallback_speaker_label":"Room","room_source_id":"room_mic",'
    '"sort_order":"start_seconds,end_seconds,channel,source_id,transcript_segment_id"}'
)


class _MergeStageInputs(TypedDict):
    policy: MergePolicy
    transcript_records: dict[str, ArtifactRecord]
    diarization_inputs: dict[str, SourceDiarizationInput]


def _online_stage_inputs(
    *,
    policy: MergePolicy,
    local_transcript_record: ArtifactRecord,
    remote_transcript_record: ArtifactRecord,
    remote_diarization_input: SourceDiarizationInput | None = None,
) -> _MergeStageInputs:
    diarization_inputs = (
        {}
        if remote_diarization_input is None
        else {policy.remote_source_id: remote_diarization_input}
    )
    return {
        "policy": policy,
        "transcript_records": {
            policy.local_source_id: local_transcript_record,
            policy.remote_source_id: remote_transcript_record,
        },
        "diarization_inputs": diarization_inputs,
    }


def _single_source_stage_inputs(
    *,
    policy: MergePolicy,
    room_transcript_record: ArtifactRecord,
    room_diarization_input: SourceDiarizationInput | None = None,
) -> _MergeStageInputs:
    diarization_inputs = (
        {} if room_diarization_input is None else {policy.room_source_id: room_diarization_input}
    )
    return {
        "policy": policy,
        "transcript_records": {policy.room_source_id: room_transcript_record},
        "diarization_inputs": diarization_inputs,
    }


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
    policy = MergePolicy()

    result = run_merge_timing_stage(
        store=store,
        ledger=ledger,
        **_online_stage_inputs(
            policy=policy,
            local_transcript_record=local_transcript_record,
            remote_transcript_record=remote_transcript_record,
            remote_diarization_input=SourceDiarizationInput(
                regular_record=regular_record,
                exclusive_record=exclusive_record,
            ),
        ),
        now=NOW,
    )

    assert result.skipped is False
    assert result.state.status == "completed"
    assert result.merged_transcript_record.artifact_type == MERGED_TRANSCRIPT_ARTIFACT_TYPE
    assert result.merged_transcript_record.path == ONLINE_DUAL_STREAM_MERGED_TRANSCRIPT_PATH
    assert result.merged_transcript_record.metadata["contract"] == "merged-transcript.v1"
    payload_bytes = store.retrieve(result.merged_transcript_record.id)
    assert (
        hashlib.sha256(payload_bytes).hexdigest()
        == ONLINE_DUAL_STREAM_MERGED_TRANSCRIPT_GOLDEN_SHA256
    )
    payload = json.loads(payload_bytes)
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


def test_merge_timing_stage_persists_single_source_room_artifact(
    tmp_path: Path,
) -> None:
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    room_source = store.store(artifact_type="source_audio", name="room.wav", data=b"room")
    room_transcript_record = _persist_transcript(
        store=store,
        source_record=room_source,
        source_id="room_mic",
    )
    regular_record, exclusive_record = _persist_remote_diarization_pair(
        store=store,
        source_record=room_source,
        source_id="room_mic",
        capture_profile="in_person_single_mic",
        speaker_label_prefix="Room speaker",
        name_prefix="room-",
    )
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)
    policy = MergePolicy(capture_profile="in_person_single_mic")

    result = run_merge_timing_stage(
        store=store,
        ledger=ledger,
        **_single_source_stage_inputs(
            policy=policy,
            room_transcript_record=room_transcript_record,
            room_diarization_input=SourceDiarizationInput(
                regular_record=regular_record,
                exclusive_record=exclusive_record,
            ),
        ),
        now=NOW,
    )

    assert result.skipped is False
    assert result.state.status == "completed"
    assert result.merged_transcript_record.path == IN_PERSON_SINGLE_MIC_MERGED_TRANSCRIPT_PATH
    assert result.merged_transcript_record.metadata["room_source_id"] == "room_mic"
    assert result.merged_transcript_record.metadata["room_diarization_status"] == "used"
    payload_bytes = store.retrieve(result.merged_transcript_record.id)
    assert (
        hashlib.sha256(payload_bytes).hexdigest()
        == IN_PERSON_SINGLE_MIC_MERGED_TRANSCRIPT_GOLDEN_SHA256
    )
    payload = json.loads(payload_bytes)
    Draft202012Validator(
        _load_merged_transcript_schema(),
        format_checker=FormatChecker(),
    ).validate(payload)
    artifact = MergedTranscriptArtifact.from_dict(payload)
    assert artifact.policy.capture_profile == "in_person_single_mic"
    assert [(source.source_id, source.channel) for source in artifact.sources] == [
        ("room_mic", "room")
    ]
    assert artifact.sources[0].diarization_status == "used"
    assert [segment.source_id for segment in artifact.segments] == ["room_mic"]
    assert artifact.segments[0].channel == "room"
    assert artifact.segments[0].speaker.kind == "diarized_room"
    assert artifact.segments[0].speaker.label == "Room speaker 1"


def test_merge_timing_stage_rejects_single_source_exclusive_capture_profile_mismatch(
    tmp_path: Path,
) -> None:
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    room_source = store.store(artifact_type="source_audio", name="room.wav", data=b"room")
    room_transcript_record = _persist_transcript(
        store=store,
        source_record=room_source,
        source_id="room_mic",
    )
    regular_record, exclusive_record = _persist_remote_diarization_pair(
        store=store,
        source_record=room_source,
        source_id="room_mic",
        capture_profile="in_person_single_mic",
        speaker_label_prefix="Room speaker",
        name_prefix="room-",
    )
    exclusive_payload = json.loads(store.retrieve(exclusive_record.id))
    exclusive_payload["context"]["capture_profile"] = "online_dual_stream"
    mismatched_exclusive_record = store.store(
        artifact_type="diarization",
        name="room-exclusive-profile-mismatch.json",
        data=json.dumps(exclusive_payload).encode("utf-8"),
        metadata=exclusive_record.metadata,
    )
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)
    policy = MergePolicy(capture_profile="in_person_single_mic")

    with pytest.raises(
        MergeTimingError,
        match="Room exclusive diarization capture_profile must match the merge policy",
    ):
        run_merge_timing_stage(
            store=store,
            ledger=ledger,
            **_single_source_stage_inputs(
                policy=policy,
                room_transcript_record=room_transcript_record,
                room_diarization_input=SourceDiarizationInput(
                    regular_record=regular_record,
                    exclusive_record=mismatched_exclusive_record,
                ),
            ),
            now=NOW,
        )


def test_merge_timing_stage_reuses_verified_completed_single_source_output() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    room_source = store.store(artifact_type="source_audio", name="room.wav", data=b"room")
    room_transcript_record = _persist_transcript(
        store=store,
        source_record=room_source,
        source_id="room_mic",
    )
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)
    policy = MergePolicy(capture_profile="in_person_single_mic")

    first = run_merge_timing_stage(
        store=store,
        ledger=ledger,
        **_single_source_stage_inputs(
            policy=policy,
            room_transcript_record=room_transcript_record,
            room_diarization_input=SourceDiarizationInput(
                status="skipped",
                reason="diarization_disabled",
            ),
        ),
        now=NOW,
    )
    second = run_merge_timing_stage(
        store=store,
        ledger=ledger,
        **_single_source_stage_inputs(
            policy=policy,
            room_transcript_record=room_transcript_record,
            room_diarization_input=SourceDiarizationInput(
                status="skipped",
                reason="diarization_disabled",
            ),
        ),
        now=NOW,
    )

    assert second.skipped is True
    assert second.skip_reason == "completed_output_reused"
    assert second.merged_transcript_record == first.merged_transcript_record
    assert (
        first.merged_transcript_record.metadata["policy_config_id"]
        == IN_PERSON_SINGLE_MIC_POLICY_CONFIG_ID
    )
    room_source_info = second.merged_transcript.sources[0]
    room_segment = second.merged_transcript.segments[0]
    assert room_source_info.diarization_status == "skipped"
    assert room_source_info.diarization_reason == "diarization_disabled"
    assert room_segment.speaker.kind == "fallback_room"
    assert room_segment.speaker.label == "Room"
    assert room_segment.speaker.confidence.status == "unavailable"


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
    policy = MergePolicy()

    first = run_merge_timing_stage(
        store=store,
        ledger=ledger,
        **_online_stage_inputs(
            policy=policy,
            local_transcript_record=local_transcript_record,
            remote_transcript_record=remote_transcript_record,
            remote_diarization_input=SourceDiarizationInput(
                status="skipped",
                reason="diarization_disabled",
            ),
        ),
        now=NOW,
    )
    second = run_merge_timing_stage(
        store=store,
        ledger=ledger,
        **_online_stage_inputs(
            policy=policy,
            local_transcript_record=local_transcript_record,
            remote_transcript_record=remote_transcript_record,
            remote_diarization_input=SourceDiarizationInput(
                status="skipped",
                reason="diarization_disabled",
            ),
        ),
        now=NOW,
    )

    assert second.skipped is True
    assert second.skip_reason == "completed_output_reused"
    assert second.merged_transcript_record == first.merged_transcript_record
    assert (
        first.merged_transcript_record.metadata["policy_config_id"]
        == ONLINE_DUAL_STREAM_POLICY_CONFIG_ID
    )


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
    policy = MergePolicy()

    run_merge_timing_stage(
        store=store,
        ledger=ledger,
        **_online_stage_inputs(
            policy=policy,
            local_transcript_record=local_transcript_record,
            remote_transcript_record=remote_transcript_record,
            remote_diarization_input=SourceDiarizationInput(
                status="skipped",
                reason="diarization_disabled",
            ),
        ),
        now=NOW,
    )
    changed_policy = MergePolicy(remote_fallback_speaker_label="Remote participant")
    second = run_merge_timing_stage(
        store=store,
        ledger=ledger,
        **_online_stage_inputs(
            policy=changed_policy,
            local_transcript_record=local_transcript_record,
            remote_transcript_record=remote_transcript_record,
            remote_diarization_input=SourceDiarizationInput(
                status="skipped",
                reason="diarization_disabled",
            ),
        ),
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
        **_online_stage_inputs(
            policy=changed_policy,
            local_transcript_record=local_transcript_record,
            remote_transcript_record=other_remote_transcript,
            remote_diarization_input=SourceDiarizationInput(
                status="skipped",
                reason="diarization_disabled",
            ),
        ),
        attempt=3,
        now=NOW,
    )

    assert second.skipped is False
    assert third.skipped is False


def test_merge_timing_stage_reuses_explicit_policy_for_custom_source_ids() -> None:
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
    policy = MergePolicy(local_source_id="host_mic", remote_source_id="call_audio")

    first = run_merge_timing_stage(
        store=store,
        ledger=ledger,
        **_online_stage_inputs(
            policy=policy,
            local_transcript_record=local_transcript_record,
            remote_transcript_record=remote_transcript_record,
            remote_diarization_input=SourceDiarizationInput(
                status="skipped",
                reason="diarization_disabled",
            ),
        ),
        now=NOW,
    )
    second = run_merge_timing_stage(
        store=store,
        ledger=ledger,
        **_online_stage_inputs(
            policy=policy,
            local_transcript_record=local_transcript_record,
            remote_transcript_record=remote_transcript_record,
            remote_diarization_input=SourceDiarizationInput(
                status="skipped",
                reason="diarization_disabled",
            ),
        ),
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
    policy = MergePolicy()

    result = run_merge_timing_stage(
        store=store,
        ledger=ledger,
        **_online_stage_inputs(
            policy=policy,
            local_transcript_record=local_transcript_record,
            remote_transcript_record=remote_transcript_record,
            remote_diarization_input=SourceDiarizationInput(
                status="skipped",
                reason="diarization_disabled",
            ),
        ),
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
    policy = MergePolicy()
    first = run_merge_timing_stage(
        store=store,
        ledger=ledger,
        **_online_stage_inputs(
            policy=policy,
            local_transcript_record=local_transcript_record,
            remote_transcript_record=remote_transcript_record,
            remote_diarization_input=SourceDiarizationInput(
                status="skipped",
                reason="diarization_disabled",
            ),
        ),
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
        **_online_stage_inputs(
            policy=policy,
            local_transcript_record=local_transcript_record,
            remote_transcript_record=remote_transcript_record,
            remote_diarization_input=SourceDiarizationInput(
                status="skipped",
                reason="diarization_disabled",
            ),
        ),
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
    policy = MergePolicy()

    with pytest.raises(MergeTimingInputError, match="both regular and exclusive"):
        run_merge_timing_stage(
            store=store,
            ledger=ledger,
            **_online_stage_inputs(
                policy=policy,
                local_transcript_record=local_transcript_record,
                remote_transcript_record=remote_transcript_record,
                remote_diarization_input=SourceDiarizationInput(
                    regular_record=regular_record,
                ),
            ),
            now=NOW,
        )


def test_merge_timing_stage_rejects_unexpected_or_unused_source_inputs() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    local_source = store.store(artifact_type="source_audio", name="local.wav", data=b"local")
    remote_source = store.store(artifact_type="source_audio", name="remote.wav", data=b"remote")
    extra_source = store.store(artifact_type="source_audio", name="extra.wav", data=b"extra")
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
    extra_transcript_record = _persist_transcript(
        store=store,
        source_record=extra_source,
        source_id="extra_source",
    )
    regular_record, exclusive_record = _persist_remote_diarization_pair(
        store=store,
        source_record=remote_source,
    )
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)
    policy = MergePolicy()

    with pytest.raises(MergeTimingInputError, match="unexpected transcript record"):
        run_merge_timing_stage(
            store=store,
            ledger=ledger,
            policy=policy,
            transcript_records={
                policy.local_source_id: local_transcript_record,
                policy.remote_source_id: remote_transcript_record,
                "extra_source": extra_transcript_record,
            },
        )

    with pytest.raises(MergeTimingInputError, match="unexpected diarization input"):
        run_merge_timing_stage(
            store=store,
            ledger=ledger,
            policy=policy,
            transcript_records={
                policy.local_source_id: local_transcript_record,
                policy.remote_source_id: remote_transcript_record,
            },
            diarization_inputs={
                "extra_source": SourceDiarizationInput(
                    regular_record=regular_record,
                    exclusive_record=exclusive_record,
                )
            },
        )

    with pytest.raises(MergeTimingInputError, match="both regular and exclusive"):
        run_merge_timing_stage(
            store=store,
            ledger=ledger,
            policy=policy,
            transcript_records={
                policy.local_source_id: local_transcript_record,
                policy.remote_source_id: remote_transcript_record,
            },
            diarization_inputs={
                policy.local_source_id: SourceDiarizationInput(regular_record=regular_record)
            },
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
    policy = MergePolicy()

    with pytest.raises(MergeTimingInputError, match="status is invalid"):
        run_merge_timing_stage(
            store=store,
            ledger=ledger,
            **_online_stage_inputs(
                policy=policy,
                local_transcript_record=local_transcript_record,
                remote_transcript_record=remote_transcript_record,
                remote_diarization_input=SourceDiarizationInput(status="bad"),
            ),
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
            **_online_stage_inputs(
                policy=MergePolicy(local_source_id="wrong_local"),
                local_transcript_record=local_transcript_record,
                remote_transcript_record=remote_transcript_record,
            ),
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
    policy = MergePolicy()

    with pytest.raises(MergeTimingError, match="match metadata"):
        run_merge_timing_stage(
            store=store,
            ledger=ledger,
            **_online_stage_inputs(
                policy=policy,
                local_transcript_record=local_transcript_record,
                remote_transcript_record=remote_transcript_record,
                remote_diarization_input=SourceDiarizationInput(
                    status="skipped",
                    reason="diarization_disabled",
                ),
            ),
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
    policy = MergePolicy()

    result = run_merge_timing_stage(
        store=store,
        ledger=ledger,
        **_online_stage_inputs(
            policy=policy,
            local_transcript_record=local_transcript_record,
            remote_transcript_record=remote_transcript_record,
            remote_diarization_input=SourceDiarizationInput(
                regular_record=regular_record,
                exclusive_record=exclusive_record,
            ),
        ),
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
    policy = MergePolicy()

    result = run_merge_timing_stage(
        store=store,
        ledger=ledger,
        **_online_stage_inputs(
            policy=policy,
            local_transcript_record=local_transcript_record,
            remote_transcript_record=remote_transcript_record,
            remote_diarization_input=SourceDiarizationInput(
                status="skipped",
                reason="diarization_disabled",
            ),
        ),
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
    policy = MergePolicy()

    with pytest.raises(MergeTimingError, match="Remote transcript session_id"):
        run_merge_timing_stage(
            store=store,
            ledger=ledger,
            **_online_stage_inputs(
                policy=policy,
                local_transcript_record=local_transcript_record,
                remote_transcript_record=remote_transcript_record,
                remote_diarization_input=SourceDiarizationInput(
                    status="skipped",
                    reason="diarization_disabled",
                ),
            ),
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
    source_id: str = "system_audio",
    capture_profile: str = "online_dual_stream",
    speaker_label_prefix: str = "Remote speaker",
    name_prefix: str = "",
) -> tuple[ArtifactRecord, ArtifactRecord]:
    source_ref = ArtifactRef.from_record(source_record)
    speakers = (
        DiarizedSpeaker(
            id="speaker_1",
            label=f"{speaker_label_prefix} 1",
            embedding=SpeakerEmbeddingRef.unavailable("fixture"),
        ),
        DiarizedSpeaker(
            id="speaker_2",
            label=f"{speaker_label_prefix} 2",
            embedding=SpeakerEmbeddingRef.unavailable("fixture"),
        ),
    )
    regular = DiarizationArtifact(
        version=DIARIZATION_SCHEMA_VERSION,
        kind="regular_timeline",
        session_id=SESSION_ID,
        source_artifact=source_ref,
        source_id=source_id,
        context=DiarizationContext(
            capture_profile=capture_profile,
            source_id=source_id,
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
        name=f"{name_prefix}regular.json",
    )
    exclusive = DiarizationArtifact(
        version=DIARIZATION_SCHEMA_VERSION,
        kind="exclusive_projection",
        session_id=SESSION_ID,
        source_artifact=source_ref,
        source_id=source_id,
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
        name=f"{name_prefix}exclusive.json",
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
