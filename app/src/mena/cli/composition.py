"""Composition root for the CLI interface adapter."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from mena import processing_orchestration
from mena.correction_workflow import PlaybackPlayer
from mena.discovery import AudioDiscovery, discover_audio_routes
from mena.protocol import WorkerProcess
from mena.settings import AppSettings
from mena.setup import CommandIdentity, CommandRunner, HardwareFacts

ProviderFactory = Callable[[], Any]
DiscoveryFn = Callable[..., AudioDiscovery]
WorkerFactory = Callable[..., Any]
ProcessingCommandRunner = Callable[..., int]


@dataclass(frozen=True)
class CliDependencies:
    """All replaceable dependencies built at the CLI composition root."""

    data_root: Path | None = None
    discoverer: DiscoveryFn = discover_audio_routes
    worker_factory: WorkerFactory = WorkerProcess
    now: datetime | None = None
    run_id: str | None = None
    transcription_provider_factory: ProviderFactory | None = None
    diarization_provider_factory: ProviderFactory | None = None
    semantic_coherence_provider_factory: ProviderFactory | None = None
    summarization_provider_factory: ProviderFactory | None = None
    settings: AppSettings | None = None
    config_path: Path | None = None
    input_fn: Callable[[str], str] | None = None
    playback_player: PlaybackPlayer | None = None
    setup_runner: CommandRunner | None = None
    setup_command_identity: CommandIdentity | None = None
    setup_hardware: HardwareFacts | None = None
    processing_command_runner: ProcessingCommandRunner = (
        processing_orchestration.run_processing_command
    )


@dataclass(frozen=True)
class CommandContext:
    """Resolved invocation context shared by command handlers."""

    dependencies: CliDependencies
    settings: AppSettings
    sessions_root: Path
