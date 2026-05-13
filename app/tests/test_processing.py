import json
from collections.abc import Callable
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from mena.artifacts import ArtifactStore, FileSystemArtifactStore, InMemoryArtifactStore
from mena.processing import (
    PROCESSING_STATE_ARTIFACT_TYPE,
    PROCESSING_STATE_VERSION,
    ArtifactRef,
    ProcessingErrorInfo,
    ProcessingStageLedger,
    ProcessingStageState,
    ProcessingStateError,
)

SESSION_ID = "00000000-0000-0000-0000-000000000001"
OTHER_SESSION_ID = "00000000-0000-0000-0000-000000000002"
RUN_ID = "run-20260513"
NOW = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
StoreFactory = Callable[[Path], ArtifactStore]


def _memory_store(_tmp_path: Path) -> ArtifactStore:
    return InMemoryArtifactStore(session_id=SESSION_ID)


def _file_system_store(tmp_path: Path) -> ArtifactStore:
    return FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)


@pytest.mark.parametrize("store_factory", [_memory_store, _file_system_store])
def test_processing_ledger_persists_running_and_completed_states(
    tmp_path: Path,
    store_factory: StoreFactory,
) -> None:
    store = store_factory(tmp_path)
    source = store.store(artifact_type="source_audio", name="local.wav", data=b"audio")
    output = store.store(artifact_type="transcript", name="segments.json", data=b"[]")
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    running = ledger.mark_running(
        stage="transcription",
        input_artifacts=(ArtifactRef.from_record(source),),
        now=NOW,
    )
    completed = ledger.mark_completed(
        stage="transcription",
        input_artifacts=(ArtifactRef.from_record(source),),
        output_artifacts=(ArtifactRef.from_record(output),),
        started_at=running.started_at,
        now=NOW,
    )

    assert running.started_at == "2026-05-13T12:00:00Z"
    assert completed.completed_at == "2026-05-13T12:00:00Z"
    assert [state.status for state in ledger.states(stage="transcription")] == [
        "running",
        "completed",
    ]
    assert ledger.latest("transcription") == completed
    records = store.list(artifact_type=PROCESSING_STATE_ARTIFACT_TYPE)
    assert [record.metadata["status"] for record in records] == ["running", "completed"]
    persisted = ProcessingStageState.from_dict(json.loads(store.retrieve(records[-1].id)))
    assert persisted == completed


@pytest.mark.parametrize("store_factory", [_memory_store, _file_system_store])
def test_processing_ledger_skips_completed_stage_only_when_outputs_verify(
    tmp_path: Path,
    store_factory: StoreFactory,
) -> None:
    store = store_factory(tmp_path)
    output = store.store(artifact_type="transcript", name="segments.json", data=b"[]")
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    ledger.mark_completed(
        stage="transcription",
        input_artifacts=(),
        output_artifacts=(ArtifactRef.from_record(output),),
        now=NOW,
    )

    assert ledger.should_skip_completed("transcription") is True


def test_processing_ledger_does_not_skip_when_output_file_is_corrupt(tmp_path: Path) -> None:
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    output = store.store(artifact_type="transcript", name="segments.json", data=b"[]")
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)
    ledger.mark_completed(
        stage="transcription",
        input_artifacts=(),
        output_artifacts=(ArtifactRef.from_record(output),),
        now=NOW,
    )

    (tmp_path / output.path).write_bytes(b"changed")

    assert ledger.should_skip_completed("transcription") is False


def test_processing_ledger_does_not_skip_when_output_ref_contract_drifts(tmp_path: Path) -> None:
    store = _memory_store(tmp_path)
    output = store.store(artifact_type="transcript", name="segments.json", data=b"[]")
    mismatched_ref = ArtifactRef(
        id=output.id,
        artifact_type="diarization",
        version=output.version,
        sha256=output.sha256,
    )
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)
    ledger.mark_completed(
        stage="transcription",
        input_artifacts=(),
        output_artifacts=(mismatched_ref,),
        now=NOW,
    )

    assert ledger.should_skip_completed("transcription") is False


def test_processing_ledger_does_not_skip_unknown_stage_or_missing_output(tmp_path: Path) -> None:
    store = _memory_store(tmp_path)
    missing_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/transcript/missing.json",
        artifact_type="transcript",
        version=1,
        sha256="0" * 64,
    )
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    assert ledger.should_skip_completed("transcription") is False

    ledger.mark_completed(
        stage="transcription",
        input_artifacts=(),
        output_artifacts=(missing_ref,),
        now=NOW,
    )

    assert ledger.should_skip_completed("transcription") is False


def test_processing_ledger_tracks_latest_retryable_failure(tmp_path: Path) -> None:
    store = _memory_store(tmp_path)
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    ledger.mark_failed(
        stage="diarization",
        input_artifacts=(),
        error=ProcessingErrorInfo(message="model unavailable", retryable=True, code="model"),
        now=NOW,
    )
    assert ledger.retryable_failure("diarization") is True

    ledger.mark_failed(
        stage="diarization",
        input_artifacts=(),
        error=ProcessingErrorInfo(message="bad input", retryable=False, code="input"),
        attempt=2,
        now=NOW,
    )
    assert ledger.retryable_failure("diarization") is False


def test_processing_ledger_rejects_cross_session_state(tmp_path: Path) -> None:
    store = _memory_store(tmp_path)
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)
    state = ProcessingStageState(
        version=PROCESSING_STATE_VERSION,
        session_id=OTHER_SESSION_ID,
        run_id=RUN_ID,
        stage="transcription",
        status="running",
        input_artifacts=(),
        output_artifacts=(),
        started_at="2026-05-13T12:00:00Z",
        completed_at=None,
        error=None,
        attempt=1,
    )

    with pytest.raises(ProcessingStateError, match="session_id"):
        ledger.record(state)


def test_processing_ledger_rejects_cross_run_state(tmp_path: Path) -> None:
    store = _memory_store(tmp_path)
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)
    state = ProcessingStageState(
        version=PROCESSING_STATE_VERSION,
        session_id=SESSION_ID,
        run_id="other-run",
        stage="transcription",
        status="running",
        input_artifacts=(),
        output_artifacts=(),
        started_at="2026-05-13T12:00:00Z",
        completed_at=None,
        error=None,
        attempt=1,
    )

    with pytest.raises(ProcessingStateError, match="run_id"):
        ledger.record(state)


def test_processing_state_validation_rejects_invalid_terminal_states() -> None:
    completed_without_outputs = _state_dict(status="completed", output_artifacts=[])

    with pytest.raises(ProcessingStateError, match="output artifacts"):
        ProcessingStageState.from_dict(completed_without_outputs)

    failed_without_error = _state_dict(status="failed", error=None)

    with pytest.raises(ProcessingStateError, match="carry an error"):
        ProcessingStageState.from_dict(failed_without_error)


def test_processing_state_validation_rejects_invalid_payloads() -> None:
    cases = [
        ("not-an-object", "JSON object"),
        (_state_dict(version=2), "version"),
        (_state_dict(stage="Transcription"), "stable identifier"),
        (_state_dict(status="done"), "status"),
        (_state_dict(attempt=0), "attempt"),
        (_state_dict(status="running", completed_at="2026-05-13T12:00:00Z"), "completed_at"),
        ({**_state_dict(status="completed"), "completed_at": None}, "Terminal"),
        (
            _state_dict(
                status="completed",
                output_artifacts=[_artifact_ref_dict()],
                error={"message": "bad", "retryable": False, "code": None},
            ),
            "must not carry an error",
        ),
        (
            _state_dict(
                status="skipped",
                error={"message": "bad", "retryable": False, "code": None},
            ),
            "Only failed",
        ),
        (_state_dict(input_artifacts={}), "array"),
        (_state_dict(started_at=123), "started_at"),
        (_state_dict(stage=""), "stage"),
    ]

    for payload, message in cases:
        with pytest.raises(ProcessingStateError, match=message):
            ProcessingStageState.from_dict(payload)


def test_processing_state_validation_rejects_boolean_integer_fields() -> None:
    payload = _state_dict()
    payload["attempt"] = True

    with pytest.raises(ProcessingStateError, match="integer"):
        ProcessingStageState.from_dict(payload)

    ref_payload = _artifact_ref_dict()
    ref_payload["version"] = True

    with pytest.raises(ProcessingStateError, match="integer"):
        ArtifactRef.from_dict(ref_payload)


def test_artifact_ref_validation_rejects_invalid_payloads() -> None:
    cases = [
        ("not-an-object", "JSON object"),
        ({**_artifact_ref_dict(), "version": 0}, "positive"),
    ]

    for payload, message in cases:
        with pytest.raises(ProcessingStateError, match=message):
            ArtifactRef.from_dict(payload)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (None, "present"),
        ("not-an-object", "JSON object"),
        ({"message": "failed", "retryable": False, "code": ""}, "code"),
        ({"message": "failed", "retryable": "yes", "code": None}, "boolean"),
    ],
)
def test_processing_error_validation_rejects_invalid_payloads(
    payload: object,
    message: str,
) -> None:
    with pytest.raises(ProcessingStateError, match=message):
        ProcessingErrorInfo.from_dict(payload)


def test_processing_state_schema_accepts_valid_state() -> None:
    validator = Draft202012Validator(
        _load_processing_state_schema(),
        format_checker=FormatChecker(),
    )

    validator.validate(_state_dict())


def test_processing_state_schema_rejects_incomplete_state() -> None:
    payload = _state_dict()
    del payload["stage"]
    validator = Draft202012Validator(
        _load_processing_state_schema(),
        format_checker=FormatChecker(),
    )

    with pytest.raises(ValidationError, match="'stage' is a required property"):
        validator.validate(payload)


def test_processing_state_schema_rejects_semantic_contract_violations() -> None:
    payloads = [
        _state_dict(status="completed"),
        _state_dict(status="failed", error=None),
        _state_dict(status="running", completed_at="2026-05-13T12:00:00Z"),
        _state_dict(
            status="failed",
            error={"message": "failed", "retryable": True, "code": ""},
        ),
        _state_dict(input_artifacts=[{**_artifact_ref_dict(), "artifact_type": "Transcript"}]),
    ]
    validator = Draft202012Validator(
        _load_processing_state_schema(),
        format_checker=FormatChecker(),
    )

    for payload in payloads:
        with pytest.raises(ValidationError):
            validator.validate(payload)


def test_processing_state_schema_is_valid() -> None:
    Draft202012Validator.check_schema(_load_processing_state_schema())


def _state_dict(
    *,
    version: int = PROCESSING_STATE_VERSION,
    stage: str = "transcription",
    status: str = "running",
    input_artifacts: object | None = None,
    output_artifacts: list[dict[str, object]] | None = None,
    started_at: object = "2026-05-13T12:00:00Z",
    completed_at: object | None = None,
    error: dict[str, object] | None = None,
    attempt: int = 1,
) -> dict[str, object]:
    if output_artifacts is None:
        output_artifacts = []
    if input_artifacts is None:
        input_artifacts = []
    if completed_at is None and status in {"completed", "failed", "skipped"}:
        completed_at = "2026-05-13T12:00:00Z"
    return {
        "version": version,
        "session_id": SESSION_ID,
        "run_id": RUN_ID,
        "stage": stage,
        "status": status,
        "input_artifacts": input_artifacts,
        "output_artifacts": output_artifacts,
        "started_at": started_at,
        "completed_at": completed_at,
        "error": error,
        "attempt": attempt,
    }


def _artifact_ref_dict() -> dict[str, object]:
    return {
        "id": f"{SESSION_ID}:artifacts/transcript/segments.json",
        "artifact_type": "transcript",
        "version": 1,
        "sha256": "0" * 64,
    }


def _load_processing_state_schema() -> dict[str, object]:
    schema_path = files("mena.schemas").joinpath("processing-state.v1.schema.json")
    return cast(dict[str, object], json.loads(schema_path.read_text(encoding="utf-8")))
