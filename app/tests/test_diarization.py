import json
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

import mena.diarization as diarization_module
from mena.artifacts import FileSystemArtifactStore, InMemoryArtifactStore
from mena.diarization import (
    DIARIZATION_ARTIFACT_TYPE,
    DIARIZATION_SCHEMA_VERSION,
    DIARIZATION_STAGE,
    DiarizationArtifact,
    DiarizationContext,
    DiarizationContractError,
    DiarizationError,
    DiarizationInputError,
    DiarizationProviderMetadata,
    DiarizationProviderUnavailable,
    DiarizationRequest,
    DiarizationSegment,
    DiarizedSpeaker,
    FakeDiarizationProvider,
    PyannoteCommunityDiarizationProvider,
    SpeakerEmbeddingRef,
    decide_diarization,
    persist_diarization_artifact,
    run_diarization_stage,
)
from mena.processing import ArtifactRef, ProcessingStageLedger

SESSION_ID = "00000000-0000-0000-0000-000000000001"
RUN_ID = "run-20260517"
NOW = datetime(2026, 5, 17, 10, 0, 0, tzinfo=UTC)


def test_fake_diarization_stage_persists_three_artifacts_and_completed_checkpoint(
    tmp_path: Path,
) -> None:
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    source = store.store(artifact_type="source_audio", name="system_audio.wav", data=b"audio")
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)
    provider = FakeDiarizationProvider()

    result = run_diarization_stage(
        store=store,
        ledger=ledger,
        provider=provider,
        source_artifact=source,
        context=DiarizationContext(
            capture_profile="online_dual_stream",
            source_id="system_audio",
            remote_speaker_count=2,
        ),
        now=NOW,
    )

    assert result.skipped is False
    assert result.state.status == "completed"
    assert [record.metadata["kind"] for record in result.artifact_records] == [
        "regular_timeline",
        "exclusive_projection",
        "correction_overlay",
    ]
    assert result.state.output_artifacts == tuple(
        ArtifactRef.from_record(record) for record in result.artifact_records
    )
    for record in result.artifact_records:
        assert record.artifact_type == DIARIZATION_ARTIFACT_TYPE
        payload = json.loads(store.retrieve(record.id))
        Draft202012Validator(_load_diarization_schema(), format_checker=FormatChecker()).validate(
            payload
        )
        assert DiarizationArtifact.from_dict(payload).source_artifact == ArtifactRef.from_record(
            source
        )
    regular, exclusive, overlay = result.artifacts
    assert regular.kind == "regular_timeline"
    assert any(segment.overlapped for segment in regular.segments)
    assert exclusive.kind == "exclusive_projection"
    assert overlay.kind == "correction_overlay"
    assert overlay.segments == ()
    assert len(overlay.base_artifacts) == 2
    assert [state.status for state in ledger.states(stage=DIARIZATION_STAGE)] == [
        "running",
        "completed",
    ]
    assert provider.calls[0].context.source_id == "system_audio"


def test_diarization_stage_reuses_completed_output_for_same_context_and_provider() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    source = store.store(artifact_type="source_audio", name="system_audio.wav", data=b"audio")
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)
    context = DiarizationContext(
        capture_profile="online_dual_stream",
        source_id="system_audio",
        remote_speaker_count=2,
    )

    first = run_diarization_stage(
        store=store,
        ledger=ledger,
        provider=FakeDiarizationProvider(),
        source_artifact=source,
        context=context,
        now=NOW,
    )
    second_provider = FakeDiarizationProvider()
    second = run_diarization_stage(
        store=store,
        ledger=ledger,
        provider=second_provider,
        source_artifact=source,
        context=context,
        now=NOW,
    )

    assert second.skipped is True
    assert second.skip_reason == "completed_output_reused"
    assert second.state.status == "skipped"
    assert second.state.output_artifacts == first.state.output_artifacts
    assert second.artifact_records == first.artifact_records
    assert second_provider.calls == []
    assert [state.status for state in ledger.states(stage=DIARIZATION_STAGE)] == [
        "running",
        "completed",
        "skipped",
    ]


def test_diarization_stage_reuse_is_scoped_to_provider_config_and_context() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    source = store.store(artifact_type="source_audio", name="system_audio.wav", data=b"audio")
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)
    context = DiarizationContext(
        capture_profile="online_dual_stream",
        source_id="system_audio",
        remote_speaker_count=2,
    )

    run_diarization_stage(
        store=store,
        ledger=ledger,
        provider=FakeDiarizationProvider(checkpoint="fixture-a"),
        source_artifact=source,
        context=context,
        now=NOW,
    )
    changed_provider = FakeDiarizationProvider(checkpoint="fixture-b")
    changed_provider_result = run_diarization_stage(
        store=store,
        ledger=ledger,
        provider=changed_provider,
        source_artifact=source,
        context=context,
        now=NOW,
    )
    changed_context_provider = FakeDiarizationProvider(checkpoint="fixture-b")
    changed_context_result = run_diarization_stage(
        store=store,
        ledger=ledger,
        provider=changed_context_provider,
        source_artifact=source,
        context=DiarizationContext(
            capture_profile="online_dual_stream",
            source_id="system_audio",
            remote_speaker_count=3,
        ),
        now=NOW,
    )

    assert changed_provider_result.skipped is False
    assert changed_provider.calls
    assert changed_context_result.skipped is False
    assert changed_context_provider.calls


def test_diarization_stage_records_explicit_skip_for_local_mic() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    source = store.store(artifact_type="source_audio", name="local_mic.wav", data=b"audio")
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)
    provider = FakeDiarizationProvider()

    result = run_diarization_stage(
        store=store,
        ledger=ledger,
        provider=provider,
        source_artifact=source,
        context=DiarizationContext(capture_profile="online_dual_stream", source_id="local_mic"),
        now=NOW,
    )

    assert result.skipped is True
    assert result.skip_reason == "structural_local_mic_attribution"
    assert result.state.status == "skipped"
    assert result.state.output_artifacts == ()
    assert provider.calls == []
    assert store.list(artifact_type=DIARIZATION_ARTIFACT_TYPE) == ()


def test_diarization_stage_reuses_one_timestamp_for_skipped_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TickingDatetime:
        calls = iter(
            (
                datetime(2026, 5, 17, 10, 0, 0, tzinfo=UTC),
                datetime(2026, 5, 17, 10, 0, 1, tzinfo=UTC),
            )
        )

        @staticmethod
        def now(tz: object) -> datetime:
            return next(TickingDatetime.calls)

    monkeypatch.setattr(diarization_module, "datetime", TickingDatetime)
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    source = store.store(artifact_type="source_audio", name="local_mic.wav", data=b"audio")
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    result = run_diarization_stage(
        store=store,
        ledger=ledger,
        provider=FakeDiarizationProvider(),
        source_artifact=source,
        context=DiarizationContext(capture_profile="online_dual_stream", source_id="local_mic"),
    )

    assert result.state.started_at == "2026-05-17T10:00:00Z"
    assert result.state.completed_at == result.state.started_at


def test_diarization_policy_runs_room_mic_and_skips_single_remote_speaker() -> None:
    assert decide_diarization(
        DiarizationContext(capture_profile="in_person_single_mic", source_id="room_mic")
    ).should_run
    single_remote = decide_diarization(
        DiarizationContext(
            capture_profile="online_dual_stream",
            source_id="system_audio",
            remote_speaker_count=1,
        )
    )
    disabled = decide_diarization(
        DiarizationContext(
            capture_profile="online_dual_stream",
            source_id="system_audio",
            enabled=False,
        )
    )

    assert single_remote.should_run is False
    assert single_remote.reason == "single_remote_speaker"
    assert disabled.should_run is False
    assert disabled.reason == "diarization_disabled"


def test_diarization_policy_records_unknown_source_as_not_applicable() -> None:
    decision = decide_diarization(
        DiarizationContext(capture_profile="online_dual_stream", source_id="browser_tab")
    )

    assert decision.should_run is False
    assert decision.reason == "source_not_applicable"
    assert decision.fallback_label == "Speaker unknown"


def test_diarization_stage_fails_before_provider_when_source_artifact_is_corrupt(
    tmp_path: Path,
) -> None:
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    source = store.store(artifact_type="source_audio", name="system_audio.wav", data=b"audio")
    (tmp_path / source.path).write_bytes(b"changed")
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)
    provider = FakeDiarizationProvider()

    with pytest.raises(DiarizationInputError, match="artifact content"):
        run_diarization_stage(
            store=store,
            ledger=ledger,
            provider=provider,
            source_artifact=source,
            context=DiarizationContext(
                capture_profile="online_dual_stream",
                source_id="system_audio",
                remote_speaker_count=2,
            ),
            now=NOW,
        )

    assert provider.calls == []
    latest = ledger.latest(DIARIZATION_STAGE)
    assert latest is not None
    assert latest.status == "failed"
    assert latest.error is not None
    assert latest.error.code == "input_artifact_invalid"
    assert store.list(artifact_type=DIARIZATION_ARTIFACT_TYPE) == ()


def test_diarization_stage_records_failed_checkpoint_for_missing_input_artifact() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    source = store.store(artifact_type="source_audio", name="system_audio.wav", data=b"audio")
    del store._records[source.id]
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)
    provider = FakeDiarizationProvider()

    with pytest.raises(DiarizationInputError, match="not available"):
        run_diarization_stage(
            store=store,
            ledger=ledger,
            provider=provider,
            source_artifact=source,
            context=DiarizationContext(
                capture_profile="online_dual_stream",
                source_id="system_audio",
                remote_speaker_count=2,
            ),
            now=NOW,
        )

    assert provider.calls == []
    latest = ledger.latest(DIARIZATION_STAGE)
    assert latest is not None
    assert latest.status == "failed"
    assert latest.error is not None
    assert latest.error.code == "input_artifact_invalid"


def test_diarization_stage_records_retryable_provider_unavailable() -> None:
    class UnavailableProvider:
        name = "unavailable"
        config_id = "unavailable"

        def diarize(self, request: DiarizationRequest) -> Any:
            raise DiarizationProviderUnavailable("missing local model")

    store = InMemoryArtifactStore(session_id=SESSION_ID)
    source = store.store(artifact_type="source_audio", name="system_audio.wav", data=b"audio")
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    with pytest.raises(DiarizationProviderUnavailable, match="missing local model"):
        run_diarization_stage(
            store=store,
            ledger=ledger,
            provider=UnavailableProvider(),
            source_artifact=source,
            context=DiarizationContext(
                capture_profile="online_dual_stream",
                source_id="system_audio",
            ),
            now=NOW,
        )

    latest = ledger.latest(DIARIZATION_STAGE)
    assert latest is not None
    assert latest.status == "failed"
    assert latest.error is not None
    assert latest.error.retryable is True
    assert latest.error.code == "provider_unavailable"


def test_diarization_stage_records_nonretryable_provider_failure() -> None:
    class FailingProvider:
        name = "failing"
        config_id = "failing"

        def diarize(self, request: DiarizationRequest) -> Any:
            raise DiarizationError("bad diarization")

    store = InMemoryArtifactStore(session_id=SESSION_ID)
    source = store.store(artifact_type="source_audio", name="system_audio.wav", data=b"audio")
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    with pytest.raises(DiarizationError, match="bad diarization"):
        run_diarization_stage(
            store=store,
            ledger=ledger,
            provider=FailingProvider(),
            source_artifact=source,
            context=DiarizationContext(
                capture_profile="online_dual_stream",
                source_id="system_audio",
            ),
            now=NOW,
        )

    latest = ledger.latest(DIARIZATION_STAGE)
    assert latest is not None
    assert latest.status == "failed"
    assert latest.error is not None
    assert latest.error.retryable is False
    assert latest.error.code == "diarization_failed"


def test_diarization_stage_records_failed_checkpoint_for_untyped_provider_exception() -> None:
    class CrashingProvider:
        name = "crashing"
        config_id = "crashing"

        def diarize(self, request: DiarizationRequest) -> Any:
            raise ValueError("library exploded")

    store = InMemoryArtifactStore(session_id=SESSION_ID)
    source = store.store(artifact_type="source_audio", name="system_audio.wav", data=b"audio")
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    with pytest.raises(DiarizationError, match="library exploded"):
        run_diarization_stage(
            store=store,
            ledger=ledger,
            provider=CrashingProvider(),
            source_artifact=source,
            context=DiarizationContext(
                capture_profile="online_dual_stream",
                source_id="system_audio",
            ),
            now=NOW,
        )

    latest = ledger.latest(DIARIZATION_STAGE)
    assert latest is not None
    assert latest.status == "failed"
    assert latest.error is not None
    assert latest.error.retryable is False
    assert latest.error.code == "provider_exception"


def test_diarization_completed_reuse_fails_closed_when_output_ref_is_missing() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    source = store.store(artifact_type="source_audio", name="system_audio.wav", data=b"audio")
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)
    context = DiarizationContext(
        capture_profile="online_dual_stream",
        source_id="system_audio",
        remote_speaker_count=2,
    )
    first = run_diarization_stage(
        store=store,
        ledger=ledger,
        provider=FakeDiarizationProvider(),
        source_artifact=source,
        context=context,
        now=NOW,
    )
    for record in first.artifact_records:
        del store._records[record.id]
    provider = FakeDiarizationProvider()

    second = run_diarization_stage(
        store=store,
        ledger=ledger,
        provider=provider,
        source_artifact=source,
        context=context,
        now=NOW,
    )

    assert second.skipped is False
    assert provider.calls


def test_diarization_completed_reuse_fails_closed_when_output_payload_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    source = store.store(artifact_type="source_audio", name="system_audio.wav", data=b"audio")
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)
    context = DiarizationContext(
        capture_profile="online_dual_stream",
        source_id="system_audio",
        remote_speaker_count=2,
    )
    first = run_diarization_stage(
        store=store,
        ledger=ledger,
        provider=FakeDiarizationProvider(),
        source_artifact=source,
        context=context,
        now=NOW,
    )
    original_retrieve = store.retrieve

    def retrieve_with_invalid_reused_payload(artifact_id: str) -> bytes:
        if artifact_id == first.artifact_records[0].id:
            return b"{not-json"
        return original_retrieve(artifact_id)

    monkeypatch.setattr(store, "retrieve", retrieve_with_invalid_reused_payload)
    provider = FakeDiarizationProvider()

    second = run_diarization_stage(
        store=store,
        ledger=ledger,
        provider=provider,
        source_artifact=source,
        context=context,
        attempt=2,
        now=NOW,
    )

    assert second.skipped is False
    assert provider.calls
    assert [state.status for state in ledger.states(stage=DIARIZATION_STAGE)] == [
        "running",
        "completed",
        "running",
        "completed",
    ]


def test_persist_diarization_artifact_validates_contract_before_writing() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    source = store.store(artifact_type="source_audio", name="room.wav", data=b"audio")
    artifact = _diarization_artifact(
        source_ref=ArtifactRef.from_record(source),
        segments=(),
    )

    with pytest.raises(DiarizationContractError, match="must contain segments"):
        persist_diarization_artifact(store=store, artifact=artifact, name="bad.json")

    assert store.list(artifact_type=DIARIZATION_ARTIFACT_TYPE) == ()


def test_diarization_schema_rejects_incomplete_provider_metadata() -> None:
    source_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/source_audio/system.wav",
        artifact_type="source_audio",
        version=1,
        sha256="0" * 64,
    )
    payload = _diarization_artifact(source_ref=source_ref).to_dict()
    del cast(dict[str, object], payload["provider"])["checkpoint"]
    validator = Draft202012Validator(_load_diarization_schema(), format_checker=FormatChecker())

    with pytest.raises(ValidationError, match="'checkpoint' is a required property"):
        validator.validate(payload)


def test_diarization_schema_rejects_python_contract_mismatches() -> None:
    source_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/source_audio/system.wav",
        artifact_type="source_audio",
        version=1,
        sha256="0" * 64,
    )
    validator = Draft202012Validator(_load_diarization_schema(), format_checker=FormatChecker())

    payload = _diarization_artifact(source_ref=source_ref).to_dict()
    cast(dict[str, object], payload["context"])["remote_speaker_count"] = 0
    with pytest.raises(ValidationError, match="minimum"):
        validator.validate(payload)

    payload = _diarization_artifact(source_ref=source_ref).to_dict()
    embedding = cast(
        dict[str, object],
        cast(list[dict[str, object]], payload["speakers"])[0]["embedding"],
    )
    embedding["status"] = "unavailable"
    embedding["reason"] = None
    with pytest.raises(ValidationError, match="not of type 'string'"):
        validator.validate(payload)

    payload = _diarization_artifact(source_ref=source_ref).to_dict()
    embedding = cast(
        dict[str, object],
        cast(list[dict[str, object]], payload["speakers"])[0]["embedding"],
    )
    embedding["status"] = "available"
    embedding["artifact"] = None
    with pytest.raises(ValidationError, match="not of type 'object'"):
        validator.validate(payload)

    payload = _diarization_artifact(source_ref=source_ref, kind="correction_overlay").to_dict()
    payload["segments"] = []
    payload["base_artifacts"] = [source_ref.to_dict(), source_ref.to_dict(), source_ref.to_dict()]
    with pytest.raises(ValidationError, match="is too long"):
        validator.validate(payload)


def test_diarization_schema_is_valid() -> None:
    Draft202012Validator.check_schema(_load_diarization_schema())


def test_diarization_contract_rejects_unknown_speaker_and_bad_embedding() -> None:
    source_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/source_audio/system.wav",
        artifact_type="source_audio",
        version=1,
        sha256="0" * 64,
    )
    payload = _diarization_artifact(source_ref=source_ref).to_dict()
    cast(list[dict[str, object]], payload["segments"])[0]["speaker_id"] = "speaker_missing"

    with pytest.raises(DiarizationContractError, match="unknown speaker"):
        DiarizationArtifact.from_dict(payload)

    payload = _diarization_artifact(source_ref=source_ref).to_dict()
    embedding = cast(
        dict[str, object], cast(list[dict[str, object]], payload["speakers"])[0]["embedding"]
    )
    embedding["status"] = "available"
    embedding["artifact"] = None

    with pytest.raises(DiarizationContractError, match="must reference"):
        DiarizationArtifact.from_dict(payload)

    payload = _diarization_artifact(source_ref=source_ref).to_dict()
    embedding = cast(
        dict[str, object],
        cast(list[dict[str, object]], payload["speakers"])[0]["embedding"],
    )
    embedding["status"] = "mystery"

    with pytest.raises(DiarizationContractError, match="status"):
        DiarizationArtifact.from_dict(payload)

    payload = _diarization_artifact(source_ref=source_ref).to_dict()
    embedding = cast(
        dict[str, object],
        cast(list[dict[str, object]], payload["speakers"])[0]["embedding"],
    )
    embedding["reason"] = None

    with pytest.raises(DiarizationContractError, match="record a reason"):
        DiarizationArtifact.from_dict(payload)


def test_diarization_contract_rejects_unknown_policy_version() -> None:
    source_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/source_audio/system.wav",
        artifact_type="source_audio",
        version=1,
        sha256="0" * 64,
    )
    payload = _diarization_artifact(source_ref=source_ref).to_dict()
    context = cast(dict[str, object], payload["context"])
    context["policy_version"] = 999

    with pytest.raises(DiarizationContractError, match="policy_version"):
        DiarizationArtifact.from_dict(payload)


def test_diarization_contract_requires_schema_required_nullable_and_object_fields() -> None:
    source_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/source_audio/system.wav",
        artifact_type="source_audio",
        version=1,
        sha256="0" * 64,
    )
    payload = _diarization_artifact(source_ref=source_ref).to_dict()
    del cast(dict[str, object], payload["context"])["remote_speaker_count"]

    with pytest.raises(DiarizationContractError, match="remote_speaker_count"):
        DiarizationArtifact.from_dict(payload)

    payload = _diarization_artifact(source_ref=source_ref).to_dict()
    segment = cast(list[dict[str, object]], payload["segments"])[0]
    del segment["metadata"]

    with pytest.raises(
        DiarizationContractError,
        match="Diarization segment metadata field 'metadata' must be a JSON object\\.",
    ):
        DiarizationArtifact.from_dict(payload)

    payload = _diarization_artifact(source_ref=source_ref).to_dict()
    speaker = cast(list[dict[str, object]], payload["speakers"])[0]
    speaker["metadata"] = {1: "bad"}

    with pytest.raises(
        DiarizationContractError,
        match="Diarized speaker metadata field 'metadata' must be a JSON object\\.",
    ):
        DiarizationArtifact.from_dict(payload)

    payload = _diarization_artifact(source_ref=source_ref).to_dict()
    provider = cast(dict[str, object], payload["provider"])
    provider["parameters"] = {"bad": object()}

    with pytest.raises(
        DiarizationContractError,
        match="Diarization provider parameters field 'parameters' must be JSON serializable\\.",
    ):
        DiarizationArtifact.from_dict(payload)

    payload = _diarization_artifact(source_ref=source_ref).to_dict()
    provider = cast(dict[str, object], payload["provider"])
    provider["metadata"] = MappingProxyType({})

    with pytest.raises(
        DiarizationContractError,
        match="Diarization provider metadata field 'metadata' must be a JSON object\\.",
    ):
        DiarizationArtifact.from_dict(payload)

    payload = _diarization_artifact(source_ref=source_ref).to_dict()
    speaker = cast(list[dict[str, object]], payload["speakers"])[0]
    embedding = cast(dict[str, object], speaker["embedding"])
    del embedding["artifact"]

    with pytest.raises(DiarizationContractError, match="artifact"):
        DiarizationArtifact.from_dict(payload)

    payload = _diarization_artifact(source_ref=source_ref).to_dict()
    speaker = cast(list[dict[str, object]], payload["speakers"])[0]
    del speaker["embedding"]

    with pytest.raises(DiarizationContractError, match="embedding"):
        DiarizationArtifact.from_dict(payload)

    payload = _diarization_artifact(source_ref=source_ref).to_dict()
    payload["overlay_entries"] = [None]

    with pytest.raises(DiarizationContractError, match="correction overlay entry"):
        DiarizationArtifact.from_dict(payload)


def test_diarization_context_rejects_non_positive_remote_speaker_count() -> None:
    with pytest.raises(DiarizationContractError, match="at least 1"):
        DiarizationContext(
            capture_profile="online_dual_stream",
            source_id="system_audio",
            remote_speaker_count=0,
        )


def test_pyannote_provider_reports_missing_optional_dependency(tmp_path: Path) -> None:
    source_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/source_audio/system.wav",
        artifact_type="source_audio",
        version=1,
        sha256="0" * 64,
    )
    provider = PyannoteCommunityDiarizationProvider(
        module_loader=lambda _name: _raise_import_error(),
    )

    with pytest.raises(DiarizationProviderUnavailable, match="not installed"):
        provider.diarize(
            DiarizationRequest(
                session_id=SESSION_ID,
                source_artifact=source_ref,
                source_id="system_audio",
                context=DiarizationContext(
                    capture_profile="online_dual_stream",
                    source_id="system_audio",
                ),
                audio_path=tmp_path / "system.wav",
            )
        )


def test_pyannote_provider_classifies_cuda_oom_as_unavailable(tmp_path: Path) -> None:
    source_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/source_audio/system.wav",
        artifact_type="source_audio",
        version=1,
        sha256="0" * 64,
    )

    class FakeTorchDevice:
        def __init__(self, device: str) -> None:
            self.type = device

    class FakeTorch:
        device = FakeTorchDevice

    class OomPipeline:
        def to(self, _device: object) -> object:
            return self

        def __call__(self, _audio_path: str) -> object:
            raise RuntimeError("CUDA out of memory while allocating pyannote tensors")

    def module_loader(name: str) -> object:
        if name == "torch":
            return FakeTorch()
        raise AssertionError(f"unexpected module import: {name}")

    provider = PyannoteCommunityDiarizationProvider(
        device="cuda",
        pipeline_factory=lambda *_args, **_kwargs: OomPipeline(),
        module_loader=module_loader,
    )

    with pytest.raises(DiarizationProviderUnavailable, match="CUDA out of memory"):
        provider.diarize(
            DiarizationRequest(
                session_id=SESSION_ID,
                source_artifact=source_ref,
                source_id="system_audio",
                context=DiarizationContext(
                    capture_profile="online_dual_stream",
                    source_id="system_audio",
                ),
                audio_path=tmp_path / "system.wav",
            )
        )


def test_pyannote_provider_classifies_cublas_alloc_failure_without_cuda_token(
    tmp_path: Path,
) -> None:
    source_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/source_audio/system.wav",
        artifact_type="source_audio",
        version=1,
        sha256="0" * 64,
    )

    class FakeTorchDevice:
        def __init__(self, device: str) -> None:
            self.type = device

    class FakeTorch:
        device = FakeTorchDevice

    class CublasPipeline:
        def to(self, _device: object) -> object:
            return self

        def __call__(self, _audio_path: str) -> object:
            raise RuntimeError("cuBLAS failed with status CUBLAS_STATUS_ALLOC_FAILED")

    def module_loader(name: str) -> object:
        if name == "torch":
            return FakeTorch()
        raise AssertionError(f"unexpected module import: {name}")

    provider = PyannoteCommunityDiarizationProvider(
        device="cuda",
        pipeline_factory=lambda *_args, **_kwargs: CublasPipeline(),
        module_loader=module_loader,
    )

    with pytest.raises(DiarizationProviderUnavailable, match="CUBLAS_STATUS_ALLOC_FAILED"):
        provider.diarize(
            DiarizationRequest(
                session_id=SESSION_ID,
                source_artifact=source_ref,
                source_id="system_audio",
                context=DiarizationContext(
                    capture_profile="online_dual_stream",
                    source_id="system_audio",
                ),
                audio_path=tmp_path / "system.wav",
            )
        )


def test_pyannote_provider_classifies_cudnn_not_initialized_as_unavailable(
    tmp_path: Path,
) -> None:
    source_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/source_audio/system.wav",
        artifact_type="source_audio",
        version=1,
        sha256="0" * 64,
    )

    class FakeTorchDevice:
        def __init__(self, device: str) -> None:
            self.type = device

    class FakeTorch:
        device = FakeTorchDevice

    class CudnnInitPipeline:
        def to(self, _device: object) -> object:
            return self

        def __call__(self, _audio_path: str) -> object:
            raise RuntimeError("cuDNN error: CUDNN_STATUS_NOT_INITIALIZED")

    def module_loader(name: str) -> object:
        if name == "torch":
            return FakeTorch()
        raise AssertionError(f"unexpected module import: {name}")

    provider = PyannoteCommunityDiarizationProvider(
        device="cuda",
        pipeline_factory=lambda *_args, **_kwargs: CudnnInitPipeline(),
        module_loader=module_loader,
    )

    with pytest.raises(DiarizationProviderUnavailable, match="CUDNN_STATUS_NOT_INITIALIZED"):
        provider.diarize(
            DiarizationRequest(
                session_id=SESSION_ID,
                source_artifact=source_ref,
                source_id="system_audio",
                context=DiarizationContext(
                    capture_profile="online_dual_stream",
                    source_id="system_audio",
                ),
                audio_path=tmp_path / "system.wav",
            )
        )


def test_pyannote_provider_does_not_classify_cpu_oom_as_gpu_failure(
    tmp_path: Path,
) -> None:
    source_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/source_audio/system.wav",
        artifact_type="source_audio",
        version=1,
        sha256="0" * 64,
    )

    class FakeTorchDevice:
        def __init__(self, device: str) -> None:
            self.type = device

    class FakeTorch:
        device = FakeTorchDevice

    class CpuOomPipeline:
        def to(self, _device: object) -> object:
            return self

        def __call__(self, _audio_path: str) -> object:
            raise RuntimeError(
                "DefaultCPUAllocator: not enough memory: you tried to allocate 1073741824 bytes"
            )

    def module_loader(name: str) -> object:
        if name == "torch":
            return FakeTorch()
        raise AssertionError(f"unexpected module import: {name}")

    provider = PyannoteCommunityDiarizationProvider(
        device="cpu",
        pipeline_factory=lambda *_args, **_kwargs: CpuOomPipeline(),
        module_loader=module_loader,
    )

    with pytest.raises(RuntimeError, match="DefaultCPUAllocator") as excinfo:
        provider.diarize(
            DiarizationRequest(
                session_id=SESSION_ID,
                source_artifact=source_ref,
                source_id="system_audio",
                context=DiarizationContext(
                    capture_profile="online_dual_stream",
                    source_id="system_audio",
                ),
                audio_path=tmp_path / "system.wav",
            )
        )
    assert not isinstance(excinfo.value, DiarizationProviderUnavailable)


def test_pyannote_provider_reports_incompatible_optional_dependency(tmp_path: Path) -> None:
    source_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/source_audio/system.wav",
        artifact_type="source_audio",
        version=1,
        sha256="0" * 64,
    )

    class ModuleWithoutPipeline:
        pass

    provider = PyannoteCommunityDiarizationProvider(
        module_loader=lambda _name: ModuleWithoutPipeline(),
    )

    with pytest.raises(DiarizationProviderUnavailable, match="Pipeline.from_pretrained"):
        provider.diarize(
            DiarizationRequest(
                session_id=SESSION_ID,
                source_artifact=source_ref,
                source_id="system_audio",
                context=DiarizationContext(
                    capture_profile="online_dual_stream",
                    source_id="system_audio",
                ),
                audio_path=tmp_path / "system.wav",
            )
        )


def test_pyannote_provider_requires_audio_path() -> None:
    source_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/source_audio/system.wav",
        artifact_type="source_audio",
        version=1,
        sha256="0" * 64,
    )
    provider = PyannoteCommunityDiarizationProvider(pipeline_factory=lambda *_args: object())

    with pytest.raises(DiarizationInputError, match="requires an audio_path"):
        provider.diarize(
            DiarizationRequest(
                session_id=SESSION_ID,
                source_artifact=source_ref,
                source_id="system_audio",
                context=DiarizationContext(
                    capture_profile="online_dual_stream",
                    source_id="system_audio",
                ),
            )
        )


def test_pyannote_provider_maps_turns_without_confidence_scores(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    class FakeTurn:
        def __init__(self, start: float, end: float) -> None:
            self.start = start
            self.end = end

    class FakeAnnotation:
        def itertracks(self, *, yield_label: bool) -> list[tuple[FakeTurn, str, str]]:
            assert yield_label is True
            return [
                (FakeTurn(0.0, 1.0), "track-a", "SPEAKER_00"),
                (FakeTurn(0.8, 2.0), "track-b", "SPEAKER_01"),
            ]

    class FakePipeline:
        def __call__(self, audio_path: str) -> FakeAnnotation:
            calls.append({"audio_path": audio_path})
            return FakeAnnotation()

    def pipeline_factory(*args: object, **kwargs: object) -> FakePipeline:
        calls.append({"pipeline_args": list(args), "pipeline_kwargs": kwargs})
        return FakePipeline()

    source_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/source_audio/system.wav",
        artifact_type="source_audio",
        version=1,
        sha256="0" * 64,
    )
    provider = PyannoteCommunityDiarizationProvider(
        checkpoint="pyannote/speaker-diarization-community-1",
        token="token",
        pipeline_factory=pipeline_factory,
    )

    result = provider.diarize(
        DiarizationRequest(
            session_id=SESSION_ID,
            source_artifact=source_ref,
            source_id="system_audio",
            context=DiarizationContext(
                capture_profile="online_dual_stream",
                source_id="system_audio",
            ),
            audio_path=tmp_path / "system.wav",
        )
    )

    assert calls[0]["pipeline_args"] == ["pyannote/speaker-diarization-community-1"]
    assert calls[0]["pipeline_kwargs"] == {"token": "token"}
    assert calls[1]["audio_path"] == str(tmp_path / "system.wav")
    assert [speaker.id for speaker in result.speakers] == ["speaker_00", "speaker_01"]
    assert result.regular_segments[0].speaker_id == "speaker_00"
    assert [segment.overlapped for segment in result.regular_segments] == [True, True]
    assert [segment.id for segment in result.exclusive_segments] == [
        "exclusive_000001",
        "exclusive_000002",
    ]
    assert [segment.metadata["projection"] for segment in result.exclusive_segments] == [
        "regular timeline projected to one speaker per instant",
        "regular timeline projected to one speaker per instant",
    ]
    assert result.speakers[0].embedding.status == "unavailable"


def test_pyannote_provider_normalizes_community_diarize_output(tmp_path: Path) -> None:
    class FakeTurn:
        def __init__(self, start: float, end: float) -> None:
            self.start = start
            self.end = end

    class FakeAnnotation:
        def __init__(self, tracks: list[tuple[FakeTurn, str, str]]) -> None:
            self._tracks = tracks

        def itertracks(self, *, yield_label: bool) -> list[tuple[FakeTurn, str, str]]:
            assert yield_label is True
            return self._tracks

    class FakeDiarizeOutput:
        def __init__(self) -> None:
            self.speaker_diarization = FakeAnnotation(
                [
                    (FakeTurn(0.0, 1.0), "track-a", "SPEAKER_00"),
                    (FakeTurn(0.8, 2.0), "track-b", "SPEAKER_01"),
                ]
            )
            self.exclusive_speaker_diarization = FakeAnnotation(
                [
                    (FakeTurn(0.0, 0.8), "track-a", "SPEAKER_00"),
                    (FakeTurn(0.8, 2.0), "track-b", "SPEAKER_01"),
                ]
            )

    class FakePipeline:
        def __call__(self, audio_path: str) -> FakeDiarizeOutput:
            return FakeDiarizeOutput()

    source_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/source_audio/system.wav",
        artifact_type="source_audio",
        version=1,
        sha256="0" * 64,
    )
    provider = PyannoteCommunityDiarizationProvider(
        pipeline_factory=lambda *_args, **_kwargs: FakePipeline()
    )

    result = provider.diarize(
        DiarizationRequest(
            session_id=SESSION_ID,
            source_artifact=source_ref,
            source_id="system_audio",
            context=DiarizationContext(
                capture_profile="online_dual_stream",
                source_id="system_audio",
            ),
            audio_path=tmp_path / "system.wav",
        )
    )

    assert [segment.id for segment in result.regular_segments] == [
        "turn_000001",
        "turn_000002",
    ]
    assert [segment.overlapped for segment in result.regular_segments] == [True, True]
    assert [segment.id for segment in result.exclusive_segments] == [
        "exclusive_000001",
        "exclusive_000002",
    ]
    assert [segment.overlapped for segment in result.exclusive_segments] == [False, False]
    assert [segment.speaker_id for segment in result.exclusive_segments] == [
        "speaker_00",
        "speaker_01",
    ]


def test_pyannote_provider_converts_cpu_device_before_moving_pipeline(tmp_path: Path) -> None:
    class FakeTorchDevice:
        def __init__(self, device: str) -> None:
            self.type = device

    class FakeTorch:
        device = FakeTorchDevice

    class FakeTurn:
        start = 0.0
        end = 1.0

    class FakeAnnotation:
        def itertracks(self, *, yield_label: bool) -> list[tuple[FakeTurn, str, str]]:
            assert yield_label is True
            return [(FakeTurn(), "track-a", "SPEAKER_00")]

    class FakePipeline:
        def __init__(self) -> None:
            self.devices: list[FakeTorchDevice] = []

        def to(self, device: FakeTorchDevice) -> "FakePipeline":
            self.devices.append(device)
            return self

        def __call__(self, audio_path: str) -> FakeAnnotation:
            return FakeAnnotation()

    def module_loader(name: str) -> object:
        assert name == "torch"
        return FakeTorch()

    pipeline = FakePipeline()
    source_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/source_audio/system.wav",
        artifact_type="source_audio",
        version=1,
        sha256="0" * 64,
    )
    provider = PyannoteCommunityDiarizationProvider(
        pipeline_factory=lambda _checkpoint: pipeline,
        module_loader=module_loader,
    )

    provider.diarize(
        DiarizationRequest(
            session_id=SESSION_ID,
            source_artifact=source_ref,
            source_id="system_audio",
            context=DiarizationContext(
                capture_profile="online_dual_stream",
                source_id="system_audio",
            ),
            audio_path=tmp_path / "system.wav",
        )
    )

    assert len(pipeline.devices) == 1
    assert isinstance(pipeline.devices[0], FakeTorchDevice)
    assert pipeline.devices[0].type == "cpu"


def test_pyannote_provider_applies_non_cpu_device(tmp_path: Path) -> None:
    class FakeTorchDevice:
        def __init__(self, device: str) -> None:
            self.type = device

    class FakeTorch:
        device = FakeTorchDevice

    class FakeTurn:
        start = 0.0
        end = 1.0

    class FakeAnnotation:
        def itertracks(self, *, yield_label: bool) -> list[tuple[FakeTurn, str, str]]:
            assert yield_label is True
            return [(FakeTurn(), "track-a", "SPEAKER_00")]

    class FakePipeline:
        def __init__(self) -> None:
            self.devices: list[FakeTorchDevice] = []

        def to(self, device: FakeTorchDevice) -> "FakePipeline":
            self.devices.append(device)
            return self

        def __call__(self, audio_path: str) -> FakeAnnotation:
            return FakeAnnotation()

    def module_loader(name: str) -> object:
        assert name == "torch"
        return FakeTorch()

    pipeline = FakePipeline()
    source_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/source_audio/system.wav",
        artifact_type="source_audio",
        version=1,
        sha256="0" * 64,
    )
    provider = PyannoteCommunityDiarizationProvider(
        device="cuda",
        pipeline_factory=lambda _checkpoint: pipeline,
        module_loader=module_loader,
    )

    provider.diarize(
        DiarizationRequest(
            session_id=SESSION_ID,
            source_artifact=source_ref,
            source_id="system_audio",
            context=DiarizationContext(
                capture_profile="online_dual_stream",
                source_id="system_audio",
            ),
            audio_path=tmp_path / "system.wav",
        )
    )

    assert len(pipeline.devices) == 1
    assert isinstance(pipeline.devices[0], FakeTorchDevice)
    assert pipeline.devices[0].type == "cuda"


def test_pyannote_provider_rejects_unapplied_non_cpu_device(tmp_path: Path) -> None:
    source_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/source_audio/system.wav",
        artifact_type="source_audio",
        version=1,
        sha256="0" * 64,
    )
    provider = PyannoteCommunityDiarizationProvider(
        device="cuda",
        pipeline_factory=lambda _checkpoint: object(),
    )

    with pytest.raises(DiarizationProviderUnavailable, match="does not support moving"):
        provider.diarize(
            DiarizationRequest(
                session_id=SESSION_ID,
                source_artifact=source_ref,
                source_id="system_audio",
                context=DiarizationContext(
                    capture_profile="online_dual_stream",
                    source_id="system_audio",
                ),
                audio_path=tmp_path / "system.wav",
            )
        )


def test_pyannote_provider_rejects_non_callable_pipeline_to(tmp_path: Path) -> None:
    class FakePipeline:
        to = "cuda"

    source_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/source_audio/system.wav",
        artifact_type="source_audio",
        version=1,
        sha256="0" * 64,
    )
    provider = PyannoteCommunityDiarizationProvider(
        device="cuda",
        pipeline_factory=lambda _checkpoint: FakePipeline(),
    )

    with pytest.raises(DiarizationProviderUnavailable, match="non-callable to"):
        provider.diarize(
            DiarizationRequest(
                session_id=SESSION_ID,
                source_artifact=source_ref,
                source_id="system_audio",
                context=DiarizationContext(
                    capture_profile="online_dual_stream",
                    source_id="system_audio",
                ),
                audio_path=tmp_path / "system.wav",
            )
        )


def test_pyannote_provider_reports_missing_torch_for_device_move(tmp_path: Path) -> None:
    class FakePipeline:
        def to(self, _device: object) -> "FakePipeline":
            return self

    source_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/source_audio/system.wav",
        artifact_type="source_audio",
        version=1,
        sha256="0" * 64,
    )
    provider = PyannoteCommunityDiarizationProvider(
        device="cuda",
        pipeline_factory=lambda _checkpoint: FakePipeline(),
        module_loader=lambda _name: _raise_import_error(),
    )

    with pytest.raises(DiarizationProviderUnavailable, match="torch is not installed"):
        provider.diarize(
            DiarizationRequest(
                session_id=SESSION_ID,
                source_artifact=source_ref,
                source_id="system_audio",
                context=DiarizationContext(
                    capture_profile="online_dual_stream",
                    source_id="system_audio",
                ),
                audio_path=tmp_path / "system.wav",
            )
        )


def test_pyannote_provider_reports_broken_torch_import_for_device_move(tmp_path: Path) -> None:
    class FakePipeline:
        def to(self, _device: object) -> "FakePipeline":
            return self

    source_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/source_audio/system.wav",
        artifact_type="source_audio",
        version=1,
        sha256="0" * 64,
    )
    provider = PyannoteCommunityDiarizationProvider(
        device="cuda",
        pipeline_factory=lambda _checkpoint: FakePipeline(),
        module_loader=lambda _name: _raise_os_error(),
    )

    with pytest.raises(DiarizationProviderUnavailable, match="torch could not be imported"):
        provider.diarize(
            DiarizationRequest(
                session_id=SESSION_ID,
                source_artifact=source_ref,
                source_id="system_audio",
                context=DiarizationContext(
                    capture_profile="online_dual_stream",
                    source_id="system_audio",
                ),
                audio_path=tmp_path / "system.wav",
            )
        )


def test_pyannote_provider_reports_incompatible_torch_device(tmp_path: Path) -> None:
    class FakeTorch:
        device = "cuda"

    class FakePipeline:
        def to(self, _device: object) -> "FakePipeline":
            return self

    source_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/source_audio/system.wav",
        artifact_type="source_audio",
        version=1,
        sha256="0" * 64,
    )
    provider = PyannoteCommunityDiarizationProvider(
        device="cuda",
        pipeline_factory=lambda _checkpoint: FakePipeline(),
        module_loader=lambda _name: FakeTorch(),
    )

    with pytest.raises(DiarizationProviderUnavailable, match="torch.device is unavailable"):
        provider.diarize(
            DiarizationRequest(
                session_id=SESSION_ID,
                source_artifact=source_ref,
                source_id="system_audio",
                context=DiarizationContext(
                    capture_profile="online_dual_stream",
                    source_id="system_audio",
                ),
                audio_path=tmp_path / "system.wav",
            )
        )


def test_pyannote_provider_does_not_mask_factory_type_errors(tmp_path: Path) -> None:
    def pipeline_factory(_checkpoint: str, *, token: str) -> object:
        raise TypeError("internal construction error")

    source_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/source_audio/system.wav",
        artifact_type="source_audio",
        version=1,
        sha256="0" * 64,
    )
    provider = PyannoteCommunityDiarizationProvider(
        token="token",
        pipeline_factory=pipeline_factory,
    )

    with pytest.raises(DiarizationProviderUnavailable, match="internal construction error"):
        provider.diarize(
            DiarizationRequest(
                session_id=SESSION_ID,
                source_artifact=source_ref,
                source_id="system_audio",
                context=DiarizationContext(
                    capture_profile="online_dual_stream",
                    source_id="system_audio",
                ),
                audio_path=tmp_path / "system.wav",
            )
        )


def test_pyannote_provider_handles_no_token_factory_and_empty_output(tmp_path: Path) -> None:
    class EmptyAnnotation:
        def itertracks(self, *, yield_label: bool) -> list[tuple[object, str, str]]:
            assert yield_label is True
            return []

    class FakePipeline:
        def __call__(self, audio_path: str) -> EmptyAnnotation:
            return EmptyAnnotation()

    calls: list[tuple[object, ...]] = []

    def pipeline_factory(*args: object) -> FakePipeline:
        calls.append(args)
        return FakePipeline()

    source_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/source_audio/system.wav",
        artifact_type="source_audio",
        version=1,
        sha256="0" * 64,
    )
    provider = PyannoteCommunityDiarizationProvider(pipeline_factory=pipeline_factory)

    with pytest.raises(DiarizationError, match="no speaker turns"):
        provider.diarize(
            DiarizationRequest(
                session_id=SESSION_ID,
                source_artifact=source_ref,
                source_id="system_audio",
                context=DiarizationContext(
                    capture_profile="online_dual_stream",
                    source_id="system_audio",
                ),
                audio_path=tmp_path / "system.wav",
            )
        )

    assert calls == [("pyannote/speaker-diarization-community-1",)]


def test_pyannote_provider_rejects_invalid_itertracks_output(tmp_path: Path) -> None:
    class BadAnnotation:
        def itertracks(self, *, yield_label: bool) -> list[tuple[object, str]]:
            assert yield_label is True
            return [(object(), "track")]

    class FakePipeline:
        def __call__(self, audio_path: str) -> BadAnnotation:
            return BadAnnotation()

    source_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/source_audio/system.wav",
        artifact_type="source_audio",
        version=1,
        sha256="0" * 64,
    )
    provider = PyannoteCommunityDiarizationProvider(
        pipeline_factory=lambda *_args, **_kwargs: FakePipeline()
    )

    with pytest.raises(DiarizationContractError, match="itertracks"):
        provider.diarize(
            DiarizationRequest(
                session_id=SESSION_ID,
                source_artifact=source_ref,
                source_id="system_audio",
                context=DiarizationContext(
                    capture_profile="online_dual_stream",
                    source_id="system_audio",
                ),
                audio_path=tmp_path / "system.wav",
            )
        )


def _diarization_artifact(
    *,
    source_ref: ArtifactRef,
    kind: str = "regular_timeline",
    segments: tuple[DiarizationSegment, ...] | None = None,
) -> DiarizationArtifact:
    if segments is None:
        segments = (
            DiarizationSegment(
                id="turn_000001",
                speaker_id="speaker_1",
                start_seconds=0.0,
                end_seconds=1.0,
                overlapped=False,
            ),
        )
    return DiarizationArtifact(
        version=DIARIZATION_SCHEMA_VERSION,
        kind=kind,
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
            checkpoint="deterministic-fixture",
            device="test",
            parameters={},
            metadata={},
        ),
        speakers=(
            DiarizedSpeaker(
                id="speaker_1",
                label="Remote speaker 1",
                embedding=SpeakerEmbeddingRef.unavailable("fixture"),
            ),
        ),
        segments=segments,
        base_artifacts=(),
        overlay_entries=(),
        created_at="2026-05-17T10:00:00Z",
    )


def _raise_import_error() -> Any:
    raise ImportError("missing")


def _raise_os_error() -> Any:
    raise OSError("libtorch_cuda.so")


def _load_diarization_schema() -> dict[str, object]:
    schema_path = files("mena.schemas").joinpath("diarization.v1.schema.json")
    return cast(dict[str, object], json.loads(schema_path.read_text(encoding="utf-8")))
