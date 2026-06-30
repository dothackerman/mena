import hashlib
import json
import queue
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pytest import CaptureFixture

from mena.artifacts import (
    ArtifactRecord,
    ArtifactStore,
    ArtifactStoreError,
    FileSystemArtifactStore,
)
from mena.capture import make_wav_bytes
from mena.cli import (
    COMMANDS,
    WORKER_EXIT_TIMEOUT_SECONDS,
    SourceAudioInput,
    _CaptureEventMonitor,
    _collect_capture_segments,
    _latest_failed_stage_input_artifacts,
    _record_terminal_local_llm_release,
    _require_provider,
    _resolve_latest_merged_transcript_record,
    _resolve_record_language,
    _resolve_record_profile,
    _resolve_route_selections,
    _resolve_runtime_fallback_input,
    _run_processing_command,
    _run_processing_stages,
    _runtime_fallback_stage_for_provider_failure,
    _wait_for_record_stop,
    main,
)
from mena.correction_overlay import (
    CorrectionEntry,
    create_correction_overlay,
    persist_correction_overlay_artifact,
)
from mena.correction_workflow import PlaybackRequest
from mena.diarization import (
    DIARIZATION_STAGE,
    DiarizationProvider,
    DiarizationProviderUnavailable,
    FakeDiarizationProvider,
    PyannoteCommunityDiarizationProvider,
)
from mena.discovery import AudioDiscovery, AudioRoute, DiscoveryStatus, PlaybackStream
from mena.merge_timing import (
    MERGED_TRANSCRIPT_ARTIFACT_TYPE,
    MERGED_TRANSCRIPT_SCHEMA_VERSION,
    MergedTranscriptArtifact,
    MergedTranscriptSegment,
    MergedTranscriptSource,
    MergePolicy,
    SegmentConfidence,
    SpeakerIdentity,
    persist_merged_transcript_artifact,
)
from mena.processing import (
    ArtifactRef,
    ProcessingErrorInfo,
    ProcessingStageLedger,
    ProcessingStageState,
)
from mena.profiles import CaptureProfileId, get_capture_profile, parse_capture_profile
from mena.protocol import (
    CaptureRouteBindingPayload,
    ProtocolError,
    ProtocolMessage,
    StartCapturePayload,
    WorkerProcess,
)
from mena.review_flagging import (
    REVIEW_QUEUE_SCHEMA_VERSION,
    ConfidenceFlaggingPolicy,
    ReviewFlag,
    ReviewQueueArtifact,
    persist_review_queue_artifact,
)
from mena.runtime_profile import (
    PROCESSING_REQUIRED_RUNTIME_STAGES,
    SUMMARIZATION_RUNTIME_STAGES,
    RuntimeFallbackUnavailable,
    runtime_diarization_factory,
    runtime_semantic_factory,
    runtime_transcription_factory,
    select_runtime_profile,
    select_runtime_profile_after_stage_failure,
    store_runtime_profile_provenance,
)
from mena.semantic_coherence import (
    FakeSemanticCoherenceProvider,
    OllamaSemanticCoherenceProvider,
    SemanticCoherenceProvider,
)
from mena.sessions import (
    SESSION_RECORD_FILENAME,
    SESSION_RECORD_VERSION,
    CreatedSession,
    create_session,
    update_session_capture_metadata,
)
from mena.settings import DATA_ROOT_ENV_VAR, AppSettings
from mena.setup import (
    CommandCheck,
    CommandIdentity,
    CommandRun,
    HardwareFacts,
    LocalInstallationState,
    OllamaState,
    PythonSurfaceCheck,
    RuntimeProfileState,
    build_runtime_profile_state,
    default_profile_catalog,
    evaluate_setup,
    get_alpha_local_profile,
    load_runtime_profile_state,
    persist_runtime_profile_state,
)
from mena.summarization import (
    FakeSummarizationProvider,
    SummarizationError,
    SummarizationPolicy,
    SummaryArtifact,
    SummaryResult,
    persist_summary_artifact,
)
from mena.transcription import (
    TRANSCRIPT_SCHEMA_VERSION,
    TRANSCRIPTION_STAGE,
    FakeTranscriptionProvider,
    FasterWhisperTranscriptionProvider,
    TranscriptArtifact,
    TranscriptionProvider,
    TranscriptionProviderUnavailable,
    TranscriptionRequest,
    TranscriptLanguage,
    TranscriptProviderMetadata,
    TranscriptSegment,
    persist_transcript_artifact,
)

SESSION_ID = "00000000-0000-0000-0000-000000000001"


def test_main_prints_help_without_command(capsys: CaptureFixture[str]) -> None:
    exit_code = main([])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Privacy-first local meeting notes PoC." in captured.out


def test_main_exposes_architecture_command_surface(capsys: CaptureFixture[str]) -> None:
    for command in [
        command
        for command in COMMANDS[1:]
        if command
        not in {
            "correct",
            "process",
            "resume",
            "summarize",
            "export",
            "inspect",
            "setup",
        }
    ]:
        exit_code = main([command])
        captured = capsys.readouterr()

        assert exit_code == 0
        assert f"Command '{command}' is not implemented yet." in captured.out


def test_main_prints_version(capsys: CaptureFixture[str]) -> None:
    exit_code = main(["--version"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.strip()


def test_setup_check_reports_product_paths_and_ready_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    state = _setup_state(settings=settings)

    def _detect_state(_settings: AppSettings, **_kwargs: object) -> LocalInstallationState:
        return state

    monkeypatch.setattr("mena.cli.detect_local_installation_state", _detect_state)

    exit_code = main(["setup", "--check"], settings=settings)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Profile: linux-alpha-local-v1" in captured.out
    assert "Current executable: /home/user/.local/bin/mena" in captured.out
    assert "Plain mena on PATH: /home/user/.local/bin/mena" in captured.out
    assert f"sessions: {settings.paths.sessions_dir}" in captured.out
    assert "Invocation mode: installed-product" in captured.out
    assert "Local provider readiness: complete" in captured.out


def test_setup_decline_does_not_mutate_or_report_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    state = _setup_state(settings=settings, ollama_model_present=False)
    runner = SetupFakeRunner()

    def _detect_state(_settings: AppSettings, **_kwargs: object) -> LocalInstallationState:
        return state

    monkeypatch.setattr("mena.cli.detect_local_installation_state", _detect_state)

    exit_code = main(
        ["setup"],
        settings=settings,
        input_fn=lambda _: "n",
        setup_runner=runner,
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert runner.run_calls == []
    assert "Setup declined; local provider readiness is incomplete." in captured.out
    assert "Local provider readiness: incomplete" in captured.out


def test_setup_interrupt_does_not_mutate_or_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    state = _setup_state(settings=settings, ollama_model_present=False)
    runner = SetupFakeRunner()

    def _detect_state(_settings: AppSettings, **_kwargs: object) -> LocalInstallationState:
        return state

    def _interrupt(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("mena.cli.detect_local_installation_state", _detect_state)

    exit_code = main(
        ["setup"],
        settings=settings,
        input_fn=_interrupt,
        setup_runner=runner,
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert runner.run_calls == []
    assert "Setup interrupted; local provider readiness is incomplete." in captured.out


def test_setup_interrupt_during_initial_detection_does_not_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    runner = SetupFakeRunner()

    def _detect_state(_settings: AppSettings, **_kwargs: object) -> LocalInstallationState:
        raise KeyboardInterrupt

    monkeypatch.setattr("mena.cli.detect_local_installation_state", _detect_state)

    exit_code = main(["setup", "--check"], settings=settings, setup_runner=runner)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert runner.run_calls == []
    assert "Setup interrupted; local provider readiness is incomplete." in captured.out


def test_setup_does_not_mutate_from_repo_dev_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    state = _setup_state(
        settings=settings,
        command_identity=CommandIdentity(
            current_executable=Path("/repo/app/.venv/bin/mena"),
            plain_mena_path=Path("/repo/app/.venv/bin/mena"),
            version="0.1.0",
            invocation_mode="repo/dev",
        ),
        ollama_model_present=False,
        asr_model_present=False,
        diarization_model_present=False,
        pyannote_token_present=True,
    )
    runner = SetupFakeRunner()

    def _detect_state(_settings: AppSettings, **_kwargs: object) -> LocalInstallationState:
        return state

    monkeypatch.setattr("mena.cli.detect_local_installation_state", _detect_state)

    exit_code = main(["setup", "--yes"], settings=settings, setup_runner=runner)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert runner.run_calls == []
    assert "current invocation mode is repo/dev" in captured.out
    assert (
        "Setup actions were not run because this is not the installed product command."
        in captured.out
    )


def test_setup_yes_executes_planned_actions_through_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    state = _setup_state(
        settings=settings,
        ollama_model_present=False,
        asr_model_present=False,
        diarization_model_present=False,
        pyannote_token_present=True,
    )
    ready_state = _setup_state(settings=settings, pyannote_token_present=True)
    detected_states = [state, ready_state]
    runner = SetupFakeRunner()

    def _detect_state(_settings: AppSettings, **_kwargs: object) -> LocalInstallationState:
        return detected_states.pop(0) if detected_states else ready_state

    monkeypatch.setattr("mena.cli.detect_local_installation_state", _detect_state)

    exit_code = main(["setup", "--yes"], settings=settings, setup_runner=runner)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert ("ollama", "pull", "llama3.2:3b") in runner.run_calls
    assert any(call[1] == "-c" and "faster_whisper" in call[2] for call in runner.run_calls)
    assert any(call[1] == "-c" and "pyannote.audio" in call[2] for call in runner.run_calls)
    assert "Setup action results:" in captured.out
    assert "Local provider readiness: complete" in captured.out
    assert "Automatic local runtime fallback is not authorized" in captured.out
    assert "Accepted runtime profile state:" in captured.out
    assert (settings.paths.state_home / "setup-state.json").is_file()
    accepted = load_runtime_profile_state(settings.paths)
    assert accepted.fallback_authorization.local_status == "configured_unauthorized"
    assert accepted.fallback_authorization.automatic_local_fallback_authorized is False
    assert "[missing]" in captured.out
    assert (
        captured.out.rsplit("Mena Linux alpha setup preflight", maxsplit=1)[-1].count("[missing]")
        == 0
    )


def test_setup_authorize_local_fallback_flag_persists_automatic_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    state = _setup_state(settings=settings, pyannote_token_present=True)

    def _detect_state(_settings: AppSettings, **_kwargs: object) -> LocalInstallationState:
        return state

    monkeypatch.setattr("mena.cli.detect_local_installation_state", _detect_state)

    exit_code = main(
        ["setup", "--yes", "--authorize-local-fallback"],
        settings=settings,
        setup_runner=SetupFakeRunner(),
    )
    captured = capsys.readouterr()

    accepted = load_runtime_profile_state(settings.paths)
    assert exit_code == 0
    assert "Automatic local runtime fallback authorized for:" in captured.out
    assert accepted.fallback_authorization.local_status == "authorized"
    assert accepted.fallback_authorization.automatic_local_fallback_authorized is True


def test_setup_authorize_local_fallback_reports_unavailable_blockers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    primary_ready = _setup_state(settings=settings, pyannote_token_present=True)
    fallback_missing_asr = _setup_state(
        settings=settings,
        asr_model_present=False,
        pyannote_token_present=True,
    )

    def _detect_state(_settings: AppSettings, **kwargs: object) -> LocalInstallationState:
        profile = cast(Any, kwargs.get("profile"))
        if profile is not None and profile.fallback:
            return fallback_missing_asr
        return primary_ready

    monkeypatch.setattr("mena.cli.detect_local_installation_state", _detect_state)

    exit_code = main(
        ["setup", "--yes", "--authorize-local-fallback"],
        settings=settings,
        setup_runner=SetupFakeRunner(),
    )
    captured = capsys.readouterr()

    accepted = load_runtime_profile_state(settings.paths)
    assert exit_code == 0
    assert accepted.fallback_authorization.local_status == "unavailable"
    assert accepted.fallback_authorization.automatic_local_fallback_authorized is False
    assert "Automatic local runtime fallback is unavailable and remains disabled." in captured.out
    assert "Fallback blocker:" in captured.out
    assert "selected faster-whisper model" in captured.out


def test_setup_authorize_local_fallback_runs_fallback_actions_before_persisting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    primary_ready = _setup_state(settings=settings, pyannote_token_present=True)
    fallback_missing_asr = _setup_state(
        settings=settings,
        asr_model_present=False,
        pyannote_token_present=True,
    )
    fallback_ready = _setup_state(settings=settings, pyannote_token_present=True)
    fallback_states = [fallback_missing_asr, fallback_ready]
    runner = SetupFakeRunner()

    def _detect_state(_settings: AppSettings, **kwargs: object) -> LocalInstallationState:
        profile = cast(Any, kwargs.get("profile"))
        if profile is not None and profile.fallback:
            return fallback_states.pop(0)
        return primary_ready

    monkeypatch.setattr("mena.cli.detect_local_installation_state", _detect_state)

    exit_code = main(
        ["setup", "--yes", "--authorize-local-fallback"],
        settings=settings,
        setup_runner=runner,
    )
    captured = capsys.readouterr()

    accepted = load_runtime_profile_state(settings.paths)
    assert exit_code == 0
    assert any(call[1] == "-c" and "WhisperModel('small'" in call[2] for call in runner.run_calls)
    assert accepted.fallback_authorization.local_status == "authorized"
    assert accepted.fallback_authorization.automatic_local_fallback_authorized is True
    assert "Profile: linux-alpha-cpu-fallback-v1" in captured.out
    assert "Setup action results:" in captured.out


def test_setup_authorize_local_fallback_prompts_before_running_fallback_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    primary_ready = _setup_state(settings=settings, pyannote_token_present=True)
    fallback_missing_asr = _setup_state(
        settings=settings,
        asr_model_present=False,
        pyannote_token_present=True,
    )
    runner = SetupFakeRunner()
    prompts: list[str] = []

    def _detect_state(_settings: AppSettings, **kwargs: object) -> LocalInstallationState:
        profile = cast(Any, kwargs.get("profile"))
        if profile is not None and profile.fallback:
            return fallback_missing_asr
        return primary_ready

    def _decline(prompt: str) -> str:
        prompts.append(prompt)
        return "n"

    monkeypatch.setattr("mena.cli.detect_local_installation_state", _detect_state)

    exit_code = main(
        ["setup", "--authorize-local-fallback"],
        settings=settings,
        input_fn=_decline,
        setup_runner=runner,
    )
    captured = capsys.readouterr()

    accepted = load_runtime_profile_state(settings.paths)
    assert exit_code == 0
    assert prompts == ["Proceed with automated fallback setup actions? [y/N] "]
    assert runner.run_calls == []
    assert accepted.fallback_authorization.local_status == "unavailable"
    assert accepted.fallback_authorization.automatic_local_fallback_authorized is False
    assert "Fallback setup actions declined" in captured.out


def test_setup_evaluates_fallback_profile_before_authorizing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    primary_ready = _setup_state(settings=settings, pyannote_token_present=True)
    fallback_missing_asr = _setup_state(
        settings=settings,
        asr_model_present=False,
        pyannote_token_present=True,
    )
    observed_profiles: list[str] = []

    def _detect_state(_settings: AppSettings, **kwargs: object) -> LocalInstallationState:
        profile = cast(Any, kwargs.get("profile"))
        if profile is not None and profile.fallback:
            observed_profiles.append(profile.id)
            return fallback_missing_asr
        observed_profiles.append("primary")
        return primary_ready

    monkeypatch.setattr("mena.cli.detect_local_installation_state", _detect_state)

    assert main(["setup", "--yes"], settings=settings, setup_runner=SetupFakeRunner()) == 0

    accepted = load_runtime_profile_state(settings.paths)
    assert observed_profiles == ["primary", "linux-alpha-cpu-fallback-v1"]
    assert accepted.fallback_authorization.local_status == "unavailable"
    assert accepted.fallback_authorization.automatic_local_fallback_authorized is False
    assert any(
        "selected faster-whisper model" in reason
        for reason in accepted.fallback_authorization.blocking_reasons
    )


def test_setup_interrupt_during_ready_fallback_evaluation_does_not_persist_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    primary_ready = _setup_state(settings=settings, pyannote_token_present=True)

    def _detect_state(_settings: AppSettings, **kwargs: object) -> LocalInstallationState:
        profile = cast(Any, kwargs.get("profile"))
        if profile is not None and profile.fallback:
            raise KeyboardInterrupt
        return primary_ready

    monkeypatch.setattr("mena.cli.detect_local_installation_state", _detect_state)

    exit_code = main(["setup", "--yes"], settings=settings, setup_runner=SetupFakeRunner())
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Setup interrupted; local provider readiness is incomplete." in captured.out
    assert not (settings.paths.state_home / "setup-state.json").exists()


def test_setup_with_only_manual_actions_does_not_prompt_or_execute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    state = _setup_state(
        settings=settings,
        ollama_model_present=True,
        asr_model_present=False,
        diarization_model_present=False,
        python_surfaces={"faster_whisper": False, "pyannote.audio": False},
    )
    runner = SetupFakeRunner()

    def _detect_state(_settings: AppSettings, **_kwargs: object) -> LocalInstallationState:
        return state

    def _prompt(_prompt: str) -> str:
        raise AssertionError("manual-only setup should not prompt")

    monkeypatch.setattr("mena.cli.detect_local_installation_state", _detect_state)

    exit_code = main(["setup"], settings=settings, input_fn=_prompt, setup_runner=runner)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert runner.run_calls == []
    assert "only manual actions remain" in captured.out


def test_setup_interrupt_during_actions_does_not_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    state = _setup_state(
        settings=settings,
        ollama_model_present=False,
        asr_model_present=False,
        diarization_model_present=False,
        pyannote_token_present=True,
    )
    runner = InterruptingSetupRunner()

    def _detect_state(_settings: AppSettings, **_kwargs: object) -> LocalInstallationState:
        return state

    monkeypatch.setattr("mena.cli.detect_local_installation_state", _detect_state)

    exit_code = main(["setup", "--yes"], settings=settings, setup_runner=runner)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert runner.run_calls
    assert "Setup interrupted; local provider readiness is incomplete." in captured.out


def test_setup_interrupt_during_post_action_refresh_does_not_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    state = _setup_state(
        settings=settings,
        ollama_model_present=False,
        asr_model_present=False,
        diarization_model_present=False,
        pyannote_token_present=True,
    )
    detected_states: list[LocalInstallationState] = [state]
    runner = SetupFakeRunner()

    def _detect_state(_settings: AppSettings, **_kwargs: object) -> LocalInstallationState:
        if detected_states:
            return detected_states.pop(0)
        raise KeyboardInterrupt

    monkeypatch.setattr("mena.cli.detect_local_installation_state", _detect_state)

    exit_code = main(["setup", "--yes"], settings=settings, setup_runner=runner)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert runner.run_calls
    assert "Setup interrupted; local provider readiness is incomplete." in captured.out


def test_setup_interrupt_during_refreshed_fallback_evaluation_does_not_persist_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    initial_state = _setup_state(
        settings=settings,
        ollama_model_present=False,
        asr_model_present=False,
        diarization_model_present=False,
        pyannote_token_present=True,
    )
    ready_state = _setup_state(settings=settings, pyannote_token_present=True)
    detected_states: list[LocalInstallationState] = [initial_state, ready_state]
    runner = SetupFakeRunner()

    def _detect_state(_settings: AppSettings, **kwargs: object) -> LocalInstallationState:
        profile = cast(Any, kwargs.get("profile"))
        if profile is not None and profile.fallback:
            raise KeyboardInterrupt
        return detected_states.pop(0)

    monkeypatch.setattr("mena.cli.detect_local_installation_state", _detect_state)

    exit_code = main(["setup", "--yes"], settings=settings, setup_runner=runner)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert runner.run_calls
    assert "Setup interrupted; local provider readiness is incomplete." in captured.out
    assert not (settings.paths.state_home / "setup-state.json").exists()


def test_record_help_lists_profile_options(capsys: CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["record", "--help"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 0
    assert "--profile {online,in-person}" in captured.out
    assert "--name NAME" in captured.out
    assert "--dry-run" in captured.out
    assert "--verbose" in captured.out


def test_record_creates_session_bundle_and_record(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "record",
            "--profile",
            "online",
            "--language",
            "en",
            "--playback-stream",
            "pulse:sink-input:151",
            "--name",
            "Planning",
        ],
        data_root=tmp_path,
        discoverer=_available_discoverer,
        worker_factory=FakeWorkerProcess,
        input_fn=lambda _prompt: "q",
    )

    captured = capsys.readouterr()
    sessions = list((tmp_path / "sessions").iterdir())
    assert exit_code == 0
    assert "Created session" in captured.out
    assert "Recording notice: you are responsible for obtaining consent" in captured.out
    assert f"To process this recording, run: mena process {sessions[0] / 'session.json'}" in (
        captured.out
    )
    assert len(sessions) == 1

    record_path = sessions[0] / "session.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["version"] == 1
    assert record["id"]
    assert record["created_at"].endswith("Z")
    assert record["name"] == "Planning"
    assert record["status"] == "recorded"
    assert record["profile"] == "online_dual_stream"
    assert record["language"] == "en"
    assert record["discovery"]["status"] == "available"
    assert record["capture_plan"]["status"] == "finalized"
    assert record["capture_plan"]["selected_sources"] == ["local_mic", "system_audio"]
    assert record["capture_plan"]["routes"][1]["route_id"] == "pulse:sink-input:151"
    assert record["recording"]["stop_reason"] == "user_stop"
    assert record["recording"]["finalization"]["status"] == "finalized"
    assert record["capture_metadata"]["segment_count"] == 2


def test_record_does_not_offer_process_handoff_for_empty_partial_capture(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "record",
            "--profile",
            "online",
            "--language",
            "en",
            "--playback-stream",
            "pulse:sink-input:151",
        ],
        data_root=tmp_path,
        discoverer=_available_discoverer,
        worker_factory=PartialWorkerProcess,
        input_fn=lambda _prompt: "q",
    )

    captured = capsys.readouterr()
    record_path = next((tmp_path / "sessions").iterdir()) / "session.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert record["status"] == "interrupted_partial"
    assert record["recording"]["finalization"]["status"] == "interrupted_partial"
    assert record["capture_metadata"]["segment_count"] == 0
    assert "No processable audio segments were sealed." in captured.out
    assert "mena process" not in captured.out


def test_record_rejects_identical_online_source_hashes(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    IdenticalHashWorkerProcess.pcm_bytes = b"\x01\x00" * 320

    exit_code = main(
        [
            "record",
            "--profile",
            "online",
            "--language",
            "en",
            "--playback-stream",
            "pulse:sink-input:151",
        ],
        data_root=tmp_path,
        discoverer=_available_discoverer,
        worker_factory=IdenticalHashWorkerProcess,
        input_fn=lambda _prompt: "q",
    )

    captured = capsys.readouterr()
    record_path = next((tmp_path / "sessions").iterdir()) / "session.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert record["status"] == "failed"
    assert record["recording"]["finalization"]["status"] == "failed"
    assert "source independence failed" in record["recording"]["finalization"]["error"]
    assert "Capture finalized as failed" in captured.out
    assert "mena process" not in captured.out


def test_record_allows_identical_silent_online_source_hashes(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "record",
            "--profile",
            "online",
            "--language",
            "en",
            "--playback-stream",
            "pulse:sink-input:151",
        ],
        data_root=tmp_path,
        discoverer=_available_discoverer,
        worker_factory=SilentIdenticalHashWorkerProcess,
        input_fn=lambda _prompt: "q",
    )

    captured = capsys.readouterr()
    record_path = next((tmp_path / "sessions").iterdir()) / "session.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert record["status"] == "recorded"
    assert record["recording"]["finalization"]["status"] == "finalized"
    assert "source independence failed" not in json.dumps(record)
    assert "mena process" in captured.out


def test_record_defaults_to_runtime_sessions_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    monkeypatch.delenv(DATA_ROOT_ENV_VAR, raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))

    exit_code = main(
        ["record", "--profile", "online", "--dry-run"],
        discoverer=_available_discoverer,
    )
    captured = capsys.readouterr()

    sessions_dir = tmp_path / "xdg-data" / "mena" / "sessions"
    sessions = list(sessions_dir.iterdir())
    assert exit_code == 0
    assert len(sessions) == 1
    assert f"Session record: {sessions[0] / 'session.json'}" in captured.out


def test_record_data_root_override_remains_session_scoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    exit_code = main(
        ["record", "--profile", "online", "--dry-run"],
        data_root=tmp_path / "smoke-data",
        discoverer=_available_discoverer,
    )

    assert exit_code == 0
    assert len(list((tmp_path / "smoke-data" / "sessions").iterdir())) == 1
    assert not (tmp_path / "config").exists()
    assert not (tmp_path / "cache").exists()
    assert not (tmp_path / "state").exists()


def test_record_requires_profile_noninteractively(capsys: CaptureFixture[str]) -> None:
    exit_code = main(["record"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Capture profile is required in non-interactive mode" in captured.out


def test_record_rejects_invalid_profile(capsys: CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["record", "--profile", "room"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "invalid choice: 'room'" in captured.err
    assert "online" in captured.err
    assert "in-person" in captured.err


def test_record_requires_language_before_capture(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    exit_code = main(
        ["record", "--profile", "online"],
        data_root=tmp_path,
        discoverer=_available_discoverer,
        worker_factory=FakeWorkerProcess,
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Meeting language is required before recording" in captured.out
    assert not (tmp_path / "sessions").exists()


def test_record_prompts_for_profile_when_omitted(tmp_path: Path) -> None:
    FakeWorkerProcess.last_start_payload = None
    answers = iter(["2", "q"])

    exit_code = main(
        ["record", "--language", "fr-CH"],
        data_root=tmp_path,
        discoverer=_available_discoverer,
        worker_factory=FakeWorkerProcess,
        input_fn=lambda _prompt: next(answers),
    )

    assert exit_code == 0
    assert FakeWorkerProcess.last_start_payload is not None
    assert FakeWorkerProcess.last_start_payload.profile == "in_person_single_mic"
    assert FakeWorkerProcess.last_start_payload.language == "fr-CH"
    assert FakeWorkerProcess.last_start_payload.selected_sources == ("room_mic",)


def test_resolve_record_profile_accepts_numeric_online_prompt() -> None:
    selection = _resolve_record_profile(None, input_fn=lambda _prompt: "1")

    assert selection.id is CaptureProfileId.ONLINE_DUAL_STREAM


def test_resolve_record_language_prompts_with_default() -> None:
    assert (
        _resolve_record_language(
            None,
            default_language="de-CH",
            input_fn=lambda _prompt: "",
            dry_run=False,
        )
        == "de-CH"
    )


def test_resolve_route_selections_prompts_for_ambiguous_online_routes() -> None:
    answers = iter(["2", "2", "2"])

    selections = _resolve_route_selections(
        profile=CaptureProfileId.ONLINE_DUAL_STREAM,
        discovery=_ambiguous_discoverer(CaptureProfileId.ONLINE_DUAL_STREAM),
        microphone_route_id=None,
        playback_stream_route_id=None,
        output_sink_route_id=None,
        input_fn=lambda _prompt: next(answers),
    )

    assert selections.microphone_route_id == "pipewire:node:32"
    assert selections.playback_stream_route_id == "pulse:sink-input:152"
    assert selections.output_sink_route_id == "pipewire:node:43"


def test_resolve_route_selections_rejects_in_person_online_flags() -> None:
    with pytest.raises(ValueError, match="accepts --mic only"):
        _resolve_route_selections(
            profile=CaptureProfileId.IN_PERSON_SINGLE_MIC,
            discovery=_available_discoverer(CaptureProfileId.IN_PERSON_SINGLE_MIC),
            microphone_route_id=None,
            playback_stream_route_id="pulse:sink-input:151",
            output_sink_route_id=None,
            input_fn=None,
        )


def test_resolve_route_selections_rejects_missing_playback_streams() -> None:
    discovery = AudioDiscovery(
        version=1,
        profile=CaptureProfileId.ONLINE_DUAL_STREAM,
        status=DiscoveryStatus.AVAILABLE,
        sources=_available_discoverer(CaptureProfileId.ONLINE_DUAL_STREAM).sources,
        sinks=_available_discoverer(CaptureProfileId.ONLINE_DUAL_STREAM).sinks,
        playback_streams=(),
    )

    with pytest.raises(ValueError, match="No playback streams were discovered"):
        _resolve_route_selections(
            profile=CaptureProfileId.ONLINE_DUAL_STREAM,
            discovery=discovery,
            microphone_route_id=None,
            playback_stream_route_id=None,
            output_sink_route_id=None,
            input_fn=None,
        )


def test_resolve_route_selections_requires_single_playback_confirmation() -> None:
    with pytest.raises(ValueError, match="Playback stream confirmation is required"):
        _resolve_route_selections(
            profile=CaptureProfileId.ONLINE_DUAL_STREAM,
            discovery=_available_discoverer(CaptureProfileId.ONLINE_DUAL_STREAM),
            microphone_route_id=None,
            playback_stream_route_id=None,
            output_sink_route_id=None,
            input_fn=None,
        )


def test_resolve_route_selections_prompts_for_single_playback_stream() -> None:
    selections = _resolve_route_selections(
        profile=CaptureProfileId.ONLINE_DUAL_STREAM,
        discovery=_available_discoverer(CaptureProfileId.ONLINE_DUAL_STREAM),
        microphone_route_id=None,
        playback_stream_route_id=None,
        output_sink_route_id=None,
        input_fn=lambda _prompt: "1",
    )

    assert selections.playback_stream_route_id == "pulse:sink-input:151"


def test_resolve_route_selections_rejects_inactive_explicit_playback_stream() -> None:
    discovery = _discovery_with_playback_streams(
        PlaybackStream(
            id=51,
            route_id="pulse:sink-input:151",
            name="Paused Stream",
            app_binary_name="firefox",
            stream_name="WebRTC playback",
            media_name="Meet",
            sink_input_id="151",
            active=False,
        )
    )

    with pytest.raises(ValueError, match="Selected playback stream route .* is not active"):
        _resolve_route_selections(
            profile=CaptureProfileId.ONLINE_DUAL_STREAM,
            discovery=discovery,
            microphone_route_id=None,
            playback_stream_route_id="pulse:sink-input:151",
            output_sink_route_id=None,
            input_fn=None,
        )


def test_resolve_route_selections_rejects_inactive_only_playback_streams() -> None:
    discovery = _discovery_with_playback_streams(
        PlaybackStream(
            id=51,
            route_id="pulse:sink-input:151",
            name="Paused Stream",
            app_binary_name="firefox",
            stream_name="WebRTC playback",
            media_name="Meet",
            sink_input_id="151",
            active=False,
        )
    )

    with pytest.raises(ValueError, match="No active playback streams were discovered"):
        _resolve_route_selections(
            profile=CaptureProfileId.ONLINE_DUAL_STREAM,
            discovery=discovery,
            microphone_route_id=None,
            playback_stream_route_id=None,
            output_sink_route_id=None,
            input_fn=lambda _prompt: "1",
        )


def test_resolve_route_selections_rejects_invalid_prompt_choice() -> None:
    with pytest.raises(ValueError, match="Invalid microphone routes selection"):
        _resolve_route_selections(
            profile=CaptureProfileId.ONLINE_DUAL_STREAM,
            discovery=_ambiguous_discoverer(CaptureProfileId.ONLINE_DUAL_STREAM),
            microphone_route_id=None,
            playback_stream_route_id=None,
            output_sink_route_id=None,
            input_fn=lambda _prompt: "x",
        )


def test_wait_for_record_stop_continues_after_invalid_command(
    capsys: CaptureFixture[str],
) -> None:
    answers = iter(["later", "", "q"])

    _wait_for_record_stop(lambda _prompt: next(answers))

    captured = capsys.readouterr()
    assert captured.out.count("Recording continues. Type q to stop.") == 2


def test_capture_event_monitor_drains_progress_and_returns_finalization(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    created_session = create_session(
        profile=parse_capture_profile("online"),
        language="en",
        data_root=tmp_path,
    )
    start_payload = _monitor_start_payload()
    worker = _EventWorker(
        [
            ProtocolMessage(
                type="segments_flushed",
                session_id=SESSION_ID,
                payload={
                    "segments": [
                        _segment_payload(source_id="local_mic", index=1),
                        _segment_payload(source_id="system_audio", index=1),
                    ]
                },
            ),
            ProtocolMessage(
                type="capture_status",
                session_id=SESSION_ID,
                payload={
                    "status": "recording",
                    "elapsed_seconds": 12.5,
                    "sealed_segment_count": 1,
                },
            ),
            ProtocolMessage(
                type="capture_route_event",
                session_id=SESSION_ID,
                payload={
                    "event": "degraded",
                    "code": "dedicated_route_unavailable",
                    "message": "Using selected output sink monitor.",
                    "source_id": "system_audio",
                    "occurred_at": None,
                    "elapsed_seconds": 0.0,
                    "segment_index": 1,
                    "route_id": "pipewire:node:42",
                    "route_label": "Headphones",
                    "capture_mode": "system_mix_remote",
                },
            ),
            ProtocolMessage(
                type="capture_route_event",
                session_id=SESSION_ID,
                payload={
                    "event": "recovered",
                    "code": "dedicated_route_recovered",
                    "message": "Dedicated meeting route recovered.",
                    "source_id": "system_audio",
                    "occurred_at": None,
                    "elapsed_seconds": 30.0,
                    "segment_index": 2,
                    "route_id": "pulse:sink-input:51",
                    "route_label": "Video call",
                    "capture_mode": "dedicated_route",
                },
            ),
            _finalized_capture_message(),
        ]
    )
    monitor = _CaptureEventMonitor(
        cast(WorkerProcess, worker),
        record_path=created_session.record_path,
        start_payload=start_payload,
    )

    monitor.start()
    finalized = monitor.wait_for_finalized()

    record = json.loads(created_session.record_path.read_text(encoding="utf-8"))
    captured = capsys.readouterr()
    assert finalized.type == "capture_finalized"
    assert record["capture_metadata"]["segment_count"] == 2
    assert record["capture_metadata"]["segments"][0]["source_id"] == "local_mic"
    assert record["capture_metadata"]["segments"][1]["source_id"] == "system_audio"
    assert record["capture_metadata"]["sources"][0]["route_label"] == "Studio mic"
    assert record["degradations"][0]["capture_mode"] == "system_mix_remote"
    assert record["degradations"][1]["event"] == "recovered"
    assert "Sealed local_mic segment 1." in captured.out
    assert "Sealed system_audio segment 1." in captured.out
    assert "Recording: 12s elapsed, 1 sealed segment(s)." in captured.out
    assert "Capture degraded: Using selected output sink monitor." in captured.out
    assert "Capture recovered: Dedicated meeting route recovered." in captured.out


def test_capture_event_monitor_raises_structured_worker_error(tmp_path: Path) -> None:
    worker = _EventWorker(
        [
            ProtocolMessage(
                type="error",
                session_id=SESSION_ID,
                payload={
                    "code": "capture_flush_failed",
                    "message": "disk full",
                    "recoverable": False,
                    "details": {},
                },
            )
        ]
    )
    created_session = create_session(
        profile=parse_capture_profile("online"),
        language="en",
        data_root=tmp_path,
    )
    monitor = _CaptureEventMonitor(
        cast(WorkerProcess, worker),
        record_path=created_session.record_path,
        start_payload=_monitor_start_payload(),
    )

    monitor.start()

    with pytest.raises(ProtocolError, match="disk full"):
        monitor.wait_for_finalized()


def test_wait_for_record_stop_surfaces_background_capture_error(tmp_path: Path) -> None:
    worker = _EventWorker(
        [
            ProtocolMessage(
                type="error",
                session_id=SESSION_ID,
                payload={
                    "code": "capture_flush_failed",
                    "message": "disk full",
                    "recoverable": False,
                    "details": {},
                },
            )
        ]
    )
    created_session = create_session(
        profile=parse_capture_profile("online"),
        language="en",
        data_root=tmp_path,
    )
    monitor = _CaptureEventMonitor(
        cast(WorkerProcess, worker),
        record_path=created_session.record_path,
        start_payload=_monitor_start_payload(),
    )
    prompt_started = threading.Event()

    def blocking_input(_prompt: str) -> str:
        prompt_started.set()
        time.sleep(5)
        return "q"

    monitor.start()

    with pytest.raises(ProtocolError, match="disk full"):
        _wait_for_record_stop(blocking_input, capture_monitor=monitor)
    assert prompt_started.is_set()


def test_capture_event_monitor_propagates_reader_failure(tmp_path: Path) -> None:
    created_session = create_session(
        profile=parse_capture_profile("online"),
        language="en",
        data_root=tmp_path,
    )
    monitor = _CaptureEventMonitor(
        cast(WorkerProcess, _EventWorker([])),
        record_path=created_session.record_path,
        start_payload=_monitor_start_payload(),
    )

    monitor.start()

    with pytest.raises(StopIteration):
        monitor.wait_for_finalized()


def test_capture_event_monitor_rejects_unexpected_event_type(tmp_path: Path) -> None:
    worker = _EventWorker([ProtocolMessage(type="hello_ack", session_id=SESSION_ID)])
    created_session = create_session(
        profile=parse_capture_profile("online"),
        language="en",
        data_root=tmp_path,
    )
    monitor = _CaptureEventMonitor(
        cast(WorkerProcess, worker),
        record_path=created_session.record_path,
        start_payload=_monitor_start_payload(),
    )

    monitor.start()

    with pytest.raises(ProtocolError, match="Unexpected capture event 'hello_ack'"):
        monitor.wait_for_finalized()


def test_record_requires_interactive_stop_before_capture_starts(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "record",
            "--profile",
            "online",
            "--language",
            "en",
            "--playback-stream",
            "pulse:sink-input:151",
        ],
        data_root=tmp_path,
        discoverer=_available_discoverer,
        worker_factory=FakeWorkerProcess,
    )
    captured = capsys.readouterr()
    record_path = next((tmp_path / "sessions").iterdir()) / "session.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))

    assert exit_code == 1
    assert "recording requires an interactive terminal to stop" in captured.out
    assert "capture_metadata" not in record


def test_record_rejects_ambiguous_routes_without_explicit_selection(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    exit_code = main(
        ["record", "--profile", "online", "--language", "en"],
        data_root=tmp_path,
        discoverer=_ambiguous_discoverer,
        worker_factory=FakeWorkerProcess,
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Multiple microphone routes were discovered; pass --mic." in captured.out


@pytest.mark.parametrize("exc_type", [EOFError, KeyboardInterrupt])
def test_record_route_prompt_cancellation_does_not_start_capture(
    exc_type: type[BaseException],
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    FakeWorkerProcess.last_start_payload = None

    def _cancel_route_prompt(_prompt: str) -> str:
        raise exc_type

    exit_code = main(
        ["record", "--profile", "online", "--language", "en"],
        data_root=tmp_path,
        discoverer=_ambiguous_discoverer,
        worker_factory=FakeWorkerProcess,
        input_fn=_cancel_route_prompt,
    )

    captured = capsys.readouterr()
    record_path = next((tmp_path / "sessions").iterdir()) / "session.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert "Recording setup cancelled." in captured.out
    assert FakeWorkerProcess.last_start_payload is None
    assert record["status"] == "created"
    assert "capture" not in record


def test_record_uses_explicit_noninteractive_route_flags(tmp_path: Path) -> None:
    FakeWorkerProcess.last_start_payload = None

    exit_code = main(
        [
            "record",
            "--profile",
            "online",
            "--language",
            "en",
            "--mic",
            "pipewire:node:32",
            "--playback-stream",
            "pulse:sink-input:152",
            "--output-sink",
            "pipewire:node:43",
        ],
        data_root=tmp_path,
        discoverer=_ambiguous_discoverer,
        worker_factory=FakeWorkerProcess,
        input_fn=lambda _prompt: "q",
    )

    assert exit_code == 0
    assert FakeWorkerProcess.last_start_payload is not None
    route_ids = [route.route_id for route in FakeWorkerProcess.last_start_payload.routes]
    assert route_ids == ["pipewire:node:32", "pulse:sink-input:152", "pipewire:node:43"]


def test_record_verbose_prints_route_plan_and_enables_worker_diagnostics(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    VerboseCapturingWorkerProcess.last_capture_diagnostics = None

    exit_code = main(
        [
            "record",
            "--profile",
            "online",
            "--language",
            "en",
            "--playback-stream",
            "pulse:sink-input:151",
            "--verbose",
        ],
        data_root=tmp_path,
        discoverer=_available_discoverer,
        worker_factory=VerboseCapturingWorkerProcess,
        input_fn=lambda _prompt: "q",
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Record diagnostics:" in captured.out
    assert "profile: online_dual_stream" in captured.out
    assert "route_id=pipewire:node:31" in captured.out
    assert "route_id=pulse:sink-input:151" in captured.out
    assert VerboseCapturingWorkerProcess.last_capture_diagnostics is True


def test_record_dry_run_persists_discovery_and_prints_routes(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    exit_code = main(
        ["record", "--profile", "online", "--dry-run"],
        data_root=tmp_path,
        discoverer=_unavailable_discoverer,
    )

    captured = capsys.readouterr()
    record_path = next((tmp_path / "sessions").iterdir()) / "session.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert "Audio discovery: unavailable (online_dual_stream)" in captured.out
    assert "PipeWire command 'pw-dump' was not found." in captured.out
    assert record["discovery"]["status"] == "unavailable"
    assert record["discovery"]["profile"] == "online_dual_stream"
    assert "capture_metadata" not in record


def test_record_does_not_start_capture_when_discovery_unavailable(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    exit_code = main(
        ["record", "--profile", "online", "--language", "en"],
        data_root=tmp_path,
        discoverer=_unavailable_discoverer,
        worker_factory=FakeWorkerProcess,
    )

    captured = capsys.readouterr()
    record_path = next((tmp_path / "sessions").iterdir()) / "session.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert "Capture not started: audio discovery is unavailable." in captured.out
    assert record["discovery"]["status"] == "unavailable"
    assert "capture_metadata" not in record


def test_record_reports_worker_failure_without_capture_metadata(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "record",
            "--profile",
            "online",
            "--language",
            "en",
            "--playback-stream",
            "pulse:sink-input:151",
        ],
        data_root=tmp_path,
        discoverer=_available_discoverer,
        worker_factory=FailingWorkerProcess,
        input_fn=lambda _prompt: "q",
    )

    captured = capsys.readouterr()
    record_path = next((tmp_path / "sessions").iterdir()) / "session.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert "Capture failed: worker refused handshake" in captured.out
    assert record["discovery"]["status"] == "available"
    assert record["status"] == "failed"
    assert record["recording"]["stop_reason"] == "error"
    assert record["recording"]["finalization"]["status"] == "failed"
    assert record["capture_plan"]["status"] == "failed"
    assert record["failures"][0]["message"] == "worker refused handshake"
    assert "capture_metadata" not in record


def test_record_persists_worker_spawn_failure_without_capture_metadata(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    def _spawn_fails(*, session_id: str, session_dir: Path) -> FakeWorkerProcess:
        raise OSError("worker process limit reached")

    exit_code = main(
        [
            "record",
            "--profile",
            "online",
            "--language",
            "en",
            "--playback-stream",
            "pulse:sink-input:151",
        ],
        data_root=tmp_path,
        discoverer=_available_discoverer,
        worker_factory=_spawn_fails,
        input_fn=lambda _prompt: "q",
    )

    captured = capsys.readouterr()
    record_path = next((tmp_path / "sessions").iterdir()) / "session.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert "Capture failed: worker process limit reached" in captured.out
    assert record["status"] == "failed"
    assert record["recording"]["stop_reason"] == "error"
    assert record["recording"]["finalization"]["status"] == "failed"
    assert record["recording"]["finalization"]["error"] == "worker process limit reached"
    assert record["capture_plan"]["status"] == "failed"
    assert record["failures"][0]["message"] == "worker process limit reached"
    assert "capture_metadata" not in record


def test_record_rejects_nonzero_worker_exit_without_capture_metadata(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "record",
            "--profile",
            "online",
            "--language",
            "en",
            "--playback-stream",
            "pulse:sink-input:151",
        ],
        data_root=tmp_path,
        discoverer=_available_discoverer,
        worker_factory=NonzeroExitWorkerProcess,
        input_fn=lambda _prompt: "q",
    )

    captured = capsys.readouterr()
    record_path = next((tmp_path / "sessions").iterdir()) / "session.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert "Capture failed: Worker exited with status 3." in captured.out
    assert record["status"] == "failed"
    assert record["recording"]["finalization"]["error"] == "Worker exited with status 3."
    assert record["failures"][0]["code"] == "capture_failed"
    assert "capture_metadata" not in record


def test_record_keyboard_interrupt_finalizes_with_interrupted_stop_reason(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    def _interrupt_stop(_prompt: str) -> str:
        raise KeyboardInterrupt

    exit_code = main(
        [
            "record",
            "--profile",
            "online",
            "--language",
            "en",
            "--playback-stream",
            "pulse:sink-input:151",
        ],
        data_root=tmp_path,
        discoverer=_available_discoverer,
        worker_factory=FakeWorkerProcess,
        input_fn=_interrupt_stop,
    )

    captured = capsys.readouterr()
    record_path = next((tmp_path / "sessions").iterdir()) / "session.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert "Recording stop requested; finalizing capture." in captured.out
    assert record["status"] == "recorded"
    assert record["recording"]["stop_reason"] == "interrupted"
    assert record["recording"]["finalization"]["status"] == "finalized"


def test_record_real_worker_monitor_path_finalizes(
    tmp_path: Path,
) -> None:
    MonitoredWorkerProcess.stop_requested = False

    exit_code = main(
        [
            "record",
            "--profile",
            "online",
            "--language",
            "en",
            "--playback-stream",
            "pulse:sink-input:151",
        ],
        data_root=tmp_path,
        discoverer=_available_discoverer,
        worker_factory=MonitoredWorkerProcess,
        input_fn=lambda _prompt: "q",
    )

    record_path = next((tmp_path / "sessions").iterdir()) / "session.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert MonitoredWorkerProcess.stop_requested
    assert record["status"] == "recorded"
    assert record["recording"]["finalization"]["status"] == "finalized"


def test_record_startup_interrupt_does_not_wait_on_unstarted_monitor(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    InterruptingStartWorkerProcess.stop_requested = False
    InterruptingStartWorkerProcess.exited = False

    exit_code = main(
        [
            "record",
            "--profile",
            "online",
            "--language",
            "en",
            "--playback-stream",
            "pulse:sink-input:151",
        ],
        data_root=tmp_path,
        discoverer=_available_discoverer,
        worker_factory=InterruptingStartWorkerProcess,
        input_fn=lambda _prompt: "q",
    )

    captured = capsys.readouterr()
    record_path = next((tmp_path / "sessions").iterdir()) / "session.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert "Recording stop requested; finalizing capture." in captured.out
    assert InterruptingStartWorkerProcess.stop_requested
    assert InterruptingStartWorkerProcess.exited
    assert record["status"] == "recorded"
    assert record["recording"]["stop_reason"] == "interrupted"
    assert record["recording"]["finalization"]["status"] == "finalized"


def test_record_wait_interrupt_after_finalization_does_not_finalize_again(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    InterruptingWaitAfterFinalizeWorkerProcess.stop_calls = 0

    exit_code = main(
        [
            "record",
            "--profile",
            "online",
            "--language",
            "en",
            "--playback-stream",
            "pulse:sink-input:151",
        ],
        data_root=tmp_path,
        discoverer=_available_discoverer,
        worker_factory=InterruptingWaitAfterFinalizeWorkerProcess,
        input_fn=lambda _prompt: "q",
    )

    captured = capsys.readouterr()
    record_path = next((tmp_path / "sessions").iterdir()) / "session.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert "Worker teardown interrupted after capture finalization." in captured.out
    assert InterruptingWaitAfterFinalizeWorkerProcess.stop_calls == 1
    assert record["status"] == "recorded"
    assert record["recording"]["finalization"]["status"] == "finalized"


def test_record_keyboard_interrupt_persists_finalization_failure(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    def _interrupt_stop(_prompt: str) -> str:
        raise KeyboardInterrupt

    exit_code = main(
        [
            "record",
            "--profile",
            "online",
            "--language",
            "en",
            "--playback-stream",
            "pulse:sink-input:151",
        ],
        data_root=tmp_path,
        discoverer=_available_discoverer,
        worker_factory=FinalizationFailingWorkerProcess,
        input_fn=_interrupt_stop,
    )

    captured = capsys.readouterr()
    record_path = next((tmp_path / "sessions").iterdir()) / "session.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert "Capture failed: finalization failed" in captured.out
    assert record["status"] == "failed"
    assert record["recording"]["finalization"]["error"] == "finalization failed"
    assert record["failures"][0]["message"] == "finalization failed"


def test_record_uses_configurable_worker_exit_timeout(tmp_path: Path) -> None:
    TimeoutCapturingWorkerProcess.last_timeout = None

    exit_code = main(
        [
            "record",
            "--profile",
            "online",
            "--language",
            "en",
            "--playback-stream",
            "pulse:sink-input:151",
            "--worker-exit-timeout",
            "7.5",
        ],
        data_root=tmp_path,
        discoverer=_available_discoverer,
        worker_factory=TimeoutCapturingWorkerProcess,
        input_fn=lambda _prompt: "q",
    )

    assert exit_code == 0
    assert TimeoutCapturingWorkerProcess.last_timeout == 7.5


def test_record_worker_exit_timeout_default_is_generous() -> None:
    assert WORKER_EXIT_TIMEOUT_SECONDS == 30.0


def test_main_summarize_applies_active_overlay_and_manual_notes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    session_dir, store, merged_record, overlay_record = _build_summarize_session_bundle(tmp_path)
    notes_path = tmp_path / "notes.md"
    notes_path.write_text("- manual note from CLI", encoding="utf-8")
    captured: dict[str, object] = {}

    def _fake_run_summarization_stage(
        *,
        store: ArtifactStore,
        ledger: ProcessingStageLedger,
        merged_transcript_record: ArtifactRecord,
        provider: object,
        correction_overlay_record: ArtifactRecord | None = None,
        policy: SummarizationPolicy | None = None,
        manual_notes_record: ArtifactRecord | None = None,
        stage: str = "summarization",
        attempt: int = 1,
        skip_completed: bool = True,
        now: datetime | None = None,
    ) -> SimpleNamespace:
        captured["merged"] = merged_transcript_record
        captured["overlay"] = correction_overlay_record
        captured["manual"] = manual_notes_record
        captured["language"] = policy.language if policy is not None else None
        return SimpleNamespace(
            summary_record=store.store(
                artifact_type="summary",
                name="summary/final.json",
                data=b"{}",
                version=1,
            ),
        )

    monkeypatch.setattr("mena.cli.run_summarization_stage", _fake_run_summarization_stage)
    settings = AppSettings.from_env(env={}, home=tmp_path)
    _persist_ready_runtime_profile_state(settings, monkeypatch)

    exit_code = main(
        [
            "summarize",
            str(session_dir),
            "--manual-notes",
            str(notes_path),
            "--language",
            "de-CH",
        ],
        settings=settings,
    )
    captured_output = capsys.readouterr()

    assert exit_code == 0
    assert captured.get("merged") == merged_record
    assert captured.get("overlay") == overlay_record
    assert captured.get("manual") is not None
    assert captured.get("language") == "de-CH"
    provenance_records = store.list(artifact_type="runtime_profile_provenance")
    assert len(provenance_records) == 1
    provenance = json.loads(store.retrieve(provenance_records[0].id))
    assert provenance["run_id"].startswith("summarize-")
    assert any(
        stage["stage"] == "summarization" for stage in provenance["actual_profile"]["stages"]
    )
    assert "Summary written to:" in captured_output.out


def test_main_summarize_uses_unique_runtime_profile_provenance_per_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_dir, store, _, _ = _build_summarize_session_bundle(tmp_path)

    def _fake_run_summarization_stage(
        *,
        store: ArtifactStore,
        ledger: ProcessingStageLedger,
        merged_transcript_record: ArtifactRecord,
        provider: object,
        correction_overlay_record: ArtifactRecord | None = None,
        policy: SummarizationPolicy | None = None,
        manual_notes_record: ArtifactRecord | None = None,
        stage: str = "summarization",
        attempt: int = 1,
        skip_completed: bool = True,
        now: datetime | None = None,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            summary_record=store.store(
                artifact_type="summary",
                name=f"summary/{ledger.run_id}.json",
                data=b"{}",
                version=1,
            ),
        )

    monkeypatch.setattr("mena.cli.run_summarization_stage", _fake_run_summarization_stage)
    settings = AppSettings.from_env(env={}, home=tmp_path)
    _persist_ready_runtime_profile_state(settings, monkeypatch)

    assert main(["summarize", str(session_dir)], settings=settings) == 0
    assert main(["summarize", str(session_dir)], settings=settings) == 0

    provenance_records = store.list(artifact_type="runtime_profile_provenance")
    assert len(provenance_records) == 2
    run_ids = {json.loads(store.retrieve(record.id))["run_id"] for record in provenance_records}
    assert len(run_ids) == 2


def test_main_summarize_records_terminal_runtime_stewardship_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    session_dir, store, _, _ = _build_summarize_session_bundle(tmp_path)

    def _fake_unload_ollama_model(**_kwargs: object) -> str:
        return "unloaded"

    def _fake_run_summarization_stage(
        *,
        store: ArtifactStore,
        ledger: ProcessingStageLedger,
        provider: object,
        **_kwargs: object,
    ) -> SimpleNamespace:
        cast(Any, provider)._runtime_resource_used = True
        return SimpleNamespace(
            summary_record=store.store(
                artifact_type="summary",
                name=f"summary/{ledger.run_id}.json",
                data=b"{}",
                version=1,
            ),
        )

    monkeypatch.setattr("mena.summarization.unload_ollama_model", _fake_unload_ollama_model)
    monkeypatch.setattr("mena.cli.run_summarization_stage", _fake_run_summarization_stage)
    settings = AppSettings.from_env(env={}, home=tmp_path)
    _persist_ready_runtime_profile_state(settings, monkeypatch)

    exit_code = main(["summarize", str(session_dir)], settings=settings)
    captured = capsys.readouterr()

    stewardship_records = store.list(artifact_type="runtime_stewardship_provenance")
    assert exit_code == 0
    assert "Runtime stewardship: Ollama model release completed: unloaded." in captured.out
    assert len(stewardship_records) == 1
    payload = json.loads(store.retrieve(stewardship_records[0].id))
    assert payload["boundary"] == "summarize"
    assert payload["releases"][0]["status"] == "released"
    assert payload["releases"][0]["lease"]["stage"] == "summarization"
    assert payload["releases"][0]["lease"]["provider"] == "ollama"


def test_main_summarize_uses_session_language_before_config_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_dir, _, _, _ = _build_summarize_session_bundle(tmp_path)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[summary]
language = "it-CH"

[ollama]
endpoint = "http://127.0.0.1:11436"
model = "mistral:7b"
""".strip(),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def _fake_run_summarization_stage(
        *,
        store: ArtifactStore,
        ledger: ProcessingStageLedger,
        merged_transcript_record: ArtifactRecord,
        provider: object,
        correction_overlay_record: ArtifactRecord | None = None,
        policy: SummarizationPolicy | None = None,
        manual_notes_record: ArtifactRecord | None = None,
        stage: str = "summarization",
        attempt: int = 1,
        skip_completed: bool = True,
        now: datetime | None = None,
    ) -> SimpleNamespace:
        provider_metadata = cast(Any, provider).metadata
        captured["language"] = policy.language if policy is not None else None
        captured["provider_model"] = provider_metadata.model
        captured["provider_endpoint"] = provider_metadata.endpoint
        return SimpleNamespace(
            summary_record=store.store(
                artifact_type="summary",
                name="summary/final.json",
                data=b"{}",
                version=1,
            ),
        )

    monkeypatch.setattr("mena.cli.run_summarization_stage", _fake_run_summarization_stage)
    settings = AppSettings.from_env(env={}, home=tmp_path, config_path=config_path)
    _persist_ready_runtime_profile_state(settings, monkeypatch)

    exit_code = main(["summarize", str(session_dir)], settings=settings)

    assert exit_code == 0
    assert captured["language"] == "fr-CH"
    assert captured["provider_model"] == "mistral:7b"
    assert captured["provider_endpoint"] == "http://127.0.0.1:11436"


def test_main_summarize_cli_language_overrides_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_dir, _, _, _ = _build_summarize_session_bundle(tmp_path)
    config_path = tmp_path / "config.toml"
    config_path.write_text("[summary]\nlanguage = 'fr-CH'\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def _fake_run_summarization_stage(
        *,
        store: ArtifactStore,
        ledger: ProcessingStageLedger,
        merged_transcript_record: ArtifactRecord,
        provider: object,
        correction_overlay_record: ArtifactRecord | None = None,
        policy: SummarizationPolicy | None = None,
        manual_notes_record: ArtifactRecord | None = None,
        stage: str = "summarization",
        attempt: int = 1,
        skip_completed: bool = True,
        now: datetime | None = None,
    ) -> SimpleNamespace:
        captured["language"] = policy.language if policy is not None else None
        return SimpleNamespace(
            summary_record=store.store(
                artifact_type="summary",
                name="summary/final.json",
                data=b"{}",
                version=1,
            ),
        )

    monkeypatch.setattr("mena.cli.run_summarization_stage", _fake_run_summarization_stage)
    settings = AppSettings.from_env(env={}, home=tmp_path, config_path=config_path)
    _persist_ready_runtime_profile_state(settings, monkeypatch)

    exit_code = main(
        ["summarize", str(session_dir), "--language", "it-CH"],
        settings=settings,
    )

    assert exit_code == 0
    assert captured["language"] == "it-CH"


def test_main_summarize_rejects_empty_explicit_cli_language(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    session_dir, _, _, _ = _build_summarize_session_bundle(tmp_path)
    config_path = tmp_path / "config.toml"
    config_path.write_text("[summary]\nlanguage = 'fr-CH'\n", encoding="utf-8")

    def _fake_run_summarization_stage(
        *,
        store: ArtifactStore,
        ledger: ProcessingStageLedger,
        merged_transcript_record: ArtifactRecord,
        provider: object,
        correction_overlay_record: ArtifactRecord | None = None,
        policy: SummarizationPolicy | None = None,
        manual_notes_record: ArtifactRecord | None = None,
        stage: str = "summarization",
        attempt: int = 1,
        skip_completed: bool = True,
        now: datetime | None = None,
    ) -> SimpleNamespace:
        raise AssertionError("summarization should not run with an invalid CLI language")

    monkeypatch.setattr("mena.cli.run_summarization_stage", _fake_run_summarization_stage)

    exit_code = main(
        ["summarize", str(session_dir), "--language", ""],
        config_path=config_path,
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Cannot summarize session: --language must be a non-empty string" in captured.out


def test_main_reports_config_errors_before_command_execution(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("[summary]\nlanguage = 123\n", encoding="utf-8")

    exit_code = main(
        ["record", "--profile", "online", "--dry-run"],
        data_root=tmp_path,
        discoverer=_available_discoverer,
        config_path=config_path,
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Cannot load configuration:" in captured.out
    assert not (tmp_path / "sessions").exists()


def test_main_process_does_not_preconfigure_default_semantic_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_dir = _create_session_record(tmp_path)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[ollama]
endpoint = "http://127.0.0.1:11436"
model = "semantic-model:latest"
""".strip(),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def _fake_run_processing_command(**kwargs: object) -> int:
        provider_factory = cast(Any, kwargs["semantic_coherence_provider_factory"])
        provider = provider_factory()
        captured["model"] = provider.metadata.model
        captured["endpoint"] = provider.metadata.endpoint
        return 0

    monkeypatch.setattr("mena.cli._run_processing_command", _fake_run_processing_command)

    exit_code = main(["process", str(session_dir)], config_path=config_path)

    assert exit_code == 0
    assert captured["model"] == "llama3.2:3b"
    assert captured["endpoint"] == "http://localhost:11434"


def test_main_summarize_accepts_session_json_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_dir, _, _, _ = _build_summarize_session_bundle(tmp_path)
    session_json_path = session_dir / SESSION_RECORD_FILENAME
    captured: dict[str, object] = {}

    def _fake_run_summarization_stage(
        *,
        store: ArtifactStore,
        ledger: ProcessingStageLedger,
        merged_transcript_record: ArtifactRecord,
        provider: object,
        correction_overlay_record: ArtifactRecord | None = None,
        policy: SummarizationPolicy | None = None,
        manual_notes_record: ArtifactRecord | None = None,
        stage: str = "summarization",
        attempt: int = 1,
        skip_completed: bool = True,
        now: datetime | None = None,
    ) -> SimpleNamespace:
        captured["merged"] = merged_transcript_record
        captured["overlay"] = correction_overlay_record
        captured["manual"] = manual_notes_record
        captured["language"] = policy.language if policy is not None else None
        return SimpleNamespace(
            summary_record=store.store(
                artifact_type="summary",
                name="summary/final.json",
                data=b"{}",
                version=1,
            ),
        )

    monkeypatch.setattr("mena.cli.run_summarization_stage", _fake_run_summarization_stage)
    settings = AppSettings.from_env(env={}, home=tmp_path)
    _persist_ready_runtime_profile_state(settings, monkeypatch)

    exit_code = main(["summarize", str(session_json_path)], settings=settings)

    assert exit_code == 0


def test_main_summarize_requires_accepted_runtime_profile_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    session_dir, _, _, _ = _build_summarize_session_bundle(tmp_path)
    settings = AppSettings.from_env(env={}, home=tmp_path)
    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state",
        lambda *_args, **_kwargs: _setup_state(settings=settings, pyannote_token_present=True),
    )

    exit_code = main(["summarize", str(session_dir)], settings=settings)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Accepted runtime profile state is missing" in captured.out
    assert str(settings.paths.state_home / "setup-state.json") in captured.out


def test_main_summarize_revalidates_only_summarization_runtime_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_dir, store, _, _ = _build_summarize_session_bundle(tmp_path)
    settings = AppSettings.from_env(env={}, home=tmp_path)
    ready_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = build_runtime_profile_state(evaluate_setup(get_alpha_local_profile(), ready_state))
    persist_runtime_profile_state(accepted, paths=settings.paths)
    observed_profile_stages: list[tuple[str, ...]] = []

    def _detect_summary_state(
        _settings: AppSettings,
        **kwargs: object,
    ) -> LocalInstallationState:
        profile = cast(Any, kwargs["profile"])
        observed_profile_stages.append(tuple(stage.stage for stage in profile.stages))
        return _setup_state(
            settings=settings,
            hardware=HardwareFacts(
                system="Linux",
                ram_gb=32.0,
                cuda_available=False,
                vram_gb=None,
                avx2_available=True,
            ),
            asr_model_present=False,
            diarization_model_present=False,
            pyannote_token_present=False,
            python_surfaces={"faster_whisper": False, "pyannote.audio": False},
        )

    def _fake_run_summarization_stage(
        *,
        store: ArtifactStore,
        ledger: ProcessingStageLedger,
        merged_transcript_record: ArtifactRecord,
        provider: object,
        correction_overlay_record: ArtifactRecord | None = None,
        policy: SummarizationPolicy | None = None,
        manual_notes_record: ArtifactRecord | None = None,
        stage: str = "summarization",
        attempt: int = 1,
        skip_completed: bool = True,
        now: datetime | None = None,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            summary_record=store.store(
                artifact_type="summary",
                name=f"summary/{ledger.run_id}.json",
                data=b"{}",
                version=1,
            ),
        )

    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state", _detect_summary_state
    )
    monkeypatch.setattr("mena.cli.run_summarization_stage", _fake_run_summarization_stage)

    assert main(["summarize", str(session_dir)], settings=settings) == 0

    assert observed_profile_stages == [("summarization",)]
    provenance_records = store.list(artifact_type="runtime_profile_provenance")
    assert len(provenance_records) == 1
    provenance = json.loads(store.retrieve(provenance_records[0].id))
    assert provenance["revalidation"]["ready"] is True
    assert provenance["actual_profile"]["stages"] == [
        {
            "device": "local-service",
            "model": settings.local_llm.ollama_model,
            "policy": "local-preferred, no setup-time remote fallback",
            "provider": "Ollama",
            "stage": "summarization",
        }
    ]


def test_summarize_runtime_scope_still_checks_ollama_after_bad_hardware_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    ready_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = build_runtime_profile_state(evaluate_setup(get_alpha_local_profile(), ready_state))
    persist_runtime_profile_state(accepted, paths=settings.paths)

    def _detect_summary_state(
        _settings: AppSettings,
        **_kwargs: object,
    ) -> LocalInstallationState:
        state = _setup_state(settings=settings, pyannote_token_present=True)
        return replace(
            state,
            hardware=HardwareFacts(
                system="Darwin",
                ram_gb=None,
                cuda_available=False,
                vram_gb=None,
                avx2_available=None,
            ),
            commands=(
                CommandCheck("mena", Path("/usr/bin/mena")),
                CommandCheck("ollama", None),
            ),
            ollama=OllamaState(
                endpoint=settings.local_llm.ollama_endpoint,
                model=settings.local_llm.ollama_model,
                command=CommandCheck("ollama", None),
                reachable=False,
                model_present=False,
                detail="ollama command missing",
            ),
        )

    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state", _detect_summary_state
    )

    with pytest.raises(ValueError, match="Install and start Ollama locally"):
        select_runtime_profile(
            settings=settings,
            runner=None,
            command_identity=None,
            hardware=None,
            allow_runtime_fallback=False,
            required_stages=SUMMARIZATION_RUNTIME_STAGES,
        )


def test_main_summarize_reports_missing_merged_transcript(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    session_dir = _create_session_record(tmp_path)

    exit_code = main(["summarize", str(session_dir)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "No merged transcript artifact found for this session." in captured.out


def test_main_summarize_rejects_invalid_session_path(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    exit_code = main(["summarize", str(tmp_path / "missing-session.json")])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert (
        "Cannot summarize session: Session path must be a session directory or"
        " an existing session.json path."
    ) in captured.out


def test_main_summarize_rejects_when_no_completed_correction_overlay_is_available(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    session_dir = _create_session_record(tmp_path)
    _persist_merged_transcript(
        store=FileSystemArtifactStore(session_id=SESSION_ID, session_dir=session_dir)
    )

    exit_code = main(["summarize", str(session_dir)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert (
        "No completed correction overlay found."
        " Run correction and complete it before summarization." in captured.out
    )


def test_main_summarize_reports_stage_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    def _run_stage_fails(*_: object, **__: object) -> None:
        raise SummarizationError("Local summary provider is unavailable.")

    monkeypatch.setattr("mena.cli.run_summarization_stage", _run_stage_fails)

    session_dir, _, _, _ = _build_summarize_session_bundle(tmp_path)
    settings = AppSettings.from_env(env={}, home=tmp_path)
    _persist_ready_runtime_profile_state(settings, monkeypatch)
    exit_code = main(["summarize", str(session_dir)], settings=settings)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Cannot summarize session: Local summary provider is unavailable." in captured.out


def test_main_export_command_exports_latest_summary_markdown(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    session_dir, store, merged_record = _build_verified_export_session_bundle(tmp_path)
    _persist_summary_artifact_for_cli(store=store, merged_transcript_record=merged_record)

    exit_code = main(["export", str(session_dir)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Export written to:" in captured.out
    assert "Export contract: summary-markdown.v1" in captured.out
    assert '"summary_artifact_id"' in captured.out
    assert '"merged_transcript_artifact_id"' in captured.out
    assert '"manual_notes_artifact_id"' in captured.out
    assert '"correction_overlay_artifact_id"' in captured.out

    export_line = next(
        line for line in captured.out.splitlines() if line.startswith("Export written to:")
    )
    export_path = Path(export_line.removeprefix("Export written to: ").strip())
    assert export_path.as_posix().startswith("artifacts/")
    assert (session_dir / export_path).exists()


def test_main_export_command_rejects_unknown_summary_artifact(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    session_dir, store, merged_record = _build_verified_export_session_bundle(tmp_path)
    _persist_summary_artifact_for_cli(store=store, merged_transcript_record=merged_record)

    exit_code = main(
        [
            "export",
            str(session_dir),
            "--summary-artifact-id",
            "summary-not-found-id",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert (
        "Cannot export session summary markdown: Summary artifact not found:"
        " summary-not-found-id." in captured.out
    )


def test_main_export_command_rejects_unverified_bundle(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    session_dir = _create_session_record(tmp_path)
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=session_dir)
    _persist_summary_artifact_for_cli(
        store=store,
        merged_transcript_record=None,
    )

    exit_code = main(["export", str(session_dir)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert (
        "Cannot export session summary markdown: Session bundle failed integrity verification"
        in captured.out
    )


def test_main_inspect_command_reports_bundle_integrity(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    session_dir = _create_session_record(tmp_path)

    exit_code = main(["inspect", str(session_dir)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert f"Integrity report for {session_dir}: OK" in captured.out
    assert '"ok": true' in captured.out
    assert '"artifact_count": 0' in captured.out


def test_main_inspect_command_rejects_invalid_session(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    missing_session_path = tmp_path / "missing-session"
    exit_code = main(["inspect", str(missing_session_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert f"Integrity report for {missing_session_path}: FAILED" in captured.out
    assert '"ok": false' in captured.out


def test_main_inspect_command_rejects_broken_bundle(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    session_dir = _create_session_record(tmp_path)
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=session_dir)
    artifact_record = store.store(
        artifact_type=MERGED_TRANSCRIPT_ARTIFACT_TYPE,
        name="merged/merged.json",
        data=b'{"version":1}',
        version=1,
        status="sealed",
        metadata={"contract": "merged-transcript.v1"},
    )
    (session_dir / artifact_record.path).unlink()

    exit_code = main(["inspect", str(session_dir)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert f"Integrity report for {session_dir}: FAILED" in captured.out
    assert '"ok": false' in captured.out
    assert '"code": "artifact_missing"' in captured.out


def test_resolve_latest_merged_transcript_record_ignores_corrupt_payload(
    tmp_path: Path,
) -> None:
    session_dir = _create_session_record(tmp_path)
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=session_dir)
    valid_record = _persist_merged_transcript(store=store)
    corrupt_record = store.store(
        artifact_type=MERGED_TRANSCRIPT_ARTIFACT_TYPE,
        name="merged/invalid.json",
        data=b"not-json",
        version=1,
        status="sealed",
        metadata={"contract": "merged-transcript.v1"},
    )
    resolved = _resolve_latest_merged_transcript_record(store=store)

    assert resolved is not None
    assert resolved.id == valid_record.id
    assert resolved.id != corrupt_record.id


def test_main_correct_handles_artifact_store_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    def _raise_artifact_error(**_kwargs: object) -> None:
        raise ArtifactStoreError("Unsupported artifact manifest version.")

    monkeypatch.setattr("mena.cli.run_correction_workflow", _raise_artifact_error)

    exit_code = main(["correct", str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Cannot run correction workflow: Unsupported artifact manifest version." in captured.out


def test_main_correct_playback_prompt_uses_injected_player(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    session_dir, source_records = _build_cli_playback_session(tmp_path, valid_source_refs=True)
    requests: list[PlaybackRequest] = []
    inputs = iter(["p", "s"])

    exit_code = main(
        ["correct", str(session_dir)],
        input_fn=lambda _: next(inputs),
        playback_player=requests.append,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert len(requests) == 1
    assert requests[0].source_id == "system_audio"
    assert requests[0].audio_path == session_dir / source_records["system_audio"].path
    assert "Correction completed. reviewed=1, skipped=1, corrected=0." in captured.out


def test_main_correct_reports_playback_source_reference_errors(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    session_dir, _ = _build_cli_playback_session(tmp_path, valid_source_refs=False)
    inputs = iter(["p", "s"])

    exit_code = main(
        ["correct", str(session_dir)],
        input_fn=lambda _: next(inputs),
        playback_player=lambda _: None,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Playback failed:" in captured.out
    assert "missing transcript artifact" in captured.out
    assert "Correction completed. reviewed=1, skipped=1, corrected=0." in captured.out


def test_main_correct_does_not_select_runtime_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Path] = []

    def _fake_run_correction_workflow(**kwargs: object) -> None:
        calls.append(cast(Path, kwargs["session_path"]))

    def _fail_runtime_profile_selection(**_kwargs: object) -> None:
        raise AssertionError("correction must not acquire runtime resources")

    monkeypatch.setattr("mena.cli.run_correction_workflow", _fake_run_correction_workflow)
    monkeypatch.setattr("mena.cli.select_runtime_profile", _fail_runtime_profile_selection)

    assert main(["correct", str(tmp_path)]) == 0
    assert calls == [tmp_path]


def test_run_processing_command_executes_stage_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    session = _build_processing_session(tmp_path)
    calls: list[str] = []

    def _fake_run_transcription_stage(
        *,
        store: FileSystemArtifactStore,
        ledger: object,
        provider: object,
        source_artifact: object,
        source_id: str | None = None,
        audio_path: object = None,
        language: str | None = None,
        attempt: int = 1,
        now: object = None,
    ) -> object:
        calls.append(f"transcription:{source_id}:{language}")
        transcript_record = store.store(
            artifact_type="transcript",
            name=f"transcript-{source_id}.json",
            data=b"{}",
            version=1,
            metadata={},
        )
        return SimpleNamespace(transcript_record=transcript_record)

    def _fake_run_diarization_stage(
        *,
        store: FileSystemArtifactStore,
        ledger: object,
        provider: object,
        source_artifact: object,
        context: object,
        audio_path: object = None,
        attempt: int = 1,
        now: object = None,
        **_kwargs: object,
    ) -> object:
        source_id = getattr(context, "source_id", "unknown")
        regular_record = store.store(
            artifact_type="diarization",
            name=f"regular_{source_id}.json",
            data=b"{}",
            version=1,
            metadata={},
        )
        exclusive_record = store.store(
            artifact_type="diarization",
            name=f"exclusive_{source_id}.json",
            data=b"{}",
            version=1,
            metadata={},
        )
        calls.append("diarization")
        return SimpleNamespace(
            artifacts=(
                SimpleNamespace(kind="regular_timeline"),
                SimpleNamespace(kind="exclusive_projection"),
            ),
            artifact_records=(regular_record, exclusive_record),
            skipped=False,
        )

    def _fake_run_merge_timing_stage(
        *,
        store: FileSystemArtifactStore,
        ledger: object,
        local_transcript_record: object,
        remote_transcript_record: object,
        remote_regular_diarization_record: object | None = None,
        remote_exclusive_diarization_record: object | None = None,
        attempt: int = 1,
        now: object = None,
        **_kwargs: object,
    ) -> object:
        calls.append("merge")
        merged_record = store.store(
            artifact_type="merged_transcript",
            name="merged.json",
            data=b"{}",
            version=1,
            metadata={
                "policy_config_id": "cfg",
                "local_source_id": "local_mic",
                "remote_source_id": "system_audio",
                "remote_diarization_status": "used",
                "remote_diarization_reason": "unused",
            },
        )
        return SimpleNamespace(merged_transcript_record=merged_record)

    def _fake_run_confidence_flagging_stage(
        *,
        store: FileSystemArtifactStore,
        ledger: object,
        merged_transcript_record: object,
        attempt: int = 1,
        now: object = None,
        **_kwargs: object,
    ) -> object:
        calls.append("confidence")
        return SimpleNamespace(review_queue_record=None, skipped=False)

    def _fake_run_semantic_coherence_stage(
        *,
        store: FileSystemArtifactStore,
        ledger: object,
        merged_transcript_record: object,
        provider: object,
        attempt: int = 1,
        now: object = None,
        **_kwargs: object,
    ) -> object:
        calls.append("semantic")
        return SimpleNamespace(review_queue_record=None, skipped=False)

    monkeypatch.setattr("mena.cli.run_transcription_stage", _fake_run_transcription_stage)
    monkeypatch.setattr("mena.cli.run_diarization_stage", _fake_run_diarization_stage)
    monkeypatch.setattr("mena.cli.run_merge_timing_stage", _fake_run_merge_timing_stage)
    monkeypatch.setattr(
        "mena.cli.run_confidence_flagging_stage", _fake_run_confidence_flagging_stage
    )
    monkeypatch.setattr("mena.cli.run_semantic_coherence_stage", _fake_run_semantic_coherence_stage)

    exit_code = _run_processing_command(
        session_path=session.record_path,
        run_id="manual-run-full",
        attempt=1,
        now=datetime(2026, 5, 8, 6, 0, 0, tzinfo=UTC),
        transcription_provider_factory=lambda: SimpleNamespace(
            name="fake", transcribe=lambda _: None
        ),
        diarization_provider_factory=lambda: SimpleNamespace(
            name="fake", config_id="cfg", diarize=lambda _: None
        ),
        semantic_coherence_provider_factory=lambda: SimpleNamespace(
            name="fake",
            metadata=SimpleNamespace(config_id="cfg", to_dict=lambda: {"config_id": "cfg"}),
            check=lambda *_args, **_kwargs: True,
        ),
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Processing completed for run 'manual-run-full' (attempt 1)." in captured.out
    assert "transcription:local_mic:en" in calls
    assert "transcription:system_audio:en" in calls
    assert calls.count("diarization") == 2
    assert "merge" in calls
    assert "confidence" in calls
    assert "semantic" in calls


def test_run_processing_command_skips_semantic_stage_when_provider_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    session = _build_processing_session(tmp_path)

    def _fake_run_transcription_stage(
        *,
        store: FileSystemArtifactStore,
        source_id: str | None = None,
        **_kwargs: object,
    ) -> object:
        transcript_record = store.store(
            artifact_type="transcript",
            name=f"transcript-{source_id}.json",
            data=b"{}",
            version=1,
            metadata={},
        )
        return SimpleNamespace(transcript_record=transcript_record)

    def _fake_run_diarization_stage(**_kwargs: object) -> object:
        return SimpleNamespace(artifacts=(), artifact_records=(), skipped=True)

    def _fake_run_merge_timing_stage(
        *,
        store: FileSystemArtifactStore,
        **_kwargs: object,
    ) -> object:
        merged_record = store.store(
            artifact_type="merged_transcript",
            name="merged.json",
            data=b"{}",
            version=1,
            metadata={
                "policy_config_id": "cfg",
                "local_source_id": "local_mic",
                "remote_source_id": "system_audio",
                "remote_diarization_status": "skipped",
                "remote_diarization_reason": "provider unavailable",
            },
        )
        return SimpleNamespace(merged_transcript_record=merged_record)

    def _fake_run_confidence_flagging_stage(**_kwargs: object) -> object:
        return SimpleNamespace(review_queue_record=None, skipped=False)

    def _fake_run_semantic_coherence_stage(
        *,
        ledger: ProcessingStageLedger,
        merged_transcript_record: ArtifactRecord,
        attempt: int,
        now: datetime,
        skip_provider_unavailable: bool,
        **_kwargs: object,
    ) -> object:
        assert skip_provider_unavailable is True
        timestamp = now.isoformat(timespec="seconds").replace("+00:00", "Z")
        state = ledger.record(
            ProcessingStageState(
                version=1,
                session_id=ledger.session_id,
                run_id=ledger.run_id,
                stage="semantic_coherence",
                status="skipped",
                input_artifacts=(ArtifactRef.from_record(merged_transcript_record),),
                output_artifacts=(),
                started_at=timestamp,
                completed_at=timestamp,
                error=None,
                attempt=attempt,
            )
        )
        return SimpleNamespace(
            state=state,
            review_queue_record=None,
            skipped=True,
            skip_reason="provider_unavailable",
        )

    monkeypatch.setattr("mena.cli.run_transcription_stage", _fake_run_transcription_stage)
    monkeypatch.setattr("mena.cli.run_diarization_stage", _fake_run_diarization_stage)
    monkeypatch.setattr("mena.cli.run_merge_timing_stage", _fake_run_merge_timing_stage)
    monkeypatch.setattr(
        "mena.cli.run_confidence_flagging_stage", _fake_run_confidence_flagging_stage
    )
    monkeypatch.setattr("mena.cli.run_semantic_coherence_stage", _fake_run_semantic_coherence_stage)

    exit_code = _run_processing_command(
        session_path=session.record_path,
        run_id="semantic-unavailable",
        attempt=1,
        now=datetime(2026, 5, 8, 6, 0, 0, tzinfo=UTC),
        transcription_provider_factory=lambda: SimpleNamespace(
            name="fake", transcribe=lambda _: None
        ),
        diarization_provider_factory=lambda: SimpleNamespace(
            name="fake", config_id="cfg", diarize=lambda _: None
        ),
        semantic_coherence_provider_factory=lambda: SimpleNamespace(
            name="fake",
            metadata=SimpleNamespace(config_id="cfg", to_dict=lambda: {"config_id": "cfg"}),
            check=lambda *_args, **_kwargs: True,
        ),
    )

    store = FileSystemArtifactStore(session_id=session.record.id, session_dir=session.bundle_path)
    ledger = ProcessingStageLedger(
        store=store, session_id=session.record.id, run_id="semantic-unavailable"
    )
    state = ledger.latest("semantic_coherence")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert state is not None
    assert state.status == "skipped"
    assert state.output_artifacts == ()
    assert state.error is None
    assert "Semantic review skipped: local semantic LLM provider is unavailable." in captured.out


def test_main_process_requires_accepted_runtime_profile_state(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    session = _build_processing_session(tmp_path)
    settings = AppSettings.from_env(env={}, home=tmp_path)

    exit_code = main(["process", str(session.record_path)], settings=settings)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Accepted runtime profile state is missing" in captured.out
    assert str(settings.paths.state_home / "setup-state.json") in captured.out


def test_main_process_revalidates_only_derived_runtime_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _build_processing_session(tmp_path)
    settings = AppSettings.from_env(env={}, home=tmp_path)
    ready_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = build_runtime_profile_state(evaluate_setup(get_alpha_local_profile(), ready_state))
    persist_runtime_profile_state(accepted, paths=settings.paths)
    observed: dict[str, object] = {}

    def _detect_processing_state(
        _settings: AppSettings,
        **kwargs: object,
    ) -> LocalInstallationState:
        profile = cast(Any, kwargs["profile"])
        observed["stages"] = tuple(stage.stage for stage in profile.stages)
        observed["commands"] = profile.required_commands
        observed["playback"] = profile.playback_commands
        observed["surfaces"] = tuple(surface.import_name for surface in profile.python_surfaces)
        return replace(
            _setup_state(settings=settings, pyannote_token_present=True),
            commands=(
                CommandCheck("mena", Path("/usr/bin/mena")),
                CommandCheck("ollama", Path("/usr/bin/ollama")),
            ),
            playback_commands=(),
        )

    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state", _detect_processing_state
    )
    processing_calls = _patch_processing_stages(monkeypatch)

    assert main(["process", str(session.record_path)], settings=settings) == 0

    assert set(cast(tuple[str, ...], observed["stages"])) == PROCESSING_REQUIRED_RUNTIME_STAGES
    assert observed["commands"] == ("mena",)
    assert observed["playback"] == ()
    assert observed["surfaces"] == ("faster_whisper", "pyannote.audio")
    assert processing_calls["session_id"] == session.record.id


def test_main_process_does_not_preflight_optional_semantic_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _build_processing_session(tmp_path)
    settings = AppSettings.from_env(env={}, home=tmp_path)
    ready_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = build_runtime_profile_state(evaluate_setup(get_alpha_local_profile(), ready_state))
    persist_runtime_profile_state(accepted, paths=settings.paths)

    def _detect_processing_state(
        _settings: AppSettings,
        **kwargs: object,
    ) -> LocalInstallationState:
        profile = cast(Any, kwargs["profile"])
        assert {stage.stage for stage in profile.stages} == PROCESSING_REQUIRED_RUNTIME_STAGES
        return _setup_state(
            settings=settings,
            ollama_model_present=False,
            pyannote_token_present=True,
        )

    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state", _detect_processing_state
    )
    processing_calls = _patch_processing_stages(monkeypatch)

    assert main(["process", str(session.record_path)], settings=settings) == 0

    assert processing_calls["session_id"] == session.record.id


def test_main_process_prompts_for_ready_one_run_runtime_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    session = _build_processing_session(tmp_path)
    settings = AppSettings.from_env(env={}, home=tmp_path)
    setup_state = _setup_state(settings=settings, pyannote_token_present=True)
    primary_profile = get_alpha_local_profile(settings.local_llm.ollama_model)
    fallback_profile = default_profile_catalog(settings.local_llm.ollama_model)[1]
    accepted = build_runtime_profile_state(
        evaluate_setup(primary_profile, setup_state),
        fallback_evaluation=evaluate_setup(fallback_profile, setup_state),
    )
    persist_runtime_profile_state(accepted, paths=settings.paths)
    fallback_ready_state = _setup_state(
        settings=settings,
        hardware=HardwareFacts(
            system="Linux",
            ram_gb=32.0,
            cuda_available=False,
            vram_gb=None,
            avx2_available=True,
        ),
        pyannote_token_present=True,
    )

    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state",
        lambda *_args, **_kwargs: fallback_ready_state,
    )
    processing_calls = _patch_processing_stages(monkeypatch)
    prompts: list[str] = []

    def _input(prompt: str) -> str:
        prompts.append(prompt)
        return "yes"

    exit_code = main(
        ["process", str(session.record_path)],
        settings=settings,
        input_fn=_input,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert prompts == ["Use local runtime fallback for this run? [y/N] "]
    assert "One-run local runtime fallback is ready:" in captured.out
    assert "Processing completed" in captured.out
    assert "with explicit runtime fallback" in captured.out
    assert processing_calls["session_id"] == session.record.id


def test_runtime_provider_factories_use_selected_profile_state(tmp_path: Path) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    setup_state = _setup_state(settings=settings, pyannote_token_present=True)
    evaluation = evaluate_setup(get_alpha_local_profile(), setup_state)
    runtime_state = build_runtime_profile_state(evaluation)

    transcription_provider = runtime_transcription_factory(
        FasterWhisperTranscriptionProvider,
        runtime_state,
    )()
    diarization_provider = runtime_diarization_factory(
        PyannoteCommunityDiarizationProvider,
        runtime_state,
    )()
    semantic_provider = runtime_semantic_factory(
        OllamaSemanticCoherenceProvider,
        settings=settings,
        runtime_state=runtime_state,
    )()

    assert transcription_provider.profile.id == evaluation.selected_asr_profile.id
    assert transcription_provider.profile.device == "cuda"
    assert transcription_provider.local_files_only
    assert diarization_provider.device == "cuda"
    assert semantic_provider.metadata.model == settings.local_llm.ollama_model
    assert semantic_provider.metadata.endpoint == settings.local_llm.ollama_endpoint


def test_runtime_profile_selection_uses_accepted_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    setup_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = _runtime_profile_state_with_evaluated_fallback(
        settings=settings,
        setup_state=setup_state,
    )
    persist_runtime_profile_state(accepted, paths=settings.paths)
    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state",
        lambda *_args, **_kwargs: setup_state,
    )

    selection = select_runtime_profile(
        settings=settings,
        runner=None,
        command_identity=None,
        hardware=None,
        allow_runtime_fallback=False,
    )

    assert selection.accepted.accepted_profile_id == accepted.accepted_profile_id
    assert selection.actual.accepted_profile_id == accepted.accepted_profile_id
    assert not selection.fallback_used


def test_runtime_profile_selection_revalidates_accepted_model_after_config_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    setup_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted_profile = get_alpha_local_profile(settings.local_llm.ollama_model)
    accepted = build_runtime_profile_state(evaluate_setup(accepted_profile, setup_state))
    persist_runtime_profile_state(accepted, paths=settings.paths)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[ollama]
model = "mistral:7b"
""".strip(),
        encoding="utf-8",
    )
    drifted_settings = AppSettings.from_env(env={}, home=tmp_path, config_path=config_path)
    observed_models: list[str] = []

    def _detect_state(_settings: AppSettings, **kwargs: object) -> LocalInstallationState:
        profile = cast(Any, kwargs["profile"])
        model = next(stage.model for stage in profile.stages if stage.stage == "summarization")
        observed_models.append(model)
        return replace(
            _setup_state(settings=drifted_settings, pyannote_token_present=True),
            ollama=OllamaState(
                endpoint=drifted_settings.local_llm.ollama_endpoint,
                model=model,
                command=CommandCheck("ollama", Path("/usr/bin/ollama")),
                reachable=True,
                model_present=True,
                detail="model present",
            ),
        )

    monkeypatch.setattr("mena.runtime_profile.detect_local_installation_state", _detect_state)

    selection = select_runtime_profile(
        settings=drifted_settings,
        runner=None,
        command_identity=None,
        hardware=None,
        allow_runtime_fallback=False,
    )

    assert observed_models == [settings.local_llm.ollama_model]
    assert selection.actual.accepted_profile_id == accepted.accepted_profile_id


def test_runtime_profile_selection_uses_setup_authorized_local_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    accepted_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = _runtime_profile_state_with_evaluated_fallback(
        settings=settings,
        setup_state=accepted_state,
    )
    persist_runtime_profile_state(accepted, paths=settings.paths)
    stale_state = _setup_state(
        settings=settings,
        hardware=HardwareFacts(
            system="Linux",
            ram_gb=32.0,
            cuda_available=False,
            vram_gb=None,
            avx2_available=True,
        ),
        pyannote_token_present=True,
    )
    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state",
        lambda *_args, **_kwargs: stale_state,
    )

    selection = select_runtime_profile(
        settings=settings,
        runner=None,
        command_identity=None,
        hardware=None,
        allow_runtime_fallback=False,
    )

    assert selection.fallback_used
    assert selection.actual.accepted_profile_id.endswith("cpu-fallback-v1")
    assert selection.fallback_reason is not None


def test_runtime_profile_selection_requires_authorization_when_setup_did_not_authorize_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    accepted_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = build_runtime_profile_state(
        evaluate_setup(get_alpha_local_profile(), accepted_state)
    )
    payload = accepted.to_dict()
    cast(dict[str, object], payload["fallback_authorization"])["local_status"] = (
        "configured_unauthorized"
    )
    cast(dict[str, object], payload["fallback_authorization"])[
        "automatic_local_fallback_authorized"
    ] = False
    settings.paths.state_home.mkdir(parents=True)
    (settings.paths.state_home / "setup-state.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    stale_state = _setup_state(
        settings=settings,
        hardware=HardwareFacts(
            system="Linux",
            ram_gb=32.0,
            cuda_available=False,
            vram_gb=None,
            avx2_available=True,
        ),
        pyannote_token_present=True,
    )
    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state",
        lambda *_args, **_kwargs: stale_state,
    )

    with pytest.raises(ValueError, match="authorize local fallback"):
        select_runtime_profile(
            settings=settings,
            runner=None,
            command_identity=None,
            hardware=None,
            allow_runtime_fallback=False,
        )

    observed_consent: dict[str, object] = {}

    def _authorize_once(failure: str, evaluation: object) -> bool:
        observed_consent["failure"] = failure
        observed_consent["profile_id"] = cast(Any, evaluation).profile.id
        return True

    callback_selection = select_runtime_profile(
        settings=settings,
        runner=None,
        command_identity=None,
        hardware=None,
        allow_runtime_fallback=False,
        authorize_runtime_fallback=_authorize_once,
    )

    selection = select_runtime_profile(
        settings=settings,
        runner=None,
        command_identity=None,
        hardware=None,
        allow_runtime_fallback=True,
    )

    assert callback_selection.fallback_used
    assert callback_selection.actual.accepted_profile_id.endswith("cpu-fallback-v1")
    assert "current ASR runtime selection" in cast(str, observed_consent["failure"])
    assert observed_consent["profile_id"] == "linux-alpha-cpu-fallback-v1"
    assert selection.fallback_used
    assert selection.actual.accepted_profile_id.endswith("cpu-fallback-v1")


def test_runtime_profile_stage_failure_selection_allows_explicit_one_run_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    setup_state = _setup_state(settings=settings, pyannote_token_present=True)
    primary_profile, fallback_profile = default_profile_catalog(settings.local_llm.ollama_model)
    accepted = build_runtime_profile_state(
        evaluate_setup(primary_profile, setup_state),
        fallback_evaluation=evaluate_setup(fallback_profile, setup_state),
        authorize_local_fallback=False,
    )
    observed_profiles: list[tuple[str, tuple[str, ...]]] = []

    def _detect_state(_settings: AppSettings, **kwargs: object) -> LocalInstallationState:
        profile = cast(Any, kwargs["profile"])
        observed_profiles.append((profile.id, tuple(stage.stage for stage in profile.stages)))
        return setup_state

    monkeypatch.setattr("mena.runtime_profile.detect_local_installation_state", _detect_state)

    selection = select_runtime_profile_after_stage_failure(
        settings=settings,
        runner=None,
        command_identity=None,
        hardware=None,
        accepted=accepted,
        failed_stage="diarization",
        failure_reason="CUDA out of memory during pyannote diarization",
        failure_class="DiarizationProviderUnavailable",
        failed_input_artifacts=("session:artifacts/source_audio/system.wav",),
        allow_runtime_fallback=True,
    )

    assert observed_profiles == [("linux-alpha-cpu-fallback-v1", ("diarization",))]
    assert selection.fallback_used
    assert selection.fallback_status == "used_after_stage_failure"
    assert selection.fallback_authorization_source == "explicit_one_run"
    assert selection.fallback_failed_stage == "diarization"
    assert selection.fallback_failed_input_artifacts == (
        "session:artifacts/source_audio/system.wav",
    )
    assert selection.actual.accepted_profile_id == "linux-alpha-cpu-fallback-v1"


def test_runtime_profile_stage_failure_selection_rejects_unready_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    setup_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = _runtime_profile_state_with_evaluated_fallback(
        settings=settings,
        setup_state=setup_state,
    )
    missing_asr_state = _setup_state(
        settings=settings,
        asr_model_present=False,
        pyannote_token_present=True,
    )

    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state",
        lambda *_args, **_kwargs: missing_asr_state,
    )

    with pytest.raises(ValueError, match="fallback is not currently viable"):
        select_runtime_profile_after_stage_failure(
            settings=settings,
            runner=None,
            command_identity=None,
            hardware=None,
            accepted=accepted,
            failed_stage="transcription",
            failure_reason="CUDA out of memory during faster-whisper transcription",
            failure_class="TranscriptionProviderUnavailable",
            failed_input_artifacts=("session:artifacts/source_audio/local.wav",),
            allow_runtime_fallback=False,
        )


def _stage_failure_accepted_state(
    *,
    settings: AppSettings,
    setup_state: LocalInstallationState,
    authorize_local_fallback: bool,
) -> RuntimeProfileState:
    primary_profile, fallback_profile = default_profile_catalog(settings.local_llm.ollama_model)
    return build_runtime_profile_state(
        evaluate_setup(primary_profile, setup_state),
        fallback_evaluation=evaluate_setup(fallback_profile, setup_state),
        authorize_local_fallback=authorize_local_fallback,
    )


def test_runtime_profile_stage_failure_selection_uses_setup_authorized_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    setup_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = _stage_failure_accepted_state(
        settings=settings, setup_state=setup_state, authorize_local_fallback=True
    )
    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state",
        lambda *_args, **_kwargs: setup_state,
    )

    selection = select_runtime_profile_after_stage_failure(
        settings=settings,
        runner=None,
        command_identity=None,
        hardware=None,
        accepted=accepted,
        failed_stage="diarization",
        failure_reason="CUDA out of memory during pyannote diarization",
        failure_class="DiarizationProviderUnavailable",
        failed_input_artifacts=(),
        allow_runtime_fallback=False,
    )

    assert selection.fallback_authorization_source == "setup_authorized"
    assert selection.actual.accepted_profile_id == "linux-alpha-cpu-fallback-v1"


def test_runtime_profile_stage_failure_selection_accepts_interactive_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    setup_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = _stage_failure_accepted_state(
        settings=settings, setup_state=setup_state, authorize_local_fallback=False
    )
    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state",
        lambda *_args, **_kwargs: setup_state,
    )

    selection = select_runtime_profile_after_stage_failure(
        settings=settings,
        runner=None,
        command_identity=None,
        hardware=None,
        accepted=accepted,
        failed_stage="diarization",
        failure_reason="CUDA out of memory during pyannote diarization",
        failure_class="DiarizationProviderUnavailable",
        failed_input_artifacts=(),
        allow_runtime_fallback=False,
        authorize_runtime_fallback=lambda *_args: True,
    )

    assert selection.fallback_authorization_source == "interactive"


def test_runtime_profile_stage_failure_selection_raises_when_fallback_declined(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    setup_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = _stage_failure_accepted_state(
        settings=settings, setup_state=setup_state, authorize_local_fallback=False
    )
    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state",
        lambda *_args, **_kwargs: setup_state,
    )

    with pytest.raises(RuntimeFallbackUnavailable, match="fallback was declined"):
        select_runtime_profile_after_stage_failure(
            settings=settings,
            runner=None,
            command_identity=None,
            hardware=None,
            accepted=accepted,
            failed_stage="diarization",
            failure_reason="CUDA out of memory during pyannote diarization",
            failure_class="DiarizationProviderUnavailable",
            failed_input_artifacts=(),
            allow_runtime_fallback=False,
            authorize_runtime_fallback=lambda *_args: False,
        )


def test_runtime_profile_stage_failure_selection_raises_when_authorizer_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    setup_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = _stage_failure_accepted_state(
        settings=settings, setup_state=setup_state, authorize_local_fallback=False
    )
    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state",
        lambda *_args, **_kwargs: setup_state,
    )

    with pytest.raises(RuntimeFallbackUnavailable, match="Re-run setup"):
        select_runtime_profile_after_stage_failure(
            settings=settings,
            runner=None,
            command_identity=None,
            hardware=None,
            accepted=accepted,
            failed_stage="diarization",
            failure_reason="CUDA out of memory during pyannote diarization",
            failure_class="DiarizationProviderUnavailable",
            failed_input_artifacts=(),
            allow_runtime_fallback=False,
        )


def test_runtime_profile_selection_revalidates_fallback_against_fallback_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    accepted_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = build_runtime_profile_state(
        evaluate_setup(get_alpha_local_profile(), accepted_state)
    )
    persist_runtime_profile_state(accepted, paths=settings.paths)
    observed_profiles: list[str] = []

    def _detect_state(_settings: AppSettings, **kwargs: object) -> LocalInstallationState:
        profile = cast(Any, kwargs["profile"])
        observed_profiles.append(profile.id)
        return _setup_state(
            settings=settings,
            hardware=HardwareFacts(
                system="Linux",
                ram_gb=32.0,
                cuda_available=False,
                vram_gb=None,
                avx2_available=True,
            ),
            pyannote_token_present=True,
        )

    monkeypatch.setattr("mena.runtime_profile.detect_local_installation_state", _detect_state)

    selection = select_runtime_profile(
        settings=settings,
        runner=None,
        command_identity=None,
        hardware=None,
        allow_runtime_fallback=True,
    )

    assert observed_profiles == ["linux-alpha-local-v1", "linux-alpha-cpu-fallback-v1"]
    assert selection.fallback_used
    assert selection.actual.selected_asr_profile.device == "cpu"


def test_runtime_profile_selection_rejects_stale_catalog_state(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    setup_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = _runtime_profile_state_with_evaluated_fallback(
        settings=settings,
        setup_state=setup_state,
    )
    payload = accepted.to_dict()
    payload["catalog_version"] = "stale-catalog"
    settings.paths.state_home.mkdir(parents=True)
    (settings.paths.state_home / "setup-state.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="incompatible"):
        select_runtime_profile(
            settings=settings,
            runner=None,
            command_identity=None,
            hardware=None,
            allow_runtime_fallback=False,
        )


def test_runtime_profile_provenance_records_accepted_actual_and_fallback(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    setup_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = _runtime_profile_state_with_evaluated_fallback(
        settings=settings,
        setup_state=setup_state,
    )
    selection = SimpleNamespace(
        accepted=accepted,
        actual=accepted,
        current_command_identity=setup_state.command_identity,
        fallback_used=False,
        fallback_reason=None,
        fallback_status="not_needed",
        revalidation=evaluate_setup(get_alpha_local_profile(), setup_state),
    )
    session = _build_processing_session(tmp_path)
    store = FileSystemArtifactStore(session_id=session.record.id, session_dir=session.bundle_path)

    record = store_runtime_profile_provenance(
        store=store,
        run_id="run-001",
        attempt=3,
        selection=cast(Any, selection),
    )
    payload = json.loads(store.retrieve(record.id))

    assert payload["attempt"] == 3
    assert payload["accepted_profile"]["id"] == accepted.accepted_profile_id
    assert payload["actual_profile"]["id"] == accepted.accepted_profile_id
    assert payload["actual_profile"]["stages"][0]["device"] == "cuda"
    assert payload["fallback"]["used"] is False
    assert payload["fallback"]["status"] == "not_needed"
    assert payload["fallback"]["authorization"]["local_status"] == "authorized"
    assert payload["fallback"]["authorization"]["local_profile_id"].endswith("cpu-fallback-v1")
    assert payload["contract"] == "runtime-profile-provenance.v1"
    assert payload["current_product"]["version"] == setup_state.command_identity.version
    assert record.metadata["attempt"] == 3
    assert record.metadata["actual_profile_id"] == accepted.accepted_profile_id


def test_main_process_executes_full_pipeline_with_stubbed_stages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    session = _build_processing_session(tmp_path)
    processing_calls = _patch_processing_stages(monkeypatch)

    exit_code = main(
        ["process", str(session.record_path)],
        run_id="manual-run-001",
        transcription_provider_factory=FakeTranscriptionProvider,
        diarization_provider_factory=FakeDiarizationProvider,
        semantic_coherence_provider_factory=lambda: FakeSemanticCoherenceProvider(results={}),
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert processing_calls["session_id"] == session.record.id
    assert processing_calls["run_id"] == "manual-run-001"
    assert processing_calls["attempt"] == 1
    assert "Processing completed for run 'manual-run-001' (attempt 1)." in captured.out


def test_main_process_stores_runtime_profile_provenance_before_processing_stages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _build_processing_session(tmp_path)
    settings = AppSettings.from_env(env={}, home=tmp_path)
    _persist_ready_runtime_profile_state(settings, monkeypatch)

    def _fail_processing_stages(**_kwargs: object) -> None:
        raise RuntimeError("stage should see existing runtime provenance")

    monkeypatch.setattr("mena.cli._run_processing_stages", _fail_processing_stages)

    with pytest.raises(RuntimeError, match="stage should see existing runtime provenance"):
        main(["process", str(session.record_path)], settings=settings, run_id="run-before-stage")

    store = FileSystemArtifactStore(session_id=session.record.id, session_dir=session.bundle_path)
    provenance_records = store.list(artifact_type="runtime_profile_provenance")
    assert len(provenance_records) == 1
    provenance = json.loads(store.retrieve(provenance_records[0].id))
    assert provenance["run_id"] == "run-before-stage"
    assert provenance["attempt"] == 1


def test_main_process_retries_authorized_fallback_after_primary_diarization_oom(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    session = _build_processing_session(tmp_path)
    settings = AppSettings.from_env(env={}, home=tmp_path)
    _persist_ready_runtime_profile_state(settings, monkeypatch)
    transcription_calls: list[tuple[str, str | None]] = []
    diarization_calls: list[tuple[str, str | None]] = []

    class RuntimeTaggedTranscriptionProvider:
        name = "runtime-tagged-transcription"

        def __init__(self, profile_id: str) -> None:
            self.profile_id = profile_id
            self._provider = FakeTranscriptionProvider()

        def transcribe(self, request: TranscriptionRequest) -> TranscriptArtifact:
            transcription_calls.append((self.profile_id, request.source_id))
            return self._provider.transcribe(request)

    class RuntimeTaggedDiarizationProvider:
        name = "runtime-tagged-diarization"

        def __init__(self, profile_id: str) -> None:
            self.profile_id = profile_id
            self._provider = FakeDiarizationProvider(checkpoint=profile_id)

        @property
        def config_id(self) -> str:
            return self._provider.config_id

        def diarize(self, request: object) -> object:
            source_id = getattr(request, "source_id", None)
            diarization_calls.append((self.profile_id, source_id))
            if self.profile_id == "linux-alpha-local-v1":
                raise DiarizationProviderUnavailable(
                    "CUDA out of memory during pyannote diarization"
                )
            return self._provider.diarize(cast(Any, request))

    def _runtime_transcription_factory(
        _factory: object,
        runtime_state: RuntimeProfileState,
    ) -> object:
        return lambda: RuntimeTaggedTranscriptionProvider(runtime_state.accepted_profile_id)

    def _runtime_diarization_factory(
        _factory: object,
        runtime_state: RuntimeProfileState,
    ) -> object:
        return lambda: RuntimeTaggedDiarizationProvider(runtime_state.accepted_profile_id)

    def _fake_run_merge_timing_stage(
        *,
        store: FileSystemArtifactStore,
        remote_regular_diarization_record: ArtifactRecord | None,
        remote_exclusive_diarization_record: ArtifactRecord | None,
        **_kwargs: object,
    ) -> object:
        assert remote_regular_diarization_record is not None
        assert remote_exclusive_diarization_record is not None
        merged_record = store.store(
            artifact_type="merged_transcript",
            name="merged/fallback-merged.json",
            data=b"{}",
            version=1,
            metadata={
                "policy_config_id": "cfg",
                "local_source_id": "local_mic",
                "remote_source_id": "system_audio",
                "remote_diarization_status": "used",
                "remote_diarization_reason": None,
            },
        )
        return SimpleNamespace(merged_transcript_record=merged_record)

    def _fake_run_confidence_flagging_stage(**_kwargs: object) -> object:
        return SimpleNamespace(review_queue_record=None, skipped=False)

    def _fake_run_semantic_coherence_stage(**_kwargs: object) -> object:
        return SimpleNamespace(review_queue_record=None, skipped=False, skip_reason=None)

    monkeypatch.setattr("mena.cli.runtime_transcription_factory", _runtime_transcription_factory)
    monkeypatch.setattr("mena.cli.runtime_diarization_factory", _runtime_diarization_factory)
    monkeypatch.setattr("mena.cli.run_merge_timing_stage", _fake_run_merge_timing_stage)
    monkeypatch.setattr(
        "mena.cli.run_confidence_flagging_stage", _fake_run_confidence_flagging_stage
    )
    monkeypatch.setattr("mena.cli.run_semantic_coherence_stage", _fake_run_semantic_coherence_stage)

    exit_code = main(
        ["process", str(session.record_path)],
        settings=settings,
        run_id="process-diarization-fallback",
        transcription_provider_factory=FasterWhisperTranscriptionProvider,
        diarization_provider_factory=PyannoteCommunityDiarizationProvider,
        # Use the real Ollama semantic provider type so the fallback path exercises
        # runtime_semantic_factory(): the failure-scoped fallback state omits the
        # semantic-review stage, so a rebuild here would raise. The provider must be
        # reused from the accepted profile instead.
        semantic_coherence_provider_factory=OllamaSemanticCoherenceProvider,
    )
    captured = capsys.readouterr()

    store = FileSystemArtifactStore(session_id=session.record.id, session_dir=session.bundle_path)
    provenance_records = store.list(artifact_type="runtime_profile_provenance")
    fallback_provenance = [
        json.loads(store.retrieve(record.id))
        for record in provenance_records
        if json.loads(store.retrieve(record.id))["fallback"]["used"]
    ]

    assert exit_code == 0
    assert transcription_calls == [
        ("linux-alpha-local-v1", "local_mic"),
        ("linux-alpha-local-v1", "system_audio"),
    ]
    assert diarization_calls == [
        ("linux-alpha-local-v1", "system_audio"),
        ("linux-alpha-cpu-fallback-v1", "system_audio"),
    ]
    assert len(provenance_records) == 2
    assert fallback_provenance[0]["fallback"]["status"] == "used_after_stage_failure"
    assert fallback_provenance[0]["fallback"]["failed_stage"] == "diarization"
    assert fallback_provenance[0]["actual_profile"]["id"] == "linux-alpha-cpu-fallback-v1"
    assert (
        "Processing completed for run 'process-diarization-fallback' (attempt 1)"
        " with explicit runtime fallback."
    ) in captured.out


def test_runtime_fallback_stage_for_provider_failure_maps_exceptions() -> None:
    assert (
        _runtime_fallback_stage_for_provider_failure(
            TranscriptionProviderUnavailable("faster-whisper CUDA resource failure")
        )
        == TRANSCRIPTION_STAGE
    )
    assert (
        _runtime_fallback_stage_for_provider_failure(
            DiarizationProviderUnavailable("pyannote CUDA resource failure")
        )
        == DIARIZATION_STAGE
    )


def test_latest_failed_stage_input_artifacts_returns_empty_without_failed_state(
    tmp_path: Path,
) -> None:
    store = FileSystemArtifactStore(session_id="session-x", session_dir=tmp_path)
    ledger = ProcessingStageLedger(store=store, session_id="session-x", run_id="run-x")
    assert _latest_failed_stage_input_artifacts(ledger=ledger, stage="transcription") == ()


def test_main_process_retries_authorized_fallback_after_primary_transcription_oom(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    session = _build_processing_session(tmp_path)
    settings = AppSettings.from_env(env={}, home=tmp_path)
    _persist_ready_runtime_profile_state(settings, monkeypatch)
    transcription_calls: list[tuple[str, str | None]] = []
    diarization_calls: list[tuple[str, str | None]] = []

    class RuntimeTaggedTranscriptionProvider:
        name = "runtime-tagged-transcription"

        def __init__(self, profile_id: str) -> None:
            self.profile_id = profile_id
            self._provider = FakeTranscriptionProvider()

        def transcribe(self, request: TranscriptionRequest) -> TranscriptArtifact:
            transcription_calls.append((self.profile_id, request.source_id))
            if self.profile_id == "linux-alpha-local-v1":
                raise TranscriptionProviderUnavailable(
                    "CUDA out of memory during faster-whisper transcription"
                )
            return self._provider.transcribe(request)

    class RuntimeTaggedDiarizationProvider:
        name = "runtime-tagged-diarization"

        def __init__(self, profile_id: str) -> None:
            self.profile_id = profile_id
            self._provider = FakeDiarizationProvider(checkpoint=profile_id)

        @property
        def config_id(self) -> str:
            return self._provider.config_id

        def diarize(self, request: object) -> object:
            source_id = getattr(request, "source_id", None)
            diarization_calls.append((self.profile_id, source_id))
            return self._provider.diarize(cast(Any, request))

    def _runtime_transcription_factory(
        _factory: object,
        runtime_state: RuntimeProfileState,
    ) -> object:
        return lambda: RuntimeTaggedTranscriptionProvider(runtime_state.accepted_profile_id)

    def _runtime_diarization_factory(
        _factory: object,
        runtime_state: RuntimeProfileState,
    ) -> object:
        return lambda: RuntimeTaggedDiarizationProvider(runtime_state.accepted_profile_id)

    def _fake_run_merge_timing_stage(
        *,
        store: FileSystemArtifactStore,
        remote_regular_diarization_record: ArtifactRecord | None,
        remote_exclusive_diarization_record: ArtifactRecord | None,
        **_kwargs: object,
    ) -> object:
        merged_record = store.store(
            artifact_type="merged_transcript",
            name="merged/fallback-merged.json",
            data=b"{}",
            version=1,
            metadata={
                "policy_config_id": "cfg",
                "local_source_id": "local_mic",
                "remote_source_id": "system_audio",
                "remote_diarization_status": "used",
                "remote_diarization_reason": None,
            },
        )
        return SimpleNamespace(merged_transcript_record=merged_record)

    def _fake_run_confidence_flagging_stage(**_kwargs: object) -> object:
        return SimpleNamespace(review_queue_record=None, skipped=False)

    def _fake_run_semantic_coherence_stage(**_kwargs: object) -> object:
        return SimpleNamespace(review_queue_record=None, skipped=False, skip_reason=None)

    monkeypatch.setattr("mena.cli.runtime_transcription_factory", _runtime_transcription_factory)
    monkeypatch.setattr("mena.cli.runtime_diarization_factory", _runtime_diarization_factory)
    monkeypatch.setattr("mena.cli.run_merge_timing_stage", _fake_run_merge_timing_stage)
    monkeypatch.setattr(
        "mena.cli.run_confidence_flagging_stage", _fake_run_confidence_flagging_stage
    )
    monkeypatch.setattr("mena.cli.run_semantic_coherence_stage", _fake_run_semantic_coherence_stage)

    exit_code = main(
        ["process", str(session.record_path)],
        settings=settings,
        run_id="process-transcription-fallback",
        transcription_provider_factory=FasterWhisperTranscriptionProvider,
        diarization_provider_factory=PyannoteCommunityDiarizationProvider,
        semantic_coherence_provider_factory=OllamaSemanticCoherenceProvider,
    )
    captured = capsys.readouterr()

    store = FileSystemArtifactStore(session_id=session.record.id, session_dir=session.bundle_path)
    fallback_provenance = [
        json.loads(store.retrieve(record.id))
        for record in store.list(artifact_type="runtime_profile_provenance")
        if json.loads(store.retrieve(record.id))["fallback"]["used"]
    ]

    assert exit_code == 0
    # Primary transcription OOMs on the first source, then the whole pipeline
    # reruns on the CPU fallback for both transcription and diarization.
    assert transcription_calls == [
        ("linux-alpha-local-v1", "local_mic"),
        ("linux-alpha-cpu-fallback-v1", "local_mic"),
        ("linux-alpha-cpu-fallback-v1", "system_audio"),
    ]
    # Diarization never ran on the primary profile (transcription failed first) and
    # is configured from the fallback profile -- not a hardcoded "cpu" default.
    assert diarization_calls == [("linux-alpha-cpu-fallback-v1", "system_audio")]
    assert fallback_provenance[0]["fallback"]["failed_stage"] == "transcription"
    assert fallback_provenance[0]["actual_profile"]["id"] == "linux-alpha-cpu-fallback-v1"
    assert (
        "Processing completed for run 'process-transcription-fallback' (attempt 1)"
        " with explicit runtime fallback."
    ) in captured.out


def test_resolve_runtime_fallback_input_guards_non_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explicit(_prompt: str) -> str:
        return "y"

    # An explicit input function is always honored verbatim.
    assert _resolve_runtime_fallback_input(explicit) is explicit
    # On a real TTY, the builtin input prompt is used.
    monkeypatch.setattr("mena.cli.sys.stdin", SimpleNamespace(isatty=lambda: True))
    assert _resolve_runtime_fallback_input(None) is input
    # On a non-TTY stdin, no prompt is wired so the fallback raises an actionable
    # error instead of input() raising EOFError.
    monkeypatch.setattr("mena.cli.sys.stdin", SimpleNamespace(isatty=lambda: False))
    assert _resolve_runtime_fallback_input(None) is None


def test_main_process_maps_unauthorized_runtime_fallback_to_exit_3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    session = _build_processing_session(tmp_path)
    settings = AppSettings.from_env(env={}, home=tmp_path)
    _persist_ready_runtime_profile_state(settings, monkeypatch)

    def _raise_unavailable(**_kwargs: object) -> int:
        raise RuntimeFallbackUnavailable(
            "Runtime stage 'diarization' failed: CUDA out of memory. Re-run setup to "
            "authorize local fallback or pass --allow-runtime-fallback for an explicit "
            "one-run fallback when supported."
        )

    monkeypatch.setattr("mena.cli._run_processing_command", _raise_unavailable)

    exit_code = main(["process", str(session.record_path)], settings=settings)
    captured = capsys.readouterr()

    assert exit_code == 3
    assert "Cannot run processing workflow" in captured.out
    assert "--allow-runtime-fallback" in captured.out


def test_main_process_records_terminal_runtime_stewardship_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    session = _build_processing_session(tmp_path)
    settings = AppSettings.from_env(env={}, home=tmp_path)
    _persist_ready_runtime_profile_state(settings, monkeypatch)

    def _fake_unload_ollama_model(**_kwargs: object) -> str:
        return "unloaded"

    def _fake_run_processing_stages(
        *,
        semantic_coherence_provider: object,
        **_kwargs: object,
    ) -> object:
        cast(Any, semantic_coherence_provider)._runtime_resource_used = True
        return SimpleNamespace(
            semantic_coherence_result=SimpleNamespace(skipped=False, skip_reason=None)
        )

    monkeypatch.setattr("mena.semantic_coherence.unload_ollama_model", _fake_unload_ollama_model)
    monkeypatch.setattr("mena.cli._run_processing_stages", _fake_run_processing_stages)

    exit_code = main(
        ["process", str(session.record_path)],
        settings=settings,
        run_id="process-release",
    )
    captured = capsys.readouterr()

    store = FileSystemArtifactStore(session_id=session.record.id, session_dir=session.bundle_path)
    stewardship_records = store.list(artifact_type="runtime_stewardship_provenance")
    assert exit_code == 0
    assert "Runtime stewardship: Ollama model release completed: unloaded." in captured.out
    assert len(stewardship_records) == 1
    payload = json.loads(store.retrieve(stewardship_records[0].id))
    assert payload["run_id"] == "process-release"
    assert payload["boundary"] == "process"
    assert payload["releases"][0]["status"] == "released"
    assert payload["releases"][0]["lease"]["stage"] == "semantic-review"
    assert payload["releases"][0]["lease"]["provider"] == "ollama"


def test_terminal_runtime_stewardship_records_unsupported_release_after_resource_use(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    store = FileSystemArtifactStore(session_id="session-1", session_dir=tmp_path)
    provider = SimpleNamespace(
        metadata=SimpleNamespace(name="fake-llm", model="model-a"),
        runtime_resource_used=True,
    )

    result = _record_terminal_local_llm_release(
        store=store,
        run_id="unsupported-release",
        attempt=1,
        provider=provider,
        stage="semantic-review",
        boundary="process",
    )
    captured = capsys.readouterr()

    stewardship_records = store.list(artifact_type="runtime_stewardship_provenance")
    assert result is not None
    assert result.status == "not_supported"
    assert "Runtime stewardship warning: Provider does not expose" in captured.out
    assert len(stewardship_records) == 1
    payload = json.loads(store.retrieve(stewardship_records[0].id))
    assert payload["releases"][0]["status"] == "not_supported"


def test_terminal_runtime_stewardship_skips_provider_without_resource_use_signal(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    store = FileSystemArtifactStore(session_id="session-1", session_dir=tmp_path)
    provider = SimpleNamespace(metadata=SimpleNamespace(name="fake", model="none"))

    result = _record_terminal_local_llm_release(
        store=store,
        run_id="custom-semantic-provider",
        attempt=1,
        provider=provider,
        stage="semantic-review",
        boundary="process",
    )
    captured = capsys.readouterr()

    assert result is None
    assert captured.out == ""
    assert store.list(artifact_type="runtime_stewardship_provenance") == ()


def test_main_resume_stores_runtime_profile_provenance_per_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _build_processing_session(tmp_path)
    store = FileSystemArtifactStore(session_id=session.record.id, session_dir=session.bundle_path)
    ledger = ProcessingStageLedger(session_id=session.record.id, run_id="run-failed", store=store)
    failed_state = ledger.mark_failed(
        stage="transcription",
        input_artifacts=(),
        error=ProcessingErrorInfo(
            message="Transient inference failure",
            retryable=True,
            code="model_timeout",
        ),
        attempt=4,
    )
    settings = AppSettings.from_env(env={}, home=tmp_path)
    setup_state = _persist_ready_runtime_profile_state(settings, monkeypatch)
    accepted = build_runtime_profile_state(evaluate_setup(get_alpha_local_profile(), setup_state))
    selection = SimpleNamespace(
        accepted=accepted,
        actual=accepted,
        current_command_identity=setup_state.command_identity,
        fallback_used=False,
        fallback_reason=None,
        fallback_status="not_needed",
        revalidation=evaluate_setup(get_alpha_local_profile(), setup_state),
    )
    store_runtime_profile_provenance(
        store=store,
        run_id=failed_state.run_id,
        attempt=failed_state.attempt,
        selection=cast(Any, selection),
    )
    processing_calls = _patch_processing_stages(monkeypatch)

    assert main(["resume", str(session.record_path)], settings=settings) == 0

    assert processing_calls["attempt"] == failed_state.attempt + 1
    provenance_records = store.list(artifact_type="runtime_profile_provenance")
    attempts = {json.loads(store.retrieve(record.id))["attempt"] for record in provenance_records}
    assert attempts == {failed_state.attempt, failed_state.attempt + 1}


def test_main_process_reports_provider_validation_errors(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    session = _build_processing_session(tmp_path)

    exit_code = main(
        ["process", str(session.record_path)],
        transcription_provider_factory=lambda: SimpleNamespace(
            name="bad",
            transcribe="not-callable",
        ),
        diarization_provider_factory=FakeDiarizationProvider,
        semantic_coherence_provider_factory=lambda: FakeSemanticCoherenceProvider(results={}),
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert (
        "Cannot run processing workflow: transcription provider must implement "
        "TranscriptionProvider."
    ) in captured.out


def test_main_process_reports_provider_factory_type_errors(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    session = _build_processing_session(tmp_path)

    def broken_factory() -> object:
        raise TypeError("missing constructor argument")

    exit_code = main(
        ["process", str(session.record_path)],
        transcription_provider_factory=broken_factory,
        diarization_provider_factory=FakeDiarizationProvider,
        semantic_coherence_provider_factory=lambda: FakeSemanticCoherenceProvider(results={}),
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert (
        "Cannot run processing workflow: transcription provider must implement "
        "TranscriptionProvider."
    ) in captured.out


def test_main_resume_retries_latest_retryable_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    session = _build_processing_session(tmp_path)
    store = FileSystemArtifactStore(session_id=session.record.id, session_dir=session.bundle_path)
    ledger = ProcessingStageLedger(session_id=session.record.id, run_id="run-failed", store=store)
    failed_state = ledger.mark_failed(
        stage="transcription",
        input_artifacts=(),
        error=ProcessingErrorInfo(
            message="Transient inference failure",
            retryable=True,
            code="model_timeout",
        ),
        attempt=4,
    )

    processing_calls = _patch_processing_stages(monkeypatch)

    exit_code = main(
        ["resume", str(session.record_path)],
        transcription_provider_factory=FakeTranscriptionProvider,
        diarization_provider_factory=FakeDiarizationProvider,
        semantic_coherence_provider_factory=lambda: FakeSemanticCoherenceProvider(results={}),
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert processing_calls["session_id"] == session.record.id
    assert processing_calls["run_id"] == failed_state.run_id
    assert processing_calls["attempt"] == failed_state.attempt + 1
    assert (
        f"Processing completed for run '{failed_state.run_id}' "
        f"(attempt {failed_state.attempt + 1})." in captured.out
    )


def test_main_resume_retries_interrupted_running_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    session = _build_processing_session(tmp_path)
    store = FileSystemArtifactStore(session_id=session.record.id, session_dir=session.bundle_path)
    ledger = ProcessingStageLedger(session_id=session.record.id, run_id="run-running", store=store)
    running_state = ledger.mark_running(
        stage="transcription",
        input_artifacts=(),
        attempt=7,
    )

    processing_calls = _patch_processing_stages(monkeypatch)

    exit_code = main(
        ["resume", str(session.record_path)],
        transcription_provider_factory=FakeTranscriptionProvider,
        diarization_provider_factory=FakeDiarizationProvider,
        semantic_coherence_provider_factory=lambda: FakeSemanticCoherenceProvider(results={}),
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert processing_calls["session_id"] == session.record.id
    assert processing_calls["run_id"] == running_state.run_id
    assert processing_calls["attempt"] == running_state.attempt + 1
    assert (
        f"Processing completed for run '{running_state.run_id}' "
        f"(attempt {running_state.attempt + 1})." in captured.out
    )


def test_main_resume_without_retryable_failure_reports_no_retry(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    session = create_session(profile=parse_capture_profile("online"), data_root=tmp_path)
    store = FileSystemArtifactStore(session_id=session.record.id, session_dir=session.bundle_path)
    ledger = ProcessingStageLedger(session_id=session.record.id, run_id="run-blocked", store=store)
    ledger.mark_failed(
        stage="transcription",
        input_artifacts=(),
        error=ProcessingErrorInfo(
            message="Non-retryable setup",
            retryable=False,
            code="input",
        ),
        attempt=1,
    )

    exit_code = main(["resume", str(session.record_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert (
        "Latest processing failure is non-retryable at run '"
        "run-blocked', stage 'transcription', attempt 1: "
        "Non-retryable setup (code=input)" in captured.out
    )


def test_main_resume_blocks_non_retryable_failure_after_running_checkpoint(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    session = create_session(profile=parse_capture_profile("online"), data_root=tmp_path)
    store = FileSystemArtifactStore(session_id=session.record.id, session_dir=session.bundle_path)
    ledger = ProcessingStageLedger(session_id=session.record.id, run_id="run-blocked", store=store)
    now = datetime(2026, 5, 8, 8, 30, tzinfo=UTC)
    ledger.mark_running(
        stage="transcription",
        input_artifacts=(),
        attempt=1,
        now=now,
    )
    ledger.mark_failed(
        stage="transcription",
        input_artifacts=(),
        error=ProcessingErrorInfo(
            message="Bad input",
            retryable=False,
            code="input",
        ),
        attempt=1,
        now=now,
    )

    exit_code = main(["resume", str(session.record_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert (
        "Latest processing failure is non-retryable at run '"
        "run-blocked', stage 'transcription', attempt 1: "
        "Bad input (code=input)" in captured.out
    )


def test_main_resume_after_mid_run_completed_checkpoint_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _build_processing_session(tmp_path)
    store = FileSystemArtifactStore(session_id=session.record.id, session_dir=session.bundle_path)
    ledger = ProcessingStageLedger(
        session_id=session.record.id, run_id="run-mid-complete", store=store
    )
    output_record = store.store(
        artifact_type="transcript",
        name="completed-transcript.json",
        data=b"{}",
        metadata={},
    )
    now = datetime(2026, 5, 8, 8, 30, tzinfo=UTC)
    running_state = ledger.mark_running(
        stage="transcription",
        input_artifacts=(),
        attempt=1,
        now=now,
    )
    completed_state = ledger.mark_completed(
        stage="transcription",
        input_artifacts=(),
        output_artifacts=(ArtifactRef.from_record(output_record),),
        attempt=running_state.attempt,
        started_at=running_state.started_at,
        now=now,
    )
    processing_calls = _patch_processing_stages(monkeypatch)

    exit_code = main(
        ["resume", str(session.record_path)],
        transcription_provider_factory=FakeTranscriptionProvider,
        diarization_provider_factory=FakeDiarizationProvider,
        semantic_coherence_provider_factory=lambda: FakeSemanticCoherenceProvider(results={}),
    )

    assert exit_code == 0
    assert processing_calls["run_id"] == completed_state.run_id
    assert processing_calls["attempt"] == completed_state.attempt + 1


def test_main_resume_after_mid_run_skipped_checkpoint_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _build_processing_session(tmp_path)
    store = FileSystemArtifactStore(session_id=session.record.id, session_dir=session.bundle_path)
    ledger = ProcessingStageLedger(session_id=session.record.id, run_id="run-mid-skip", store=store)
    output_record = store.store(
        artifact_type="transcript",
        name="skipped-transcript.json",
        data=b"{}",
        metadata={},
    )
    skipped_state = ledger.record(
        ProcessingStageState(
            version=1,
            session_id=session.record.id,
            run_id="run-mid-skip",
            stage="transcription",
            status="skipped",
            attempt=2,
            input_artifacts=(),
            output_artifacts=(ArtifactRef.from_record(output_record),),
            error=None,
            started_at="2026-05-08T08:30:00Z",
            completed_at="2026-05-08T08:30:00Z",
        )
    )
    processing_calls = _patch_processing_stages(monkeypatch)

    exit_code = main(
        ["resume", str(session.record_path)],
        transcription_provider_factory=FakeTranscriptionProvider,
        diarization_provider_factory=FakeDiarizationProvider,
        semantic_coherence_provider_factory=lambda: FakeSemanticCoherenceProvider(results={}),
    )

    assert exit_code == 0
    assert processing_calls["run_id"] == skipped_state.run_id
    assert processing_calls["attempt"] == skipped_state.attempt + 1


def test_main_resume_after_final_completed_checkpoint_reports_no_retry(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    session = create_session(profile=parse_capture_profile("online"), data_root=tmp_path)
    store = FileSystemArtifactStore(session_id=session.record.id, session_dir=session.bundle_path)
    ledger = ProcessingStageLedger(session_id=session.record.id, run_id="run-complete", store=store)
    output_record = store.store(
        artifact_type="transcript",
        name="completed-transcript.json",
        data=b"{}",
        metadata={},
    )
    now = datetime(2026, 5, 8, 8, 30, tzinfo=UTC)
    running_state = ledger.mark_running(
        stage="semantic_coherence",
        input_artifacts=(),
        attempt=1,
        now=now,
    )
    ledger.mark_completed(
        stage="semantic_coherence",
        input_artifacts=(),
        output_artifacts=(ArtifactRef.from_record(output_record),),
        attempt=running_state.attempt,
        started_at=running_state.started_at,
        now=now,
    )

    exit_code = main(["resume", str(session.record_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "No retryable processing failure found to resume." in captured.out


def test_main_resume_ignores_later_summarization_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    session = create_session(profile=parse_capture_profile("online"), data_root=tmp_path)
    store = FileSystemArtifactStore(session_id=session.record.id, session_dir=session.bundle_path)
    output_record = store.store(
        artifact_type="transcript",
        name="completed-transcript.json",
        data=b"{}",
        metadata={},
    )
    processing_ledger = ProcessingStageLedger(
        session_id=session.record.id,
        run_id="run-complete",
        store=store,
    )
    now = datetime(2026, 5, 8, 8, 30, tzinfo=UTC)
    running_state = processing_ledger.mark_running(
        stage="semantic_coherence",
        input_artifacts=(),
        attempt=1,
        now=now,
    )
    processing_ledger.mark_completed(
        stage="semantic_coherence",
        input_artifacts=(),
        output_artifacts=(ArtifactRef.from_record(output_record),),
        attempt=running_state.attempt,
        started_at=running_state.started_at,
        now=now,
    )
    summarization_ledger = ProcessingStageLedger(
        session_id=session.record.id,
        run_id=f"summarize-{session.record.id}",
        store=store,
    )
    summary_state = summarization_ledger.mark_running(
        stage="summarization",
        input_artifacts=(ArtifactRef.from_record(output_record),),
        attempt=1,
        now=datetime(2026, 5, 8, 8, 45, tzinfo=UTC),
    )
    summarization_ledger.mark_completed(
        stage="summarization",
        input_artifacts=(ArtifactRef.from_record(output_record),),
        output_artifacts=(ArtifactRef.from_record(output_record),),
        attempt=summary_state.attempt,
        started_at=summary_state.started_at,
        now=datetime(2026, 5, 8, 8, 45, tzinfo=UTC),
    )
    processing_calls = _patch_processing_stages(monkeypatch)

    exit_code = main(["resume", str(session.record_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert processing_calls == {}
    assert "No retryable processing failure found to resume." in captured.out


def test_main_resume_uses_durable_checkpoint_order_when_timestamps_tie(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _build_processing_session(tmp_path)
    store = FileSystemArtifactStore(session_id=session.record.id, session_dir=session.bundle_path)
    now = datetime(2026, 5, 8, 8, 30, tzinfo=UTC)
    older = ProcessingStageLedger(session_id=session.record.id, run_id="a-older", store=store)
    older.mark_failed(
        stage="transcription",
        input_artifacts=(),
        error=ProcessingErrorInfo(message="first", retryable=True, code="model"),
        attempt=1,
        now=now,
    )
    newer = ProcessingStageLedger(session_id=session.record.id, run_id="z-newer", store=store)
    newer_state = newer.mark_failed(
        stage="transcription",
        input_artifacts=(),
        error=ProcessingErrorInfo(message="second", retryable=True, code="model"),
        attempt=3,
        now=now,
    )
    processing_calls = _patch_processing_stages(monkeypatch)

    exit_code = main(
        ["resume", str(session.record_path)],
        transcription_provider_factory=FakeTranscriptionProvider,
        diarization_provider_factory=FakeDiarizationProvider,
        semantic_coherence_provider_factory=lambda: FakeSemanticCoherenceProvider(results={}),
    )

    assert exit_code == 0
    assert processing_calls["run_id"] == newer_state.run_id
    assert processing_calls["attempt"] == newer_state.attempt + 1


def test_main_resume_without_checkpoint_reports_no_retry(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    session = create_session(profile=parse_capture_profile("online"), data_root=tmp_path)

    exit_code = main(["resume", str(session.record_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "No retryable processing failure found to resume." in captured.out


class _EventWorker:
    def __init__(self, messages: Sequence[ProtocolMessage]) -> None:
        self._messages = iter(messages)

    def receive(self) -> ProtocolMessage:
        return next(self._messages)


def _segment_payload(
    *,
    source_id: str,
    index: int,
    path: str | None = None,
    sha256: str | None = None,
    byte_count: int = 44,
) -> dict[str, object]:
    segment_sha256 = sha256 or hashlib.sha256(f"{source_id}:{index}".encode()).hexdigest()
    return {
        "source_id": source_id,
        "index": index,
        "path": path or f"artifacts/source/{source_id}/{source_id}_{index:06d}.wav",
        "sample_rate": 48_000,
        "channels": 1,
        "channel_layout": "mono",
        "sample_format": "pcm_s16le",
        "byte_count": byte_count,
        "duration_seconds": 30.0,
        "started_at": None,
        "ended_at": None,
        "sha256": segment_sha256,
        "route_id": None,
        "route_label": None,
        "capture_mode": None,
        "status": "sealed",
        "format": "wav",
    }


def _write_segment_payload(
    session_dir: Path,
    *,
    source_id: str,
    index: int,
    pcm_bytes: bytes,
) -> dict[str, object]:
    relative_path = f"artifacts/source/{source_id}/{source_id}_{index:06d}.wav"
    path = session_dir / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    wav_bytes = make_wav_bytes(pcm_bytes)
    path.write_bytes(wav_bytes)
    return _segment_payload(
        source_id=source_id,
        index=index,
        path=relative_path,
        sha256=hashlib.sha256(wav_bytes).hexdigest(),
        byte_count=len(wav_bytes),
    )


def _monitor_start_payload() -> StartCapturePayload:
    return StartCapturePayload(
        profile="online_dual_stream",
        language="en",
        segment_seconds=30,
        selected_sources=("local_mic", "system_audio"),
        routes=(
            CaptureRouteBindingPayload(
                role="microphone",
                source_id="local_mic",
                route_id="pipewire:node:31",
                label="Studio mic",
                backend="pipewire",
                required=True,
                backend_name="alsa_input.studio",
            ),
            CaptureRouteBindingPayload(
                role="playback_stream",
                source_id="system_audio",
                route_id="pulse:sink-input:51",
                label="Video call",
                backend="pipewire",
                required=True,
                backend_name="meeting-playback",
            ),
            CaptureRouteBindingPayload(
                role="output_sink",
                source_id=None,
                route_id="pipewire:node:42",
                label="Headphones",
                backend="pipewire",
                required=True,
                backend_name="alsa_output.headphones",
            ),
        ),
    )


def _finalized_capture_message(
    *,
    profile: str = "online_dual_stream",
    selected_sources: Sequence[str] = ("local_mic", "system_audio"),
) -> ProtocolMessage:
    return ProtocolMessage(
        type="capture_finalized",
        session_id=SESSION_ID,
        payload={
            "status": "recorded",
            "stop_reason": "user_stop",
            "completed_at": None,
            "error": None,
            "capture_metadata": {
                "version": 1,
                "profile": profile,
                "sample_rate": 48_000,
                "channels": 1,
                "segment_seconds": 30,
                "segment_count": len(selected_sources),
                "sources": [_source_summary(source_id) for source_id in selected_sources],
            },
        },
    )


def _source_summary(source_id: str) -> dict[str, object]:
    if source_id == "system_audio":
        return {
            "source_id": source_id,
            "route_id": "pulse:sink-input:51",
            "route_label": "Video call",
            "capture_mode": "dedicated_route",
            "channel_layout": "mono",
            "segment_count": 1,
        }
    route_id = "pipewire:node:31"
    label = "Studio mic" if source_id == "local_mic" else "Room microphone"
    return {
        "source_id": source_id,
        "route_id": route_id,
        "route_label": label,
        "capture_mode": "selected_microphone",
        "channel_layout": "mono",
        "segment_count": 1,
    }


class FakeWorkerProcess:
    last_start_payload: StartCapturePayload | None = None

    def __init__(self, *, session_id: str, session_dir: Path) -> None:
        self.session_id = session_id
        self.session_dir = session_dir
        self.start_payload: StartCapturePayload | None = None

    def __enter__(self) -> "FakeWorkerProcess":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def handshake(self) -> ProtocolMessage:
        return ProtocolMessage(type="hello_ack", session_id=self.session_id)

    def start_capture(self, payload: StartCapturePayload) -> ProtocolMessage:
        self.start_payload = payload
        self.__class__.last_start_payload = payload
        return ProtocolMessage(
            type="capture_started",
            session_id=self.session_id,
            payload={
                "session_dir": str(self.session_dir),
                "profile": payload.profile,
                "segment_seconds": payload.segment_seconds,
                "selected_sources": list(payload.selected_sources),
            },
        )

    def stop_capture(self) -> ProtocolMessage:
        profile = self.start_payload.profile if self.start_payload else "online_dual_stream"
        selected_sources = (
            self.start_payload.selected_sources
            if self.start_payload is not None
            else ("local_mic", "system_audio")
        )
        return _finalized_capture_message(profile=profile, selected_sources=selected_sources)

    def wait(self, timeout: float | None = None) -> int:
        return 0


class VerboseCapturingWorkerProcess(FakeWorkerProcess):
    last_capture_diagnostics: bool | None = None

    def __init__(
        self,
        *,
        session_id: str,
        session_dir: Path,
        capture_diagnostics: bool = False,
    ) -> None:
        super().__init__(session_id=session_id, session_dir=session_dir)
        self.__class__.last_capture_diagnostics = capture_diagnostics


class PartialWorkerProcess(FakeWorkerProcess):
    def stop_capture(self) -> ProtocolMessage:
        profile = self.start_payload.profile if self.start_payload else "online_dual_stream"
        return ProtocolMessage(
            type="capture_finalized",
            session_id=self.session_id,
            payload={
                "status": "interrupted_partial",
                "stop_reason": "user_stop",
                "completed_at": None,
                "error": "No processable audio segments were sealed.",
                "capture_metadata": {
                    "version": 1,
                    "profile": profile,
                    "sample_rate": 48_000,
                    "channels": 1,
                    "segment_seconds": 30,
                    "segment_count": 0,
                    "segments": [],
                    "sources": [],
                },
            },
        )


class InterruptingStartWorkerProcess(WorkerProcess):
    stop_requested = False
    exited = False

    def __init__(self, *, session_id: str, session_dir: Path) -> None:
        self.session_id = session_id
        self.session_dir = session_dir

    def __enter__(self) -> "InterruptingStartWorkerProcess":
        return self

    def __exit__(self, *_args: object) -> None:
        self.__class__.exited = True

    def handshake(self) -> ProtocolMessage:
        return ProtocolMessage(type="hello_ack", session_id=self.session_id)

    def start_capture(
        self,
        payload: StartCapturePayload | Mapping[str, object],
    ) -> ProtocolMessage:
        raise KeyboardInterrupt

    def request_stop_capture(self) -> None:
        self.__class__.stop_requested = True

    def stop_capture(self) -> ProtocolMessage:
        self.__class__.stop_requested = True
        return _finalized_capture_message()

    def wait(self, timeout: float | None = None) -> int:
        return 0


class MonitoredWorkerProcess(WorkerProcess):
    stop_requested = False

    def __init__(self, *, session_id: str, session_dir: Path) -> None:
        self.session_id = session_id
        self.session_dir = session_dir
        self._messages: queue.Queue[ProtocolMessage] = queue.Queue()
        self.profile = "online_dual_stream"

    def __enter__(self) -> "MonitoredWorkerProcess":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def handshake(self) -> ProtocolMessage:
        return ProtocolMessage(type="hello_ack", session_id=self.session_id)

    def start_capture(
        self,
        payload: StartCapturePayload | Mapping[str, object],
    ) -> ProtocolMessage:
        if isinstance(payload, StartCapturePayload):
            self.profile = payload.profile
        else:
            raw_profile = payload.get("profile")
            if isinstance(raw_profile, str):
                self.profile = raw_profile
        return ProtocolMessage(
            type="capture_started",
            session_id=self.session_id,
            payload={
                "session_dir": str(self.session_dir),
                "profile": self.profile,
                "segment_seconds": 30,
                "selected_sources": ["local_mic", "system_audio"],
            },
        )

    def request_stop_capture(self) -> None:
        self.__class__.stop_requested = True
        self._messages.put(
            ProtocolMessage(
                type="segments_flushed",
                session_id=self.session_id,
                payload={
                    "segments": [
                        _segment_payload(source_id="local_mic", index=1),
                        _segment_payload(source_id="system_audio", index=1),
                    ]
                },
            )
        )
        self._messages.put(
            _finalized_capture_message(
                profile=self.profile,
                selected_sources=("local_mic", "system_audio"),
            )
        )

    def receive(self) -> ProtocolMessage:
        try:
            return self._messages.get(timeout=1)
        except queue.Empty as exc:
            raise AssertionError("monitor waited for an unexpected worker message") from exc

    def wait(self, timeout: float | None = None) -> int:
        return 0


class IdenticalHashWorkerProcess(MonitoredWorkerProcess):
    pcm_bytes = b"\x01\x00" * 320

    def request_stop_capture(self) -> None:
        self._messages.put(
            ProtocolMessage(
                type="segments_flushed",
                session_id=self.session_id,
                payload={
                    "segments": [
                        _write_segment_payload(
                            self.session_dir,
                            source_id="local_mic",
                            index=1,
                            pcm_bytes=self.pcm_bytes,
                        ),
                        _write_segment_payload(
                            self.session_dir,
                            source_id="system_audio",
                            index=1,
                            pcm_bytes=self.pcm_bytes,
                        ),
                    ]
                },
            )
        )
        self._messages.put(
            _finalized_capture_message(
                profile=self.profile,
                selected_sources=("local_mic", "system_audio"),
            )
        )


class SilentIdenticalHashWorkerProcess(IdenticalHashWorkerProcess):
    pcm_bytes = b"\x00\x00" * 320


class FailingWorkerProcess(FakeWorkerProcess):
    def handshake(self) -> ProtocolMessage:
        raise ProtocolError("worker refused handshake")


class NonzeroExitWorkerProcess(FakeWorkerProcess):
    def wait(self, timeout: float | None = None) -> int:
        return 3


class InterruptingWaitAfterFinalizeWorkerProcess(FakeWorkerProcess):
    stop_calls = 0

    def stop_capture(self) -> ProtocolMessage:
        self.__class__.stop_calls += 1
        return super().stop_capture()

    def wait(self, timeout: float | None = None) -> int:
        raise KeyboardInterrupt


class FinalizationFailingWorkerProcess(FakeWorkerProcess):
    def stop_capture(self) -> ProtocolMessage:
        raise ProtocolError("finalization failed")


class TimeoutCapturingWorkerProcess(FakeWorkerProcess):
    last_timeout: float | None = None

    def wait(self, timeout: float | None = None) -> int:
        self.__class__.last_timeout = timeout
        return 0


def _build_summarize_session_bundle(
    tmp_path: Path,
) -> tuple[Path, FileSystemArtifactStore, ArtifactRecord, ArtifactRecord]:
    session_dir = _create_session_record(tmp_path)
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=session_dir)
    merged_record = _persist_merged_transcript(store=store)
    source_ref = ArtifactRef.from_record(merged_record)
    overlay_artifact = create_correction_overlay(
        session_id=SESSION_ID,
        source_transcript_artifact=source_ref,
        entries=(
            CorrectionEntry(
                id="entry_1",
                segment_id="segment_2",
                correction_type="text_edit",
                original_flag_id="flag_1",
                user_action_at="2026-05-19T15:20:01Z",
                text="Corrected segment.",
                metadata={},
            ),
        ),
        now=datetime(2026, 5, 19, 15, 20, 0, tzinfo=UTC),
    )
    overlay_record = persist_correction_overlay_artifact(
        store=store,
        artifact=overlay_artifact,
        name="overlay/active.json",
        metadata={
            "workflow_status": "completed",
            "completed_at": "2026-05-19T15:20:00Z",
        },
    )
    return session_dir, store, merged_record, overlay_record


def _build_verified_export_session_bundle(
    tmp_path: Path,
) -> tuple[Path, FileSystemArtifactStore, ArtifactRecord]:
    session_dir = _create_session_record(tmp_path)
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=session_dir)
    merged_record = store.store(
        artifact_type=MERGED_TRANSCRIPT_ARTIFACT_TYPE,
        name="merged/merged.json",
        data=b'{"version":1}',
        version=1,
        status="sealed",
        metadata={"contract": "merged-transcript.v1"},
    )
    return session_dir, store, merged_record


def _build_cli_playback_session(
    tmp_path: Path,
    *,
    valid_source_refs: bool,
) -> tuple[Path, dict[str, ArtifactRecord]]:
    session_dir = _create_session_record(tmp_path)
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=session_dir)
    source_records: dict[str, ArtifactRecord] = {}
    if valid_source_refs:
        source_records = {
            source_id: store.store(
                artifact_type="source_audio",
                name=f"source_audio/{source_id}.wav",
                data=f"{source_id} audio".encode(),
                status="sealed",
                metadata={"source_id": source_id},
            )
            for source_id in ("local_mic", "system_audio")
        }
        transcript_records = {
            source_id: persist_transcript_artifact(
                store=store,
                transcript=_cli_transcript_fixture(
                    source_id=source_id,
                    source_record=source_record,
                ),
                name=f"transcript/{source_id}.json",
            )
            for source_id, source_record in source_records.items()
        }
        merged_transcript = _cli_merged_transcript_with_refs(
            local_ref=ArtifactRef.from_record(transcript_records["local_mic"]),
            remote_ref=ArtifactRef.from_record(transcript_records["system_audio"]),
        )
    else:
        merged_transcript = _merged_transcript_fixture()

    merged_record = persist_merged_transcript_artifact(
        store=store,
        artifact=merged_transcript,
        name="merged/merged.json",
    )
    persist_review_queue_artifact(
        store=store,
        artifact=ReviewQueueArtifact(
            version=REVIEW_QUEUE_SCHEMA_VERSION,
            session_id=SESSION_ID,
            merged_transcript_artifact=ArtifactRef.from_record(merged_record),
            policy=ConfidenceFlaggingPolicy(),
            flags=(
                ReviewFlag(
                    id="flag_cli_playback",
                    segment_id="segment_2",
                    reason_code="low_speaker_confidence",
                    start_seconds=1.0,
                    end_seconds=1.8,
                    source_id="system_audio",
                    speaker_id="remote",
                    metadata={},
                ),
            ),
            created_at="2026-05-19T15:20:00Z",
        ),
        name="review/queue.json",
    )
    return session_dir, source_records


def _cli_transcript_fixture(
    *,
    source_id: str,
    source_record: ArtifactRecord,
) -> TranscriptArtifact:
    return TranscriptArtifact(
        version=TRANSCRIPT_SCHEMA_VERSION,
        session_id=SESSION_ID,
        source_artifact=ArtifactRef.from_record(source_record),
        source_id=source_id,
        provider=TranscriptProviderMetadata(
            name="fake",
            checkpoint="fixture",
            quantization="none",
            device="test",
            timestamp_mode="segment",
        ),
        language=TranscriptLanguage(requested="en", detected="en", detection_probability=1.0),
        text=f"{source_id} transcript",
        segments=(
            TranscriptSegment(
                id=f"{source_id}_segment",
                text=f"{source_id} transcript",
                start_seconds=0.0,
                end_seconds=2.0,
            ),
        ),
        created_at="2026-05-19T15:10:00Z",
    )


def _cli_merged_transcript_with_refs(
    *,
    local_ref: ArtifactRef,
    remote_ref: ArtifactRef,
) -> MergedTranscriptArtifact:
    base = _merged_transcript_fixture()
    base_sources = {source.source_id: source for source in base.sources}

    def _source(source_id: str, transcript_ref: ArtifactRef) -> MergedTranscriptSource:
        existing = base_sources[source_id]
        return MergedTranscriptSource(
            source_id=existing.source_id,
            channel=existing.channel,
            transcript_artifact=transcript_ref,
            diarization_status=existing.diarization_status,
            diarization_reason=existing.diarization_reason,
            regular_diarization_artifact=existing.regular_diarization_artifact,
            exclusive_diarization_artifact=existing.exclusive_diarization_artifact,
        )

    return MergedTranscriptArtifact(
        version=base.version,
        session_id=base.session_id,
        policy=base.policy,
        sources=(
            _source("local_mic", local_ref),
            _source("system_audio", remote_ref),
        ),
        text=base.text,
        segments=base.segments,
        created_at=base.created_at,
    )


def _create_session_record(tmp_path: Path) -> Path:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    (session_dir / SESSION_RECORD_FILENAME).write_text(
        json.dumps(
            {
                "version": SESSION_RECORD_VERSION,
                "id": SESSION_ID,
                "created_at": "2026-05-19T15:00:00Z",
                "name": None,
                "status": "created",
                "profile": "online_dual_stream",
                "language": "fr-CH",
                "recording": {
                    "started_at": None,
                    "stopped_at": None,
                    "stop_reason": None,
                    "finalization": {
                        "status": "not_started",
                        "completed_at": None,
                        "error": None,
                    },
                },
                "capture_plan": None,
                "degradations": [],
                "failures": [],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return session_dir


def _summary_result_for_export() -> SummaryResult:
    return SummaryResult(
        timeline=("Agenda covered.",),
        important_points=("A decision was made.",),
        action_items=(),
        general_summary="The team aligned on the plan.",
    )


def _persist_summary_artifact_for_cli(
    *,
    store: FileSystemArtifactStore,
    merged_transcript_record: ArtifactRecord | None,
) -> ArtifactRecord:
    summary_result = _summary_result_for_export()
    merged_artifact_ref = (
        ArtifactRef.from_record(merged_transcript_record)
        if merged_transcript_record is not None
        else ArtifactRef(
            id=f"{SESSION_ID}:artifacts/merged_transcript/missing.json",
            artifact_type="merged_transcript",
            version=1,
            sha256="1" * 64,
        )
    )

    return persist_summary_artifact(
        store=store,
        artifact=SummaryArtifact(
            version=1,
            session_id=SESSION_ID,
            merged_transcript_artifact=merged_artifact_ref,
            manual_notes_artifact=None,
            policy=SummarizationPolicy(language="en").to_dict(),
            provider=FakeSummarizationProvider(result=summary_result).metadata,
            timeline=summary_result.timeline,
            important_points=summary_result.important_points,
            action_items=summary_result.action_items,
            general_summary=summary_result.general_summary,
            created_at="2026-05-20T10:00:00Z",
        ),
        name="summary/final.json",
        correction_overlay_record=None,
    )


def _persist_merged_transcript(*, store: FileSystemArtifactStore) -> ArtifactRecord:
    return persist_merged_transcript_artifact(
        store=store,
        artifact=_merged_transcript_fixture(),
        name="merged/merged.json",
    )


def _merged_transcript_fixture() -> MergedTranscriptArtifact:
    source_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/transcript/source.json",
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
                transcript_artifact=source_ref,
                diarization_status="not_applicable",
                diarization_reason="local_user_structural_attribution",
            ),
            MergedTranscriptSource(
                source_id="system_audio",
                channel="remote",
                transcript_artifact=source_ref,
                diarization_status="skipped",
                diarization_reason="diarization_disabled",
            ),
        ),
        text="Hello everyone",
        segments=(
            MergedTranscriptSegment(
                id="segment_1",
                text="Hello",
                start_seconds=0.0,
                end_seconds=0.8,
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
                        reason="fixture",
                    ),
                ),
                transcription_confidence=SegmentConfidence(
                    status="available",
                    metric="word_probability",
                    value=0.99,
                    reason="fixture",
                ),
                overlap_detected=False,
                transcript_segment_id="local_segment_1",
                metadata={},
            ),
            MergedTranscriptSegment(
                id="segment_2",
                text="We need to decide priorities.",
                start_seconds=1.0,
                end_seconds=2.0,
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
                    metric="word_probability",
                    value=0.88,
                    reason="fixture",
                ),
                overlap_detected=False,
                transcript_segment_id="remote_segment_1",
                metadata={},
            ),
        ),
        created_at="2026-05-19T15:00:00Z",
    )


def _build_processing_session(tmp_path: Path) -> CreatedSession:
    created_session = create_session(
        profile=parse_capture_profile("online"),
        data_root=tmp_path,
        now=datetime(2026, 5, 8, 5, 30, 0, tzinfo=UTC),
    )

    local_path = created_session.bundle_path / "local_mic_segment.wav"
    remote_path = created_session.bundle_path / "system_audio_segment.wav"
    local_bytes = b"local audio placeholder"
    remote_bytes = b"system audio placeholder"

    local_path.write_bytes(local_bytes)
    remote_path.write_bytes(remote_bytes)

    update_session_capture_metadata(
        created_session.record_path,
        {
            "version": 1,
            "sample_rate": 48000,
            "channels": 1,
            "segment_seconds": 30,
            "segment_count": 2,
            "segments": [
                {
                    "source_id": "local_mic",
                    "index": 1,
                    "path": str(local_path),
                    "sha256": hashlib.sha256(local_bytes).hexdigest(),
                    "sample_rate": 48000,
                    "channels": 1,
                    "byte_count": len(local_bytes),
                    "status": "sealed",
                    "format": "wav",
                },
                {
                    "source_id": "system_audio",
                    "index": 1,
                    "path": str(remote_path),
                    "sha256": hashlib.sha256(remote_bytes).hexdigest(),
                    "sample_rate": 48000,
                    "channels": 1,
                    "byte_count": len(remote_bytes),
                    "status": "sealed",
                    "format": "wav",
                },
            ],
        },
    )

    return created_session


def _build_multi_segment_processing_session(tmp_path: Path) -> CreatedSession:
    created_session = create_session(
        profile=parse_capture_profile("online"),
        data_root=tmp_path,
        now=datetime(2026, 5, 8, 5, 30, 0, tzinfo=UTC),
    )
    segment_facts: list[dict[str, object]] = []
    for source_id in ("local_mic", "system_audio"):
        for index in (1, 2):
            path = created_session.bundle_path / f"{source_id}_{index:06d}.wav"
            payload = make_wav_bytes(
                bytes([index, index]) * 80,
                sample_rate=8_000,
                channels=1,
            )
            path.write_bytes(payload)
            segment_facts.append(
                {
                    "source_id": source_id,
                    "index": index,
                    "path": str(path),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "sample_rate": 8_000,
                    "channels": 1,
                    "channel_layout": "mono",
                    "sample_format": "pcm_s16le",
                    "byte_count": len(payload),
                    "duration_seconds": 0.01,
                    "started_at": None,
                    "ended_at": None,
                    "status": "sealed",
                    "format": "wav",
                }
            )

    update_session_capture_metadata(
        created_session.record_path,
        {
            "version": 1,
            "profile": "online_dual_stream",
            "sample_rate": 8_000,
            "channels": 1,
            "segment_seconds": 30,
            "segment_count": len(segment_facts),
            "segments": segment_facts,
            "sources": [
                {
                    "source_id": "local_mic",
                    "route_id": None,
                    "route_label": None,
                    "channel_layout": "mono",
                    "segment_count": 2,
                },
                {
                    "source_id": "system_audio",
                    "route_id": None,
                    "route_label": None,
                    "channel_layout": "mono",
                    "segment_count": 2,
                },
            ],
        },
    )
    return created_session


def _build_single_source_processing_session(tmp_path: Path) -> CreatedSession:
    created_session = create_session(
        profile=parse_capture_profile("in-person"),
        data_root=tmp_path,
        now=datetime(2026, 5, 8, 5, 30, 0, tzinfo=UTC),
    )

    room_path = created_session.bundle_path / "room_mic_segment.wav"
    room_bytes = b"room audio placeholder"
    room_path.write_bytes(room_bytes)

    update_session_capture_metadata(
        created_session.record_path,
        {
            "version": 1,
            "sample_rate": 48000,
            "channels": 1,
            "segment_seconds": 30,
            "segment_count": 1,
            "segments": [
                {
                    "source_id": "room_mic",
                    "index": 1,
                    "path": str(room_path),
                    "sha256": hashlib.sha256(room_bytes).hexdigest(),
                    "sample_rate": 48000,
                    "channels": 1,
                    "byte_count": len(room_bytes),
                    "status": "sealed",
                    "format": "wav",
                },
            ],
        },
    )

    return created_session


def _patch_processing_stages(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    invocation: dict[str, object] = {}

    def _fake_run_processing_stages(
        *,
        session_id: str,
        profile: object,
        source_inputs: dict[str, object],
        store: object,
        ledger: object,
        attempt: int,
        now: datetime,
        transcription_provider: object,
        diarization_provider: object,
        semantic_coherence_provider: object,
        session_language: str | None = None,
    ) -> object:
        invocation["session_id"] = session_id
        invocation["run_id"] = getattr(ledger, "run_id", None)
        invocation["attempt"] = attempt
        invocation["session_language"] = session_language
        invocation["source_input_count"] = len(source_inputs)
        invocation["source_inputs"] = source_inputs
        invocation["providers"] = (
            transcription_provider,
            diarization_provider,
            semantic_coherence_provider,
        )
        invocation["profile"] = profile.name.value if hasattr(profile, "name") else profile
        return SimpleNamespace(
            semantic_coherence_result=SimpleNamespace(skipped=False, skip_reason=None)
        )

    monkeypatch.setattr("mena.cli._run_processing_stages", _fake_run_processing_stages)
    return invocation


def _unavailable_discoverer(profile: CaptureProfileId) -> AudioDiscovery:
    return AudioDiscovery(
        version=1,
        profile=profile,
        status=DiscoveryStatus.UNAVAILABLE,
        sources=(),
        sinks=(),
        playback_streams=(),
        error="PipeWire command 'pw-dump' was not found.",
    )


def _available_discoverer(profile: CaptureProfileId) -> AudioDiscovery:
    return AudioDiscovery(
        version=1,
        profile=profile,
        status=DiscoveryStatus.AVAILABLE,
        sources=(
            AudioRoute(
                id=31,
                route_id="pipewire:node:31",
                name="Headset Mic",
                description=None,
                media_class="Audio/Source",
                active=True,
                node_name="alsa_input.usb_mic",
                object_serial="131",
                pw_record_target="alsa_input.usb_mic",
            ),
        ),
        sinks=(
            AudioRoute(
                id=42,
                route_id="pipewire:node:42",
                name="Speakers",
                description=None,
                media_class="Audio/Sink",
                active=True,
                node_name="alsa_output.speakers",
                object_serial="142",
                pw_record_target="alsa_output.speakers",
            ),
        ),
        playback_streams=(
            PlaybackStream(
                id=51,
                route_id="pulse:sink-input:151",
                name="Meeting Stream",
                app_binary_name="firefox",
                stream_name="WebRTC playback",
                media_name="Meet",
                sink_input_id="151",
                active=True,
            ),
        ),
    )


def _discovery_with_playback_streams(*streams: PlaybackStream) -> AudioDiscovery:
    base = _available_discoverer(CaptureProfileId.ONLINE_DUAL_STREAM)
    return replace(base, playback_streams=streams)


def _ambiguous_discoverer(profile: CaptureProfileId) -> AudioDiscovery:
    return AudioDiscovery(
        version=1,
        profile=profile,
        status=DiscoveryStatus.AVAILABLE,
        sources=(
            AudioRoute(
                id=31,
                route_id="pipewire:node:31",
                name="Laptop Mic",
                description=None,
                media_class="Audio/Source",
                active=True,
                node_name="alsa_input.laptop_mic",
                object_serial="131",
                pw_record_target="alsa_input.laptop_mic",
            ),
            AudioRoute(
                id=32,
                route_id="pipewire:node:32",
                name="Headset Mic",
                description=None,
                media_class="Audio/Source",
                active=True,
                node_name="alsa_input.headset_mic",
                object_serial="132",
                pw_record_target="alsa_input.headset_mic",
            ),
        ),
        sinks=(
            AudioRoute(
                id=42,
                route_id="pipewire:node:42",
                name="Speakers",
                description=None,
                media_class="Audio/Sink",
                active=True,
                node_name="alsa_output.speakers",
                object_serial="142",
                pw_record_target="alsa_output.speakers",
            ),
            AudioRoute(
                id=43,
                route_id="pipewire:node:43",
                name="Headphones",
                description=None,
                media_class="Audio/Sink",
                active=True,
                node_name="alsa_output.headphones",
                object_serial="143",
                pw_record_target="alsa_output.headphones",
            ),
        ),
        playback_streams=(
            PlaybackStream(
                id=51,
                route_id="pulse:sink-input:151",
                name="Browser Stream",
                app_binary_name="firefox",
                stream_name="WebRTC playback",
                media_name="Meet",
                sink_input_id="151",
                active=True,
            ),
            PlaybackStream(
                id=52,
                route_id="pulse:sink-input:152",
                name="Call Stream",
                app_binary_name="zoom",
                stream_name="Zoom playback",
                media_name="Zoom",
                sink_input_id="152",
                active=True,
            ),
        ),
    )


def test_collect_capture_segments_requires_capture_metadata() -> None:
    with pytest.raises(ValueError, match="Session does not include capture metadata"):
        _collect_capture_segments({}, ("local_mic", "system_audio"))


def test_collect_capture_segments_rejects_non_list_segments() -> None:
    with pytest.raises(ValueError, match="Session capture metadata does not include segments"):
        _collect_capture_segments(
            {"capture_metadata": {"segments": 7}}, ("local_mic", "system_audio")
        )


def test_collect_capture_segments_rejects_unexpected_source() -> None:
    with pytest.raises(ValueError, match="unexpected capture source"):
        _collect_capture_segments(
            {
                "capture_metadata": {
                    "segments": [
                        {
                            "source_id": "unexpected",
                            "index": 1,
                            "path": "local_mic_segment.wav",
                        }
                    ]
                }
            },
            ("local_mic", "system_audio"),
        )


def test_collect_capture_segments_rejects_non_object_segment() -> None:
    with pytest.raises(ValueError, match="Capture segment metadata must be an object"):
        _collect_capture_segments(
            {"capture_metadata": {"segments": ["oops"]}}, ("local_mic", "system_audio")
        )


def test_collect_capture_segments_rejects_missing_segment_for_source() -> None:
    with pytest.raises(
        ValueError, match="Session is missing a capture segment for source 'system_audio'"
    ):
        _collect_capture_segments(
            {
                "capture_metadata": {
                    "segments": [
                        {
                            "source_id": "local_mic",
                            "index": 1,
                            "path": "local_mic_segment.wav",
                            "status": "sealed",
                        }
                    ]
                }
            },
            ("local_mic", "system_audio"),
        )


def test_collect_capture_segments_orders_multiple_segments_per_source() -> None:
    assert _collect_capture_segments(
        {
            "capture_metadata": {
                "segments": [
                    {
                        "source_id": "local_mic",
                        "index": 2,
                        "path": "local_mic_segment_2.wav",
                        "status": "sealed",
                    },
                    {
                        "source_id": "local_mic",
                        "index": 1,
                        "path": "local_mic_segment.wav",
                        "status": "sealed",
                    },
                ]
            }
        },
        ("local_mic",),
    ) == {
        "local_mic": (
            {
                "path": "local_mic_segment.wav",
                "index": 1,
                "status": "sealed",
                "sha256": None,
                "route_id": None,
                "route_label": None,
                "capture_mode": None,
            },
            {
                "path": "local_mic_segment_2.wav",
                "index": 2,
                "status": "sealed",
                "sha256": None,
                "route_id": None,
                "route_label": None,
                "capture_mode": None,
            },
        )
    }


def test_collect_capture_segments_rejects_duplicate_segment_indexes() -> None:
    with pytest.raises(ValueError, match="duplicate capture segment indexes"):
        _collect_capture_segments(
            {
                "capture_metadata": {
                    "segments": [
                        {
                            "source_id": "local_mic",
                            "index": 1,
                            "path": "a.wav",
                            "status": "sealed",
                        },
                        {
                            "source_id": "local_mic",
                            "index": 1,
                            "path": "b.wav",
                            "status": "sealed",
                        },
                    ]
                }
            },
            ("local_mic",),
        )


def test_collect_capture_segments_rejects_non_sealed_segments() -> None:
    with pytest.raises(ValueError, match="is partial, not sealed"):
        _collect_capture_segments(
            {
                "capture_metadata": {
                    "segments": [
                        {
                            "source_id": "local_mic",
                            "index": 1,
                            "path": "local_mic_segment.wav",
                            "status": "partial",
                        }
                    ]
                }
            },
            ("local_mic",),
        )


def test_collect_capture_segments_rejects_unpaired_online_segment_indexes() -> None:
    with pytest.raises(ValueError, match="Online capture segment sets must be paired"):
        _collect_capture_segments(
            {
                "capture_metadata": {
                    "segments": [
                        {
                            "source_id": "local_mic",
                            "index": 1,
                            "path": "local_mic_1.wav",
                            "status": "sealed",
                        },
                        {
                            "source_id": "local_mic",
                            "index": 2,
                            "path": "local_mic_2.wav",
                            "status": "sealed",
                        },
                        {
                            "source_id": "system_audio",
                            "index": 1,
                            "path": "system_audio_1.wav",
                            "status": "sealed",
                        },
                    ]
                }
            },
            ("local_mic", "system_audio"),
        )


def test_run_processing_stages_routes_in_person_room_mic_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = get_capture_profile(parse_capture_profile("in-person").id)
    store = FileSystemArtifactStore(session_id="session-room", session_dir=tmp_path)
    ledger = ProcessingStageLedger(session_id="session-room", run_id="run-room", store=store)
    calls: list[str] = []

    def _fake_run_transcription_stage(
        *,
        source_id: str | None = None,
        language: str | None = None,
        **_: object,
    ) -> object:
        calls.append(f"transcription:{source_id}:{language}")
        return SimpleNamespace(transcript_record=SimpleNamespace(id=f"{source_id}-transcript"))

    def _fake_run_diarization_stage(*, context: object, **_: object) -> object:
        source_id = getattr(context, "source_id", None)
        calls.append(f"diarization:{source_id}")
        return SimpleNamespace(
            skipped=False,
            artifacts=(
                SimpleNamespace(kind="regular_timeline"),
                SimpleNamespace(kind="exclusive_projection"),
            ),
            artifact_records=(
                SimpleNamespace(id=f"{source_id}-regular"),
                SimpleNamespace(id=f"{source_id}-exclusive"),
            ),
        )

    def _fake_run_room_mic_merge_timing_stage(
        *,
        room_transcript_record: SimpleNamespace,
        room_regular_diarization_record: SimpleNamespace | None,
        room_exclusive_diarization_record: SimpleNamespace | None,
        **_: object,
    ) -> object:
        calls.append(
            "room_merge:"
            f"{room_transcript_record.id}:"
            f"{room_regular_diarization_record.id if room_regular_diarization_record else None}:"
            f"{room_exclusive_diarization_record.id if room_exclusive_diarization_record else None}"
        )
        return SimpleNamespace(merged_transcript_record=SimpleNamespace(id="merged-room"))

    def _fake_run_confidence_flagging_stage(
        *, merged_transcript_record: SimpleNamespace, **_: object
    ) -> object:
        calls.append(f"confidence:{merged_transcript_record.id}")
        return SimpleNamespace(review_queue_record=None, skipped=False)

    def _fake_run_semantic_coherence_stage(
        *, merged_transcript_record: SimpleNamespace, **_: object
    ) -> object:
        calls.append(f"semantic:{merged_transcript_record.id}")
        return SimpleNamespace(review_queue_record=None, skipped=False)

    monkeypatch.setattr("mena.cli.run_transcription_stage", _fake_run_transcription_stage)
    monkeypatch.setattr("mena.cli.run_diarization_stage", _fake_run_diarization_stage)
    monkeypatch.setattr(
        "mena.cli.run_room_mic_merge_timing_stage",
        _fake_run_room_mic_merge_timing_stage,
    )
    monkeypatch.setattr(
        "mena.cli.run_merge_timing_stage",
        lambda **_kwargs: pytest.fail("online merge path should not run for room mic"),
    )
    monkeypatch.setattr(
        "mena.cli.run_confidence_flagging_stage",
        _fake_run_confidence_flagging_stage,
    )
    monkeypatch.setattr(
        "mena.cli.run_semantic_coherence_stage",
        _fake_run_semantic_coherence_stage,
    )

    _run_processing_stages(
        session_id="session-room",
        profile=profile,
        source_inputs=cast(
            dict[str, SourceAudioInput],
            {
                "room_mic": SimpleNamespace(
                    artifact=SimpleNamespace(id="room"),
                    path=tmp_path / "room_mic_segment.wav",
                )
            },
        ),
        store=store,
        ledger=ledger,
        attempt=1,
        now=datetime(2026, 5, 8, 8, 0, tzinfo=UTC),
        transcription_provider=SimpleNamespace(name="x", transcribe=lambda *_args, **_kwargs: None),
        diarization_provider=SimpleNamespace(
            name="y",
            config_id="cfg",
            diarize=lambda *_args, **_kwargs: None,
        ),
        semantic_coherence_provider=SimpleNamespace(
            metadata=SimpleNamespace(config_id="cfg"),
            check=lambda *_args, **_kwargs: True,
        ),
        session_language="de-CH",
    )

    assert calls == [
        "transcription:room_mic:de",
        "diarization:room_mic",
        "room_merge:room_mic-transcript:room_mic-regular:room_mic-exclusive",
        "confidence:merged-room",
        "semantic:merged-room",
    ]


def test_run_processing_stages_skips_diarization_for_system_audio_and_honors_unknown_artifact_kinds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = get_capture_profile(parse_capture_profile("online").id)
    store = FileSystemArtifactStore(session_id="session-skip", session_dir=tmp_path)
    ledger = ProcessingStageLedger(session_id="session-skip", run_id="run-skip", store=store)
    calls: list[str] = []

    def _fake_run_transcription_stage(*, source_id: str | None = None, **_: object) -> object:
        calls.append(f"transcription:{source_id}")
        return SimpleNamespace(transcript_record=SimpleNamespace(id=f"{source_id}-transcript"))

    def _fake_run_diarization_stage(*, context: object, **_: object) -> object:
        source_id = getattr(context, "source_id", None)
        calls.append(f"diarization:{source_id}")
        if source_id == "system_audio":
            return SimpleNamespace(
                skipped=True,
                artifacts=(),
                artifact_records=(),
            )
        return SimpleNamespace(
            skipped=False,
            artifacts=(
                SimpleNamespace(kind="regular_timeline"),
                SimpleNamespace(kind="unexpected"),
            ),
            artifact_records=(
                SimpleNamespace(),
                SimpleNamespace(),
            ),
        )

    def _fake_run_merge_timing_stage(
        *,
        local_transcript_record: SimpleNamespace,
        remote_transcript_record: SimpleNamespace,
        **_: object,
    ) -> object:
        calls.append(f"merge:{local_transcript_record.id}:{remote_transcript_record.id}")
        return SimpleNamespace(merged_transcript_record=SimpleNamespace(id="merged"))

    def _fake_run_confidence_flagging_stage(
        *, merged_transcript_record: SimpleNamespace, **_: object
    ) -> object:
        calls.append(f"confidence:{merged_transcript_record.id}")
        return SimpleNamespace(review_queue_record=None, skipped=False)

    def _fake_run_semantic_coherence_stage(
        *, merged_transcript_record: SimpleNamespace, **_: object
    ) -> object:
        calls.append(f"semantic:{merged_transcript_record.id}")
        return SimpleNamespace(review_queue_record=None, skipped=False)

    monkeypatch.setattr(
        "mena.cli.run_transcription_stage",
        _fake_run_transcription_stage,
    )
    monkeypatch.setattr(
        "mena.cli.run_diarization_stage",
        _fake_run_diarization_stage,
    )
    monkeypatch.setattr(
        "mena.cli.run_merge_timing_stage",
        _fake_run_merge_timing_stage,
    )
    monkeypatch.setattr(
        "mena.cli.run_confidence_flagging_stage",
        _fake_run_confidence_flagging_stage,
    )
    monkeypatch.setattr(
        "mena.cli.run_semantic_coherence_stage",
        _fake_run_semantic_coherence_stage,
    )

    source_inputs = cast(
        dict[str, SourceAudioInput],
        {
            "local_mic": SimpleNamespace(
                artifact=SimpleNamespace(id="local"),
                path=tmp_path / "local_mic_segment.wav",
            ),
            "system_audio": SimpleNamespace(
                artifact=SimpleNamespace(id="system"),
                path=tmp_path / "system_audio_segment.wav",
            ),
        },
    )

    _run_processing_stages(
        session_id="session-skip",
        profile=profile,
        source_inputs=source_inputs,
        store=store,
        ledger=ledger,
        attempt=2,
        now=datetime(2026, 5, 8, 8, 30, tzinfo=UTC),
        transcription_provider=SimpleNamespace(name="x", transcribe=lambda *_args, **_kwargs: None),
        diarization_provider=SimpleNamespace(
            name="y", config_id="cfg", diarize=lambda *_args, **_kwargs: None
        ),
        semantic_coherence_provider=SimpleNamespace(
            metadata=SimpleNamespace(config_id="cfg"),
            check=lambda *_args, **_kwargs: True,
        ),
    )

    assert calls.count("transcription:local_mic") == 1
    assert calls.count("transcription:system_audio") == 1
    assert "diarization:system_audio" in calls
    assert "merge:local_mic-transcript:system_audio-transcript" in calls
    assert "confidence:merged" in calls
    assert "semantic:merged" in calls


def test_main_process_uses_relative_capture_segment_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    session = _build_processing_session(tmp_path)
    record = json.loads(session.record_path.read_text(encoding="utf-8"))
    for segment in record["capture_metadata"]["segments"]:
        segment["path"] = Path(segment["path"]).name

    session.record_path.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(
        "mena.cli._run_processing_stages",
        lambda **_kwargs: None,
    )

    exit_code = _main_process_with_fake_providers(
        [
            "process",
            str(session.record_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Processing completed for run '" in captured.out


def test_main_process_prepares_provider_audio_for_multi_segment_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    session = _build_multi_segment_processing_session(tmp_path)
    invocation = _patch_processing_stages(monkeypatch)

    exit_code = _main_process_with_fake_providers(["process", str(session.record_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Processing completed for run '" in captured.out
    source_inputs = cast(dict[str, SourceAudioInput], invocation["source_inputs"])
    assert source_inputs["local_mic"].artifact.artifact_type == "provider_prep_audio"
    assert source_inputs["system_audio"].artifact.artifact_type == "provider_prep_audio"
    assert source_inputs["local_mic"].path.name == "local_mic.wav"
    store = FileSystemArtifactStore(
        session_id=session.record.id,
        session_dir=session.bundle_path,
    )
    assert len(store.list(artifact_type="source_audio")) == 4
    prep_records = store.list(artifact_type="provider_prep_audio")
    assert len(prep_records) == 2
    assert prep_records[0].metadata["transformations"] == ["lossless_wav_stitch"]
    assert all(record.metadata["sha256"] == record.sha256 for record in prep_records)
    prep_temp_dir = session.bundle_path / "artifacts" / ".provider-prep-tmp"
    assert not prep_temp_dir.exists() or not any(prep_temp_dir.iterdir())


def test_main_process_rematerializes_corrupt_reused_provider_prep_audio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    session = _build_multi_segment_processing_session(tmp_path)
    monkeypatch.setattr(
        "mena.cli._run_processing_stages",
        lambda **_kwargs: None,
    )

    first_exit_code = _main_process_with_fake_providers(["process", str(session.record_path)])
    store = FileSystemArtifactStore(
        session_id=session.record.id,
        session_dir=session.bundle_path,
    )
    prep_record = next(
        record
        for record in store.list(artifact_type="provider_prep_audio")
        if record.metadata["source_id"] == "local_mic"
    )
    prep_path = session.bundle_path / prep_record.path
    prep_path.write_bytes(b"truncated")

    second_exit_code = _main_process_with_fake_providers(["process", str(session.record_path)])

    captured = capsys.readouterr()
    assert first_exit_code == 0
    assert second_exit_code == 0
    assert store.verify(prep_record.id).ok is True
    assert hashlib.sha256(prep_path.read_bytes()).hexdigest() == prep_record.sha256
    assert captured.out.count("Processing completed for run '") == 2


def test_main_process_passes_session_language_to_processing_stages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _build_processing_session(tmp_path)
    record = json.loads(session.record_path.read_text(encoding="utf-8"))
    record["language"] = "de-CH"
    session.record_path.write_text(json.dumps(record), encoding="utf-8")
    invocation = _patch_processing_stages(monkeypatch)

    exit_code = _main_process_with_fake_providers(["process", str(session.record_path)])

    assert exit_code == 0
    assert invocation["session_language"] == "de-CH"


def test_main_process_rejects_missing_capture_segment_file(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    session = _build_processing_session(tmp_path)
    record = json.loads(session.record_path.read_text(encoding="utf-8"))
    record["capture_metadata"]["segments"][0]["path"] = str(tmp_path / "does-not-exist.wav")
    session.record_path.write_text(json.dumps(record), encoding="utf-8")

    exit_code = main(["process", str(session.record_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Capture segment file does not exist" in captured.out


def test_main_process_rejects_capture_segment_directory_path(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    session = _build_processing_session(tmp_path)
    record = json.loads(session.record_path.read_text(encoding="utf-8"))
    record["capture_metadata"]["segments"][0]["path"] = str(tmp_path)
    session.record_path.write_text(json.dumps(record), encoding="utf-8")

    exit_code = main(["process", str(session.record_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Capture segment path is not a file" in captured.out


def test_main_process_rejects_capture_segment_checksum_mismatch(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    session = _build_processing_session(tmp_path)
    record = json.loads(session.record_path.read_text(encoding="utf-8"))
    record["capture_metadata"]["segments"][0]["sha256"] = (
        "0000000000000000000000000000000000000000000000000000000000000000"
    )
    session.record_path.write_text(json.dumps(record), encoding="utf-8")

    exit_code = main(["process", str(session.record_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Capture segment checksum mismatch for source 'local_mic'" in captured.out


def test_main_process_rejects_non_sealed_capture_segment(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    session = _build_processing_session(tmp_path)
    record = json.loads(session.record_path.read_text(encoding="utf-8"))
    record["capture_metadata"]["segments"][0]["status"] = "partial"
    session.record_path.write_text(json.dumps(record), encoding="utf-8")

    exit_code = main(["process", str(session.record_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Capture segment for source 'local_mic' index 1 is partial, not sealed." in (
        captured.out
    )


def test_main_process_rejects_unpaired_online_capture_segments(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    session = _build_multi_segment_processing_session(tmp_path)
    record = json.loads(session.record_path.read_text(encoding="utf-8"))
    record["capture_metadata"]["segments"] = [
        segment
        for segment in record["capture_metadata"]["segments"]
        if not (segment["source_id"] == "system_audio" and segment["index"] == 2)
    ]
    session.record_path.write_text(json.dumps(record), encoding="utf-8")

    exit_code = main(["process", str(session.record_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Online capture segment sets must be paired across sources" in captured.out


def test_main_process_rejects_missing_session_id(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    session = _build_processing_session(tmp_path)
    record = json.loads(session.record_path.read_text(encoding="utf-8"))
    del record["id"]
    session.record_path.write_text(json.dumps(record), encoding="utf-8")

    exit_code = main(["process", str(session.record_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Session field 'id' is required and must be a non-empty string." in captured.out


def test_main_process_rejects_non_positive_capture_index(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    session = _build_processing_session(tmp_path)
    record = json.loads(session.record_path.read_text(encoding="utf-8"))
    record["capture_metadata"]["segments"][0]["index"] = 0
    session.record_path.write_text(json.dumps(record), encoding="utf-8")

    exit_code = main(["process", str(session.record_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Session field 'index' must be a positive integer." in captured.out


def test_main_process_rejects_boolean_capture_index(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    session = _build_processing_session(tmp_path)
    record = json.loads(session.record_path.read_text(encoding="utf-8"))
    record["capture_metadata"]["segments"][0]["index"] = True
    session.record_path.write_text(json.dumps(record), encoding="utf-8")

    exit_code = main(["process", str(session.record_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Session field 'index' must be a positive integer." in captured.out


def test_main_process_executes_in_person_profile_with_room_mic_pipeline(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    session = _build_single_source_processing_session(tmp_path)

    exit_code = main(
        ["process", str(session.record_path)],
        run_id="manual-room-run",
        transcription_provider_factory=FakeTranscriptionProvider,
        diarization_provider_factory=FakeDiarizationProvider,
        semantic_coherence_provider_factory=lambda: FakeSemanticCoherenceProvider(results={}),
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Processing completed for run 'manual-room-run' (attempt 1)." in captured.out
    store = FileSystemArtifactStore(
        session_id=session.record.id,
        session_dir=session.bundle_path,
    )
    assert len(store.list(artifact_type="source_audio")) == 1
    merged_records = store.list(artifact_type=MERGED_TRANSCRIPT_ARTIFACT_TYPE)
    assert len(merged_records) == 1
    merged = MergedTranscriptArtifact.from_dict(json.loads(store.retrieve(merged_records[0].id)))
    assert merged.policy.capture_profile == "in_person_single_mic"
    assert [(source.source_id, source.channel) for source in merged.sources] == [
        ("room_mic", "room")
    ]
    assert merged.segments[0].source_id == "room_mic"
    assert merged.segments[0].speaker.kind == "diarized_room"


def test_main_process_reuses_existing_source_audio_artifacts_on_second_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    session = _build_processing_session(tmp_path)
    monkeypatch.setattr(
        "mena.cli._run_processing_stages",
        lambda **_kwargs: None,
    )

    first_exit_code = _main_process_with_fake_providers(
        [
            "process",
            str(session.record_path),
        ]
    )
    second_exit_code = _main_process_with_fake_providers(
        [
            "process",
            str(session.record_path),
        ]
    )

    captured = capsys.readouterr()
    assert first_exit_code == 0
    assert second_exit_code == 0
    assert captured.out.count("Processing completed for run '") == 2


def test_main_process_reuses_source_audio_with_relative_then_absolute_session_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    session = _build_processing_session(tmp_path)
    record = json.loads(session.record_path.read_text(encoding="utf-8"))
    for segment in record["capture_metadata"]["segments"]:
        segment["path"] = Path(segment["path"]).name
    session.record_path.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "mena.cli._run_processing_stages",
        lambda **_kwargs: None,
    )

    first_exit_code = _main_process_with_fake_providers(
        ["process", str(session.bundle_path.relative_to(tmp_path))]
    )
    second_exit_code = _main_process_with_fake_providers(["process", str(session.bundle_path)])

    captured = capsys.readouterr()
    assert first_exit_code == 0
    assert second_exit_code == 0
    assert captured.out.count("Processing completed for run '") == 2


def test_main_process_reuses_source_audio_after_session_bundle_relocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    session = _build_processing_session(tmp_path)
    record = json.loads(session.record_path.read_text(encoding="utf-8"))
    for segment in record["capture_metadata"]["segments"]:
        segment["path"] = Path(segment["path"]).name
    session.record_path.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(
        "mena.cli._run_processing_stages",
        lambda **_kwargs: None,
    )

    first_exit_code = _main_process_with_fake_providers(["process", str(session.record_path)])
    relocated_root = tmp_path / "relocated"
    relocated_root.mkdir()
    relocated_bundle = relocated_root / session.bundle_path.name
    session.bundle_path.rename(relocated_bundle)
    second_exit_code = _main_process_with_fake_providers(
        ["process", str(relocated_bundle / "session.json")]
    )

    captured = capsys.readouterr()
    assert first_exit_code == 0
    assert second_exit_code == 0
    assert captured.out.count("Processing completed for run '") == 2


def test_main_process_rematerializes_corrupt_reused_source_audio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    session = _build_processing_session(tmp_path)
    monkeypatch.setattr(
        "mena.cli._run_processing_stages",
        lambda **_kwargs: None,
    )

    first_exit_code = _main_process_with_fake_providers(["process", str(session.record_path)])
    source_artifact_path = (
        session.bundle_path / "artifacts/source_audio/source_audio/local_mic_000001.wav"
    )
    source_artifact_path.write_bytes(b"truncated")
    second_exit_code = _main_process_with_fake_providers(["process", str(session.record_path)])

    captured = capsys.readouterr()
    assert first_exit_code == 0
    assert second_exit_code == 0
    assert source_artifact_path.read_bytes() == b"local audio placeholder"
    assert captured.out.count("Processing completed for run '") == 2


def test_main_process_rematerializes_source_audio_when_artifact_parent_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    session = _build_processing_session(tmp_path)
    monkeypatch.setattr(
        "mena.cli._run_processing_stages",
        lambda **_kwargs: None,
    )

    first_exit_code = _main_process_with_fake_providers(["process", str(session.record_path)])
    source_artifact_dir = session.bundle_path / "artifacts/source_audio/source_audio"
    for artifact_file in source_artifact_dir.iterdir():
        artifact_file.unlink()
    source_artifact_dir.rmdir()
    second_exit_code = _main_process_with_fake_providers(["process", str(session.record_path)])

    captured = capsys.readouterr()
    assert first_exit_code == 0
    assert second_exit_code == 0
    assert (source_artifact_dir / "local_mic_000001.wav").read_bytes() == b"local audio placeholder"
    assert (
        source_artifact_dir / "system_audio_000001.wav"
    ).read_bytes() == b"system audio placeholder"
    assert captured.out.count("Processing completed for run '") == 2


def test_main_process_rejects_source_audio_hash_change_when_sha_not_recorded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    session = _build_processing_session(tmp_path)
    record = json.loads(session.record_path.read_text(encoding="utf-8"))
    for segment in record["capture_metadata"]["segments"]:
        segment.pop("sha256", None)

    session.record_path.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(
        "mena.cli._run_processing_stages",
        lambda **_kwargs: None,
    )
    main(["process", str(session.record_path)])

    local_path = session.bundle_path / "local_mic_segment.wav"
    local_path.write_bytes(b"corrupted local segment")

    second_exit_code = main(["process", str(session.record_path)])

    captured = capsys.readouterr()
    assert second_exit_code == 1
    assert "Source artifact collision for local_mic segment 1" in captured.out


def test_require_provider_type_requirements() -> None:

    with pytest.raises(
        TypeError, match="transcription provider must implement TranscriptionProvider"
    ):
        _require_provider(
            provider_factory=lambda: SimpleNamespace(name="bad"),
            provider_type=TranscriptionProvider,
            provider_name="transcription",
        )

    with pytest.raises(TypeError, match="bogus provider must implement int"):
        _require_provider(
            provider_factory=lambda: "nope",
            provider_type=int,
            provider_name="bogus",
        )

    with pytest.raises(
        TypeError, match="semantic_coherence provider must expose semantic metadata config_id"
    ):
        _require_provider(
            provider_factory=lambda: SimpleNamespace(metadata={}, check=lambda *_args: None),
            provider_type=SemanticCoherenceProvider,
            provider_name="semantic_coherence",
        )

    with pytest.raises(
        TypeError, match="semantic_coherence provider must expose semantic metadata to_dict"
    ):
        _require_provider(
            provider_factory=lambda: SimpleNamespace(
                metadata=SimpleNamespace(config_id="cfg"),
                check=lambda *_args: None,
            ),
            provider_type=SemanticCoherenceProvider,
            provider_name="semantic_coherence",
        )

    with pytest.raises(
        TypeError, match="transcription provider must implement TranscriptionProvider"
    ):
        _require_provider(
            provider_factory=lambda: SimpleNamespace(name="bad", transcribe="not-callable"),
            provider_type=TranscriptionProvider,
            provider_name="transcription",
        )

    with pytest.raises(TypeError, match="diarization provider must implement DiarizationProvider"):
        _require_provider(
            provider_factory=lambda: SimpleNamespace(
                name="bad",
                config_id="cfg",
                diarize=None,
            ),
            provider_type=DiarizationProvider,
            provider_name="diarization",
        )


class SetupFakeRunner:
    def __init__(self) -> None:
        self.run_calls: list[tuple[str, ...]] = []
        self.run_envs: list[dict[str, str]] = []

    def which(self, command: str) -> Path | None:
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
        self.run_envs.append(dict(env or {}))
        return CommandRun(command, 0, stdout="ok")


class InterruptingSetupRunner(SetupFakeRunner):
    def run(
        self,
        args: Sequence[str],
        *,
        timeout_seconds: float = 30.0,
        env: Mapping[str, str] | None = None,
    ) -> CommandRun:
        command = tuple(args)
        self.run_calls.append(command)
        self.run_envs.append(dict(env or {}))
        raise KeyboardInterrupt


def _setup_state(
    *,
    settings: AppSettings,
    command_identity: CommandIdentity | None = None,
    hardware: HardwareFacts | None = None,
    ollama_model_present: bool = True,
    asr_model_present: bool | None = True,
    diarization_model_present: bool | None = True,
    pyannote_token_present: bool = False,
    python_surfaces: dict[str, bool] | None = None,
) -> LocalInstallationState:
    profile = get_alpha_local_profile(settings.local_llm.ollama_model)
    surface_presence = python_surfaces or {
        "faster_whisper": True,
        "pyannote.audio": True,
    }
    return LocalInstallationState(
        command_identity=command_identity
        or CommandIdentity(
            current_executable=Path("/home/user/.local/bin/mena"),
            plain_mena_path=Path("/home/user/.local/bin/mena"),
            version="0.1.0",
            invocation_mode="installed-product",
        ),
        runtime_paths=settings.paths,
        hardware=hardware
        or HardwareFacts(
            system="Linux",
            ram_gb=32.0,
            cuda_available=True,
            vram_gb=12.0,
            avx2_available=True,
        ),
        commands=tuple(
            CommandCheck(name=command, path=Path("/usr/bin") / command)
            for command in profile.required_commands
        ),
        playback_commands=tuple(
            CommandCheck(
                name=command,
                path=Path("/usr/bin") / command if command == "ffplay" else None,
            )
            for command in profile.playback_commands
        ),
        python_surfaces=tuple(
            PythonSurfaceCheck(
                requirement=requirement,
                installed=surface_presence[requirement.import_name],
            )
            for requirement in profile.python_surfaces
        ),
        ollama=OllamaState(
            endpoint=settings.local_llm.ollama_endpoint,
            model=settings.local_llm.ollama_model,
            command=CommandCheck("ollama", Path("/usr/bin/ollama")),
            reachable=True,
            model_present=ollama_model_present,
            detail=(
                "model present"
                if ollama_model_present
                else f"model {settings.local_llm.ollama_model} is not listed by Ollama"
            ),
        ),
        asr_model_present=asr_model_present,
        diarization_model_present=diarization_model_present,
        pyannote_token_present=pyannote_token_present,
        python_executable=Path("/usr/bin/python3"),
    )


def _persist_ready_runtime_profile_state(
    settings: AppSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> LocalInstallationState:
    setup_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = _runtime_profile_state_with_evaluated_fallback(
        settings=settings,
        setup_state=setup_state,
    )
    persist_runtime_profile_state(accepted, paths=settings.paths)
    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state",
        lambda *_args, **_kwargs: setup_state,
    )
    return setup_state


def _runtime_profile_state_with_evaluated_fallback(
    *,
    settings: AppSettings,
    setup_state: LocalInstallationState,
) -> RuntimeProfileState:
    primary_profile = get_alpha_local_profile(settings.local_llm.ollama_model)
    fallback_profile = default_profile_catalog(settings.local_llm.ollama_model)[1]
    return build_runtime_profile_state(
        evaluate_setup(primary_profile, setup_state),
        fallback_evaluation=evaluate_setup(fallback_profile, setup_state),
        authorize_local_fallback=True,
    )


def _main_process_with_fake_providers(argv: Sequence[str]) -> int:
    return main(
        argv,
        transcription_provider_factory=FakeTranscriptionProvider,
        diarization_provider_factory=FakeDiarizationProvider,
        semantic_coherence_provider_factory=lambda: FakeSemanticCoherenceProvider(results={}),
    )
