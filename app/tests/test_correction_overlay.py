import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from mena.artifacts import (
    ArtifactRecord,
    ArtifactStore,
    FileSystemArtifactStore,
    InMemoryArtifactStore,
)
from mena.correction_overlay import (
    CORRECTION_OVERLAY_ARTIFACT_TYPE,
    CORRECTION_OVERLAY_SCHEMA_VERSION,
    CorrectionEntry,
    CorrectionOverlayArtifact,
    CorrectionOverlayContractError,
    apply_correction_overlay,
    create_correction_overlay,
    persist_correction_overlay_artifact,
    resolve_active_correction_overlay,
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

SESSION_ID = "00000000-0000-0000-0000-000000000001"
NOW = datetime(2026, 5, 19, 15, 20, 0, tzinfo=UTC)


def test_correction_overlay_persists_schema_valid_artifact(tmp_path: Path) -> None:
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    source_record = _persist_source_transcript(store)
    overlay = _overlay_fixture(source_record=source_record)

    record = persist_correction_overlay_artifact(
        store=store,
        artifact=overlay,
        name="run-1/overlay.json",
    )

    assert record.artifact_type == CORRECTION_OVERLAY_ARTIFACT_TYPE
    assert record.metadata["contract"] == "correction-overlay.v1"
    assert record.metadata["source_transcript_artifact_id"] == source_record.id
    payload = json.loads(store.retrieve(record.id))
    Draft202012Validator(
        _load_correction_overlay_schema(),
        format_checker=FormatChecker(),
    ).validate(payload)
    artifact = CorrectionOverlayArtifact.from_dict(payload)
    assert artifact.version == CORRECTION_OVERLAY_SCHEMA_VERSION
    assert [entry.correction_type for entry in artifact.entries] == [
        "speaker_reassignment",
        "text_edit",
        "overlap_mark",
        "skip_mark",
    ]


def test_apply_correction_overlay_is_pure_and_idempotent() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    source_artifact = _merged_transcript_fixture()
    source_record = _persist_source_transcript(store, artifact=source_artifact)
    overlay = _overlay_fixture(source_record=source_record)
    source_before = deepcopy(source_artifact.to_dict())

    corrected = apply_correction_overlay(
        source_transcript=source_artifact,
        source_transcript_record=source_record,
        overlay=overlay,
    )
    corrected_again = apply_correction_overlay(
        source_transcript=source_artifact,
        source_transcript_record=source_record,
        overlay=overlay,
    )

    assert source_artifact.to_dict() == source_before
    assert corrected.to_dict() == corrected_again.to_dict()
    assert corrected.text == "hello corrected remote reply"
    first, second = corrected.segments
    assert first.speaker.speaker_id == "speaker_alice"
    assert first.speaker.label == "Alice"
    assert first.speaker.kind == "human_corrected"
    assert first.speaker.confidence.metric == "human_correction"
    assert first.overlap_detected is True
    assert second.text == "corrected remote reply"


def test_create_correction_overlay_adds_version_and_timestamp() -> None:
    source_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/merged_transcript/source.json",
        artifact_type="merged_transcript",
        version=1,
        sha256="0" * 64,
    )

    overlay = create_correction_overlay(
        session_id=SESSION_ID,
        source_transcript_artifact=source_ref,
        entries=(),
        now=NOW.replace(microsecond=123456),
    )

    assert overlay.version == CORRECTION_OVERLAY_SCHEMA_VERSION
    assert overlay.created_at == "2026-05-19T15:20:00Z"


def test_correction_overlay_rejects_non_transcript_source_ref() -> None:
    source_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/source_audio/source.wav",
        artifact_type="source_audio",
        version=1,
        sha256="0" * 64,
    )

    with pytest.raises(CorrectionOverlayContractError, match="merged transcript"):
        create_correction_overlay(
            session_id=SESSION_ID,
            source_transcript_artifact=source_ref,
            entries=(),
            now=NOW,
        )


def test_correction_overlay_rejects_invalid_entry_payloads() -> None:
    with pytest.raises(CorrectionOverlayContractError, match="speaker_id and speaker_label"):
        CorrectionEntry.from_dict(
            {
                "id": "entry_1",
                "segment_id": "segment_1",
                "correction_type": "speaker_reassignment",
                "original_flag_id": None,
                "user_action_at": "2026-05-19T15:20:00Z",
                "speaker_id": "speaker_alice",
                "speaker_label": None,
                "text": None,
                "overlap_detected": None,
                "metadata": {},
            }
        )

    with pytest.raises(CorrectionOverlayContractError, match="Text edit must include text"):
        CorrectionEntry.from_dict(
            {
                "id": "entry_2",
                "segment_id": "segment_1",
                "correction_type": "text_edit",
                "original_flag_id": None,
                "user_action_at": "2026-05-19T15:20:00Z",
                "speaker_id": None,
                "speaker_label": None,
                "text": None,
                "overlap_detected": None,
                "metadata": {},
            }
        )


def test_correction_overlay_accepts_schema_valid_empty_text_edit() -> None:
    entry = CorrectionEntry.from_dict(
        {
            "id": "entry_1",
            "segment_id": "segment_1",
            "correction_type": "text_edit",
            "original_flag_id": None,
            "user_action_at": "2026-05-19T15:20:00Z",
            "speaker_id": None,
            "speaker_label": None,
            "text": "",
            "overlap_detected": None,
            "metadata": {},
        }
    )

    assert entry.text == ""


def test_correction_entry_rejects_missing_nullable_contract_fields() -> None:
    payload = (
        _overlay_fixture(
            source_record=_persist_source_transcript(InMemoryArtifactStore(session_id=SESSION_ID))
        )
        .entries[0]
        .to_dict()
    )
    del payload["speaker_id"]

    with pytest.raises(CorrectionOverlayContractError, match="speaker_id is required"):
        CorrectionEntry.from_dict(payload)


def test_correction_entry_rejects_unknown_payload_fields() -> None:
    payload = (
        _overlay_fixture(
            source_record=_persist_source_transcript(InMemoryArtifactStore(session_id=SESSION_ID))
        )
        .entries[0]
        .to_dict()
    )
    payload["typoed_field"] = "unexpected"

    with pytest.raises(CorrectionOverlayContractError, match="unknown fields"):
        CorrectionEntry.from_dict(payload)


def test_correction_entry_rejects_invalid_user_action_timestamp() -> None:
    payload = (
        _overlay_fixture(
            source_record=_persist_source_transcript(InMemoryArtifactStore(session_id=SESSION_ID))
        )
        .entries[0]
        .to_dict()
    )
    payload["user_action_at"] = "not-a-timestamp"

    with pytest.raises(CorrectionOverlayContractError, match="date-time"):
        CorrectionEntry.from_dict(payload)


def test_apply_correction_overlay_rejects_unknown_segment() -> None:
    source_artifact = _merged_transcript_fixture()
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    source_record = _persist_source_transcript(store, artifact=source_artifact)
    overlay = create_correction_overlay(
        session_id=SESSION_ID,
        source_transcript_artifact=ArtifactRef.from_record(source_record),
        entries=(
            CorrectionEntry(
                id="entry_1",
                segment_id="missing_segment",
                correction_type="skip_mark",
                original_flag_id=None,
                user_action_at="2026-05-19T15:20:00Z",
                metadata={},
            ),
        ),
        now=NOW,
    )

    with pytest.raises(CorrectionOverlayContractError, match="unknown segment_id"):
        apply_correction_overlay(
            source_transcript=source_artifact,
            source_transcript_record=source_record,
            overlay=overlay,
        )


def test_apply_correction_overlay_rejects_source_ref_mismatch() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    source_artifact = _merged_transcript_fixture()
    source_record = _persist_source_transcript(store, artifact=source_artifact)
    other_record = store.store(
        artifact_type="merged_transcript",
        name="other-source.json",
        data=source_artifact.to_json_bytes(),
        metadata={"contract": "merged-transcript.v1"},
    )
    overlay = _overlay_fixture(source_record=other_record)

    with pytest.raises(CorrectionOverlayContractError, match="must match the source record"):
        apply_correction_overlay(
            source_transcript=source_artifact,
            source_transcript_record=source_record,
            overlay=overlay,
        )


def test_apply_correction_overlay_rejects_source_record_session_mismatch() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    other_session_id = "00000000-0000-0000-0000-000000000002"
    source_artifact = _merged_transcript_fixture()
    source_record = _persist_source_transcript(store, artifact=source_artifact)
    mismatched_record = ArtifactRecord(
        id=source_record.id.replace(SESSION_ID, other_session_id),
        session_id=other_session_id,
        artifact_type=source_record.artifact_type,
        version=source_record.version,
        path=source_record.path,
        byte_count=source_record.byte_count,
        sha256=source_record.sha256,
        status=source_record.status,
        metadata=source_record.metadata,
    )
    overlay = create_correction_overlay(
        session_id=SESSION_ID,
        source_transcript_artifact=ArtifactRef.from_record(mismatched_record),
        entries=(),
        now=NOW,
    )

    with pytest.raises(CorrectionOverlayContractError, match="record session_id"):
        apply_correction_overlay(
            source_transcript=source_artifact,
            source_transcript_record=mismatched_record,
            overlay=overlay,
        )


def test_apply_correction_overlay_rejects_source_content_mismatch() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    source_artifact = _merged_transcript_fixture()
    source_record = _persist_source_transcript(store, artifact=source_artifact)
    changed_source = replace(source_artifact, text="jello remote reply")
    overlay = _overlay_fixture(source_record=source_record)

    with pytest.raises(CorrectionOverlayContractError, match="sha256"):
        apply_correction_overlay(
            source_transcript=changed_source,
            source_transcript_record=source_record,
            overlay=overlay,
        )


def test_apply_correction_overlay_rejects_duplicate_source_segment_ids() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    source_artifact = _merged_transcript_fixture()
    first, second = source_artifact.segments
    duplicate_source = replace(
        source_artifact,
        segments=(first, replace(second, id=first.id)),
    )
    source_record = _persist_source_transcript(store, artifact=duplicate_source)
    overlay = create_correction_overlay(
        session_id=SESSION_ID,
        source_transcript_artifact=ArtifactRef.from_record(source_record),
        entries=(),
        now=NOW,
    )

    with pytest.raises(CorrectionOverlayContractError, match="segment ids must be unique"):
        apply_correction_overlay(
            source_transcript=duplicate_source,
            source_transcript_record=source_record,
            overlay=overlay,
        )


def test_apply_correction_overlay_revalidates_overlay_contract() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    source_artifact = _merged_transcript_fixture()
    source_record = _persist_source_transcript(store, artifact=source_artifact)
    overlay = _overlay_fixture(source_record=source_record)
    invalid_overlay = replace(
        overlay,
        entries=(
            replace(
                overlay.entries[0],
                correction_type="bad_type",
            ),
        ),
    )

    with pytest.raises(CorrectionOverlayContractError, match="correction_type"):
        apply_correction_overlay(
            source_transcript=source_artifact,
            source_transcript_record=source_record,
            overlay=invalid_overlay,
        )


def test_persist_correction_overlay_validates_contract_before_writing() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    source_record = _persist_source_transcript(store)
    overlay = _overlay_fixture(source_record=source_record)
    invalid = CorrectionOverlayArtifact(
        version=99,
        session_id=overlay.session_id,
        source_transcript_artifact=overlay.source_transcript_artifact,
        review_queue_artifact=None,
        entries=overlay.entries,
        created_at=overlay.created_at,
    )

    with pytest.raises(CorrectionOverlayContractError, match="Unsupported correction overlay"):
        persist_correction_overlay_artifact(store=store, artifact=invalid, name="bad.json")

    assert store.list(artifact_type=CORRECTION_OVERLAY_ARTIFACT_TYPE) == ()


def test_correction_overlay_rejects_missing_review_queue_artifact_field() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    source_record = _persist_source_transcript(store)
    payload = _overlay_fixture(source_record=source_record).to_dict()
    del payload["review_queue_artifact"]

    with pytest.raises(CorrectionOverlayContractError, match="review_queue_artifact is required"):
        CorrectionOverlayArtifact.from_dict(payload)


def test_correction_overlay_rejects_invalid_session_id_and_created_at() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    source_record = _persist_source_transcript(store)
    payload = _overlay_fixture(source_record=source_record).to_dict()
    payload["session_id"] = "not-a-uuid"

    with pytest.raises(CorrectionOverlayContractError, match="must be a UUID"):
        CorrectionOverlayArtifact.from_dict(payload)

    payload = _overlay_fixture(source_record=source_record).to_dict()
    payload["created_at"] = "2026-05-19 15:20:00"

    with pytest.raises(CorrectionOverlayContractError, match="timezone"):
        CorrectionOverlayArtifact.from_dict(payload)


def test_correction_overlay_wraps_artifact_ref_parse_errors() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    source_record = _persist_source_transcript(store)
    payload = _overlay_fixture(source_record=source_record).to_dict()
    cast(dict[str, object], payload["source_transcript_artifact"])["sha256"] = "bad"

    with pytest.raises(CorrectionOverlayContractError, match="source_transcript_artifact"):
        CorrectionOverlayArtifact.from_dict(payload)


def test_correction_overlay_rejects_unknown_top_level_fields() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    source_record = _persist_source_transcript(store)
    payload = _overlay_fixture(source_record=source_record).to_dict()
    payload["unexpected"] = True

    with pytest.raises(CorrectionOverlayContractError, match="unknown fields"):
        CorrectionOverlayArtifact.from_dict(payload)


def test_correction_overlay_schema_rejects_missing_payload_field() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    source_record = _persist_source_transcript(store)
    payload = _overlay_fixture(source_record=source_record).to_dict()
    del cast(dict[str, object], cast(list[object], payload["entries"])[0])["speaker_id"]
    validator = Draft202012Validator(
        _load_correction_overlay_schema(),
        format_checker=FormatChecker(),
    )

    with pytest.raises(ValidationError, match="not valid under any of the given schemas"):
        validator.validate(payload)


def test_correction_overlay_schema_rejects_non_transcript_source_ref() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    source_record = _persist_source_transcript(store)
    payload = _overlay_fixture(source_record=source_record).to_dict()
    cast(dict[str, object], payload["source_transcript_artifact"])["artifact_type"] = "source_audio"
    validator = Draft202012Validator(
        _load_correction_overlay_schema(),
        format_checker=FormatChecker(),
    )

    with pytest.raises(ValidationError, match="'merged_transcript' was expected"):
        validator.validate(payload)


def test_correction_overlay_schema_is_valid() -> None:
    Draft202012Validator.check_schema(_load_correction_overlay_schema())


def test_resolve_active_correction_overlay_prefers_completed_overlays_and_ignores_staged() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    source_record = _persist_source_transcript(store)

    staged_overlay = create_correction_overlay(
        session_id=SESSION_ID,
        source_transcript_artifact=ArtifactRef.from_record(source_record),
        entries=(),
        created_at="2026-05-19T15:20:00Z",
    )
    first_staged_record = persist_correction_overlay_artifact(
        store=store,
        artifact=staged_overlay,
        name="overlays/staged.json",
        status="staged",
        metadata={"workflow_status": "cancelled"},
    )

    completed_overlay = create_correction_overlay(
        session_id=SESSION_ID,
        source_transcript_artifact=ArtifactRef.from_record(source_record),
        entries=(),
        created_at="2026-05-19T15:21:00Z",
    )
    completed_record = persist_correction_overlay_artifact(
        store=store,
        artifact=completed_overlay,
        name="overlays/completed.json",
        status="sealed",
        metadata={"workflow_status": "completed", "completed_at": "2026-05-19T15:21:00Z"},
    )

    resolved = resolve_active_correction_overlay(
        store=store,
        source_transcript_record=source_record,
    )

    assert resolved is not None
    resolved_record, _ = resolved
    assert resolved_record.id == completed_record.id
    assert resolved_record.id != first_staged_record.id


def test_resolve_active_correction_overlay_returns_none_without_completed_overlay() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    source_record = _persist_source_transcript(store)

    persist_correction_overlay_artifact(
        store=store,
        artifact=create_correction_overlay(
            session_id=SESSION_ID,
            source_transcript_artifact=ArtifactRef.from_record(source_record),
            entries=(),
            created_at="2026-05-19T15:20:00Z",
        ),
        name="overlays/ignore.json",
        status="staged",
        metadata={"workflow_status": "cancelled"},
    )

    resolved = resolve_active_correction_overlay(
        store=store,
        source_transcript_record=source_record,
    )

    assert resolved is None


def test_resolve_active_correction_overlay_rejects_invalid_completed_at() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    source_record = _persist_source_transcript(store)
    source_ref = ArtifactRef.from_record(source_record)

    invalid_overlay = create_correction_overlay(
        session_id=SESSION_ID,
        source_transcript_artifact=source_ref,
        entries=(),
        created_at="2026-05-19T15:20:00Z",
    )
    persist_correction_overlay_artifact(
        store=store,
        artifact=invalid_overlay,
        name="overlays/invalid-completed-at.json",
        status="sealed",
        metadata={"workflow_status": "completed", "completed_at": "not-a-timestamp"},
    )

    older_overlay = create_correction_overlay(
        session_id=SESSION_ID,
        source_transcript_artifact=source_ref,
        entries=(),
        created_at="2026-05-19T15:00:00Z",
    )
    _ = persist_correction_overlay_artifact(
        store=store,
        artifact=older_overlay,
        name="overlays/older.json",
        status="sealed",
        metadata={"workflow_status": "completed", "completed_at": "2026-05-19T15:10:00Z"},
    )

    with pytest.raises(CorrectionOverlayContractError, match="completed_at metadata"):
        resolve_active_correction_overlay(
            store=store,
            source_transcript_record=source_record,
        )


def test_resolve_active_correction_overlay_rejects_invalid_completed_payload() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    source_record = _persist_source_transcript(store)
    store.store(
        artifact_type=CORRECTION_OVERLAY_ARTIFACT_TYPE,
        name="overlays/invalid.json",
        data=b"not-json",
        version=1,
        status="sealed",
        metadata={
            "workflow_status": "completed",
            "completed_at": "2026-05-19T15:22:00Z",
            "source_transcript_artifact_id": source_record.id,
        },
    )

    valid_overlay = create_correction_overlay(
        session_id=SESSION_ID,
        source_transcript_artifact=ArtifactRef.from_record(source_record),
        entries=(),
        created_at="2026-05-19T15:20:00Z",
    )
    persist_correction_overlay_artifact(
        store=store,
        artifact=valid_overlay,
        name="overlays/valid.json",
        status="sealed",
        metadata={"workflow_status": "completed", "completed_at": "2026-05-19T15:20:00Z"},
    )

    with pytest.raises(CorrectionOverlayContractError, match="could not be decoded"):
        resolve_active_correction_overlay(
            store=store,
            source_transcript_record=source_record,
        )


def test_resolve_active_correction_overlay_prefers_newest_completed_at() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    source_record = _persist_source_transcript(store)

    older_overlay = create_correction_overlay(
        session_id=SESSION_ID,
        source_transcript_artifact=ArtifactRef.from_record(source_record),
        entries=(),
        created_at="2026-05-19T15:20:00Z",
    )
    persist_correction_overlay_artifact(
        store=store,
        artifact=older_overlay,
        name="overlays/older.json",
        status="sealed",
        metadata={"workflow_status": "completed", "completed_at": "2026-05-19T15:20:01Z"},
    )

    newer_overlay = create_correction_overlay(
        session_id=SESSION_ID,
        source_transcript_artifact=ArtifactRef.from_record(source_record),
        entries=(),
        created_at="2026-05-19T15:19:00Z",
    )
    newer_record = persist_correction_overlay_artifact(
        store=store,
        artifact=newer_overlay,
        name="overlays/newer.json",
        status="sealed",
        metadata={"workflow_status": "completed", "completed_at": "2026-05-19T15:21:01Z"},
    )

    resolved = resolve_active_correction_overlay(
        store=store,
        source_transcript_record=source_record,
    )

    assert resolved is not None
    resolved_record, _ = resolved
    assert resolved_record.id == newer_record.id


def test_resolve_active_correction_overlay_tiebreaks_by_sha_and_id() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    source_record = _persist_source_transcript(store)
    source_ref = ArtifactRef.from_record(source_record)

    first_overlay = create_correction_overlay(
        session_id=SESSION_ID,
        source_transcript_artifact=source_ref,
        entries=(
            CorrectionEntry(
                id="entry_alpha",
                segment_id="segment_1",
                correction_type="text_edit",
                original_flag_id="flag_alpha",
                user_action_at="2026-05-19T15:20:00Z",
                text="alpha",
                metadata={},
            ),
        ),
        created_at="2026-05-19T15:20:00Z",
    )
    first_record = persist_correction_overlay_artifact(
        store=store,
        artifact=first_overlay,
        name="overlays/alpha.json",
        status="sealed",
        metadata={"workflow_status": "completed", "completed_at": "2026-05-19T15:20:00Z"},
    )

    second_overlay = create_correction_overlay(
        session_id=SESSION_ID,
        source_transcript_artifact=source_ref,
        entries=(
            CorrectionEntry(
                id="entry_bravo",
                segment_id="segment_2",
                correction_type="text_edit",
                original_flag_id="flag_bravo",
                user_action_at="2026-05-19T15:20:00Z",
                text="bravo",
                metadata={},
            ),
        ),
        created_at="2026-05-19T15:20:00Z",
    )
    second_record = persist_correction_overlay_artifact(
        store=store,
        artifact=second_overlay,
        name="overlays/bravo.json",
        status="sealed",
        metadata={"workflow_status": "completed", "completed_at": "2026-05-19T15:20:00Z"},
    )

    resolved = resolve_active_correction_overlay(
        store=store,
        source_transcript_record=source_record,
    )

    assert resolved is not None
    resolved_record, _ = resolved
    expected = max((first_record, second_record), key=lambda record: (record.sha256, record.id))
    assert resolved_record.id == expected.id


def _persist_source_transcript(
    store: ArtifactStore,
    artifact: MergedTranscriptArtifact | None = None,
) -> ArtifactRecord:
    return persist_merged_transcript_artifact(
        store=store,
        artifact=artifact or _merged_transcript_fixture(),
        name="source.json",
    )


def _overlay_fixture(*, source_record: ArtifactRecord) -> CorrectionOverlayArtifact:
    source_ref = ArtifactRef.from_record(source_record)
    return create_correction_overlay(
        session_id=SESSION_ID,
        source_transcript_artifact=source_ref,
        entries=(
            CorrectionEntry(
                id="entry_speaker",
                segment_id="segment_1",
                correction_type="speaker_reassignment",
                original_flag_id="flag_1",
                user_action_at="2026-05-19T15:20:00Z",
                speaker_id="speaker_alice",
                speaker_label="Alice",
                metadata={"reason": "user_selected_candidate"},
            ),
            CorrectionEntry(
                id="entry_text",
                segment_id="segment_2",
                correction_type="text_edit",
                original_flag_id="flag_2",
                user_action_at="2026-05-19T15:21:00Z",
                text="corrected remote reply",
                metadata={},
            ),
            CorrectionEntry(
                id="entry_overlap",
                segment_id="segment_1",
                correction_type="overlap_mark",
                original_flag_id=None,
                user_action_at="2026-05-19T15:22:00Z",
                overlap_detected=True,
                metadata={},
            ),
            CorrectionEntry(
                id="entry_skip",
                segment_id="segment_2",
                correction_type="skip_mark",
                original_flag_id="flag_3",
                user_action_at="2026-05-19T15:23:00Z",
                metadata={},
            ),
        ),
        now=NOW,
    )


def _merged_transcript_fixture() -> MergedTranscriptArtifact:
    source_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/transcript/source.json",
        artifact_type="transcript",
        version=1,
        sha256="0" * 64,
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
                    ),
                ),
                transcription_confidence=SegmentConfidence(
                    status="available",
                    metric="word_probability",
                    value=0.99,
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
                ),
                overlap_detected=False,
                transcript_segment_id="remote_segment_1",
                metadata={},
            ),
        ),
        created_at="2026-05-19T15:00:00Z",
    )


def _load_correction_overlay_schema() -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads(
            files("mena.schemas")
            .joinpath("correction-overlay.v1.schema.json")
            .read_text(encoding="utf-8")
        ),
    )
