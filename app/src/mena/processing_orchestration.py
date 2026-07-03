"""Processing orchestration extracted from the CLI layer."""

from __future__ import annotations

import hashlib
import shutil
import wave
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from mena.artifacts import ArtifactRecord, FileSystemArtifactStore
from mena.diarization import (
    DiarizationContext,
    DiarizationProvider,
    DiarizationProviderUnavailable,
    run_diarization_stage,
)
from mena.fields import optional_str, required_int, required_str
from mena.merge_timing import (
    run_merge_timing_stage,
    run_room_mic_merge_timing_stage,
)
from mena.processing import (
    ProcessingStageLedger,
)
from mena.profiles import CaptureProfile, DiarisationPolicy
from mena.provider_validation import ProviderValidationError as ProviderValidationError
from mena.provider_validation import require_provider
from mena.runtime_profile import (
    RuntimeProfileSelection,
    resolve_processing_runtime,
    run_processing_with_runtime_fallback,
    select_runtime_profile,
    select_runtime_profile_after_stage_failure,
)
from mena.semantic_coherence import (
    SemanticCoherenceProvider,
    SemanticCoherenceStageResult,
    run_semantic_coherence_stage,
)
from mena.sessions import (
    resolve_session,
    session_id,
    session_language_or_default,
    session_profile,
)
from mena.settings import AppSettings
from mena.transcription import (
    TranscriptionProvider,
    TranscriptionProviderUnavailable,
    run_transcription_stage,
)

ProviderFactory = Callable[[], Any]
ProviderRunner = Callable[..., Any]
ProcessingStagesRunner = Callable[..., Any]
RuntimeFallbackAuthorizer = Callable[[str, Any], bool]
RuntimeProfileSelector = Callable[..., RuntimeProfileSelection]
RuntimeProfileSelectorAfterFailure = Callable[..., RuntimeProfileSelection]
RuntimeProfileStore = Callable[..., Any]


@dataclass(frozen=True)
class SourceAudioInput:
    source_id: str
    index: int
    path: Path
    artifact: ArtifactRecord


@dataclass(frozen=True)
class CaptureSegmentInput:
    source_id: str
    index: int
    path: Path
    artifact: ArtifactRecord
    sha256: str
    route_id: str | None
    route_label: str | None
    capture_mode: str | None


@dataclass(frozen=True)
class ProcessingPipelineResult:
    semantic_coherence_result: SemanticCoherenceStageResult


def run_processing_command(
    *,
    session_path: Path,
    run_id: str,
    attempt: int,
    now: datetime | None,
    transcription_provider_factory: ProviderFactory,
    diarization_provider_factory: ProviderFactory,
    semantic_coherence_provider_factory: ProviderFactory,
    settings: AppSettings | None = None,
    setup_runner: Any | None = None,
    setup_command_identity: Any | None = None,
    setup_hardware: Any | None = None,
    allow_runtime_fallback: bool = False,
    runtime_fallback_input_fn: Callable[[str], str] | None = None,
    terminal_llm_release: Callable[..., object] | None = None,
    processing_stages_runner: ProcessingStagesRunner | None = None,
    transcription_stage_runner: ProviderRunner = run_transcription_stage,
    diarization_stage_runner: ProviderRunner = run_diarization_stage,
    merge_timing_stage_runner: ProviderRunner = run_merge_timing_stage,
    room_mic_merge_timing_stage_runner: ProviderRunner = run_room_mic_merge_timing_stage,
    confidence_flagging_stage_runner: ProviderRunner | None = None,
    semantic_coherence_stage_runner: ProviderRunner = run_semantic_coherence_stage,
    runtime_selection_fn: RuntimeProfileSelector | None = None,
    runtime_profile_selector_after_failure_fn: RuntimeProfileSelectorAfterFailure | None = None,
    runtime_profile_provenance_store_fn: RuntimeProfileStore | None = None,
) -> int:
    runtime_selection: RuntimeProfileSelection | None = None
    effective_settings: AppSettings | None = None
    runtime_fallback_authorizer: RuntimeFallbackAuthorizer | None = None
    base_transcription_provider_factory = transcription_provider_factory
    base_diarization_provider_factory = diarization_provider_factory
    base_semantic_coherence_provider_factory = semantic_coherence_provider_factory
    resolved_session = resolve_session(session_path)
    session_dir = resolved_session.session_dir
    session_record = resolved_session.record
    session_id_value = session_id(session_record)
    profile = _resolve_capture_profile(session_record)
    session_language = session_language_or_default(session_record, "")
    _validate_processing_profile_support(profile)
    store = FileSystemArtifactStore(session_id=session_id_value, session_dir=session_dir)
    source_inputs = prepare_source_audio_inputs(
        session_dir=session_dir,
        profile=profile,
        session_record=session_record,
        store=store,
    )
    if runtime_selection_fn is None:
        runtime_selection_fn = select_runtime_profile
    if runtime_profile_selector_after_failure_fn is None:
        runtime_profile_selector_after_failure_fn = select_runtime_profile_after_stage_failure
    if runtime_profile_provenance_store_fn is None:
        runtime_profile_provenance_store_fn = _store_runtime_profile_provenance
    if processing_stages_runner is None:
        processing_stages_runner = run_processing_stages

    if _requires_runtime_profile(
        transcription_provider_factory,
        diarization_provider_factory,
        semantic_coherence_provider_factory,
    ):
        effective_settings = settings or AppSettings.from_env()
        runtime_fallback_authorizer = (
            None
            if runtime_fallback_input_fn is None
            else lambda failure, fallback: _confirm_one_run_runtime_fallback(
                accepted_failure=failure,
                fallback_evaluation=fallback,
                input_fn=runtime_fallback_input_fn,
            )
        )
        runtime_bound = resolve_processing_runtime(
            settings=effective_settings,
            store=store,
            run_id=run_id,
            attempt=attempt,
            transcription_provider_factory=base_transcription_provider_factory,
            diarization_provider_factory=base_diarization_provider_factory,
            semantic_coherence_provider_factory=base_semantic_coherence_provider_factory,
            runner=setup_runner,
            command_identity=setup_command_identity,
            hardware=setup_hardware,
            allow_runtime_fallback=allow_runtime_fallback,
            authorize_runtime_fallback=runtime_fallback_authorizer,
            runtime_selection_fn=runtime_selection_fn,
            runtime_profile_store_fn=runtime_profile_provenance_store_fn,
        )
        runtime_selection = runtime_bound.selection
        transcription_provider = runtime_bound.transcription_provider
        diarization_provider = runtime_bound.diarization_provider
        semantic_coherence_provider = runtime_bound.semantic_coherence_provider
    else:
        runtime_bound = None
        transcription_provider = cast(
            TranscriptionProvider,
            _require_provider(
                transcription_provider_factory,
                TranscriptionProvider,
                "transcription",
            ),
        )
        diarization_provider = cast(
            DiarizationProvider,
            _require_provider(
                diarization_provider_factory,
                DiarizationProvider,
                "diarization",
            ),
        )
        semantic_coherence_provider = cast(
            SemanticCoherenceProvider,
            _require_provider(
                semantic_coherence_provider_factory,
                SemanticCoherenceProvider,
                "semantic_coherence",
            ),
        )

    pipeline_timestamp = now if now is not None else datetime.now(UTC)
    ledger = ProcessingStageLedger(store=store, session_id=session_id_value, run_id=run_id)

    def _run_stages(
        *,
        transcription_provider: TranscriptionProvider,
        diarization_provider: DiarizationProvider,
        semantic_coherence_provider: SemanticCoherenceProvider,
    ) -> ProcessingPipelineResult:
        return processing_stages_runner(
            session_id=session_id_value,
            profile=profile,
            source_inputs=source_inputs,
            store=store,
            ledger=ledger,
            attempt=attempt,
            now=pipeline_timestamp,
            transcription_provider=transcription_provider,
            diarization_provider=diarization_provider,
            semantic_coherence_provider=semantic_coherence_provider,
            session_language=session_language or None,
            transcription_stage_runner=transcription_stage_runner,
            diarization_stage_runner=diarization_stage_runner,
            merge_timing_stage_runner=merge_timing_stage_runner,
            room_mic_merge_timing_stage_runner=room_mic_merge_timing_stage_runner,
            confidence_flagging_stage_runner=confidence_flagging_stage_runner,
            semantic_coherence_stage_runner=semantic_coherence_stage_runner,
        )

    try:
        if runtime_bound is None:
            pipeline_result = _run_stages(
                transcription_provider=transcription_provider,
                diarization_provider=diarization_provider,
                semantic_coherence_provider=semantic_coherence_provider,
            )
        elif effective_settings is not None:
            runtime_result = run_processing_with_runtime_fallback(
                runtime=runtime_bound,
                run=lambda runtime: _run_stages(
                    transcription_provider=runtime.transcription_provider,
                    diarization_provider=runtime.diarization_provider,
                    semantic_coherence_provider=runtime.semantic_coherence_provider,
                ),
                settings=effective_settings,
                store=store,
                run_id=run_id,
                attempt=attempt,
                runner=setup_runner,
                command_identity=setup_command_identity,
                hardware=setup_hardware,
                allow_runtime_fallback=allow_runtime_fallback,
                authorize_runtime_fallback=runtime_fallback_authorizer,
                transcription_provider_factory=base_transcription_provider_factory,
                diarization_provider_factory=base_diarization_provider_factory,
                semantic_coherence_provider_factory=base_semantic_coherence_provider_factory,
                failed_input_artifacts=lambda stage: latest_failed_stage_input_artifacts(
                    ledger=ledger,
                    stage=stage,
                ),
                runtime_profile_selector_after_failure_fn=(
                    runtime_profile_selector_after_failure_fn
                ),
                runtime_profile_store_fn=runtime_profile_provenance_store_fn,
            )
            pipeline_result = runtime_result.value
            runtime_bound = runtime_result.runtime
            runtime_selection = runtime_bound.selection
            transcription_provider = runtime_bound.transcription_provider
            diarization_provider = runtime_bound.diarization_provider
            semantic_coherence_provider = runtime_bound.semantic_coherence_provider
        else:
            raise RuntimeError("Runtime settings were not initialized for runtime processing.")
    finally:
        if runtime_selection is not None and terminal_llm_release is not None:
            terminal_llm_release(
                store=store,
                run_id=run_id,
                attempt=attempt,
                provider=semantic_coherence_provider,
                stage="semantic-review",
                boundary="process",
            )

    fallback = (
        " with explicit runtime fallback"
        if runtime_selection is not None and runtime_selection.fallback_used
        else ""
    )
    print(f"Processing completed for run '{run_id}' (attempt {attempt}){fallback}.")
    if pipeline_result is None:
        return 0
    semantic_result = getattr(pipeline_result, "semantic_coherence_result", None)
    if getattr(semantic_result, "skip_reason", None) == "provider_unavailable":
        print(
            "Semantic review skipped: local semantic LLM provider is unavailable. "
            "Processing completed without semantic incoherence flags."
        )
    return 0


def run_processing_stages(
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
    session_language: str | None = None,
    transcription_stage_runner: ProviderRunner = run_transcription_stage,
    diarization_stage_runner: ProviderRunner = run_diarization_stage,
    merge_timing_stage_runner: ProviderRunner = run_merge_timing_stage,
    room_mic_merge_timing_stage_runner: ProviderRunner = run_room_mic_merge_timing_stage,
    confidence_flagging_stage_runner: ProviderRunner | None = None,
    semantic_coherence_stage_runner: ProviderRunner = run_semantic_coherence_stage,
) -> ProcessingPipelineResult:
    transcript_records: dict[str, ArtifactRecord] = {}
    for source in profile.sources:
        source_id = source.logical_id.value
        source_input = source_inputs[source_id]
        transcription_result = cast(
            Any,
            transcription_stage_runner(
                store=store,
                ledger=ledger,
                provider=transcription_provider,
                source_artifact=source_input.artifact,
                source_id=source_id,
                audio_path=source_input.path,
                language=_asr_language_from_session_language(session_language),
                attempt=attempt,
                now=now,
            ),
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
        result = cast(
            Any,
            diarization_stage_runner(
                store=store,
                ledger=ledger,
                provider=diarization_provider,
                source_artifact=source_input.artifact,
                context=context,
                audio_path=source_input.path,
                attempt=attempt,
                now=now,
            ),
        )
        if result.skipped and not result.artifact_records:
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

    source_ids = tuple(source.logical_id.value for source in profile.sources)
    if source_ids == ("room_mic",):
        room_regular: ArtifactRecord | None = None
        room_exclusive: ArtifactRecord | None = None
        room_records = diarization_records.get("room_mic")
        if room_records is not None:
            room_regular, room_exclusive = room_records
        merge_result = cast(
            Any,
            room_mic_merge_timing_stage_runner(
                store=store,
                ledger=ledger,
                room_transcript_record=transcript_records["room_mic"],
                room_regular_diarization_record=room_regular,
                room_exclusive_diarization_record=room_exclusive,
                attempt=attempt,
                now=now,
            ),
        )
    else:
        local_source_id = source_ids[0]
        remote_source_id = source_ids[1]
        remote_regular: ArtifactRecord | None = None
        remote_exclusive: ArtifactRecord | None = None
        remote_records = diarization_records.get(remote_source_id)
        if remote_records is not None:
            remote_regular, remote_exclusive = remote_records

        merge_result = cast(
            Any,
            merge_timing_stage_runner(
                store=store,
                ledger=ledger,
                local_transcript_record=transcript_records[local_source_id],
                remote_transcript_record=transcript_records[remote_source_id],
                remote_regular_diarization_record=remote_regular,
                remote_exclusive_diarization_record=remote_exclusive,
                attempt=attempt,
                now=now,
            ),
        )

    if confidence_flagging_stage_runner is None:
        from mena.review_flagging import run_confidence_flagging_stage

        confidence_flagging_stage_runner = run_confidence_flagging_stage
    confidence_flagging_stage_runner(
        store=store,
        ledger=ledger,
        merged_transcript_record=merge_result.merged_transcript_record,
        attempt=attempt,
        now=now,
    )

    semantic_result = cast(
        Any,
        semantic_coherence_stage_runner(
            store=store,
            ledger=ledger,
            merged_transcript_record=merge_result.merged_transcript_record,
            provider=semantic_coherence_provider,
            attempt=attempt,
            now=now,
            skip_provider_unavailable=True,
        ),
    )
    return ProcessingPipelineResult(semantic_coherence_result=semantic_result)


def runtime_fallback_stage_for_provider_failure(
    exc: TranscriptionProviderUnavailable | DiarizationProviderUnavailable,
) -> str:
    from mena.runtime_profile import runtime_fallback_stage_for_provider_failure as _resolver_stage

    return _resolver_stage(exc)


def latest_failed_stage_input_artifacts(
    *,
    ledger: ProcessingStageLedger,
    stage: str,
) -> tuple[str, ...]:
    state = ledger.latest(stage)
    if state is None or state.status != "failed":
        return ()
    return tuple(artifact.id for artifact in state.input_artifacts)


def collect_capture_segments(
    session_record: dict[str, object],
    required_sources: tuple[str, ...],
) -> dict[str, tuple[dict[str, object], ...]]:
    capture_metadata = session_record.get("capture_metadata")
    if not isinstance(capture_metadata, dict):
        raise ValueError("Session does not include capture metadata.")

    raw_segments = capture_metadata.get("segments")
    if not isinstance(raw_segments, list):
        raise ValueError("Session capture metadata does not include segments.")

    grouped: dict[str, list[dict[str, object]]] = {}
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
        _ = _required_str(raw_segment, "path")
        status = _required_str(raw_segment, "status")
        if status != "sealed":
            raise ValueError(
                f"Capture segment for source '{source_id}' index {index} is {status}, not sealed."
            )
        expected_sha = raw_segment.get("sha256")
        if expected_sha is not None and not isinstance(expected_sha, str):
            raise ValueError("Capture segment sha256 must be a string when provided.")
        route_id = raw_segment.get("route_id")
        if route_id is not None and not isinstance(route_id, str):
            raise ValueError("Capture segment route_id must be a string when provided.")
        route_label = raw_segment.get("route_label")
        if route_label is not None and not isinstance(route_label, str):
            raise ValueError("Capture segment route_label must be a string when provided.")
        capture_mode = raw_segment.get("capture_mode")
        if capture_mode is not None and not isinstance(capture_mode, str):
            raise ValueError("Capture segment capture_mode must be a string when provided.")
        grouped[source_id].append(
            {
                "path": _required_str(raw_segment, "path"),
                "index": index,
                "status": status,
                "sha256": expected_sha,
                "route_id": route_id,
                "route_label": route_label,
                "capture_mode": capture_mode,
            }
        )

    selected: dict[str, tuple[dict[str, object], ...]] = {}
    for source_id in required_sources:
        segments = grouped[source_id]
        if not segments:
            raise ValueError(f"Session is missing a capture segment for source '{source_id}'.")
        ordered = tuple(sorted(segments, key=_segment_metadata_index))
        indexes = [_segment_metadata_index(segment) for segment in ordered]
        if len(set(indexes)) != len(indexes):
            raise ValueError(f"Session has duplicate capture segment indexes for '{source_id}'.")
        expected_indexes = list(range(1, len(indexes) + 1))
        if indexes != expected_indexes:
            raise ValueError(
                f"Session capture segments for source '{source_id}' must be contiguous "
                "starting at 1."
            )
        selected[source_id] = ordered

    validate_capture_segment_alignment(selected, required_sources)
    return selected


def prepare_source_audio_inputs(
    *,
    session_dir: Path,
    profile: CaptureProfile,
    session_record: dict[str, object],
    store: FileSystemArtifactStore,
) -> dict[str, SourceAudioInput]:
    required_sources = tuple(source.logical_id.value for source in profile.sources)
    capture_segments = collect_capture_segments(session_record, required_sources)

    inputs: dict[str, SourceAudioInput] = {}
    for source_id in required_sources:
        segment_inputs: list[CaptureSegmentInput] = []
        for segment_metadata in capture_segments[source_id]:
            raw_path = _required_str(segment_metadata, "path")
            index = _required_int(segment_metadata, "index")
            expected_sha = segment_metadata.get("sha256")
            if expected_sha is not None and not isinstance(expected_sha, str):
                raise ValueError("Capture segment sha256 must be a string when provided.")
            route_id = _optional_metadata_str(segment_metadata, "route_id")
            route_label = _optional_metadata_str(segment_metadata, "route_label")
            capture_mode = _optional_metadata_str(segment_metadata, "capture_mode")
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
                route_id=route_id,
                route_label=route_label,
                capture_mode=capture_mode,
            )
            segment_inputs.append(
                CaptureSegmentInput(
                    source_id=source_id,
                    index=index,
                    path=segment_path,
                    artifact=artifact,
                    sha256=actual_sha,
                    route_id=route_id,
                    route_label=route_label,
                    capture_mode=capture_mode,
                )
            )

        if len(segment_inputs) == 1:
            only = segment_inputs[0]
            inputs[source_id] = SourceAudioInput(
                source_id=source_id,
                index=only.index,
                path=only.path,
                artifact=only.artifact,
            )
            continue

        prep_temp_dir = store.artifact_root / ".provider-prep-tmp"
        prep_temp_dir.mkdir(parents=True, exist_ok=True)
        prep_path = prep_temp_dir / f".{source_id}.{uuid4()}.wav"
        try:
            prep_metadata = _stitch_provider_prep_audio(
                source_id=source_id,
                segments=tuple(segment_inputs),
                output_path=prep_path,
            )
            prep_sha = _required_str(prep_metadata, "sha256")
            prep_artifact = _ensure_provider_prep_audio_artifact(
                store=store,
                source_id=source_id,
                source_path=prep_path,
                artifact_sha=prep_sha,
                metadata=prep_metadata,
            )
        finally:
            prep_path.unlink(missing_ok=True)
            with suppress(OSError):
                prep_temp_dir.rmdir()
        inputs[source_id] = SourceAudioInput(
            source_id=source_id,
            index=1,
            path=store.session_dir / prep_artifact.path,
            artifact=prep_artifact,
        )

    return inputs


def validate_capture_segment_alignment(
    selected: dict[str, tuple[dict[str, object], ...]],
    required_sources: tuple[str, ...],
) -> None:
    if len(required_sources) <= 1:
        return
    reference_source = required_sources[0]
    reference_indexes = tuple(
        _segment_metadata_index(segment) for segment in selected[reference_source]
    )
    for source_id in required_sources[1:]:
        indexes = tuple(_segment_metadata_index(segment) for segment in selected[source_id])
        if indexes != reference_indexes:
            raise ValueError(
                "Online capture segment sets must be paired across sources: "
                f"{reference_source} indexes {list(reference_indexes)}; "
                f"{source_id} indexes {list(indexes)}."
            )


def _segment_metadata_index(segment: dict[str, object]) -> int:
    value = segment.get("index")
    if type(value) is not int:
        raise ValueError("Capture segment index must be an integer.")
    return value


def _ensure_source_audio_artifact(
    *,
    store: FileSystemArtifactStore,
    source_id: str,
    index: int,
    path: Path,
    artifact_sha: str,
    payload: bytes,
    route_id: str | None,
    route_label: str | None,
    capture_mode: str | None,
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
                artifact_path.parent.mkdir(parents=True, exist_ok=True)
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
            "route_id": route_id,
            "route_label": route_label,
            "capture_mode": capture_mode,
        },
    )


def _stitch_provider_prep_audio(
    *,
    source_id: str,
    segments: tuple[CaptureSegmentInput, ...],
    output_path: Path,
) -> dict[str, object]:
    if len(segments) < 2:
        raise ValueError("Provider-prep audio requires at least two source segments.")

    params: tuple[int, int, int] | None = None
    frame_count = 0
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as out:
        for segment in segments:
            with wave.open(str(segment.path), "rb") as wav_file:
                if wav_file.getcomptype() != "NONE":
                    raise ValueError(
                        f"Capture segment for source '{source_id}' must be uncompressed PCM WAV."
                    )
                segment_params = (
                    wav_file.getframerate(),
                    wav_file.getnchannels(),
                    wav_file.getsampwidth(),
                )
                if params is None:
                    params = segment_params
                    sample_rate, channels, sample_width = params
                    out.setnchannels(channels)
                    out.setsampwidth(sample_width)
                    out.setframerate(sample_rate)
                elif segment_params != params:
                    raise ValueError(
                        f"Capture segments for source '{source_id}' have incompatible WAV format."
                    )
                frame_count += wav_file.getnframes()
                while frames := wav_file.readframes(32_768):
                    out.writeframesraw(frames)

    if params is None:
        raise ValueError(f"Session is missing provider-prep segments for source '{source_id}'.")
    sample_rate, channels, sample_width = params
    prep_sha = _sha256_file(output_path)
    metadata: dict[str, object] = {
        "source_id": source_id,
        "derived_from_segments": [
            {
                "source_id": segment.source_id,
                "index": segment.index,
                "path": str(segment.path),
                "artifact_id": segment.artifact.id,
                "sha256": segment.sha256,
                "route_id": segment.route_id,
                "route_label": segment.route_label,
                "capture_mode": segment.capture_mode,
            }
            for segment in segments
        ],
        "transformations": ["lossless_wav_stitch"],
        "provider_requirement": "single_audio_path",
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_format": f"pcm_s{sample_width * 8}le",
        "duration_seconds": frame_count / sample_rate,
        "sha256": prep_sha,
    }
    return metadata


def _ensure_provider_prep_audio_artifact(
    *,
    store: FileSystemArtifactStore,
    source_id: str,
    source_path: Path,
    artifact_sha: str,
    metadata: dict[str, object],
) -> ArtifactRecord:
    for existing in store.list(artifact_type="provider_prep_audio"):
        if existing.metadata.get("source_id") != source_id:
            continue
        if existing.sha256 != artifact_sha:
            raise ValueError(
                f"Provider-prep artifact collision for source '{source_id}': "
                "prepared audio bytes changed since a prior processing attempt."
            )
        verification = store.verify(existing.id)
        if not verification.ok or verification.expected_sha256 != artifact_sha:
            artifact_path = store.session_dir / existing.path
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            _replace_file_from_path(source_path=source_path, destination_path=artifact_path)
            verification = store.verify(existing.id)
            if not verification.ok or verification.expected_sha256 != artifact_sha:
                raise ValueError(
                    f"Provider-prep artifact verification failed for source '{source_id}'."
                )
        return existing

    stored = _store_provider_prep_audio_artifact(
        store=store,
        source_id=source_id,
        source_path=source_path,
        metadata=metadata,
    )
    if stored.sha256 != artifact_sha:
        raise ValueError(f"Provider-prep artifact checksum drifted for source '{source_id}'.")
    return stored


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _replace_file_from_path(*, source_path: Path, destination_path: Path) -> None:
    temp_path = destination_path.with_name(f".{destination_path.name}.{uuid4()}.tmp")
    try:
        with source_path.open("rb") as source, temp_path.open("xb") as destination:
            shutil.copyfileobj(source, destination, length=1024 * 1024)
        temp_path.replace(destination_path)
    finally:
        temp_path.unlink(missing_ok=True)


def _store_provider_prep_audio_artifact(
    *,
    store: FileSystemArtifactStore,
    source_id: str,
    source_path: Path,
    metadata: dict[str, object],
) -> ArtifactRecord:
    return store.store_file(
        artifact_type="provider_prep_audio",
        name=f"provider-prep/{source_id}.wav",
        source_path=source_path,
        status="sealed",
        metadata=metadata,
    )


def _resolve_capture_profile(session_record: dict[str, object]) -> CaptureProfile:
    from mena.profiles import get_capture_profile

    return get_capture_profile(session_profile(session_record))


def _requires_runtime_profile(
    transcription_provider_factory: ProviderFactory,
    diarization_provider_factory: ProviderFactory,
    semantic_coherence_provider_factory: ProviderFactory,
) -> bool:
    from mena.runtime_profile import production_runtime_required

    return production_runtime_required(
        transcription_provider_factory=transcription_provider_factory,
        diarization_provider_factory=diarization_provider_factory,
        semantic_coherence_provider_factory=semantic_coherence_provider_factory,
    )


def _validate_processing_profile_support(profile: CaptureProfile) -> None:
    source_ids = tuple(source.logical_id.value for source in profile.sources)
    if source_ids not in {("local_mic", "system_audio"), ("room_mic",)}:
        raise ValueError(
            "Processing currently supports online_dual_stream and in_person_single_mic profiles."
        )


def _asr_language_from_session_language(language: str | None) -> str | None:
    if language is None:
        return None
    normalized = language.replace("_", "-").split("-", 1)[0].lower()
    return normalized or None


def _confirm_one_run_runtime_fallback(
    *,
    accepted_failure: str,
    fallback_evaluation: Any,
    input_fn: Callable[[str], str],
) -> bool:
    print(f"Accepted runtime profile is unavailable: {accepted_failure}")
    print(
        "One-run local runtime fallback is ready: "
        f"{fallback_evaluation.profile.id} using "
        f"{fallback_evaluation.selected_asr_profile.checkpoint}/"
        f"{fallback_evaluation.selected_asr_profile.device}."
    )
    answer = input_fn("Use local runtime fallback for this run? [y/N] ")
    return answer.strip().lower() in {"y", "yes"}


def _store_runtime_profile_provenance(
    *,
    store: FileSystemArtifactStore,
    run_id: str,
    attempt: int,
    selection: RuntimeProfileSelection,
    name_suffix: str | None = None,
) -> None:
    from mena.runtime_profile import store_runtime_profile_provenance

    store_runtime_profile_provenance(
        store=store,
        run_id=run_id,
        attempt=attempt,
        selection=selection,
        name_suffix=name_suffix or "",
    )


def _require_provider(
    provider_factory: ProviderFactory,
    provider_type: type[Any],
    provider_name: str,
) -> Any:
    return require_provider(provider_factory, provider_type, provider_name)


def _required_str(payload: dict[str, object], key: str) -> str:
    value = required_str(
        payload,
        key,
        error=ValueError,
        message=f"Session field '{key}' is required and must be a non-empty string.",
    )
    return value


def _required_int(payload: dict[str, object], key: str) -> int:
    value = required_int(
        payload,
        key,
        error=ValueError,
        message=f"Session field '{key}' must be a positive integer.",
    )
    if value <= 0:
        raise ValueError(f"Session field '{key}' must be a positive integer.")
    return value


def _optional_metadata_str(payload: dict[str, object], key: str) -> str | None:
    return optional_str(
        payload,
        key,
        error=ValueError,
        message=f"Session field '{key}' must be a non-empty string or null.",
    )


# Backward-compatibility shims for existing CLI import paths.
_run_processing_command = run_processing_command
_run_processing_stages = run_processing_stages
_collect_capture_segments = collect_capture_segments
