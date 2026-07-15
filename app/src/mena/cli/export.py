"""``mena export`` parser and console handler."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from mena import export_workflow
from mena.artifacts import (
    ArtifactStoreError,
)
from mena.cli._console import _resolve_console_input
from mena.cli.command import bind_handler
from mena.cli.composition import CommandContext
from mena.export import SummaryMarkdownExportError
from mena.interaction import (
    ChoiceOption,
    ChoiceRequest,
    ConfirmationRequest,
    InteractionPort,
    TextRequest,
    WorkflowNotice,
)
from mena.session_integrity import (
    SessionIntegrityError,
)
from mena.session_lock import SessionLockContention
from mena.settings import AppSettings


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
            settings=_settings_with_sessions_root(context.settings, context.sessions_root),
            interaction=_resolve_export_interaction(dependencies.input_fn),
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


class ConsoleExportInteraction:
    """Console adapter for export workflow interaction topics."""

    def __init__(self, input_fn: Callable[[str], str] | None) -> None:
        self._input_fn = input_fn

    def choose(self, request: ChoiceRequest) -> ChoiceOption:
        raise ValueError(f"Unsupported export choice topic '{request.topic}'.")

    def confirm(self, request: ConfirmationRequest) -> bool:
        if request.topic != "export.format.markdown":
            raise ValueError(f"Unsupported export confirmation topic '{request.topic}'.")
        if self._input_fn is None:
            return True
        print("Available export format:")
        print("  1) Markdown (.md)")
        answer = self._input_fn("Export as Markdown notes? [Y/n]: ")
        return answer.strip().lower() in {"", "1", "y", "yes"}

    def request_text(self, request: TextRequest) -> str:
        raise ValueError(f"Unsupported export text topic '{request.topic}'.")

    def notify(self, notice: WorkflowNotice) -> None:
        topic = notice.topic
        context = notice.context
        if topic == "export.session_selected.latest":
            print(f"Using latest summarized session: {context['session_path']}")
        elif topic == "export.session_selected.summary_artifact":
            print(f"Using session containing requested summary artifact: {context['session_path']}")
        elif topic == "export.completed":
            print(f"Markdown notes exported to: {context['export_path']}")
        else:
            raise ValueError(f"Unsupported export notice topic '{topic}'.")


def _run_export_command(
    *,
    session_path: Path | None,
    summary_artifact_id: str | None,
    export_format: export_workflow.ExportFormat | None,
    settings: AppSettings,
    input_fn: Callable[[str], str] | None,
) -> int:
    result = export_workflow.run_export_workflow(
        export_workflow.ExportCommandInput(
            session_path=session_path,
            summary_artifact_id=summary_artifact_id,
            export_format=export_format,
        ),
        settings=settings,
        interaction=_resolve_export_interaction(input_fn),
    )
    if result.status == "cancelled":
        print("Export cancelled.")
        return 1
    return 0


def _settings_with_sessions_root(settings: AppSettings, sessions_root: Path) -> AppSettings:
    return replace(
        settings,
        paths=replace(settings.paths, sessions_dir=sessions_root),
    )


def _resolve_export_interaction(
    input_fn: Callable[[str], str] | None,
) -> InteractionPort:
    return ConsoleExportInteraction(_resolve_console_input(input_fn))
