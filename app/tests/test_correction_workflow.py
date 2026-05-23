import json
import os
import subprocess
import wave
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mena.artifacts import ArtifactRecord, FileSystemArtifactStore
from mena.cli import main
from mena.correction_overlay import (
    CORRECTION_OVERLAY_ARTIFACT_TYPE,
)
from mena.correction_workflow import (
    CorrectionWorkflowError,
    LocalPlaybackPlayer,
    PlaybackError,
    PlaybackRequest,
    _load_review_queue,
    _new_speaker_id,
    _ordered_review_flags,
    _ordered_speaker_catalog,
    _playback_window,
    _print_review_item,
    _prompt_overlap_mark,
    _prompt_speaker_assignment,
    _resolve_session,
    _timestamp,
    run_correction_workflow,
)
from mena.correction_workflow import (
    _extract_session_id as extract_session_id,
)
from mena.merge_timing import (
    MERGED_TRANSCRIPT_SCHEMA_VERSION,
    MergedTranscriptArtifact,
    MergedTranscriptSegment,
    MergedTranscriptSource,
    MergePolicy,
    SegmentConfidence,
    SpeakerIdentity,
    persist_merged_transcript_artifact,
)
from mena.processing import ArtifactRef
from mena.review_flagging import (
    REVIEW_QUEUE_SCHEMA_VERSION,
    ConfidenceFlaggingPolicy,
    ReviewFlag,
    ReviewQueueArtifact,
    persist_review_queue_artifact,
)
from mena.sessions import SESSION_RECORD_FILENAME, SESSION_RECORD_VERSION
from mena.transcription import (
    TRANSCRIPT_SCHEMA_VERSION,
    TranscriptArtifact,
    TranscriptLanguage,
    TranscriptProviderMetadata,
    TranscriptSegment,
    persist_transcript_artifact,
)

SESSION_ID = "00000000-0000-0000-0000-000000000001"
NOW = datetime(2026, 5, 19, 15, 20, 0, tzinfo=UTC)


def test_run_correction_workflow_persists_overlay_and_keeps_source_transcript_immutable(
    tmp_path: Path,
) -> None:
    session_dir, store, review_record = _build_session_bundle(
        tmp_path,
        flags=(
            ReviewFlag(
                id="flag_low_transcript",
                segment_id="segment_1",
                reason_code="low_transcription_confidence",
                start_seconds=0.0,
                end_seconds=0.8,
                source_id="local_mic",
                speaker_id="you",
                metadata={},
            ),
            ReviewFlag(
                id="flag_speaker_overlap",
                segment_id="segment_2",
                reason_code="speaker_overlap",
                start_seconds=1.0,
                end_seconds=1.8,
                source_id="system_audio",
                speaker_id="remote",
                metadata={},
            ),
        ),
    )

    source_record = store.list(artifact_type="merged_transcript")[0]
    source_path = session_dir / source_record.path
    source_bytes_before = source_path.read_bytes()
    inputs = iter(["r", "2", "o", "y"])

    result = run_correction_workflow(
        session_path=session_dir,
        now=NOW,
        input_fn=lambda _: next(inputs),
    )

    assert result.reviewed == 2
    assert result.skipped == 0
    assert result.corrected == 2
    payload = json.loads(store.retrieve(result.overlay_artifact_record.id))
    assert payload["review_queue_artifact"]["id"] == review_record.id
    assert len(payload["entries"]) == 2
    assert payload["version"] == 1
    assert payload["source_transcript_artifact"]["artifact_type"] == "merged_transcript"
    assert payload["entries"][0]["correction_type"] in {
        "speaker_reassignment",
        "overlap_mark",
    }
    assert len(store.list(artifact_type=CORRECTION_OVERLAY_ARTIFACT_TYPE)) == 1
    assert result.overlay_artifact_record.metadata["workflow_status"] == "completed"
    assert result.overlay_artifact_record.metadata["completed_at"] == "2026-05-19T15:20:00Z"
    assert source_path.read_bytes() == source_bytes_before


def test_run_correction_workflow_rejects_invalid_action_and_reprompts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session_dir, _, _ = _build_session_bundle(
        tmp_path,
        flags=(
            ReviewFlag(
                id="flag_invalid_action",
                segment_id="segment_1",
                reason_code="low_transcription_confidence",
                start_seconds=0.0,
                end_seconds=0.8,
                source_id="local_mic",
                speaker_id="you",
                metadata={},
            ),
        ),
    )
    inputs = iter(["z", "s"])

    result = run_correction_workflow(
        session_path=session_dir,
        now=NOW,
        input_fn=lambda _: next(inputs),
    )
    captured = capsys.readouterr()

    assert result.reviewed == 1
    assert result.skipped == 1
    assert "Invalid action" in captured.out


def test_run_correction_workflow_records_all_action_types(
    tmp_path: Path,
) -> None:
    session_dir, store, _ = _build_session_bundle(
        tmp_path,
        flags=(
            ReviewFlag(
                id="flag_speaker_reassign",
                segment_id="segment_1",
                reason_code="low_transcription_confidence",
                start_seconds=0.0,
                end_seconds=0.8,
                source_id="local_mic",
                speaker_id="you",
                metadata={},
            ),
            ReviewFlag(
                id="flag_text_edit",
                segment_id="segment_2",
                reason_code="low_speaker_confidence",
                start_seconds=1.0,
                end_seconds=1.8,
                source_id="system_audio",
                speaker_id="remote",
                metadata={},
            ),
            ReviewFlag(
                id="flag_overlap_mark",
                segment_id="segment_1",
                reason_code="speaker_overlap",
                start_seconds=0.0,
                end_seconds=0.8,
                source_id="local_mic",
                speaker_id="you",
                metadata={},
            ),
            ReviewFlag(
                id="flag_speaker_reassign_2",
                segment_id="segment_2",
                reason_code="low_speaker_confidence",
                start_seconds=1.0,
                end_seconds=1.8,
                source_id="system_audio",
                speaker_id="remote",
                metadata={},
            ),
        ),
    )
    inputs = iter(
        [
            "r",
            "1",  # keep first segment as `you`
            "t",
            "corrected",
            "o",
            "y",
            "r",
            "2",  # reassign to `remote`
        ]
    )

    result = run_correction_workflow(
        session_path=session_dir,
        now=NOW,
        input_fn=lambda _: next(inputs),
    )
    payload = json.loads(store.retrieve(result.overlay_artifact_record.id))

    assert result.reviewed == 4
    assert result.skipped == 0
    assert result.corrected == 4
    assert [entry["correction_type"] for entry in payload["entries"]] == [
        "speaker_reassignment",
        "text_edit",
        "overlap_mark",
        "speaker_reassignment",
    ]
    assert payload["entries"][1]["text"] == "corrected"
    assert payload["entries"][2]["overlap_detected"] is True
    assert payload["entries"][3]["speaker_id"] == "remote"


def test_run_correction_workflow_allows_new_speaker_label(
    tmp_path: Path,
) -> None:
    session_dir, store, _ = _build_session_bundle(
        tmp_path,
        flags=(
            ReviewFlag(
                id="flag_new_label",
                segment_id="segment_2",
                reason_code="low_speaker_confidence",
                start_seconds=1.0,
                end_seconds=1.8,
                source_id="system_audio",
                speaker_id="remote",
                metadata={},
            ),
        ),
    )
    inputs = iter(["r", "Alice Johnson"])

    result = run_correction_workflow(
        session_path=session_dir,
        now=NOW,
        input_fn=lambda _: next(inputs),
    )
    payload = json.loads(store.retrieve(result.overlay_artifact_record.id))

    assert result.reviewed == 1
    assert result.corrected == 1
    assert payload["entries"][0]["speaker_id"] == "alice_johnson"
    assert payload["entries"][0]["speaker_label"] == "Alice Johnson"


def test_run_correction_workflow_displays_context_and_candidates(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session_dir, _, _ = _build_session_bundle(
        tmp_path,
        flags=(
            ReviewFlag(
                id="flag_context",
                segment_id="segment_2",
                reason_code="low_speaker_confidence",
                start_seconds=1.0,
                end_seconds=1.8,
                source_id="system_audio",
                speaker_id="remote",
                metadata={},
            ),
        ),
    )
    inputs = iter(["s"])

    run_correction_workflow(
        session_path=session_dir,
        now=NOW,
        input_fn=lambda _: next(inputs),
    )
    captured = capsys.readouterr()

    assert "Reason: low_speaker_confidence" in captured.out
    assert "Context (previous):" in captured.out
    assert "Candidate speakers:" in captured.out
    assert "1) you (You)" in captured.out


def test_playback_window_includes_context_and_clamps_start() -> None:
    first_segment, second_segment = _merged_transcript_fixture().segments

    first_start, first_end = _playback_window(first_segment)
    second_start, second_end = _playback_window(second_segment)

    assert first_start == pytest.approx(0.0)
    assert first_end == pytest.approx(2.3)
    assert second_start == pytest.approx(0.0)
    assert second_end == pytest.approx(3.3)


def test_local_playback_player_uses_ffplay_argument_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio_path = tmp_path / "source.wav"
    audio_path.write_bytes(b"not decoded by ffplay test")
    calls: list[tuple[list[str], bool]] = []

    def _which(command: str) -> str | None:
        return "/usr/bin/ffplay" if command == "ffplay" else None

    def _run(args: list[str], *, check: bool) -> None:
        calls.append((args, check))

    monkeypatch.setattr("mena.correction_workflow.shutil.which", _which)
    monkeypatch.setattr("mena.correction_workflow.subprocess.run", _run)

    LocalPlaybackPlayer()(
        PlaybackRequest(
            audio_path=audio_path,
            start_seconds=1.25,
            end_seconds=3.5,
            source_id="local_mic",
            segment_id="segment_1",
        )
    )

    assert calls == [
        (
            [
                "/usr/bin/ffplay",
                "-nodisp",
                "-autoexit",
                "-loglevel",
                "error",
                "-ss",
                "1.250",
                "-t",
                "2.250",
                str(audio_path),
            ],
            True,
        )
    ]


def test_local_playback_player_clips_wav_for_simple_players_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio_path = tmp_path / "source.wav"
    _write_wav_fixture(audio_path)
    clip_paths: list[Path] = []

    def _which(command: str) -> str | None:
        if command == "pw-play":
            return "/usr/bin/pw-play"
        return None

    def _run(args: list[str], *, check: bool) -> None:
        assert args[0] == "/usr/bin/pw-play"
        assert check is True
        clip_path = Path(args[1])
        assert clip_path.exists()
        clip_paths.append(clip_path)

    monkeypatch.setattr("mena.correction_workflow.shutil.which", _which)
    monkeypatch.setattr("mena.correction_workflow.subprocess.run", _run)

    LocalPlaybackPlayer()(
        PlaybackRequest(
            audio_path=audio_path,
            start_seconds=0.05,
            end_seconds=0.2,
            source_id="local_mic",
            segment_id="segment_1",
        )
    )

    assert len(clip_paths) == 1
    assert not clip_paths[0].exists()


def test_local_playback_player_reports_missing_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mena.correction_workflow.shutil.which", lambda _: None)

    with pytest.raises(PlaybackError, match="No supported local audio playback command"):
        LocalPlaybackPlayer()(
            PlaybackRequest(
                audio_path=Path("missing.wav"),
                start_seconds=0.0,
                end_seconds=1.0,
                source_id="local_mic",
                segment_id="segment_1",
            )
        )


def test_local_playback_player_maps_missing_detected_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio_path = tmp_path / "source.wav"
    audio_path.write_bytes(b"not decoded by ffplay test")
    monkeypatch.setattr(
        "mena.correction_workflow.shutil.which",
        lambda command: "/usr/bin/ffplay" if command == "ffplay" else None,
    )

    def _missing(_args: list[str], *, check: bool) -> None:
        raise FileNotFoundError

    monkeypatch.setattr("mena.correction_workflow.subprocess.run", _missing)

    with pytest.raises(PlaybackError, match="Playback command not found"):
        LocalPlaybackPlayer()(
            PlaybackRequest(
                audio_path=audio_path,
                start_seconds=0.0,
                end_seconds=1.0,
                source_id="local_mic",
                segment_id="segment_1",
            )
        )


def test_local_playback_player_maps_subprocess_os_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio_path = tmp_path / "source.wav"
    audio_path.write_bytes(b"not decoded by ffplay test")
    monkeypatch.setattr(
        "mena.correction_workflow.shutil.which",
        lambda command: "/usr/bin/ffplay" if command == "ffplay" else None,
    )

    def _permission_error(_args: list[str], *, check: bool) -> None:
        raise PermissionError("not executable")

    monkeypatch.setattr("mena.correction_workflow.subprocess.run", _permission_error)

    with pytest.raises(PlaybackError, match="could not be started"):
        LocalPlaybackPlayer()(
            PlaybackRequest(
                audio_path=audio_path,
                start_seconds=0.0,
                end_seconds=1.0,
                source_id="local_mic",
                segment_id="segment_1",
            )
        )


def test_local_playback_player_rejects_unclippable_source_for_simple_players(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio_path = tmp_path / "source.wav"
    audio_path.write_bytes(b"not a wav")

    def _which(command: str) -> str | None:
        if command == "pw-play":
            return "/usr/bin/pw-play"
        return None

    monkeypatch.setattr("mena.correction_workflow.shutil.which", _which)

    with pytest.raises(PlaybackError, match="Only readable PCM WAV"):
        LocalPlaybackPlayer()(
            PlaybackRequest(
                audio_path=audio_path,
                start_seconds=0.0,
                end_seconds=1.0,
                source_id="local_mic",
                segment_id="segment_1",
            )
        )


def test_local_playback_player_maps_temp_clip_write_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio_path = tmp_path / "source.wav"
    _write_wav_fixture(audio_path)

    def _which(command: str) -> str | None:
        if command == "pw-play":
            return "/usr/bin/pw-play"
        return None

    class FailingWaveWriter:
        def __enter__(self) -> "FailingWaveWriter":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def setparams(self, _params: object) -> None:
            return None

        def writeframes(self, _frames: bytes) -> None:
            raise OSError("disk full")

    original_wave_open = wave.open

    def _wave_open(path: str, mode: str = "rb") -> object:
        if mode == "wb":
            return FailingWaveWriter()
        return original_wave_open(path, mode)

    monkeypatch.setattr("mena.correction_workflow.shutil.which", _which)
    monkeypatch.setattr("mena.correction_workflow.wave.open", _wave_open)

    with pytest.raises(PlaybackError, match="Temporary playback clip"):
        LocalPlaybackPlayer()(
            PlaybackRequest(
                audio_path=audio_path,
                start_seconds=0.0,
                end_seconds=0.2,
                source_id="local_mic",
                segment_id="segment_1",
            )
        )


def test_local_playback_player_maps_temp_clip_creation_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio_path = tmp_path / "source.wav"
    _write_wav_fixture(audio_path)

    def _which(command: str) -> str | None:
        if command == "pw-play":
            return "/usr/bin/pw-play"
        return None

    monkeypatch.setattr("mena.correction_workflow.shutil.which", _which)
    monkeypatch.setattr(
        "mena.correction_workflow.tempfile.mkstemp",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("tmp unavailable")),
    )

    with pytest.raises(PlaybackError, match="Temporary playback clip could not be created"):
        LocalPlaybackPlayer()(
            PlaybackRequest(
                audio_path=audio_path,
                start_seconds=0.0,
                end_seconds=0.2,
                source_id="local_mic",
                segment_id="segment_1",
            )
        )


def test_local_playback_player_maps_subprocess_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio_path = tmp_path / "source.wav"
    audio_path.write_bytes(b"not decoded by ffplay test")
    monkeypatch.setattr(
        "mena.correction_workflow.shutil.which",
        lambda command: "/usr/bin/ffplay" if command == "ffplay" else None,
    )

    def _fail(args: list[str], *, check: bool) -> None:
        raise subprocess.CalledProcessError(returncode=9, cmd=args)

    monkeypatch.setattr("mena.correction_workflow.subprocess.run", _fail)

    with pytest.raises(PlaybackError, match="exited with status 9"):
        LocalPlaybackPlayer()(
            PlaybackRequest(
                audio_path=audio_path,
                start_seconds=0.0,
                end_seconds=1.0,
                source_id="local_mic",
                segment_id="segment_1",
            )
        )


def test_run_correction_workflow_playback_invokes_fake_player_and_returns_to_same_item(
    tmp_path: Path,
) -> None:
    session_dir, store, _, source_records = _build_playback_session_bundle(
        tmp_path,
        flags=(
            ReviewFlag(
                id="flag_play_remote",
                segment_id="segment_2",
                reason_code="low_speaker_confidence",
                start_seconds=1.0,
                end_seconds=1.8,
                source_id="system_audio",
                speaker_id="remote",
                metadata={},
            ),
        ),
    )
    requests: list[PlaybackRequest] = []
    inputs = iter(["p", "s"])

    result = run_correction_workflow(
        session_path=session_dir,
        now=NOW,
        input_fn=lambda _: next(inputs),
        playback_player=requests.append,
    )

    assert result.reviewed == 1
    assert result.skipped == 1
    assert result.corrected == 0
    assert len(requests) == 1
    assert requests[0].source_id == "system_audio"
    assert requests[0].segment_id == "segment_2"
    assert requests[0].audio_path == session_dir / source_records["system_audio"].path
    assert requests[0].start_seconds == pytest.approx(0.0)
    assert requests[0].end_seconds == pytest.approx(3.3)
    payload = json.loads(store.retrieve(result.overlay_artifact_record.id))
    assert [entry["original_flag_id"] for entry in payload["entries"]] == ["flag_play_remote"]


def test_run_correction_workflow_playback_failure_does_not_consume_review_item(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session_dir, _, _, _ = _build_playback_session_bundle(
        tmp_path,
        flags=(
            ReviewFlag(
                id="flag_play_failure",
                segment_id="segment_1",
                reason_code="low_transcription_confidence",
                start_seconds=0.0,
                end_seconds=0.8,
                source_id="local_mic",
                speaker_id="you",
                metadata={},
            ),
        ),
    )
    requests: list[PlaybackRequest] = []
    inputs = iter(["p", "s"])

    def _fail_playback(request: PlaybackRequest) -> None:
        requests.append(request)
        raise PlaybackError("audio device unavailable")

    result = run_correction_workflow(
        session_path=session_dir,
        now=NOW,
        input_fn=lambda _: next(inputs),
        playback_player=_fail_playback,
    )
    captured = capsys.readouterr()

    assert result.reviewed == 1
    assert result.skipped == 1
    assert len(requests) == 1
    assert "Playback failed: audio device unavailable" in captured.out


def test_run_correction_workflow_playback_rejects_missing_transcript_reference(
    tmp_path: Path,
) -> None:
    session_dir, _, _ = _build_session_bundle(
        tmp_path,
        flags=(
            ReviewFlag(
                id="flag_missing_transcript",
                segment_id="segment_1",
                reason_code="low_transcription_confidence",
                start_seconds=0.0,
                end_seconds=0.8,
                source_id="local_mic",
                speaker_id="you",
                metadata={},
            ),
        ),
    )
    requests: list[PlaybackRequest] = []

    with pytest.raises(CorrectionWorkflowError, match="missing transcript artifact"):
        run_correction_workflow(
            session_path=session_dir,
            now=NOW,
            input_fn=lambda _: "p",
            playback_player=requests.append,
        )

    assert requests == []


def test_run_correction_workflow_playback_rejects_stale_source_audio_reference(
    tmp_path: Path,
) -> None:
    session_dir, _, _, _ = _build_playback_session_bundle(
        tmp_path,
        flags=(
            ReviewFlag(
                id="flag_stale_source",
                segment_id="segment_2",
                reason_code="low_speaker_confidence",
                start_seconds=1.0,
                end_seconds=1.8,
                source_id="system_audio",
                speaker_id="remote",
                metadata={},
            ),
        ),
        source_audio_ref_sha_by_id={"system_audio": "0" * 64},
    )

    with pytest.raises(CorrectionWorkflowError, match="Source audio artifact .* sha256 is stale"):
        run_correction_workflow(
            session_path=session_dir,
            now=NOW,
            input_fn=lambda _: "p",
            playback_player=lambda _: None,
        )


def test_run_correction_workflow_playback_rejects_version_mismatched_source_audio_reference(
    tmp_path: Path,
) -> None:
    session_dir, _, _, _ = _build_playback_session_bundle(
        tmp_path,
        flags=(
            ReviewFlag(
                id="flag_stale_source_version",
                segment_id="segment_2",
                reason_code="low_speaker_confidence",
                start_seconds=1.0,
                end_seconds=1.8,
                source_id="system_audio",
                speaker_id="remote",
                metadata={},
            ),
        ),
        source_audio_ref_version_by_id={"system_audio": 999},
    )

    with pytest.raises(CorrectionWorkflowError, match="Source audio artifact .* version is stale"):
        run_correction_workflow(
            session_path=session_dir,
            now=NOW,
            input_fn=lambda _: "p",
            playback_player=lambda _: None,
        )


def test_run_correction_workflow_playback_wraps_malformed_transcript_source_ref(
    tmp_path: Path,
) -> None:
    session_dir = _create_session_record(tmp_path)
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=session_dir)
    source_record = store.store(
        artifact_type="source_audio",
        name="source_audio/system_audio.wav",
        data=b"system audio",
        status="sealed",
        metadata={"source_id": "system_audio"},
    )
    transcript_payload = _transcript_fixture(
        source_id="system_audio",
        source_record=source_record,
    ).to_dict()
    transcript_payload["source_artifact"] = {
        "id": source_record.id,
        "version": source_record.version,
        "sha256": source_record.sha256,
    }
    transcript_record = store.store(
        artifact_type="transcript",
        name="transcript/system_audio.json",
        data=json.dumps(transcript_payload, sort_keys=True).encode(),
        status="sealed",
        metadata={},
        version=TRANSCRIPT_SCHEMA_VERSION,
    )
    merged_record = persist_merged_transcript_artifact(
        store=store,
        artifact=_merged_transcript_fixture_with_refs(
            local_ref=ArtifactRef.from_record(transcript_record),
            remote_ref=ArtifactRef.from_record(transcript_record),
        ),
        name="merged/merged.json",
    )
    persist_review_queue_artifact(
        store=store,
        artifact=_review_queue_fixture(
            merged_record=ArtifactRef.from_record(merged_record),
            flags=(
                ReviewFlag(
                    id="flag_malformed_transcript_ref",
                    segment_id="segment_2",
                    reason_code="low_speaker_confidence",
                    start_seconds=1.0,
                    end_seconds=1.8,
                    source_id="system_audio",
                    speaker_id="remote",
                    metadata={},
                ),
            ),
        ),
        name="review/queue.json",
    )

    with pytest.raises(
        CorrectionWorkflowError,
        match="Transcript artifact for source 'system_audio' payload is invalid",
    ):
        run_correction_workflow(
            session_path=session_dir,
            now=NOW,
            input_fn=lambda _: "p",
            playback_player=lambda _: None,
        )


def test_run_correction_workflow_playback_rejects_corrupt_source_audio_artifact(
    tmp_path: Path,
) -> None:
    session_dir, _, _, source_records = _build_playback_session_bundle(
        tmp_path,
        flags=(
            ReviewFlag(
                id="flag_corrupt_source",
                segment_id="segment_2",
                reason_code="low_speaker_confidence",
                start_seconds=1.0,
                end_seconds=1.8,
                source_id="system_audio",
                speaker_id="remote",
                metadata={},
            ),
        ),
    )
    (session_dir / source_records["system_audio"].path).write_bytes(b"changed audio")

    with pytest.raises(CorrectionWorkflowError, match="Source audio artifact .* is invalid"):
        run_correction_workflow(
            session_path=session_dir,
            now=NOW,
            input_fn=lambda _: "p",
            playback_player=lambda _: None,
        )


def test_run_correction_workflow_persists_skip_mark_entries(tmp_path: Path) -> None:
    session_dir, store, _ = _build_session_bundle(
        tmp_path,
        flags=(
            ReviewFlag(
                id="flag_low_speaker",
                segment_id="segment_2",
                reason_code="low_speaker_confidence",
                start_seconds=1.0,
                end_seconds=1.8,
                source_id="system_audio",
                speaker_id="remote",
                metadata={},
            ),
        ),
    )

    result = run_correction_workflow(
        session_path=session_dir,
        now=NOW,
        input_fn=lambda _: "s",
    )
    payload = json.loads(store.retrieve(result.overlay_artifact_record.id))

    assert result.reviewed == 1
    assert result.skipped == 1
    assert result.corrected == 0
    assert payload["entries"] == [
        {
            "correction_type": "skip_mark",
            "id": "entry_0001",
            "metadata": {"reason": "user_action"},
            "original_flag_id": "flag_low_speaker",
            "overlap_detected": None,
            "segment_id": "segment_2",
            "speaker_id": None,
            "speaker_label": None,
            "text": None,
            "user_action_at": "2026-05-19T15:20:01Z",
        }
    ]


def test_run_correction_workflow_persists_partial_overlay_on_cancellation(
    tmp_path: Path,
) -> None:
    session_dir, store, _ = _build_session_bundle(
        tmp_path,
        flags=(
            ReviewFlag(
                id="flag_kept",
                segment_id="segment_1",
                reason_code="low_transcription_confidence",
                start_seconds=0.0,
                end_seconds=0.8,
                source_id="local_mic",
                speaker_id="you",
                metadata={},
            ),
            ReviewFlag(
                id="flag_cancelled",
                segment_id="segment_2",
                reason_code="low_speaker_confidence",
                start_seconds=1.0,
                end_seconds=1.8,
                source_id="system_audio",
                speaker_id="remote",
                metadata={},
            ),
        ),
    )
    inputs = iter(("s",))

    def _input(_: str) -> str:
        try:
            return next(inputs)
        except StopIteration as exc:
            raise EOFError from exc

    with pytest.raises(EOFError):
        run_correction_workflow(session_path=session_dir, now=NOW, input_fn=_input)

    overlays = store.list(artifact_type=CORRECTION_OVERLAY_ARTIFACT_TYPE)
    assert len(overlays) == 1
    assert overlays[0].status == "staged"
    assert overlays[0].metadata["workflow_status"] == "cancelled"
    assert "completed_at" not in overlays[0].metadata
    assert overlays[0].metadata["reviewed"] == 1
    payload = json.loads(store.retrieve(overlays[0].id))
    assert [entry["correction_type"] for entry in payload["entries"]] == ["skip_mark"]
    assert payload["entries"][0]["original_flag_id"] == "flag_kept"


def test_run_correction_workflow_can_persist_two_runs_in_same_second(tmp_path: Path) -> None:
    session_dir, store, _ = _build_session_bundle(
        tmp_path,
        flags=(
            ReviewFlag(
                id="flag_repeat",
                segment_id="segment_2",
                reason_code="low_speaker_confidence",
                start_seconds=1.0,
                end_seconds=1.8,
                source_id="system_audio",
                speaker_id="remote",
                metadata={},
            ),
        ),
    )

    first = run_correction_workflow(
        session_path=session_dir,
        now=NOW,
        input_fn=lambda _: "s",
    )
    second = run_correction_workflow(
        session_path=session_dir,
        now=NOW,
        input_fn=lambda _: "s",
    )

    assert first.overlay_artifact_record.id != second.overlay_artifact_record.id
    assert len(store.list(artifact_type=CORRECTION_OVERLAY_ARTIFACT_TYPE)) == 2


def test_run_correction_workflow_rejects_missing_review_queue(tmp_path: Path) -> None:
    session_dir = _create_session_record(tmp_path)

    with pytest.raises(CorrectionWorkflowError, match="No review queue artifact"):
        run_correction_workflow(session_path=session_dir, now=NOW, input_fn=lambda _: "s")


def test_run_correction_workflow_rejects_stale_review_queue_reference(tmp_path: Path) -> None:
    session_dir, store, _ = _build_session_bundle(
        tmp_path,
        flags=(
            ReviewFlag(
                id="flag_1",
                segment_id="segment_1",
                reason_code="low_transcription_confidence",
                start_seconds=0.0,
                end_seconds=0.8,
                source_id="local_mic",
                speaker_id="you",
                metadata={},
            ),
        ),
    )
    stale_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/merged_transcript/missing.json",
        artifact_type="merged_transcript",
        version=1,
        sha256="0" * 64,
    )
    stale_review = _review_queue_fixture(
        created_at="2026-05-19T16:00:00Z",
        merged_record=stale_ref,
        flags=(
            ReviewFlag(
                id="stale_flag",
                segment_id="segment_1",
                reason_code="low_transcription_confidence",
                start_seconds=0.0,
                end_seconds=0.8,
                source_id="local_mic",
                speaker_id="you",
                metadata={},
            ),
        ),
    )
    # Not used in assertions; this creates an invalid additional queue that must be ignored.
    persist_review_queue_artifact(
        store=store,
        artifact=stale_review,
        name="stale/review-queue.json",
    )

    with pytest.raises(CorrectionWorkflowError, match="missing merged transcript"):
        run_correction_workflow(session_path=session_dir, now=NOW, input_fn=lambda _: "s")


def test_load_review_queue_rejects_failed_integrity_verification(tmp_path: Path) -> None:
    session_dir, store, review_record = _build_session_bundle(
        tmp_path,
        flags=(
            ReviewFlag(
                id="flag_integrity",
                segment_id="segment_1",
                reason_code="low_transcription_confidence",
                start_seconds=0.0,
                end_seconds=0.8,
                source_id="local_mic",
                speaker_id="you",
                metadata={},
            ),
        ),
    )
    review_path = session_dir / review_record.path
    payload = json.loads(review_path.read_text(encoding="utf-8"))
    payload["flags"][0]["segment_id"] = "segment_2"
    review_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(CorrectionWorkflowError, match="Review queue artifact .* is invalid"):
        _load_review_queue(store=store, session_id=SESSION_ID)


def test_load_review_queue_uses_file_mtime_when_created_at_ties(tmp_path: Path) -> None:
    session_dir = _create_session_record(tmp_path)
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=session_dir)
    merged_record = persist_merged_transcript_artifact(
        store=store,
        artifact=_merged_transcript_fixture(),
        name="merged/merged.json",
    )
    merged_ref = ArtifactRef.from_record(merged_record)
    first = persist_review_queue_artifact(
        store=store,
        artifact=_review_queue_fixture(
            merged_record=merged_ref,
            flags=(
                ReviewFlag(
                    id="first_queue_flag",
                    segment_id="segment_1",
                    reason_code="low_transcription_confidence",
                    start_seconds=0.0,
                    end_seconds=0.8,
                    source_id="local_mic",
                    speaker_id="you",
                    metadata={},
                ),
            ),
        ),
        name="review/z-older-queue.json",
    )
    second = persist_review_queue_artifact(
        store=store,
        artifact=_review_queue_fixture(
            merged_record=merged_ref,
            flags=(
                ReviewFlag(
                    id="second_queue_flag",
                    segment_id="segment_2",
                    reason_code="low_speaker_confidence",
                    start_seconds=1.0,
                    end_seconds=1.8,
                    source_id="system_audio",
                    speaker_id="remote",
                    metadata={},
                ),
            ),
        ),
        name="review/a-newer-queue.json",
    )
    os.utime(session_dir / first.path, ns=(1_000_000_000, 1_000_000_000))
    os.utime(session_dir / second.path, ns=(2_000_000_000, 2_000_000_000))

    record, queue = _load_review_queue(store=store, session_id=SESSION_ID)

    assert second.path < first.path
    assert record.id == second.id
    assert [flag.id for flag in queue.flags] == ["second_queue_flag"]


def test_resolve_session_supports_session_directory_and_session_record_file(tmp_path: Path) -> None:
    session_dir = _create_session_record(tmp_path)
    session_record_path = session_dir / SESSION_RECORD_FILENAME
    resolved_dir, record = _resolve_session(session_record_path)

    assert resolved_dir == session_dir
    assert isinstance(record, dict)
    assert record["id"] == SESSION_ID


def test_resolve_session_handles_bad_inputs_and_read_failures(tmp_path: Path) -> None:
    (tmp_path / SESSION_RECORD_FILENAME).write_text("{", encoding="utf-8")

    with pytest.raises(
        CorrectionWorkflowError,
        match="Session path must be a session directory or an existing session.json path.",
    ):
        _resolve_session(tmp_path / "missing")

    with pytest.raises(CorrectionWorkflowError, match="Cannot read session record"):
        _resolve_session(tmp_path / SESSION_RECORD_FILENAME)


def test_extract_session_id_validates_payload(tmp_path: Path) -> None:
    with pytest.raises(CorrectionWorkflowError, match="non-empty id"):
        extract_session_id({})

    assert extract_session_id({"id": SESSION_ID}) == SESSION_ID


def test_prompt_overlap_mark_reprompts_on_invalid_input() -> None:
    values = iter(["maybe", "yes"])
    response = _prompt_overlap_mark(input_fn=_read_with_values(values))

    assert response is True


def test_prompt_speaker_assignment_rejects_invalid_choices() -> None:
    values = iter(["3", "", "2"])
    input_fn = _read_with_values(values)

    speaker_id, speaker_label = _prompt_speaker_assignment(
        input_fn=input_fn,
        candidates=[("you", "You"), ("remote", "Remote")],
    )

    assert speaker_id == "remote"
    assert speaker_label == "Remote"


def test_prompt_speaker_assignment_adds_new_speaker_id_for_unknown_label() -> None:
    values = iter(["new person"])
    input_fn = _read_with_values(values)
    candidates: list[tuple[str, str]] = [("you", "You"), ("remote", "Remote")]

    speaker_id, speaker_label = _prompt_speaker_assignment(
        input_fn=input_fn,
        candidates=candidates,
    )

    assert speaker_id == "new_person"
    assert speaker_label == "new person"
    assert ("new_person", "new person") in candidates


def test_new_speaker_id_rewrites_and_makes_unique(tmp_path: Path) -> None:
    used_ids = {"alice", "alice_1"}
    assert _new_speaker_id("Alice", used_ids) == "alice_2"
    assert _new_speaker_id("123abc", used_ids) == "speaker_123abc"


def test_print_review_item_omits_candidates_when_empty(capsys: pytest.CaptureFixture[str]) -> None:
    segment = _merged_transcript_fixture().segments[0]

    _print_review_item(
        index=1,
        total=1,
        flag=ReviewFlag(
            id="flag",
            segment_id=segment.id,
            reason_code="speaker_overlap",
            start_seconds=segment.start_seconds,
            end_seconds=segment.end_seconds,
            source_id=segment.source_id,
            speaker_id=segment.speaker.speaker_id,
            metadata={},
        ),
        segment=segment,
        ordered_segments=(_merged_transcript_fixture().segments[0],),
        segment_position=0,
        candidates=[],
    )

    captured = capsys.readouterr()
    assert "Candidate speakers:" not in captured.out


def test_timestamp_normalizes_naive_datetime_and_increments_index() -> None:
    naive = datetime(2026, 5, 19, 12, 0, 0)
    assert _timestamp(naive) == "2026-05-19T12:00:00Z"
    assert _timestamp(datetime(2026, 5, 19, 12, 0, 0), index=2) == "2026-05-19T12:00:02Z"


def test_ordered_review_flags_rejects_unknown_segment_reference(tmp_path: Path) -> None:
    session_dir, store, _ = _build_session_bundle(
        tmp_path,
        flags=(
            ReviewFlag(
                id="flag_unknown_segment",
                segment_id="missing",
                reason_code="low_speaker_confidence",
                start_seconds=0.0,
                end_seconds=0.8,
                source_id="local_mic",
                speaker_id="you",
                metadata={},
            ),
        ),
    )
    with pytest.raises(CorrectionWorkflowError, match="references unknown segment_id"):
        run_correction_workflow(session_dir, now=NOW, input_fn=lambda _: "s")


def test_ordered_speaker_catalog_deduplicates_speakers() -> None:
    transcript = _merged_transcript_fixture()
    duplicated = transcript.segments + (transcript.segments[0],)
    catalog = _ordered_speaker_catalog(
        MergedTranscriptArtifact(
            version=transcript.version,
            session_id=transcript.session_id,
            policy=transcript.policy,
            sources=transcript.sources,
            text=transcript.text,
            segments=duplicated,
            created_at=transcript.created_at,
        )
    )

    assert catalog == [("you", "You"), ("remote", "Remote")]


def test_ordered_review_flags_orders_and_rejects_unknown_segments() -> None:
    transcript = _merged_transcript_fixture()
    review_queue = _review_queue_fixture(
        merged_record=ArtifactRef(
            id=f"{SESSION_ID}:artifacts/merged_transcript/review-source.json",
            artifact_type="merged_transcript",
            version=1,
            sha256="1" * 64,
        ),
        flags=(
            ReviewFlag(
                id="late",
                segment_id="segment_2",
                reason_code="speaker_overlap",
                start_seconds=1.0,
                end_seconds=1.8,
                source_id="system_audio",
                speaker_id="remote",
                metadata={},
            ),
            ReviewFlag(
                id="early",
                segment_id="segment_1",
                start_seconds=0.0,
                end_seconds=0.8,
                source_id="local_mic",
                reason_code="low_transcript_confidence",
                speaker_id="you",
                metadata={},
            ),
        ),
    )
    review_flags = _ordered_review_flags(
        review_queue=ReviewQueueArtifact(
            version=review_queue.version,
            session_id=review_queue.session_id,
            merged_transcript_artifact=review_queue.merged_transcript_artifact,
            policy=review_queue.policy,
            flags=review_queue.flags,
            created_at=review_queue.created_at,
        ),
        source_transcript=transcript,
    )

    assert [flag.id for flag, _ in review_flags] == ["early", "late"]

    with pytest.raises(
        CorrectionWorkflowError,
        match="references unknown segment_id unknown_segment",
    ):
        _ordered_review_flags(
            review_queue=ReviewQueueArtifact(
                version=review_queue.version,
                session_id=review_queue.session_id,
                merged_transcript_artifact=review_queue.merged_transcript_artifact,
                policy=review_queue.policy,
                flags=(
                    ReviewFlag(
                        id="bad",
                        segment_id="unknown_segment",
                        reason_code="low_transcript_confidence",
                        start_seconds=0.0,
                        end_seconds=0.8,
                        source_id="local_mic",
                        speaker_id="you",
                        metadata={},
                    ),
                ),
                created_at=review_queue.created_at,
            ),
            source_transcript=transcript,
        )


def test_main_correct_rejects_missing_session_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["correct", str(tmp_path / "missing_session.json")])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert (
        "Session path must be a session directory or an existing session.json path."
    ) in captured.out


def test_main_correct_runs_interactive_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session_dir, _, _ = _build_session_bundle(
        tmp_path,
        flags=(
            ReviewFlag(
                id="flag_main",
                segment_id="segment_1",
                reason_code="low_transcription_confidence",
                start_seconds=0.0,
                end_seconds=0.8,
                source_id="local_mic",
                speaker_id="you",
                metadata={},
            ),
        ),
    )
    monkeypatch.setattr("builtins.input", lambda _: "s")

    exit_code = main(["correct", str(session_dir)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Correction completed." in captured.out


@pytest.mark.parametrize("error", [EOFError, KeyboardInterrupt])
def test_main_correct_handles_interactive_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: type[BaseException],
) -> None:
    session_dir, _, _ = _build_session_bundle(
        tmp_path,
        flags=(
            ReviewFlag(
                id="flag_cancel",
                segment_id="segment_1",
                reason_code="low_transcription_confidence",
                start_seconds=0.0,
                end_seconds=0.8,
                source_id="local_mic",
                speaker_id="you",
                metadata={},
            ),
        ),
    )

    def _cancel(_: str) -> str:
        raise error

    monkeypatch.setattr("builtins.input", _cancel)

    exit_code = main(["correct", str(session_dir)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Correction workflow cancelled." in captured.out


def _build_session_bundle(
    tmp_path: Path,
    *,
    flags: tuple[ReviewFlag, ...],
) -> tuple[Path, FileSystemArtifactStore, ArtifactRecord]:
    session_dir = _create_session_record(tmp_path)
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=session_dir)
    merged_record = persist_merged_transcript_artifact(
        store=store,
        artifact=_merged_transcript_fixture(),
        name="merged/merged.json",
    )
    review_record = persist_review_queue_artifact(
        store=store,
        artifact=_review_queue_fixture(
            merged_record=ArtifactRef.from_record(merged_record),
            flags=flags,
        ),
        name="review/queue.json",
    )
    return session_dir, store, review_record


def _write_wav_fixture(path: Path) -> None:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(100)
        audio.writeframes(b"\x00\x00" * 50)


def _build_playback_session_bundle(
    tmp_path: Path,
    *,
    flags: tuple[ReviewFlag, ...],
    source_audio_ref_sha_by_id: dict[str, str] | None = None,
    source_audio_ref_version_by_id: dict[str, int] | None = None,
) -> tuple[Path, FileSystemArtifactStore, ArtifactRecord, dict[str, ArtifactRecord]]:
    session_dir = _create_session_record(tmp_path)
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=session_dir)
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
    stale_sha = source_audio_ref_sha_by_id or {}
    stale_version = source_audio_ref_version_by_id or {}
    transcript_records = {
        source_id: persist_transcript_artifact(
            store=store,
            transcript=_transcript_fixture(
                source_id=source_id,
                source_record=source_record,
                source_ref_sha=stale_sha.get(source_id),
                source_ref_version=stale_version.get(source_id),
            ),
            name=f"transcript/{source_id}.json",
        )
        for source_id, source_record in source_records.items()
    }
    merged_record = persist_merged_transcript_artifact(
        store=store,
        artifact=_merged_transcript_fixture_with_refs(
            local_ref=ArtifactRef.from_record(transcript_records["local_mic"]),
            remote_ref=ArtifactRef.from_record(transcript_records["system_audio"]),
        ),
        name="merged/merged.json",
    )
    review_record = persist_review_queue_artifact(
        store=store,
        artifact=_review_queue_fixture(
            merged_record=ArtifactRef.from_record(merged_record),
            flags=flags,
        ),
        name="review/queue.json",
    )
    return session_dir, store, review_record, source_records


def _transcript_fixture(
    *,
    source_id: str,
    source_record: ArtifactRecord,
    source_ref_sha: str | None = None,
    source_ref_version: int | None = None,
) -> TranscriptArtifact:
    source_ref = ArtifactRef.from_record(source_record)
    if source_ref_sha is not None or source_ref_version is not None:
        source_ref = ArtifactRef(
            id=source_ref.id,
            artifact_type=source_ref.artifact_type,
            version=source_ref.version if source_ref_version is None else source_ref_version,
            sha256=source_ref.sha256 if source_ref_sha is None else source_ref_sha,
        )
    return TranscriptArtifact(
        version=TRANSCRIPT_SCHEMA_VERSION,
        session_id=SESSION_ID,
        source_artifact=source_ref,
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


def _merged_transcript_fixture_with_refs(
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


def _read_with_values(values: Iterator[str]) -> Callable[[str], str]:
    def _next(_: str) -> str:
        return next(values)

    return _next


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


def _review_queue_fixture(
    *,
    merged_record: ArtifactRef,
    flags: tuple[ReviewFlag, ...],
    created_at: str = "2026-05-19T15:20:00Z",
) -> ReviewQueueArtifact:
    return ReviewQueueArtifact(
        version=REVIEW_QUEUE_SCHEMA_VERSION,
        session_id=SESSION_ID,
        merged_transcript_artifact=merged_record,
        policy=ConfidenceFlaggingPolicy(),
        flags=flags,
        created_at=created_at,
    )


def _merged_transcript_fixture() -> MergedTranscriptArtifact:
    source_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/transcript/segment.json",
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
                diarization_status="unavailable",
                diarization_reason="diarization_disabled",
            ),
        ),
        text="hello remote reply",
        segments=(
            MergedTranscriptSegment(
                id="segment_1",
                text="hello",
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
                        reason="local_user_structural",
                    ),
                ),
                transcription_confidence=SegmentConfidence(
                    status="available",
                    metric="word_probability",
                    value=0.99,
                    reason="mean_word_probability",
                ),
                overlap_detected=False,
                transcript_segment_id="local_segment_1",
                metadata={},
            ),
            MergedTranscriptSegment(
                id="segment_2",
                text="remote reply",
                start_seconds=1.0,
                end_seconds=1.8,
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
                    reason="mean_word_probability",
                ),
                overlap_detected=False,
                transcript_segment_id="remote_segment_1",
                metadata={},
            ),
        ),
        created_at="2026-05-19T15:00:00Z",
    )
