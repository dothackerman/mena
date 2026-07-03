"""Session bundle creation for the mena PoC."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from mena.artifacts import FileSystemArtifactStore
from mena.fileops import write_json_atomically
from mena.processing import has_completed_processing_pipeline
from mena.profiles import CaptureProfileSelection
from mena.settings import AppSettings
from mena.timestamps import parse_utc_timestamp, utc_timestamp, utc_timestamp_for_filename

SESSION_RECORD_VERSION = 1
SESSION_RECORD_FILENAME = "session.json"
SESSION_DISCOVERY_KEY = "discovery"
SESSION_CAPTURE_PLAN_KEY = "capture_plan"
SESSION_CAPTURE_METADATA_KEY = "capture_metadata"
SESSION_DEGRADATIONS_KEY = "degradations"
SESSION_FAILURES_KEY = "failures"
DEFAULT_SESSION_LANGUAGE = "en"


@dataclass(frozen=True)
class RecordingFinalization:
    """Finalization facts for the recording lifecycle."""

    status: str
    completed_at: str | None
    error: str | None


@dataclass(frozen=True)
class RecordingLifecycle:
    """Recording lifecycle facts persisted on every session record."""

    started_at: str | None
    stopped_at: str | None
    stop_reason: str | None
    finalization: RecordingFinalization


@dataclass(frozen=True)
class SessionRecord:
    """Canonical session record written when `mena record` creates a bundle."""

    version: int
    id: str
    created_at: str
    name: str | None
    status: str
    profile: str
    language: str
    recording: RecordingLifecycle
    capture_plan: dict[str, object] | None
    degradations: tuple[dict[str, object], ...]
    failures: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class ResolvedSession:
    """A session bundle path and its canonical record."""

    session_dir: Path
    record_path: Path
    record: dict[str, object]


@dataclass(frozen=True)
class CreatedSession:
    """Result of creating a session bundle on disk."""

    bundle_path: Path
    record_path: Path
    record: SessionRecord


@dataclass(frozen=True)
class ProcessableSessionCandidate:
    """A recorded runtime session that `mena process` can process by default."""

    session_dir: Path
    record_path: Path
    session_id: str
    name: str | None
    profile: str
    created_at: str
    completed_at: str
    sort_completed_at: datetime


class SessionRecordError(ValueError):
    """Raised when a session record cannot satisfy the session contract."""


def default_data_root() -> Path:
    """Return the local data root for runtime session bundles."""

    return AppSettings.from_env().paths.sessions_dir.parent


def create_session(
    *,
    profile: CaptureProfileSelection,
    language: str = DEFAULT_SESSION_LANGUAGE,
    name: str | None = None,
    data_root: Path | None = None,
    now: datetime | None = None,
) -> CreatedSession:
    """Create a session bundle and write its initial session record."""

    created_at = (now or datetime.now(UTC)).astimezone(UTC)
    normalized_language = _validate_language(language)
    session_id = str(uuid4())
    bundle_path = _sessions_root(data_root) / _bundle_directory_name(session_id, created_at)
    bundle_path.mkdir(parents=True, exist_ok=False)

    record = SessionRecord(
        version=SESSION_RECORD_VERSION,
        id=session_id,
        created_at=utc_timestamp(created_at),
        name=name,
        status="created",
        profile=profile.id.value,
        language=normalized_language,
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
    record_path = bundle_path / SESSION_RECORD_FILENAME
    write_json_atomically(record_path, asdict(record))
    return CreatedSession(bundle_path=bundle_path, record_path=record_path, record=record)


def read_session_record(record_path: Path) -> dict[str, object]:
    """Read a session record JSON object from disk."""

    data = json.loads(record_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Session record must be a JSON object.")
    return data


def resolve_session(session_path: Path) -> ResolvedSession:
    """Resolve a session directory or session.json path into a session reference."""

    session_dir, record_path = resolve_session_paths(session_path)
    if not session_dir.is_dir() or not record_path.is_file():
        raise SessionRecordError(
            "Session path must be a session directory or an existing session.json path."
        )

    try:
        record = read_session_record(record_path)
    except Exception as exc:
        raise SessionRecordError(f"Cannot read session record at {record_path}.") from exc
    return ResolvedSession(session_dir=session_dir, record_path=record_path, record=record)


def resolve_session_paths(session_path: Path) -> tuple[Path, Path]:
    """Return the session directory and session record path for a session input path."""

    if session_path.name == SESSION_RECORD_FILENAME:
        return session_path.parent, session_path
    return session_path, session_path / SESSION_RECORD_FILENAME


def latest_recorded_session_record_path(sessions_root: Path) -> Path | None:
    """Return the newest successfully recorded session record under a sessions root."""

    candidates = processable_session_candidates(sessions_root, include_processed=True)
    if not candidates:
        return None
    newest = candidates[0]
    tied = [
        candidate
        for candidate in candidates
        if candidate.sort_completed_at == newest.sort_completed_at
    ]
    if len(tied) > 1:
        raise SessionRecordError(
            "latest recorded session is ambiguous; pass an explicit session path."
        )
    return newest.record_path


def processable_session_candidates(
    sessions_root: Path,
    *,
    include_processed: bool = False,
) -> tuple[ProcessableSessionCandidate, ...]:
    """Return recorded runtime sessions that are candidates for pathless processing."""

    candidates: list[ProcessableSessionCandidate] = []
    try:
        for session_dir in sessions_root.iterdir():
            if not session_dir.is_dir():
                continue
            record_path = session_dir / SESSION_RECORD_FILENAME
            if not record_path.is_file():
                continue
            try:
                record = read_session_record(record_path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if record.get("status") != "recorded":
                continue
            try:
                completed_at = _recording_completed_at(record)
            except ValueError as exc:
                raise SessionRecordError(
                    "recorded session has invalid recording.finalization.completed_at; "
                    "pass an explicit session path."
                ) from exc
            if completed_at is None:
                raise SessionRecordError(
                    "recorded session is missing recording.finalization.completed_at; "
                    "pass an explicit session path."
                )
            try:
                record_session_id = session_id(record)
                record_profile = session_profile(record)
                record_created_at = _required_record_str(record, "created_at")
                record_completed_at = _required_completed_at(record)
            except SessionRecordError:
                continue
            if not include_processed and _processing_is_complete(session_dir, record):
                continue
            try:
                candidates.append(
                    ProcessableSessionCandidate(
                        session_dir=session_dir,
                        record_path=record_path,
                        session_id=record_session_id,
                        name=_optional_record_str(record, "name"),
                        profile=record_profile,
                        created_at=record_created_at,
                        completed_at=record_completed_at,
                        sort_completed_at=completed_at,
                    )
                )
            except SessionRecordError:
                continue
    except OSError:
        return ()
    return tuple(
        sorted(
            candidates,
            key=lambda candidate: (
                candidate.sort_completed_at,
                str(candidate.record_path),
            ),
            reverse=True,
        )
    )


def session_id(session_record: Mapping[str, object]) -> str:
    """Return the required session id from a session record."""

    value = session_record.get("id")
    if not isinstance(value, str) or not value:
        raise SessionRecordError("Session field 'id' is required and must be a non-empty string.")
    return value


def session_language_or_default(
    session_record: Mapping[str, object],
    default_language: str,
) -> str:
    """Return the persisted session language or a caller-provided default."""

    language = session_record.get("language")
    if isinstance(language, str) and language:
        return language
    return default_language


def session_profile(session_record: Mapping[str, object]) -> str:
    """Return the required capture profile id from a session record."""

    value = session_record.get("profile")
    if not isinstance(value, str) or not value:
        raise SessionRecordError(
            "Session field 'profile' is required and must be a non-empty string."
        )
    return value


def _processing_is_complete(
    session_dir: Path,
    session_record: Mapping[str, object],
) -> bool:
    session_id_value = session_id(session_record)
    store = FileSystemArtifactStore(session_id=session_id_value, session_dir=session_dir)
    return has_completed_processing_pipeline(store, session_id_value)


def _required_record_str(session_record: Mapping[str, object], key: str) -> str:
    value = session_record.get(key)
    if not isinstance(value, str) or not value:
        raise SessionRecordError(f"Session field '{key}' is required and must be a string.")
    return value


def _optional_record_str(session_record: Mapping[str, object], key: str) -> str | None:
    value = session_record.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise SessionRecordError(f"Session field '{key}' must be a string or null.")
    return value


def _required_completed_at(session_record: Mapping[str, object]) -> str:
    finalization = session_recording_finalization(session_record)
    value = finalization.get("completed_at")
    if not isinstance(value, str) or not value:
        raise SessionRecordError("Session field 'recording.finalization.completed_at' is required.")
    return value


def session_recording(session_record: Mapping[str, object]) -> Mapping[str, object]:
    """Return the recording lifecycle object from a session record."""

    value = session_record.get("recording")
    if not isinstance(value, Mapping):
        raise SessionRecordError("Session field 'recording' is required and must be an object.")
    return value


def session_recording_finalization(
    session_record: Mapping[str, object],
) -> Mapping[str, object]:
    """Return the recording finalization object from a session record."""

    recording = session_recording(session_record)
    value = recording.get("finalization")
    if not isinstance(value, Mapping):
        raise SessionRecordError(
            "Session field 'recording.finalization' is required and must be an object."
        )
    return value


def _recording_completed_at(session_record: Mapping[str, object]) -> datetime | None:
    recording = session_record.get("recording")
    if not isinstance(recording, Mapping):
        return None
    finalization = recording.get("finalization")
    if not isinstance(finalization, Mapping):
        return None
    if finalization.get("status") != "finalized":
        return None
    completed_at = finalization.get("completed_at")
    if not isinstance(completed_at, str):
        return None
    return parse_utc_timestamp(completed_at)


def session_recording_finalization_status(
    session_record: Mapping[str, object],
) -> str:
    """Return the persisted recording finalization status."""

    finalization = session_recording_finalization(session_record)
    value = finalization.get("status")
    if not isinstance(value, str) or not value:
        raise SessionRecordError(
            "Session field 'recording.finalization.status' is required and must be a "
            "non-empty string."
        )
    return value


def is_recording_started(session_record: Mapping[str, object]) -> bool:
    """Return whether session recording has started."""

    recording = session_recording(session_record)
    return isinstance(recording.get("started_at"), str) and bool(recording["started_at"])


def is_recording_finalized(session_record: Mapping[str, object]) -> bool:
    """Return whether recording reached a terminal finalization state."""

    return session_recording_finalization_status(session_record) in {
        "finalized",
        "failed",
        "interrupted_partial",
    }


def session_capture_metadata(
    session_record: Mapping[str, object],
) -> Mapping[str, object] | None:
    """Return capture metadata when present."""

    value = session_record.get(SESSION_CAPTURE_METADATA_KEY)
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise SessionRecordError("Session field 'capture_metadata' must be an object.")
    return value


def session_discovery(session_record: Mapping[str, object]) -> Mapping[str, object] | None:
    """Return persisted audio discovery facts when present."""

    value = session_record.get(SESSION_DISCOVERY_KEY)
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise SessionRecordError("Session field 'discovery' must be an object.")
    return value


def session_failures(session_record: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    """Return persisted session failure facts."""

    return _session_mapping_tuple(session_record, SESSION_FAILURES_KEY)


def session_degradations(
    session_record: Mapping[str, object],
) -> tuple[Mapping[str, object], ...]:
    """Return persisted session degradation facts."""

    return _session_mapping_tuple(session_record, SESSION_DEGRADATIONS_KEY)


def update_session_record(
    record_path: Path,
    *,
    updates: Mapping[str, object],
) -> dict[str, object]:
    """Apply orchestrator-owned updates to a session record."""

    record = read_session_record(record_path)
    record.update(updates)
    write_json_atomically(record_path, record)
    return record


def persist_session_discovery(
    record_path: Path, discovery: Mapping[str, object]
) -> dict[str, object]:
    """Persist read-only audio discovery facts on the canonical session record."""

    return update_session_record(record_path, updates={SESSION_DISCOVERY_KEY: dict(discovery)})


def update_session_discovery(
    record_path: Path, discovery: Mapping[str, object]
) -> dict[str, object]:
    """Persist read-only audio discovery facts on the canonical session record."""

    return persist_session_discovery(record_path, discovery)


def persist_session_capture_plan(
    record_path: Path,
    capture_plan: Mapping[str, object],
) -> dict[str, object]:
    """Persist the selected capture plan on the canonical session record."""

    return update_session_record(
        record_path,
        updates={SESSION_CAPTURE_PLAN_KEY: dict(capture_plan)},
    )


def update_session_capture_plan(
    record_path: Path,
    capture_plan: Mapping[str, object],
) -> dict[str, object]:
    """Persist the selected capture plan on the canonical session record."""

    return persist_session_capture_plan(record_path, capture_plan)


def persist_session_capture_metadata(
    record_path: Path,
    capture_metadata: Mapping[str, object],
) -> dict[str, object]:
    """Persist capture metadata emitted by the worker on the canonical session record."""

    return update_session_record(
        record_path,
        updates={SESSION_CAPTURE_METADATA_KEY: dict(capture_metadata)},
    )


def update_session_capture_metadata(
    record_path: Path,
    capture_metadata: Mapping[str, object],
) -> dict[str, object]:
    """Persist capture metadata emitted by the worker on the canonical session record."""

    return persist_session_capture_metadata(record_path, capture_metadata)


def persist_session_recording_lifecycle(
    record_path: Path,
    *,
    status: str,
    recording: Mapping[str, object],
    capture_plan: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Persist session-level recording lifecycle facts."""

    updates: dict[str, object] = {"status": status, "recording": dict(recording)}
    if capture_plan is not None:
        updates[SESSION_CAPTURE_PLAN_KEY] = dict(capture_plan)
    return update_session_record(record_path, updates=updates)


def append_session_degradation_fact(
    record_path: Path,
    degradation: Mapping[str, object],
) -> dict[str, object]:
    """Append an explicit capture degradation fact to the session record."""

    return _append_session_list_item(record_path, SESSION_DEGRADATIONS_KEY, degradation)


def append_session_degradation(
    record_path: Path,
    degradation: Mapping[str, object],
) -> dict[str, object]:
    """Append an explicit capture degradation fact to the session record."""

    return append_session_degradation_fact(record_path, degradation)


def append_session_failure_fact(
    record_path: Path,
    failure: Mapping[str, object],
) -> dict[str, object]:
    """Append an explicit capture failure fact to the session record."""

    return _append_session_list_item(record_path, SESSION_FAILURES_KEY, failure)


def append_session_failure(
    record_path: Path,
    failure: Mapping[str, object],
) -> dict[str, object]:
    """Append an explicit capture failure fact to the session record."""

    return append_session_failure_fact(record_path, failure)


def _sessions_root(data_root: Path | None) -> Path:
    if data_root:
        return data_root / "sessions"
    return AppSettings.from_env().paths.sessions_dir


def _validate_language(language: str) -> str:
    normalized = language.strip()
    if not normalized:
        raise ValueError("Session language must be a non-empty string.")
    return normalized


def _append_session_list_item(
    record_path: Path,
    key: str,
    item: Mapping[str, object],
) -> dict[str, object]:
    record = read_session_record(record_path)
    existing = record.get(key, [])
    if not isinstance(existing, list):
        raise ValueError(f"Session field '{key}' must be an array.")
    record[key] = [*existing, dict(item)]
    write_json_atomically(record_path, record)
    return record


def _session_mapping_tuple(
    session_record: Mapping[str, object],
    key: str,
) -> tuple[Mapping[str, object], ...]:
    value = session_record.get(key, [])
    if not isinstance(value, list):
        raise SessionRecordError(f"Session field '{key}' must be an array.")
    if not all(isinstance(item, Mapping) for item in value):
        raise SessionRecordError(f"Session field '{key}' must contain objects.")
    return tuple(value)


def _bundle_directory_name(session_id: str, created_at: datetime) -> str:
    timestamp = utc_timestamp_for_filename(created_at)
    return f"{session_id}_{timestamp}"
