import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mena.artifacts import ArtifactRecord, FileSystemArtifactStore
from mena.cli import main
from mena.correction_overlay import (
    CORRECTION_OVERLAY_ARTIFACT_TYPE,
)
from mena.correction_workflow import CorrectionWorkflowError, run_correction_workflow
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


def test_run_correction_workflow_skips_and_persists_empty_entries(tmp_path: Path) -> None:
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
    assert payload["entries"] == []


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
