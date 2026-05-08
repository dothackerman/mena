"""Session bundle creation for the mena PoC."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from mena.profiles import CaptureProfileName

SESSION_RECORD_VERSION = 1
SESSION_RECORD_FILENAME = "session.json"
DATA_ROOT_ENV_VAR = "MENA_DATA_ROOT"


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

    configured_root = os.environ.get(DATA_ROOT_ENV_VAR)
    if configured_root:
        return Path(configured_root).expanduser()
    return Path.home() / ".local" / "share" / "mena"


def create_session(
    *,
    profile: CaptureProfileName,
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
        profile=profile.value,
    )
    record_path = bundle_path / SESSION_RECORD_FILENAME
    record_path.write_text(
        json.dumps(asdict(record), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return CreatedSession(bundle_path=bundle_path, record_path=record_path, record=record)


def _sessions_root(data_root: Path | None) -> Path:
    return (data_root or default_data_root()) / "sessions"


def _bundle_directory_name(session_id: str, created_at: datetime) -> str:
    timestamp = created_at.isoformat(timespec="seconds").replace("+00:00", "Z").replace(":", "")
    return f"{session_id}_{timestamp}"
