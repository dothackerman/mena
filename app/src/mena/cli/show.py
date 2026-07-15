"""Explicit placeholder contract for ``mena show``."""

from __future__ import annotations

import argparse

from mena.cli.command import bind_handler
from mena.cli.composition import CommandContext


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser], **_: object) -> None:
    parser = subparsers.add_parser("show", help="Planned; not implemented in the current PoC.")
    bind_handler(parser, run)


def run(args: argparse.Namespace, context: CommandContext) -> int:
    print("Command 'show' is not implemented yet.")
    return 0
