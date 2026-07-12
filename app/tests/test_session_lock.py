import concurrent.futures
import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

from mena.session_lock import (
    ORCHESTRATOR_LOCK_FILENAME,
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
