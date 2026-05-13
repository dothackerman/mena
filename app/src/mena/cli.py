"""CLI adapter for the mena PoC."""

from __future__ import annotations

import argparse
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from mena.discovery import AudioDiscovery, DiscoveryStatus, discover_audio_routes, format_discovery
from mena.profiles import capture_profile_choices, parse_capture_profile
from mena.protocol import ProtocolError, WorkerExitedError, WorkerProcess
from mena.sessions import create_session, update_session_capture_metadata, update_session_discovery

COMMANDS = (
    "record",
    "list",
    "show",
    "process",
    "correct",
    "export",
    "resume",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mena",
        description="Privacy-first local meeting notes PoC.",
    )
    parser.add_argument("--version", action="store_true", help="Show the package version.")

    subparsers = parser.add_subparsers(dest="command")
    record_parser = subparsers.add_parser("record", help="Create a recording session.")
    record_parser.add_argument(
        "--profile",
        choices=capture_profile_choices(),
        required=True,
        help="Capture profile for this recording session.",
    )
    record_parser.add_argument("--name", help="Friendly session name for human reference.")
    record_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Create a session and print read-only audio discovery without starting capture.",
    )
    record_parser.set_defaults(command="record")

    for command in COMMANDS[1:]:
        subparser = subparsers.add_parser(command)
        subparser.set_defaults(command=command)

    return parser


DiscoveryFn = Callable[..., AudioDiscovery]
WorkerFactory = Callable[..., Any]


def main(
    argv: Sequence[str] | None = None,
    *,
    data_root: Path | None = None,
    discoverer: DiscoveryFn = discover_audio_routes,
    worker_factory: WorkerFactory = WorkerProcess,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        from mena import __version__

        print(__version__)
        return 0

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "record":
        profile = parse_capture_profile(args.profile)
        created_session = create_session(profile=profile, name=args.name, data_root=data_root)
        print(f"Created session {created_session.record.id}")
        print(f"Session record: {created_session.record_path}")
        discovery = discoverer(profile.id)
        update_session_discovery(created_session.record_path, discovery.to_dict())
        print(format_discovery(discovery))
        if args.dry_run:
            return 0
        if discovery.status is not DiscoveryStatus.AVAILABLE:
            print("Capture not started: audio discovery is unavailable.")
            return 1
        with worker_factory(
            session_id=created_session.record.id,
            session_dir=created_session.bundle_path,
        ) as worker:
            try:
                worker.handshake()
                worker.start_capture({"profile": profile.id.value})
                finalized = worker.stop_capture()
                exit_code = worker.wait(timeout=2)
                if exit_code != 0:
                    raise WorkerExitedError(f"Worker exited with status {exit_code}.")
            except (ProtocolError, WorkerExitedError, subprocess.TimeoutExpired) as exc:
                print(f"Capture failed: {exc}")
                return 1
        update_session_capture_metadata(created_session.record_path, finalized.payload)
        print("Capture finalized")
        return 0

    print(f"Command '{args.command}' is not implemented yet.")
    return 0
