"""``mena summarize`` parser and console handler."""

from __future__ import annotations

import argparse

from mena import summarize_workflow
from mena.artifacts import ArtifactStoreError
from mena.cli import _shared
from mena.cli.command import bind_handler
from mena.cli.composition import CommandContext
from mena.correction_overlay import CorrectionOverlayContractError
from mena.manual_notes import ManualNotesIngestionError
from mena.runtime_profile import RuntimeFallbackUnavailable
from mena.session_lock import SessionLockContention
from mena.settings import SettingsError, validate_summary_language
from mena.summarization import SummarizationError

workflow = summarize_workflow


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    *,
    optional_session_parent: argparse.ArgumentParser,
    runtime_fallback_parent: argparse.ArgumentParser,
    **_: object,
) -> None:
    parser = subparsers.add_parser(
        "summarize", parents=[optional_session_parent, runtime_fallback_parent]
    )
    parser.add_argument(
        "--manual-notes", dest="manual_notes", help="Optional path to manual notes file."
    )
    parser.add_argument(
        "--language", default=None, help="Language code for the summary model prompt."
    )
    bind_handler(parser, run)


def run(args: argparse.Namespace, context: CommandContext) -> int:
    dependencies = context.dependencies
    try:
        interaction = _shared.ConsoleSummarizeInteraction(dependencies.input_fn)
        session_path = summarize_workflow.resolve_summarize_session_path(
            explicit_session_path=args.session_path,
            sessions_root=context.sessions_root,
            interaction=interaction,
        )
        return summarize_workflow.run_summarization_command(
            session_path=session_path,
            manual_notes=args.manual_notes,
            language=None
            if args.language is None
            else validate_summary_language(args.language, "--language"),
            settings=context.settings,
            setup_runner=dependencies.setup_runner,
            setup_command_identity=dependencies.setup_command_identity,
            setup_hardware=dependencies.setup_hardware,
            allow_runtime_fallback=args.allow_runtime_fallback,
            summarization_provider_factory=dependencies.summarization_provider_factory,
            interaction=interaction,
        )
    except SessionLockContention as exc:
        print(f"Cannot summarize session: {exc}")
        return 4
    except RuntimeFallbackUnavailable as exc:
        print(f"Cannot summarize session: {exc}")
        return 3
    except (
        ArtifactStoreError,
        CorrectionOverlayContractError,
        ManualNotesIngestionError,
        SettingsError,
        SummarizationError,
        ValueError,
    ) as exc:
        print(f"Cannot summarize session: {exc}")
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Summarization cancelled.")
        return 1
