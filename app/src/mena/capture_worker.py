"""Capture worker subprocess entry point."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import IO, TextIO

from mena.protocol import (
    ProtocolError,
    ProtocolMessage,
    hello_ack_message,
    read_message,
    validate_hello,
    write_message,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mena.capture_worker")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--session-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run_worker(session_id=args.session_id, session_dir=args.session_dir)
    except ProtocolError as exc:
        print(f"protocol error: {exc}", file=sys.stderr)
        return 2


def run_worker(
    *,
    session_id: str,
    session_dir: Path,
    stdin: IO[bytes] | None = None,
    stdout: IO[bytes] | None = None,
    stderr: TextIO | None = None,
) -> int:
    protocol_in = stdin or sys.stdin.buffer
    protocol_out = stdout or sys.stdout.buffer
    diagnostics = stderr or sys.stderr

    hello = read_message(protocol_in)
    validate_hello(hello)
    write_message(protocol_out, hello_ack_message(session_id=session_id, request_id=hello.id))

    while True:
        try:
            message = read_message(protocol_in)
        except EOFError:
            print("orchestrator closed protocol stdin", file=diagnostics)
            return 0

        if message.session_id != session_id:
            raise ProtocolError("Message session_id does not match worker session.")
        if message.type == "start_capture":
            write_message(
                protocol_out,
                ProtocolMessage(
                    type="capture_started",
                    session_id=session_id,
                    payload={"session_dir": str(session_dir)},
                ),
            )
        elif message.type == "stop_capture":
            write_message(
                protocol_out,
                ProtocolMessage(
                    type="capture_finalized",
                    session_id=session_id,
                    payload={"status": "stopped"},
                ),
            )
            return 0
        else:
            raise ProtocolError(f"Unexpected worker command '{message.type}'.")


if __name__ == "__main__":
    raise SystemExit(main())
