"""``mena setup`` parser and console handler."""

from __future__ import annotations

import argparse
from collections.abc import Callable

from mena.cli.command import bind_handler
from mena.cli.composition import CommandContext
from mena.interaction import (
    ChoiceOption,
    ChoiceRequest,
    ConfirmationRequest,
    InteractionPort,
    TextRequest,
    WorkflowNotice,
)
from mena.settings import AppSettings
from mena.setup import (
    CommandIdentity,
    CommandRunner,
    HardwareFacts,
)
from mena.setup_detection import DefaultSetupDetector
from mena.setup_workflow import (
    SetupMessageEvent,
    SetupReportEvent,
    SetupWorkflowOptions,
    run_setup_workflow,
)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser], **_: object) -> None:
    parser = subparsers.add_parser("setup", help="Run Linux alpha setup/preflight.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Only evaluate local install/provider readiness; do not mutate.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm planned automated setup actions without prompting.",
    )
    parser.add_argument(
        "--authorize-local-fallback",
        action="store_true",
        help="Authorize automatic local runtime fallback when the fallback profile is ready.",
    )
    parser.add_argument(
        "--summarization-provider",
        choices=("local", "mistral"),
        default="local",
        help=(
            "Accepted summarization primary provider. 'mistral' uses the fixed "
            "allow-listed direct Mistral model and requires the disclosure to be "
            "accepted."
        ),
    )
    parser.add_argument(
        "--authorize-mistral-summary-fallback",
        action="store_true",
        help=(
            "Accept Mistral as the first ordered summarization fallback provider "
            "for a local primary; requires the disclosure to be accepted."
        ),
    )
    parser.add_argument(
        "--accept-mistral-summary-disclosure",
        action="store_true",
        help=(
            "Acknowledge the Mistral summarization disclosure non-interactively. "
            "--yes alone never accepts the disclosure."
        ),
    )
    bind_handler(parser, run)


def run(args: argparse.Namespace, context: CommandContext) -> int:
    dependencies = context.dependencies
    return _run_setup_command(
        check_only=args.check,
        assume_yes=args.yes,
        authorize_local_fallback=args.authorize_local_fallback,
        summarization_provider=args.summarization_provider,
        mistral_summary_fallback=args.authorize_mistral_summary_fallback,
        accept_mistral_disclosure=args.accept_mistral_summary_disclosure,
        settings=context.settings,
        input_fn=dependencies.input_fn,
        runner=dependencies.setup_runner,
        command_identity=dependencies.setup_command_identity,
        hardware=dependencies.setup_hardware,
    )


def _run_setup_command(
    *,
    check_only: bool,
    assume_yes: bool,
    authorize_local_fallback: bool,
    settings: AppSettings,
    input_fn: Callable[[str], str] | None,
    runner: CommandRunner | None,
    command_identity: CommandIdentity | None,
    hardware: HardwareFacts | None,
    summarization_provider: str = "local",
    mistral_summary_fallback: bool = False,
    accept_mistral_disclosure: bool = False,
) -> int:
    result = run_setup_workflow(
        settings=settings,
        options=SetupWorkflowOptions(
            check_only=check_only,
            assume_yes=assume_yes,
            authorize_local_fallback=authorize_local_fallback,
            summarization_provider=summarization_provider,
            mistral_summary_fallback=mistral_summary_fallback,
            accept_mistral_disclosure=accept_mistral_disclosure,
        ),
        interaction=_CliSetupInteraction(input_fn=input_fn),
        runner=runner,
        detector=DefaultSetupDetector(
            command_identity=command_identity,
            hardware=hardware,
        ),
    )
    for event in result.events:
        if isinstance(event, SetupReportEvent):
            print(event.render(), end="")
        elif isinstance(event, SetupMessageEvent):
            print(event.text)
    return result.exit_code


class _CliSetupInteraction(InteractionPort):
    def __init__(self, input_fn: Callable[[str], str] | None) -> None:
        self._input_fn = input if input_fn is None else input_fn

    def choose(self, request: ChoiceRequest) -> ChoiceOption:
        raise ValueError(f"Unsupported setup choice request: {request.topic}")

    def confirm(self, request: ConfirmationRequest) -> bool:
        if request.topic == "setup.primary_actions":
            prompt = "Proceed with automated setup actions? [y/N] "
        elif request.topic == "setup.fallback_actions":
            prompt = "Proceed with automated fallback setup actions? [y/N] "
        elif request.topic == "setup.mistral_summary_disclosure":
            # Render the disclosure here, not only via buffered workflow
            # events: setup events are printed after the workflow returns, so
            # the prompt must carry the disclosure itself to satisfy the
            # PDR-004 "disclose before selecting or authorizing" requirement.
            disclosure = request.context.get("disclosure", ())
            print("Mistral summarization disclosure:")
            if isinstance(disclosure, tuple):
                for line in disclosure:
                    print(f"  - {line}")
            role = str(request.context.get("role", "provider"))
            prompt = (
                f"Accept the Mistral summarization disclosure above and enable "
                f"Mistral as {role} provider? [y/N] "
            )
        else:
            raise ValueError(f"Unsupported setup confirmation request: {request.topic}")
        answer = self._input_fn(prompt)
        return answer.strip().lower() in {"y", "yes"}

    def request_text(self, request: TextRequest) -> str:
        raise ValueError(f"Unsupported setup text request: {request.topic}")

    def notify(self, notice: WorkflowNotice) -> None:
        return None
