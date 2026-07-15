"""``mena resume`` parser and console handler."""

from __future__ import annotations

import argparse

from mena.cli.command import bind_handler
from mena.cli.composition import CommandContext
from mena.cli.processing import run as run_processing


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    *,
    session_parent: argparse.ArgumentParser,
    runtime_fallback_parent: argparse.ArgumentParser,
    **_: object,
) -> None:
    parser = subparsers.add_parser("resume", parents=[session_parent, runtime_fallback_parent])
    bind_handler(parser, run)


def run(args: argparse.Namespace, context: CommandContext) -> int:
    return run_processing(args, context, resume=True)
