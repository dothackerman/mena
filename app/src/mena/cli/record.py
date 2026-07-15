"""``mena record`` parser and console handler."""

from __future__ import annotations

import argparse

from mena.cli import _shared
from mena.cli.command import bind_handler
from mena.cli.composition import CommandContext
from mena.profiles import capture_profile_choices
from mena.recording_control import RecordCommandInput, run_recording_control_workflow
from mena.session_lock import SessionLockContention


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    **_: object,
) -> None:
    parser = subparsers.add_parser("record", help="Create a recording session.")
    parser.add_argument(
        "--profile",
        choices=capture_profile_choices(),
        help="Capture profile for this recording session.",
    )
    parser.add_argument("--name", help="Friendly session name for human reference.")
    parser.add_argument(
        "--language", default=None, help="Meeting language to persist as the session language."
    )
    parser.add_argument("--mic", default=None, help="Microphone route id to capture.")
    parser.add_argument(
        "--playback-stream",
        default=None,
        help="Playback stream route id to capture for online meetings.",
    )
    parser.add_argument("--output-sink", default=None, help="Meeting audio output route id.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Create a session and print read-only audio discovery without starting capture.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print route planning details and write worker capture diagnostics to stderr.",
    )
    parser.add_argument(
        "--worker-exit-timeout",
        type=float,
        default=_shared.WORKER_EXIT_TIMEOUT_SECONDS,
        help="Seconds to wait for capture worker teardown after stop_capture.",
    )
    bind_handler(parser, run)


def run(args: argparse.Namespace, context: CommandContext) -> int:
    dependencies = context.dependencies
    try:
        profile = _shared._resolve_record_profile(args.profile, input_fn=dependencies.input_fn)
        language = _shared._resolve_record_language(
            args.language,
            default_language=context.settings.summary.language,
            input_fn=dependencies.input_fn,
            dry_run=args.dry_run,
        )
    except ValueError as exc:
        print(f"Cannot start recording: {exc}")
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Recording setup cancelled.")
        return 1
    try:
        result = run_recording_control_workflow(
            RecordCommandInput(
                profile=profile,
                language=language,
                name=args.name,
                microphone_route_id=args.mic,
                playback_stream_route_id=args.playback_stream,
                output_sink_route_id=args.output_sink,
                dry_run=args.dry_run,
                verbose=args.verbose,
                worker_exit_timeout=args.worker_exit_timeout,
                stop_input_available=_shared._record_input(dependencies.input_fn) is not None,
            ),
            data_root=_shared._effective_record_data_root(dependencies.data_root, context.settings),
            discoverer=dependencies.discoverer,
            worker_factory=dependencies.worker_factory,
            interaction=_shared.ConsoleRecordInteraction(dependencies.input_fn),
            now=dependencies.now,
        )
    except SessionLockContention as exc:
        print(f"Cannot start recording: {exc}")
        return 4
    return result.exit_code
