"""``mena correct`` parser and console handler."""

from __future__ import annotations

import argparse
from pathlib import Path

from mena.artifacts import ArtifactStoreError
from mena.cli import _shared
from mena.cli.command import bind_handler
from mena.cli.composition import CommandContext
from mena.correction_workflow import CorrectionWorkflowError, run_correction_workflow
from mena.session_lock import SessionLockContention


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    *,
    session_parent: argparse.ArgumentParser,
    **_: object,
) -> None:
    parser = subparsers.add_parser("correct", parents=[session_parent])
    bind_handler(parser, run)


def run(args: argparse.Namespace, context: CommandContext) -> int:
    dependencies = context.dependencies
    try:
        run_correction_workflow(
            session_path=Path(args.session_path),
            now=dependencies.now,
            interaction=_shared.ConsoleCorrectionInteraction(dependencies.input_fn),
            playback_player=dependencies.playback_player,
        )
    except SessionLockContention as exc:
        print(f"Cannot run correction workflow: {exc}")
        return 4
    except (ArtifactStoreError, CorrectionWorkflowError) as exc:
        print(f"Cannot run correction workflow: {exc}")
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Correction workflow cancelled.")
        return 1
    return 0
