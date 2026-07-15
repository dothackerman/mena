"""``mena summarize`` parser and console handler."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from shlex import quote
from typing import cast

from mena import summarize_workflow
from mena.artifacts import (
    ArtifactStoreError,
)
from mena.cli._console import (
    _choice_default_index,
    _prompt_choice,
    _resolve_console_input,
)
from mena.cli._runtime_fallback import _print_runtime_stewardship_release_notice
from mena.cli.command import bind_handler
from mena.cli.composition import CommandContext, ProviderFactory
from mena.correction_overlay import CorrectionOverlayContractError
from mena.interaction import (
    ChoiceOption,
    ChoiceRequest,
    ConfirmationRequest,
    InteractionPort,
    TextRequest,
    WorkflowNotice,
)
from mena.manual_notes import ManualNotesIngestionError
from mena.runtime_profile import RuntimeFallbackUnavailable
from mena.runtime_stewardship import (
    RUNTIME_STEWARDSHIP_RELEASE_NOTICE_TOPIC,
    RuntimeReleaseResult,
)
from mena.session_lock import SessionLockContention
from mena.settings import AppSettings, SettingsError, validate_summary_language
from mena.setup import (
    CommandIdentity,
    CommandRunner,
    HardwareFacts,
)
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
        "summarize",
        parents=[optional_session_parent, runtime_fallback_parent],
        help="Generate a structured note package for a session.",
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
        interaction = ConsoleSummarizeInteraction(dependencies.input_fn)
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


class ConsoleSummarizeInteraction:
    """Console adapter for summarize workflow interaction topics."""

    def __init__(self, input_fn: Callable[[str], str] | None) -> None:
        self._input_fn = input_fn

    def choose(self, request: ChoiceRequest) -> ChoiceOption:
        if request.topic != "summarize.session":
            raise ValueError(f"Unsupported summarize choice topic '{request.topic}'.")
        if request.default_option_id is None:
            raise ValueError("Summarize session choice requires a recommended default.")

        reader = _resolve_console_input(self._input_fn)
        default = request.option(request.default_option_id)
        if reader is None:
            print(
                "Multiple ready sessions found for summarization."
                f" Using newest session by default: {_format_summarize_choice(default)}"
            )
            print("Pass the session path argument to summarize a different session.")
            return default

        return _prompt_choice(
            header="Ready session to summarize:",
            label="ready session",
            choices=request.options,
            formatter=lambda option: _format_summarize_choice(
                option,
                recommended=option.option_id == request.default_option_id,
            ),
            input_fn=reader,
            default_index=_choice_default_index(request),
        )

    def confirm(self, request: ConfirmationRequest) -> bool:
        if request.topic != "runtime_fallback.summarization_remote":
            raise ValueError(f"Unsupported summarize confirmation topic '{request.topic}'.")
        reader = _resolve_console_input(self._input_fn)
        reason = str(request.context.get("reason", "accepted provider unavailable"))
        print(f"Accepted summarization provider is unavailable: {reason}")
        disclosure = request.context.get("disclosure", ())
        if isinstance(disclosure, tuple):
            print("Mistral summarization disclosure:")
            for line in disclosure:
                print(f"  - {line}")
        if reader is None:
            print(
                "Remote Mistral fallback is not pre-authorized and this run is "
                "non-interactive; declining. Re-run setup with "
                "--authorize-mistral-summary-fallback to pre-authorize it."
            )
            return False
        answer = reader("Use Mistral summarization fallback for this run? [y/N] ")
        return answer.strip().lower() in {"y", "yes"}

    def request_text(self, request: TextRequest) -> str:
        raise ValueError(f"Unsupported summarize text topic '{request.topic}'.")

    def notify(self, notice: WorkflowNotice) -> None:
        topic = notice.topic
        context = notice.context
        if topic == "summarize.session_selected":
            print(f"Session path: {context['session_dir']}")
        elif topic == "summarize.started":
            print("Creating meeting notes...")
        elif topic == "summarize.completed":
            print(f"Notes created: {context['summary_path']}")
            print(f"Session path: {context['session_dir']}")
            print(
                "Next command: "
                f"mena export --summary-artifact-id {quote(str(context['summary_artifact_id']))} "
                f"{quote(str(context['session_dir']))}"
            )
        elif topic == RUNTIME_STEWARDSHIP_RELEASE_NOTICE_TOPIC:
            _print_runtime_stewardship_release_notice(cast(RuntimeReleaseResult, context["result"]))
        else:
            raise ValueError(f"Unsupported summarize notice topic '{topic}'.")


def _format_summarize_choice(option: ChoiceOption, *, recommended: bool = False) -> str:
    label = option.label
    if option.detail:
        label = f"{label} | {option.detail}"
    if recommended:
        label = f"{label} [recommended]"
    return label


def _run_summarize_command(
    *,
    session_path: Path,
    manual_notes: str | None,
    language: str | None,
    settings: AppSettings,
    setup_runner: CommandRunner | None = None,
    setup_command_identity: CommandIdentity | None = None,
    setup_hardware: HardwareFacts | None = None,
    allow_runtime_fallback: bool = False,
    summarization_provider_factory: ProviderFactory | None = None,
    interaction: InteractionPort | None = None,
) -> int:
    return summarize_workflow.run_summarization_command(
        session_path=session_path,
        manual_notes=manual_notes,
        language=language,
        settings=settings,
        setup_runner=setup_runner,
        setup_command_identity=setup_command_identity,
        setup_hardware=setup_hardware,
        allow_runtime_fallback=allow_runtime_fallback,
        summarization_provider_factory=summarization_provider_factory,
        interaction=interaction,
    )
