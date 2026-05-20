import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from pytest import CaptureFixture

from mena.artifacts import ArtifactStoreError, FileSystemArtifactStore
from mena.cli import (
    COMMANDS,
    WORKER_EXIT_TIMEOUT_SECONDS,
    SourceAudioInput,
    _collect_capture_segments,
    _require_provider,
    _run_processing_command,
    _run_processing_stages,
    main,
)
from mena.diarization import FakeDiarizationProvider
from mena.discovery import AudioDiscovery, AudioRoute, DiscoveryStatus, PlaybackStream
from mena.processing import ArtifactRef, ProcessingErrorInfo, ProcessingStageLedger
from mena.profiles import CaptureProfileId, get_capture_profile, parse_capture_profile
from mena.protocol import ProtocolError, ProtocolMessage
from mena.semantic_coherence import FakeSemanticCoherenceProvider, SemanticCoherenceProvider
from mena.sessions import CreatedSession, create_session, update_session_capture_metadata
from mena.transcription import FakeTranscriptionProvider, TranscriptionProvider


def test_main_prints_help_without_command(capsys: CaptureFixture[str]) -> None:
    exit_code = main([])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Privacy-first local meeting notes PoC." in captured.out


def test_main_exposes_architecture_command_surface(capsys: CaptureFixture[str]) -> None:
    for command in [
        command for command in COMMANDS[1:] if command not in {"correct", "process", "resume"}
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
            metadata=SimpleNamespace(config_id="cfg"),
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


def test_main_resume_after_completed_checkpoint_reports_no_retry(
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
        stage="transcription",
        input_artifacts=(),
        attempt=1,
        now=now,
    )
    ledger.mark_completed(
        stage="transcription",
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


def test_run_processing_stages_rejects_non_bi_source_profile(tmp_path: Path) -> None:
    base_profile = get_capture_profile(parse_capture_profile("online").id)
    one_source_profile = replace(base_profile, sources=(base_profile.sources[0],))
    store = FileSystemArtifactStore(session_id="session-no2", session_dir=tmp_path)
    ledger = ProcessingStageLedger(session_id="session-no2", run_id="run-no2", store=store)

    with pytest.raises(
        ValueError, match=r"Processing currently supports profiles with exactly two sources."
    ):
        _run_processing_stages(
            session_id="session-no2",
            profile=one_source_profile,
            source_inputs=cast(
                dict[str, SourceAudioInput],
                {
                    "local_mic": SimpleNamespace(
                        artifact=SimpleNamespace(id="a", artifact_type="source_audio"),
                        path=tmp_path / "local_mic_segment.wav",
                    )
                },
            ),
            store=store,
            ledger=ledger,
            attempt=1,
            now=datetime(2026, 5, 8, 8, 0, tzinfo=UTC),
            transcription_provider=SimpleNamespace(
                name="fake", transcribe=lambda *_args, **_kwargs: None
            ),
            diarization_provider=SimpleNamespace(
                name="fake", config_id="cfg", diarize=lambda *_args, **_kwargs: None
            ),
            semantic_coherence_provider=SimpleNamespace(
                metadata=SimpleNamespace(config_id="cfg"), check=lambda *_args, **_kwargs: True
            ),
        )


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
