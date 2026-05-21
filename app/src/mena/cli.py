"""CLI adapter for the mena PoC."""

from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mena.artifacts import (
    ArtifactRecord,
    ArtifactStore,
    ArtifactStoreError,
    FileSystemArtifactStore,
)
from mena.correction_overlay import (
    CorrectionOverlayContractError,
    resolve_active_correction_overlay,
)
from mena.correction_workflow import CorrectionWorkflowError, run_correction_workflow
from mena.discovery import AudioDiscovery, DiscoveryStatus, discover_audio_routes, format_discovery
from mena.manual_notes import ManualNotesIngestionError, ingest_manual_notes_file
from mena.merge_timing import (
    MERGED_TRANSCRIPT_ARTIFACT_TYPE,
    MergedTranscriptArtifact,
    MergeTimingContractError,
)
from mena.processing import ProcessingStageLedger
from mena.profiles import capture_profile_choices, parse_capture_profile
from mena.protocol import ProtocolError, WorkerExitedError, WorkerProcess
from mena.sessions import (
    SESSION_RECORD_FILENAME,
    create_session,
    read_session_record,
    update_session_capture_metadata,
    update_session_discovery,
)
from mena.summarization import (
    DEFAULT_LANGUAGE,
    OllamaSummarizationProvider,
    SummarizationError,
    SummarizationPolicy,
    run_summarization_stage,
)

COMMANDS = (
    "record",
    "list",
    "show",
    "process",
    "correct",
    "summarize",
    "export",
    "resume",
)
WORKER_EXIT_TIMEOUT_SECONDS = 30.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mena",
        description="Privacy-first local meeting notes PoC.",
    )
    parser.add_argument("--version", action="store_true", help="Show the package version.")

    subparsers = parser.add_subparsers(dest="command")
    record_parser = subparsers.add_parser("record", help="Create a recording session.")
    record_parser.add_argument(
        "--profile",
        choices=capture_profile_choices(),
        required=True,
        help="Capture profile for this recording session.",
    )
    record_parser.add_argument("--name", help="Friendly session name for human reference.")
    record_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Create a session and print read-only audio discovery without starting capture.",
    )
    record_parser.add_argument(
        "--worker-exit-timeout",
        type=float,
        default=WORKER_EXIT_TIMEOUT_SECONDS,
        help="Seconds to wait for capture worker teardown after stop_capture.",
    )
    record_parser.set_defaults(command="record")

    for command in COMMANDS[1:]:
        subparser = subparsers.add_parser(command)
        if command == "correct":
            subparser.add_argument(
                "session_path",
                help="Path to the session directory or session.json.",
            )
        elif command == "summarize":
            subparser.add_argument(
                "session_path",
                help="Path to the session directory or session.json.",
            )
            subparser.add_argument(
                "--manual-notes",
                dest="manual_notes",
                help="Optional path to manual notes file.",
            )
            subparser.add_argument(
                "--language",
                default=DEFAULT_LANGUAGE,
                help="Language code for the summary model prompt.",
            )
        subparser.set_defaults(command=command)

    return parser


DiscoveryFn = Callable[..., AudioDiscovery]
WorkerFactory = Callable[..., Any]


def main(
    argv: Sequence[str] | None = None,
    *,
    data_root: Path | None = None,
    discoverer: DiscoveryFn = discover_audio_routes,
    worker_factory: WorkerFactory = WorkerProcess,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        from mena import __version__

        print(__version__)
        return 0

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "record":
        profile = parse_capture_profile(args.profile)
        created_session = create_session(profile=profile, name=args.name, data_root=data_root)
        print(f"Created session {created_session.record.id}")
        print(f"Session record: {created_session.record_path}")
        discovery = discoverer(profile.id)
        update_session_discovery(created_session.record_path, discovery.to_dict())
        print(format_discovery(discovery))
        if args.dry_run:
            return 0
        if discovery.status is not DiscoveryStatus.AVAILABLE:
            print("Capture not started: audio discovery is unavailable.")
            return 1
        with worker_factory(
            session_id=created_session.record.id,
            session_dir=created_session.bundle_path,
        ) as worker:
            try:
                worker.handshake()
                worker.start_capture({"profile": profile.id.value})
                finalized = worker.stop_capture()
                exit_code = worker.wait(timeout=args.worker_exit_timeout)
                if exit_code != 0:
                    raise WorkerExitedError(f"Worker exited with status {exit_code}.")
            except (ProtocolError, WorkerExitedError, subprocess.TimeoutExpired) as exc:
                print(f"Capture failed: {exc}")
                return 1
        update_session_capture_metadata(created_session.record_path, finalized.payload)
        print("Capture finalized")
        return 0

    if args.command == "correct":
        try:
            run_correction_workflow(
                session_path=Path(args.session_path),
                now=None,
                input_fn=input,
            )
        except (ArtifactStoreError, CorrectionWorkflowError) as exc:
            print(f"Cannot run correction workflow: {exc}")
            return 1
        except (EOFError, KeyboardInterrupt):
            print("Correction workflow cancelled.")
            return 1
        return 0

    if args.command == "summarize":
        try:
            return _run_summarize_command(
                session_path=Path(args.session_path),
                manual_notes=args.manual_notes,
                language=args.language,
            )
        except (
            ArtifactStoreError,
            CorrectionOverlayContractError,
            ManualNotesIngestionError,
            SummarizationError,
            ValueError,
        ) as exc:
            print(f"Cannot summarize session: {exc}")
            return 1
        except (EOFError, KeyboardInterrupt):
            print("Summarization cancelled.")
            return 1

    print(f"Command '{args.command}' is not implemented yet.")
    return 0


def _run_summarize_command(
    *,
    session_path: Path,
    manual_notes: str | None,
    language: str,
) -> int:
    session_dir, session_record = _resolve_session(session_path)
    session_id = _extract_session_id(session_record)
    store = FileSystemArtifactStore(session_id=session_id, session_dir=session_dir)

    merged_transcript_record = _resolve_latest_merged_transcript_record(store=store)
    if merged_transcript_record is None:
        raise SummarizationError("No merged transcript artifact found for this session.")

    resolved_overlay = resolve_active_correction_overlay(
        store=store,
        source_transcript_record=merged_transcript_record,
    )
    if resolved_overlay is None:
        raise SummarizationError(
            "No completed correction overlay found."
            " Run correction and complete it before summarization."
        )
    manual_notes_record = ingest_manual_notes_file(
        store=store,
        notes_path=manual_notes,
    )

    result = run_summarization_stage(
        store=store,
        ledger=ProcessingStageLedger(
            store=store,
            session_id=session_id,
            run_id=f"summarize-{session_id}",
        ),
        merged_transcript_record=merged_transcript_record,
        correction_overlay_record=resolved_overlay[0] if resolved_overlay is not None else None,
        manual_notes_record=manual_notes_record,
        provider=OllamaSummarizationProvider(),
        policy=SummarizationPolicy(language=language),
    )

    print(f"Summary written to: {result.summary_record.path}")
    return 0


def _resolve_session(session_path: Path) -> tuple[Path, dict[str, object]]:
    if session_path.is_dir():
        session_record_path = session_path / SESSION_RECORD_FILENAME
        session_dir = session_path
    elif session_path.is_file() and session_path.name == SESSION_RECORD_FILENAME:
        session_record_path = session_path
        session_dir = session_path.parent
    else:
        raise SummarizationError(
            "Session path must be a session directory or an existing session.json path."
        )

    try:
        record = read_session_record(session_record_path)
    except Exception as exc:  # noqa: BLE001
        raise SummarizationError(f"Cannot read session record at {session_record_path}.") from exc
    return session_dir, record


def _extract_session_id(session_record: dict[str, object]) -> str:
    session_id = session_record.get("id")
    if not isinstance(session_id, str) or not session_id:
        raise SummarizationError("Session record must include a non-empty id.")
    return session_id


def _resolve_latest_merged_transcript_record(
    *,
    store: ArtifactStore,
) -> ArtifactRecord | None:
    best: tuple[tuple[datetime, str, str], ArtifactRecord] | None = None
    for record in store.list(artifact_type=MERGED_TRANSCRIPT_ARTIFACT_TYPE, status="sealed"):
        key = _merged_transcript_timestamp_key(store=store, record=record)
        if key is None:
            continue
        item = (key, record)
        if best is None or item > best:
            best = item
    return None if best is None else best[1]


def _merged_transcript_timestamp_key(
    *,
    store: ArtifactStore,
    record: ArtifactRecord,
) -> tuple[datetime, str, str] | None:
    if not store.verify(record.id).ok:
        return None
    try:
        merged = MergedTranscriptArtifact.from_dict(
            json.loads(store.retrieve(record.id).decode("utf-8"))
        )
    except (json.JSONDecodeError, UnicodeDecodeError, MergeTimingContractError):
        return None
    created_at = _parse_iso_timestamp(merged.created_at)
    if created_at is None:
        return None
    return (created_at, record.path, record.id)


def _parse_iso_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)
