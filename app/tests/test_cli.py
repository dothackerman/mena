import hashlib
import json
from collections.abc import Sequence
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
from mena.cli import (
    COMMANDS,
    WORKER_EXIT_TIMEOUT_SECONDS,
    SourceAudioInput,
    _collect_capture_segments,
    _require_provider,
    _resolve_latest_merged_transcript_record,
    _run_processing_command,
    _run_processing_stages,
    main,
)
from mena.correction_overlay import (
    CorrectionEntry,
    create_correction_overlay,
    persist_correction_overlay_artifact,
)
from mena.correction_workflow import PlaybackRequest
from mena.diarization import DiarizationProvider, FakeDiarizationProvider
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
from mena.protocol import ProtocolError, ProtocolMessage
from mena.review_flagging import (
    REVIEW_QUEUE_SCHEMA_VERSION,
    ConfidenceFlaggingPolicy,
    ReviewFlag,
    ReviewQueueArtifact,
    persist_review_queue_artifact,
)
from mena.semantic_coherence import FakeSemanticCoherenceProvider, SemanticCoherenceProvider
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
    get_alpha_local_profile,
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
    FakeTranscriptionProvider,
    TranscriptArtifact,
    TranscriptionProvider,
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
    runner = SetupFakeRunner()

    def _detect_state(_settings: AppSettings, **_kwargs: object) -> LocalInstallationState:
        return state

    monkeypatch.setattr("mena.cli.detect_local_installation_state", _detect_state)

    exit_code = main(["setup", "--yes"], settings=settings, setup_runner=runner)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert ("ollama", "pull", "llama3.2:3b") in runner.run_calls
    assert any(call[1] == "-c" and "faster_whisper" in call[2] for call in runner.run_calls)
    assert any(call[1] == "-c" and "pyannote.audio" in call[2] for call in runner.run_calls)
    assert "Setup action results:" in captured.out
    assert captured.out.rstrip().endswith("Local provider readiness: complete")


def test_record_help_lists_profile_options(capsys: CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["record", "--help"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 0
    assert "--profile {online,in-person}" in captured.out
    assert "--name NAME" in captured.out
    assert "--dry-run" in captured.out


def test_record_creates_session_bundle_and_record(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    exit_code = main(
        ["record", "--profile", "online", "--name", "Planning"],
        data_root=tmp_path,
        discoverer=_available_discoverer,
        worker_factory=FakeWorkerProcess,
    )

    captured = capsys.readouterr()
    sessions = list((tmp_path / "sessions").iterdir())
    assert exit_code == 0
    assert "Created session" in captured.out
    assert len(sessions) == 1

    record_path = sessions[0] / "session.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["version"] == 1
    assert record["id"]
    assert record["created_at"].endswith("Z")
    assert record["name"] == "Planning"
    assert record["status"] == "created"
    assert record["profile"] == "online_dual_stream"
    assert record["discovery"]["status"] == "available"
    assert record["capture_metadata"]["segment_count"] == 0


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


def test_record_requires_profile(capsys: CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["record"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "the following arguments are required: --profile" in captured.err


def test_record_rejects_invalid_profile(capsys: CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["record", "--profile", "room"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "invalid choice: 'room'" in captured.err
    assert "online" in captured.err
    assert "in-person" in captured.err


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
        ["record", "--profile", "online"],
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
        ["record", "--profile", "online"],
        data_root=tmp_path,
        discoverer=_available_discoverer,
        worker_factory=FailingWorkerProcess,
    )

    captured = capsys.readouterr()
    record_path = next((tmp_path / "sessions").iterdir()) / "session.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert "Capture failed: worker refused handshake" in captured.out
    assert record["discovery"]["status"] == "available"
    assert "capture_metadata" not in record


def test_record_rejects_nonzero_worker_exit_without_capture_metadata(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    exit_code = main(
        ["record", "--profile", "online"],
        data_root=tmp_path,
        discoverer=_available_discoverer,
        worker_factory=NonzeroExitWorkerProcess,
    )

    captured = capsys.readouterr()
    record_path = next((tmp_path / "sessions").iterdir()) / "session.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert "Capture failed: Worker exited with status 3." in captured.out
    assert "capture_metadata" not in record


def test_record_uses_configurable_worker_exit_timeout(tmp_path: Path) -> None:
    TimeoutCapturingWorkerProcess.last_timeout = None

    exit_code = main(
        ["record", "--profile", "online", "--worker-exit-timeout", "7.5"],
        data_root=tmp_path,
        discoverer=_available_discoverer,
        worker_factory=TimeoutCapturingWorkerProcess,
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

    exit_code = main(
        [
            "summarize",
            str(session_dir),
            "--manual-notes",
            str(notes_path),
            "--language",
            "de-CH",
        ]
    )
    captured_output = capsys.readouterr()

    assert exit_code == 0
    assert captured.get("merged") == merged_record
    assert captured.get("overlay") == overlay_record
    assert captured.get("manual") is not None
    assert captured.get("language") == "de-CH"
    assert "Summary written to:" in captured_output.out


def test_main_summarize_uses_configured_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_dir, _, _, _ = _build_summarize_session_bundle(tmp_path)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[summary]
language = "fr-CH"

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

    exit_code = main(["summarize", str(session_dir)], config_path=config_path)

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

    exit_code = main(
        ["summarize", str(session_dir), "--language", "it-CH"],
        config_path=config_path,
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


def test_main_process_uses_configured_ollama_defaults_for_semantic_provider(
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
    assert captured["model"] == "semantic-model:latest"
    assert captured["endpoint"] == "http://127.0.0.1:11436"


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

    exit_code = main(["summarize", str(session_json_path)])

    assert exit_code == 0


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
    exit_code = main(["summarize", str(session_dir)])
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

    exit_code = main(
        ["correct", str(session_dir)],
        input_fn=lambda _: "p",
        playback_player=lambda _: None,
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Cannot run correction workflow:" in captured.out
    assert "missing transcript artifact" in captured.out


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
        attempt: int = 1,
        now: object = None,
    ) -> object:
        calls.append(f"transcription:{source_id}")
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
    assert "transcription:local_mic" in calls
    assert "transcription:system_audio" in calls
    assert calls.count("diarization") == 2
    assert "merge" in calls
    assert "confidence" in calls
    assert "semantic" in calls


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


class FakeWorkerProcess:
    def __init__(self, *, session_id: str, session_dir: Path) -> None:
        self.session_id = session_id
        self.session_dir = session_dir

    def __enter__(self) -> "FakeWorkerProcess":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def handshake(self) -> ProtocolMessage:
        return ProtocolMessage(type="hello_ack", session_id=self.session_id)

    def start_capture(self, payload: dict[str, object] | None = None) -> ProtocolMessage:
        return ProtocolMessage(
            type="capture_started",
            session_id=self.session_id,
            payload={
                "session_dir": str(self.session_dir),
                "profile": payload["profile"] if payload else None,
            },
        )

    def stop_capture(self) -> ProtocolMessage:
        return ProtocolMessage(
            type="capture_finalized",
            session_id=self.session_id,
            payload={
                "version": 1,
                "sample_rate": 48000,
                "channels": 1,
                "segment_seconds": 30,
                "segment_count": 0,
                "segments": [],
            },
        )

    def wait(self, timeout: float | None = None) -> int:
        return 0


class FailingWorkerProcess(FakeWorkerProcess):
    def handshake(self) -> ProtocolMessage:
        raise ProtocolError("worker refused handshake")


class NonzeroExitWorkerProcess(FakeWorkerProcess):
    def wait(self, timeout: float | None = None) -> int:
        return 3


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
    ) -> None:
        invocation["session_id"] = session_id
        invocation["run_id"] = getattr(ledger, "run_id", None)
        invocation["attempt"] = attempt
        invocation["source_input_count"] = len(source_inputs)
        invocation["providers"] = (
            transcription_provider,
            diarization_provider,
            semantic_coherence_provider,
        )
        invocation["profile"] = profile.name.value if hasattr(profile, "name") else profile

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
                        }
                    ]
                }
            },
            ("local_mic", "system_audio"),
        )


def test_collect_capture_segments_rejects_multiple_segments_per_source() -> None:
    with pytest.raises(ValueError, match="exactly one segment per source"):
        _collect_capture_segments(
            {
                "capture_metadata": {
                    "segments": [
                        {
                            "source_id": "local_mic",
                            "index": 1,
                            "path": "local_mic_segment.wav",
                        },
                        {
                            "source_id": "local_mic",
                            "index": 2,
                            "path": "local_mic_segment_2.wav",
                        },
                    ]
                }
            },
            ("local_mic",),
        )


def test_run_processing_stages_routes_in_person_room_mic_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = get_capture_profile(parse_capture_profile("in-person").id)
    store = FileSystemArtifactStore(session_id="session-room", session_dir=tmp_path)
    ledger = ProcessingStageLedger(session_id="session-room", run_id="run-room", store=store)
    calls: list[str] = []

    def _fake_run_transcription_stage(*, source_id: str | None = None, **_: object) -> object:
        calls.append(f"transcription:{source_id}")
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
    )

    assert calls == [
        "transcription:room_mic",
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

    exit_code = main(
        [
            "process",
            str(session.record_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Processing completed for run '" in captured.out


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

    first_exit_code = main(
        [
            "process",
            str(session.record_path),
        ]
    )
    second_exit_code = main(
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

    first_exit_code = main(["process", str(session.bundle_path.relative_to(tmp_path))])
    second_exit_code = main(["process", str(session.bundle_path)])

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

    first_exit_code = main(["process", str(session.record_path)])
    relocated_root = tmp_path / "relocated"
    relocated_root.mkdir()
    relocated_bundle = relocated_root / session.bundle_path.name
    session.bundle_path.rename(relocated_bundle)
    second_exit_code = main(["process", str(relocated_bundle / "session.json")])

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

    first_exit_code = main(["process", str(session.record_path)])
    source_artifact_path = (
        session.bundle_path / "artifacts/source_audio/source_audio/local_mic_000001.wav"
    )
    source_artifact_path.write_bytes(b"truncated")
    second_exit_code = main(["process", str(session.record_path)])

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

    first_exit_code = main(["process", str(session.record_path)])
    source_artifact_dir = session.bundle_path / "artifacts/source_audio/source_audio"
    for artifact_file in source_artifact_dir.iterdir():
        artifact_file.unlink()
    source_artifact_dir.rmdir()
    second_exit_code = main(["process", str(session.record_path)])

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

    def which(self, command: str) -> Path | None:
        return Path("/usr/bin") / command

    def run(self, args: Sequence[str], *, timeout_seconds: float = 30.0) -> CommandRun:
        command = tuple(args)
        self.run_calls.append(command)
        return CommandRun(command, 0, stdout="ok")


def _setup_state(
    *,
    settings: AppSettings,
    ollama_model_present: bool = True,
    asr_model_present: bool | None = True,
    diarization_model_present: bool | None = True,
    pyannote_token_present: bool = False,
) -> LocalInstallationState:
    profile = get_alpha_local_profile(settings.local_llm.ollama_model)
    return LocalInstallationState(
        command_identity=CommandIdentity(
            current_executable=Path("/home/user/.local/bin/mena"),
            plain_mena_path=Path("/home/user/.local/bin/mena"),
            version="0.1.0",
            invocation_mode="installed-product",
        ),
        runtime_paths=settings.paths,
        hardware=HardwareFacts(
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
            PythonSurfaceCheck(requirement=requirement, installed=True)
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
