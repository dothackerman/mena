import json
from datetime import UTC, datetime
from pathlib import Path

from mena.artifacts import ArtifactRecord, FileSystemArtifactStore
from mena.correction_overlay import (
    CorrectionEntry,
    create_correction_overlay,
    persist_correction_overlay_artifact,
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
from mena.sessions import SESSION_RECORD_FILENAME, SESSION_RECORD_VERSION

SESSION_ID = "00000000-0000-0000-0000-000000000001"


def _build_summarize_session_bundle(
    tmp_path: Path,
) -> tuple[Path, FileSystemArtifactStore, ArtifactRecord, ArtifactRecord]:
    session_dir = _create_session_record(tmp_path)
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=session_dir)
    merged_record = _persist_merged_transcript(store=store)
    overlay_artifact = create_correction_overlay(
        session_id=SESSION_ID,
        source_transcript_artifact=ArtifactRef.from_record(merged_record),
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


def _build_ready_summarize_session_bundle_with_metadata(
    tmp_path: Path,
    *,
    directory_name: str,
    created_at: str,
    name: str | None = None,
) -> Path:
    session_dir = tmp_path / "sessions" / directory_name
    session_dir.mkdir(parents=True)
    _write_session_record(session_dir, created_at=created_at, name=name)

    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=session_dir)
    merged_record = _persist_merged_transcript(store=store)
    overlay_artifact = create_correction_overlay(
        session_id=SESSION_ID,
        source_transcript_artifact=ArtifactRef.from_record(merged_record),
        entries=(),
        now=datetime(2026, 5, 19, 15, 20, 0, tzinfo=UTC),
    )
    persist_correction_overlay_artifact(
        store=store,
        artifact=overlay_artifact,
        name=f"overlay/{directory_name}.json",
        metadata={
            "workflow_status": "completed",
            "completed_at": "2026-05-19T15:20:00Z",
        },
    )
    return session_dir


def _create_session_record(tmp_path: Path) -> Path:
    session_dir = tmp_path / "session"
    session_dir.mkdir(parents=True)
    _write_session_record(
        session_dir,
        created_at="2026-05-19T15:00:00Z",
        name=None,
    )
    return session_dir


def _write_session_record(
    session_dir: Path,
    *,
    created_at: str,
    name: str | None,
) -> None:
    record = {
        "version": SESSION_RECORD_VERSION,
        "id": SESSION_ID,
        "created_at": created_at,
        "name": name,
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
    }
    (session_dir / SESSION_RECORD_FILENAME).write_text(
        json.dumps(record, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _persist_merged_transcript(*, store: FileSystemArtifactStore) -> ArtifactRecord:
    source_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/transcript/source.json",
        artifact_type="transcript",
        version=1,
        sha256="1" * 64,
    )
    artifact = MergedTranscriptArtifact(
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
            _merged_segment(
                segment_id="segment_1",
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
                confidence=0.99,
            ),
            _merged_segment(
                segment_id="segment_2",
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
                confidence=0.88,
            ),
        ),
        created_at="2026-05-19T15:00:00Z",
    )
    return persist_merged_transcript_artifact(
        store=store,
        artifact=artifact,
        name="merged/merged.json",
    )


def _merged_segment(
    *,
    segment_id: str,
    text: str,
    start_seconds: float,
    end_seconds: float,
    source_id: str,
    channel: str,
    speaker: SpeakerIdentity,
    confidence: float,
) -> MergedTranscriptSegment:
    return MergedTranscriptSegment(
        id=segment_id,
        text=text,
        start_seconds=start_seconds,
        end_seconds=end_seconds,
        source_id=source_id,
        channel=channel,
        speaker=speaker,
        transcription_confidence=SegmentConfidence(
            status="available",
            metric="word_probability",
            value=confidence,
            reason="fixture",
        ),
        overlap_detected=False,
        transcript_segment_id=(
            "local_segment_1" if source_id == "local_mic" else "remote_segment_1"
        ),
        metadata={},
    )
