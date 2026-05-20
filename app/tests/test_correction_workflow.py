import json
import os
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
    _load_review_queue,
    _new_speaker_id,
    _ordered_review_flags,
    _ordered_speaker_catalog,
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
