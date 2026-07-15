"""``mena export`` parser and console handler."""

from __future__ import annotations

import argparse
from pathlib import Path

from mena import export_workflow
from mena.artifacts import ArtifactStoreError
from mena.cli import _shared
from mena.cli.command import bind_handler
from mena.cli.composition import CommandContext
from mena.export import SummaryMarkdownExportError
from mena.session_integrity import SessionIntegrityError
from mena.session_lock import SessionLockContention


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    *,
    optional_session_parent: argparse.ArgumentParser,
    **_: object,
) -> None:
    parser = subparsers.add_parser(
        "export",
        parents=[optional_session_parent],
        help="Render a persisted note package as Markdown.",
    )
    parser.add_argument(
        "--format",
        choices=("markdown",),
        dest="export_format",
        default=None,
        help="Export output format.",
    )
    parser.add_argument(
        "--summary-artifact-id",
        dest="summary_artifact_id",
        default=None,
        help="Optional explicit summary artifact id to export.",
    )
    bind_handler(parser, run)


def run(args: argparse.Namespace, context: CommandContext) -> int:
    dependencies = context.dependencies
    try:
        result = export_workflow.run_export_workflow(
            export_workflow.ExportCommandInput(
                session_path=Path(args.session_path) if args.session_path else None,
                summary_artifact_id=args.summary_artifact_id,
                export_format=args.export_format,
            ),
            settings=_shared._settings_with_sessions_root(context.settings, context.sessions_root),
            interaction=_shared._resolve_export_interaction(dependencies.input_fn),
        )
        if result.status == "cancelled":
            print("Export cancelled.")
            return 1
        return 0
    except SessionLockContention as exc:
        print(f"Cannot export session summary markdown: {exc}")
        return 4
    except (
        ArtifactStoreError,
        SummaryMarkdownExportError,
        SessionIntegrityError,
        OSError,
        ValueError,
    ) as exc:
        print(f"Cannot export session summary markdown: {exc}")
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Export cancelled.")
        return 1
