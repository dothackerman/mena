"""``mena inspect`` parser and rendering handler."""

from __future__ import annotations

import argparse
from pathlib import Path

from mena.artifacts import ArtifactStoreError
from mena.cli import _shared
from mena.cli.command import bind_handler
from mena.cli.composition import CommandContext

shared = _shared


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    *,
    session_parent: argparse.ArgumentParser,
    **_: object,
) -> None:
    parser = subparsers.add_parser("inspect", parents=[session_parent])
    bind_handler(parser, run)


def run(args: argparse.Namespace, context: CommandContext) -> int:
    try:
        return _shared._run_inspect_command(session_path=Path(args.session_path))
    except (ArtifactStoreError, ValueError, OSError) as exc:
        print(f"Cannot inspect session: {exc}")
        return 1
