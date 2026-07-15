"""``mena list`` parser and rendering handler."""

from __future__ import annotations

import argparse

from mena.cli import _shared
from mena.cli.command import bind_handler
from mena.cli.composition import CommandContext


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser], **_: object) -> None:
    parser = subparsers.add_parser("list", help="List discovered session bundles.")
    bind_handler(parser, run)


def run(args: argparse.Namespace, context: CommandContext) -> int:
    return _shared._run_list_command(sessions_root=context.sessions_root)
