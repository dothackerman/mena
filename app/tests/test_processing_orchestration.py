import json
import wave
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from mena.artifacts import FileSystemArtifactStore
from mena.diarization import DiarizationProviderUnavailable
from mena.interaction import ConfirmationRequest, ScriptedInteraction, ScriptedResponse
from mena.processing import ArtifactRef, ProcessingErrorInfo, ProcessingStageLedger
from mena.processing_orchestration import (
    CaptureSegmentInput,
    SourceAudioInput,
    _asr_language_from_session_language,
    _ensure_provider_prep_audio_artifact,
    _stitch_provider_prep_audio,
    collect_capture_segments,
    latest_failed_stage_input_artifacts,
    prepare_source_audio_inputs,
    run_processing_command,
    run_processing_stages,
    runtime_fallback_stage_for_provider_failure,
    validate_capture_segment_alignment,
)
from mena.profiles import get_capture_profile
from mena.runtime_profile import RuntimeFallbackUnavailable, RuntimeProfileSelection
from mena.transcription import TranscriptionProviderUnavailable

SESSION_ID = "00000000-0000-0000-0000-000000000001"
NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=UTC)


def _segment(source_id: str, index: int, *, status: str = "sealed") -> dict[str, object]:
    return {
        "source_id": source_id,
        "index": index,
        "path": f"{source_id}-{index}.wav",
        "status": status,
    }


def _write_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16_000)
        wav_file.writeframes(b"\0\0" * 16)


def _write_wav_with_rate(path: Path, sample_rate: int) -> None:
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"\0\0" * 16)


def _write_processing_session(tmp_path: Path) -> Path:
    local_path = tmp_path / "local_mic-1.wav"
    system_path = tmp_path / "system_audio-1.wav"
    _write_wav(local_path)
    _write_wav(system_path)
    session_record = {
        "id": SESSION_ID,
        "profile": "online_dual_stream",
        "language": "fr-CH",
        "capture_metadata": {
            "segments": [
                _segment("local_mic", 1),
                _segment("system_audio", 1),
            ]
        },
    }
    record_path = tmp_path / "session.json"
    record_path.write_text(json.dumps(session_record), encoding="utf-8")
    return tmp_path


class _FakeTranscription:
    name = "fake-transcription"

    def transcribe(self) -> None:
        raise AssertionError("not called")


class _FakeDiarization:
    name = "fake-diarization"
    config_id = "fake-diarization"

    def diarize(self) -> None:
        raise AssertionError("not called")


class _FakeSemantic:
    metadata = SimpleNamespace(config_id="fake-semantic", to_dict=lambda: {})

    def check(self) -> None:
        raise AssertionError("not called")


def _successful_processing_stages(**_kwargs: object) -> object:
    return SimpleNamespace(semantic_coherence_result=SimpleNamespace(skip_reason=None))


def test_run_processing_command_delegates_to_injected_stage_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import mena.processing_orchestration as orchestration

    session_path = _write_processing_session(tmp_path)
    monkeypatch.setattr(orchestration, "_requires_runtime_profile", lambda *_args: False)
    calls: list[dict[str, object]] = []

    def _stage_runner(**kwargs: object) -> object:
        calls.append(kwargs)
        return _successful_processing_stages(**kwargs)

    assert (
        run_processing_command(
            session_path=session_path,
            run_id="run-1",
            attempt=2,
            now=NOW,
            transcription_provider_factory=_FakeTranscription,
            diarization_provider_factory=_FakeDiarization,
            semantic_coherence_provider_factory=_FakeSemantic,
            processing_stages_runner=_stage_runner,
        )
        == 0
    )

    assert calls[0]["session_language"] == "fr-CH"
    source_inputs = cast(dict[str, object], calls[0]["source_inputs"])
    assert sorted(source_inputs) == ["local_mic", "system_audio"]
    assert "Processing completed for run 'run-1' (attempt 2)." in capsys.readouterr().out


def test_run_processing_command_authorizes_runtime_fallback_through_interaction(
    tmp_path: Path,
) -> None:
    session_path = _write_processing_session(tmp_path)
    fallback_evaluation = SimpleNamespace(profile=SimpleNamespace(id="fallback"))
    observed_authorization: list[bool] = []

    def _runtime_selection(**kwargs: object) -> RuntimeProfileSelection:
        authorizer = kwargs["authorize_runtime_fallback"]
        assert callable(authorizer)
        observed_authorization.append(authorizer("gpu missing", fallback_evaluation))
        return cast(
            RuntimeProfileSelection,
            SimpleNamespace(
                accepted=SimpleNamespace(),
                actual=SimpleNamespace(
                    selected_asr_profile=SimpleNamespace(
                        id="fake-asr",
                        checkpoint="tiny",
                        device="cpu",
                        compute_type="int8",
                        reason="test",
                    )
                ),
                fallback_used=True,
            ),
        )

    interaction = ScriptedInteraction(
        [ScriptedResponse(kind="confirm", topic="runtime_fallback.one_run", value=True)]
    )

    assert (
        run_processing_command(
            session_path=session_path,
            run_id="run-1",
            attempt=1,
            now=NOW,
            diarization_provider_factory=_FakeDiarization,
            semantic_coherence_provider_factory=_FakeSemantic,
            interaction=interaction,
            runtime_selection_fn=_runtime_selection,
            runtime_profile_provenance_store_fn=lambda **_kwargs: None,
            processing_stages_runner=_successful_processing_stages,
        )
        == 0
    )

    assert observed_authorization == [True]
    assert interaction.pending == 0
    request = interaction.requests[0]
    assert isinstance(request, ConfirmationRequest)
    assert request.topic == "runtime_fallback.one_run"
    assert request.default is False
    assert request.context["accepted_failure"] == "gpu missing"
    assert request.context["fallback_evaluation"] is fallback_evaluation


def test_run_processing_command_declines_runtime_fallback_through_interaction(
    tmp_path: Path,
) -> None:
    session_path = _write_processing_session(tmp_path)
    fallback_evaluation = SimpleNamespace(profile=SimpleNamespace(id="fallback"))

    def _runtime_selection(**kwargs: object) -> RuntimeProfileSelection:
        authorizer = kwargs["authorize_runtime_fallback"]
        assert callable(authorizer)
        if not authorizer("gpu missing", fallback_evaluation):
            raise RuntimeFallbackUnavailable(
                "Accepted runtime profile is unavailable and fallback was declined."
            )
        raise AssertionError("fallback should have been declined")

    interaction = ScriptedInteraction(
        [ScriptedResponse(kind="confirm", topic="runtime_fallback.one_run", value=False)]
    )

    with pytest.raises(RuntimeFallbackUnavailable, match="fallback was declined"):
        run_processing_command(
            session_path=session_path,
            run_id="run-1",
            attempt=1,
            now=NOW,
            diarization_provider_factory=_FakeDiarization,
            semantic_coherence_provider_factory=_FakeSemantic,
            interaction=interaction,
            runtime_selection_fn=_runtime_selection,
            runtime_profile_provenance_store_fn=lambda **_kwargs: None,
            processing_stages_runner=_successful_processing_stages,
        )

    assert interaction.pending == 0
    assert interaction.requests[0].topic == "runtime_fallback.one_run"


def test_run_processing_stages_sequences_room_mic_profile(tmp_path: Path) -> None:
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id="run-1")
    profile = get_capture_profile("in_person_single_mic")
    source_artifact = store.store(
        artifact_type="source_audio",
        name="source_audio/room_mic_000001.wav",
        data=b"audio",
    )
    source_inputs = {
        "room_mic": SourceAudioInput(
            source_id="room_mic",
            index=1,
            path=tmp_path / source_artifact.path,
            artifact=source_artifact,
        )
    }
    calls: list[tuple[str, dict[str, object]]] = []

    def _transcription_stage(**kwargs: object) -> object:
        calls.append(("transcription", kwargs))
        transcript = store.store(
            artifact_type="transcript",
            name="transcripts/room_mic.json",
            data=b"{}",
        )
        return SimpleNamespace(transcript_record=transcript)

    def _diarization_stage(**kwargs: object) -> object:
        calls.append(("diarization", kwargs))
        regular = store.store(
            artifact_type="diarization",
            name="diarization/regular.json",
            data=b"{}",
        )
        exclusive = store.store(
            artifact_type="diarization",
            name="diarization/exclusive.json",
            data=b"{}",
        )
        return SimpleNamespace(
            skipped=False,
            artifacts=(
                SimpleNamespace(kind="regular_timeline"),
                SimpleNamespace(kind="exclusive_projection"),
            ),
            artifact_records=(regular, exclusive),
        )

    def _room_merge_stage(**kwargs: object) -> object:
        calls.append(("room_merge", kwargs))
        merged = store.store(
            artifact_type="merged_transcript",
            name="merged/room.json",
            data=b"{}",
        )
        return SimpleNamespace(merged_transcript_record=merged)

    def _confidence_stage(**kwargs: object) -> object:
        calls.append(("confidence", kwargs))
        return None

    def _semantic_stage(**kwargs: object) -> object:
        calls.append(("semantic", kwargs))
        return SimpleNamespace(skip_reason="provider_unavailable")

    result = run_processing_stages(
        session_id=SESSION_ID,
        profile=profile,
        source_inputs=source_inputs,
        store=store,
        ledger=ledger,
        attempt=1,
        now=NOW,
        transcription_provider=SimpleNamespace(name="transcription"),
        diarization_provider=SimpleNamespace(name="diarization", config_id="diarization"),
        semantic_coherence_provider=SimpleNamespace(metadata=SimpleNamespace(config_id="semantic")),
        session_language="it-CH",
        transcription_stage_runner=_transcription_stage,
        diarization_stage_runner=_diarization_stage,
        room_mic_merge_timing_stage_runner=_room_merge_stage,
        confidence_flagging_stage_runner=_confidence_stage,
        semantic_coherence_stage_runner=_semantic_stage,
    )

    assert [name for name, _kwargs in calls] == [
        "transcription",
        "diarization",
        "room_merge",
        "confidence",
        "semantic",
    ]
    assert calls[0][1]["language"] == "it"
    assert calls[2][1]["room_regular_diarization_record"] is not None
    assert calls[2][1]["room_exclusive_diarization_record"] is not None
    assert result.semantic_coherence_result.skip_reason == "provider_unavailable"


def test_prepare_source_audio_inputs_stitches_multiple_segments(tmp_path: Path) -> None:
    first_path = tmp_path / "room_mic-1.wav"
    second_path = tmp_path / "room_mic-2.wav"
    _write_wav(first_path)
    _write_wav(second_path)
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    profile = get_capture_profile("in_person_single_mic")
    session_record: dict[str, object] = {
        "capture_metadata": {
            "segments": [
                {
                    **_segment("room_mic", 1),
                    "route_id": "room",
                    "route_label": "Room",
                    "capture_mode": "in_person",
                },
                {
                    **_segment("room_mic", 2),
                    "route_id": "room",
                    "route_label": "Room",
                    "capture_mode": "in_person",
                },
            ]
        }
    }

    inputs = prepare_source_audio_inputs(
        session_dir=tmp_path,
        profile=profile,
        session_record=session_record,
        store=store,
    )

    source = inputs["room_mic"]
    assert source.index == 1
    assert source.artifact.artifact_type == "provider_prep_audio"
    assert source.path.exists()
    prep_records = store.list(artifact_type="provider_prep_audio")
    assert len(prep_records) == 1
    assert prep_records[0].metadata["sample_rate"] == 16_000
    assert len(cast(list[object], prep_records[0].metadata["derived_from_segments"])) == 2


def test_prepare_source_audio_inputs_rejects_source_artifact_collision(
    tmp_path: Path,
) -> None:
    segment_path = tmp_path / "room_mic-1.wav"
    _write_wav(segment_path)
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    profile = get_capture_profile("in_person_single_mic")
    session_record: dict[str, object] = {
        "capture_metadata": {"segments": [_segment("room_mic", 1)]}
    }
    prepare_source_audio_inputs(
        session_dir=tmp_path,
        profile=profile,
        session_record=session_record,
        store=store,
    )
    segment_path.write_bytes(b"changed")

    with pytest.raises(ValueError, match="checksum has changed"):
        prepare_source_audio_inputs(
            session_dir=tmp_path,
            profile=profile,
            session_record=session_record,
            store=store,
        )


def test_provider_prep_helpers_reject_incompatible_or_changed_audio(
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "room_mic-1.wav"
    second_path = tmp_path / "room_mic-2.wav"
    _write_wav_with_rate(first_path, 16_000)
    _write_wav_with_rate(second_path, 8_000)
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    first_artifact = store.store(
        artifact_type="source_audio",
        name="source_audio/room_mic_000001.wav",
        data=first_path.read_bytes(),
    )
    second_artifact = store.store(
        artifact_type="source_audio",
        name="source_audio/room_mic_000002.wav",
        data=second_path.read_bytes(),
    )
    segments = (
        CaptureSegmentInput(
            source_id="room_mic",
            index=1,
            path=first_path,
            artifact=first_artifact,
            sha256=first_artifact.sha256,
            route_id=None,
            route_label=None,
            capture_mode=None,
        ),
        CaptureSegmentInput(
            source_id="room_mic",
            index=2,
            path=second_path,
            artifact=second_artifact,
            sha256=second_artifact.sha256,
            route_id=None,
            route_label=None,
            capture_mode=None,
        ),
    )

    with pytest.raises(ValueError, match="incompatible WAV format"):
        _stitch_provider_prep_audio(
            source_id="room_mic",
            segments=segments,
            output_path=tmp_path / "prep.wav",
        )

    existing_path = tmp_path / "existing.wav"
    _write_wav(existing_path)
    existing = store.store_file(
        artifact_type="provider_prep_audio",
        name="provider-prep/room_mic.wav",
        source_path=existing_path,
        metadata={"source_id": "room_mic"},
    )
    changed_path = tmp_path / "changed.wav"
    changed_path.write_bytes(b"changed")

    with pytest.raises(ValueError, match="prepared audio bytes changed"):
        _ensure_provider_prep_audio_artifact(
            store=store,
            source_id="room_mic",
            source_path=changed_path,
            artifact_sha="1" * 64,
            metadata={"source_id": "room_mic"},
        )
    assert existing.artifact_type == "provider_prep_audio"


@pytest.mark.parametrize(
    ("extra", "match"),
    [
        ({"route_id": 1}, "route_id must be a string"),
        ({"route_label": 1}, "route_label must be a string"),
        ({"capture_mode": 1}, "capture_mode must be a string"),
    ],
)
def test_collect_capture_segments_rejects_invalid_optional_metadata(
    extra: dict[str, object],
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        collect_capture_segments(
            {"capture_metadata": {"segments": [{**_segment("local_mic", 1), **extra}]}},
            ("local_mic",),
        )


def test_collect_capture_segments_orders_and_validates_expected_sources() -> None:
    session_record: dict[str, object] = {
        "capture_metadata": {
            "segments": [
                _segment("system_audio", 2),
                _segment("local_mic", 2),
                _segment("system_audio", 1),
                _segment("local_mic", 1),
            ]
        }
    }

    selected = collect_capture_segments(session_record, ("local_mic", "system_audio"))

    assert [segment["index"] for segment in selected["local_mic"]] == [1, 2]
    assert [segment["index"] for segment in selected["system_audio"]] == [1, 2]


@pytest.mark.parametrize(
    ("session_record", "match"),
    [
        ({}, "capture metadata"),
        ({"capture_metadata": {}}, "does not include segments"),
        ({"capture_metadata": {"segments": ["bad"]}}, "must be an object"),
        (
            {"capture_metadata": {"segments": [_segment("unexpected", 1)]}},
            "unexpected capture source",
        ),
        (
            {"capture_metadata": {"segments": [_segment("local_mic", 1, status="open")]}},
            "not sealed",
        ),
        (
            {
                "capture_metadata": {
                    "segments": [
                        {**_segment("local_mic", 1), "sha256": 1},
                    ]
                }
            },
            "sha256 must be a string",
        ),
        (
            {
                "capture_metadata": {
                    "segments": [
                        _segment("local_mic", 1),
                        _segment("local_mic", 1),
                    ]
                }
            },
            "duplicate",
        ),
        (
            {"capture_metadata": {"segments": [_segment("local_mic", 2)]}},
            "contiguous",
        ),
    ],
)
def test_collect_capture_segments_rejects_invalid_metadata(
    session_record: dict[str, object],
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        collect_capture_segments(session_record, ("local_mic",))


def test_validate_capture_segment_alignment_rejects_unpaired_online_segments() -> None:
    selected = {
        "local_mic": (_segment("local_mic", 1), _segment("local_mic", 2)),
        "system_audio": (_segment("system_audio", 1),),
    }

    with pytest.raises(ValueError, match="must be paired"):
        validate_capture_segment_alignment(selected, ("local_mic", "system_audio"))


def test_processing_orchestration_small_helpers_cover_runtime_decisions(
    tmp_path: Path,
) -> None:
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id="run-1")
    source = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/source_audio/local.wav",
        artifact_type="source_audio",
        version=1,
        sha256="0" * 64,
    )
    ledger.mark_failed(
        stage="transcription",
        input_artifacts=(source,),
        error=ProcessingErrorInfo(message="temporary", retryable=True, code="gpu"),
        now=NOW,
    )

    assert _asr_language_from_session_language("de-CH") == "de"
    assert _asr_language_from_session_language("") is None
    assert latest_failed_stage_input_artifacts(ledger=ledger, stage="transcription") == (source.id,)
    assert latest_failed_stage_input_artifacts(ledger=ledger, stage="diarization") == ()
    assert (
        runtime_fallback_stage_for_provider_failure(
            TranscriptionProviderUnavailable("cuda unavailable")
        )
        == "transcription"
    )
    assert (
        runtime_fallback_stage_for_provider_failure(
            DiarizationProviderUnavailable("cuda unavailable")
        )
        == "diarization"
    )
