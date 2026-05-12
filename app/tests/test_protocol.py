import subprocess
import sys
from io import BytesIO, StringIO
from pathlib import Path

import pytest

from mena.capture_worker import build_parser, run_worker
from mena.protocol import (
    MAX_FRAME_BYTES,
    PROTOCOL_MAJOR,
    ProtocolError,
    ProtocolMessage,
    WorkerExitedError,
    WorkerProcess,
    hello_message,
    read_message,
    validate_hello,
)


def test_protocol_message_encodes_compact_json_line() -> None:
    message = ProtocolMessage(
        type="start_capture",
        id="message-1",
        session_id="session-1",
        payload={"profile": "online_dual_stream"},
    )

    encoded = message.encode()

    assert encoded.endswith(b"\n")
    assert b" " not in encoded
    assert ProtocolMessage.decode(encoded) == message


def test_protocol_rejects_unknown_message_type() -> None:
    line = (
        b'{"v":1,"type":"pause_capture","id":"message-1","session_id":"session-1","payload":{}}\n'
    )

    with pytest.raises(ProtocolError, match="Unknown protocol message type 'pause_capture'"):
        ProtocolMessage.decode(line)


def test_protocol_encode_rejects_unknown_message_type() -> None:
    with pytest.raises(ProtocolError, match="Unknown protocol message type 'pause_capture'"):
        ProtocolMessage(type="pause_capture", session_id="session-1").encode()


@pytest.mark.parametrize(
    ("line", "message"),
    [
        (b'{"v":1\n', "valid JSON"),
        (b"[]\n", "JSON object"),
        (
            b'{"v":1,"type":"hello","id":"message-1","session_id":"session-1"}\n',
            "payload",
        ),
        (
            b'{"v":"1","type":"hello","id":"message-1","session_id":"session-1","payload":{}}\n',
            "integer",
        ),
        (
            b'{"v":1,"type":"hello","id":"message-1","session_id":"","payload":{}}\n',
            "non-empty string",
        ),
        (b"\xff\n", "valid UTF-8"),
    ],
)
def test_protocol_rejects_malformed_frames(line: bytes, message: str) -> None:
    with pytest.raises(ProtocolError, match=message):
        ProtocolMessage.decode(line)


def test_protocol_rejects_oversized_frame() -> None:
    with pytest.raises(ProtocolError, match="exceeds"):
        ProtocolMessage.decode(b"{" + (b'"x":' + b'"a"' * MAX_FRAME_BYTES) + b"}\n")


def test_read_message_rejects_truncated_frame(tmp_path: Path) -> None:
    path = tmp_path / "frame.jsonl"
    path.write_bytes(
        b'{"v":1,"type":"hello","id":"message-1","session_id":"session-1","payload":{}}'
    )

    with path.open("rb") as stream, pytest.raises(ProtocolError, match="LF terminated"):
        read_message(stream)


def test_hello_version_mismatch_is_protocol_error() -> None:
    message = hello_message("session-1")
    bad_hello = ProtocolMessage(
        type=message.type,
        id=message.id,
        session_id=message.session_id,
        payload={**message.payload, "protocol_major": PROTOCOL_MAJOR + 1},
    )

    with pytest.raises(ProtocolError, match="Unsupported protocol major version"):
        validate_hello(bad_hello)


def test_validate_hello_requires_hello_type_and_worker_role() -> None:
    with pytest.raises(ProtocolError, match="First protocol message must be hello"):
        validate_hello(ProtocolMessage(type="start_capture", session_id="session-1"))

    with pytest.raises(ProtocolError, match="Hello role must be 'worker'"):
        validate_hello(
            ProtocolMessage(
                type="hello",
                session_id="session-1",
                payload={"protocol_major": PROTOCOL_MAJOR, "protocol_minor": 0, "role": "host"},
            )
        )


def test_read_message_reports_eof() -> None:
    with pytest.raises(EOFError, match="closed stdout"):
        read_message(BytesIO())


def test_capture_worker_parser_requires_session_fields(tmp_path: Path) -> None:
    args = build_parser().parse_args(["--session-id", "session-1", "--session-dir", str(tmp_path)])

    assert args.session_id == "session-1"
    assert args.session_dir == tmp_path


def test_worker_process_handshake_start_stop_round_trip(tmp_path: Path) -> None:
    with WorkerProcess(session_id="session-1", session_dir=tmp_path) as worker:
        hello_ack = worker.handshake()
        started = worker.start_capture({"profile": "online_dual_stream"})
        finalized = worker.stop_capture()
        exit_code = worker.wait(timeout=2)
        stderr = worker.read_stderr()

    assert hello_ack.type == "hello_ack"
    assert started.payload == {"session_dir": str(tmp_path)}
    assert finalized.payload == {"status": "stopped"}
    assert exit_code == 0
    assert stderr == ""


def test_worker_run_loop_handles_start_stop_with_in_memory_streams(tmp_path: Path) -> None:
    stdin = BytesIO(
        b"".join(
            [
                hello_message("session-1").encode(),
                ProtocolMessage(type="start_capture", session_id="session-1").encode(),
                ProtocolMessage(type="stop_capture", session_id="session-1").encode(),
            ]
        )
    )
    stdout = BytesIO()
    stderr = StringIO()

    exit_code = run_worker(
        session_id="session-1",
        session_dir=tmp_path,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
    )

    frames = stdout.getvalue().splitlines(keepends=True)
    assert exit_code == 0
    assert [ProtocolMessage.decode(frame).type for frame in frames] == [
        "hello_ack",
        "capture_started",
        "capture_finalized",
    ]
    assert stderr.getvalue() == ""


def test_worker_run_loop_stops_on_orchestrator_eof(tmp_path: Path) -> None:
    stdin = BytesIO(hello_message("session-1").encode())
    stdout = BytesIO()
    stderr = StringIO()

    exit_code = run_worker(
        session_id="session-1",
        session_dir=tmp_path,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert ProtocolMessage.decode(stdout.getvalue()).type == "hello_ack"
    assert "orchestrator closed protocol stdin" in stderr.getvalue()


def test_worker_run_loop_rejects_unexpected_command(tmp_path: Path) -> None:
    stdin = BytesIO(
        hello_message("session-1").encode()
        + ProtocolMessage(type="capture_started", session_id="session-1").encode()
    )

    with pytest.raises(ProtocolError, match="Unexpected worker command 'capture_started'"):
        run_worker(session_id="session-1", session_dir=tmp_path, stdin=stdin, stdout=BytesIO())


def test_worker_run_loop_rejects_mismatched_session_id(tmp_path: Path) -> None:
    stdin = BytesIO(
        hello_message("session-1").encode()
        + ProtocolMessage(type="start_capture", session_id="session-2").encode()
    )

    with pytest.raises(ProtocolError, match="session_id does not match"):
        run_worker(session_id="session-1", session_dir=tmp_path, stdin=stdin, stdout=BytesIO())


def test_worker_process_context_terminates_running_child(tmp_path: Path) -> None:
    with WorkerProcess(
        session_id="session-1",
        session_dir=tmp_path,
        argv=[sys.executable, "-c", "import time; time.sleep(60)"],
    ) as worker:
        process = worker.process

    assert process.poll() is not None


def test_worker_version_mismatch_exits_with_diagnostics_only_on_stderr(tmp_path: Path) -> None:
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "mena.capture_worker",
            "--session-id",
            "session-1",
            "--session-dir",
            str(tmp_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    process.stdin.write(
        ProtocolMessage(
            type="hello",
            session_id="session-1",
            payload={"protocol_major": 99, "protocol_minor": 0, "role": "worker"},
        ).encode()
    )
    process.stdin.close()

    assert process.wait(timeout=2) == 2
    assert process.stdout.read() == b""
    assert b"protocol error:" in process.stderr.read()


def test_worker_process_detects_child_exit_before_event(tmp_path: Path) -> None:
    with (
        WorkerProcess(
            session_id="session-1",
            session_dir=tmp_path,
            argv=[sys.executable, "-c", "raise SystemExit(3)"],
        ) as worker,
        pytest.raises(WorkerExitedError, match="Worker exited"),
    ):
        worker.handshake()
