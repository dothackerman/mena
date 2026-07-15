"""``mena setup`` parser and console handler."""

from __future__ import annotations

import argparse

from mena.cli import _shared
from mena.cli.command import bind_handler
from mena.cli.composition import CommandContext


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
    return _shared._run_setup_command(
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
