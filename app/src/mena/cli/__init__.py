"""CLI composition root and supported console entry point."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from mena.cli import correct, export, inspect, list, process, record, resume, setup, show, summarize
from mena.cli.composition import (
    CliDependencies,
    CommandContext,
    DiscoveryFn,
    ProcessingCommandRunner,
    ProviderFactory,
    WorkerFactory,
)
from mena.correction_workflow import PlaybackPlayer
from mena.discovery import discover_audio_routes
from mena.processing_orchestration import run_processing_command
from mena.protocol import WorkerProcess
from mena.settings import AppSettings, SettingsError
from mena.setup import CommandIdentity, CommandRunner, HardwareFacts

COMMAND_REGISTRARS = (
    record.register,
    setup.register,
    list.register,
    show.register,
    process.register,
    correct.register,
    summarize.register,
    export.register,
    inspect.register,
    resume.register,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mena", description="Privacy-first local meeting notes PoC."
    )
    parser.add_argument("--version", action="store_true", help="Show the package version.")
    subparsers = parser.add_subparsers(dest="command")

    session_parent = argparse.ArgumentParser(add_help=False)
    session_parent.add_argument(
        "session_path", help="Path to the session directory or session.json."
    )
    optional_session_parent = argparse.ArgumentParser(add_help=False)
    optional_session_parent.add_argument(
        "session_path",
        nargs="?",
        default=None,
        help="Path to the session directory or session.json.",
    )
    process_session_parent = argparse.ArgumentParser(add_help=False)
    process_session_parent.add_argument(
        "session_path",
        nargs="?",
        help=(
            "Path to the session directory or session.json. "
            "Defaults to the latest recorded session."
        ),
    )
    runtime_fallback_parent = argparse.ArgumentParser(add_help=False)
    runtime_fallback_parent.add_argument(
        "--allow-runtime-fallback",
        action="store_true",
        help="Allow explicit one-run fallback if the accepted runtime profile is unavailable.",
    )
    for register_command in COMMAND_REGISTRARS:
        register_command(
            subparsers,
            session_parent=session_parent,
            optional_session_parent=optional_session_parent,
            process_session_parent=process_session_parent,
            runtime_fallback_parent=runtime_fallback_parent,
        )
    return parser


def run(argv: Sequence[str] | None, dependencies: CliDependencies) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        from mena import __version__

        print(__version__)
        return 0
    if args.command is None:
        parser.print_help()
        return 0
    try:
        settings = dependencies.settings or AppSettings.from_env(
            config_path=dependencies.config_path
        )
        sessions_root = (
            dependencies.data_root / "sessions"
            if dependencies.data_root is not None
            else settings.paths.sessions_dir
        )
    except SettingsError as exc:
        print(f"Cannot load configuration: {exc}")
        return 1
    handler = args.command_handler
    return int(handler(args, CommandContext(dependencies, settings, sessions_root)))


def main(
    argv: Sequence[str] | None = None,
    *,
    data_root: Path | None = None,
    discoverer: DiscoveryFn = discover_audio_routes,
    worker_factory: WorkerFactory = WorkerProcess,
    now: datetime | None = None,
    run_id: str | None = None,
    transcription_provider_factory: ProviderFactory | None = None,
    diarization_provider_factory: ProviderFactory | None = None,
    semantic_coherence_provider_factory: ProviderFactory | None = None,
    summarization_provider_factory: ProviderFactory | None = None,
    settings: AppSettings | None = None,
    config_path: Path | None = None,
    input_fn: Callable[[str], str] | None = None,
    playback_player: PlaybackPlayer | None = None,
    setup_runner: CommandRunner | None = None,
    setup_command_identity: CommandIdentity | None = None,
    setup_hardware: HardwareFacts | None = None,
    processing_command_runner: ProcessingCommandRunner = run_processing_command,
) -> int:
    """Compatible console entry point backed by one frozen composition root."""

    return run(
        argv,
        CliDependencies(
            data_root=data_root,
            discoverer=discoverer,
            worker_factory=worker_factory,
            now=now,
            run_id=run_id,
            transcription_provider_factory=transcription_provider_factory,
            diarization_provider_factory=diarization_provider_factory,
            semantic_coherence_provider_factory=semantic_coherence_provider_factory,
            summarization_provider_factory=summarization_provider_factory,
            settings=settings,
            config_path=config_path,
            input_fn=input_fn,
            playback_player=playback_player,
            setup_runner=setup_runner,
            setup_command_identity=setup_command_identity,
            setup_hardware=setup_hardware,
            processing_command_runner=processing_command_runner,
        ),
    )


__all__ = ["main"]
