from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from mena.artifacts import ArtifactRecord, FileSystemArtifactStore
from mena.correction_overlay import (
    CorrectionEntry,
    create_correction_overlay,
    persist_correction_overlay_artifact,
)
from mena.fileops import write_json_atomically
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
from mena.sessions import (
    SESSION_RECORD_FILENAME,
    SESSION_RECORD_VERSION,
    RecordingFinalization,
    RecordingLifecycle,
    SessionRecord,
)
from mena.transcription import (
    TRANSCRIPT_SCHEMA_VERSION,
    TranscriptArtifact,
    TranscriptLanguage,
    TranscriptProviderMetadata,
    TranscriptSegment,
    persist_transcript_artifact,
)

SESSION_ID = "00000000-0000-0000-0000-000000000001"


@dataclass(frozen=True)
class SummarizationSessionSpec:
    directory_name: str = "session"
    created_at: str = "2026-05-19T15:00:00Z"
    name: str | None = None
    session_id: str | None = None


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
    session_id = session.session_id or _default_session_id(session.directory_name)
    session_dir = root / session.directory_name
    session_dir.mkdir(parents=True)
    _write_session_record(session_dir, session=session, session_id=session_id)

    store = FileSystemArtifactStore(session_id=session_id, session_dir=session_dir)
    merged_record = _persist_merged_transcript(store, session_id=session_id)
    resolved_corrections = corrections if corrections is not None else (_text_correction(),)
    overlay = create_correction_overlay(
        session_id=session_id,
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


def _write_session_record(
    session_dir: Path, *, session: SummarizationSessionSpec, session_id: str
) -> None:
    record = SessionRecord(
        version=SESSION_RECORD_VERSION,
        id=session_id,
        created_at=session.created_at,
        name=session.name,
        status="created",
        profile="online_dual_stream",
        language="fr-CH",
        recording=RecordingLifecycle(
            started_at=None,
            stopped_at=None,
            stop_reason=None,
            finalization=RecordingFinalization(status="not_started", completed_at=None, error=None),
        ),
        capture_plan=None,
        degradations=(),
        failures=(),
    )
    write_json_atomically(session_dir / SESSION_RECORD_FILENAME, asdict(record))


def _persist_merged_transcript(
    store: FileSystemArtifactStore, *, session_id: str
) -> ArtifactRecord:
    local_transcript = _persist_source_transcript(
        store,
        session_id=session_id,
        source_id="local_mic",
        segment_id="local_segment_1",
        text="Hello",
        start=0.0,
        end=0.8,
    )
    remote_transcript = _persist_source_transcript(
        store,
        session_id=session_id,
        source_id="system_audio",
        segment_id="remote_segment_1",
        text="We need to decide priorities.",
        start=1.0,
        end=2.0,
    )
    segments = (
        _segment(
            segment_id="segment_1",
            text="Hello",
            start=0.0,
            end=0.8,
            source_id="local_mic",
            channel="local",
            speaker_id="you",
            label="You",
            kind="local_user",
            confidence=0.99,
        ),
        _segment(
            segment_id="segment_2",
            text="We need to decide priorities.",
            start=1.0,
            end=2.0,
            source_id="system_audio",
            channel="remote",
            speaker_id="remote",
            label="Remote",
            kind="fallback_remote",
            confidence=0.88,
        ),
    )
    artifact = MergedTranscriptArtifact(
        version=MERGED_TRANSCRIPT_SCHEMA_VERSION,
        session_id=session_id,
        policy=MergePolicy(),
        sources=(
            MergedTranscriptSource(
                source_id="local_mic",
                channel="local",
                transcript_artifact=ArtifactRef.from_record(local_transcript),
                diarization_status="not_applicable",
                diarization_reason="local_user_structural_attribution",
            ),
            MergedTranscriptSource(
                source_id="system_audio",
                channel="remote",
                transcript_artifact=ArtifactRef.from_record(remote_transcript),
                diarization_status="skipped",
                diarization_reason="diarization_disabled",
            ),
        ),
        text=" ".join(segment.text for segment in segments),
        segments=segments,
        created_at="2026-05-19T15:00:00Z",
    )
    return persist_merged_transcript_artifact(
        store=store, artifact=artifact, name="merged/merged.json"
    )


def _persist_source_transcript(
    store: FileSystemArtifactStore,
    *,
    session_id: str,
    source_id: str,
    segment_id: str,
    text: str,
    start: float,
    end: float,
) -> ArtifactRecord:
    source_audio = store.store(
        artifact_type="capture_audio",
        name=f"source/{source_id}.wav",
        data=f"{source_id} fixture audio".encode(),
    )
    transcript = TranscriptArtifact(
        version=TRANSCRIPT_SCHEMA_VERSION,
        session_id=session_id,
        source_artifact=ArtifactRef.from_record(source_audio),
        source_id=source_id,
        provider=TranscriptProviderMetadata(
            name="fake",
            checkpoint="fixture",
            quantization="none",
            device="test",
            timestamp_mode="segment",
        ),
        language=TranscriptLanguage(requested="fr", detected="fr", detection_probability=1.0),
        text=text,
        segments=(
            TranscriptSegment(
                id=segment_id,
                text=text,
                start_seconds=start,
                end_seconds=end,
            ),
        ),
        created_at="2026-05-19T15:00:00Z",
    )
    return persist_transcript_artifact(
        store=store,
        transcript=transcript,
        name=f"source/{source_id}.json",
    )


def _segment(
    *,
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


def _default_session_id(directory_name: str) -> str:
    if directory_name == "session":
        return SESSION_ID
    return str(uuid5(NAMESPACE_URL, f"https://mena.test/sessions/{directory_name}"))
