"""Interactive correction workflow orchestration for the CLI."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from mena.artifacts import ArtifactRecord, FileSystemArtifactStore
from mena.correction_overlay import (
    CorrectionEntry,
    create_correction_overlay,
    persist_correction_overlay_artifact,
)
from mena.merge_timing import (
    MERGED_TRANSCRIPT_ARTIFACT_TYPE,
    MergedTranscriptArtifact,
    MergedTranscriptSegment,
)
from mena.processing import ArtifactRef
from mena.review_flagging import (
    REVIEW_QUEUE_ARTIFACT_TYPE,
    ReviewFlag,
    ReviewFlaggingContractError,
    ReviewQueueArtifact,
)
from mena.sessions import SESSION_RECORD_FILENAME, read_session_record


class CorrectionWorkflowError(RuntimeError):
    """Base class for correction workflow failures."""


@dataclass(frozen=True)
class CorrectionWorkflowResult:
    """Summary and persisted output of one correction run."""

    reviewed: int
    skipped: int
    corrected: int
    overlay_artifact_record: ArtifactRecord


def run_correction_workflow(
    session_path: str | Path,
    *,
    now: datetime | None = None,
    input_fn: Callable[[str], str] = input,
) -> CorrectionWorkflowResult:
    """Run an interactive correction session and persist a correction overlay."""

    session_path = Path(session_path)
    session_dir, session_record = _resolve_session(session_path)
    session_id = _extract_session_id(session_record)
    store = FileSystemArtifactStore(session_id=session_id, session_dir=session_dir)

    review_queue_record, review_queue = _load_review_queue(
        store=store,
        session_id=session_id,
    )
    source_record, source_transcript = _load_source_transcript(
        store=store,
        source_ref=review_queue.merged_transcript_artifact,
        session_id=session_id,
    )
    review_items = _ordered_review_flags(
        review_queue=review_queue,
        source_transcript=source_transcript,
    )
    speaker_catalog = _ordered_speaker_catalog(source_transcript)
    ordered_segments = tuple(
        sorted(source_transcript.segments, key=lambda segment: segment.start_seconds)
    )
    segment_positions = {segment.id: index for index, segment in enumerate(ordered_segments)}

    entries: list[CorrectionEntry] = []
    reviewed = 0
    skipped = 0
    corrected = 0

    try:
        for index, (flag, segment) in enumerate(review_items, start=1):
            segment_position = segment_positions[segment.id]
            _print_review_item(
                index=index,
                total=len(review_items),
                flag=flag,
                segment=segment,
                ordered_segments=ordered_segments,
                segment_position=segment_position,
                candidates=speaker_catalog,
            )

            action = _prompt_action(input_fn=input_fn)
            reviewed += 1

            if action == "skip":
                entries.append(
                    _make_skip_entry(
                        entry_id=f"entry_{index:04d}",
                        segment_id=segment.id,
                        flag_id=flag.id,
                        now=_timestamp(now, index=index),
                    )
                )
                skipped += 1
                continue

            if action == "speaker_reassignment":
                speaker_id, speaker_label = _prompt_speaker_assignment(
                    input_fn=input_fn,
                    candidates=speaker_catalog,
                )
                entry = _make_speaker_entry(
                    entry_id=f"entry_{index:04d}",
                    segment_id=segment.id,
                    flag_id=flag.id,
                    speaker_id=speaker_id,
                    speaker_label=speaker_label,
                    now=_timestamp(now, index=index),
                )
            elif action == "text_edit":
                corrected_text = _prompt_text_edit(input_fn=input_fn)
                entry = _make_text_entry(
                    entry_id=f"entry_{index:04d}",
                    segment_id=segment.id,
                    flag_id=flag.id,
                    text=corrected_text,
                    now=_timestamp(now, index=index),
                )
            else:
                overlap_marked = _prompt_overlap_mark(input_fn=input_fn)
                entry = _make_overlap_entry(
                    entry_id=f"entry_{index:04d}",
                    segment_id=segment.id,
                    flag_id=flag.id,
                    overlap_detected=overlap_marked,
                    now=_timestamp(now, index=index),
                )

            entries.append(entry)
            corrected += 1
    except (EOFError, KeyboardInterrupt):
        if entries:
            _persist_overlay(
                store=store,
                session_id=session_id,
                source_record=source_record,
                review_queue_record=review_queue_record,
                entries=entries,
                created_at=_timestamp(now),
                status="staged",
                metadata={
                    "workflow_status": "cancelled",
                    "reviewed": reviewed,
                    "skipped": skipped,
                    "corrected": corrected,
                },
            )
        raise

    completed_at = _timestamp(now)
    overlay_record = _persist_overlay(
        store=store,
        session_id=session_id,
        source_record=source_record,
        review_queue_record=review_queue_record,
        entries=entries,
        created_at=completed_at,
        status="sealed",
        metadata={
            "workflow_status": "completed",
            "completed_at": completed_at,
            "reviewed": reviewed,
            "skipped": skipped,
            "corrected": corrected,
        },
    )

    print(f"Correction completed. reviewed={reviewed}, skipped={skipped}, corrected={corrected}.")
    return CorrectionWorkflowResult(
        reviewed=reviewed,
        skipped=skipped,
        corrected=corrected,
        overlay_artifact_record=overlay_record,
    )


def _persist_overlay(
    *,
    store: FileSystemArtifactStore,
    session_id: str,
    source_record: ArtifactRecord,
    review_queue_record: ArtifactRecord,
    entries: list[CorrectionEntry],
    created_at: str,
    status: str,
    metadata: dict[str, object],
) -> ArtifactRecord:
    overlay = create_correction_overlay(
        session_id=session_id,
        source_transcript_artifact=ArtifactRef.from_record(source_record),
        review_queue_artifact=ArtifactRef.from_record(review_queue_record),
        entries=tuple(entries),
        created_at=created_at,
    )
    return persist_correction_overlay_artifact(
        store=store,
        artifact=overlay,
        name=_overlay_name(session_id=session_id),
        status=status,
        metadata=metadata,
    )


def _resolve_session(session_path: Path) -> tuple[Path, dict[str, object]]:
    if session_path.is_dir():
        session_record_path = session_path / SESSION_RECORD_FILENAME
        session_dir = session_path
    elif session_path.is_file() and session_path.name == SESSION_RECORD_FILENAME:
        session_record_path = session_path
        session_dir = session_path.parent
    else:
        raise CorrectionWorkflowError(
            "Session path must be a session directory or an existing session.json path."
        )

    try:
        record = read_session_record(session_record_path)
    except Exception as exc:  # noqa: BLE001
        raise CorrectionWorkflowError(
            f"Cannot read session record at {session_record_path}."
        ) from exc

    return session_dir, record


def _extract_session_id(session_record: dict[str, object]) -> str:
    session_id = session_record.get("id")
    if not isinstance(session_id, str) or not session_id:
        raise CorrectionWorkflowError("Session record must include a non-empty id.")
    return session_id


def _load_review_queue(
    *,
    store: FileSystemArtifactStore,
    session_id: str,
) -> tuple[ArtifactRecord, ReviewQueueArtifact]:
    records = store.list(artifact_type=REVIEW_QUEUE_ARTIFACT_TYPE)
    if not records:
        raise CorrectionWorkflowError("No review queue artifact found for this session.")

    latest: tuple[ArtifactRecord, ReviewQueueArtifact] | None = None
    for record in records:
        verification = store.verify(record.id)
        if not verification.ok:
            raise CorrectionWorkflowError(
                f"Review queue artifact {record.id} is invalid: {verification.error}"
            )

        try:
            queue = ReviewQueueArtifact.from_dict(json.loads(store.retrieve(record.id)))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CorrectionWorkflowError(
                f"Review queue artifact {record.id} payload is not valid JSON."
            ) from exc
        except ReviewFlaggingContractError as exc:
            raise CorrectionWorkflowError(
                f"Review queue artifact {record.id} is invalid: {exc}"
            ) from exc

        if queue.session_id != session_id:
            raise CorrectionWorkflowError(
                "Review queue session_id does not match the session record."
            )

        latest_key = (
            None
            if latest is None
            else _review_queue_recency_key(
                store=store,
                record=latest[0],
                queue=latest[1],
            )
        )
        candidate_key = _review_queue_recency_key(store=store, record=record, queue=queue)
        if latest_key is None or candidate_key > latest_key:
            latest = (record, queue)

    if latest is None:
        raise CorrectionWorkflowError("No valid review queue artifact was found for this session.")
    return latest


def _review_queue_recency_key(
    *,
    store: FileSystemArtifactStore,
    record: ArtifactRecord,
    queue: ReviewQueueArtifact,
) -> tuple[str, int, str]:
    path = store.session_dir / record.path
    return (queue.created_at, path.stat().st_mtime_ns, record.id)


def _load_source_transcript(
    *,
    store: FileSystemArtifactStore,
    source_ref: ArtifactRef,
    session_id: str,
) -> tuple[ArtifactRecord, MergedTranscriptArtifact]:
    if source_ref.artifact_type != MERGED_TRANSCRIPT_ARTIFACT_TYPE:
        raise CorrectionWorkflowError(
            "Review queue source transcript must reference merged_transcript."
        )

    records = {
        record.id: record for record in store.list(artifact_type=MERGED_TRANSCRIPT_ARTIFACT_TYPE)
    }
    try:
        source_record = records[source_ref.id]
    except KeyError as exc:
        raise CorrectionWorkflowError(
            "Review queue references a missing merged transcript artifact."
        ) from exc

    if source_record.sha256 != source_ref.sha256:
        raise CorrectionWorkflowError("Review queue source transcript sha256 is stale.")

    verification = store.verify(source_record.id)
    if not verification.ok:
        raise CorrectionWorkflowError(
            f"Source transcript artifact is invalid: {verification.error}"
        )

    try:
        source_transcript = MergedTranscriptArtifact.from_dict(
            json.loads(store.retrieve(source_record.id))
        )
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CorrectionWorkflowError(
            "Source transcript artifact payload is not valid JSON."
        ) from exc
    except Exception as exc:
        raise CorrectionWorkflowError("Source transcript artifact payload is invalid.") from exc

    if source_transcript.session_id != session_id:
        raise CorrectionWorkflowError("Source transcript session_id does not match session record.")

    return source_record, source_transcript


def _ordered_review_flags(
    *,
    review_queue: ReviewQueueArtifact,
    source_transcript: MergedTranscriptArtifact,
) -> tuple[tuple[ReviewFlag, MergedTranscriptSegment], ...]:
    segment_index = {segment.id: segment for segment in source_transcript.segments}
    review_items: list[tuple[ReviewFlag, MergedTranscriptSegment]] = []
    for flag in review_queue.flags:
        segment = segment_index.get(flag.segment_id)
        if segment is None:
            raise CorrectionWorkflowError(
                f"Review flag {flag.id} references unknown segment_id {flag.segment_id}."
            )
        review_items.append((flag, segment))

    return tuple(
        sorted(
            review_items,
            key=lambda pair: (pair[1].start_seconds, pair[1].end_seconds, pair[0].start_seconds),
        )
    )


def _ordered_speaker_catalog(source_transcript: MergedTranscriptArtifact) -> list[tuple[str, str]]:
    ordered: list[tuple[str, str]] = []
    seen: set[str] = set()
    for segment in sorted(source_transcript.segments, key=lambda segment: segment.start_seconds):
        speaker_id = segment.speaker.speaker_id
        if speaker_id in seen:
            continue
        ordered.append((speaker_id, segment.speaker.label))
        seen.add(speaker_id)
    return ordered


def _prompt_action(input_fn: Callable[[str], str]) -> str:
    while True:
        raw = (
            input_fn("Action [s]kip, [r]eassign speaker, [t]ext edit, or [o]verlap: ")
            .strip()
            .lower()
        )
        if raw in {"s", "skip"}:
            return "skip"
        if raw in {"r", "speaker", "reassign", "speaker_reassignment"}:
            return "speaker_reassignment"
        if raw in {"t", "text", "text_edit", "edit"}:
            return "text_edit"
        if raw in {"o", "overlap", "overlap_mark"}:
            return "overlap_mark"
        print("Invalid action. Use [s]kip, [r]eassign, [t]ext, or [o]verlap.")


def _prompt_speaker_assignment(
    *,
    input_fn: Callable[[str], str],
    candidates: list[tuple[str, str]],
) -> tuple[str, str]:
    while True:
        raw = input_fn("Choose existing speaker number or enter a new speaker label: ").strip()
        if raw.isdigit():
            index = int(raw) - 1
            if 0 <= index < len(candidates):
                return candidates[index]
            print("Choose a valid number from the list.")
            continue
        if not raw:
            print("Enter a speaker id or a label.")
            continue

        lower_raw = raw.lower()
        for speaker_id, speaker_label in candidates:
            if raw == speaker_id or lower_raw == speaker_label.lower():
                return speaker_id, speaker_label

        used_ids = {speaker_id for speaker_id, _ in candidates}
        speaker_id = _new_speaker_id(raw, used_ids)
        speaker_label = raw
        candidates.append((speaker_id, speaker_label))
        return speaker_id, speaker_label


def _prompt_text_edit(input_fn: Callable[[str], str]) -> str:
    return input_fn("Corrected text: ")


def _prompt_overlap_mark(input_fn: Callable[[str], str]) -> bool:
    while True:
        raw = input_fn("Mark overlap? [y/N]: ").strip().lower()
        if raw in {"", "n", "no"}:
            return False
        if raw in {"y", "yes"}:
            return True
        print("Please answer y or n.")


def _make_speaker_entry(
    *,
    entry_id: str,
    segment_id: str,
    flag_id: str,
    speaker_id: str,
    speaker_label: str,
    now: str,
) -> CorrectionEntry:
    return CorrectionEntry(
        id=entry_id,
        segment_id=segment_id,
        correction_type="speaker_reassignment",
        original_flag_id=flag_id,
        user_action_at=now,
        speaker_id=speaker_id,
        speaker_label=speaker_label,
        metadata={"reason": "user_action"},
    )


def _make_text_entry(
    *,
    entry_id: str,
    segment_id: str,
    flag_id: str,
    text: str,
    now: str,
) -> CorrectionEntry:
    return CorrectionEntry(
        id=entry_id,
        segment_id=segment_id,
        correction_type="text_edit",
        original_flag_id=flag_id,
        user_action_at=now,
        text=text,
        metadata={"reason": "user_action"},
    )


def _make_overlap_entry(
    *,
    entry_id: str,
    segment_id: str,
    flag_id: str,
    overlap_detected: bool,
    now: str,
) -> CorrectionEntry:
    return CorrectionEntry(
        id=entry_id,
        segment_id=segment_id,
        correction_type="overlap_mark",
        original_flag_id=flag_id,
        user_action_at=now,
        overlap_detected=overlap_detected,
        metadata={"reason": "user_action"},
    )


def _make_skip_entry(
    *,
    entry_id: str,
    segment_id: str,
    flag_id: str,
    now: str,
) -> CorrectionEntry:
    return CorrectionEntry(
        id=entry_id,
        segment_id=segment_id,
        correction_type="skip_mark",
        original_flag_id=flag_id,
        user_action_at=now,
        metadata={"reason": "user_action"},
    )


def _print_review_item(
    *,
    index: int,
    total: int,
    flag: ReviewFlag,
    segment: MergedTranscriptSegment,
    ordered_segments: tuple[MergedTranscriptSegment, ...],
    segment_position: int,
    candidates: list[tuple[str, str]],
) -> None:
    print(f"\nReview item {index}/{total}")
    print(f"Segment: {segment.id} [{segment.start_seconds:.2f} - {segment.end_seconds:.2f}]")
    print(f"Reason: {flag.reason_code}")
    print(f"Current speaker: {segment.speaker.speaker_id} ({segment.speaker.label})")
    print(f"Text: {segment.text}")
    previous_segment, next_segment = _surrounding_segments(
        ordered_segments=ordered_segments,
        segment_position=segment_position,
    )
    if previous_segment is not None:
        print(
            "Context (previous): "
            f"[{previous_segment.start_seconds:.2f} - {previous_segment.end_seconds:.2f}] "
            f"{previous_segment.text}"
        )
    if next_segment is not None:
        print(
            "Context (next): "
            f"[{next_segment.start_seconds:.2f} - {next_segment.end_seconds:.2f}] "
            f"{next_segment.text}"
        )
    if candidates:
        print("Candidate speakers:")
        for candidate_index, (speaker_id, speaker_label) in enumerate(candidates, start=1):
            print(f"{candidate_index}) {speaker_id} ({speaker_label})")


def _surrounding_segments(
    *,
    ordered_segments: tuple[MergedTranscriptSegment, ...],
    segment_position: int,
) -> tuple[MergedTranscriptSegment | None, MergedTranscriptSegment | None]:
    previous_segment = ordered_segments[segment_position - 1] if segment_position > 0 else None
    next_segment = (
        ordered_segments[segment_position + 1]
        if segment_position < len(ordered_segments) - 1
        else None
    )
    return previous_segment, next_segment


def _new_speaker_id(label: str, used_ids: set[str]) -> str:
    base = label.strip().lower().replace(" ", "_")
    base = re.sub(r"[^a-z0-9_:-]", "_", base)
    base = re.sub(r"_+", "_", base).strip("_")
    if not base or not base[0].isalpha():
        base = f"speaker_{base or 'new'}"

    candidate = base
    index = 1
    while candidate in used_ids:
        candidate = f"{base}_{index}"
        index += 1
    return candidate


def _timestamp(now: datetime | None, *, index: int = 0) -> str:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    now = now.astimezone(UTC) + timedelta(seconds=index)
    return now.isoformat(timespec="seconds").replace("+00:00", "Z")


def _overlay_name(*, session_id: str) -> Path:
    timestamp = datetime.now(UTC).isoformat(timespec="microseconds").replace(":", "")
    return Path("correction_overlay") / f"{session_id}-{timestamp}-{uuid4().hex}.json"
