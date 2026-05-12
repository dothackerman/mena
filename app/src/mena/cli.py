"""CLI adapter for the mena PoC."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path

from mena.discovery import AudioDiscovery, discover_audio_routes, format_discovery
from mena.profiles import capture_profile_choices, parse_capture_profile
from mena.sessions import create_session, update_session_discovery

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


def main(
    argv: Sequence[str] | None = None,
    *,
    data_root: Path | None = None,
    discoverer: DiscoveryFn = discover_audio_routes,
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
        if args.dry_run:
            discovery = discoverer(profile.id)
            update_session_discovery(created_session.record_path, discovery.to_dict())
            print(format_discovery(discovery))
        return 0

    print(f"Command '{args.command}' is not implemented yet.")
    return 0
