import json
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from mena.artifacts import (
    ARTIFACT_MANIFEST_FILENAME,
    ARTIFACT_MANIFEST_VERSION,
    ArtifactStore,
    ArtifactStoreError,
    FileSystemArtifactStore,
    InMemoryArtifactStore,
)

SESSION_ID = "00000000-0000-0000-0000-000000000001"
StoreFactory = Callable[[Path], ArtifactStore]


def _memory_store(_tmp_path: Path) -> ArtifactStore:
    return InMemoryArtifactStore(session_id=SESSION_ID)


def _file_system_store(tmp_path: Path) -> ArtifactStore:
    return FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)


@pytest.mark.parametrize("store_factory", [_memory_store, _file_system_store])
def test_artifact_store_contract_stores_retrieves_lists_and_verifies(
    tmp_path: Path,
    store_factory: StoreFactory,
) -> None:
    store = store_factory(tmp_path)

    record = store.store(
        artifact_type="transcript",
        name="segments/local.json",
        data=b'{"text":"hello"}',
        metadata={"source": "local_mic"},
    )

    assert record.id == f"{SESSION_ID}:artifacts/transcript/segments/local.json"
    assert record.session_id == SESSION_ID
    assert record.artifact_type == "transcript"
    assert record.version == 1
    assert record.path == "artifacts/transcript/segments/local.json"
    assert record.byte_count == 16
    assert record.status == "sealed"
    assert record.metadata == {"source": "local_mic"}
    assert store.retrieve(record.id) == b'{"text":"hello"}'
    assert store.list() == (record,)
    assert store.list(artifact_type="transcript") == (record,)
    assert store.list(artifact_type="summary") == ()
    assert store.verify(record.id).to_dict() == {
        "artifact_id": record.id,
        "path": record.path,
        "ok": True,
        "expected_sha256": record.sha256,
        "actual_sha256": record.sha256,
        "expected_byte_count": record.byte_count,
        "actual_byte_count": record.byte_count,
        "status": "sealed",
        "error": None,
    }


def test_file_system_artifact_store_writes_manifest_matching_v1_schema(tmp_path: Path) -> None:
    store = _file_system_store(tmp_path)

    store.store(artifact_type="source_audio", name="local_mic.wav", data=b"audio")
    manifest_path = tmp_path / "artifacts" / ARTIFACT_MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(
        _load_artifact_manifest_schema(),
        format_checker=FormatChecker(),
    )

    assert manifest["version"] == ARTIFACT_MANIFEST_VERSION
    assert manifest["session_id"] == SESSION_ID
    validator.validate(manifest)


def test_artifact_manifest_schema_rejects_incomplete_records() -> None:
    manifest = {
        "version": 1,
        "session_id": SESSION_ID,
        "artifacts": [
            {
                "id": "artifact-1",
                "session_id": SESSION_ID,
                "artifact_type": "transcript",
            }
        ],
    }
    validator = Draft202012Validator(
        _load_artifact_manifest_schema(),
        format_checker=FormatChecker(),
    )

    with pytest.raises(ValidationError, match="'version' is a required property"):
        validator.validate(manifest)


def test_artifact_manifest_schema_is_valid() -> None:
    Draft202012Validator.check_schema(_load_artifact_manifest_schema())


@pytest.mark.parametrize("store_factory", [_memory_store, _file_system_store])
def test_artifact_store_rejects_path_traversal(
    tmp_path: Path,
    store_factory: StoreFactory,
) -> None:
    store = store_factory(tmp_path)

    with pytest.raises(ArtifactStoreError, match="parent path"):
        store.store(artifact_type="transcript", name="../escape.json", data=b"")


@pytest.mark.parametrize("store_factory", [_memory_store, _file_system_store])
def test_artifact_store_refuses_to_overwrite_existing_artifact(
    tmp_path: Path,
    store_factory: StoreFactory,
) -> None:
    store = store_factory(tmp_path)

    store.store(artifact_type="transcript", name="same.json", data=b"first")

    with pytest.raises(FileExistsError, match="Artifact already exists"):
        store.store(artifact_type="transcript", name="same.json", data=b"second")


def test_file_system_artifact_store_detects_corrupt_artifact(tmp_path: Path) -> None:
    store = _file_system_store(tmp_path)
    record = store.store(artifact_type="transcript", name="same.json", data=b"first")

    (tmp_path / record.path).write_bytes(b"changed")
    verification = store.verify(record.id)

    assert verification.ok is False
    assert verification.actual_byte_count == 7
    assert verification.error == "artifact content does not match metadata"


def test_file_system_artifact_store_detects_missing_artifact(tmp_path: Path) -> None:
    store = _file_system_store(tmp_path)
    record = store.store(artifact_type="transcript", name="same.json", data=b"first")

    (tmp_path / record.path).unlink()
    verification = store.verify(record.id)

    assert verification.ok is False
    assert verification.actual_sha256 is None
    assert verification.actual_byte_count is None
    assert verification.error == "artifact file is missing"


def test_file_system_artifact_store_reads_existing_manifest(tmp_path: Path) -> None:
    writer = _file_system_store(tmp_path)
    record = writer.store(artifact_type="transcript", name="same.json", data=b"first")
    reader = _file_system_store(tmp_path)

    assert reader.list() == (record,)
    assert reader.retrieve(record.id) == b"first"


def test_file_system_artifact_store_rejects_manifest_record_id_drift(tmp_path: Path) -> None:
    store = _file_system_store(tmp_path)
    store.store(artifact_type="transcript", name="same.json", data=b"first")
    manifest_path = tmp_path / "artifacts" / ARTIFACT_MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"][0]["id"] = "wrong"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    reader = _file_system_store(tmp_path)

    with pytest.raises(ArtifactStoreError, match="id must match"):
        reader.list()


def test_file_system_artifact_store_rejects_manifest_path_escape(tmp_path: Path) -> None:
    store = _file_system_store(tmp_path)
    store.store(artifact_type="transcript", name="same.json", data=b"first")
    manifest_path = tmp_path / "artifacts" / ARTIFACT_MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"][0]["path"] = "../escape.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    reader = _file_system_store(tmp_path)

    with pytest.raises(ArtifactStoreError, match="artifacts directory"):
        reader.list()


def test_file_system_artifact_store_rejects_cross_session_manifest_record(
    tmp_path: Path,
) -> None:
    store = _file_system_store(tmp_path)
    store.store(artifact_type="transcript", name="same.json", data=b"first")
    manifest_path = tmp_path / "artifacts" / ARTIFACT_MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"][0]["session_id"] = "00000000-0000-0000-0000-000000000002"
    manifest["artifacts"][0]["id"] = (
        "00000000-0000-0000-0000-000000000002:artifacts/transcript/same.json"
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    reader = _file_system_store(tmp_path)

    with pytest.raises(ArtifactStoreError, match="session_id"):
        reader.list()


def test_file_system_artifact_store_rejects_duplicate_manifest_ids(tmp_path: Path) -> None:
    store = _file_system_store(tmp_path)
    store.store(artifact_type="transcript", name="same.json", data=b"first")
    manifest_path = tmp_path / "artifacts" / ARTIFACT_MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"].append(dict(manifest["artifacts"][0]))
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    reader = _file_system_store(tmp_path)

    with pytest.raises(ArtifactStoreError, match="Duplicate artifact id"):
        reader.list()


def test_file_system_artifact_store_rejects_boolean_integer_fields(tmp_path: Path) -> None:
    store = _file_system_store(tmp_path)
    store.store(artifact_type="transcript", name="same.json", data=b"first")
    manifest_path = tmp_path / "artifacts" / ARTIFACT_MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"][0]["version"] = True
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    reader = _file_system_store(tmp_path)

    with pytest.raises(ArtifactStoreError, match="integer"):
        reader.list()


def test_file_system_artifact_store_rejects_non_json_metadata_before_writing(
    tmp_path: Path,
) -> None:
    store = _file_system_store(tmp_path)

    with pytest.raises(ArtifactStoreError, match="JSON serializable"):
        store.store(
            artifact_type="transcript",
            name="same.json",
            data=b"first",
            metadata={"bad": {"not-json"}},
        )

    assert not (tmp_path / "artifacts" / "transcript" / "same.json").exists()
    assert store.list() == ()


def test_file_system_artifact_store_rolls_back_artifact_when_manifest_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)

    def fail_write(_records: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, "_write_records", fail_write)

    with pytest.raises(OSError, match="disk full"):
        store.store(artifact_type="transcript", name="same.json", data=b"first")

    assert not (tmp_path / "artifacts" / "transcript" / "same.json").exists()


def test_artifact_store_rejects_invalid_artifact_type(tmp_path: Path) -> None:
    store = _file_system_store(tmp_path)

    with pytest.raises(ArtifactStoreError, match="Artifact type"):
        store.store(artifact_type="Transcript", name="same.json", data=b"first")


def test_artifact_store_rejects_invalid_status(tmp_path: Path) -> None:
    store = _file_system_store(tmp_path)

    with pytest.raises(ArtifactStoreError, match="Artifact status"):
        store.store(artifact_type="transcript", name="same.json", data=b"first", status="done")


def test_artifact_store_rejects_invalid_version(tmp_path: Path) -> None:
    store = _file_system_store(tmp_path)

    with pytest.raises(ArtifactStoreError, match="version"):
        store.store(artifact_type="transcript", name="same.json", data=b"first", version=0)


def test_file_system_artifact_store_rejects_manifest_session_mismatch(tmp_path: Path) -> None:
    store = _file_system_store(tmp_path)
    store.store(artifact_type="transcript", name="same.json", data=b"first")
    manifest_path = tmp_path / "artifacts" / ARTIFACT_MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["session_id"] = "00000000-0000-0000-0000-000000000002"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    reader = _file_system_store(tmp_path)

    with pytest.raises(ArtifactStoreError, match="session_id"):
        reader.list()


def test_file_system_artifact_store_rejects_manifest_version_mismatch(tmp_path: Path) -> None:
    store = _file_system_store(tmp_path)
    store.store(artifact_type="transcript", name="same.json", data=b"first")
    manifest_path = tmp_path / "artifacts" / ARTIFACT_MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = 99
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    reader = _file_system_store(tmp_path)

    with pytest.raises(ArtifactStoreError, match="version"):
        reader.list()


def test_file_system_artifact_store_raises_for_unknown_artifact_id(tmp_path: Path) -> None:
    store = _file_system_store(tmp_path)

    with pytest.raises(KeyError, match="Unknown artifact id"):
        store.retrieve("missing")


def _load_artifact_manifest_schema() -> dict[str, object]:
    schema_path = files("mena.schemas").joinpath("artifact-manifest.v1.schema.json")
    return cast(dict[str, object], json.loads(schema_path.read_text(encoding="utf-8")))
