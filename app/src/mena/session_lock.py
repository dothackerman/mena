"""Exclusive per-session orchestrator lock."""

from __future__ import annotations

import errno
import json
import os
import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from uuid import uuid4

from mena.fileops import write_json_atomically
from mena.timestamps import utc_timestamp

ORCHESTRATOR_LOCK_FILENAME = "orchestrator.lock"
RECLAIM_GUARD_FILENAME = f".{ORCHESTRATOR_LOCK_FILENAME}.reclaim"


class SessionLockContention(RuntimeError):
    """Raised when another orchestrator already holds session authority."""


@dataclass(frozen=True)
class SessionLockInfo:
    pid: int | None
    hostname: str | None
    acquired_at: str | None
    command: str | None
    owner_token: str | None

    @classmethod
    def from_mapping(cls, data: object) -> SessionLockInfo:
        if not isinstance(data, dict):
            return cls(
                pid=None,
                hostname=None,
                acquired_at=None,
                command=None,
                owner_token=None,
            )
        pid = data.get("pid")
        hostname = data.get("hostname")
        acquired_at = data.get("acquired_at")
        command = data.get("command")
        owner_token = data.get("owner_token")
        return cls(
            pid=pid if type(pid) is int else None,
            hostname=hostname if isinstance(hostname, str) else None,
            acquired_at=acquired_at if isinstance(acquired_at, str) else None,
            command=command if isinstance(command, str) else None,
            owner_token=owner_token if isinstance(owner_token, str) else None,
        )


class SessionLock:
    """Context manager owning one session-bundle lock file."""

    def __init__(self, *, session_dir: Path, command: str) -> None:
        self.session_dir = session_dir
        self.command = command
        self.lock_path = session_dir / ORCHESTRATOR_LOCK_FILENAME
        self._owner_token = str(uuid4())
        self._owner_payload: dict[str, object] | None = None
        self._held = False

    def __enter__(self) -> SessionLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()

    def acquire(self) -> None:
        payload = _lock_payload(command=self.command, owner_token=self._owner_token)
        try:
            _create_exclusive_json(self.lock_path, payload)
        except FileExistsError:
            holder = _read_lock_info(self.lock_path)
            if _is_reclaimable_stale_lock(holder):
                _reclaim_stale_lock(
                    self.lock_path,
                    payload=payload,
                    expected_holder=holder,
                )
            else:
                raise SessionLockContention(
                    _contention_message(
                        holder=holder,
                        session_dir=self.session_dir,
                    )
                ) from None
        self._owner_payload = payload
        self._held = True

    def release(self) -> None:
        if not self._held:
            return
        try:
            _unlink_if_payload_matches(self.lock_path, self._owner_payload)
        except FileNotFoundError:
            pass
        finally:
            self._held = False
            self._owner_payload = None


def acquire_session_lock(*, session_dir: Path, command: str) -> SessionLock:
    """Return a context manager for exclusive orchestrator authority."""

    return SessionLock(session_dir=session_dir, command=command)


def _lock_payload(*, command: str, owner_token: str) -> dict[str, object]:
    return {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "acquired_at": utc_timestamp(datetime.now(UTC)),
        "command": command,
        "owner_token": owner_token,
    }


def _read_json_object(path: Path) -> dict[str, object] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _read_lock_info(path: Path) -> SessionLockInfo:
    data = _read_json_object(path)
    return SessionLockInfo.from_mapping(data)


def _is_reclaimable_stale_lock(holder: SessionLockInfo) -> bool:
    if holder.hostname != socket.gethostname() or holder.pid is None:
        return False
    return not _pid_is_alive(holder.pid)


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH
    return True


def _create_exclusive_json(path: Path, payload: dict[str, object]) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    fd = os.open(path, flags, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _reclaim_stale_lock(
    path: Path,
    *,
    payload: dict[str, object],
    expected_holder: SessionLockInfo,
) -> None:
    guard_path = path.with_name(RECLAIM_GUARD_FILENAME)
    _acquire_reclaim_guard(path=guard_path, payload=payload, session_dir=path.parent)
    try:
        holder = _read_lock_info(path)
        if holder != expected_holder or not _is_reclaimable_stale_lock(holder):
            raise SessionLockContention(
                _contention_message(holder=holder, session_dir=path.parent)
            ) from None
        _replace_stale_lock(path, payload)
    finally:
        _unlink_if_payload_matches(guard_path, payload)


def _acquire_reclaim_guard(
    *,
    path: Path,
    payload: dict[str, object],
    session_dir: Path,
) -> None:
    while True:
        try:
            _create_exclusive_json(path, payload)
            return
        except FileExistsError:
            holder = _read_lock_info(path)
            if not _is_reclaimable_stale_lock(holder):
                raise SessionLockContention(
                    _contention_message(holder=holder, session_dir=session_dir)
                ) from None
            path.unlink(missing_ok=True)


def _replace_stale_lock(path: Path, payload: dict[str, object]) -> None:
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    write_json_atomically(temp_path, payload)
    os.replace(temp_path, path)


def _unlink_if_payload_matches(
    path: Path,
    expected_payload: dict[str, object] | None,
) -> None:
    if expected_payload is None:
        return
    current_payload = _read_json_object(path)
    if current_payload == expected_payload:
        path.unlink(missing_ok=True)


def _contention_message(*, holder: SessionLockInfo, session_dir: Path) -> str:
    pid = holder.pid if holder.pid is not None else "unknown"
    command = holder.command if holder.command else "unknown"
    return (
        "Session is already locked by orchestrator "
        f"pid={pid} command={command} for session {session_dir}."
    )
