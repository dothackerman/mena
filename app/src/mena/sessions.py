"""Session bundle creation for the mena PoC."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from mena.fileops import write_json_atomically
from mena.profiles import CaptureProfileSelection
from mena.settings import AppSettings
from mena.timestamps import utc_timestamp, utc_timestamp_for_filename

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
class CreatedSession:
    """Result of creating a session bundle on disk."""

    bundle_path: Path
    record_path: Path
    record: SessionRecord


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


def update_session_discovery(
    record_path: Path, discovery: Mapping[str, object]
) -> dict[str, object]:
    """Persist read-only audio discovery facts on the canonical session record."""

    return update_session_record(record_path, updates={SESSION_DISCOVERY_KEY: dict(discovery)})


def update_session_capture_plan(
    record_path: Path,
    capture_plan: Mapping[str, object],
) -> dict[str, object]:
    """Persist the selected capture plan on the canonical session record."""

    return update_session_record(
        record_path,
        updates={SESSION_CAPTURE_PLAN_KEY: dict(capture_plan)},
    )


def update_session_capture_metadata(
    record_path: Path,
    capture_metadata: Mapping[str, object],
) -> dict[str, object]:
    """Persist capture metadata emitted by the worker on the canonical session record."""

    return update_session_record(
        record_path,
        updates={SESSION_CAPTURE_METADATA_KEY: dict(capture_metadata)},
    )


def append_session_degradation(
    record_path: Path,
    degradation: Mapping[str, object],
) -> dict[str, object]:
    """Append an explicit capture degradation fact to the session record."""

    return _append_session_list_item(record_path, SESSION_DEGRADATIONS_KEY, degradation)


def append_session_failure(
    record_path: Path,
    failure: Mapping[str, object],
) -> dict[str, object]:
    """Append an explicit capture failure fact to the session record."""

    return _append_session_list_item(record_path, SESSION_FAILURES_KEY, failure)


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


def _bundle_directory_name(session_id: str, created_at: datetime) -> str:
    timestamp = utc_timestamp_for_filename(created_at)
    return f"{session_id}_{timestamp}"
