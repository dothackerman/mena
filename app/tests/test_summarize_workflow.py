import json
from pathlib import Path

import pytest

from mena.artifacts import FileSystemArtifactStore, InMemoryArtifactStore
from mena.merge_timing import MERGED_TRANSCRIPT_ARTIFACT_TYPE
from mena.sessions import resolve_session, session_language_or_default
from mena.summarize_workflow import (
    _merged_transcript_timestamp_key,
    _parse_iso_timestamp,
    _resolve_latest_merged_transcript_record,
)

SESSION_ID = "00000000-0000-0000-0000-000000000001"


def test_summarize_workflow_resolves_session_directory_and_record_path(
    tmp_path: Path,
) -> None:
    record: dict[str, object] = {"id": SESSION_ID, "language": "de-CH"}
    session_file = tmp_path / "session.json"
    session_file.write_text(json.dumps(record), encoding="utf-8")

    resolved_from_dir = resolve_session(tmp_path)
    resolved_from_file = resolve_session(session_file)
    assert resolved_from_dir.session_dir == tmp_path
    assert resolved_from_dir.record == record
    assert resolved_from_file.session_dir == tmp_path
    assert resolved_from_file.record == record
    assert session_language_or_default(record, "en") == "de-CH"
    assert session_language_or_default({"id": SESSION_ID}, "en") == "en"


def test_summarize_workflow_rejects_invalid_session_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Session path must be"):
        resolve_session(tmp_path / "missing.json")

    bad_session = tmp_path / "session.json"
    bad_session.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="Cannot read session record"):
        resolve_session(bad_session)


def test_summarize_workflow_filters_invalid_merged_transcripts(tmp_path: Path) -> None:
    memory_store = InMemoryArtifactStore(session_id=SESSION_ID)
    invalid = memory_store.store(
        artifact_type=MERGED_TRANSCRIPT_ARTIFACT_TYPE,
        name="merged-invalid.json",
        data=b"{",
        status="sealed",
    )

    assert _parse_iso_timestamp("") is None
    assert _parse_iso_timestamp("not-a-date") is None
    assert _merged_transcript_timestamp_key(store=memory_store, record=invalid) is None
    assert _resolve_latest_merged_transcript_record(store=memory_store) is None

    file_store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    corrupt = file_store.store(
        artifact_type=MERGED_TRANSCRIPT_ARTIFACT_TYPE,
        name="merged-corrupt.json",
        data=b"{",
        status="sealed",
    )
    (tmp_path / corrupt.path).write_bytes(b"changed")

    assert _merged_transcript_timestamp_key(store=file_store, record=corrupt) is None
