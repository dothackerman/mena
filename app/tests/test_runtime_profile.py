from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from mena.artifacts import FileSystemArtifactStore
from mena.diarization import DiarizationProvider, DiarizationProviderUnavailable
from mena.provider_validation import ProviderValidationError
from mena.runtime_profile import (
    BoundProcessingRuntime,
    RuntimeFallbackUnavailable,
    RuntimeProfileSelection,
    bind_processing_runtime,
    run_processing_with_runtime_fallback,
    runtime_fallback_stage_for_provider_failure,
    runtime_stage_policy_payload,
)
from mena.semantic_coherence import SemanticCoherenceProvider
from mena.settings import AppSettings
from mena.setup import (
    CommandIdentity,
    RuntimeFallbackAuthorization,
    RuntimeProfileState,
    SetupEvaluation,
    StageProfile,
)
from mena.transcription import (
    FasterWhisperModelProfile,
    TranscriptionProvider,
    TranscriptionProviderUnavailable,
)


def _runtime_state(profile_id: str, *, device: str = "cuda") -> RuntimeProfileState:
    return RuntimeProfileState(
        schema_version=1,
        catalog_version="catalog-v1",
        accepted_profile_id=profile_id,
        accepted_profile_version="2026.07",
        accepted_at="2026-07-02T12:00:00Z",
        product_version="0.1.0-test",
        stage_policies=(
            StageProfile(
                stage="transcription",
                provider="faster-whisper",
                model="small",
                device=device,
                policy="local-only",
            ),
            StageProfile(
                stage="diarization",
                provider="pyannote",
                model="community-1",
                device=device,
                policy="local-only",
            ),
            StageProfile(
                stage="semantic-review",
                provider="ollama",
                model="llama3.2:3b",
                device="cpu",
                policy="optional-local",
            ),
        ),
        selected_asr_profile=FasterWhisperModelProfile(
            id=f"asr-{device}",
            checkpoint="small",
            device=device,
            compute_type="int8",
            reason="test",
        ),
        readiness_summary=(),
        evidence_digest=f"digest-{profile_id}",
        evidence={},
        fallback_authorization=RuntimeFallbackAuthorization(
            local_status="authorized",
            local_profile_id="fallback",
            local_profile_version="2026.07",
            automatic_local_fallback_authorized=True,
            stage_scope=("transcription", "diarization"),
            placement_change_allowed=True,
            profile_change_allowed=True,
            remote_status="not_configured",
        ),
    )


def _selection(
    profile_id: str,
    *,
    fallback_used: bool = False,
    fallback_status: str = "not_needed",
    device: str = "cuda",
) -> RuntimeProfileSelection:
    state = _runtime_state(profile_id, device=device)
    return RuntimeProfileSelection(
        accepted=_runtime_state("accepted"),
        actual=state,
        fallback_used=fallback_used,
        current_command_identity=cast(
            CommandIdentity,
            SimpleNamespace(
                version="0.1.0-test",
                current_executable="/tmp/mena",
                plain_mena_path=None,
                invocation_mode="test",
            ),
        ),
        revalidation=cast(SetupEvaluation, SimpleNamespace(ready=True, blocking_reasons=())),
        fallback_status=fallback_status,
    )


class _Transcription:
    name = "transcription"

    def __init__(self, label: str = "primary") -> None:
        self.label = label

    def transcribe(self, _request: object) -> object:
        raise AssertionError("not called")


class _Diarization:
    name = "diarization"

    def __init__(self, label: str = "primary") -> None:
        self.label = label

    @property
    def config_id(self) -> str:
        return self.label

    def diarize(self, _request: object) -> object:
        raise AssertionError("not called")


class _Semantic:
    def __init__(self) -> None:
        self.metadata = SimpleNamespace(config_id="semantic", to_dict=lambda: {})

    def check(self, _request: object) -> object:
        raise AssertionError("not called")


def test_runtime_fallback_stage_for_provider_failure_maps_provider_errors() -> None:
    assert (
        runtime_fallback_stage_for_provider_failure(
            TranscriptionProviderUnavailable("cuda unavailable")
        )
        == "transcription"
    )
    assert (
        runtime_fallback_stage_for_provider_failure(
            DiarizationProviderUnavailable("cuda unavailable")
        )
        == "diarization"
    )


def test_bind_processing_runtime_constructs_bound_provider_set() -> None:
    semantic = cast(SemanticCoherenceProvider, _Semantic())

    runtime = bind_processing_runtime(
        selection=_selection("accepted"),
        settings=cast(
            AppSettings,
            SimpleNamespace(local_llm=SimpleNamespace(ollama_endpoint="http://ollama")),
        ),
        transcription_provider_factory=_Transcription,
        diarization_provider_factory=_Diarization,
        semantic_coherence_provider_factory=_Semantic,
        semantic_coherence_provider=semantic,
    )

    assert runtime.selection.actual.accepted_profile_id == "accepted"
    assert isinstance(runtime.transcription_provider, _Transcription)
    assert isinstance(runtime.diarization_provider, _Diarization)
    assert runtime.semantic_coherence_provider is semantic
    assert runtime_stage_policy_payload(runtime.selection.actual)[0]["stage"] == "transcription"


def test_bind_processing_runtime_validates_bound_injected_providers() -> None:
    class InvalidTranscription:
        name = "invalid-transcription"
        transcribe = "not-callable"

    with pytest.raises(
        ProviderValidationError,
        match="transcription provider must implement TranscriptionProvider",
    ):
        bind_processing_runtime(
            selection=_selection("accepted"),
            settings=cast(
                AppSettings,
                SimpleNamespace(local_llm=SimpleNamespace(ollama_endpoint="http://ollama")),
            ),
            transcription_provider_factory=InvalidTranscription,
            diarization_provider_factory=_Diarization,
            semantic_coherence_provider_factory=_Semantic,
            semantic_coherence_provider=cast(SemanticCoherenceProvider, _Semantic()),
        )


def test_bind_processing_runtime_wraps_bound_factory_type_errors() -> None:
    def broken_factory() -> object:
        raise TypeError("missing constructor argument")

    with pytest.raises(
        ProviderValidationError,
        match="transcription provider must implement TranscriptionProvider",
    ):
        bind_processing_runtime(
            selection=_selection("accepted"),
            settings=cast(
                AppSettings,
                SimpleNamespace(local_llm=SimpleNamespace(ollama_endpoint="http://ollama")),
            ),
            transcription_provider_factory=broken_factory,
            diarization_provider_factory=_Diarization,
            semantic_coherence_provider_factory=_Semantic,
            semantic_coherence_provider=cast(SemanticCoherenceProvider, _Semantic()),
        )


def test_run_processing_with_runtime_fallback_retries_once_and_records_provenance() -> None:
    calls: list[str] = []
    stored: list[dict[str, object]] = []
    primary = BoundProcessingRuntime(
        selection=_selection("accepted"),
        transcription_provider=cast(TranscriptionProvider, _Transcription("primary")),
        diarization_provider=cast(DiarizationProvider, _Diarization("primary")),
        semantic_coherence_provider=cast(SemanticCoherenceProvider, _Semantic()),
    )

    def _run(runtime: BoundProcessingRuntime) -> str:
        calls.append(runtime.selection.actual.accepted_profile_id)
        if len(calls) == 1:
            raise TranscriptionProviderUnavailable("gpu exhausted")
        return runtime.selection.actual.accepted_profile_id

    def _fallback_selector(**kwargs: Any) -> RuntimeProfileSelection:
        assert kwargs["failed_stage"] == "transcription"
        assert kwargs["failure_class"] == "TranscriptionProviderUnavailable"
        assert kwargs["failed_input_artifacts"] == ("artifact-1",)
        return _selection(
            "fallback",
            fallback_used=True,
            fallback_status="used_after_stage_failure",
            device="cpu",
        )

    def _store(**kwargs: object) -> None:
        stored.append(kwargs)

    result = run_processing_with_runtime_fallback(
        runtime=primary,
        run=_run,
        settings=cast(
            AppSettings,
            SimpleNamespace(local_llm=SimpleNamespace(ollama_endpoint="http://ollama")),
        ),
        store=cast(FileSystemArtifactStore, SimpleNamespace()),
        run_id="run-1",
        attempt=1,
        runner=None,
        command_identity=None,
        hardware=None,
        allow_runtime_fallback=False,
        authorize_runtime_fallback=None,
        transcription_provider_factory=_Transcription,
        diarization_provider_factory=_Diarization,
        semantic_coherence_provider_factory=_Semantic,
        failed_input_artifacts=lambda stage: ("artifact-1",) if stage == "transcription" else (),
        runtime_profile_selector_after_failure_fn=_fallback_selector,
        runtime_profile_store_fn=_store,
    )

    assert result.value == "fallback"
    assert calls == ["accepted", "fallback"]
    assert result.runtime.selection.fallback_status == "used_after_stage_failure"
    assert result.runtime.semantic_coherence_provider is primary.semantic_coherence_provider
    assert stored[0]["name_suffix"] == "stage-fallback"


def test_run_processing_with_runtime_fallback_does_not_retry_after_fallback_used() -> None:
    runtime = BoundProcessingRuntime(
        selection=_selection("fallback", fallback_used=True, device="cpu"),
        transcription_provider=cast(TranscriptionProvider, _Transcription("fallback")),
        diarization_provider=cast(DiarizationProvider, _Diarization("fallback")),
        semantic_coherence_provider=cast(SemanticCoherenceProvider, _Semantic()),
    )

    with pytest.raises(TranscriptionProviderUnavailable):
        run_processing_with_runtime_fallback(
            runtime=runtime,
            run=lambda _runtime: (_ for _ in ()).throw(
                TranscriptionProviderUnavailable("still unavailable")
            ),
            settings=cast(
                AppSettings,
                SimpleNamespace(local_llm=SimpleNamespace(ollama_endpoint="http://ollama")),
            ),
            store=cast(FileSystemArtifactStore, SimpleNamespace()),
            run_id="run-1",
            attempt=1,
            runner=None,
            command_identity=None,
            hardware=None,
            allow_runtime_fallback=False,
            authorize_runtime_fallback=None,
            transcription_provider_factory=_Transcription,
            diarization_provider_factory=_Diarization,
            semantic_coherence_provider_factory=_Semantic,
            failed_input_artifacts=lambda _stage: (),
            runtime_profile_selector_after_failure_fn=lambda **_kwargs: (_ for _ in ()).throw(
                RuntimeFallbackUnavailable("should not retry")
            ),
            runtime_profile_store_fn=lambda **_kwargs: None,
        )
