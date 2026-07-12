import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mena.artifacts import FileSystemArtifactStore
from mena.processing import (
    PROCESSING_STATE_ARTIFACT_TYPE,
    PROCESSING_STATE_VERSION,
    ArtifactRef,
    ProcessingErrorInfo,
    ProcessingStageLedger,
)
from mena.profiles import parse_capture_profile
from mena.session_integrity import (
    SessionIntegrityError,
    SessionIntegrityReport,
    load_verified_session_bundle,
    rebuild_session_index,
    verify_session_bundle,
)
from mena.session_lock import acquire_session_lock
from mena.sessions import CreatedSession, create_session, update_session_capture_metadata


def test_verify_session_bundle_accepts_complete_capture_and_manifest_artifacts(
    tmp_path: Path,
) -> None:
    session = _create_recorded_session(tmp_path)
    store = FileSystemArtifactStore(session_id=session.record.id, session_dir=session.bundle_path)
    source = store.store(
        artifact_type="source_audio",
        name="source_audio/local_mic_000001.wav",
        data=b"audio",
        metadata={
            "source_id": "local_mic",
            "segment_index": 1,
            "source_sha256": _sha256(b"audio"),
        },
    )
    transcript = {
        "version": 1,
        "session_id": session.record.id,
        "source_artifact": ArtifactRef.from_record(source).to_dict(),
        "text": "hello",
    }
    store.store(
        artifact_type="transcript",
        name="transcripts/local.json",
        data=json.dumps(transcript).encode("utf-8"),
    )

    report = verify_session_bundle(session.bundle_path)

    assert report.ok
    assert report.session_id == session.record.id
    assert report.artifact_count == 2
    assert report.capture_segment_count == 1
    assert report.findings == ()
    assert report.to_dict()["ok"] is True
    assert load_verified_session_bundle(session.record_path).record["id"] == session.record.id


def test_verify_session_bundle_detects_corrupt_capture_segment(tmp_path: Path) -> None:
    session = _create_recorded_session(tmp_path)
    (session.bundle_path / "artifacts" / "source" / "local_mic_000001.wav").write_bytes(b"changed")

    report = verify_session_bundle(session.bundle_path)

    assert not report.ok
    assert _codes(report) == {
        "capture_segment_byte_count_mismatch",
        "capture_segment_sha256_mismatch",
    }


def test_verify_session_bundle_detects_corrupt_manifest_artifact(tmp_path: Path) -> None:
    session = _create_recorded_session(tmp_path)
    store = FileSystemArtifactStore(session_id=session.record.id, session_dir=session.bundle_path)
    record = store.store(artifact_type="summary", name="summary.json", data=b'{"ok": true}')
    (session.bundle_path / record.path).write_bytes(b'{"ok": false}')

    report = verify_session_bundle(session.bundle_path)

    assert not report.ok
    assert "artifact_sha256_mismatch" in _codes(report)


def test_verify_session_bundle_detects_processing_state_missing_artifact_ref(
    tmp_path: Path,
) -> None:
    session = _create_recorded_session(tmp_path)
    store = FileSystemArtifactStore(session_id=session.record.id, session_dir=session.bundle_path)
    missing_ref = ArtifactRef(
        id=f"{session.record.id}:artifacts/transcript/missing.json",
        artifact_type="transcript",
        version=1,
        sha256="0" * 64,
    )
    ledger = ProcessingStageLedger(store=store, session_id=session.record.id, run_id="run-1")
    ledger.mark_failed(
        stage="summarization",
        input_artifacts=(missing_ref,),
        error=ProcessingErrorInfo(message="blocked", retryable=False),
        now=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    report = verify_session_bundle(session.bundle_path)

    assert not report.ok
    assert "artifact_ref_missing" in _codes(report)


def test_verify_session_bundle_detects_orphaned_artifact_file(tmp_path: Path) -> None:
    session = _create_recorded_session(tmp_path)
    orphan = session.bundle_path / "artifacts" / "summary" / "orphan.json"
    orphan.parent.mkdir(parents=True)
    orphan.write_text("{}", encoding="utf-8")

    report = verify_session_bundle(session.bundle_path)

    assert not report.ok
    assert "orphaned_artifact_file" in _codes(report)


def test_verify_session_bundle_ignores_orchestrator_lock_file(tmp_path: Path) -> None:
    session = _create_recorded_session(tmp_path)

    with acquire_session_lock(session_dir=session.bundle_path, command="process"):
        report = verify_session_bundle(session.bundle_path)

    assert report.ok
    assert "orphaned_artifact_file" not in _codes(report)


def test_verify_session_bundle_ignores_hidden_provider_prep_temps(tmp_path: Path) -> None:
    session = _create_recorded_session(tmp_path)
    temp_file = session.bundle_path / "artifacts" / ".provider-prep-tmp" / ".local_mic.tmp.wav"
    temp_file.parent.mkdir(parents=True)
    temp_file.write_bytes(b"interrupted provider prep")

    report = verify_session_bundle(session.bundle_path)

    assert report.ok
    assert "orphaned_artifact_file" not in _codes(report)


def test_verify_session_bundle_reports_invalid_session_record(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    (session_dir / "session.json").write_text("[]", encoding="utf-8")

    report = verify_session_bundle(session_dir)

    assert not report.ok
    assert _codes(report) == {"session_record_invalid", "session_id_missing"}


def test_verify_session_bundle_reports_malformed_capture_metadata(tmp_path: Path) -> None:
    session = create_session(
        profile=parse_capture_profile("online"),
        data_root=tmp_path,
        now=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )
    record = json.loads(session.record_path.read_text(encoding="utf-8"))
    record["capture_metadata"] = {"segment_count": 1, "segments": [{"path": ""}, "bad"]}
    session.record_path.write_text(json.dumps(record), encoding="utf-8")

    report = verify_session_bundle(session.bundle_path)

    assert not report.ok
    assert _codes(report) == {
        "capture_segment_count_mismatch",
        "capture_segment_path_invalid",
        "capture_segment_invalid",
    }


def test_verify_session_bundle_reports_capture_metadata_shape_errors(tmp_path: Path) -> None:
    session = create_session(
        profile=parse_capture_profile("online"),
        data_root=tmp_path,
        now=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )
    record = json.loads(session.record_path.read_text(encoding="utf-8"))
    record["capture_metadata"] = "bad"
    session.record_path.write_text(json.dumps(record), encoding="utf-8")

    metadata_report = verify_session_bundle(session.bundle_path)
    record["capture_metadata"] = {}
    session.record_path.write_text(json.dumps(record), encoding="utf-8")
    segments_report = verify_session_bundle(session.bundle_path)

    assert "capture_metadata_invalid" in _codes(metadata_report)
    assert "capture_segments_invalid" in _codes(segments_report)


def test_verify_session_bundle_reports_manifest_shape_errors(tmp_path: Path) -> None:
    session = _create_recorded_session(tmp_path)
    manifest_path = session.bundle_path / "artifacts" / "artifact-manifest.v1.json"
    manifest_path.write_text("[]", encoding="utf-8")

    object_report = verify_session_bundle(session.bundle_path)
    manifest_path.write_text(
        json.dumps({"version": 999, "session_id": "other", "artifacts": "bad"}),
        encoding="utf-8",
    )
    fields_report = verify_session_bundle(session.bundle_path)

    assert "artifact_manifest_invalid" in _codes(object_report)
    assert {
        "artifact_manifest_version_unsupported",
        "artifact_manifest_session_mismatch",
        "artifact_manifest_records_invalid",
    }.issubset(_codes(fields_report))


def test_verify_session_bundle_reports_invalid_manifest_records(tmp_path: Path) -> None:
    session = _create_recorded_session(tmp_path)
    store = FileSystemArtifactStore(session_id=session.record.id, session_dir=session.bundle_path)
    record = store.store(artifact_type="summary", name="summary.json", data=b"{}")
    manifest_path = session.bundle_path / "artifacts" / "artifact-manifest.v1.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"].extend(["bad", dict(manifest["artifacts"][0])])
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = verify_session_bundle(session.bundle_path)

    assert record.id in manifest["artifacts"][0]["id"]
    assert {"artifact_record_invalid", "artifact_record_duplicate"}.issubset(_codes(report))


def test_verify_session_bundle_reports_payload_and_metadata_reference_errors(
    tmp_path: Path,
) -> None:
    session = _create_recorded_session(tmp_path)
    store = FileSystemArtifactStore(session_id=session.record.id, session_dir=session.bundle_path)
    target = store.store(artifact_type="transcript", name="target.json", data=b"{}")
    bad_ref = ArtifactRef(
        id=target.id,
        artifact_type=target.artifact_type,
        version=target.version,
        sha256="0" * 64,
    )
    payload = {
        "session_id": "00000000-0000-0000-0000-000000000099",
        "nested": [bad_ref.to_dict()],
    }
    store.store(
        artifact_type="summary",
        name="summary.json",
        data=json.dumps(payload).encode("utf-8"),
        metadata={"source_artifact_id": "missing"},
    )

    report = verify_session_bundle(session.bundle_path)

    assert {
        "artifact_payload_session_mismatch",
        "artifact_ref_metadata_mismatch",
        "artifact_metadata_ref_missing",
    }.issubset(_codes(report))


def test_verify_session_bundle_reports_invalid_processing_state_payload(
    tmp_path: Path,
) -> None:
    session = _create_recorded_session(tmp_path)
    store = FileSystemArtifactStore(session_id=session.record.id, session_dir=session.bundle_path)
    store.store(
        artifact_type="processing_state",
        name="run/stage/000001-completed.json",
        data=json.dumps(
            {
                "version": 1,
                "session_id": session.record.id,
                "run_id": "run",
                "stage": "stage",
                "status": "completed",
                "input_artifacts": [],
                "output_artifacts": [],
                "started_at": "2026-05-22T12:00:00Z",
                "completed_at": "2026-05-22T12:00:00Z",
                "error": None,
                "attempt": 1,
            }
        ).encode("utf-8"),
    )

    report = verify_session_bundle(session.bundle_path)

    assert "processing_state_invalid" in _codes(report)


def test_load_verified_session_bundle_rejects_broken_session(tmp_path: Path) -> None:
    session = _create_recorded_session(tmp_path)
    (session.bundle_path / "artifacts" / "source" / "local_mic_000001.wav").unlink()

    with pytest.raises(SessionIntegrityError, match="capture_segment_missing"):
        load_verified_session_bundle(session.bundle_path)


def test_rebuild_session_index_projects_integrity_status(tmp_path: Path) -> None:
    good = _create_recorded_session(tmp_path, name="Good")
    bad = _create_recorded_session(tmp_path, name="Bad")
    (bad.bundle_path / "artifacts" / "source" / "local_mic_000001.wav").unlink()

    entries = rebuild_session_index(tmp_path / "sessions")

    by_name = {entry.name: entry for entry in entries}
    assert by_name["Good"].session_id == good.record.id
    assert by_name["Good"].integrity_ok is True
    assert by_name["Bad"].session_id == bad.record.id
    assert by_name["Bad"].integrity_ok is False
    assert by_name["Bad"].finding_count == 1
    assert by_name["Good"].to_dict()["integrity_ok"] is True


def test_rebuild_session_index_handles_missing_root_and_skips_invalid_records(
    tmp_path: Path,
) -> None:
    sessions_root = tmp_path / "sessions"
    bad = sessions_root / "bad"
    bad.mkdir(parents=True)
    (bad / "session.json").write_text("[]", encoding="utf-8")

    assert rebuild_session_index(tmp_path / "missing") == ()
    assert rebuild_session_index(sessions_root) == ()


def test_processing_state_version_constant_remains_manifested() -> None:
    assert PROCESSING_STATE_VERSION == 1
    assert PROCESSING_STATE_ARTIFACT_TYPE == "processing_state"


def _create_recorded_session(tmp_path: Path, *, name: str | None = None) -> CreatedSession:
    session = create_session(
        profile=parse_capture_profile("online"),
        name=name,
        data_root=tmp_path,
        now=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )
    source_path = session.bundle_path / "artifacts" / "source" / "local_mic_000001.wav"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"audio")
    update_session_capture_metadata(
        session.record_path,
        {
            "version": 1,
            "sample_rate": 48000,
            "channels": 1,
            "segment_seconds": 30,
            "segment_count": 1,
            "segments": [
                {
                    "source_id": "local_mic",
                    "index": 1,
                    "path": "artifacts/source/local_mic_000001.wav",
                    "sample_rate": 48000,
                    "channels": 1,
                    "byte_count": len(b"audio"),
                    "sha256": _sha256(b"audio"),
                    "status": "sealed",
                    "format": "wav",
                }
            ],
        },
    )
    return session


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _codes(report: SessionIntegrityReport) -> set[str]:
    return {finding.code for finding in report.findings}
