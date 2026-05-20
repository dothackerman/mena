"""CLI adapter for the mena PoC."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from mena.artifacts import ArtifactRecord, ArtifactStoreError, FileSystemArtifactStore
from mena.correction_workflow import CorrectionWorkflowError, run_correction_workflow
from mena.diarization import (
    DiarizationContext,
    DiarizationError,
    DiarizationProvider,
    PyannoteCommunityDiarizationProvider,
    run_diarization_stage,
)
from mena.discovery import AudioDiscovery, DiscoveryStatus, discover_audio_routes, format_discovery
from mena.merge_timing import MergeTimingError, run_merge_timing_stage
from mena.processing import (
    PROCESSING_STATE_ARTIFACT_TYPE,
    ProcessingStageLedger,
    ProcessingStageState,
    ProcessingStateError,
)
from mena.profiles import (
    CaptureProfile,
    DiarisationPolicy,
    capture_profile_choices,
    get_capture_profile,
    parse_capture_profile,
)
from mena.protocol import ProtocolError, WorkerExitedError, WorkerProcess
from mena.review_flagging import (
    ReviewFlaggingError,
    run_confidence_flagging_stage,
)
from mena.semantic_coherence import (
    OllamaSemanticCoherenceProvider,
    SemanticCoherenceError,
    SemanticCoherenceProvider,
    run_semantic_coherence_stage,
)
from mena.sessions import (
    SESSION_RECORD_FILENAME,
    create_session,
    read_session_record,
    update_session_capture_metadata,
    update_session_discovery,
)
from mena.transcription import (
    FasterWhisperTranscriptionProvider,
    TranscriptionError,
    TranscriptionProvider,
    run_transcription_stage,
)

COMMANDS = (
    "record",
    "list",
    "show",
    "process",
    "correct",
    "export",
    "resume",
)
WORKER_EXIT_TIMEOUT_SECONDS = 30.0


ProviderFactory = Callable[[], Any]


@dataclass(frozen=True)
class SourceAudioInput:
    source_id: str
    index: int
    path: Path
    artifact: ArtifactRecord


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
        if command in {"correct", "process", "resume"}:
            subparser.add_argument(
                "session_path",
                help="Path to the session directory or session.json.",
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
    now: datetime | None = None,
    run_id: str | None = None,
    transcription_provider_factory: ProviderFactory = FasterWhisperTranscriptionProvider,
    diarization_provider_factory: ProviderFactory = PyannoteCommunityDiarizationProvider,
    semantic_coherence_provider_factory: ProviderFactory = OllamaSemanticCoherenceProvider,
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
                now=now,
                input_fn=input,
            )
        except (ArtifactStoreError, CorrectionWorkflowError) as exc:
            print(f"Cannot run correction workflow: {exc}")
            return 1
        except (EOFError, KeyboardInterrupt):
            print("Correction workflow cancelled.")
            return 1
        return 0

    if args.command in {"process", "resume"}:
        try:
            if args.command == "process":
                invocation_run_id = run_id or str(uuid4())
                invocation_attempt = 1
            else:
                session_dir, session_record = _resolve_session(Path(args.session_path))
                session_id = _extract_session_id(session_record)
                store = FileSystemArtifactStore(session_id=session_id, session_dir=session_dir)
                resumable = _latest_resumable_processing_state(store, session_id=session_id)
                if resumable is None:
                    blocked = _latest_non_retryable_processing_failure(store, session_id=session_id)
                    if blocked is not None:
                        reason = (
                            blocked.error.message
                            if blocked.error is not None
                            else "Unknown processing failure."
                        )
                        code = (
                            blocked.error.code
                            if blocked.error is not None and blocked.error.code is not None
                            else None
                        )
                        context = f" (code={code})" if code else ""
                        print(
                            "Latest processing failure is non-retryable at run "
                            f"'{blocked.run_id}', stage '{blocked.stage}', "
                            f"attempt {blocked.attempt}: "
                            f"{reason}{context}"
                        )
                    else:
                        print("No retryable processing failure found to resume.")
                    return 1
                invocation_run_id = resumable.run_id
                invocation_attempt = resumable.attempt + 1

            return _run_processing_command(
                session_path=Path(args.session_path),
                run_id=invocation_run_id,
                attempt=invocation_attempt,
                now=now,
                transcription_provider_factory=transcription_provider_factory,
                diarization_provider_factory=diarization_provider_factory,
                semantic_coherence_provider_factory=semantic_coherence_provider_factory,
            )
        except (
            ArtifactStoreError,
            ProcessingStateError,
            ValueError,
            TranscriptionError,
            DiarizationError,
            MergeTimingError,
            ReviewFlaggingError,
            SemanticCoherenceError,
            OSError,
        ) as exc:
            print(f"Cannot run processing workflow: {exc}")
            return 1
        except (EOFError, KeyboardInterrupt):
            print("Processing workflow interrupted.")
            return 1

    print(f"Command '{args.command}' is not implemented yet.")
    return 0


def _run_processing_command(
    *,
    session_path: Path,
    run_id: str,
    attempt: int,
    now: datetime | None,
    transcription_provider_factory: ProviderFactory,
    diarization_provider_factory: ProviderFactory,
    semantic_coherence_provider_factory: ProviderFactory,
) -> int:
    session_dir, session_record = _resolve_session(session_path)
    session_id = _extract_session_id(session_record)
    profile = get_capture_profile(_required_str(session_record, "profile"))
    store = FileSystemArtifactStore(session_id=session_id, session_dir=session_dir)
    source_inputs = _prepare_source_audio_inputs(
        session_dir=session_dir,
        profile=profile,
        session_record=session_record,
        store=store,
    )

    pipeline_timestamp = now if now is not None else datetime.now(UTC)
    transcription_provider = _require_provider(
        transcription_provider_factory,
        TranscriptionProvider,
        "transcription",
    )
    diarization_provider = _require_provider(
        diarization_provider_factory,
        DiarizationProvider,
        "diarization",
    )
    semantic_coherence_provider = _require_provider(
        semantic_coherence_provider_factory,
        SemanticCoherenceProvider,
        "semantic_coherence",
    )

    _run_processing_stages(
        session_id=session_id,
        profile=profile,
        source_inputs=source_inputs,
        store=store,
        ledger=ProcessingStageLedger(store=store, session_id=session_id, run_id=run_id),
        attempt=attempt,
        now=pipeline_timestamp,
        transcription_provider=transcription_provider,
        diarization_provider=diarization_provider,
        semantic_coherence_provider=semantic_coherence_provider,
    )

    print(f"Processing completed for run '{run_id}' (attempt {attempt}).")
    return 0


def _run_processing_stages(
    *,
    session_id: str,
    profile: CaptureProfile,
    source_inputs: dict[str, SourceAudioInput],
    store: FileSystemArtifactStore,
    ledger: ProcessingStageLedger,
    attempt: int,
    now: datetime,
    transcription_provider: TranscriptionProvider,
    diarization_provider: DiarizationProvider,
    semantic_coherence_provider: SemanticCoherenceProvider,
) -> None:
    if len(profile.sources) != 2:
        raise ValueError("Processing currently supports profiles with exactly two sources.")

    transcript_records: dict[str, ArtifactRecord] = {}
    for source in profile.sources:
        source_id = source.logical_id.value
        source_input = source_inputs[source_id]
        transcription_result = run_transcription_stage(
            store=store,
            ledger=ledger,
            provider=transcription_provider,
            source_artifact=source_input.artifact,
            source_id=source_id,
            audio_path=source_input.path,
            attempt=attempt,
            now=now,
        )
        transcript_records[source_id] = transcription_result.transcript_record

    diarization_records: dict[str, tuple[ArtifactRecord | None, ArtifactRecord | None]] = {}
    for source in profile.sources:
        source_id = source.logical_id.value
        source_input = source_inputs[source_id]
        context = DiarizationContext(
            capture_profile=profile.name.value,
            source_id=source_id,
            enabled=source.diarisation_policy != DiarisationPolicy.NONE,
        )
        result = run_diarization_stage(
            store=store,
            ledger=ledger,
            provider=diarization_provider,
            source_artifact=source_input.artifact,
            context=context,
            audio_path=source_input.path,
            attempt=attempt,
            now=now,
        )
        if result.skipped:
            continue
        regular: ArtifactRecord | None = None
        exclusive: ArtifactRecord | None = None
        for artifact, artifact_record in zip(
            result.artifacts, result.artifact_records, strict=True
        ):
            if artifact.kind == "regular_timeline":
                regular = artifact_record
            elif artifact.kind == "exclusive_projection":
                exclusive = artifact_record
        if regular is not None or exclusive is not None:
            diarization_records[source_id] = (regular, exclusive)

    local_source_id = profile.sources[0].logical_id.value
    remote_source_id = profile.sources[1].logical_id.value
    remote_regular: ArtifactRecord | None = None
    remote_exclusive: ArtifactRecord | None = None
    remote_records = diarization_records.get(remote_source_id)
    if remote_records is not None:
        remote_regular, remote_exclusive = remote_records

    merge_result = run_merge_timing_stage(
        store=store,
        ledger=ledger,
        local_transcript_record=transcript_records[local_source_id],
        remote_transcript_record=transcript_records[remote_source_id],
        remote_regular_diarization_record=remote_regular,
        remote_exclusive_diarization_record=remote_exclusive,
        attempt=attempt,
        now=now,
    )

    run_confidence_flagging_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merge_result.merged_transcript_record,
        attempt=attempt,
        now=now,
    )

    run_semantic_coherence_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merge_result.merged_transcript_record,
        provider=semantic_coherence_provider,
        attempt=attempt,
        now=now,
    )


def _prepare_source_audio_inputs(
    *,
    session_dir: Path,
    profile: CaptureProfile,
    session_record: dict[str, object],
    store: FileSystemArtifactStore,
) -> dict[str, SourceAudioInput]:
    required_sources = tuple(source.logical_id.value for source in profile.sources)
    capture_segments = _collect_capture_segments(session_record, required_sources)

    inputs: dict[str, SourceAudioInput] = {}
    for source_id in required_sources:
        segment = capture_segments[source_id]
        raw_path = segment[0]
        index = segment[1]
        expected_sha = segment[2]

        segment_path = Path(raw_path)
        if not segment_path.is_absolute():
            segment_path = session_dir / segment_path

        if not segment_path.exists():
            raise ValueError(f"Capture segment file does not exist: {segment_path}")
        if not segment_path.is_file():
            raise ValueError(f"Capture segment path is not a file: {segment_path}")

        payload = segment_path.read_bytes()
        actual_sha = hashlib.sha256(payload).hexdigest()
        if expected_sha is not None and expected_sha != actual_sha:
            raise ValueError(
                f"Capture segment checksum mismatch for source '{source_id}' at {segment_path}."
            )

        artifact = _ensure_source_audio_artifact(
            store=store,
            source_id=source_id,
            index=index,
            path=segment_path,
            artifact_sha=actual_sha,
            payload=payload,
        )
        inputs[source_id] = SourceAudioInput(
            source_id=source_id,
            index=index,
            path=segment_path,
            artifact=artifact,
        )

    return inputs


def _collect_capture_segments(
    session_record: dict[str, object],
    required_sources: tuple[str, ...],
) -> dict[str, tuple[str, int, str | None]]:
    capture_metadata = session_record.get("capture_metadata")
    if not isinstance(capture_metadata, dict):
        raise ValueError("Session does not include capture metadata.")

    raw_segments = capture_metadata.get("segments")
    if not isinstance(raw_segments, list):
        raise ValueError("Session capture metadata does not include segments.")

    grouped: dict[str, list[tuple[str, int, str | None]]] = {}
    for source in required_sources:
        grouped[source] = []
    for raw_segment in raw_segments:
        if not isinstance(raw_segment, dict):
            raise ValueError("Capture segment metadata must be an object.")
        source_id = _required_str(raw_segment, "source_id")
        if source_id not in grouped:
            raise ValueError(
                f"Session contains an unexpected capture source '{source_id}' for processing."
            )
        index = _required_int(raw_segment, "index")
        path = _required_str(raw_segment, "path")
        expected_sha = raw_segment.get("sha256")
        if expected_sha is not None and not isinstance(expected_sha, str):
            raise ValueError("Capture segment sha256 must be a string when provided.")
        grouped[source_id].append((path, index, expected_sha))

    selected: dict[str, tuple[str, int, str | None]] = {}
    for source_id in required_sources:
        segments = grouped[source_id]
        if not segments:
            raise ValueError(f"Session is missing a capture segment for source '{source_id}'.")
        if len(segments) != 1:
            raise ValueError(
                f"Session has {len(segments)} capture segments for source '{source_id}'. "
                "Processing currently expects exactly one segment per source."
            )
        path, index, expected_sha = segments[0]
        selected[source_id] = (path, index, expected_sha)

    return selected


def _ensure_source_audio_artifact(
    *,
    store: FileSystemArtifactStore,
    source_id: str,
    index: int,
    path: Path,
    artifact_sha: str,
    payload: bytes,
) -> ArtifactRecord:
    normalized_path = str(path.resolve(strict=True))
    metadata_path_key = "source_path"
    metadata_index_key = "segment_index"
    metadata_source_key = "source_id"
    metadata_sha_key = "source_sha256"

    for existing in store.list(artifact_type="source_audio"):
        metadata = existing.metadata
        if (
            metadata.get(metadata_source_key) == source_id
            and metadata.get(metadata_index_key) == index
        ):
            if metadata.get(metadata_sha_key) != artifact_sha:
                raise ValueError(
                    f"Source artifact collision for {source_id} segment {index}: "
                    "checksum has changed since prior processing attempt."
                )
            verification = store.verify(existing.id)
            if not verification.ok or verification.expected_sha256 != artifact_sha:
                artifact_path = store.session_dir / existing.path
                artifact_path.write_bytes(payload)
                verification = store.verify(existing.id)
                if not verification.ok or verification.expected_sha256 != artifact_sha:
                    raise ValueError(
                        f"Source artifact verification failed for {source_id} segment {index}."
                    )
            return existing

    return store.store(
        artifact_type="source_audio",
        name=f"source_audio/{source_id}_{index:06d}.wav",
        data=payload,
        status="sealed",
        metadata={
            metadata_source_key: source_id,
            metadata_index_key: index,
            metadata_path_key: normalized_path,
            metadata_sha_key: artifact_sha,
        },
    )


def _latest_processing_state(
    store: FileSystemArtifactStore,
    session_id: str,
    *,
    allowed_statuses: frozenset[str],
    require_retryable_error: bool | None = None,
) -> ProcessingStageState | None:
    matches: list[tuple[int, ProcessingStageState]] = []
    for state_record in store.list(artifact_type=PROCESSING_STATE_ARTIFACT_TYPE):
        payload = json.loads(store.retrieve(state_record.id))
        state = ProcessingStageState.from_dict(payload)
        if state.session_id != session_id or state.status not in allowed_statuses:
            continue

        if state.status == "failed":
            if state.error is None:
                continue
            if require_retryable_error is True and not state.error.retryable:
                continue
            if require_retryable_error is False and state.error.retryable:
                continue

        matches.append((_processing_state_order(state_record), state))

    if not matches:
        return None
    return max(matches, key=lambda item: item[0])[1]


def _processing_state_order(record: ArtifactRecord) -> int:
    prefix = Path(record.path).stem.split("-", 1)[0]
    try:
        return int(prefix)
    except ValueError as exc:
        raise ProcessingStateError(
            "Processing state artifact name must start with sequence."
        ) from exc


def _latest_retryable_processing_state(
    store: FileSystemArtifactStore,
    session_id: str,
) -> ProcessingStageState | None:
    return _latest_processing_state(
        store=store,
        session_id=session_id,
        allowed_statuses=frozenset({"failed"}),
        require_retryable_error=True,
    )


def _latest_resumable_processing_state(
    store: FileSystemArtifactStore,
    session_id: str,
) -> ProcessingStageState | None:
    latest = _latest_processing_state(
        store=store,
        session_id=session_id,
        allowed_statuses=frozenset({"running", "completed", "failed", "skipped"}),
    )
    if latest is None or latest.status == "running":
        return latest
    if latest.error is not None and latest.error.retryable:
        return latest
    return None


def _latest_non_retryable_processing_failure(
    store: FileSystemArtifactStore,
    session_id: str,
) -> ProcessingStageState | None:
    latest = _latest_processing_state(
        store=store,
        session_id=session_id,
        allowed_statuses=frozenset({"running", "completed", "failed", "skipped"}),
    )
    if latest is None or latest.status != "failed":
        return None
    if latest.error is not None and not latest.error.retryable:
        return latest
    return None


def _resolve_session(session_path: Path) -> tuple[Path, dict[str, object]]:
    if session_path.is_dir():
        session_record_path = session_path / SESSION_RECORD_FILENAME
        resolved_session_dir = session_path
    elif session_path.is_file() and session_path.name == SESSION_RECORD_FILENAME:
        session_record_path = session_path
        resolved_session_dir = session_path.parent
    else:
        raise ValueError(
            "Session path must be a session directory or an existing session.json path."
        )

    try:
        record = read_session_record(session_record_path)
    except Exception as exc:
        raise ValueError(f"Cannot read session record at {session_record_path}.") from exc

    return resolved_session_dir, record


def _extract_session_id(session_record: dict[str, object]) -> str:
    return _required_str(session_record, "id")


def _required_str(payload: dict[str, object] | object, key: str) -> str:
    value = payload.get(key) if isinstance(payload, dict) else None
    if not isinstance(value, str) or not value:
        raise ValueError(f"Session field '{key}' is required and must be a non-empty string.")
    return value


def _required_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or value <= 0:
        raise ValueError(f"Session field '{key}' must be a positive integer.")
    return value


def _provider_implements(provider: Any, *required_attrs: str) -> bool:
    return all(hasattr(provider, attr) for attr in required_attrs)


def _require_provider(
    provider_factory: ProviderFactory,
    provider_type: type[Any],
    provider_name: str,
) -> Any:
    provider = provider_factory()
    required: tuple[str, ...] = ()
    if provider_type is TranscriptionProvider:
        required = ("name", "transcribe")
    elif provider_type is DiarizationProvider:
        required = ("name", "config_id", "diarize")
    elif provider_type is SemanticCoherenceProvider:
        required = ("metadata", "check")

    if required:
        if not _provider_implements(provider, *required):
            raise TypeError(f"{provider_name} provider must implement {provider_type.__name__}.")
        if provider_type is SemanticCoherenceProvider:
            metadata = provider.metadata
            config_id = getattr(metadata, "config_id", None)
            if not isinstance(config_id, str) or not config_id:
                raise TypeError(
                    f"{provider_name} provider must expose semantic metadata config_id."
                )
        return provider

    try:
        valid = isinstance(provider, provider_type)
    except TypeError:
        valid = False
    if not valid:
        raise TypeError(f"{provider_name} provider must implement {provider_type.__name__}.")
    return provider
