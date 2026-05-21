import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

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
    _resolve_latest_merged_transcript_record,
    main,
)
from mena.correction_overlay import (
    CorrectionEntry,
    create_correction_overlay,
    persist_correction_overlay_artifact,
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
from mena.processing import ArtifactRef, ProcessingStageLedger
from mena.profiles import CaptureProfileId
from mena.protocol import ProtocolError, ProtocolMessage
from mena.sessions import SESSION_RECORD_FILENAME, SESSION_RECORD_VERSION
from mena.summarization import SummarizationError, SummarizationPolicy

SESSION_ID = "00000000-0000-0000-0000-000000000001"


def test_main_prints_help_without_command(capsys: CaptureFixture[str]) -> None:
    exit_code = main([])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Privacy-first local meeting notes PoC." in captured.out


def test_main_exposes_architecture_command_surface(capsys: CaptureFixture[str]) -> None:
    for command in [command for command in COMMANDS[1:] if command not in {"correct", "summarize"}]:
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
