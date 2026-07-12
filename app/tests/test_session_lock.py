import concurrent.futures
import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

import mena.session_lock as session_lock_module
from mena.session_lock import (
    ORCHESTRATOR_LOCK_FILENAME,
    RECLAIM_GUARD_FILENAME,
    SessionLockContention,
    acquire_session_lock,
)


def test_session_lock_rejects_second_mutating_command_with_holder_details(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    with (
        acquire_session_lock(session_dir=session_dir, command="process"),
        pytest.raises(SessionLockContention) as exc_info,
        acquire_session_lock(session_dir=session_dir, command="summarize"),
    ):
        raise AssertionError("second command should not acquire the lock")

    message = str(exc_info.value)
    assert f"pid={os.getpid()}" in message
    assert "command=process" in message
    assert str(session_dir) in message


def test_session_lock_retries_when_lock_disappears_during_acquire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    lock_path = session_dir / ORCHESTRATOR_LOCK_FILENAME
    lock_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "hostname": socket.gethostname(),
                "acquired_at": "2026-07-12T18:00:00Z",
                "command": "process",
                "owner_token": "released",
                "process_start_ticks": 100,
            }
        ),
        encoding="utf-8",
    )
    original_create = session_lock_module._create_exclusive_json
    release_before_read_pending = True

    def _release_before_holder_read(
        path: Path,
        payload: dict[str, object],
    ) -> None:
        nonlocal release_before_read_pending
        if release_before_read_pending:
            release_before_read_pending = False
            assert path == lock_path
            lock_path.unlink()
            raise FileExistsError
        original_create(path, payload)

    monkeypatch.setattr(
        session_lock_module,
        "_create_exclusive_json",
        _release_before_holder_read,
    )

    with acquire_session_lock(session_dir=session_dir, command="summarize"):
        payload = json.loads(lock_path.read_text(encoding="utf-8"))

    assert payload["command"] == "summarize"
    assert not lock_path.exists()


def test_session_lock_reclaims_same_host_dead_pid(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    lock_path = session_dir / ORCHESTRATOR_LOCK_FILENAME
    lock_path.write_text(
        json.dumps(
            {
                "pid": 999_999_999,
                "hostname": socket.gethostname(),
                "acquired_at": "2026-07-12T18:00:00Z",
                "command": "process",
            }
        ),
        encoding="utf-8",
    )

    with acquire_session_lock(session_dir=session_dir, command="summarize"):
        payload = json.loads(lock_path.read_text(encoding="utf-8"))

    assert payload["pid"] == os.getpid()
    assert payload["hostname"] == socket.gethostname()
    assert payload["command"] == "summarize"
    assert not lock_path.exists()


def test_session_lock_allows_only_one_concurrent_stale_reclaim(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    lock_path = session_dir / ORCHESTRATOR_LOCK_FILENAME
    lock_path.write_text(
        json.dumps(
            {
                "pid": 999_999_999,
                "hostname": socket.gethostname(),
                "acquired_at": "2026-07-12T18:00:00Z",
                "command": "process",
            }
        ),
        encoding="utf-8",
    )
    start = threading.Barrier(2)
    release = threading.Event()
    acquired: list[str] = []

    def _contend(command: str) -> str:
        start.wait(timeout=5)
        try:
            with acquire_session_lock(session_dir=session_dir, command=command):
                acquired.append(command)
                release.wait(timeout=5)
                return "acquired"
        except SessionLockContention:
            return "contended"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(_contend, "summarize"),
            executor.submit(_contend, "export"),
        ]
        try:
            for _ in range(500):
                if acquired or all(future.done() for future in futures):
                    break
                time.sleep(0.01)
        finally:
            release.set()
        results = [future.result(timeout=5) for future in futures]

    assert results.count("acquired") == 1
    assert results.count("contended") == 1
    assert len(acquired) == 1
    assert not lock_path.exists()


def test_session_lock_reclaim_guard_blocks_concurrent_reclaimer(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    lock_path = session_dir / ORCHESTRATOR_LOCK_FILENAME
    guard_path = session_dir / RECLAIM_GUARD_FILENAME
    stale_lock_payload = {
        "pid": 999_999_999,
        "hostname": socket.gethostname(),
        "acquired_at": "2026-07-12T18:00:00Z",
        "command": "process",
    }
    guard_payload = {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "acquired_at": "2026-07-12T18:05:00Z",
        "command": "summarize",
        "owner_token": "fresh",
        "process_start_ticks": 100,
    }
    lock_path.write_text(json.dumps(stale_lock_payload), encoding="utf-8")
    guard = session_lock_module._acquire_reclaim_guard(
        path=guard_path,
        payload=guard_payload,
        session_dir=session_dir,
    )

    try:
        with (
            pytest.raises(SessionLockContention, match="command=summarize"),
            acquire_session_lock(session_dir=session_dir, command="export"),
        ):
            raise AssertionError("active reclaim guard should block this contender")
    finally:
        session_lock_module._release_reclaim_guard(guard)

    assert json.loads(lock_path.read_text(encoding="utf-8")) == stale_lock_payload


def test_session_lock_ignores_obsolete_reclaim_guard_file_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    guard_path = session_dir / RECLAIM_GUARD_FILENAME
    first_payload = {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "acquired_at": "2026-07-12T18:00:00Z",
        "command": "summarize",
        "owner_token": "first",
        "process_start_ticks": 100,
    }
    second_payload = {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "acquired_at": "2026-07-12T18:05:00Z",
        "command": "export",
        "owner_token": "second",
        "process_start_ticks": 100,
    }
    guard = session_lock_module._acquire_reclaim_guard(
        path=guard_path,
        payload=first_payload,
        session_dir=session_dir,
    )
    stale_fd = os.open(guard_path, os.O_RDWR)
    session_lock_module._release_reclaim_guard(guard)
    original_open = session_lock_module._open_reclaim_guard_file
    stale_open_pending = True

    def _open_stale_guard_once(path: Path) -> int:
        nonlocal stale_open_pending
        if path == guard_path and stale_open_pending:
            stale_open_pending = False
            return stale_fd
        return original_open(path)

    monkeypatch.setattr(
        session_lock_module,
        "_open_reclaim_guard_file",
        _open_stale_guard_once,
    )

    second_guard = session_lock_module._acquire_reclaim_guard(
        path=guard_path,
        payload=second_payload,
        session_dir=session_dir,
    )
    try:
        assert json.loads(guard_path.read_text(encoding="utf-8")) == second_payload
    finally:
        session_lock_module._release_reclaim_guard(second_guard)

    assert not guard_path.exists()


def test_session_lock_publishes_only_complete_lock_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    lock_path = session_dir / ORCHESTRATOR_LOCK_FILENAME
    payload = {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "acquired_at": "2026-07-12T18:00:00Z",
        "command": "process",
        "owner_token": "owner",
        "process_start_ticks": 100,
    }
    original_link = session_lock_module._link_path

    def _interrupt_before_publish(src: Path | str, dst: Path | str) -> None:
        assert Path(dst) == lock_path
        assert not lock_path.exists()
        assert json.loads(Path(src).read_text(encoding="utf-8")) == payload
        raise KeyboardInterrupt

    monkeypatch.setattr(session_lock_module, "_link_path", _interrupt_before_publish)

    with pytest.raises(KeyboardInterrupt):
        session_lock_module._create_exclusive_json(lock_path, payload)

    assert not lock_path.exists()
    assert list(session_dir.iterdir()) == []

    monkeypatch.setattr(session_lock_module, "_link_path", original_link)
    session_lock_module._create_exclusive_json(lock_path, payload)
    assert json.loads(lock_path.read_text(encoding="utf-8")) == payload


def test_session_lock_reclaims_reused_same_host_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    lock_path = session_dir / ORCHESTRATOR_LOCK_FILENAME
    lock_path.write_text(
        json.dumps(
            {
                "pid": 1234,
                "hostname": socket.gethostname(),
                "acquired_at": "2026-07-12T18:00:00Z",
                "command": "process",
                "owner_token": "stale",
                "process_start_ticks": 100,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(session_lock_module, "_pid_is_alive", lambda _pid: True)
    monkeypatch.setattr(session_lock_module, "_process_start_ticks", lambda _pid: 200)

    with acquire_session_lock(session_dir=session_dir, command="summarize"):
        payload = json.loads(lock_path.read_text(encoding="utf-8"))

    assert payload["command"] == "summarize"
    assert payload["process_start_ticks"] == 200
    assert not lock_path.exists()


def test_session_lock_releases_on_normal_completion_and_keyboard_interrupt(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    lock_path = session_dir / ORCHESTRATOR_LOCK_FILENAME

    with acquire_session_lock(session_dir=session_dir, command="export"):
        assert lock_path.exists()
    assert not lock_path.exists()

    with (
        pytest.raises(KeyboardInterrupt),
        acquire_session_lock(session_dir=session_dir, command="correct"),
    ):
        assert lock_path.exists()
        raise KeyboardInterrupt

    assert not lock_path.exists()


def test_session_lock_release_does_not_remove_replaced_lock(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    lock_path = session_dir / ORCHESTRATOR_LOCK_FILENAME
    replacement = {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "acquired_at": "2026-07-12T18:05:00Z",
        "command": "summarize",
        "owner_token": "replacement",
    }

    with acquire_session_lock(session_dir=session_dir, command="process"):
        lock_path.write_text(json.dumps(replacement), encoding="utf-8")

    assert json.loads(lock_path.read_text(encoding="utf-8")) == replacement
