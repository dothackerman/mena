import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from mena.artifacts import ArtifactRecord, FileSystemArtifactStore, session_artifact_manifest_path
from mena.exportable_summary import (
    ExportableSummaryError,
    resolve_exportable_summary,
    resolve_exportable_summary_session,
)
from mena.merge_timing import MERGED_TRANSCRIPT_ARTIFACT_TYPE
from mena.ollama_summarization import (
    OLLAMA_SUMMARIZATION_NUM_CTX,
    OLLAMA_SUMMARIZATION_SEED,
)
from mena.processing import ArtifactRef
from mena.sessions import SESSION_RECORD_FILENAME, SESSION_RECORD_VERSION
from mena.summarization import (
    REQUIRED_NOTE_PRODUCTS,
    SUMMARY_ARTIFACT_VERSION,
    FakeSummarizationProvider,
    ProductRequestRecord,
    SummarizationPolicy,
    SummaryArtifact,
    SummaryResult,
    SummaryTimelineEntry,
    persist_summary_artifact,
)

SESSION_ID = "00000000-0000-0000-0000-000000000001"
OTHER_SESSION_ID = "00000000-0000-0000-0000-000000000002"


def test_resolve_exportable_summary_prefers_latest_payload_timestamp(
    tmp_path: Path,
) -> None:
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    _persist_summary(store=store, created_at="2026-05-18T17:00:00Z", name="summary/old.json")
    new_record = _persist_summary(
        store=store,
        created_at="2026-05-20T17:00:00Z",
        name="summary/new.json",
    )

    selected = resolve_exportable_summary(store=store)

    assert selected.record.id == new_record.id
    assert selected.summary.created_at == "2026-05-20T17:00:00Z"


def test_resolve_exportable_summary_skips_incompatible_payloads(
    tmp_path: Path,
) -> None:
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    valid_record = _persist_summary(
        store=store,
        created_at="2026-05-18T17:00:00Z",
        name="summary/valid.json",
    )
    store.store(
        artifact_type="summary",
        name="summary/new-invalid.json",
        data=b"not-json",
        version=1,
        status="sealed",
    )

    selected = resolve_exportable_summary(store=store)

    assert selected.record.id == valid_record.id


def test_resolve_exportable_summary_rejects_corrupt_current_payload(
    tmp_path: Path,
) -> None:
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    _persist_summary(store=store, created_at="2026-05-18T17:00:00Z", name="summary/valid.json")
    store.store(
        artifact_type="summary",
        name="summary/new-corrupt-current.json",
        data=b"not-json",
        version=SUMMARY_ARTIFACT_VERSION,
        status="sealed",
    )

    with pytest.raises(ExportableSummaryError, match="not valid JSON"):
        resolve_exportable_summary(store=store)


def test_resolve_exportable_summary_session_uses_payload_timestamp_over_manifest_metadata(
    tmp_path: Path,
) -> None:
    stale_dir, stale_store, _ = _build_export_session(tmp_path / "session-stale-metadata")
    stale_summary = _persist_summary(
        store=stale_store,
        created_at="2026-05-20T10:00:00Z",
    )
    _update_artifact_manifest_record(
        stale_dir,
        artifact_id=stale_summary.id,
        metadata_updates={"summary_created_at": "2026-07-03T09:00:00Z"},
    )
    current_dir, current_store, _ = _build_export_session(tmp_path / "session-current")
    _persist_summary(store=current_store, created_at="2026-06-15T10:00:00Z")

    selected = resolve_exportable_summary_session(sessions_root=tmp_path)

    assert selected is not None
    assert selected.session_dir == current_dir


def test_resolve_exportable_summary_session_ignores_copied_requested_id(
    tmp_path: Path,
) -> None:
    target_dir, target_store, _ = _build_export_session(tmp_path / "session-target")
    requested_summary = _persist_summary(store=target_store, name="summary/requested.json")
    stale_dir, stale_store, _ = _build_export_session(
        tmp_path / "session-stale-copy",
        session_id_value=OTHER_SESSION_ID,
    )
    stale_summary = _persist_summary(store=stale_store, name="summary/copied.json")
    _update_artifact_manifest_record(
        stale_dir,
        artifact_id=stale_summary.id,
        updates={
            "id": requested_summary.id,
            "session_id": SESSION_ID,
        },
    )

    selected = resolve_exportable_summary_session(
        sessions_root=tmp_path,
        summary_artifact_id=requested_summary.id,
    )

    assert selected is not None
    assert selected.session_dir == target_dir
    assert selected.summary_artifact_id == requested_summary.id


def test_resolve_exportable_summary_session_rejects_fabricated_requested_id(
    tmp_path: Path,
) -> None:
    unsummarized_dir = _create_session_record(tmp_path / "session-unsummarized")
    fabricated_summary_id = f"{SESSION_ID}:artifacts/summary/missing.json"

    selected = resolve_exportable_summary_session(
        sessions_root=tmp_path,
        summary_artifact_id=fabricated_summary_id,
    )

    assert selected is None
    assert not (unsummarized_dir / "artifacts").exists()


def test_resolve_exportable_summary_session_preserves_corrupt_newest_selection(
    tmp_path: Path,
) -> None:
    older_dir, older_store, _ = _build_export_session(tmp_path / "session-older")
    _persist_summary(store=older_store, created_at="2026-07-03T08:00:00Z")
    corrupt_dir, corrupt_store, _ = _build_export_session(tmp_path / "session-newer-corrupt")
    corrupt_summary = _persist_summary(store=corrupt_store, created_at="2026-07-03T09:00:00Z")
    (corrupt_dir / corrupt_summary.path).write_text("{", encoding="utf-8")

    selected = resolve_exportable_summary_session(sessions_root=tmp_path)

    assert selected is not None
    assert selected.session_dir == corrupt_dir
    assert selected.session_dir != older_dir
    assert selected.summary_artifact_id == corrupt_summary.id


def test_resolve_exportable_summary_session_skips_incompatible_payload(
    tmp_path: Path,
) -> None:
    valid_dir, valid_store, _ = _build_export_session(tmp_path / "session-older-valid")
    _persist_summary(store=valid_store, created_at="2026-07-03T08:00:00Z")
    incompatible_dir, incompatible_store, _ = _build_export_session(
        tmp_path / "session-newer-incompatible"
    )
    incompatible_summary = _persist_summary(
        store=incompatible_store,
        created_at="2026-07-03T09:00:00Z",
    )
    payload_path = incompatible_dir / incompatible_summary.path
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    del payload["request_records"]
    payload_bytes = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    payload_path.write_bytes(payload_bytes)
    _update_artifact_manifest_record(
        incompatible_dir,
        artifact_id=incompatible_summary.id,
        updates={
            "sha256": hashlib.sha256(payload_bytes).hexdigest(),
            "byte_count": len(payload_bytes),
        },
    )

    selected = resolve_exportable_summary_session(sessions_root=tmp_path)

    assert selected is not None
    assert selected.session_dir == valid_dir


def test_resolve_exportable_summary_session_does_not_mutate_unsummarized_sessions(
    tmp_path: Path,
) -> None:
    unsummarized_dir = _create_session_record(tmp_path / "session-unsummarized")
    valid_dir, valid_store, _ = _build_export_session(tmp_path / "session-valid")
    _persist_summary(store=valid_store, created_at="2026-07-03T09:00:00Z")

    selected = resolve_exportable_summary_session(sessions_root=tmp_path)

    assert selected is not None
    assert selected.session_dir == valid_dir
    assert not (unsummarized_dir / "artifacts").exists()


def _build_export_session(
    session_dir: Path,
    *,
    session_id_value: str = SESSION_ID,
) -> tuple[Path, FileSystemArtifactStore, ArtifactRecord]:
    session_dir = _create_session_record(session_dir, session_id_value=session_id_value)
    store = FileSystemArtifactStore(session_id=session_id_value, session_dir=session_dir)
    merged_record = store.store(
        artifact_type=MERGED_TRANSCRIPT_ARTIFACT_TYPE,
        name="merged/merged.json",
        data=b'{"version":1}',
        version=1,
        status="sealed",
        metadata={"contract": "merged-transcript.v1"},
    )
    return session_dir, store, merged_record


def _create_session_record(session_dir: Path, *, session_id_value: str = SESSION_ID) -> Path:
    session_dir.mkdir(parents=True)
    (session_dir / SESSION_RECORD_FILENAME).write_text(
        json.dumps(
            {
                "version": SESSION_RECORD_VERSION,
                "id": session_id_value,
                "created_at": "2026-05-19T15:00:00Z",
                "name": None,
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
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return session_dir


def _persist_summary(
    *,
    store: FileSystemArtifactStore,
    created_at: str = "2026-05-20T10:00:00Z",
    name: str = "summary/final.json",
) -> ArtifactRecord:
    summary_result = SummaryResult(
        timeline=(
            SummaryTimelineEntry(
                text="Agenda covered.",
                source_segment_ids=("merged_segment_000001",),
                start_seconds=0.0,
            ),
        ),
        action_items=(),
        general_summary="The team aligned on the plan.",
        request_records=_request_records_for_products(REQUIRED_NOTE_PRODUCTS),
    )
    merged_record = store.store(
        artifact_type=MERGED_TRANSCRIPT_ARTIFACT_TYPE,
        name=f"merged/{name.replace('/', '_')}.json",
        data=b'{"version":1}',
        version=1,
        status="sealed",
        metadata={"contract": "merged-transcript.v1"},
    )
    return persist_summary_artifact(
        store=store,
        artifact=SummaryArtifact(
            version=SUMMARY_ARTIFACT_VERSION,
            session_id=store.session_id,
            merged_transcript_artifact=ArtifactRef.from_record(merged_record),
            manual_notes_artifact=None,
            policy=SummarizationPolicy(language="en").to_dict(),
            provider=FakeSummarizationProvider(result=summary_result).metadata,
            timeline=summary_result.timeline,
            action_items=summary_result.action_items,
            general_summary=summary_result.general_summary,
            created_at=created_at,
            request_records=summary_result.request_records,
        ),
        name=name,
        correction_overlay_record=None,
    )


def _request_records_for_products(products: tuple[str, ...]) -> tuple[ProductRequestRecord, ...]:
    return tuple(
        ProductRequestRecord(
            product=product,
            model="fake-summarizer",
            num_ctx=OLLAMA_SUMMARIZATION_NUM_CTX,
            temperature=0,
            seed=OLLAMA_SUMMARIZATION_SEED,
            prompt_eval_count=None,
            eval_count=None,
        )
        for product in products
    )


def _update_artifact_manifest_record(
    session_dir: Path,
    *,
    artifact_id: str,
    updates: Mapping[str, object] | None = None,
    metadata_updates: Mapping[str, object | None] | None = None,
) -> None:
    manifest_path = session_artifact_manifest_path(session_dir)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for item in manifest["artifacts"]:
        if item["id"] != artifact_id:
            continue
        if updates is not None:
            for key, value in updates.items():
                if value is None:
                    item.pop(key, None)
                else:
                    item[key] = value
        if metadata_updates is not None:
            metadata = item.setdefault("metadata", {})
            for key, value in metadata_updates.items():
                if value is None:
                    metadata.pop(key, None)
                else:
                    metadata[key] = value
        break
    else:
        raise AssertionError(f"Artifact record not found: {artifact_id}")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
