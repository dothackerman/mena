"""Processing stage ledger and checkpoint contract."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from mena.artifacts import ArtifactRecord, ArtifactStore
from mena.fields import optional_str, required_bool, required_int, required_str
from mena.timestamps import utc_timestamp

PROCESSING_STATE_ARTIFACT_TYPE = "processing_state"
PROCESSING_STATE_VERSION = 1
PROCESSING_STAGE_STATUSES = frozenset({"pending", "running", "completed", "failed", "skipped"})
PROCESSING_PIPELINE_STAGES = frozenset(
    {"transcription", "diarization", "merge_timing", "confidence_flagging", "semantic_coherence"}
)
_ARTIFACT_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_:-]*$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_STAGE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

# GPU faults that are NOT resource exhaustion: falling back to CPU cannot help,
# so these must never be classified as a recoverable GPU-resource failure.
_GPU_NON_RESOURCE_MARKERS = (
    "no kernel image",
    "invalid device function",
    "device-side assert",
    "illegal memory access",
    "misaligned address",
)
# cuBLAS/cuDNN status tokens are GPU-only and commonly surface GPU exhaustion
# (including downstream NOT_INITIALIZED cascades) without the word "cuda".
_GPU_RESOURCE_TOKENS = (
    "cublas_status_alloc_failed",
    "cublas_status_not_initialized",
    "cudnn_status_not_initialized",
)


def is_gpu_resource_error(exc: Exception) -> bool:
    """Return True when an exception looks like a recoverable GPU-resource failure.

    Shared by the transcription and diarization providers so the heuristic cannot
    drift between stages. A CPU fallback only helps for resource exhaustion, so
    non-resource GPU faults (deny-list) are excluded, and generic alloc/OOM tokens
    stay gated by "cuda" to avoid misclassifying CPU exhaustion as a GPU failure.
    """

    message = f"{exc.__class__.__name__}: {exc}".lower()
    if any(marker in message for marker in _GPU_NON_RESOURCE_MARKERS):
        return False
    if any(token in message for token in _GPU_RESOURCE_TOKENS):
        return True
    return "cuda" in message and ("out of memory" in message or "alloc" in message)


class ProcessingStateError(RuntimeError):
    """Raised when processing checkpoint state violates the v1 contract."""


@dataclass(frozen=True)
class ArtifactRef:
    """Stable reference to an artifact consumed or produced by a processing stage."""

    id: str
    artifact_type: str
    version: int
    sha256: str

    @classmethod
    def from_record(cls, record: ArtifactRecord) -> Self:
        return cls(
            id=record.id,
            artifact_type=record.artifact_type,
            version=record.version,
            sha256=record.sha256,
        )

    @classmethod
    def from_dict(cls, data: object) -> Self:
        if not isinstance(data, dict):
            raise ProcessingStateError("Artifact ref must be a JSON object.")
        artifact_id = _required_str(data, "id")
        artifact_type = _required_str(data, "artifact_type")
        version = _required_int(data, "version")
        sha256 = _required_str(data, "sha256")
        if not _ARTIFACT_TYPE_PATTERN.fullmatch(artifact_type):
            raise ProcessingStateError("Artifact ref artifact_type must be a stable identifier.")
        if version < 1:
            raise ProcessingStateError("Artifact ref version must be positive.")
        if not _SHA256_PATTERN.fullmatch(sha256):
            raise ProcessingStateError("Artifact ref sha256 must be a lowercase hex digest.")
        return cls(id=artifact_id, artifact_type=artifact_type, version=version, sha256=sha256)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "artifact_type": self.artifact_type,
            "version": self.version,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class ProcessingErrorInfo:
    """Structured failure summary for a processing stage."""

    message: str
    retryable: bool
    code: str | None = None

    @classmethod
    def from_dict(cls, data: object) -> Self:
        if data is None:
            raise ProcessingStateError("Processing error must be present for failed stages.")
        if not isinstance(data, dict):
            raise ProcessingStateError("Processing error must be a JSON object.")
        message = _required_str(data, "message")
        retryable = _required_bool(data, "retryable")
        code = data.get("code")
        if code is not None and (not isinstance(code, str) or not code):
            raise ProcessingStateError("Processing error code must be null or a non-empty string.")
        return cls(message=message, retryable=retryable, code=code)

    def to_dict(self) -> dict[str, object]:
        return {
            "message": self.message,
            "retryable": self.retryable,
            "code": self.code,
        }


@dataclass(frozen=True)
class ProcessingStageState:
    """Versioned checkpoint state for one processing stage transition."""

    version: int
    session_id: str
    run_id: str
    stage: str
    status: str
    input_artifacts: tuple[ArtifactRef, ...]
    output_artifacts: tuple[ArtifactRef, ...]
    started_at: str | None
    completed_at: str | None
    error: ProcessingErrorInfo | None
    attempt: int
    skip_reason: str | None = None

    @classmethod
    def from_dict(cls, data: object) -> Self:
        if not isinstance(data, dict):
            raise ProcessingStateError("Processing stage state must be a JSON object.")
        error_data = data.get("error")
        state = cls(
            version=_required_int(data, "version"),
            session_id=_required_str(data, "session_id"),
            run_id=_required_str(data, "run_id"),
            stage=_required_str(data, "stage"),
            status=_required_str(data, "status"),
            input_artifacts=_artifact_refs(data.get("input_artifacts")),
            output_artifacts=_artifact_refs(data.get("output_artifacts")),
            started_at=_optional_str(data, "started_at"),
            completed_at=_optional_str(data, "completed_at"),
            error=None if error_data is None else ProcessingErrorInfo.from_dict(error_data),
            attempt=_required_int(data, "attempt"),
            skip_reason=_optional_str(data, "skip_reason"),
        )
        _validate_stage_state(state)
        return state

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "stage": self.stage,
            "status": self.status,
            "input_artifacts": [artifact.to_dict() for artifact in self.input_artifacts],
            "output_artifacts": [artifact.to_dict() for artifact in self.output_artifacts],
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": None if self.error is None else self.error.to_dict(),
            "attempt": self.attempt,
            "skip_reason": self.skip_reason,
        }


class ProcessingStageLedger:
    """Small sequential processing checkpoint ledger backed by the artifact store."""

    def __init__(self, *, store: ArtifactStore, session_id: str, run_id: str) -> None:
        self.store = store
        self.session_id = session_id
        self.run_id = run_id

    def mark_running(
        self,
        *,
        stage: str,
        input_artifacts: tuple[ArtifactRef, ...] = (),
        attempt: int = 1,
        now: datetime | None = None,
    ) -> ProcessingStageState:
        return self.record(
            ProcessingStageState(
                version=PROCESSING_STATE_VERSION,
                session_id=self.session_id,
                run_id=self.run_id,
                stage=stage,
                status="running",
                input_artifacts=input_artifacts,
                output_artifacts=(),
                started_at=_timestamp(now),
                completed_at=None,
                error=None,
                attempt=attempt,
            )
        )

    def mark_completed(
        self,
        *,
        stage: str,
        input_artifacts: tuple[ArtifactRef, ...],
        output_artifacts: tuple[ArtifactRef, ...],
        attempt: int = 1,
        started_at: str | None = None,
        now: datetime | None = None,
    ) -> ProcessingStageState:
        return self.record(
            ProcessingStageState(
                version=PROCESSING_STATE_VERSION,
                session_id=self.session_id,
                run_id=self.run_id,
                stage=stage,
                status="completed",
                input_artifacts=input_artifacts,
                output_artifacts=output_artifacts,
                started_at=started_at or _timestamp(now),
                completed_at=_timestamp(now),
                error=None,
                attempt=attempt,
            )
        )

    def mark_failed(
        self,
        *,
        stage: str,
        input_artifacts: tuple[ArtifactRef, ...],
        error: ProcessingErrorInfo,
        output_artifacts: tuple[ArtifactRef, ...] = (),
        attempt: int = 1,
        started_at: str | None = None,
        now: datetime | None = None,
    ) -> ProcessingStageState:
        return self.record(
            ProcessingStageState(
                version=PROCESSING_STATE_VERSION,
                session_id=self.session_id,
                run_id=self.run_id,
                stage=stage,
                status="failed",
                input_artifacts=input_artifacts,
                output_artifacts=output_artifacts,
                started_at=started_at or _timestamp(now),
                completed_at=_timestamp(now),
                error=error,
                attempt=attempt,
            )
        )

    def record(self, state: ProcessingStageState) -> ProcessingStageState:
        _validate_stage_state(state)
        if state.session_id != self.session_id:
            raise ProcessingStateError("Stage state session_id does not match ledger.")
        if state.run_id != self.run_id:
            raise ProcessingStateError("Stage state run_id does not match ledger.")
        if state.status == "completed" and not all(
            self._verified_output(artifact) for artifact in state.output_artifacts
        ):
            raise ProcessingStateError(
                "Completed stages must declare stored and verified output artifacts."
            )
        name = self._state_name(state)
        self.store.store(
            artifact_type=PROCESSING_STATE_ARTIFACT_TYPE,
            name=name,
            data=(json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n").encode("utf-8"),
            version=PROCESSING_STATE_VERSION,
            metadata={"stage": state.stage, "status": state.status, "run_id": state.run_id},
        )
        return state

    def states(self, *, stage: str | None = None) -> tuple[ProcessingStageState, ...]:
        records = self.store.list(artifact_type=PROCESSING_STATE_ARTIFACT_TYPE)
        states = []
        for record in records:
            state = ProcessingStageState.from_dict(json.loads(self.store.retrieve(record.id)))
            if (
                state.session_id == self.session_id
                and state.run_id == self.run_id
                and (stage is None or state.stage == stage)
            ):
                states.append(state)
        return tuple(states)

    def latest(self, stage: str) -> ProcessingStageState | None:
        states = self.states(stage=stage)
        return states[-1] if states else None

    def should_skip_completed(self, stage: str) -> bool:
        state = self.latest(stage)
        if state is None or state.status != "completed":
            return False
        if not state.output_artifacts:
            return False
        return all(self._verified_output(artifact) for artifact in state.output_artifacts)

    def retryable_failure(self, stage: str) -> bool:
        state = self.latest(stage)
        return (
            state is not None
            and state.status == "failed"
            and state.error is not None
            and state.error.retryable
        )

    def _verified_output(self, artifact: ArtifactRef) -> bool:
        record = next((item for item in self.store.list() if item.id == artifact.id), None)
        if record is None:
            return False
        if (
            record.artifact_type != artifact.artifact_type
            or record.version != artifact.version
            or record.sha256 != artifact.sha256
        ):
            return False
        try:
            verification = self.store.verify(artifact.id)
        except KeyError:
            return False
        return verification.ok and verification.expected_sha256 == artifact.sha256

    def _state_name(self, state: ProcessingStageState) -> str:
        sequence = len(self.store.list(artifact_type=PROCESSING_STATE_ARTIFACT_TYPE)) + 1
        return f"{state.run_id}/{state.stage}/{sequence:06d}-{state.status}.json"


def _validate_stage_state(state: ProcessingStageState) -> None:
    if state.version != PROCESSING_STATE_VERSION:
        raise ProcessingStateError("Unsupported processing state version.")
    if not _STAGE_PATTERN.fullmatch(state.stage):
        raise ProcessingStateError("Processing stage must be a stable identifier.")
    if state.status not in PROCESSING_STAGE_STATUSES:
        valid = ", ".join(sorted(PROCESSING_STAGE_STATUSES))
        raise ProcessingStateError(f"Processing status must be one of: {valid}.")
    if state.attempt < 1:
        raise ProcessingStateError("Processing attempt must be positive.")
    if state.status == "running" and state.completed_at is not None:
        raise ProcessingStateError("Running stages must not have completed_at.")
    if state.status in {"completed", "failed", "skipped"} and state.completed_at is None:
        raise ProcessingStateError("Terminal stages must have completed_at.")
    if state.status == "completed" and state.error is not None:
        raise ProcessingStateError("Completed stages must not carry an error.")
    if state.status == "completed" and not state.output_artifacts:
        raise ProcessingStateError("Completed stages must declare output artifacts.")
    if state.status == "failed" and state.error is None:
        raise ProcessingStateError("Failed stages must carry an error.")
    if state.status != "failed" and state.error is not None:
        raise ProcessingStateError("Only failed stages may carry an error.")
    if state.status != "skipped" and state.skip_reason is not None:
        raise ProcessingStateError("Only skipped stages may carry a skip_reason.")


def _artifact_refs(data: object) -> tuple[ArtifactRef, ...]:
    if not isinstance(data, list):
        raise ProcessingStateError("Artifact refs must be an array.")
    return tuple(ArtifactRef.from_dict(item) for item in data)


def _timestamp(now: datetime | None) -> str:
    return utc_timestamp(now or datetime.now(UTC))


def _processing_state_order(record: ArtifactRecord) -> int:
    prefix = Path(record.path).stem.split("-", 1)[0]
    try:
        return int(prefix)
    except ValueError as exc:
        raise ProcessingStateError(
            "Processing state artifact name must start with sequence."
        ) from exc


def _latest_processing_state(
    store: ArtifactStore,
    session_id: str,
    *,
    allowed_statuses: frozenset[str],
    require_retryable_error: bool | None = None,
) -> ProcessingStageState | None:
    matches: list[tuple[int, ProcessingStageState]] = []
    for state_record in store.list(artifact_type=PROCESSING_STATE_ARTIFACT_TYPE):
        payload = json.loads(store.retrieve(state_record.id))
        state = ProcessingStageState.from_dict(payload)
        if (
            state.session_id != session_id
            or state.status not in allowed_statuses
            or state.stage not in PROCESSING_PIPELINE_STAGES
        ):
            continue

        if state.status == "failed":
            if state.error is None:
                continue
            if require_retryable_error is True and not state.error.retryable:
                continue
            if require_retryable_error is False and state.error.retryable:
                continue

        matches.append((_processing_state_order(state_record), state))

    if not matches:
        return None
    return max(matches, key=lambda item: item[0])[1]


def latest_retryable_processing_state(
    store: ArtifactStore,
    session_id: str,
) -> ProcessingStageState | None:
    return _latest_processing_state(
        store=store,
        session_id=session_id,
        allowed_statuses=frozenset({"failed"}),
        require_retryable_error=True,
    )


def latest_resumable_processing_state(
    store: ArtifactStore,
    session_id: str,
) -> ProcessingStageState | None:
    latest = _latest_processing_state(
        store=store,
        session_id=session_id,
        allowed_statuses=frozenset({"running", "completed", "failed", "skipped"}),
    )
    if latest is None or latest.status == "running":
        return latest
    if latest.status in {"completed", "skipped"}:
        return None if _is_final_processing_stage(latest) else latest
    if latest.error is not None and latest.error.retryable:
        return latest
    return None


def has_completed_processing_pipeline(store: ArtifactStore, session_id: str) -> bool:
    """Return True when the latest processing checkpoint completed the pipeline."""

    latest = _latest_processing_state(
        store=store,
        session_id=session_id,
        allowed_statuses=frozenset({"running", "completed", "failed", "skipped"}),
    )
    return (
        latest is not None
        and latest.status in {"completed", "skipped"}
        and _is_final_processing_stage(latest)
    )


def _is_final_processing_stage(state: ProcessingStageState) -> bool:
    return state.stage == "semantic_coherence"


def latest_non_retryable_processing_failure(
    store: ArtifactStore,
    session_id: str,
) -> ProcessingStageState | None:
    latest = _latest_processing_state(
        store=store,
        session_id=session_id,
        allowed_statuses=frozenset({"running", "completed", "failed", "skipped"}),
    )
    if latest is None or latest.status != "failed":
        return None
    if latest.error is not None and not latest.error.retryable:
        return latest
    return None


def _required_str(data: dict[str, object], key: str) -> str:
    return required_str(
        data,
        key,
        error=ProcessingStateError,
        message=f"Processing field '{key}' must be a non-empty string.",
    )


def _optional_str(data: dict[str, object], key: str) -> str | None:
    return optional_str(
        data,
        key,
        error=ProcessingStateError,
        message=f"Processing field '{key}' must be null or a non-empty string.",
    )


def _required_int(data: dict[str, object], key: str) -> int:
    return required_int(
        data,
        key,
        error=ProcessingStateError,
        message=f"Processing field '{key}' must be an integer.",
    )


def _required_bool(data: dict[str, object], key: str) -> bool:
    return required_bool(
        data,
        key,
        error=ProcessingStateError,
        message=f"Processing field '{key}' must be a boolean.",
    )
