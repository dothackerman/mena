import hashlib
import inspect
import json
import queue
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pytest import CaptureFixture

import mena.cli as cli_module
from mena.artifacts import (
    ArtifactRecord,
    FileSystemArtifactStore,
    session_artifact_manifest_path,
)
from mena.capture import make_wav_bytes
from mena.cli import list as list_command
from mena.cli import main
from mena.cli import setup as setup_command
from mena.cli import summarize as summarize_command
from mena.cli._console import _resolve_console_input
from mena.cli.correct import ConsoleCorrectionInteraction
from mena.cli.processing import ConsoleProcessingInteraction
from mena.cli.record import _resolve_record_language, _resolve_record_profile
from mena.diarization import FakeDiarizationProvider
from mena.discovery import (
    AudioDiscovery,
    AudioRoute,
    DiscoveryStatus,
    PlaybackStream,
    format_discovery,
)
from mena.interaction import (
    ChoiceOption,
    ChoiceRequest,
    ConfirmationRequest,
    TextRequest,
    WorkflowNotice,
)
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
from mena.mistral_llm import MISTRAL_SUMMARY_DISCLOSURE, MistralSummaryState
from mena.ollama_summarization import (
    OLLAMA_SUMMARIZATION_NUM_CTX,
    OLLAMA_SUMMARIZATION_SEED,
)
from mena.processing import (
    PROCESSING_STATE_ARTIFACT_TYPE,
    ArtifactRef,
    ProcessingErrorInfo,
    ProcessingStageLedger,
    ProcessingStageState,
)
from mena.profiles import CaptureProfileId, parse_capture_profile
from mena.protocol import (
    CaptureRouteBindingPayload,
    ProtocolError,
    ProtocolMessage,
    StartCapturePayload,
    WorkerProcess,
)
from mena.recording_control import WORKER_EXIT_TIMEOUT_SECONDS
from mena.review_flagging import (
    REVIEW_QUEUE_SCHEMA_VERSION,
    ConfidenceFlaggingPolicy,
    ReviewFlag,
    ReviewQueueArtifact,
    persist_review_queue_artifact,
)
from mena.runtime_profile import (
    SUMMARIZATION_RUNTIME_STAGES,
    RuntimeFallbackUnavailable,
    select_runtime_profile,
)
from mena.runtime_stewardship import (
    RUNTIME_STEWARDSHIP_RELEASE_NOTICE_TOPIC,
    RuntimeLease,
    RuntimeReleaseResult,
)
from mena.semantic_coherence import (
    FakeSemanticCoherenceProvider,
)
from mena.session_lock import SessionLockContention, acquire_session_lock
from mena.sessions import (
    SESSION_RECORD_FILENAME,
    SESSION_RECORD_VERSION,
    CreatedSession,
    create_session,
    persist_session_recording_lifecycle,
    update_session_capture_metadata,
)
from mena.settings import AppSettings
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
    REQUIRED_NOTE_PRODUCTS,
    SUMMARY_SCHEMA_VERSION,
    FakeSummarizationProvider,
    ProductRequestRecord,
    SummarizationError,
    SummarizationPolicy,
    SummaryArtifact,
    SummaryResult,
    SummaryTimelineEntry,
    persist_summary_artifact,
)
from mena.transcription import (
    TRANSCRIPT_SCHEMA_VERSION,
    FakeTranscriptionProvider,
    TranscriptArtifact,
    TranscriptLanguage,
    TranscriptProviderMetadata,
    TranscriptSegment,
    persist_transcript_artifact,
)
from support.runtime_profiles import arrange_accepted_local_runtime
from support.scenarios.summarization import (
    SummarizationSessionSpec,
    arrange_ready_summarization,
)
from test_correction_workflow import (
    _build_session_bundle as _build_correction_session_bundle,
)

COMMANDS = (
    "record",
    "setup",
    "list",
    "show",
    "process",
    "correct",
    "summarize",
    "export",
    "inspect",
    "resume",
)
UNIMPLEMENTED_COMMANDS = ("show",)

SESSION_ID = "00000000-0000-0000-0000-000000000001"


def test_main_prints_help_without_command(capsys: CaptureFixture[str]) -> None:
    exit_code = main([])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Privacy-first local meeting notes PoC." in captured.out


def test_main_help_describes_every_registered_command(capsys: CaptureFixture[str]) -> None:
    exit_code = main([])

    captured = capsys.readouterr()
    assert exit_code == 0
    command_descriptions = {
        fields[0]: fields[1]
        for line in captured.out.splitlines()
        if len(fields := line.split(maxsplit=1)) == 2 and fields[0] in COMMANDS
    }
    assert set(command_descriptions) == set(COMMANDS)
    assert all(description.strip() for description in command_descriptions.values())
    normalized_help = " ".join(captured.out.split())
    assert "show Planned; not implemented in the current PoC." in normalized_help


def test_main_exposes_architecture_command_surface(capsys: CaptureFixture[str]) -> None:
    assert set(UNIMPLEMENTED_COMMANDS) <= set(COMMANDS)
    for command in UNIMPLEMENTED_COMMANDS:
        exit_code = main([command])
        captured = capsys.readouterr()

        assert exit_code == 0
        assert f"Command '{command}' is not implemented yet." in captured.out


def test_main_defaults_resolve_under_isolated_test_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    projected_roots: list[Path] = []
    resolved_settings: list[AppSettings] = []
    default_settings_factory = AppSettings.from_env

    def _settings_from_env(
        _cls: type[AppSettings],
        env: Mapping[str, str] | None = None,
        *,
        home: Path | None = None,
        config_path: Path | None = None,
    ) -> AppSettings:
        settings = default_settings_factory(
            env,
            home=home,
            config_path=config_path,
        )
        resolved_settings.append(settings)
        return settings

    def _rebuild_session_index(sessions_root: Path) -> tuple[()]:
        projected_roots.append(sessions_root)
        return ()

    monkeypatch.setattr(AppSettings, "from_env", classmethod(_settings_from_env))
    monkeypatch.setattr(list_command, "rebuild_session_index", _rebuild_session_index)

    exit_code = main(["list"])

    assert exit_code == 0
    assert projected_roots == [tmp_path / "mena-data" / "sessions"]
    assert len(resolved_settings) == 1
    assert resolved_settings[0].paths.data_home == tmp_path / "xdg-data" / "mena"
    assert resolved_settings[0].paths.config_home == tmp_path / "xdg-config" / "mena"
    assert resolved_settings[0].paths.cache_home == tmp_path / "xdg-cache" / "mena"
    assert resolved_settings[0].paths.state_home == tmp_path / "xdg-state" / "mena"
    assert "No sessions found." in capsys.readouterr().out


def test_main_prints_version(capsys: CaptureFixture[str]) -> None:
    exit_code = main(["--version"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.strip()


def test_main_returns_exit_code_4_for_session_lock_contention(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    session = create_session(
        profile=parse_capture_profile("online"),
        data_root=tmp_path,
        now=datetime(2026, 5, 8, 5, 30, 0, tzinfo=UTC),
    )

    def _locked_processing_runner(**_kwargs: object) -> int:
        raise SessionLockContention(
            f"Session is already locked by orchestrator pid=123 command=summarize "
            f"for session {session.bundle_path}."
        )

    exit_code = main(
        ["process", str(session.bundle_path)],
        settings=AppSettings.from_env(env={}, home=tmp_path),
        processing_command_runner=_locked_processing_runner,
    )

    captured = capsys.readouterr()
    assert exit_code == 4
    assert "pid=123 command=summarize" in captured.out


def test_setup_check_reports_product_paths_and_ready_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    state = _setup_state(settings=settings)

    def _detect_state(_settings: AppSettings, **_kwargs: object) -> LocalInstallationState:
        return state

    monkeypatch.setattr("mena.setup_detection.detect_local_installation_state", _detect_state)

    exit_code = main(["setup", "--check"], settings=settings)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Profile: linux-alpha-local-v1" in captured.out
    assert "Current executable: /home/user/.local/bin/mena" in captured.out
    assert "Plain mena on PATH: /home/user/.local/bin/mena" in captured.out
    assert f"sessions: {settings.paths.sessions_dir}" in captured.out
    assert "Invocation mode: installed-product" in captured.out
    assert "Local provider readiness: complete" in captured.out


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

    monkeypatch.setattr("mena.setup_detection.detect_local_installation_state", _detect_state)

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

    monkeypatch.setattr("mena.setup_detection.detect_local_installation_state", _detect_state)

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

    monkeypatch.setattr("mena.setup_detection.detect_local_installation_state", _detect_state)

    exit_code = main(["setup", "--yes"], settings=settings, setup_runner=runner)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert runner.run_calls == []
    assert "current invocation mode is repo/dev" in captured.out
    assert (
        "Setup actions were not run because this is not the installed product command."
        in captured.out
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

    monkeypatch.setattr("mena.setup_detection.detect_local_installation_state", _detect_state)

    exit_code = main(
        ["setup", "--yes", "--authorize-local-fallback"],
        settings=settings,
        setup_runner=SetupFakeRunner(),
    )
    captured = capsys.readouterr()

    accepted = load_runtime_profile_state(settings.paths)
    assert exit_code == 0
    assert "Automatic local runtime fallback authorized for:" in captured.out
    assert "Setup is complete. Mena is ready to record." in captured.out
    assert "Next command: mena record" in captured.out
    assert "Optional local fallback policy: enabled" in captured.out
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

    monkeypatch.setattr("mena.setup_detection.detect_local_installation_state", _detect_state)

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
    assert "Setup is complete. Mena is ready to record." in captured.out
    assert "Next command: mena record" in captured.out
    assert "Optional local fallback policy: unavailable" in captured.out
    assert "Fallback blocker:" in captured.out
    assert "selected faster-whisper model" in captured.out


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

    monkeypatch.setattr("mena.setup_detection.detect_local_installation_state", _detect_state)

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

    monkeypatch.setattr("mena.setup_detection.detect_local_installation_state", _detect_state)

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

    monkeypatch.setattr("mena.setup_detection.detect_local_installation_state", _detect_state)

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

    monkeypatch.setattr("mena.setup_detection.detect_local_installation_state", _detect_state)

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

    monkeypatch.setattr("mena.setup_detection.detect_local_installation_state", _detect_state)

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

    monkeypatch.setattr("mena.setup_detection.detect_local_installation_state", _detect_state)

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


def test_record_prompt_text_for_output_sink_selection(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    FakeWorkerProcess.last_start_payload = None
    prompts: list[str] = []

    def _input(prompt: str) -> str:
        prompts.append(prompt)
        if "record>" in prompt:
            return "q"
        return "1"

    exit_code = main(
        [
            "record",
            "--profile",
            "online",
            "--language",
            "en",
            "--mic",
            "pipewire:node:31",
            "--playback-stream",
            "pulse:sink-input:151",
        ],
        data_root=tmp_path,
        discoverer=_ambiguous_discoverer,
        worker_factory=FakeWorkerProcess,
        input_fn=_input,
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Where do you hear the meeting?" in captured.out.splitlines()
    assert any(prompt.startswith("Select meeting audio output [1]:") for prompt in prompts)
    assert FakeWorkerProcess.last_start_payload is not None
    output_sink_route_ids = [
        route.route_id
        for route in FakeWorkerProcess.last_start_payload.routes
        if route.role == "output_sink"
    ]
    assert output_sink_route_ids == ["pipewire:node:42"]


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


def test_format_discovery_uses_neutral_idle_route_wording() -> None:
    discovery = AudioDiscovery(
        version=1,
        profile=CaptureProfileId.ONLINE_DUAL_STREAM,
        status=DiscoveryStatus.AVAILABLE,
        sources=(
            AudioRoute(
                id=31,
                route_id="pipewire:node:31",
                name="Headset Mic",
                description=None,
                media_class="Audio/Source",
                active=False,
                node_name="alsa_input.usb_mic",
                object_serial="131",
                pw_record_target="alsa_input.usb_mic",
            ),
        ),
        sinks=(),
        playback_streams=(
            PlaybackStream(
                id=51,
                route_id="pulse:sink-input:151",
                name="Meeting Stream",
                app_binary_name="firefox",
                stream_name="WebRTC playback",
                media_name="Meet",
                sink_input_id="151",
                active=False,
            ),
        ),
    )

    formatted = format_discovery(discovery)

    assert "[idle]" in formatted
    assert "[inactive]" not in formatted


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


def test_cli_does_not_wire_concrete_processing_provider_defaults() -> None:
    source = inspect.getsource(cli_module)

    assert "FasterWhisperTranscriptionProvider" not in source
    assert "PyannoteCommunityDiarizationProvider" not in source
    assert "OllamaSemanticCoherenceProvider" not in source


def test_main_summarize_rejects_empty_explicit_cli_language(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    scenario = arrange_ready_summarization(root=tmp_path)
    session_dir = scenario.session_dir
    config_path = tmp_path / "config.toml"
    config_path.write_text("[summary]\nlanguage = 'fr-CH'\n", encoding="utf-8")

    # The adapter validates the --language flag before delegating to the
    # workflow, so an empty value fails fast without any summarization run.
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


def test_main_process_leaves_default_semantic_provider_to_resolver(
    tmp_path: Path,
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
    processing = _ProcessingCommandSpy(
        side_effect=lambda kwargs: captured.update(
            {"semantic_factory": kwargs["semantic_coherence_provider_factory"]}
        )
    )

    exit_code = main(
        ["process", str(session_dir)],
        config_path=config_path,
        processing_command_runner=processing,
    )

    assert exit_code == 0
    assert captured["semantic_factory"] is None


def test_main_process_without_session_path_uses_latest_recorded_session(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    older = _create_runtime_session(settings, status="recorded", created_at="2026-05-19T10:00:00Z")
    _mark_processing_complete(older)
    _create_runtime_session(settings, status="failed", created_at="2026-05-19T11:00:00Z")
    latest = _create_runtime_session(
        settings,
        status="recorded",
        created_at="2026-05-19T12:00:00Z",
    )
    processing = _ProcessingCommandSpy()

    exit_code = main(["process"], settings=settings, processing_command_runner=processing)

    assert exit_code == 0
    assert processing.session_paths == [latest.record_path]
    assert older.record_path != latest.record_path


def test_main_process_without_session_path_prefers_unprocessed_recording(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    unprocessed = _create_runtime_session(
        settings,
        status="recorded",
        created_at="2026-05-19T10:00:00Z",
    )
    processed = _create_runtime_session(
        settings,
        status="recorded",
        created_at="2026-05-19T12:00:00Z",
    )
    _mark_processing_complete(processed)
    processing = _ProcessingCommandSpy()

    exit_code = main(["process"], settings=settings, processing_command_runner=processing)

    assert exit_code == 0
    assert processing.session_paths == [unprocessed.record_path]


def test_main_process_without_session_path_skips_record_with_malformed_id(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    valid = _create_runtime_session(
        settings,
        status="recorded",
        created_at="2026-05-19T10:00:00Z",
    )
    malformed = _create_runtime_session(
        settings,
        status="recorded",
        created_at="2026-05-19T12:00:00Z",
    )
    malformed_record = json.loads(malformed.record_path.read_text(encoding="utf-8"))
    malformed_record.pop("id")
    malformed.record_path.write_text(json.dumps(malformed_record), encoding="utf-8")
    processing = _ProcessingCommandSpy()

    exit_code = main(["process"], settings=settings, processing_command_runner=processing)

    assert exit_code == 0
    assert processing.session_paths == [valid.record_path]


def test_main_process_without_session_path_skips_provider_unavailable_semantic_skip(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    unprocessed = _create_runtime_session(
        settings,
        status="recorded",
        created_at="2026-05-19T10:00:00Z",
    )
    skipped = _create_runtime_session(
        settings,
        status="recorded",
        created_at="2026-05-19T12:00:00Z",
    )
    _mark_semantic_provider_unavailable_skip(skipped)
    processing = _ProcessingCommandSpy()

    exit_code = main(["process"], settings=settings, processing_command_runner=processing)

    assert exit_code == 0
    assert processing.session_paths == [unprocessed.record_path]


def test_main_process_without_session_path_keeps_completed_session_with_corrupt_output(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    session = _create_runtime_session(
        settings,
        status="recorded",
        created_at="2026-05-19T12:00:00Z",
    )
    _mark_processing_complete(session)
    output_path = session.bundle_path / "artifacts" / "semantic_review" / "semantic-review.json"
    output_path.write_bytes(b"{")
    processing = _ProcessingCommandSpy()

    exit_code = main(["process"], settings=settings, processing_command_runner=processing)

    assert exit_code == 0
    assert processing.session_paths == [session.record_path]


def test_main_process_without_session_path_reports_corrupt_completed_manifest(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    valid_unprocessed = _create_runtime_session(
        settings,
        status="recorded",
        created_at="2026-05-19T10:00:00Z",
    )
    corrupt_completed = _create_runtime_session(
        settings,
        status="recorded",
        created_at="2026-05-19T12:00:00Z",
    )
    _mark_processing_complete(corrupt_completed)
    session_artifact_manifest_path(corrupt_completed.bundle_path).write_text(
        json.dumps({"version": 999, "session_id": corrupt_completed.record.id, "artifacts": []}),
        encoding="utf-8",
    )
    processing = _ProcessingCommandSpy()

    exit_code = main(["process"], settings=settings, processing_command_runner=processing)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert processing.calls == []
    assert valid_unprocessed.record_path != corrupt_completed.record_path
    assert "Cannot run processing workflow: Unsupported artifact manifest version." in captured.out


def test_main_process_without_session_path_reports_unreadable_completed_manifest(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    valid_unprocessed = _create_runtime_session(
        settings,
        status="recorded",
        created_at="2026-05-19T10:00:00Z",
    )
    unreadable_completed = _create_runtime_session(
        settings,
        status="recorded",
        created_at="2026-05-19T12:00:00Z",
    )
    _mark_processing_complete(unreadable_completed)
    manifest_path = session_artifact_manifest_path(unreadable_completed.bundle_path)
    manifest_path.unlink()
    manifest_path.mkdir()
    processing = _ProcessingCommandSpy()

    exit_code = main(["process"], settings=settings, processing_command_runner=processing)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert processing.calls == []
    assert valid_unprocessed.record_path != unreadable_completed.record_path
    assert "Cannot run processing workflow:" in captured.out
    assert "artifact-manifest.v1.json" in captured.out


def test_main_process_without_session_path_keeps_failed_processing_candidate(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    session = _create_runtime_session(
        settings,
        status="recorded",
        created_at="2026-05-19T10:00:00Z",
    )
    _mark_processing_failed(session)
    processing = _ProcessingCommandSpy()

    exit_code = main(["process"], settings=settings, processing_command_runner=processing)

    assert exit_code == 0
    assert processing.session_paths == [session.record_path]


def test_main_process_without_session_path_prompts_for_multiple_candidates(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    older = _create_runtime_session(
        settings,
        status="recorded",
        created_at="2026-05-19T10:00:00Z",
    )
    latest = _create_runtime_session(
        settings,
        status="recorded",
        created_at="2026-05-19T12:00:00Z",
    )
    processing = _ProcessingCommandSpy()

    exit_code = main(
        ["process"],
        settings=settings,
        input_fn=lambda _prompt: "",
        processing_command_runner=processing,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert processing.session_paths == [latest.record_path]
    assert "Processable recordings:" in captured.out
    assert str(latest.record_path) in captured.out
    assert str(older.record_path) in captured.out


def test_main_process_without_session_path_can_select_older_candidate(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    older = _create_runtime_session(
        settings,
        status="recorded",
        created_at="2026-05-19T10:00:00Z",
    )
    _create_runtime_session(
        settings,
        status="recorded",
        created_at="2026-05-19T12:00:00Z",
    )
    processing = _ProcessingCommandSpy()

    exit_code = main(
        ["process"],
        settings=settings,
        input_fn=lambda _prompt: "2",
        processing_command_runner=processing,
    )

    assert exit_code == 0
    assert processing.session_paths == [older.record_path]


def test_main_process_without_session_path_rejects_multiple_candidates_noninteractive(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    _create_runtime_session(
        settings,
        status="recorded",
        created_at="2026-05-19T10:00:00Z",
    )
    _create_runtime_session(
        settings,
        status="recorded",
        created_at="2026-05-19T12:00:00Z",
    )

    exit_code = main(["process"], settings=settings)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert (
        "Cannot run processing workflow: multiple processable recordings found; "
        "pass an explicit session path."
    ) in captured.out


def test_main_process_without_session_path_uses_latest_completed_recording(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    short = _create_runtime_session(
        settings,
        status="recorded",
        created_at="2026-05-19T11:00:00Z",
        completed_at="2026-05-19T11:30:00Z",
    )
    _mark_processing_complete(short)
    long = _create_runtime_session(
        settings,
        status="recorded",
        created_at="2026-05-19T10:00:00Z",
        completed_at="2026-05-19T12:00:00Z",
    )
    processing = _ProcessingCommandSpy()

    exit_code = main(["process"], settings=settings, processing_command_runner=processing)

    assert exit_code == 0
    assert processing.session_paths == [long.record_path]
    assert short.record_path != long.record_path


def test_main_process_without_session_path_rejects_ambiguous_latest_recording(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    _create_runtime_session(
        settings,
        status="recorded",
        created_at="2026-05-19T10:00:00Z",
        completed_at="2026-05-19T12:00:00Z",
    )
    _create_runtime_session(
        settings,
        status="recorded",
        created_at="2026-05-19T11:00:00Z",
        completed_at="2026-05-19T12:00:00Z",
    )

    exit_code = main(["process"], settings=settings)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert (
        "Cannot run processing workflow: multiple processable recordings found; "
        "pass an explicit session path."
    ) in captured.out


def test_main_process_without_session_path_rejects_unorderable_recorded_session(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    _create_runtime_session(
        settings,
        status="recorded",
        created_at="2026-05-19T10:00:00Z",
        completed_at="2026-05-19T12:00:00Z",
    )
    unorderable = _create_runtime_session(
        settings,
        status="recorded",
        created_at="2026-05-19T13:00:00Z",
    )
    record = json.loads(unorderable.record_path.read_text(encoding="utf-8"))
    record["recording"]["finalization"]["completed_at"] = None
    unorderable.record_path.write_text(json.dumps(record), encoding="utf-8")

    exit_code = main(["process"], settings=settings)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert (
        "Cannot run processing workflow: recorded session is missing "
        "recording.finalization.completed_at; pass an explicit session path."
    ) in captured.out


def test_main_process_without_session_path_reports_missing_recorded_session(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    _create_runtime_session(settings, status="failed", created_at="2026-05-19T11:00:00Z")

    exit_code = main(["process"], settings=settings)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Cannot run processing workflow: no recorded sessions found." in captured.out


def test_main_process_without_session_path_reports_missing_sessions_root(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)

    exit_code = main(["process"], settings=settings)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Cannot run processing workflow: no recorded sessions found." in captured.out


def test_main_process_explicit_session_path_overrides_latest_recorded_session(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    explicit = _create_runtime_session(
        settings,
        status="recorded",
        created_at="2026-05-19T10:00:00Z",
    )
    latest = _create_runtime_session(
        settings,
        status="recorded",
        created_at="2026-05-19T12:00:00Z",
    )
    processing = _ProcessingCommandSpy()

    exit_code = main(
        ["process", str(explicit.record_path)],
        settings=settings,
        processing_command_runner=processing,
    )

    assert exit_code == 0
    assert processing.session_paths == [explicit.record_path]
    assert explicit.record_path != latest.record_path


def test_main_process_prints_start_and_success_handoff(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    session = _create_runtime_session(
        settings,
        status="recorded",
        created_at="2026-05-19T10:00:00Z",
    )
    processing = _ProcessingCommandSpy(
        side_effect=lambda _kwargs: print("third-party dependency warning")
    )

    exit_code = main(["process"], settings=settings, processing_command_runner=processing)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert f"Processing recording: {session.record_path}" in captured.out
    assert "third-party dependency warning" in captured.out
    assert "Processing completed successfully." in captured.out
    assert f"Next command: mena correct {session.record_path}" in captured.out
    assert captured.out.rfind("Processing completed successfully.") > captured.out.rfind(
        "third-party dependency warning"
    )


def test_main_process_omits_correction_handoff_after_semantic_provider_skip(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    session = _create_runtime_session(
        settings,
        status="recorded",
        created_at="2026-05-19T10:00:00Z",
    )
    processing = _ProcessingCommandSpy(
        side_effect=lambda _kwargs: _mark_semantic_provider_unavailable_skip(session)
    )

    exit_code = main(["process"], settings=settings, processing_command_runner=processing)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Processing completed successfully." in captured.out
    assert "Next command: mena correct" not in captured.out


def test_main_process_does_not_print_success_handoff_on_failure(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    _create_runtime_session(
        settings,
        status="recorded",
        created_at="2026-05-19T10:00:00Z",
    )

    def _raise_processing_failed(**_kwargs: object) -> int:
        raise ValueError("processing failed")

    exit_code = main(
        ["process"],
        settings=settings,
        processing_command_runner=_raise_processing_failed,
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Cannot run processing workflow: processing failed" in captured.out
    assert "Processing completed successfully." not in captured.out
    assert "Next command: mena correct" not in captured.out


def test_main_summarize_accepts_session_json_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = arrange_ready_summarization(root=tmp_path)
    session_dir = scenario.session_dir
    session_json_path = session_dir / SESSION_RECORD_FILENAME
    observed: dict[str, object] = {}

    def _fake_run_summarization_command(**kwargs: object) -> int:
        observed["session_path"] = kwargs["session_path"]
        observed["summarization_provider_factory"] = kwargs["summarization_provider_factory"]
        return 0

    def _summarization_provider_factory() -> FakeSummarizationProvider:
        return FakeSummarizationProvider(result=_summary_result_for_export())

    monkeypatch.setattr(
        "mena.summarize_workflow.run_summarization_command",
        _fake_run_summarization_command,
    )
    settings = AppSettings.from_env(env={}, home=tmp_path)

    exit_code = main(
        ["summarize", str(session_json_path)],
        settings=settings,
        summarization_provider_factory=_summarization_provider_factory,
    )

    # A session.json file path is accepted and forwarded to the summarize
    # workflow, which resolves both directory and record-file paths.
    assert exit_code == 0
    assert observed["session_path"] == session_json_path
    assert observed["summarization_provider_factory"] is _summarization_provider_factory


def test_main_summarize_auto_selects_ready_session_when_session_path_is_omitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    session_dir = arrange_ready_summarization(
        root=tmp_path / "sessions",
        session=SummarizationSessionSpec(directory_name="single-session"),
        corrections=(),
    ).session_dir
    observed: dict[str, Path] = {}

    def _fake_run_summarization_command(**kwargs: object) -> int:
        observed["session_path"] = cast(Path, kwargs["session_path"])
        return 0

    monkeypatch.setattr(
        "mena.summarize_workflow.run_summarization_command",
        _fake_run_summarization_command,
    )

    exit_code = main(["summarize"], data_root=tmp_path)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert observed["session_path"] == session_dir
    assert f"Session path: {session_dir}" in captured.out


def test_main_summarize_prompts_and_recommends_newest_ready_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    arrange_ready_summarization(
        root=tmp_path / "sessions",
        session=SummarizationSessionSpec(
            directory_name="older", created_at="2026-05-19T12:00:00Z", name="Older Ready Session"
        ),
        corrections=(),
    )
    newer_session = arrange_ready_summarization(
        root=tmp_path / "sessions",
        session=SummarizationSessionSpec(
            directory_name="newer", created_at="2026-05-19T13:00:00Z", name="Newest Ready Session"
        ),
        corrections=(),
    ).session_dir
    observed: dict[str, Path] = {}

    def _fake_run_summarization_command(**kwargs: object) -> int:
        observed["session_path"] = cast(Path, kwargs["session_path"])
        return 0

    monkeypatch.setattr(
        "mena.summarize_workflow.run_summarization_command",
        _fake_run_summarization_command,
    )

    exit_code = main(
        ["summarize"],
        data_root=tmp_path,
        input_fn=lambda _prompt: "",
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert observed["session_path"] == newer_session
    assert "Ready session to summarize" in captured.out
    assert "Newest Ready Session" in captured.out
    assert "[recommended]" in captured.out
    assert f"Session path: {newer_session}" in captured.out


def test_main_summarize_with_no_ready_sessions_returns_error(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    exit_code = main(["summarize"], data_root=tmp_path)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "No ready sessions were found for summarization." in captured.out


def test_main_summarize_ignores_invalid_ready_session_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arrange_ready_summarization(
        root=tmp_path / "sessions",
        session=SummarizationSessionSpec(
            directory_name="invalid-session", created_at="2026-05-19T11:00:00Z"
        ),
        corrections=(),
    )
    valid_session = arrange_ready_summarization(
        root=tmp_path / "sessions",
        session=SummarizationSessionSpec(
            directory_name="valid-session", created_at="2026-05-19T12:00:00Z"
        ),
        corrections=(),
    ).session_dir
    invalid_session = tmp_path / "sessions" / "bad-session"
    invalid_session.mkdir(parents=True)
    # Not a valid session record; should be ignored during candidate discovery.
    (invalid_session / SESSION_RECORD_FILENAME).write_text("{invalid json", encoding="utf-8")

    observed: dict[str, Path] = {}

    def _fake_run_summarization_command(**kwargs: object) -> int:
        observed["session_path"] = cast(Path, kwargs["session_path"])
        return 0

    monkeypatch.setattr(
        "mena.summarize_workflow.run_summarization_command",
        _fake_run_summarization_command,
    )

    exit_code = main(["summarize"], data_root=tmp_path)

    assert exit_code == 0
    assert observed["session_path"] == valid_session


def test_main_summarize_requires_accepted_runtime_profile_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    scenario = arrange_ready_summarization(root=tmp_path)
    session_dir = scenario.session_dir
    settings = AppSettings.from_env(env={}, home=tmp_path)
    runtime = arrange_accepted_local_runtime(settings=settings)
    (settings.paths.state_home / "setup-state.json").unlink()
    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state",
        lambda *_args, **_kwargs: runtime.installation_state,
    )

    exit_code = main(["summarize", str(session_dir)], settings=settings)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Accepted runtime profile state is missing" in captured.out
    assert str(settings.paths.state_home / "setup-state.json") in captured.out


def test_summarize_runtime_scope_still_checks_ollama_after_bad_hardware_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    runtime = arrange_accepted_local_runtime(settings=settings)

    def _detect_summary_state(
        _settings: AppSettings,
        **_kwargs: object,
    ) -> LocalInstallationState:
        state = runtime.installation_state
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
    scenario = arrange_ready_summarization(root=tmp_path)
    (scenario.session_dir / scenario.merged_record.path).unlink()

    exit_code = main(["summarize", str(scenario.session_dir)])
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
    scenario = arrange_ready_summarization(root=tmp_path)
    manifest_path = scenario.store.manifest_path
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert isinstance(manifest, dict)
    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, list)
    manifest["artifacts"] = [
        artifact
        for artifact in artifacts
        if isinstance(artifact, dict) and artifact.get("id") != scenario.overlay_record.id
    ]
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    exit_code = main(["summarize", str(scenario.session_dir)])
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
    def _run_command_fails(**_kwargs: object) -> int:
        raise SummarizationError("Local summary provider is unavailable.")

    monkeypatch.setattr(
        "mena.summarize_workflow.run_summarization_command",
        _run_command_fails,
    )

    scenario = arrange_ready_summarization(root=tmp_path)

    session_dir = scenario.session_dir
    settings = AppSettings.from_env(env={}, home=tmp_path)
    exit_code = main(["summarize", str(session_dir)], settings=settings)
    captured = capsys.readouterr()

    # The adapter maps a workflow SummarizationError to exit code 1.
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
    assert "Markdown notes exported to:" in captured.out
    # AC4: provenance/contract detail must not clutter normal export output.
    assert "Export written to:" not in captured.out
    assert "Export contract:" not in captured.out
    assert "Export provenance:" not in captured.out
    export_line = next(
        line for line in captured.out.splitlines() if line.startswith("Markdown notes exported to:")
    )
    export_path = Path(export_line.removeprefix("Markdown notes exported to:").strip())
    assert export_path.exists()
    assert str(export_path).startswith(str(session_dir))


def test_main_export_command_format_flag_skips_confirmation_prompt(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    session_dir, store, merged_record = _build_verified_export_session_bundle(tmp_path)
    _persist_summary_artifact_for_cli(store=store, merged_transcript_record=merged_record)

    exit_code = main(["export", str(session_dir), "--format", "markdown"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Available export format:" not in captured.out
    assert "Markdown notes exported to:" in captured.out


def test_main_export_command_reports_when_no_summarized_sessions(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)

    exit_code = main(["export"], settings=settings, input_fn=lambda _prompt: "y")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "No summarized sessions were found." in captured.out
    assert "Markdown notes exported to:" not in captured.out


def test_main_export_command_defaults_to_latest_session_with_markdown_confirmation(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    session_dir, store, merged_record = _build_verified_export_session_bundle_at(
        settings.paths.sessions_dir / "session-2026-07-03"
    )
    _persist_summary_artifact_for_cli(store=store, merged_transcript_record=merged_record)

    exit_code = main(["export"], settings=settings, input_fn=lambda _prompt: "y")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Available export format:" in captured.out
    assert "Using latest summarized session:" in captured.out
    assert "Markdown notes exported to:" in captured.out
    export_line = next(
        line for line in captured.out.splitlines() if line.startswith("Markdown notes exported to:")
    )
    export_path = Path(export_line.removeprefix("Markdown notes exported to:").strip())
    assert export_path.exists()
    assert str(export_path).startswith(str(session_dir))


def test_main_export_command_honors_data_root_for_pathless_export(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    session_dir, store, merged_record = _build_verified_export_session_bundle_at(
        tmp_path / "sessions" / "session-2026-07-03"
    )
    _persist_summary_artifact_for_cli(store=store, merged_transcript_record=merged_record)

    exit_code = main(["export", "--format", "markdown"], data_root=tmp_path)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Using latest summarized session:" in captured.out
    assert "Markdown notes exported to:" in captured.out
    export_line = next(
        line for line in captured.out.splitlines() if line.startswith("Markdown notes exported to:")
    )
    export_path = Path(export_line.removeprefix("Markdown notes exported to:").strip())
    assert export_path.exists()
    assert str(export_path).startswith(str(session_dir))


def test_main_export_command_accepts_numbered_markdown_confirmation(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    session_dir, store, merged_record = _build_verified_export_session_bundle_at(
        settings.paths.sessions_dir / "session-2026-07-03"
    )
    _persist_summary_artifact_for_cli(store=store, merged_transcript_record=merged_record)

    exit_code = main(["export"], settings=settings, input_fn=lambda _prompt: "1")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Available export format:" in captured.out
    assert "Markdown notes exported to:" in captured.out
    export_line = next(
        line for line in captured.out.splitlines() if line.startswith("Markdown notes exported to:")
    )
    export_path = Path(export_line.removeprefix("Markdown notes exported to:").strip())
    assert str(export_path).startswith(str(session_dir))


def test_main_export_command_describes_explicit_summary_session_selection(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    older_dir, older_store, older_merged = _build_verified_export_session_bundle_at(
        settings.paths.sessions_dir / "session-older"
    )
    older_summary = _persist_summary_artifact_for_cli(
        store=older_store,
        merged_transcript_record=older_merged,
        summary_created_at="2026-05-20T10:00:00Z",
        summary_name="summary/older-final.json",
    )
    _, newer_store, newer_merged = _build_verified_export_session_bundle_at(
        settings.paths.sessions_dir / "session-newer"
    )
    _persist_summary_artifact_for_cli(
        store=newer_store,
        merged_transcript_record=newer_merged,
        summary_created_at="2026-06-15T10:00:00Z",
        summary_name="summary/newer-final.json",
    )

    exit_code = main(
        ["export", "--summary-artifact-id", older_summary.id],
        settings=settings,
        input_fn=lambda _prompt: "y",
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert f"Using session containing requested summary artifact: {older_dir}" in captured.out
    assert "Using latest summarized session:" not in captured.out
    export_line = next(
        line for line in captured.out.splitlines() if line.startswith("Markdown notes exported to:")
    )
    export_path = Path(export_line.removeprefix("Markdown notes exported to:").strip())
    assert str(export_path).startswith(str(older_dir))


def test_main_export_command_exact_id_with_malformed_session_record_fails_integrity(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    session_dir, store, merged_record = _build_verified_export_session_bundle_at(
        settings.paths.sessions_dir / "session-malformed-record"
    )
    summary = _persist_summary_artifact_for_cli(
        store=store,
        merged_transcript_record=merged_record,
    )
    _update_artifact_manifest_record(
        session_dir,
        artifact_id=summary.id,
        updates={"artifact_type": None},
    )
    session_record_path = session_dir / SESSION_RECORD_FILENAME
    session_record = json.loads(session_record_path.read_text(encoding="utf-8"))
    session_record.pop("id")
    session_record_path.write_text(json.dumps(session_record), encoding="utf-8")

    exit_code = main(
        ["export", "--summary-artifact-id", summary.id],
        settings=settings,
        input_fn=lambda _prompt: "y",
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Session bundle failed integrity verification" in captured.out
    assert "Summary artifact not found" not in captured.out


def test_main_export_command_exact_id_with_bad_manifest_session_id_fails_integrity(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    session_dir, store, merged_record = _build_verified_export_session_bundle_at(
        settings.paths.sessions_dir / "session-bad-summary-record"
    )
    summary = _persist_summary_artifact_for_cli(
        store=store,
        merged_transcript_record=merged_record,
    )
    _update_artifact_manifest_record(
        session_dir,
        artifact_id=summary.id,
        updates={"session_id": None},
    )

    exit_code = main(
        ["export", "--summary-artifact-id", summary.id],
        settings=settings,
        input_fn=lambda _prompt: "y",
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Session bundle failed integrity verification" in captured.out
    assert "artifact_record_invalid" in captured.out
    assert "Summary artifact not found" not in captured.out


def test_main_export_command_default_ignores_malformed_non_summary_record(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    valid_dir, valid_store, valid_merged = _build_verified_export_session_bundle_at(
        settings.paths.sessions_dir / "session-valid"
    )
    _persist_summary_artifact_for_cli(
        store=valid_store,
        merged_transcript_record=valid_merged,
        summary_created_at="2026-05-20T10:00:00Z",
    )
    malformed_dir = _create_session_record(
        session_dir=settings.paths.sessions_dir / "session-malformed-export"
    )
    _append_malformed_artifact_manifest_record(
        malformed_dir,
        artifact_type="summary_markdown_export",
        metadata={"summary_created_at": "2026-07-01T10:00:00Z"},
    )

    exit_code = main(["export"], settings=settings, input_fn=lambda _prompt: "y")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert f"Using latest summarized session: {valid_dir}" in captured.out
    assert "Session bundle failed integrity verification" not in captured.out


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


def test_main_export_command_declined_confirmation_cancels_cleanly(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    session_dir, store, merged_record = _build_verified_export_session_bundle(tmp_path)
    _persist_summary_artifact_for_cli(store=store, merged_transcript_record=merged_record)

    exit_code = main(["export", str(session_dir)], input_fn=lambda _prompt: "n")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Export cancelled." in captured.out
    assert "Cannot export session summary markdown" not in captured.out
    assert "Markdown notes exported to:" not in captured.out


def test_main_export_command_interrupt_during_confirmation_cancels_cleanly(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    session_dir, store, merged_record = _build_verified_export_session_bundle(tmp_path)
    _persist_summary_artifact_for_cli(store=store, merged_transcript_record=merged_record)

    def _interrupt(_prompt: str) -> str:
        raise EOFError

    exit_code = main(["export", str(session_dir)], input_fn=_interrupt)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Export cancelled." in captured.out
    assert "Cannot export session summary markdown" not in captured.out


def test_main_export_command_announces_default_session_before_format_prompt(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    _, store, merged_record = _build_verified_export_session_bundle_at(
        settings.paths.sessions_dir / "session-2026-07-03"
    )
    _persist_summary_artifact_for_cli(store=store, merged_transcript_record=merged_record)

    exit_code = main(["export"], settings=settings, input_fn=lambda _prompt: "y")
    captured = capsys.readouterr()

    assert exit_code == 0
    session_index = captured.out.index("Using latest summarized session:")
    format_index = captured.out.index("Available export format:")
    assert session_index < format_index


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


def test_main_inspect_command_succeeds_on_locked_session(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    session_dir = _create_session_record(tmp_path)

    with acquire_session_lock(session_dir=session_dir, command="process"):
        exit_code = main(["inspect", str(session_dir)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert f"Integrity report for {session_dir}: OK" in captured.out
    assert '"ok": true' in captured.out
    assert "orphaned_artifact_file" not in captured.out


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


def _create_named_runtime_session(
    settings: AppSettings,
    *,
    name: str | None,
    status: str,
    created_at: str,
) -> CreatedSession:
    session = create_session(
        profile=parse_capture_profile("online"),
        name=name,
        data_root=settings.paths.sessions_dir.parent,
        now=datetime.fromisoformat(created_at.replace("Z", "+00:00")),
    )
    persist_session_recording_lifecycle(
        session.record_path,
        status=status,
        recording={
            "started_at": "2026-05-19T09:59:00Z",
            "stopped_at": created_at,
            "stop_reason": "user_stop",
            "finalization": {
                "status": "finalized" if status == "recorded" else status,
                "completed_at": created_at,
                "error": None if status == "recorded" else "capture failed",
            },
        },
    )
    return session


def _snapshot_tree(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_main_list_prints_sessions_newest_first_with_six_fields(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    oldest = _create_named_runtime_session(
        settings, name=None, status="recorded", created_at="2026-05-19T10:00:00Z"
    )
    middle = _create_named_runtime_session(
        settings, name="Team sync", status="failed", created_at="2026-05-19T11:00:00Z"
    )
    newest = _create_named_runtime_session(
        settings, name="Roadmap review", status="recorded", created_at="2026-05-19T12:00:00Z"
    )

    exit_code = main(["list"], settings=settings)
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line]

    assert exit_code == 0
    assert len(lines) == 3
    # Newest created_at first; short ids appear in that order.
    assert lines[0].split(" | ")[1] == newest.record.id[:8]
    assert lines[1].split(" | ")[1] == middle.record.id[:8]
    assert lines[2].split(" | ")[1] == oldest.record.id[:8]
    # Six fields, in the documented order, for the newest session.
    fields = lines[0].split(" | ")
    assert fields == [
        "2026-05-19T12:00:00Z",
        newest.record.id[:8],
        "Roadmap review",
        "online_dual_stream",
        "recorded",
        str(newest.bundle_path),
    ]
    # A session without a name renders the Untitled placeholder.
    assert lines[2].split(" | ")[2] == "Untitled recording"


def test_main_list_prints_corrupt_bundle_as_degraded_line_and_exits_zero(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    healthy = _create_named_runtime_session(
        settings, name="Healthy", status="recorded", created_at="2026-05-19T12:00:00Z"
    )
    degraded = _create_named_runtime_session(
        settings, name="Broken", status="recorded", created_at="2026-05-19T10:00:00Z"
    )
    # Introduce an orphaned artifact file so integrity verification fails while
    # the session record itself stays readable and complete.
    stray = degraded.bundle_path / "artifacts" / "stray" / "stray.json"
    stray.parent.mkdir(parents=True)
    stray.write_text("{}", encoding="utf-8")

    exit_code = main(["list"], settings=settings)
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line]

    assert exit_code == 0
    assert len(lines) == 2
    healthy_line = next(line for line in lines if healthy.record.id[:8] in line)
    degraded_line = next(line for line in lines if degraded.record.id[:8] in line)
    assert "DEGRADED" not in healthy_line
    assert "DEGRADED" in degraded_line
    assert "orphaned_artifact_file" in degraded_line
    assert "Artifact file is not referenced by the manifest or capture metadata." in degraded_line
    assert str(degraded.bundle_path) in degraded_line


def test_main_list_prints_unreadable_session_record_as_degraded_line(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    unreadable = _create_named_runtime_session(
        settings,
        name="Broken record",
        status="recorded",
        created_at="2026-05-19T12:00:00Z",
    )
    unreadable.record_path.write_text("{not json", encoding="utf-8")

    exit_code = main(["list"], settings=settings)
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line]

    assert exit_code == 0
    assert len(lines) == 1
    fields = lines[0].split(" | ")
    assert fields[:6] == [
        "unknown",
        unreadable.bundle_path.name[:8],
        "Untitled recording",
        "unknown",
        "unknown",
        str(unreadable.bundle_path),
    ]
    assert "DEGRADED" in fields[6]
    assert "session_record_invalid" in fields[6]
    assert "session_record_unreadable" in fields[6]


def test_main_list_sorts_unparseable_timestamps_last_in_projection_order(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    parseable = _create_named_runtime_session(
        settings,
        name="Parseable",
        status="recorded",
        created_at="2026-05-19T10:00:00Z",
    )
    unparseable_a = _create_named_runtime_session(
        settings,
        name="Unparseable A",
        status="recorded",
        created_at="2026-05-19T11:00:00Z",
    )
    unparseable_b = _create_named_runtime_session(
        settings,
        name="Unparseable B",
        status="recorded",
        created_at="2026-05-19T12:00:00Z",
    )
    for session, created_at in (
        (unparseable_a, "not-a-date"),
        (unparseable_b, "also-not-a-date"),
    ):
        record = json.loads(session.record_path.read_text(encoding="utf-8"))
        record["created_at"] = created_at
        session.record_path.write_text(json.dumps(record), encoding="utf-8")
    expected_unparseable_order = [
        session.record.id[:8]
        for session in sorted(
            (unparseable_a, unparseable_b),
            key=lambda item: item.bundle_path,
        )
    ]

    exit_code = main(["list"], settings=settings)
    captured = capsys.readouterr()
    listed_ids = [line.split(" | ")[1] for line in captured.out.splitlines() if line]

    assert exit_code == 0
    assert listed_ids == [parseable.record.id[:8], *expected_unparseable_order]


def test_main_list_empty_root_prints_no_sessions_and_exits_zero(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)

    exit_code = main(["list"], settings=settings)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.out.strip() == "No sessions found."


def test_main_list_does_not_write_any_index_file(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    _create_named_runtime_session(
        settings, name="Only", status="recorded", created_at="2026-05-19T12:00:00Z"
    )
    data_root = settings.paths.sessions_dir.parent
    before = _snapshot_tree(data_root)

    exit_code = main(["list"], settings=settings)
    capsys.readouterr()

    after = _snapshot_tree(data_root)
    assert exit_code == 0
    assert before == after


def test_main_correct_handles_artifact_store_errors(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    session_dir = _create_session_record(tmp_path)
    manifest_path = session_artifact_manifest_path(session_dir)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps({"version": 999, "session_id": SESSION_ID, "artifacts": []}),
        encoding="utf-8",
    )

    # The correction workflow surfaces a real ArtifactStoreError, which the
    # adapter maps to exit code 1 with a "Cannot run correction workflow:" prefix.
    exit_code = main(["correct", str(session_dir)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Cannot run correction workflow: Unsupported artifact manifest version." in captured.out


def test_console_correction_interaction_reprompts_invalid_action(
    capsys: CaptureFixture[str],
) -> None:
    answers = iter(["z", "s"])
    interaction = ConsoleCorrectionInteraction(lambda _: next(answers))

    chosen = interaction.choose(
        ChoiceRequest(
            topic="correction.action",
            options=(
                ChoiceOption(option_id="playback", label="play"),
                ChoiceOption(option_id="skip", label="skip"),
            ),
        )
    )
    captured = capsys.readouterr()

    assert chosen.option_id == "skip"
    assert "Invalid action. Use [p]lay, [s]kip, [r]eassign, [t]ext, or [o]verlap." in captured.out


def test_console_correction_interaction_reads_speaker_text_and_validation_notices(
    capsys: CaptureFixture[str],
) -> None:
    prompts: list[str] = []

    def _input(prompt: str) -> str:
        prompts.append(prompt)
        return "2"

    interaction = ConsoleCorrectionInteraction(_input)

    answer = interaction.request_text(TextRequest(topic="correction.speaker_assignment"))
    interaction.notify(WorkflowNotice(topic="correction.speaker_assignment.invalid_number"))
    interaction.notify(WorkflowNotice(topic="correction.speaker_assignment.empty"))
    captured = capsys.readouterr()

    assert answer == "2"
    assert prompts == ["Choose existing speaker number or enter a new speaker label: "]
    assert "Choose a valid number from the list." in captured.out
    assert "Enter a speaker id or a label." in captured.out


def test_console_correction_interaction_reprompts_invalid_overlap(
    capsys: CaptureFixture[str],
) -> None:
    answers = iter(["maybe", "yes"])
    interaction = ConsoleCorrectionInteraction(lambda _: next(answers))

    assert interaction.confirm(ConfirmationRequest(topic="correction.overlap_mark")) is True
    captured = capsys.readouterr()

    assert "Please answer y or n." in captured.out


def test_console_correction_interaction_prints_playback_and_completion_notices(
    capsys: CaptureFixture[str],
) -> None:
    interaction = ConsoleCorrectionInteraction(lambda _: "s")

    interaction.notify(
        WorkflowNotice(
            topic="correction.playback_completed",
            context={"source_id": "system_audio", "start_seconds": 0.0, "end_seconds": 3.3},
        )
    )
    interaction.notify(
        WorkflowNotice(topic="correction.playback_failed", context={"error": "nope"})
    )
    interaction.notify(
        WorkflowNotice(
            topic="correction.completed",
            context={"reviewed": 1, "skipped": 1, "corrected": 0},
        )
    )
    captured = capsys.readouterr()

    assert "Playback completed for system_audio 0.00-3.30s." in captured.out
    assert "Playback failed: nope" in captured.out
    assert "Correction completed. reviewed=1, skipped=1, corrected=0." in captured.out


def test_main_correct_does_not_select_runtime_profile(
    tmp_path: Path,
) -> None:
    # A correctable session with an empty review queue: no runtime profile state
    # is persisted, so the correction command completing proves it never acquires
    # summarization runtime resources (which would require accepted profile state).
    session_dir, _, _ = _build_correction_session_bundle(tmp_path, flags=())

    assert main(["correct", str(session_dir)]) == 0


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


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("y", True),
        ("yes", True),
        ("", False),
        ("n", False),
        ("no", False),
        ("later", False),
    ],
)
def test_console_processing_interaction_preserves_runtime_fallback_prompt(
    answer: str,
    expected: bool,
    capsys: CaptureFixture[str],
) -> None:
    prompts: list[str] = []

    def _input(prompt: str) -> str:
        prompts.append(prompt)
        return answer

    interaction = ConsoleProcessingInteraction(_input)
    fallback_evaluation = SimpleNamespace(
        profile=SimpleNamespace(id="linux-alpha-local-cpu-fallback-v1"),
        selected_asr_profile=SimpleNamespace(checkpoint="small", device="cpu"),
    )

    assert (
        interaction.confirm(
            ConfirmationRequest(
                topic="runtime_fallback.one_run",
                context={
                    "accepted_failure": "CUDA out of memory",
                    "fallback_evaluation": fallback_evaluation,
                },
                default=False,
            )
        )
        is expected
    )

    captured = capsys.readouterr()
    assert prompts == ["Use local runtime fallback for this run? [y/N] "]
    assert "Accepted runtime profile is unavailable: CUDA out of memory" in captured.out
    assert (
        "One-run local runtime fallback is ready: "
        "linux-alpha-local-cpu-fallback-v1 using small/cpu."
    ) in captured.out


def test_console_processing_interaction_rejects_unsupported_confirmation_topic() -> None:
    def _input(_prompt: str) -> str:
        raise AssertionError("not called")

    interaction = ConsoleProcessingInteraction(_input)

    with pytest.raises(
        ValueError,
        match="Unsupported processing confirmation topic 'record.capture_route.microphone'",
    ):
        interaction.confirm(ConfirmationRequest(topic="record.capture_route.microphone"))


def test_main_process_delegates_explicit_session_to_processing_workflow(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    session = _build_processing_session(tmp_path)
    processing = _ProcessingCommandSpy()

    exit_code = main(
        ["process", str(session.record_path)],
        run_id="manual-run-001",
        processing_command_runner=processing,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert processing.session_paths == [session.record_path]
    assert processing.calls[0]["run_id"] == "manual-run-001"
    assert processing.calls[0]["attempt"] == 1
    assert "Processing completed successfully." in captured.out


def test_main_process_without_path_delegates_latest_session_to_processing_workflow(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    session = _build_processing_session(settings.paths.sessions_dir.parent)
    persist_session_recording_lifecycle(
        session.record_path,
        status="recorded",
        recording={
            "started_at": "2026-05-19T09:59:00Z",
            "stopped_at": "2026-05-19T10:00:00Z",
            "stop_reason": "user_stop",
            "finalization": {
                "status": "finalized",
                "completed_at": "2026-05-19T10:00:00Z",
                "error": None,
            },
        },
    )
    processing = _ProcessingCommandSpy(
        side_effect=lambda _kwargs: _mark_processing_complete(session)
    )

    exit_code = main(
        ["process"],
        settings=settings,
        run_id="manual-run-pathless",
        processing_command_runner=processing,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert processing.session_paths == [session.record_path]
    assert processing.calls[0]["run_id"] == "manual-run-pathless"
    assert f"Processing recording: {session.record_path}" in captured.out
    assert "Processing completed successfully." in captured.out
    assert f"Next command: mena correct {session.record_path}" in captured.out


def test_resolve_console_input_guards_non_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explicit(_prompt: str) -> str:
        return "y"

    # An explicit input function is always honored verbatim.
    assert _resolve_console_input(explicit) is explicit
    # On a real TTY, the builtin input prompt is used.
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
    assert _resolve_console_input(None) is input
    # On a non-TTY stdin, no prompt is wired so the fallback raises an actionable
    # error instead of input() raising EOFError.
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: False))
    assert _resolve_console_input(None) is None


def test_main_process_maps_unauthorized_runtime_fallback_to_exit_3(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    session = _build_processing_session(tmp_path)
    settings = AppSettings.from_env(env={}, home=tmp_path)

    def _raise_unavailable(**_kwargs: object) -> int:
        raise RuntimeFallbackUnavailable(
            "Runtime stage 'diarization' failed: CUDA out of memory. Re-run setup to "
            "authorize local fallback or pass --allow-runtime-fallback for an explicit "
            "one-run fallback when supported."
        )

    exit_code = main(
        ["process", str(session.record_path)],
        settings=settings,
        processing_command_runner=_raise_unavailable,
    )
    captured = capsys.readouterr()

    assert exit_code == 3
    assert "Cannot run processing workflow" in captured.out
    assert "--allow-runtime-fallback" in captured.out


def test_console_processing_interaction_renders_runtime_stewardship_release_notice(
    capsys: CaptureFixture[str],
) -> None:
    interaction = ConsoleProcessingInteraction(None)
    release = RuntimeReleaseResult(
        lease=RuntimeLease(
            resource_kind="local_llm",
            stage="semantic-review",
            boundary="process",
            provider="ollama",
            model="llama3.1:8b",
        ),
        status="released",
        detail="Ollama model release completed: unloaded.",
    )

    interaction.notify(
        WorkflowNotice(
            topic=RUNTIME_STEWARDSHIP_RELEASE_NOTICE_TOPIC,
            context={"result": release},
        )
    )
    captured = capsys.readouterr()

    assert "Runtime stewardship: Ollama model release completed: unloaded." in captured.out


def test_console_processing_interaction_renders_runtime_stewardship_release_warning(
    capsys: CaptureFixture[str],
) -> None:
    interaction = ConsoleProcessingInteraction(None)
    release = RuntimeReleaseResult(
        lease=RuntimeLease(
            resource_kind="local_llm",
            stage="semantic-review",
            boundary="process",
            provider="ollama",
            model="llama3.1:8b",
        ),
        status="failed",
        detail="release failed",
    )

    interaction.notify(
        WorkflowNotice(
            topic=RUNTIME_STEWARDSHIP_RELEASE_NOTICE_TOPIC,
            context={"result": release},
        )
    )
    captured = capsys.readouterr()

    assert "Runtime stewardship warning: release failed" in captured.out


def test_main_resume_runs_next_attempt_through_locked_workflow(
    tmp_path: Path,
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

    assert _main_process_with_fake_providers(["resume", str(session.record_path)]) == 0

    _assert_processing_attempt(
        session,
        run_id=failed_state.run_id,
        attempt=failed_state.attempt + 1,
    )


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
    exit_code = _main_process_with_fake_providers(["resume", str(session.record_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    _assert_processing_attempt(
        session,
        run_id=failed_state.run_id,
        attempt=failed_state.attempt + 1,
    )
    assert "Processing completed successfully." not in captured.out


def test_main_resume_retries_interrupted_running_state(
    tmp_path: Path,
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
    exit_code = _main_process_with_fake_providers(["resume", str(session.record_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    _assert_processing_attempt(
        session,
        run_id=running_state.run_id,
        attempt=running_state.attempt + 1,
    )
    assert "Processing completed successfully." not in captured.out


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


def test_main_resume_reports_lock_contention_before_retry_lookup(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    session = create_session(profile=parse_capture_profile("online"), data_root=tmp_path)

    with acquire_session_lock(session_dir=session.bundle_path, command="process"):
        exit_code = main(["resume", str(session.record_path)])

    captured = capsys.readouterr()
    assert exit_code == 4
    assert "pid=" in captured.out
    assert "command=process" in captured.out
    assert "No retryable processing failure found to resume." not in captured.out


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
    exit_code = _main_process_with_fake_providers(["resume", str(session.record_path)])

    assert exit_code == 0
    _assert_processing_attempt(
        session,
        run_id=completed_state.run_id,
        attempt=completed_state.attempt + 1,
    )


def test_main_resume_after_mid_run_skipped_checkpoint_continues(
    tmp_path: Path,
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
    exit_code = _main_process_with_fake_providers(["resume", str(session.record_path)])

    assert exit_code == 0
    _assert_processing_attempt(
        session,
        run_id=skipped_state.run_id,
        attempt=skipped_state.attempt + 1,
    )


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
    exit_code = main(["resume", str(session.record_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "No retryable processing failure found to resume." in captured.out


def test_main_resume_uses_durable_checkpoint_order_when_timestamps_tie(
    tmp_path: Path,
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
    exit_code = _main_process_with_fake_providers(["resume", str(session.record_path)])

    assert exit_code == 0
    _assert_processing_attempt(
        session,
        run_id=newer_state.run_id,
        attempt=newer_state.attempt + 1,
    )


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


def _build_verified_export_session_bundle(
    tmp_path: Path,
) -> tuple[Path, FileSystemArtifactStore, ArtifactRecord]:
    return _build_verified_export_session_bundle_at(tmp_path / "session")


def _build_verified_export_session_bundle_at(
    session_dir: Path,
) -> tuple[Path, FileSystemArtifactStore, ArtifactRecord]:
    session_dir = _create_session_record(session_dir=session_dir)
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


def _create_session_record(
    tmp_path: Path | None = None,
    *,
    session_dir: Path | None = None,
) -> Path:
    if tmp_path is not None and session_dir is not None:
        raise ValueError("Provide either tmp_path or session_dir, not both.")
    if session_dir is None:
        if tmp_path is None:
            raise ValueError("Provide either tmp_path or session_dir.")
        session_dir = tmp_path / "session"

    session_dir.mkdir(parents=True)
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


def _create_runtime_session(
    settings: AppSettings,
    *,
    status: str,
    created_at: str,
    completed_at: str | None = None,
) -> CreatedSession:
    final_completed_at = completed_at or created_at
    session = create_session(
        profile=parse_capture_profile("online"),
        data_root=settings.paths.sessions_dir.parent,
        now=datetime.fromisoformat(created_at.replace("Z", "+00:00")),
    )
    persist_session_recording_lifecycle(
        session.record_path,
        status=status,
        recording={
            "started_at": "2026-05-19T09:59:00Z",
            "stopped_at": final_completed_at,
            "stop_reason": "user_stop",
            "finalization": {
                "status": "finalized" if status == "recorded" else status,
                "completed_at": final_completed_at,
                "error": None if status == "recorded" else "capture failed",
            },
        },
    )
    return session


def _mark_processing_complete(session: CreatedSession) -> None:
    record = json.loads(session.record_path.read_text(encoding="utf-8"))
    store = FileSystemArtifactStore(
        session_id=cast(str, record["id"]),
        session_dir=session.bundle_path,
    )
    output = store.store(
        artifact_type="semantic_review",
        name="semantic-review.json",
        data=b"{}",
        version=1,
        metadata={},
    )
    ledger = ProcessingStageLedger(
        store=store,
        session_id=cast(str, record["id"]),
        run_id="completed-process-run",
    )
    ledger.mark_completed(
        stage="semantic_coherence",
        input_artifacts=(),
        output_artifacts=(ArtifactRef.from_record(output),),
        attempt=1,
        now=datetime(2026, 5, 19, 13, 0, 0, tzinfo=UTC),
    )


def _mark_processing_failed(session: CreatedSession) -> None:
    record = json.loads(session.record_path.read_text(encoding="utf-8"))
    store = FileSystemArtifactStore(
        session_id=cast(str, record["id"]),
        session_dir=session.bundle_path,
    )
    ledger = ProcessingStageLedger(
        store=store,
        session_id=cast(str, record["id"]),
        run_id="failed-process-run",
    )
    ledger.mark_failed(
        stage="semantic_coherence",
        input_artifacts=(),
        error=ProcessingErrorInfo(message="processing failed", retryable=False),
        attempt=1,
        now=datetime(2026, 5, 19, 13, 0, 0, tzinfo=UTC),
    )


def _mark_semantic_provider_unavailable_skip(session: CreatedSession) -> None:
    record = json.loads(session.record_path.read_text(encoding="utf-8"))
    store = FileSystemArtifactStore(
        session_id=cast(str, record["id"]),
        session_dir=session.bundle_path,
    )
    ledger = ProcessingStageLedger(
        store=store,
        session_id=cast(str, record["id"]),
        run_id="skipped-process-run",
    )
    now = datetime(2026, 5, 19, 13, 0, 0, tzinfo=UTC)
    timestamp = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    ledger.record(
        ProcessingStageState(
            version=1,
            session_id=cast(str, record["id"]),
            run_id="skipped-process-run",
            stage="semantic_coherence",
            status="skipped",
            input_artifacts=(),
            output_artifacts=(),
            started_at=timestamp,
            completed_at=timestamp,
            error=None,
            attempt=1,
            skip_reason="provider_unavailable",
        )
    )


def _summary_result_for_export() -> SummaryResult:
    return SummaryResult(
        timeline=(
            SummaryTimelineEntry(
                text="Agenda covered.",
                source_segment_ids=("merged_segment_000001",),
                start_seconds=0.0,
            ),
        ),
        action_items=(),
        general_summary="The team aligned on the plan.",
        request_records=_request_records_for_products(REQUIRED_NOTE_PRODUCTS),
    )


def _persist_summary_artifact_for_cli(
    *,
    store: FileSystemArtifactStore,
    merged_transcript_record: ArtifactRecord | None,
    summary_created_at: str = "2026-05-20T10:00:00Z",
    summary_name: str = "summary/final.json",
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
            version=SUMMARY_SCHEMA_VERSION,
            session_id=SESSION_ID,
            merged_transcript_artifact=merged_artifact_ref,
            manual_notes_artifact=None,
            policy=SummarizationPolicy(language="en").to_dict(),
            provider=FakeSummarizationProvider(result=summary_result).metadata,
            timeline=summary_result.timeline,
            action_items=summary_result.action_items,
            general_summary=summary_result.general_summary,
            created_at=summary_created_at,
            request_records=summary_result.request_records,
        ),
        name=summary_name,
        correction_overlay_record=None,
    )


def _append_malformed_artifact_manifest_record(
    session_dir: Path,
    *,
    artifact_type: object = "transcript",
    metadata: Mapping[str, object] | None = None,
) -> None:
    manifest_path = session_artifact_manifest_path(session_dir)
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        session_record = json.loads((session_dir / SESSION_RECORD_FILENAME).read_text("utf-8"))
        manifest = {
            "version": 1,
            "session_id": session_record["id"],
            "artifacts": [],
        }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest["artifacts"].append(
        {
            "artifact_type": artifact_type,
            "path": "artifacts/transcript/malformed.json",
            "status": "sealed",
            "metadata": dict(metadata or {}),
        }
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _update_artifact_manifest_record(
    session_dir: Path,
    *,
    artifact_id: str,
    updates: Mapping[str, object] | None = None,
    metadata_updates: Mapping[str, object | None] | None = None,
) -> None:
    manifest_path = session_artifact_manifest_path(session_dir)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for item in manifest["artifacts"]:
        if item["id"] != artifact_id:
            continue
        if updates is not None:
            item.update(updates)
        if metadata_updates is not None:
            metadata = item.setdefault("metadata", {})
            for key, value in metadata_updates.items():
                if value is None:
                    metadata.pop(key, None)
                else:
                    metadata[key] = value
        break
    else:
        raise AssertionError(f"Artifact record not found: {artifact_id}")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _request_records_for_products(products: tuple[str, ...]) -> tuple[ProductRequestRecord, ...]:
    return tuple(
        ProductRequestRecord(
            product=product,
            model="fake-summarizer",
            num_ctx=OLLAMA_SUMMARIZATION_NUM_CTX,
            temperature=0,
            seed=OLLAMA_SUMMARIZATION_SEED,
            prompt_eval_count=None,
            eval_count=None,
        )
        for product in products
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


def _processing_state_payloads(session: CreatedSession) -> list[dict[str, object]]:
    store = FileSystemArtifactStore(session_id=session.record.id, session_dir=session.bundle_path)
    return [
        json.loads(store.retrieve(record.id))
        for record in store.list(artifact_type=PROCESSING_STATE_ARTIFACT_TYPE)
    ]


def _completed_processing_payloads(session: CreatedSession) -> list[dict[str, object]]:
    return [
        payload
        for payload in _processing_state_payloads(session)
        if payload["status"] == "completed"
    ]


def _assert_processing_attempt(
    session: CreatedSession,
    *,
    run_id: str,
    attempt: int,
) -> None:
    assert any(
        payload["run_id"] == run_id and payload["attempt"] == attempt
        for payload in _processing_state_payloads(session)
    )


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


def _ambiguous_default_discoverer(profile: CaptureProfileId) -> AudioDiscovery:
    discovery = _ambiguous_discoverer(profile)
    return replace(
        discovery,
        sources=(
            discovery.sources[0],
            replace(discovery.sources[1], is_default=True),
        ),
        sinks=(
            discovery.sinks[0],
            replace(discovery.sinks[1], is_default=True),
        ),
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


class _ProcessingCommandSpy:
    def __init__(
        self,
        *,
        result: int = 0,
        side_effect: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.result = result
        self.side_effect = side_effect
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> int:
        self.calls.append(dict(kwargs))
        if self.side_effect is not None:
            self.side_effect(self.calls[-1])
        return self.result

    @property
    def session_paths(self) -> list[Path]:
        return [cast(Path, call["session_path"]) for call in self.calls]


def _main_process_with_fake_providers(argv: Sequence[str]) -> int:
    return main(
        argv,
        transcription_provider_factory=FakeTranscriptionProvider,
        diarization_provider_factory=FakeDiarizationProvider,
        semantic_coherence_provider_factory=lambda: FakeSemanticCoherenceProvider(results={}),
    )


def _mistral_ready_summary_state() -> MistralSummaryState:
    return MistralSummaryState(
        key_present=True,
        reachable=True,
        request_model="mistral-small-latest",
        resolved_model="mistral-small-2603",
        model_allowed=True,
        policy="mena-mistral-summary-allowlist-2026-06-29-v1",
        detail=(
            "Request model 'mistral-small-latest' resolved to 'mistral-small-2603' "
            "inside the accepted snapshot."
        ),
    )


def _mistral_aware_detection(
    settings: AppSettings,
) -> Callable[..., LocalInstallationState]:
    base = _setup_state(settings=settings, pyannote_token_present=True)

    def _detect(_settings: AppSettings, **kwargs: object) -> LocalInstallationState:
        profile = kwargs.get("profile")
        stages = getattr(profile, "stages", ())
        has_mistral = any(
            stage.stage == "summarization" and stage.provider == "Mistral" for stage in stages
        )
        if has_mistral:
            return replace(base, mistral_summary=_mistral_ready_summary_state())
        return base

    return _detect


def test_setup_mistral_primary_prompts_disclosure_and_persists_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    monkeypatch.setattr(
        "mena.setup_detection.detect_local_installation_state",
        _mistral_aware_detection(settings),
    )
    prompts: list[str] = []

    def _answer(prompt: str) -> str:
        prompts.append(prompt)
        return "y"

    exit_code = main(
        ["setup", "--summarization-provider", "mistral"],
        settings=settings,
        input_fn=_answer,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert any("Accept the Mistral summarization disclosure" in prompt for prompt in prompts)
    assert "sent to your configured Mistral account" in captured.out
    assert "Accepted summarization primary: Mistral (mistral-small-latest)" in captured.out
    accepted = load_runtime_profile_state(settings.paths)
    assert accepted.accepted_profile_id == "linux-alpha-mistral-summary-v1"
    assert accepted.summarization_fallback_providers == ()


def test_setup_mistral_primary_declined_disclosure_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    monkeypatch.setattr(
        "mena.setup_detection.detect_local_installation_state",
        _mistral_aware_detection(settings),
    )

    exit_code = main(
        ["setup", "--summarization-provider", "mistral"],
        settings=settings,
        input_fn=lambda _prompt: "n",
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Mistral summarization was declined" in captured.out
    assert not (settings.paths.state_home / "setup-state.json").exists()


def test_setup_mistral_fallback_flag_persists_the_ordered_fallback_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    monkeypatch.setattr(
        "mena.setup_detection.detect_local_installation_state",
        _mistral_aware_detection(settings),
    )

    exit_code = main(
        [
            "setup",
            "--authorize-mistral-summary-fallback",
            "--accept-mistral-summary-disclosure",
        ],
        settings=settings,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "disclosure accepted via explicit setup option" in captured.out
    assert "Accepted summarization fallback order: Mistral (mistral-small-latest)" in captured.out
    accepted = load_runtime_profile_state(settings.paths)
    assert accepted.accepted_profile_id == "linux-alpha-local-v1"
    assert [entry.provider for entry in accepted.summarization_fallback_providers] == ["Mistral"]
    assert accepted.fallback_authorization.remote_status == "authorized"
    assert accepted.fallback_authorization.remote_stage_scope == ("summarization",)


def test_main_summarize_exits_3_when_summarization_fallback_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    scenario = arrange_ready_summarization(root=tmp_path)
    session_dir = scenario.session_dir
    settings = AppSettings.from_env(env={}, home=tmp_path)

    def _raise_unavailable(**_kwargs: object) -> int:
        raise RuntimeFallbackUnavailable(
            "Accepted summarization provider is unavailable and the accepted ordered "
            "fallback-provider list is empty: ollama is down"
        )

    monkeypatch.setattr(
        "mena.summarize_workflow.run_summarization_command",
        _raise_unavailable,
    )

    exit_code = main(["summarize", str(session_dir)], settings=settings)

    # A recoverable RuntimeFallbackUnavailable maps to the exit-code-3 contract.
    captured = capsys.readouterr()
    assert exit_code == 3
    assert "Cannot summarize session:" in captured.out
    assert "fallback-provider list is empty" in captured.out


def test_console_summarize_interaction_renders_completion_handoff(
    capsys: CaptureFixture[str],
) -> None:
    interaction = summarize_command.ConsoleSummarizeInteraction(None)

    interaction.notify(
        WorkflowNotice(
            topic="summarize.completed",
            context={
                "summary_path": "artifacts/summary/summary/final.json",
                "session_dir": Path("/data/session"),
                "summary_artifact_id": "sid:artifacts/summary/final.json",
            },
        )
    )

    captured = capsys.readouterr()
    assert "Notes created: artifacts/summary/summary/final.json" in captured.out
    assert "Session path: /data/session" in captured.out
    assert (
        "Next command: mena export --summary-artifact-id "
        "sid:artifacts/summary/final.json /data/session"
    ) in captured.out


def test_console_summarize_interaction_renders_runtime_stewardship_release_notice(
    capsys: CaptureFixture[str],
) -> None:
    interaction = summarize_command.ConsoleSummarizeInteraction(None)
    release = RuntimeReleaseResult(
        lease=RuntimeLease(
            resource_kind="local_llm",
            stage="summarization",
            boundary="summarize",
            provider="Ollama",
            model="llama3.1:8b",
        ),
        status="released",
        detail="Ollama model release completed: unloaded.",
    )

    interaction.notify(
        WorkflowNotice(
            topic=RUNTIME_STEWARDSHIP_RELEASE_NOTICE_TOPIC,
            context={"result": release},
        )
    )

    captured = capsys.readouterr()
    assert "Runtime stewardship: Ollama model release completed: unloaded." in captured.out


def test_console_summarize_interaction_renders_runtime_stewardship_release_warning(
    capsys: CaptureFixture[str],
) -> None:
    interaction = summarize_command.ConsoleSummarizeInteraction(None)
    release = RuntimeReleaseResult(
        lease=RuntimeLease(
            resource_kind="local_llm",
            stage="summarization",
            boundary="summarize",
            provider="Ollama",
            model="llama3.1:8b",
        ),
        status="failed",
        detail="release failed",
    )

    interaction.notify(
        WorkflowNotice(
            topic=RUNTIME_STEWARDSHIP_RELEASE_NOTICE_TOPIC,
            context={"result": release},
        )
    )

    captured = capsys.readouterr()
    assert "Runtime stewardship warning: release failed" in captured.out


def test_console_summarize_interaction_confirms_remote_fallback(
    capsys: CaptureFixture[str],
) -> None:
    answers = iter(["y"])
    interaction = summarize_command.ConsoleSummarizeInteraction(lambda _prompt: next(answers))

    accepted = interaction.confirm(
        ConfirmationRequest(
            topic="runtime_fallback.summarization_remote",
            context={
                "reason": "ollama is down",
                "disclosure": ("Text goes to your Mistral account.",),
            },
        )
    )

    captured = capsys.readouterr()
    assert accepted is True
    assert "ollama is down" in captured.out
    assert "Text goes to your Mistral account." in captured.out


def test_console_summarize_interaction_declines_remote_fallback_non_interactively(
    capsys: CaptureFixture[str],
) -> None:
    interaction = summarize_command.ConsoleSummarizeInteraction(None)

    accepted = interaction.confirm(
        ConfirmationRequest(
            topic="runtime_fallback.summarization_remote",
            context={"reason": "ollama is down", "disclosure": ()},
        )
    )

    captured = capsys.readouterr()
    assert accepted is False
    assert "--authorize-mistral-summary-fallback" in captured.out


def test_console_summarize_interaction_rejects_unknown_confirmation_topics() -> None:
    interaction = summarize_command.ConsoleSummarizeInteraction(lambda _prompt: "y")

    with pytest.raises(ValueError, match="Unsupported summarize confirmation topic"):
        interaction.confirm(ConfirmationRequest(topic="summarize.other"))


def test_cli_setup_interaction_renders_the_disclosure_before_prompting(
    capsys: CaptureFixture[str],
) -> None:
    prompts: list[str] = []

    def _answer(prompt: str) -> str:
        # The disclosure must already be on screen when the prompt runs;
        # buffered workflow events are only printed after the workflow ends.
        captured = capsys.readouterr()
        assert "Mistral summarization disclosure:" in captured.out
        assert "special-category" in captured.out
        prompts.append(prompt)
        return "y"

    interaction = setup_command._CliSetupInteraction(input_fn=_answer)
    accepted = interaction.confirm(
        ConfirmationRequest(
            topic="setup.mistral_summary_disclosure",
            context={"role": "primary", "disclosure": MISTRAL_SUMMARY_DISCLOSURE},
        )
    )

    assert accepted is True
    assert "Accept the Mistral summarization disclosure above" in prompts[0]
