from pathlib import Path

import pytest

from mena.artifacts import InMemoryArtifactStore
from mena.manual_notes import ManualNotesIngestionError, ingest_manual_notes_file
from mena.summarization import MANUAL_NOTES_ARTIFACT_TYPE, MANUAL_NOTES_CONTRACT

SESSION_ID = "00000000-0000-0000-0000-000000000001"


def test_manual_notes_ingestion_absent_does_not_create_artifact() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)

    record = ingest_manual_notes_file(store=store, notes_path=None)

    assert record is None
    assert store.list(artifact_type=MANUAL_NOTES_ARTIFACT_TYPE) == ()


def test_manual_notes_ingestion_persists_raw_utf8_file(tmp_path: Path) -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    notes_path = tmp_path / "meeting notes.md"
    notes_path.write_text("# Notes\n\n- Release is blocked.\n", encoding="utf-8")

    record = ingest_manual_notes_file(store=store, notes_path=notes_path)

    assert record is not None
    assert record.artifact_type == MANUAL_NOTES_ARTIFACT_TYPE
    assert record.version == 1
    assert record.metadata["contract"] == MANUAL_NOTES_CONTRACT
    assert record.path.startswith("artifacts/manual_notes/raw/meeting-notes-")
    assert store.retrieve(record.id) == b"# Notes\n\n- Release is blocked.\n"


def test_manual_notes_ingestion_reuses_existing_artifact_for_same_content(tmp_path: Path) -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    notes_path = tmp_path / "notes.md"
    notes_path.write_text("- Release is blocked.\n", encoding="utf-8")

    first = ingest_manual_notes_file(store=store, notes_path=notes_path)
    second = ingest_manual_notes_file(store=store, notes_path=notes_path)

    assert second == first
    assert len(store.list(artifact_type=MANUAL_NOTES_ARTIFACT_TYPE)) == 1


def test_manual_notes_ingestion_restores_same_content_when_existing_artifact_is_stale(
    tmp_path: Path,
) -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    notes_path = tmp_path / "notes.md"
    notes_path.write_text("- Release is blocked.\n", encoding="utf-8")

    first = ingest_manual_notes_file(store=store, notes_path=notes_path)
    assert first is not None
    store._data.pop(first.id)

    second = ingest_manual_notes_file(store=store, notes_path=notes_path)

    assert second is not None
    assert second.id != first.id
    assert store.verify(second.id).ok
    assert store.retrieve(second.id) == b"- Release is blocked.\n"


def test_manual_notes_ingestion_rejects_invalid_path(tmp_path: Path) -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)

    with pytest.raises(ManualNotesIngestionError, match="does not exist"):
        ingest_manual_notes_file(store=store, notes_path=tmp_path / "missing.md")

    assert store.list(artifact_type=MANUAL_NOTES_ARTIFACT_TYPE) == ()


def test_manual_notes_ingestion_rejects_non_file_path(tmp_path: Path) -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)

    with pytest.raises(ManualNotesIngestionError, match="must be a file"):
        ingest_manual_notes_file(store=store, notes_path=tmp_path)

    assert store.list(artifact_type=MANUAL_NOTES_ARTIFACT_TYPE) == ()


def test_manual_notes_ingestion_rejects_unreadable_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    notes_path = tmp_path / "notes.md"
    notes_path.write_text("- Notes\n", encoding="utf-8")

    def unreadable(path: Path) -> bytes:
        if path == notes_path:
            raise OSError("permission denied")
        return b""

    monkeypatch.setattr(Path, "read_bytes", unreadable)

    with pytest.raises(ManualNotesIngestionError, match="could not be read"):
        ingest_manual_notes_file(store=store, notes_path=notes_path)

    assert store.list(artifact_type=MANUAL_NOTES_ARTIFACT_TYPE) == ()


def test_manual_notes_ingestion_rejects_non_utf8_file(tmp_path: Path) -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    notes_path = tmp_path / "notes.md"
    notes_path.write_bytes(b"\xff")

    with pytest.raises(ManualNotesIngestionError, match="valid UTF-8"):
        ingest_manual_notes_file(store=store, notes_path=notes_path)

    assert store.list(artifact_type=MANUAL_NOTES_ARTIFACT_TYPE) == ()
