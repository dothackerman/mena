"""CLI adapter for the mena PoC."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

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
    for command in COMMANDS:
        subparser = subparsers.add_parser(command)
        subparser.set_defaults(command=command)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        from mena import __version__

        print(__version__)
        return 0

    if args.command is None:
        parser.print_help()
        return 0

    print(f"Command '{args.command}' is not implemented yet.")
    return 0

