"""Exclusive per-session orchestrator lock."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import socket
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from uuid import uuid4

from mena.timestamps import utc_timestamp

ORCHESTRATOR_LOCK_FILENAME = "orchestrator.lock"
RECLAIM_GUARD_FILENAME = f".{ORCHESTRATOR_LOCK_FILENAME}.reclaim"
_RECLAIM_GUARD_MUTEX = threading.Lock()


class SessionLockContention(RuntimeError):
    """Raised when another orchestrator already holds session authority."""


@dataclass(frozen=True)
class SessionLockInfo:
    pid: int | None
    hostname: str | None
    acquired_at: str | None
    command: str | None
    owner_token: str | None
    process_start_ticks: int | None

    @classmethod
    def from_mapping(cls, data: object) -> SessionLockInfo:
        if not isinstance(data, dict):
            return cls(
                pid=None,
                hostname=None,
                acquired_at=None,
                command=None,
                owner_token=None,
                process_start_ticks=None,
            )
        pid = data.get("pid")
        hostname = data.get("hostname")
        acquired_at = data.get("acquired_at")
        command = data.get("command")
        owner_token = data.get("owner_token")
        process_start_ticks = data.get("process_start_ticks")
        return cls(
            pid=pid if type(pid) is int else None,
            hostname=hostname if isinstance(hostname, str) else None,
            acquired_at=acquired_at if isinstance(acquired_at, str) else None,
            command=command if isinstance(command, str) else None,
            owner_token=owner_token if isinstance(owner_token, str) else None,
            process_start_ticks=(process_start_ticks if type(process_start_ticks) is int else None),
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
        "process_start_ticks": _current_process_start_ticks(),
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
    if not _pid_is_alive(holder.pid):
        return True
    if holder.process_start_ticks is None:
        return False
    process_start_ticks = _process_start_ticks(holder.pid)
    return process_start_ticks is not None and process_start_ticks != holder.process_start_ticks


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


def _current_process_start_ticks() -> int | None:
    return _process_start_ticks(os.getpid())


def _process_start_ticks(pid: int) -> int | None:
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        fields_after_command = stat_text.rsplit(") ", maxsplit=1)[1].split()
        start_ticks = fields_after_command[19]
    except IndexError:
        return None
    try:
        return int(start_ticks)
    except ValueError:
        return None


def _create_exclusive_json(path: Path, payload: dict[str, object]) -> None:
    temp_path = path.with_name(f".{path.name}.{uuid4()}.tmp")
    try:
        temp_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _link_path(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _reclaim_stale_lock(
    path: Path,
    *,
    payload: dict[str, object],
    expected_holder: SessionLockInfo,
) -> None:
    guard_path = path.with_name(RECLAIM_GUARD_FILENAME)
    guard = _acquire_reclaim_guard(
        path=guard_path,
        payload=payload,
        session_dir=path.parent,
    )
    try:
        holder = _read_lock_info(path)
        if holder != expected_holder or not _is_reclaimable_stale_lock(holder):
            raise SessionLockContention(
                _contention_message(holder=holder, session_dir=path.parent)
            ) from None
        _replace_stale_lock(path, payload)
    finally:
        _release_reclaim_guard(guard)


@dataclass(frozen=True)
class _HeldReclaimGuard:
    path: Path
    fd: int
    local_mutex_acquired: bool


def _acquire_reclaim_guard(
    *,
    path: Path,
    payload: dict[str, object],
    session_dir: Path,
) -> _HeldReclaimGuard:
    local_mutex_acquired = _RECLAIM_GUARD_MUTEX.acquire(blocking=False)
    if not local_mutex_acquired:
        holder = _read_lock_info(path)
        raise SessionLockContention(
            _contention_message(holder=holder, session_dir=session_dir)
        ) from None
    try:
        while True:
            fd = _open_reclaim_guard_file(path)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                holder = _read_lock_info(path)
                os.close(fd)
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise
                if not _is_reclaimable_stale_lock(holder):
                    raise SessionLockContention(
                        _contention_message(holder=holder, session_dir=session_dir)
                    ) from None
                continue
            if not _fd_matches_path(fd, path):
                os.close(fd)
                continue
            _write_json_to_fd(fd, payload)
            return _HeldReclaimGuard(
                path=path,
                fd=fd,
                local_mutex_acquired=local_mutex_acquired,
            )
    except BaseException:
        _RECLAIM_GUARD_MUTEX.release()
        raise


def _replace_stale_lock(path: Path, payload: dict[str, object]) -> None:
    temp_path = path.with_name(f".{path.name}.{uuid4()}.tmp")
    try:
        temp_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _link_path(source: Path, destination: Path) -> None:
    os.link(source, destination)


def _open_reclaim_guard_file(path: Path) -> int:
    return os.open(path, os.O_CREAT | os.O_RDWR, 0o644)


def _write_json_to_fd(fd: int, payload: dict[str, object]) -> None:
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, encoded)
    os.fsync(fd)


def _fd_matches_path(fd: int, path: Path) -> bool:
    try:
        fd_stat = os.fstat(fd)
        path_stat = path.stat()
    except OSError:
        return False
    return fd_stat.st_dev == path_stat.st_dev and fd_stat.st_ino == path_stat.st_ino


def _release_reclaim_guard(guard: _HeldReclaimGuard) -> None:
    try:
        if _fd_matches_path(guard.fd, guard.path):
            guard.path.unlink(missing_ok=True)
    finally:
        os.close(guard.fd)
        if guard.local_mutex_acquired:
            _RECLAIM_GUARD_MUTEX.release()


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
