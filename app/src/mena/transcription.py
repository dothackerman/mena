"""Transcription provider port, transcript artifact contract, and local adapters."""

from __future__ import annotations

import importlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, Self, cast

from mena.artifacts import ArtifactRecord, ArtifactStore
from mena.processing import (
    ArtifactRef,
    ProcessingErrorInfo,
    ProcessingStageLedger,
    ProcessingStageState,
)

TRANSCRIPT_ARTIFACT_TYPE = "transcript"
TRANSCRIPT_ARTIFACT_VERSION = 1
TRANSCRIPT_SCHEMA_VERSION = 1
TRANSCRIPTION_STAGE = "transcription"
_STABLE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_:-]*$")


class TranscriptionError(RuntimeError):
    """Base class for transcription-stage failures."""


class TranscriptionContractError(TranscriptionError):
    """Raised when a transcript payload violates the v1 contract."""


class TranscriptionInputError(TranscriptionError):
    """Raised when transcription input cannot be used."""


class TranscriptionProviderUnavailable(TranscriptionError):
    """Raised when a local transcription adapter cannot run on this machine."""


@dataclass(frozen=True)
class TranscriptWord:
    """Word-level timing emitted by an ASR provider when available."""

    text: str
    start_seconds: float
    end_seconds: float
    probability: float | None = None

    @classmethod
    def from_dict(cls, data: object) -> Self:
        item = _mapping(data, "Transcript word")
        word = cls(
            text=_required_str(item, "text", "Transcript word"),
            start_seconds=_required_number(item, "start_seconds", "Transcript word"),
            end_seconds=_required_number(item, "end_seconds", "Transcript word"),
            probability=_optional_number(item, "probability", "Transcript word"),
        )
        _validate_span(word.start_seconds, word.end_seconds, "Transcript word")
        _validate_probability(word.probability, "Transcript word probability")
        return word

    def to_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "start_seconds": self.start_seconds,
            "end_seconds": self.end_seconds,
            "probability": self.probability,
        }


@dataclass(frozen=True)
class TranscriptSegment:
    """Segment-level ASR output preserved as a raw derived transcript."""

    id: str
    text: str
    start_seconds: float
    end_seconds: float
    avg_logprob: float | None = None
    no_speech_probability: float | None = None
    compression_ratio: float | None = None
    words: tuple[TranscriptWord, ...] = ()

    @classmethod
    def from_dict(cls, data: object) -> Self:
        item = _mapping(data, "Transcript segment")
        segment = cls(
            id=_required_str(item, "id", "Transcript segment"),
            text=_required_str(item, "text", "Transcript segment"),
            start_seconds=_required_number(item, "start_seconds", "Transcript segment"),
            end_seconds=_required_number(item, "end_seconds", "Transcript segment"),
            avg_logprob=_optional_number(item, "avg_logprob", "Transcript segment"),
            no_speech_probability=_optional_number(
                item,
                "no_speech_probability",
                "Transcript segment",
            ),
            compression_ratio=_optional_number(item, "compression_ratio", "Transcript segment"),
            words=tuple(TranscriptWord.from_dict(word) for word in _list(item, "words")),
        )
        _validate_stable_id(segment.id, "Transcript segment id")
        _validate_span(segment.start_seconds, segment.end_seconds, "Transcript segment")
        _validate_probability(
            segment.no_speech_probability,
            "Transcript segment no_speech_probability",
        )
        return segment

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "text": self.text,
            "start_seconds": self.start_seconds,
            "end_seconds": self.end_seconds,
            "avg_logprob": self.avg_logprob,
            "no_speech_probability": self.no_speech_probability,
            "compression_ratio": self.compression_ratio,
            "words": [word.to_dict() for word in self.words],
        }


@dataclass(frozen=True)
class TranscriptLanguage:
    """Requested and detected language facts for one transcription pass."""

    requested: str | None = None
    detected: str | None = None
    detection_probability: float | None = None

    @classmethod
    def from_dict(cls, data: object) -> Self:
        item = _mapping(data, "Transcript language")
        language = cls(
            requested=_optional_str(item, "requested", "Transcript language"),
            detected=_optional_str(item, "detected", "Transcript language"),
            detection_probability=_optional_number(
                item,
                "detection_probability",
                "Transcript language",
            ),
        )
        _validate_probability(language.detection_probability, "Language detection probability")
        return language

    def to_dict(self) -> dict[str, object]:
        return {
            "requested": self.requested,
            "detected": self.detected,
            "detection_probability": self.detection_probability,
        }


@dataclass(frozen=True)
class TranscriptProviderMetadata:
    """Replay metadata required to understand and reproduce ASR output."""

    name: str
    checkpoint: str
    quantization: str
    device: str
    timestamp_mode: str
    decode_parameters: Mapping[str, object] = field(default_factory=dict)
    metadata: Mapping[str, object] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: object) -> Self:
        item = _mapping(data, "Transcript provider")
        provider = cls(
            name=_required_str(item, "name", "Transcript provider"),
            checkpoint=_required_str(item, "checkpoint", "Transcript provider"),
            quantization=_required_str(item, "quantization", "Transcript provider"),
            device=_required_str(item, "device", "Transcript provider"),
            timestamp_mode=_required_str(item, "timestamp_mode", "Transcript provider"),
            decode_parameters=_json_object(
                item.get("decode_parameters"),
                "Transcript provider decode_parameters",
            ),
            metadata=_json_object(item.get("metadata"), "Transcript provider metadata"),
        )
        _validate_stable_id(provider.name, "Transcript provider name")
        return provider

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "checkpoint": self.checkpoint,
            "quantization": self.quantization,
            "device": self.device,
            "timestamp_mode": self.timestamp_mode,
            "decode_parameters": dict(self.decode_parameters),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class TranscriptArtifact:
    """Persisted `transcript.v1` raw ASR artifact."""

    version: int
    session_id: str
    source_artifact: ArtifactRef
    source_id: str | None
    provider: TranscriptProviderMetadata
    language: TranscriptLanguage
    text: str
    segments: tuple[TranscriptSegment, ...]
    created_at: str

    @classmethod
    def from_dict(cls, data: object) -> Self:
        item = _mapping(data, "Transcript artifact")
        transcript = cls(
            version=_required_int(item, "version", "Transcript artifact"),
            session_id=_required_str(item, "session_id", "Transcript artifact"),
            source_artifact=ArtifactRef.from_dict(item.get("source_artifact")),
            source_id=_optional_str(item, "source_id", "Transcript artifact"),
            provider=TranscriptProviderMetadata.from_dict(item.get("provider")),
            language=TranscriptLanguage.from_dict(item.get("language")),
            text=_required_str(item, "text", "Transcript artifact"),
            segments=tuple(
                TranscriptSegment.from_dict(segment) for segment in _list(item, "segments")
            ),
            created_at=_required_str(item, "created_at", "Transcript artifact"),
        )
        if transcript.version != TRANSCRIPT_SCHEMA_VERSION:
            raise TranscriptionContractError("Unsupported transcript artifact version.")
        if transcript.source_id is not None:
            _validate_stable_id(transcript.source_id, "Transcript source_id")
        if not transcript.segments:
            raise TranscriptionContractError(
                "Transcript artifact must contain at least one segment."
            )
        return transcript

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "session_id": self.session_id,
            "source_artifact": self.source_artifact.to_dict(),
            "source_id": self.source_id,
            "provider": self.provider.to_dict(),
            "language": self.language.to_dict(),
            "text": self.text,
            "segments": [segment.to_dict() for segment in self.segments],
            "created_at": self.created_at,
        }

    def to_json_bytes(self) -> bytes:
        return (json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n").encode("utf-8")


@dataclass(frozen=True)
class TranscriptionRequest:
    """Provider-facing request for one source-audio artifact."""

    session_id: str
    source_artifact: ArtifactRef
    source_id: str | None = None
    audio_path: Path | None = None
    language: str | None = None
    initial_prompt: str | None = None
    hotwords: tuple[str, ...] = ()
    created_at: str | None = None


class TranscriptionProvider(Protocol):
    """Port implemented by ASR providers."""

    name: str

    def transcribe(self, request: TranscriptionRequest) -> TranscriptArtifact:
        """Transcribe one source-audio artifact into a raw transcript artifact payload."""


@dataclass(frozen=True)
class HardwareSnapshot:
    """Small hardware facts used for deterministic faster-whisper profile selection."""

    cuda_available: bool = False
    vram_gb: float | None = None
    ram_gb: float | None = None
    avx2_available: bool | None = None


@dataclass(frozen=True)
class FasterWhisperModelProfile:
    """Concrete local faster-whisper model/runtime profile."""

    id: str
    checkpoint: str
    device: str
    compute_type: str
    reason: str


def select_faster_whisper_profile(hardware: HardwareSnapshot) -> FasterWhisperModelProfile:
    """Choose a conservative local faster-whisper profile from hardware facts."""

    if hardware.cuda_available and hardware.vram_gb is not None and hardware.vram_gb >= 10:
        return FasterWhisperModelProfile(
            id="cuda_quality",
            checkpoint="large-v3-turbo",
            device="cuda",
            compute_type="float16",
            reason="CUDA with enough VRAM for the PoC quality ceiling.",
        )
    if hardware.cuda_available and hardware.vram_gb is not None and hardware.vram_gb >= 5:
        return FasterWhisperModelProfile(
            id="cuda_balanced",
            checkpoint="medium",
            device="cuda",
            compute_type="int8_float16",
            reason="CUDA is available, but VRAM is below the large-v3-turbo tier.",
        )
    if hardware.ram_gb is not None and hardware.ram_gb <= 8:
        return FasterWhisperModelProfile(
            id="cpu_minimal",
            checkpoint="base",
            device="cpu",
            compute_type="int8",
            reason="CPU fallback for machines with limited RAM.",
        )
    if hardware.avx2_available is False:
        return FasterWhisperModelProfile(
            id="cpu_minimal_no_avx2",
            checkpoint="base",
            device="cpu",
            compute_type="int8",
            reason="CPU fallback for brittle machines without AVX2.",
        )
    return FasterWhisperModelProfile(
        id="cpu_balanced",
        checkpoint="small",
        device="cpu",
        compute_type="int8",
        reason="Portable CPU default when stronger hardware has not been detected.",
    )


@dataclass(frozen=True)
class FasterWhisperDecodeOptions:
    """Decode settings persisted with faster-whisper transcript artifacts."""

    beam_size: int = 5
    word_timestamps: bool = True
    condition_on_previous_text: bool = False
    hallucination_silence_threshold: float | None = 1.0
    vad_filter: bool = False

    def to_parameters(self, request: TranscriptionRequest) -> dict[str, object]:
        parameters: dict[str, object] = {
            "beam_size": self.beam_size,
            "word_timestamps": self.word_timestamps,
            "condition_on_previous_text": self.condition_on_previous_text,
            "vad_filter": self.vad_filter,
        }
        if self.hallucination_silence_threshold is not None:
            parameters["hallucination_silence_threshold"] = self.hallucination_silence_threshold
        if request.language is not None:
            parameters["language"] = request.language
        if request.initial_prompt is not None:
            parameters["initial_prompt"] = request.initial_prompt
        if request.hotwords:
            parameters["hotwords"] = list(request.hotwords)
        return parameters


ModelFactory = Callable[..., Any]
ModuleLoader = Callable[[str], Any]


class FasterWhisperTranscriptionProvider:
    """Local `faster-whisper` ASR adapter behind the transcription port."""

    name = "faster-whisper"

    def __init__(
        self,
        *,
        profile: FasterWhisperModelProfile | None = None,
        decode_options: FasterWhisperDecodeOptions | None = None,
        local_files_only: bool = False,
        model_factory: ModelFactory | None = None,
        module_loader: ModuleLoader = importlib.import_module,
    ) -> None:
        self.profile = profile or select_faster_whisper_profile(HardwareSnapshot())
        self.decode_options = decode_options or FasterWhisperDecodeOptions()
        self.local_files_only = local_files_only
        self._model_factory = model_factory
        self._module_loader = module_loader
        self._model: Any | None = None

    def transcribe(self, request: TranscriptionRequest) -> TranscriptArtifact:
        if request.audio_path is None:
            raise TranscriptionInputError("faster-whisper transcription requires an audio_path.")
        model = self._load_model()
        decode_parameters = self.decode_options.to_parameters(request)
        try:
            segments_iter, info = model.transcribe(str(request.audio_path), **decode_parameters)
            segments = tuple(
                _segment_from_faster_whisper(item, index=index)
                for index, item in enumerate(cast(Iterable[object], segments_iter), start=1)
            )
        except Exception as exc:
            if _is_cuda_resource_error(exc):
                raise TranscriptionProviderUnavailable(
                    f"faster-whisper CUDA resource failure: {exc}"
                ) from exc
            raise
        if not segments:
            raise TranscriptionError("faster-whisper returned no transcript segments.")
        text = " ".join(segment.text for segment in segments).strip()
        provider = TranscriptProviderMetadata(
            name=self.name,
            checkpoint=self.profile.checkpoint,
            quantization=self.profile.compute_type,
            device=self.profile.device,
            timestamp_mode="word" if self.decode_options.word_timestamps else "segment",
            decode_parameters=decode_parameters,
            metadata={"profile_id": self.profile.id, "profile_reason": self.profile.reason},
        )
        language = TranscriptLanguage(
            requested=request.language,
            detected=_optional_attr_str(info, "language"),
            detection_probability=_optional_attr_number(info, "language_probability"),
        )
        return TranscriptArtifact(
            version=TRANSCRIPT_SCHEMA_VERSION,
            session_id=request.session_id,
            source_artifact=request.source_artifact,
            source_id=request.source_id,
            provider=provider,
            language=language,
            text=text,
            segments=segments,
            created_at=request.created_at or _timestamp(None),
        )

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model
        if self._model_factory is None:
            try:
                module = self._module_loader("faster_whisper")
            except ImportError as exc:
                raise TranscriptionProviderUnavailable(
                    "faster-whisper is not installed in this environment."
                ) from exc
            self._model_factory = cast(ModelFactory, module.WhisperModel)
        try:
            self._model = self._model_factory(
                self.profile.checkpoint,
                device=self.profile.device,
                compute_type=self.profile.compute_type,
                local_files_only=self.local_files_only,
            )
        except Exception as exc:
            if _is_cuda_resource_error(exc):
                raise TranscriptionProviderUnavailable(
                    f"faster-whisper CUDA resource failure: {exc}"
                ) from exc
            raise
        return self._model


def _is_cuda_resource_error(exc: Exception) -> bool:
    message = f"{exc.__class__.__name__}: {exc}".lower()
    return "cuda" in message and (
        "out of memory" in message or "alloc" in message or "cublas_status_alloc_failed" in message
    )


class FakeTranscriptionProvider:
    """Deterministic fake ASR provider for fast workflow and contract tests."""

    name = "fake"

    def __init__(
        self,
        *,
        segments: tuple[TranscriptSegment, ...] | None = None,
        detected_language: str = "en",
    ) -> None:
        self.segments = segments or (
            TranscriptSegment(
                id="segment_000001",
                text="hello world",
                start_seconds=0.0,
                end_seconds=1.2,
                avg_logprob=-0.05,
                no_speech_probability=0.01,
                compression_ratio=1.0,
                words=(
                    TranscriptWord(
                        text="hello",
                        start_seconds=0.0,
                        end_seconds=0.5,
                        probability=0.99,
                    ),
                    TranscriptWord(
                        text="world",
                        start_seconds=0.6,
                        end_seconds=1.2,
                        probability=0.98,
                    ),
                ),
            ),
        )
        self.detected_language = detected_language
        self.calls: list[TranscriptionRequest] = []

    def transcribe(self, request: TranscriptionRequest) -> TranscriptArtifact:
        self.calls.append(request)
        text = " ".join(segment.text for segment in self.segments).strip()
        return TranscriptArtifact(
            version=TRANSCRIPT_SCHEMA_VERSION,
            session_id=request.session_id,
            source_artifact=request.source_artifact,
            source_id=request.source_id,
            provider=TranscriptProviderMetadata(
                name=self.name,
                checkpoint="deterministic-fixture",
                quantization="none",
                device="test",
                timestamp_mode="word",
                decode_parameters={
                    "language": request.language,
                    "initial_prompt": request.initial_prompt,
                    "hotwords": list(request.hotwords),
                },
                metadata={"purpose": "contract-test"},
            ),
            language=TranscriptLanguage(
                requested=request.language,
                detected=self.detected_language,
                detection_probability=1.0,
            ),
            text=text,
            segments=self.segments,
            created_at=request.created_at or _timestamp(None),
        )


@dataclass(frozen=True)
class TranscriptionStageResult:
    """Result of running or reusing one transcription stage checkpoint."""

    state: ProcessingStageState
    transcript: TranscriptArtifact
    transcript_record: ArtifactRecord
    skipped: bool = False


def persist_transcript_artifact(
    *,
    store: ArtifactStore,
    transcript: TranscriptArtifact,
    name: str | Path,
) -> ArtifactRecord:
    """Persist a transcript payload through the artifact store contract."""

    TranscriptArtifact.from_dict(transcript.to_dict())
    return store.store(
        artifact_type=TRANSCRIPT_ARTIFACT_TYPE,
        name=name,
        data=transcript.to_json_bytes(),
        version=TRANSCRIPT_ARTIFACT_VERSION,
        metadata={
            "category": "derived",
            "contract": "transcript.v1",
            "provider": transcript.provider.name,
            "checkpoint": transcript.provider.checkpoint,
            "source_artifact_id": transcript.source_artifact.id,
            "source_id": transcript.source_id,
        },
    )


def run_transcription_stage(
    *,
    store: ArtifactStore,
    ledger: ProcessingStageLedger,
    provider: TranscriptionProvider,
    source_artifact: ArtifactRecord,
    source_id: str | None = None,
    audio_path: Path | None = None,
    language: str | None = None,
    initial_prompt: str | None = None,
    hotwords: tuple[str, ...] = (),
    stage: str = TRANSCRIPTION_STAGE,
    attempt: int = 1,
    skip_completed: bool = True,
    now: datetime | None = None,
) -> TranscriptionStageResult:
    """Run one transcription stage and write processing checkpoints around it."""

    input_ref = ArtifactRef.from_record(source_artifact)
    if skip_completed:
        completed = _completed_state_for_inputs(
            store=store,
            ledger=ledger,
            stage=stage,
            input_artifacts=(input_ref,),
        )
        if completed is not None:
            if not completed.output_artifacts:
                raise TranscriptionContractError(
                    "Completed transcription stage has no output refs."
                )
            output_ref = completed.output_artifacts[0]
            record = _record_for_ref(store=store, artifact_ref=output_ref)
            transcript = TranscriptArtifact.from_dict(json.loads(store.retrieve(record.id)))
            if transcript.language.requested != language:
                completed = None
            else:
                return TranscriptionStageResult(
                    state=completed,
                    transcript=transcript,
                    transcript_record=record,
                    skipped=True,
                )

    started = ledger.mark_running(
        stage=stage,
        input_artifacts=(input_ref,),
        attempt=attempt,
        now=now,
    )
    verification = store.verify(source_artifact.id)
    if not verification.ok:
        failed = ledger.mark_failed(
            stage=stage,
            input_artifacts=(input_ref,),
            error=ProcessingErrorInfo(
                message=verification.error or "source artifact failed verification",
                retryable=False,
                code="input_artifact_invalid",
            ),
            attempt=attempt,
            started_at=started.started_at,
            now=now,
        )
        raise TranscriptionInputError(failed.error.message if failed.error else "invalid input")

    try:
        request = TranscriptionRequest(
            session_id=source_artifact.session_id,
            source_artifact=input_ref,
            source_id=source_id,
            audio_path=audio_path,
            language=language,
            initial_prompt=initial_prompt,
            hotwords=hotwords,
            created_at=_timestamp(now),
        )
        transcript = provider.transcribe(request)
        transcript_record = persist_transcript_artifact(
            store=store,
            transcript=transcript,
            name=_transcript_artifact_name(
                run_id=ledger.run_id,
                source_id=source_id,
                source_sha256=source_artifact.sha256,
                language=language,
                attempt=attempt,
            ),
        )
    except TranscriptionProviderUnavailable as exc:
        failed = _mark_transcription_failed(
            ledger=ledger,
            stage=stage,
            input_ref=input_ref,
            attempt=attempt,
            started_at=started.started_at,
            now=now,
            message=str(exc),
            retryable=True,
            code="provider_unavailable",
        )
        raise TranscriptionProviderUnavailable(
            failed.error.message if failed.error else str(exc)
        ) from exc
    except TranscriptionError as exc:
        failed = _mark_transcription_failed(
            ledger=ledger,
            stage=stage,
            input_ref=input_ref,
            attempt=attempt,
            started_at=started.started_at,
            now=now,
            message=str(exc),
            retryable=False,
            code="transcription_failed",
        )
        raise TranscriptionError(failed.error.message if failed.error else str(exc)) from exc

    completed = ledger.mark_completed(
        stage=stage,
        input_artifacts=(input_ref,),
        output_artifacts=(ArtifactRef.from_record(transcript_record),),
        attempt=attempt,
        started_at=started.started_at,
        now=now,
    )
    return TranscriptionStageResult(
        state=completed,
        transcript=transcript,
        transcript_record=transcript_record,
    )


def _completed_state_for_inputs(
    *,
    store: ArtifactStore,
    ledger: ProcessingStageLedger,
    stage: str,
    input_artifacts: tuple[ArtifactRef, ...],
) -> ProcessingStageState | None:
    for state in reversed(ledger.states(stage=stage)):
        if (
            state.status == "completed"
            and state.input_artifacts == input_artifacts
            and state.output_artifacts
            and all(
                _artifact_ref_verifies(store=store, artifact_ref=ref)
                for ref in state.output_artifacts
            )
        ):
            return state
    return None


def _mark_transcription_failed(
    *,
    ledger: ProcessingStageLedger,
    stage: str,
    input_ref: ArtifactRef,
    attempt: int,
    started_at: str | None,
    now: datetime | None,
    message: str,
    retryable: bool,
    code: str,
) -> ProcessingStageState:
    return ledger.mark_failed(
        stage=stage,
        input_artifacts=(input_ref,),
        error=ProcessingErrorInfo(message=message, retryable=retryable, code=code),
        attempt=attempt,
        started_at=started_at,
        now=now,
    )


def _record_for_ref(*, store: ArtifactStore, artifact_ref: ArtifactRef) -> ArtifactRecord:
    for record in store.list(artifact_type=artifact_ref.artifact_type):
        if (
            record.id == artifact_ref.id
            and record.version == artifact_ref.version
            and record.sha256 == artifact_ref.sha256
        ):
            return record
    raise TranscriptionContractError("Transcript output ref does not match a known artifact.")


def _artifact_ref_verifies(*, store: ArtifactStore, artifact_ref: ArtifactRef) -> bool:
    try:
        record = _record_for_ref(store=store, artifact_ref=artifact_ref)
        verification = store.verify(record.id)
    except KeyError:
        return False
    return verification.ok


def _transcript_artifact_name(
    *,
    run_id: str,
    source_id: str | None,
    source_sha256: str,
    language: str | None,
    attempt: int,
) -> Path:
    source_label = source_id or "source"
    _validate_stable_id(source_label, "Transcript source_id")
    language_label = _transcript_language_label(language)
    path = Path(run_id) / source_label / source_sha256[:12]
    if language_label is not None:
        path /= language_label
    return path / f"attempt-{attempt:03d}.json"


def _transcript_language_label(language: str | None) -> str | None:
    if language is None:
        return None
    normalized = re.sub(r"[^a-z0-9_:-]+", "-", language.lower()).strip("-")
    if not normalized:
        return None
    return f"language-{normalized}"


def _segment_from_faster_whisper(segment: object, *, index: int) -> TranscriptSegment:
    words_attr = getattr(segment, "words", None)
    words = (
        () if words_attr is None else tuple(_word_from_faster_whisper(word) for word in words_attr)
    )
    return TranscriptSegment(
        id=f"segment_{index:06d}",
        text=str(getattr(segment, "text", "")).strip(),
        start_seconds=_required_attr_number(segment, "start"),
        end_seconds=_required_attr_number(segment, "end"),
        avg_logprob=_optional_attr_number(segment, "avg_logprob"),
        no_speech_probability=_optional_attr_number(segment, "no_speech_prob"),
        compression_ratio=_optional_attr_number(segment, "compression_ratio"),
        words=words,
    )


def _word_from_faster_whisper(word: object) -> TranscriptWord:
    return TranscriptWord(
        text=str(getattr(word, "word", "")).strip(),
        start_seconds=_required_attr_number(word, "start"),
        end_seconds=_required_attr_number(word, "end"),
        probability=_optional_attr_number(word, "probability"),
    )


def _timestamp(now: datetime | None) -> str:
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    return timestamp.isoformat(timespec="seconds").replace("+00:00", "Z")


def _mapping(data: object, label: str) -> Mapping[str, object]:
    if not isinstance(data, dict):
        raise TranscriptionContractError(f"{label} must be a JSON object.")
    return data


def _list(data: Mapping[str, object], key: str) -> list[object]:
    value = data.get(key)
    if not isinstance(value, list):
        raise TranscriptionContractError(f"Transcript field '{key}' must be an array.")
    return value


def _required_str(data: Mapping[str, object], key: str, label: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise TranscriptionContractError(f"{label} field '{key}' must be a non-empty string.")
    return value


def _optional_str(data: Mapping[str, object], key: str, label: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise TranscriptionContractError(f"{label} field '{key}' must be null or a string.")
    return value


def _required_int(data: Mapping[str, object], key: str, label: str) -> int:
    value = data.get(key)
    if type(value) is not int:
        raise TranscriptionContractError(f"{label} field '{key}' must be an integer.")
    return value


def _required_number(data: Mapping[str, object], key: str, label: str) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TranscriptionContractError(f"{label} field '{key}' must be a number.")
    return float(value)


def _optional_number(data: Mapping[str, object], key: str, label: str) -> float | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TranscriptionContractError(f"{label} field '{key}' must be null or a number.")
    return float(value)


def _json_object(data: object, label: str) -> Mapping[str, object]:
    if data is None:
        return {}
    if not isinstance(data, dict) or any(not isinstance(key, str) for key in data):
        raise TranscriptionContractError(f"{label} must be a JSON object.")
    try:
        json.dumps(data)
    except TypeError as exc:
        raise TranscriptionContractError(f"{label} must be JSON serializable.") from exc
    return data


def _validate_stable_id(value: str, label: str) -> None:
    if not _STABLE_ID_PATTERN.fullmatch(value):
        raise TranscriptionContractError(f"{label} must be a stable identifier.")


def _validate_span(start_seconds: float, end_seconds: float, label: str) -> None:
    if start_seconds < 0 or end_seconds < 0 or end_seconds < start_seconds:
        raise TranscriptionContractError(f"{label} timestamps must be non-negative and ordered.")


def _validate_probability(value: float | None, label: str) -> None:
    if value is not None and not 0 <= value <= 1:
        raise TranscriptionContractError(f"{label} must be between 0 and 1.")


def _required_attr_number(data: object, attr: str) -> float:
    value = getattr(data, attr, None)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TranscriptionContractError(
            f"faster-whisper segment attribute '{attr}' must be numeric."
        )
    return float(value)


def _optional_attr_number(data: object, attr: str) -> float | None:
    value = getattr(data, attr, None)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TranscriptionContractError(f"faster-whisper attribute '{attr}' must be numeric.")
    return float(value)


def _optional_attr_str(data: object, attr: str) -> str | None:
    value = getattr(data, attr, None)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TranscriptionContractError(f"faster-whisper attribute '{attr}' must be a string.")
    return value
