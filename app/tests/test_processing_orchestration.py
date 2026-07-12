import json
import wave
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from mena.artifacts import FileSystemArtifactStore
from mena.diarization import DiarizationProviderUnavailable, FakeDiarizationProvider
from mena.interaction import (
    ChoiceOption,
    ChoiceRequest,
    ConfirmationRequest,
    TextRequest,
    WorkflowNotice,
)
from mena.merge_timing import MergePolicy
from mena.processing import ArtifactRef, ProcessingErrorInfo, ProcessingStageLedger
from mena.processing_orchestration import (
    CaptureSegmentInput,
    SourceAudioInput,
    _asr_language_from_session_language,
    _ensure_provider_prep_audio_artifact,
    _merge_policy_for_profile,
    _stitch_provider_prep_audio,
    collect_capture_segments,
    latest_failed_stage_input_artifacts,
    prepare_source_audio_inputs,
    run_processing_command,
    run_processing_stages,
    run_resume_processing_command,
    runtime_fallback_stage_for_provider_failure,
    validate_capture_segment_alignment,
)
from mena.profiles import get_capture_profile
from mena.runtime_profile import RuntimeFallbackUnavailable
from mena.runtime_stewardship import (
    RUNTIME_STEWARDSHIP_RELEASE_NOTICE_TOPIC,
    RuntimeLease,
    RuntimeReleaseResult,
)
from mena.semantic_coherence import FakeSemanticCoherenceProvider, SemanticCoherenceResult
from mena.session_lock import SessionLockContention, acquire_session_lock
from mena.settings import AppSettings
from mena.setup import (
    CommandIdentity,
    CommandRun,
    HardwareFacts,
    build_runtime_profile_state,
    default_profile_catalog,
    detect_local_installation_state,
    evaluate_setup,
    get_alpha_local_profile,
    persist_runtime_profile_state,
)
from mena.transcription import (
    FakeTranscriptionProvider,
    TranscriptArtifact,
    TranscriptionProviderUnavailable,
    TranscriptionRequest,
)

SESSION_ID = "00000000-0000-0000-0000-000000000001"
NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=UTC)
COMMAND_IDENTITY = CommandIdentity(
    current_executable=Path("/home/user/.local/bin/mena"),
    plain_mena_path=Path("/home/user/.local/bin/mena"),
    version="0.1.0-test",
    invocation_mode="installed-product",
)
READY_HARDWARE = HardwareFacts(
    system="Linux",
    ram_gb=32.0,
    cuda_available=True,
    vram_gb=12.0,
    avx2_available=True,
)
CPU_FALLBACK_HARDWARE = HardwareFacts(
    system="Linux",
    ram_gb=32.0,
    cuda_available=False,
    vram_gb=None,
    avx2_available=True,
)


class _ReadySetupRunner:
    def __init__(self, *, ollama_model: str) -> None:
        self._ollama_model = ollama_model
        self.which_calls: list[str] = []
        self.run_calls: list[tuple[str, ...]] = []

    def reset(self) -> None:
        self.which_calls.clear()
        self.run_calls.clear()

    def which(self, command: str) -> Path | None:
        self.which_calls.append(command)
        return Path("/usr/bin") / command

    def run(
        self,
        args: Sequence[str],
        *,
        timeout_seconds: float = 30.0,
        env: Mapping[str, str] | None = None,
    ) -> CommandRun:
        command = tuple(args)
        self.run_calls.append(command)
        if command == ("ollama", "list"):
            return CommandRun(
                args=command,
                exit_code=0,
                stdout=f"NAME ID SIZE MODIFIED\n{self._ollama_model} abc 1 GB now\n",
            )
        return CommandRun(args=command, exit_code=0)


class _NonInteractiveProcessingInteraction:
    runtime_fallback_confirmation_available = False

    def __init__(self) -> None:
        self.notices: list[WorkflowNotice] = []

    def choose(self, request: ChoiceRequest) -> ChoiceOption:
        raise AssertionError("processing workflow should not request choices")

    def confirm(self, request: ConfirmationRequest) -> bool:
        raise AssertionError("non-interactive processing workflow should not confirm fallback")

    def request_text(self, request: TextRequest) -> str:
        raise AssertionError("processing workflow should not request text")

    def notify(self, notice: WorkflowNotice) -> None:
        self.notices.append(notice)


class _ConfirmingProcessingInteraction:
    runtime_fallback_confirmation_available = True

    def __init__(self, response: bool) -> None:
        self.response = response
        self.confirmations: list[ConfirmationRequest] = []
        self.notices: list[WorkflowNotice] = []

    def choose(self, request: ChoiceRequest) -> ChoiceOption:
        raise AssertionError("processing workflow should not request choices")

    def confirm(self, request: ConfirmationRequest) -> bool:
        self.confirmations.append(request)
        return self.response

    def request_text(self, request: TextRequest) -> str:
        raise AssertionError("processing workflow should not request text")

    def notify(self, notice: WorkflowNotice) -> None:
        self.notices.append(notice)


class _UnavailableTranscriptionProvider(FakeTranscriptionProvider):
    def transcribe(self, request: TranscriptionRequest) -> TranscriptArtifact:
        raise TranscriptionProviderUnavailable("CUDA out of memory")


class _ReleasingSemanticProvider(FakeSemanticCoherenceProvider):
    runtime_resource_used = True

    def __init__(
        self,
        *,
        results: Mapping[str, SemanticCoherenceResult],
        release_status: str = "released",
        release_detail: str = "Ollama model release completed: unloaded.",
    ) -> None:
        super().__init__(results=results)
        self._release_status = release_status
        self._release_detail = release_detail

    def release_runtime_resource(self, lease: RuntimeLease) -> RuntimeReleaseResult:
        return RuntimeReleaseResult(
            lease=lease,
            status=self._release_status,
            detail=self._release_detail,
        )


class _RuntimeRequiredProviderFactory:
    requires_runtime_profile = True

    def __init__(self, provider_factory: Callable[[], object]) -> None:
        self._provider_factory = provider_factory

    def __call__(self) -> object:
        return self._provider_factory()


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


def _persist_runtime_profile_state(
    settings: AppSettings,
    *,
    runner: _ReadySetupRunner,
    authorize_local_fallback: bool,
) -> None:
    primary_profile = get_alpha_local_profile(settings.local_llm.ollama_model)
    fallback_profile = default_profile_catalog(settings.local_llm.ollama_model)[1]
    setup_state = detect_local_installation_state(
        settings,
        runner=runner,
        command_identity=COMMAND_IDENTITY,
        hardware=READY_HARDWARE,
    )
    fallback_state = detect_local_installation_state(
        settings,
        runner=runner,
        command_identity=COMMAND_IDENTITY,
        hardware=READY_HARDWARE,
        profile=fallback_profile,
    )
    persist_runtime_profile_state(
        build_runtime_profile_state(
            evaluate_setup(primary_profile, setup_state),
            fallback_evaluation=evaluate_setup(fallback_profile, fallback_state),
            authorize_local_fallback=authorize_local_fallback,
        ),
        paths=settings.paths,
    )


def test_run_processing_command_runs_pipeline_with_provider_fakes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session_path = _write_processing_session(tmp_path)

    assert (
        run_processing_command(
            session_path=session_path,
            run_id="run-1",
            attempt=2,
            now=NOW,
            transcription_provider_factory=FakeTranscriptionProvider,
            diarization_provider_factory=FakeDiarizationProvider,
            semantic_coherence_provider_factory=lambda: FakeSemanticCoherenceProvider(results={}),
        )
        == 0
    )

    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=session_path)
    assert len(store.list(artifact_type="transcript")) == 2
    merged_records = store.list(artifact_type="merged_transcript")
    assert len(merged_records) == 1
    merged_record = merged_records[0]
    assert merged_record.metadata["policy_config_id"] == MergePolicy().config_id
    assert merged_record.metadata["local_source_id"] == "local_mic"
    assert merged_record.metadata["remote_source_id"] == "system_audio"
    assert merged_record.metadata["remote_diarization_status"] == "used"
    merged_payload = json.loads(store.retrieve(merged_record.id))
    assert merged_payload["policy"]["capture_profile"] == "online_dual_stream"
    assert [source["source_id"] for source in merged_payload["sources"]] == [
        "local_mic",
        "system_audio",
    ]
    assert len(store.list(artifact_type="review_queue")) == 2
    assert "Processing completed for run 'run-1' (attempt 2)." in capsys.readouterr().out


def test_run_processing_command_rejects_locked_session_bundle(tmp_path: Path) -> None:
    session_path = _write_processing_session(tmp_path)

    with (
        acquire_session_lock(session_dir=session_path, command="summarize"),
        pytest.raises(SessionLockContention, match="command=summarize"),
    ):
        run_processing_command(
            session_path=session_path,
            run_id="run-locked",
            attempt=1,
            now=NOW,
            transcription_provider_factory=FakeTranscriptionProvider,
            diarization_provider_factory=FakeDiarizationProvider,
            semantic_coherence_provider_factory=lambda: FakeSemanticCoherenceProvider(results={}),
        )


def test_run_resume_processing_command_rejects_locked_session_bundle(tmp_path: Path) -> None:
    session_path = _write_processing_session(tmp_path)

    with (
        acquire_session_lock(session_dir=session_path, command="process"),
        pytest.raises(SessionLockContention, match="command=process"),
    ):
        run_resume_processing_command(
            session_path=session_path,
            now=NOW,
            transcription_provider_factory=FakeTranscriptionProvider,
            diarization_provider_factory=FakeDiarizationProvider,
            semantic_coherence_provider_factory=lambda: FakeSemanticCoherenceProvider(results={}),
        )


def test_run_processing_command_preserves_noninteractive_fallback_guidance(
    tmp_path: Path,
) -> None:
    session_path = _write_processing_session(tmp_path)
    settings = AppSettings.from_env(env={}, home=tmp_path)
    runner = _ReadySetupRunner(ollama_model=settings.local_llm.ollama_model)
    interaction = _NonInteractiveProcessingInteraction()
    _persist_runtime_profile_state(
        settings,
        runner=runner,
        authorize_local_fallback=False,
    )

    with pytest.raises(RuntimeFallbackUnavailable, match="pass --allow-runtime-fallback"):
        run_processing_command(
            session_path=session_path,
            run_id="run-1",
            attempt=1,
            now=NOW,
            transcription_provider_factory=_RuntimeRequiredProviderFactory(
                _UnavailableTranscriptionProvider
            ),
            diarization_provider_factory=_RuntimeRequiredProviderFactory(FakeDiarizationProvider),
            semantic_coherence_provider_factory=_RuntimeRequiredProviderFactory(
                lambda: FakeSemanticCoherenceProvider(results={})
            ),
            settings=settings,
            setup_runner=runner,
            setup_command_identity=COMMAND_IDENTITY,
            setup_hardware=READY_HARDWARE,
            interaction=interaction,
        )

    assert interaction.notices == []


def test_run_processing_command_stores_runtime_profile_provenance(
    tmp_path: Path,
) -> None:
    session_path = _write_processing_session(tmp_path)
    settings = AppSettings.from_env(env={}, home=tmp_path)
    runner = _ReadySetupRunner(ollama_model=settings.local_llm.ollama_model)
    _persist_runtime_profile_state(
        settings,
        runner=runner,
        authorize_local_fallback=False,
    )

    assert (
        run_processing_command(
            session_path=session_path,
            run_id="run-with-runtime",
            attempt=3,
            now=NOW,
            transcription_provider_factory=_RuntimeRequiredProviderFactory(
                FakeTranscriptionProvider
            ),
            diarization_provider_factory=_RuntimeRequiredProviderFactory(FakeDiarizationProvider),
            semantic_coherence_provider_factory=_RuntimeRequiredProviderFactory(
                lambda: FakeSemanticCoherenceProvider(results={})
            ),
            settings=settings,
            setup_runner=runner,
            setup_command_identity=COMMAND_IDENTITY,
            setup_hardware=READY_HARDWARE,
        )
        == 0
    )

    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=session_path)
    provenance_records = store.list(artifact_type="runtime_profile_provenance")
    assert len(provenance_records) == 1
    provenance = json.loads(store.retrieve(provenance_records[0].id))
    assert provenance["run_id"] == "run-with-runtime"
    assert provenance["attempt"] == 3


def test_run_processing_command_revalidates_only_processing_dependencies(
    tmp_path: Path,
) -> None:
    session_path = _write_processing_session(tmp_path)
    settings = AppSettings.from_env(env={}, home=tmp_path)
    runner = _ReadySetupRunner(ollama_model=settings.local_llm.ollama_model)
    _persist_runtime_profile_state(
        settings,
        runner=runner,
        authorize_local_fallback=False,
    )
    runner.reset()

    with pytest.raises(RuntimeFallbackUnavailable):
        run_processing_command(
            session_path=session_path,
            run_id="run-revalidate",
            attempt=1,
            now=NOW,
            transcription_provider_factory=_RuntimeRequiredProviderFactory(
                _UnavailableTranscriptionProvider
            ),
            diarization_provider_factory=_RuntimeRequiredProviderFactory(FakeDiarizationProvider),
            semantic_coherence_provider_factory=_RuntimeRequiredProviderFactory(
                lambda: FakeSemanticCoherenceProvider(results={})
            ),
            settings=settings,
            setup_runner=runner,
            setup_command_identity=COMMAND_IDENTITY,
            setup_hardware=READY_HARDWARE,
        )

    assert "mena" in runner.which_calls
    assert "ollama" not in runner.which_calls
    assert "ffplay" not in runner.which_calls
    assert any("faster_whisper" in " ".join(command) for command in runner.run_calls)
    assert any("pyannote.audio" in " ".join(command) for command in runner.run_calls)


def test_run_processing_command_prompts_for_ready_one_run_runtime_fallback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session_path = _write_processing_session(tmp_path)
    settings = AppSettings.from_env(env={}, home=tmp_path)
    runner = _ReadySetupRunner(ollama_model=settings.local_llm.ollama_model)
    interaction = _ConfirmingProcessingInteraction(True)
    _persist_runtime_profile_state(
        settings,
        runner=runner,
        authorize_local_fallback=False,
    )

    assert (
        run_processing_command(
            session_path=session_path,
            run_id="run-fallback",
            attempt=1,
            now=NOW,
            transcription_provider_factory=_RuntimeRequiredProviderFactory(
                FakeTranscriptionProvider
            ),
            diarization_provider_factory=_RuntimeRequiredProviderFactory(FakeDiarizationProvider),
            semantic_coherence_provider_factory=_RuntimeRequiredProviderFactory(
                lambda: FakeSemanticCoherenceProvider(results={})
            ),
            settings=settings,
            setup_runner=runner,
            setup_command_identity=COMMAND_IDENTITY,
            setup_hardware=CPU_FALLBACK_HARDWARE,
            interaction=interaction,
        )
        == 0
    )

    assert [request.topic for request in interaction.confirmations] == ["runtime_fallback.one_run"]
    assert (
        "Processing completed for run 'run-fallback' (attempt 1) "
        "with explicit runtime fallback." in capsys.readouterr().out
    )


def test_run_processing_command_records_terminal_runtime_stewardship_release(
    tmp_path: Path,
) -> None:
    session_path = _write_processing_session(tmp_path)
    settings = AppSettings.from_env(env={}, home=tmp_path)
    runner = _ReadySetupRunner(ollama_model=settings.local_llm.ollama_model)
    interaction = _NonInteractiveProcessingInteraction()
    _persist_runtime_profile_state(
        settings,
        runner=runner,
        authorize_local_fallback=False,
    )

    assert (
        run_processing_command(
            session_path=session_path,
            run_id="process-release",
            attempt=1,
            now=NOW,
            transcription_provider_factory=_RuntimeRequiredProviderFactory(
                FakeTranscriptionProvider
            ),
            diarization_provider_factory=_RuntimeRequiredProviderFactory(FakeDiarizationProvider),
            semantic_coherence_provider_factory=_RuntimeRequiredProviderFactory(
                lambda: _ReleasingSemanticProvider(results={})
            ),
            settings=settings,
            setup_runner=runner,
            setup_command_identity=COMMAND_IDENTITY,
            setup_hardware=READY_HARDWARE,
            interaction=interaction,
        )
        == 0
    )

    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=session_path)
    stewardship_records = store.list(artifact_type="runtime_stewardship_provenance")
    assert len(stewardship_records) == 1
    payload = json.loads(store.retrieve(stewardship_records[0].id))
    assert payload["run_id"] == "process-release"
    assert payload["boundary"] == "process"
    assert payload["releases"][0]["status"] == "released"
    assert payload["releases"][0]["lease"]["stage"] == "semantic-review"
    assert payload["releases"][0]["lease"]["provider"] == "fake"
    assert len(interaction.notices) == 1
    assert interaction.notices[0].topic == RUNTIME_STEWARDSHIP_RELEASE_NOTICE_TOPIC
    assert interaction.notices[0].severity == "info"


@pytest.mark.parametrize("release_status", ["failed", "not_supported"])
def test_run_processing_command_reports_terminal_runtime_release_warning(
    tmp_path: Path,
    release_status: str,
) -> None:
    session_path = _write_processing_session(tmp_path)
    settings = AppSettings.from_env(env={}, home=tmp_path)
    runner = _ReadySetupRunner(ollama_model=settings.local_llm.ollama_model)
    interaction = _NonInteractiveProcessingInteraction()
    _persist_runtime_profile_state(
        settings,
        runner=runner,
        authorize_local_fallback=False,
    )

    assert (
        run_processing_command(
            session_path=session_path,
            run_id=f"process-release-{release_status}",
            attempt=1,
            now=NOW,
            transcription_provider_factory=_RuntimeRequiredProviderFactory(
                FakeTranscriptionProvider
            ),
            diarization_provider_factory=_RuntimeRequiredProviderFactory(FakeDiarizationProvider),
            semantic_coherence_provider_factory=_RuntimeRequiredProviderFactory(
                lambda: _ReleasingSemanticProvider(
                    results={},
                    release_status=release_status,
                    release_detail=f"Runtime release {release_status}.",
                )
            ),
            settings=settings,
            setup_runner=runner,
            setup_command_identity=COMMAND_IDENTITY,
            setup_hardware=READY_HARDWARE,
            interaction=interaction,
        )
        == 0
    )

    assert len(interaction.notices) == 1
    assert interaction.notices[0].topic == RUNTIME_STEWARDSHIP_RELEASE_NOTICE_TOPIC
    assert interaction.notices[0].severity == "warning"
    release_result = cast(RuntimeReleaseResult, interaction.notices[0].context["result"])
    assert release_result.status == release_status


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
    result = run_processing_stages(
        session_id=SESSION_ID,
        profile=profile,
        source_inputs=source_inputs,
        store=store,
        ledger=ledger,
        attempt=1,
        now=NOW,
        transcription_provider=FakeTranscriptionProvider(),
        diarization_provider=FakeDiarizationProvider(),
        semantic_coherence_provider=FakeSemanticCoherenceProvider(results={}),
        session_language="it-CH",
    )

    transcript_record = store.list(artifact_type="transcript")[0]
    transcript = json.loads(store.retrieve(transcript_record.id))
    assert transcript["language"]["requested"] == "it"
    assert len(store.list(artifact_type="diarization")) == 3
    merged_records = store.list(artifact_type="merged_transcript")
    assert len(merged_records) == 1
    merged_record = merged_records[0]
    assert (
        merged_record.metadata["policy_config_id"]
        == MergePolicy(capture_profile="in_person_single_mic").config_id
    )
    assert merged_record.metadata["capture_profile"] == "in_person_single_mic"
    assert merged_record.metadata["room_source_id"] == "room_mic"
    assert merged_record.metadata["room_diarization_status"] == "used"
    merged_payload = json.loads(store.retrieve(merged_record.id))
    assert merged_payload["policy"]["capture_profile"] == "in_person_single_mic"
    assert [source["source_id"] for source in merged_payload["sources"]] == ["room_mic"]
    assert len(store.list(artifact_type="review_queue")) == 2
    assert result.semantic_coherence_result.skip_reason is None


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
        ({"route_id": 1}, "Session field 'route_id' must be a non-empty string or null."),
        ({"route_id": ""}, "Session field 'route_id' must be a non-empty string or null."),
        ({"route_label": 1}, "Session field 'route_label' must be a non-empty string or null."),
        ({"route_label": ""}, "Session field 'route_label' must be a non-empty string or null."),
        ({"capture_mode": 1}, "Session field 'capture_mode' must be a non-empty string or null."),
        ({"capture_mode": ""}, "Session field 'capture_mode' must be a non-empty string or null."),
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
    profile = get_capture_profile("online_dual_stream")
    reversed_profile = replace(profile, sources=tuple(reversed(profile.sources)))
    merge_policy = _merge_policy_for_profile(profile=reversed_profile)
    assert merge_policy.local_source_id == "local_mic"
    assert merge_policy.remote_source_id == "system_audio"
