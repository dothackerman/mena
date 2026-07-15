"""Shared process/resume console execution."""

from __future__ import annotations

import argparse
from pathlib import Path
from shlex import quote
from uuid import uuid4

from mena import processing_orchestration
from mena.artifacts import ArtifactStoreError
from mena.cli import _shared
from mena.cli.composition import CommandContext
from mena.diarization import DiarizationError
from mena.merge_timing import MergeTimingError
from mena.processing import ProcessingStateError
from mena.provider_validation import ProviderValidationError
from mena.review_flagging import ReviewFlaggingError
from mena.runtime_profile import RuntimeFallbackUnavailable
from mena.semantic_coherence import SemanticCoherenceError
from mena.session_lock import SessionLockContention
from mena.transcription import TranscriptionError


def run(args: argparse.Namespace, context: CommandContext, *, resume: bool) -> int:
    dependencies = context.dependencies
    try:
        if resume:
            session_path = Path(args.session_path)
            exit_code = processing_orchestration.run_resume_processing_command(
                session_path=session_path,
                now=dependencies.now,
                transcription_provider_factory=dependencies.transcription_provider_factory,
                diarization_provider_factory=dependencies.diarization_provider_factory,
                semantic_coherence_provider_factory=dependencies.semantic_coherence_provider_factory,
                settings=context.settings,
                setup_runner=dependencies.setup_runner,
                setup_command_identity=dependencies.setup_command_identity,
                setup_hardware=dependencies.setup_hardware,
                allow_runtime_fallback=args.allow_runtime_fallback,
                interaction=_shared._resolve_runtime_fallback_interaction(dependencies.input_fn),
            )
        else:
            session_path = _shared._resolve_process_session_path(
                args.session_path, context.settings, input_fn=dependencies.input_fn
            )
            print(f"Processing recording: {session_path}")
            exit_code = dependencies.processing_command_runner(
                session_path=session_path,
                run_id=dependencies.run_id or str(uuid4()),
                attempt=1,
                now=dependencies.now,
                transcription_provider_factory=dependencies.transcription_provider_factory,
                diarization_provider_factory=dependencies.diarization_provider_factory,
                semantic_coherence_provider_factory=dependencies.semantic_coherence_provider_factory,
                settings=context.settings,
                setup_runner=dependencies.setup_runner,
                setup_command_identity=dependencies.setup_command_identity,
                setup_hardware=dependencies.setup_hardware,
                allow_runtime_fallback=args.allow_runtime_fallback,
                interaction=_shared._resolve_runtime_fallback_interaction(dependencies.input_fn),
                orchestrator_command="process",
            )
        if not resume and exit_code == 0:
            print("Processing completed successfully.")
            if _shared._correction_handoff_available(session_path):
                print(f"Next command: mena correct {quote(str(session_path))}")
        return exit_code
    except processing_orchestration.ProcessingResumeUnavailable as exc:
        print(str(exc))
        return 1
    except RuntimeFallbackUnavailable as exc:
        print(f"Cannot run processing workflow: {exc}")
        return 3
    except SessionLockContention as exc:
        print(f"Cannot run processing workflow: {exc}")
        return 4
    except (
        ArtifactStoreError,
        ProcessingStateError,
        ValueError,
        TranscriptionError,
        DiarizationError,
        MergeTimingError,
        ReviewFlaggingError,
        SemanticCoherenceError,
        ProviderValidationError,
        OSError,
    ) as exc:
        print(f"Cannot run processing workflow: {exc}")
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Processing workflow interrupted.")
        return 1
