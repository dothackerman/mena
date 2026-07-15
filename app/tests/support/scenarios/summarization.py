from __future__ import annotations

import json
from dataclasses import dataclass
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


@dataclass(frozen=True)
class SummarizationSessionSpec:
    directory_name: str = "session"
    created_at: str = "2026-05-19T15:00:00Z"
    name: str | None = None
    under_sessions_root: bool = False


@dataclass(frozen=True)
class SummarizationScenario:
    session_dir: Path
    store: FileSystemArtifactStore
    merged_record: ArtifactRecord
    overlay_record: ArtifactRecord


def arrange_ready_summarization(
    *,
    root: Path,
    session: SummarizationSessionSpec | None = None,
    corrections: tuple[CorrectionEntry, ...] | None = None,
) -> SummarizationScenario:
    """Persist one coherent pre-summarization scenario."""
    session = session or SummarizationSessionSpec()
    parent = root / "sessions" if session.under_sessions_root else root
    session_dir = parent / session.directory_name
    session_dir.mkdir(parents=True)
    _write_session_record(session_dir, session=session)

    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=session_dir)
    merged_record = _persist_merged_transcript(store)
    resolved_corrections = corrections if corrections is not None else (_text_correction(),)
    overlay = create_correction_overlay(
        session_id=SESSION_ID,
        source_transcript_artifact=ArtifactRef.from_record(merged_record),
        entries=resolved_corrections,
        now=datetime(2026, 5, 19, 15, 20, 0, tzinfo=UTC),
    )
    overlay_record = persist_correction_overlay_artifact(
        store=store,
        artifact=overlay,
        name=f"overlay/{session.directory_name}.json",
        metadata={
            "workflow_status": "completed",
            "completed_at": "2026-05-19T15:20:00Z",
        },
    )
    return SummarizationScenario(session_dir, store, merged_record, overlay_record)


def _write_session_record(session_dir: Path, *, session: SummarizationSessionSpec) -> None:
    record = {
        "version": SESSION_RECORD_VERSION,
        "id": SESSION_ID,
        "created_at": session.created_at,
        "name": session.name,
        "status": "created",
        "profile": "online_dual_stream",
        "language": "fr-CH",
        "recording": {
            "started_at": None,
            "stopped_at": None,
            "stop_reason": None,
            "finalization": {"status": "not_started", "completed_at": None, "error": None},
        },
        "capture_plan": None,
        "degradations": [],
        "failures": [],
    }
    (session_dir / SESSION_RECORD_FILENAME).write_text(
        json.dumps(record, sort_keys=True) + "\n", encoding="utf-8"
    )


def _persist_merged_transcript(store: FileSystemArtifactStore) -> ArtifactRecord:
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
            _segment(
                "segment_1",
                "Hello",
                0.0,
                0.8,
                "local_mic",
                "local",
                "you",
                "You",
                "local_user",
                0.99,
            ),
            _segment(
                "segment_2",
                "We need to decide priorities.",
                1.0,
                2.0,
                "system_audio",
                "remote",
                "remote",
                "Remote",
                "fallback_remote",
                0.88,
            ),
        ),
        created_at="2026-05-19T15:00:00Z",
    )
    return persist_merged_transcript_artifact(
        store=store, artifact=artifact, name="merged/merged.json"
    )


def _segment(
    segment_id: str,
    text: str,
    start: float,
    end: float,
    source_id: str,
    channel: str,
    speaker_id: str,
    label: str,
    kind: str,
    confidence: float,
) -> MergedTranscriptSegment:
    speaker_confidence = (
        SegmentConfidence(status="available", metric="structural", value=1.0, reason="fixture")
        if kind == "local_user"
        else SegmentConfidence(status="unavailable", reason="diarization_disabled")
    )
    return MergedTranscriptSegment(
        id=segment_id,
        text=text,
        start_seconds=start,
        end_seconds=end,
        source_id=source_id,
        channel=channel,
        speaker=SpeakerIdentity(
            speaker_id=speaker_id, label=label, kind=kind, confidence=speaker_confidence
        ),
        transcription_confidence=SegmentConfidence(
            status="available", metric="word_probability", value=confidence, reason="fixture"
        ),
        overlap_detected=False,
        transcript_segment_id="local_segment_1" if source_id == "local_mic" else "remote_segment_1",
        metadata={},
    )


def _text_correction() -> CorrectionEntry:
    return CorrectionEntry(
        id="entry_1",
        segment_id="segment_2",
        correction_type="text_edit",
        original_flag_id="flag_1",
        user_action_at="2026-05-19T15:20:01Z",
        text="Corrected segment.",
        metadata={},
    )
