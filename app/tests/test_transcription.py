import json
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from mena.artifacts import FileSystemArtifactStore, InMemoryArtifactStore
from mena.processing import ArtifactRef, ProcessingStageLedger
from mena.transcription import (
    TRANSCRIPT_ARTIFACT_TYPE,
    TRANSCRIPT_SCHEMA_VERSION,
    TRANSCRIPTION_STAGE,
    FakeTranscriptionProvider,
    FasterWhisperDecodeOptions,
    FasterWhisperModelProfile,
    FasterWhisperTranscriptionProvider,
    HardwareSnapshot,
    TranscriptArtifact,
    TranscriptionContractError,
    TranscriptionInputError,
    TranscriptionProviderUnavailable,
    TranscriptionRequest,
    TranscriptLanguage,
    TranscriptProviderMetadata,
    TranscriptSegment,
    TranscriptWord,
    persist_transcript_artifact,
    run_transcription_stage,
    select_faster_whisper_profile,
)

SESSION_ID = "00000000-0000-0000-0000-000000000001"
RUN_ID = "run-20260514"
NOW = datetime(2026, 5, 14, 18, 0, 0, tzinfo=UTC)


def test_fake_transcription_stage_persists_transcript_and_completed_checkpoint(
    tmp_path: Path,
) -> None:
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    source = store.store(artifact_type="source_audio", name="local_mic.wav", data=b"audio")
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)
    provider = FakeTranscriptionProvider()

    result = run_transcription_stage(
        store=store,
        ledger=ledger,
        provider=provider,
        source_artifact=source,
        source_id="local_mic",
        language="en",
        hotwords=("Mena",),
        now=NOW,
    )

    assert result.skipped is False
    assert result.state.status == "completed"
    assert result.transcript_record.artifact_type == TRANSCRIPT_ARTIFACT_TYPE
    assert result.transcript_record.metadata == {
        "category": "derived",
        "contract": "transcript.v1",
        "provider": "fake",
        "checkpoint": "deterministic-fixture",
        "source_artifact_id": source.id,
        "source_id": "local_mic",
    }
    payload = json.loads(store.retrieve(result.transcript_record.id))
    Draft202012Validator(_load_transcript_schema(), format_checker=FormatChecker()).validate(
        payload
    )
    transcript = TranscriptArtifact.from_dict(payload)
    assert transcript.text == "hello world"
    assert transcript.source_artifact == ArtifactRef.from_record(source)
    assert transcript.created_at == "2026-05-14T18:00:00Z"
    assert [state.status for state in ledger.states(stage=TRANSCRIPTION_STAGE)] == [
        "running",
        "completed",
    ]
    assert result.state.output_artifacts == (ArtifactRef.from_record(result.transcript_record),)
    assert provider.calls[0].hotwords == ("Mena",)


def test_transcription_stage_reuses_verified_completed_output(tmp_path: Path) -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    source = store.store(artifact_type="source_audio", name="local_mic.wav", data=b"audio")
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    first = run_transcription_stage(
        store=store,
        ledger=ledger,
        provider=FakeTranscriptionProvider(),
        source_artifact=source,
        source_id="local_mic",
        now=NOW,
    )
    second_provider = FakeTranscriptionProvider()
    second = run_transcription_stage(
        store=store,
        ledger=ledger,
        provider=second_provider,
        source_artifact=source,
        source_id="local_mic",
        now=NOW,
    )

    assert second.skipped is True
    assert second.transcript_record == first.transcript_record
    assert second_provider.calls == []


def test_transcription_stage_does_not_reuse_completed_output_for_changed_language(
    tmp_path: Path,
) -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    source = store.store(artifact_type="source_audio", name="local_mic.wav", data=b"audio")
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    first = run_transcription_stage(
        store=store,
        ledger=ledger,
        provider=FakeTranscriptionProvider(),
        source_artifact=source,
        source_id="local_mic",
        language="en",
        now=NOW,
    )
    second_provider = FakeTranscriptionProvider()
    second = run_transcription_stage(
        store=store,
        ledger=ledger,
        provider=second_provider,
        source_artifact=source,
        source_id="local_mic",
        language="de-CH",
        now=NOW,
    )

    assert first.transcript.language.requested == "en"
    assert second.skipped is False
    assert second.transcript_record != first.transcript_record
    assert "/language-de-ch/" in second.transcript_record.path
    assert second.transcript.language.requested == "de-CH"
    assert second_provider.calls[0].language == "de-CH"


def test_transcription_stage_reuse_is_scoped_to_matching_input_artifact(
    tmp_path: Path,
) -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    local_source = store.store(artifact_type="source_audio", name="local_mic.wav", data=b"local")
    remote_source = store.store(
        artifact_type="source_audio", name="system_audio.wav", data=b"remote"
    )
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    local_first = run_transcription_stage(
        store=store,
        ledger=ledger,
        provider=FakeTranscriptionProvider(),
        source_artifact=local_source,
        source_id="local_mic",
        now=NOW,
    )
    remote_provider = FakeTranscriptionProvider(
        segments=(
            TranscriptSegment(
                id="segment_000001",
                text="remote audio",
                start_seconds=0.0,
                end_seconds=1.0,
            ),
        )
    )
    remote = run_transcription_stage(
        store=store,
        ledger=ledger,
        provider=remote_provider,
        source_artifact=remote_source,
        source_id="system_audio",
        now=NOW,
    )
    local_second_provider = FakeTranscriptionProvider()
    local_second = run_transcription_stage(
        store=store,
        ledger=ledger,
        provider=local_second_provider,
        source_artifact=local_source,
        source_id="local_mic",
        now=NOW,
    )

    assert remote.skipped is False
    assert remote_provider.calls[0].source_artifact == ArtifactRef.from_record(remote_source)
    assert local_second.skipped is True
    assert local_second.transcript_record == local_first.transcript_record
    assert local_second_provider.calls == []


def test_transcription_stage_fails_before_provider_when_source_artifact_is_corrupt(
    tmp_path: Path,
) -> None:
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    source = store.store(artifact_type="source_audio", name="local_mic.wav", data=b"audio")
    (tmp_path / source.path).write_bytes(b"changed")
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)
    provider = FakeTranscriptionProvider()

    with pytest.raises(TranscriptionInputError, match="artifact content"):
        run_transcription_stage(
            store=store,
            ledger=ledger,
            provider=provider,
            source_artifact=source,
            source_id="local_mic",
            now=NOW,
        )

    assert provider.calls == []
    latest = ledger.latest(TRANSCRIPTION_STAGE)
    assert latest is not None
    assert latest.status == "failed"
    assert latest.error is not None
    assert latest.error.code == "input_artifact_invalid"
    assert store.list(artifact_type=TRANSCRIPT_ARTIFACT_TYPE) == ()


def test_persist_transcript_artifact_validates_contract_before_writing() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    source = store.store(artifact_type="source_audio", name="room.wav", data=b"audio")
    transcript = _transcript(source_ref=ArtifactRef.from_record(source), segments=())

    with pytest.raises(TranscriptionContractError, match="at least one segment"):
        persist_transcript_artifact(store=store, transcript=transcript, name="room.json")

    assert store.list(artifact_type=TRANSCRIPT_ARTIFACT_TYPE) == ()


def test_transcript_schema_rejects_incomplete_provider_metadata() -> None:
    source_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/source_audio/local.wav",
        artifact_type="source_audio",
        version=1,
        sha256="0" * 64,
    )
    payload = _transcript(source_ref=source_ref).to_dict()
    del cast(dict[str, object], payload["provider"])["checkpoint"]
    validator = Draft202012Validator(_load_transcript_schema(), format_checker=FormatChecker())

    with pytest.raises(ValidationError, match="'checkpoint' is a required property"):
        validator.validate(payload)


def test_transcript_schema_is_valid() -> None:
    Draft202012Validator.check_schema(_load_transcript_schema())


def test_transcript_contract_rejects_bad_timing_and_probability() -> None:
    source_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/source_audio/local.wav",
        artifact_type="source_audio",
        version=1,
        sha256="0" * 64,
    )
    payload = _transcript(source_ref=source_ref).to_dict()
    cast(list[dict[str, object]], payload["segments"])[0]["end_seconds"] = -1

    with pytest.raises(TranscriptionContractError, match="timestamps"):
        TranscriptArtifact.from_dict(payload)

    payload = _transcript(source_ref=source_ref).to_dict()
    cast(dict[str, object], payload["language"])["detection_probability"] = 2

    with pytest.raises(TranscriptionContractError, match="probability"):
        TranscriptArtifact.from_dict(payload)


def test_select_faster_whisper_profile_uses_quality_ceiling_only_for_strong_cuda() -> None:
    assert (
        select_faster_whisper_profile(
            HardwareSnapshot(cuda_available=True, vram_gb=12, ram_gb=32)
        ).checkpoint
        == "large-v3-turbo"
    )
    assert (
        select_faster_whisper_profile(
            HardwareSnapshot(cuda_available=True, vram_gb=6, ram_gb=16)
        ).id
        == "cuda_balanced"
    )
    assert select_faster_whisper_profile(HardwareSnapshot(ram_gb=8)).id == "cpu_minimal"
    assert (
        select_faster_whisper_profile(HardwareSnapshot(ram_gb=16, avx2_available=False)).id
        == "cpu_minimal_no_avx2"
    )


def test_faster_whisper_provider_reports_missing_optional_dependency(tmp_path: Path) -> None:
    source_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/source_audio/local.wav",
        artifact_type="source_audio",
        version=1,
        sha256="0" * 64,
    )
    provider = FasterWhisperTranscriptionProvider(
        module_loader=lambda _name: _raise_import_error(),
    )

    with pytest.raises(TranscriptionProviderUnavailable, match="not installed"):
        provider.transcribe(
            TranscriptionRequest(
                session_id=SESSION_ID,
                source_artifact=source_ref,
                audio_path=tmp_path / "local.wav",
            )
        )


def test_faster_whisper_provider_maps_segments_and_persists_decode_parameters(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    class FakeWord:
        word = " hello"
        start = 0.0
        end = 0.5
        probability = 0.97

    class FakeSegment:
        text = " hello"
        start = 0.0
        end = 0.5
        avg_logprob = -0.1
        no_speech_prob = 0.02
        compression_ratio = 1.1
        words = [FakeWord()]

    class FakeInfo:
        language = "en"
        language_probability = 0.99

    class FakeModel:
        def transcribe(
            self, audio_path: str, **kwargs: object
        ) -> tuple[list[FakeSegment], FakeInfo]:
            calls.append({"audio_path": audio_path, **kwargs})
            return [FakeSegment()], FakeInfo()

    def model_factory(*args: object, **kwargs: object) -> FakeModel:
        calls.append({"model_args": list(args), "model_kwargs": kwargs})
        return FakeModel()

    source_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/source_audio/local.wav",
        artifact_type="source_audio",
        version=1,
        sha256="0" * 64,
    )
    provider = FasterWhisperTranscriptionProvider(
        profile=FasterWhisperModelProfile(
            id="test_profile",
            checkpoint="tiny",
            device="cpu",
            compute_type="int8",
            reason="test",
        ),
        decode_options=FasterWhisperDecodeOptions(beam_size=3),
        model_factory=model_factory,
        local_files_only=True,
    )

    transcript = provider.transcribe(
        TranscriptionRequest(
            session_id=SESSION_ID,
            source_artifact=source_ref,
            source_id="local_mic",
            audio_path=tmp_path / "local.wav",
            language="en",
            initial_prompt="Mena vocabulary",
            hotwords=("Oriol",),
            created_at="2026-05-14T18:00:00Z",
        )
    )

    assert calls[0] == {
        "model_args": ["tiny"],
        "model_kwargs": {"device": "cpu", "compute_type": "int8", "local_files_only": True},
    }
    assert calls[1]["language"] == "en"
    assert calls[1]["initial_prompt"] == "Mena vocabulary"
    assert calls[1]["hotwords"] == ["Oriol"]
    assert calls[1]["condition_on_previous_text"] is False
    assert calls[1]["hallucination_silence_threshold"] == 1.0
    assert calls[1]["beam_size"] == 3
    assert transcript.text == "hello"
    assert transcript.provider.name == "faster-whisper"
    assert transcript.provider.decode_parameters["hotwords"] == ["Oriol"]
    assert transcript.language.detected == "en"
    assert transcript.segments[0].words[0].text == "hello"


def _transcript(
    *,
    source_ref: ArtifactRef,
    segments: tuple[TranscriptSegment, ...] | None = None,
) -> TranscriptArtifact:
    if segments is None:
        segments = (
            TranscriptSegment(
                id="segment_000001",
                text="hello",
                start_seconds=0.0,
                end_seconds=0.5,
                words=(
                    TranscriptWord(
                        text="hello",
                        start_seconds=0.0,
                        end_seconds=0.5,
                        probability=0.99,
                    ),
                ),
            ),
        )
    return TranscriptArtifact(
        version=TRANSCRIPT_SCHEMA_VERSION,
        session_id=SESSION_ID,
        source_artifact=source_ref,
        source_id="local_mic",
        provider=TranscriptProviderMetadata(
            name="fake",
            checkpoint="deterministic-fixture",
            quantization="none",
            device="test",
            timestamp_mode="word",
            decode_parameters={},
            metadata={},
        ),
        language=TranscriptLanguage(requested="en", detected="en", detection_probability=1.0),
        text="hello",
        segments=segments,
        created_at="2026-05-14T18:00:00Z",
    )


def _raise_import_error() -> Any:
    raise ImportError("missing")


def _load_transcript_schema() -> dict[str, object]:
    schema_path = files("mena.schemas").joinpath("transcript.v1.schema.json")
    return cast(dict[str, object], json.loads(schema_path.read_text(encoding="utf-8")))
