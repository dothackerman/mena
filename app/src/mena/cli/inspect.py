"""``mena inspect`` parser and rendering handler."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mena.artifacts import (
    ArtifactStoreError,
)
from mena.cli.command import bind_handler
from mena.cli.composition import CommandContext
from mena.session_integrity import (
    verify_session_bundle,
)


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    *,
    session_parent: argparse.ArgumentParser,
    **_: object,
) -> None:
    parser = subparsers.add_parser(
        "inspect",
        parents=[session_parent],
        help="Verify and report a session bundle's integrity.",
    )
    bind_handler(parser, run)


def run(args: argparse.Namespace, context: CommandContext) -> int:
    try:
        return _run_inspect_command(session_path=Path(args.session_path))
    except (ArtifactStoreError, ValueError, OSError) as exc:
        print(f"Cannot inspect session: {exc}")
        return 1


def _run_inspect_command(
    *,
    session_path: Path,
) -> int:
    report = verify_session_bundle(session_path)
    print(f"Integrity report for {session_path}: {'OK' if report.ok else 'FAILED'}")
    print(json.dumps(report.to_dict(), sort_keys=True, indent=2))
    return 0 if report.ok else 1
