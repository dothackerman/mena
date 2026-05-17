"""Diarization provider port, artifacts, and scenario-aware stage runner."""

from __future__ import annotations

import importlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol, Self, cast

from mena.artifacts import ArtifactRecord, ArtifactStore
from mena.processing import (
    ArtifactRef,
    ProcessingErrorInfo,
    ProcessingStageLedger,
    ProcessingStageState,
)

DIARIZATION_ARTIFACT_TYPE = "diarization"
DIARIZATION_ARTIFACT_VERSION = 1
DIARIZATION_SCHEMA_VERSION = 1
DIARIZATION_STAGE = "diarization"
DIARIZATION_POLICY_VERSION = 1
DIARIZATION_ARTIFACT_KINDS = frozenset(
    {"regular_timeline", "exclusive_projection", "correction_overlay"}
)
EMBEDDING_STATUSES = frozenset({"available", "unavailable"})
_STABLE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_:-]*$")


class DiarizationError(RuntimeError):
    """Base class for diarization-stage failures."""


class DiarizationContractError(DiarizationError):
    """Raised when a diarization payload violates the v1 contract."""


class DiarizationInputError(DiarizationError):
    """Raised when diarization input cannot be used."""


class DiarizationProviderUnavailable(DiarizationError):
    """Raised when a local diarization adapter cannot run on this machine."""


@dataclass(frozen=True)
class SpeakerEmbeddingRef:
    """Explicit embedding availability for one anonymous diarized speaker."""

    status: str
    artifact: ArtifactRef | None = None
    reason: str | None = None

    @classmethod
    def unavailable(cls, reason: str) -> Self:
        return cls(status="unavailable", reason=reason)

    @classmethod
    def from_dict(cls, data: object) -> Self:
        item = _mapping(data, "Speaker embedding")
        embedding = cls(
            status=_required_str(item, "status", "Speaker embedding"),
            artifact=None
            if item.get("artifact") is None
            else ArtifactRef.from_dict(item.get("artifact")),
            reason=_optional_str(item, "reason", "Speaker embedding"),
        )
        if embedding.status not in EMBEDDING_STATUSES:
            raise DiarizationContractError("Speaker embedding status is invalid.")
        if embedding.status == "available" and embedding.artifact is None:
            raise DiarizationContractError(
                "Available speaker embeddings must reference an artifact."
            )
        if embedding.status == "unavailable" and not embedding.reason:
            raise DiarizationContractError("Unavailable speaker embeddings must record a reason.")
        return embedding

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "artifact": None if self.artifact is None else self.artifact.to_dict(),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class DiarizedSpeaker:
    """Anonymous speaker produced by a diarization provider."""

    id: str
    label: str
    embedding: SpeakerEmbeddingRef
    metadata: Mapping[str, object] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: object) -> Self:
        item = _mapping(data, "Diarized speaker")
        speaker = cls(
            id=_required_str(item, "id", "Diarized speaker"),
            label=_required_str(item, "label", "Diarized speaker"),
            embedding=SpeakerEmbeddingRef.from_dict(item.get("embedding")),
            metadata=_json_object(item.get("metadata"), "Diarized speaker metadata"),
        )
        _validate_stable_id(speaker.id, "Diarized speaker id")
        return speaker

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "embedding": self.embedding.to_dict(),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class DiarizationSegment:
    """Speaker turn in a regular timeline or exclusive projection."""

    id: str
    speaker_id: str
    start_seconds: float
    end_seconds: float
    overlapped: bool = False
    metadata: Mapping[str, object] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: object) -> Self:
        item = _mapping(data, "Diarization segment")
        segment = cls(
            id=_required_str(item, "id", "Diarization segment"),
            speaker_id=_required_str(item, "speaker_id", "Diarization segment"),
            start_seconds=_required_number(item, "start_seconds", "Diarization segment"),
            end_seconds=_required_number(item, "end_seconds", "Diarization segment"),
            overlapped=_required_bool(item, "overlapped", "Diarization segment"),
            metadata=_json_object(item.get("metadata"), "Diarization segment metadata"),
        )
        _validate_stable_id(segment.id, "Diarization segment id")
        _validate_stable_id(segment.speaker_id, "Diarization segment speaker_id")
        _validate_span(segment.start_seconds, segment.end_seconds, "Diarization segment")
        return segment

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "speaker_id": self.speaker_id,
            "start_seconds": self.start_seconds,
            "end_seconds": self.end_seconds,
            "overlapped": self.overlapped,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class DiarizationProviderMetadata:
    """Replay metadata for a local diarization provider run."""

    name: str
    checkpoint: str
    device: str
    parameters: Mapping[str, object] = field(default_factory=dict)
    metadata: Mapping[str, object] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: object) -> Self:
        item = _mapping(data, "Diarization provider")
        provider = cls(
            name=_required_str(item, "name", "Diarization provider"),
            checkpoint=_required_str(item, "checkpoint", "Diarization provider"),
            device=_required_str(item, "device", "Diarization provider"),
            parameters=_json_object(item.get("parameters"), "Diarization provider parameters"),
            metadata=_json_object(item.get("metadata"), "Diarization provider metadata"),
        )
        _validate_stable_id(provider.name, "Diarization provider name")
        return provider

    @property
    def config_id(self) -> str:
        payload = {
            "name": self.name,
            "checkpoint": self.checkpoint,
            "device": self.device,
            "parameters": dict(self.parameters),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "checkpoint": self.checkpoint,
            "device": self.device,
            "parameters": dict(self.parameters),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class DiarizationContext:
    """Scenario policy context for deciding whether one source should be diarized."""

    capture_profile: str
    source_id: str
    enabled: bool = True
    remote_speaker_count: int | None = None

    @property
    def policy_id(self) -> str:
        speaker_count = (
            "unknown" if self.remote_speaker_count is None else str(self.remote_speaker_count)
        )
        return (
            f"diarization_policy_v{DIARIZATION_POLICY_VERSION}:"
            f"{self.capture_profile}:{self.source_id}:"
            f"enabled={str(self.enabled).lower()}:remote_speakers={speaker_count}"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "policy_version": DIARIZATION_POLICY_VERSION,
            "capture_profile": self.capture_profile,
            "source_id": self.source_id,
            "enabled": self.enabled,
            "remote_speaker_count": self.remote_speaker_count,
        }


@dataclass(frozen=True)
class DiarizationDecision:
    """Policy decision for one diarization stage input."""

    should_run: bool
    reason: str
    fallback_label: str | None = None


def decide_diarization(context: DiarizationContext) -> DiarizationDecision:
    """Apply AD-023 scenario-aware diarization policy."""

    if not context.enabled:
        return DiarizationDecision(
            should_run=False,
            reason="diarization_disabled",
            fallback_label=_fallback_label(context),
        )
    if context.capture_profile == "online_dual_stream" and context.source_id == "local_mic":
        return DiarizationDecision(
            should_run=False,
            reason="structural_local_mic_attribution",
            fallback_label="You",
        )
    if context.capture_profile == "online_dual_stream" and context.source_id == "system_audio":
        if context.remote_speaker_count == 1:
            return DiarizationDecision(
                should_run=False,
                reason="single_remote_speaker",
                fallback_label="Remote speaker 1",
            )
        return DiarizationDecision(should_run=True, reason="remote_mix_eligible")
    if context.capture_profile == "in_person_single_mic" and context.source_id == "room_mic":
        return DiarizationDecision(should_run=True, reason="room_mic_eligible")
    return DiarizationDecision(
        should_run=False,
        reason="source_not_applicable",
        fallback_label=_fallback_label(context),
    )


@dataclass(frozen=True)
class DiarizationArtifact:
    """Persisted `diarization.v1` artifact."""

    version: int
    kind: str
    session_id: str
    source_artifact: ArtifactRef
    source_id: str
    context: DiarizationContext
    provider: DiarizationProviderMetadata
    speakers: tuple[DiarizedSpeaker, ...]
    segments: tuple[DiarizationSegment, ...]
    base_artifacts: tuple[ArtifactRef, ...]
    overlay_entries: tuple[Mapping[str, object], ...]
    created_at: str

    @classmethod
    def from_dict(cls, data: object) -> Self:
        item = _mapping(data, "Diarization artifact")
        context_data = _mapping(item.get("context"), "Diarization context")
        artifact = cls(
            version=_required_int(item, "version", "Diarization artifact"),
            kind=_required_str(item, "kind", "Diarization artifact"),
            session_id=_required_str(item, "session_id", "Diarization artifact"),
            source_artifact=ArtifactRef.from_dict(item.get("source_artifact")),
            source_id=_required_str(item, "source_id", "Diarization artifact"),
            context=DiarizationContext(
                capture_profile=_required_str(
                    context_data, "capture_profile", "Diarization context"
                ),
                source_id=_required_str(context_data, "source_id", "Diarization context"),
                enabled=_required_bool(context_data, "enabled", "Diarization context"),
                remote_speaker_count=_optional_int(
                    context_data,
                    "remote_speaker_count",
                    "Diarization context",
                ),
            ),
            provider=DiarizationProviderMetadata.from_dict(item.get("provider")),
            speakers=tuple(
                DiarizedSpeaker.from_dict(speaker) for speaker in _list(item, "speakers")
            ),
            segments=tuple(
                DiarizationSegment.from_dict(segment) for segment in _list(item, "segments")
            ),
            base_artifacts=tuple(
                ArtifactRef.from_dict(artifact_ref)
                for artifact_ref in _list(item, "base_artifacts")
            ),
            overlay_entries=tuple(
                _json_object(entry, "Diarization correction overlay entry")
                for entry in _list(item, "overlay_entries")
            ),
            created_at=_required_str(item, "created_at", "Diarization artifact"),
        )
        _validate_artifact(artifact)
        return artifact

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "kind": self.kind,
            "session_id": self.session_id,
            "source_artifact": self.source_artifact.to_dict(),
            "source_id": self.source_id,
            "context": self.context.to_dict(),
            "provider": self.provider.to_dict(),
            "speakers": [speaker.to_dict() for speaker in self.speakers],
            "segments": [segment.to_dict() for segment in self.segments],
            "base_artifacts": [artifact.to_dict() for artifact in self.base_artifacts],
            "overlay_entries": [dict(entry) for entry in self.overlay_entries],
            "created_at": self.created_at,
        }

    def to_json_bytes(self) -> bytes:
        return (json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n").encode("utf-8")


@dataclass(frozen=True)
class DiarizationRequest:
    """Provider-facing request for one eligible source-audio artifact."""

    session_id: str
    source_artifact: ArtifactRef
    source_id: str
    context: DiarizationContext
    audio_path: Path | None = None
    created_at: str | None = None


@dataclass(frozen=True)
class DiarizationResult:
    """Provider output before persistence."""

    provider: DiarizationProviderMetadata
    speakers: tuple[DiarizedSpeaker, ...]
    regular_segments: tuple[DiarizationSegment, ...]
    exclusive_segments: tuple[DiarizationSegment, ...]


class DiarizationProvider(Protocol):
    """Port implemented by local diarization providers."""

    name: str

    @property
    def config_id(self) -> str:
        """Stable provider configuration identity for checkpoint reuse."""

    def diarize(self, request: DiarizationRequest) -> DiarizationResult:
        """Diarize one eligible source-audio artifact."""


class FakeDiarizationProvider:
    """Deterministic fake diarization provider for fast workflow and contract tests."""

    name = "fake"

    def __init__(
        self,
        *,
        speakers: tuple[DiarizedSpeaker, ...] | None = None,
        regular_segments: tuple[DiarizationSegment, ...] | None = None,
        exclusive_segments: tuple[DiarizationSegment, ...] | None = None,
        checkpoint: str = "deterministic-fixture",
    ) -> None:
        self.provider = DiarizationProviderMetadata(
            name=self.name,
            checkpoint=checkpoint,
            device="test",
            parameters={},
            metadata={"purpose": "contract-test"},
        )
        self.speakers = speakers or (
            DiarizedSpeaker(
                id="speaker_1",
                label="Remote speaker 1",
                embedding=SpeakerEmbeddingRef.unavailable("fake provider does not emit embeddings"),
            ),
            DiarizedSpeaker(
                id="speaker_2",
                label="Remote speaker 2",
                embedding=SpeakerEmbeddingRef.unavailable("fake provider does not emit embeddings"),
            ),
        )
        self.regular_segments = regular_segments or (
            DiarizationSegment(
                id="turn_000001",
                speaker_id="speaker_1",
                start_seconds=0.0,
                end_seconds=1.0,
                overlapped=False,
            ),
            DiarizationSegment(
                id="turn_000002",
                speaker_id="speaker_2",
                start_seconds=0.8,
                end_seconds=1.6,
                overlapped=True,
            ),
        )
        self.exclusive_segments = exclusive_segments or (
            DiarizationSegment(
                id="exclusive_000001",
                speaker_id="speaker_1",
                start_seconds=0.0,
                end_seconds=0.8,
                overlapped=False,
            ),
            DiarizationSegment(
                id="exclusive_000002",
                speaker_id="speaker_2",
                start_seconds=0.8,
                end_seconds=1.6,
                overlapped=True,
                metadata={"projection_reason": "overlap assigned to latest speaker"},
            ),
        )
        self.calls: list[DiarizationRequest] = []

    @property
    def config_id(self) -> str:
        return self.provider.config_id

    def diarize(self, request: DiarizationRequest) -> DiarizationResult:
        self.calls.append(request)
        return DiarizationResult(
            provider=self.provider,
            speakers=self.speakers,
            regular_segments=self.regular_segments,
            exclusive_segments=self.exclusive_segments,
        )


PipelineFactory = Callable[..., Any]
ModuleLoader = Callable[[str], Any]


class PyannoteCommunityDiarizationProvider:
    """Optional local `pyannote/speaker-diarization-community-1` adapter boundary."""

    name = "pyannote"

    def __init__(
        self,
        *,
        checkpoint: str = "pyannote/speaker-diarization-community-1",
        device: str = "cpu",
        token: str | None = None,
        pipeline_factory: PipelineFactory | None = None,
        module_loader: ModuleLoader = importlib.import_module,
    ) -> None:
        self.checkpoint = checkpoint
        self.device = device
        self.token = token
        self._pipeline_factory = pipeline_factory
        self._module_loader = module_loader
        self._pipeline: Any | None = None
        self.provider = DiarizationProviderMetadata(
            name=self.name,
            checkpoint=checkpoint,
            device=device,
            parameters={"token_provided": token is not None},
            metadata={"adapter": "pyannote.audio Pipeline"},
        )

    @property
    def config_id(self) -> str:
        return self.provider.config_id

    def diarize(self, request: DiarizationRequest) -> DiarizationResult:
        if request.audio_path is None:
            raise DiarizationInputError("pyannote diarization requires an audio_path.")
        pipeline = self._load_pipeline()
        output = pipeline(str(request.audio_path))
        regular_segments = tuple(_segments_from_pyannote(output))
        if not regular_segments:
            raise DiarizationError("pyannote returned no speaker turns.")
        speakers = tuple(
            DiarizedSpeaker(
                id=speaker_id,
                label=f"Speaker {index}",
                embedding=SpeakerEmbeddingRef.unavailable(
                    "pyannote community-1 adapter boundary does not export embeddings yet"
                ),
            )
            for index, speaker_id in enumerate(_speaker_ids(regular_segments), start=1)
        )
        return DiarizationResult(
            provider=self.provider,
            speakers=speakers,
            regular_segments=regular_segments,
            exclusive_segments=_exclusive_projection(regular_segments),
        )

    def _load_pipeline(self) -> Any:
        if self._pipeline is not None:
            return self._pipeline
        if self._pipeline_factory is None:
            try:
                module = self._module_loader("pyannote.audio")
            except ImportError as exc:
                raise DiarizationProviderUnavailable(
                    "pyannote.audio is not installed in this environment."
                ) from exc
            self._pipeline_factory = cast(PipelineFactory, module.Pipeline.from_pretrained)
        try:
            self._pipeline = self._pipeline_factory(self.checkpoint, token=self.token)
        except TypeError:
            self._pipeline = self._pipeline_factory(self.checkpoint)
        return self._pipeline


@dataclass(frozen=True)
class DiarizationStageResult:
    """Result of running, reusing, or skipping one diarization stage."""

    state: ProcessingStageState
    artifacts: tuple[DiarizationArtifact, ...] = ()
    artifact_records: tuple[ArtifactRecord, ...] = ()
    skipped: bool = False
    skip_reason: str | None = None


def persist_diarization_artifact(
    *,
    store: ArtifactStore,
    artifact: DiarizationArtifact,
    name: str | Path,
) -> ArtifactRecord:
    """Persist one diarization payload through the artifact store contract."""

    DiarizationArtifact.from_dict(artifact.to_dict())
    return store.store(
        artifact_type=DIARIZATION_ARTIFACT_TYPE,
        name=name,
        data=artifact.to_json_bytes(),
        version=DIARIZATION_ARTIFACT_VERSION,
        metadata={
            "category": "derived",
            "contract": "diarization.v1",
            "kind": artifact.kind,
            "provider": artifact.provider.name,
            "checkpoint": artifact.provider.checkpoint,
            "provider_config_id": artifact.provider.config_id,
            "source_artifact_id": artifact.source_artifact.id,
            "source_id": artifact.source_id,
            "capture_profile": artifact.context.capture_profile,
            "scenario_policy_id": artifact.context.policy_id,
        },
    )


def run_diarization_stage(
    *,
    store: ArtifactStore,
    ledger: ProcessingStageLedger,
    provider: DiarizationProvider,
    source_artifact: ArtifactRecord,
    context: DiarizationContext,
    audio_path: Path | None = None,
    stage: str = DIARIZATION_STAGE,
    attempt: int = 1,
    skip_completed: bool = True,
    now: datetime | None = None,
) -> DiarizationStageResult:
    """Run one scenario-aware diarization stage and write processing checkpoints."""

    input_ref = ArtifactRef.from_record(source_artifact)
    decision = decide_diarization(context)
    if not decision.should_run:
        state = ledger.record(
            ProcessingStageState(
                version=1,
                session_id=ledger.session_id,
                run_id=ledger.run_id,
                stage=stage,
                status="skipped",
                input_artifacts=(input_ref,),
                output_artifacts=(),
                started_at=_timestamp(now),
                completed_at=_timestamp(now),
                error=None,
                attempt=attempt,
            )
        )
        return DiarizationStageResult(state=state, skipped=True, skip_reason=decision.reason)

    if skip_completed:
        completed = _completed_state_for_context(
            store=store,
            ledger=ledger,
            stage=stage,
            input_artifacts=(input_ref,),
            provider_config_id=provider.config_id,
            context=context,
        )
        if completed is not None:
            records = tuple(
                _record_for_ref(store=store, artifact_ref=ref) for ref in completed.output_artifacts
            )
            artifacts = tuple(
                DiarizationArtifact.from_dict(json.loads(store.retrieve(record.id)))
                for record in records
            )
            return DiarizationStageResult(
                state=completed,
                artifacts=artifacts,
                artifact_records=records,
                skipped=True,
                skip_reason="completed_output_reused",
            )

    started = ledger.mark_running(
        stage=stage,
        input_artifacts=(input_ref,),
        attempt=attempt,
        now=now,
    )
    verification = store.verify(source_artifact.id)
    if not verification.ok:
        failed = _mark_diarization_failed(
            ledger=ledger,
            stage=stage,
            input_ref=input_ref,
            attempt=attempt,
            started_at=started.started_at,
            now=now,
            message=verification.error or "source artifact failed verification",
            retryable=False,
            code="input_artifact_invalid",
        )
        raise DiarizationInputError(failed.error.message if failed.error else "invalid input")

    try:
        request = DiarizationRequest(
            session_id=source_artifact.session_id,
            source_artifact=input_ref,
            source_id=context.source_id,
            context=context,
            audio_path=audio_path,
            created_at=_timestamp(now),
        )
        result = provider.diarize(request)
        artifacts, records = _persist_diarization_outputs(
            store=store,
            ledger=ledger,
            source_artifact=source_artifact,
            context=context,
            provider_result=result,
            attempt=attempt,
            now=now,
        )
    except DiarizationProviderUnavailable as exc:
        failed = _mark_diarization_failed(
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
        raise DiarizationProviderUnavailable(
            failed.error.message if failed.error else str(exc)
        ) from exc
    except DiarizationError as exc:
        failed = _mark_diarization_failed(
            ledger=ledger,
            stage=stage,
            input_ref=input_ref,
            attempt=attempt,
            started_at=started.started_at,
            now=now,
            message=str(exc),
            retryable=False,
            code="diarization_failed",
        )
        raise DiarizationError(failed.error.message if failed.error else str(exc)) from exc

    completed = ledger.mark_completed(
        stage=stage,
        input_artifacts=(input_ref,),
        output_artifacts=tuple(ArtifactRef.from_record(record) for record in records),
        attempt=attempt,
        started_at=started.started_at,
        now=now,
    )
    return DiarizationStageResult(
        state=completed,
        artifacts=artifacts,
        artifact_records=records,
    )


def _persist_diarization_outputs(
    *,
    store: ArtifactStore,
    ledger: ProcessingStageLedger,
    source_artifact: ArtifactRecord,
    context: DiarizationContext,
    provider_result: DiarizationResult,
    attempt: int,
    now: datetime | None,
) -> tuple[tuple[DiarizationArtifact, ...], tuple[ArtifactRecord, ...]]:
    input_ref = ArtifactRef.from_record(source_artifact)
    created_at = _timestamp(now)
    regular = DiarizationArtifact(
        version=DIARIZATION_SCHEMA_VERSION,
        kind="regular_timeline",
        session_id=source_artifact.session_id,
        source_artifact=input_ref,
        source_id=context.source_id,
        context=context,
        provider=provider_result.provider,
        speakers=provider_result.speakers,
        segments=provider_result.regular_segments,
        base_artifacts=(),
        overlay_entries=(),
        created_at=created_at,
    )
    regular_record = persist_diarization_artifact(
        store=store,
        artifact=regular,
        name=_diarization_artifact_name(
            run_id=ledger.run_id,
            source_id=context.source_id,
            source_sha256=source_artifact.sha256,
            context=context,
            provider_config_id=provider_result.provider.config_id,
            attempt=attempt,
            kind="regular_timeline",
        ),
    )
    exclusive = DiarizationArtifact(
        version=DIARIZATION_SCHEMA_VERSION,
        kind="exclusive_projection",
        session_id=source_artifact.session_id,
        source_artifact=input_ref,
        source_id=context.source_id,
        context=context,
        provider=provider_result.provider,
        speakers=provider_result.speakers,
        segments=provider_result.exclusive_segments,
        base_artifacts=(ArtifactRef.from_record(regular_record),),
        overlay_entries=(),
        created_at=created_at,
    )
    exclusive_record = persist_diarization_artifact(
        store=store,
        artifact=exclusive,
        name=_diarization_artifact_name(
            run_id=ledger.run_id,
            source_id=context.source_id,
            source_sha256=source_artifact.sha256,
            context=context,
            provider_config_id=provider_result.provider.config_id,
            attempt=attempt,
            kind="exclusive_projection",
        ),
    )
    overlay = DiarizationArtifact(
        version=DIARIZATION_SCHEMA_VERSION,
        kind="correction_overlay",
        session_id=source_artifact.session_id,
        source_artifact=input_ref,
        source_id=context.source_id,
        context=context,
        provider=provider_result.provider,
        speakers=provider_result.speakers,
        segments=(),
        base_artifacts=(
            ArtifactRef.from_record(regular_record),
            ArtifactRef.from_record(exclusive_record),
        ),
        overlay_entries=(),
        created_at=created_at,
    )
    overlay_record = persist_diarization_artifact(
        store=store,
        artifact=overlay,
        name=_diarization_artifact_name(
            run_id=ledger.run_id,
            source_id=context.source_id,
            source_sha256=source_artifact.sha256,
            context=context,
            provider_config_id=provider_result.provider.config_id,
            attempt=attempt,
            kind="correction_overlay",
        ),
    )
    return (regular, exclusive, overlay), (regular_record, exclusive_record, overlay_record)


def _completed_state_for_context(
    *,
    store: ArtifactStore,
    ledger: ProcessingStageLedger,
    stage: str,
    input_artifacts: tuple[ArtifactRef, ...],
    provider_config_id: str,
    context: DiarizationContext,
) -> ProcessingStageState | None:
    expected_kinds = {"regular_timeline", "exclusive_projection", "correction_overlay"}
    for state in reversed(ledger.states(stage=stage)):
        if state.status != "completed" or state.input_artifacts != input_artifacts:
            continue
        if len(state.output_artifacts) != 3:
            continue
        records = tuple(
            _record_for_ref(store=store, artifact_ref=ref) for ref in state.output_artifacts
        )
        if {str(record.metadata.get("kind")) for record in records} != expected_kinds:
            continue
        if all(
            _artifact_ref_verifies(store=store, artifact_ref=ref)
            and record.metadata.get("provider_config_id") == provider_config_id
            and record.metadata.get("scenario_policy_id") == context.policy_id
            and record.metadata.get("source_id") == context.source_id
            for record, ref in zip(records, state.output_artifacts, strict=True)
        ):
            return state
    return None


def _mark_diarization_failed(
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
    raise DiarizationContractError("Diarization output ref does not match a known artifact.")


def _artifact_ref_verifies(*, store: ArtifactStore, artifact_ref: ArtifactRef) -> bool:
    try:
        record = _record_for_ref(store=store, artifact_ref=artifact_ref)
        verification = store.verify(record.id)
    except KeyError:
        return False
    return verification.ok


def _diarization_artifact_name(
    *,
    run_id: str,
    source_id: str,
    source_sha256: str,
    context: DiarizationContext,
    provider_config_id: str,
    attempt: int,
    kind: str,
) -> Path:
    _validate_stable_id(source_id, "Diarization source_id")
    _validate_stable_id(kind, "Diarization artifact kind")
    reuse_scope = sha256(f"{context.policy_id}:{provider_config_id}".encode()).hexdigest()[:12]
    return (
        Path(run_id)
        / source_id
        / source_sha256[:12]
        / reuse_scope
        / f"attempt-{attempt:03d}-{kind}.json"
    )


def _segments_from_pyannote(output: object) -> Iterable[DiarizationSegment]:
    itertracks = getattr(output, "itertracks", None)
    if itertracks is None:
        raise DiarizationContractError("pyannote output must expose itertracks.")
    for index, item in enumerate(itertracks(yield_label=True), start=1):
        try:
            turn, _track, label = item
        except ValueError as exc:
            raise DiarizationContractError(
                "pyannote itertracks must yield turn, track, label."
            ) from exc
        speaker_id = _stable_speaker_id(str(label))
        yield DiarizationSegment(
            id=f"turn_{index:06d}",
            speaker_id=speaker_id,
            start_seconds=_required_attr_number(turn, "start"),
            end_seconds=_required_attr_number(turn, "end"),
            overlapped=False,
        )


def _exclusive_projection(
    regular_segments: tuple[DiarizationSegment, ...],
) -> tuple[DiarizationSegment, ...]:
    projected = []
    previous_end = 0.0
    for index, segment in enumerate(
        sorted(regular_segments, key=lambda item: item.start_seconds), start=1
    ):
        start = max(segment.start_seconds, previous_end)
        if segment.end_seconds < start:
            continue
        projected.append(
            DiarizationSegment(
                id=f"exclusive_{index:06d}",
                speaker_id=segment.speaker_id,
                start_seconds=start,
                end_seconds=segment.end_seconds,
                overlapped=segment.overlapped or start != segment.start_seconds,
                metadata={"projection": "regular timeline projected to one speaker per instant"},
            )
        )
        previous_end = segment.end_seconds
    return tuple(projected)


def _speaker_ids(segments: tuple[DiarizationSegment, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(segment.speaker_id for segment in segments))


def _stable_speaker_id(label: str) -> str:
    normalized = re.sub(r"[^a-z0-9_]+", "_", label.lower()).strip("_")
    if not normalized or not normalized[0].isalpha():
        normalized = f"speaker_{normalized or 'unknown'}"
    return normalized


def _fallback_label(context: DiarizationContext) -> str:
    if context.capture_profile == "online_dual_stream" and context.source_id == "local_mic":
        return "You"
    if context.capture_profile == "online_dual_stream" and context.source_id == "system_audio":
        return "Remote"
    if context.capture_profile == "in_person_single_mic" and context.source_id == "room_mic":
        return "Room"
    return "Speaker unknown"


def _validate_artifact(artifact: DiarizationArtifact) -> None:
    if artifact.version != DIARIZATION_SCHEMA_VERSION:
        raise DiarizationContractError("Unsupported diarization artifact version.")
    if artifact.kind not in DIARIZATION_ARTIFACT_KINDS:
        raise DiarizationContractError("Unsupported diarization artifact kind.")
    _validate_stable_id(artifact.source_id, "Diarization source_id")
    if artifact.context.source_id != artifact.source_id:
        raise DiarizationContractError(
            "Diarization context source_id must match artifact source_id."
        )
    speaker_ids = {speaker.id for speaker in artifact.speakers}
    for segment in artifact.segments:
        if segment.speaker_id not in speaker_ids:
            raise DiarizationContractError("Diarization segment references an unknown speaker.")
    if artifact.kind in {"regular_timeline", "exclusive_projection"} and not artifact.segments:
        raise DiarizationContractError("Timeline diarization artifacts must contain segments.")
    if artifact.kind == "correction_overlay" and artifact.segments:
        raise DiarizationContractError(
            "Correction overlay seed must not contain generated segments."
        )
    if artifact.kind == "correction_overlay" and len(artifact.base_artifacts) != 2:
        raise DiarizationContractError(
            "Correction overlay seed must reference regular and exclusive artifacts."
        )


def _timestamp(now: datetime | None) -> str:
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    return timestamp.isoformat(timespec="seconds").replace("+00:00", "Z")


def _mapping(data: object, label: str) -> Mapping[str, object]:
    if not isinstance(data, dict):
        raise DiarizationContractError(f"{label} must be a JSON object.")
    return data


def _list(data: Mapping[str, object], key: str) -> list[object]:
    value = data.get(key)
    if not isinstance(value, list):
        raise DiarizationContractError(f"Diarization field '{key}' must be an array.")
    return value


def _required_str(data: Mapping[str, object], key: str, label: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise DiarizationContractError(f"{label} field '{key}' must be a non-empty string.")
    return value


def _optional_str(data: Mapping[str, object], key: str, label: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise DiarizationContractError(f"{label} field '{key}' must be null or a string.")
    return value


def _required_int(data: Mapping[str, object], key: str, label: str) -> int:
    value = data.get(key)
    if type(value) is not int:
        raise DiarizationContractError(f"{label} field '{key}' must be an integer.")
    return value


def _optional_int(data: Mapping[str, object], key: str, label: str) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if type(value) is not int:
        raise DiarizationContractError(f"{label} field '{key}' must be null or an integer.")
    return value


def _required_bool(data: Mapping[str, object], key: str, label: str) -> bool:
    value = data.get(key)
    if type(value) is not bool:
        raise DiarizationContractError(f"{label} field '{key}' must be a boolean.")
    return value


def _required_number(data: Mapping[str, object], key: str, label: str) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise DiarizationContractError(f"{label} field '{key}' must be a number.")
    return float(value)


def _json_object(data: object, label: str) -> Mapping[str, object]:
    if data is None:
        return {}
    if not isinstance(data, dict) or any(not isinstance(key, str) for key in data):
        raise DiarizationContractError(f"{label} must be a JSON object.")
    try:
        json.dumps(data)
    except TypeError as exc:
        raise DiarizationContractError(f"{label} must be JSON serializable.") from exc
    return data


def _validate_stable_id(value: str, label: str) -> None:
    if not _STABLE_ID_PATTERN.fullmatch(value):
        raise DiarizationContractError(f"{label} must be a stable identifier.")


def _validate_span(start_seconds: float, end_seconds: float, label: str) -> None:
    if start_seconds < 0 or end_seconds < 0 or end_seconds < start_seconds:
        raise DiarizationContractError(f"{label} timestamps must be non-negative and ordered.")


def _required_attr_number(data: object, attr: str) -> float:
    value = getattr(data, attr, None)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise DiarizationContractError(f"pyannote turn attribute '{attr}' must be numeric.")
    return float(value)
