"""Session bundle creation for the mena PoC."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from mena.profiles import CaptureProfileSelection
from mena.settings import AppSettings

SESSION_RECORD_VERSION = 1
SESSION_RECORD_FILENAME = "session.json"
SESSION_DISCOVERY_KEY = "discovery"
SESSION_CAPTURE_METADATA_KEY = "capture_metadata"


@dataclass(frozen=True)
class SessionRecord:
    """Minimal canonical session record written when `mena record` starts."""

    version: int
    id: str
    created_at: str
    name: str | None
    status: str
    profile: str


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
    name: str | None = None,
    data_root: Path | None = None,
    now: datetime | None = None,
) -> CreatedSession:
    """Create a session bundle and write its initial session record."""

    created_at = (now or datetime.now(UTC)).astimezone(UTC)
    session_id = str(uuid4())
    bundle_path = _sessions_root(data_root) / _bundle_directory_name(session_id, created_at)
    bundle_path.mkdir(parents=True, exist_ok=False)

    record = SessionRecord(
        version=SESSION_RECORD_VERSION,
        id=session_id,
        created_at=created_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
        name=name,
        status="created",
        profile=profile.id.value,
    )
    record_path = bundle_path / SESSION_RECORD_FILENAME
    _write_json_atomically(record_path, asdict(record))
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
    _write_json_atomically(record_path, record)
    return record


def update_session_discovery(
    record_path: Path, discovery: Mapping[str, object]
) -> dict[str, object]:
    """Persist read-only audio discovery facts on the canonical session record."""

    return update_session_record(record_path, updates={SESSION_DISCOVERY_KEY: dict(discovery)})


def update_session_capture_metadata(
    record_path: Path,
    capture_metadata: Mapping[str, object],
) -> dict[str, object]:
    """Persist capture metadata emitted by the worker on the canonical session record."""

    return update_session_record(
        record_path,
        updates={SESSION_CAPTURE_METADATA_KEY: dict(capture_metadata)},
    )


def _sessions_root(data_root: Path | None) -> Path:
    if data_root:
        return data_root / "sessions"
    return AppSettings.from_env().paths.sessions_dir


def _bundle_directory_name(session_id: str, created_at: datetime) -> str:
    timestamp = created_at.isoformat(timespec="seconds").replace("+00:00", "Z").replace(":", "")
    return f"{session_id}_{timestamp}"


def _write_json_atomically(path: Path, data: Mapping[str, object]) -> None:
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)
