"""Runtime profile selection, provider construction, and provenance."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mena.artifacts import ArtifactRecord, FileSystemArtifactStore
from mena.diarization import PyannoteCommunityDiarizationProvider
from mena.semantic_coherence import OllamaSemanticCoherenceProvider
from mena.settings import AppSettings
from mena.setup import (
    CommandIdentity,
    CommandRunner,
    HardwareFacts,
    RuntimeProfileState,
    SetupEvaluation,
    SubprocessCommandRunner,
    build_runtime_profile_state,
    default_profile_catalog,
    detect_local_installation_state,
    evaluate_setup,
    load_runtime_profile_state,
)
from mena.summarization import OllamaSummarizationProvider
from mena.transcription import FasterWhisperTranscriptionProvider

ProviderFactory = Callable[[], Any]
RUNTIME_PROFILE_PROVENANCE_CONTRACT = "runtime-profile-provenance.v1"


@dataclass(frozen=True)
class RuntimeProfileSelection:
    accepted: RuntimeProfileState
    actual: RuntimeProfileState
    fallback_used: bool
    current_command_identity: CommandIdentity
    revalidation: SetupEvaluation
    fallback_reason: str | None = None
    fallback_status: str = "not_needed"


def select_runtime_profile(
    *,
    settings: AppSettings,
    runner: CommandRunner | None,
    command_identity: CommandIdentity | None,
    hardware: HardwareFacts | None,
    allow_runtime_fallback: bool,
) -> RuntimeProfileSelection:
    """Select the accepted or explicit fallback runtime profile for derived artifacts."""

    accepted = load_runtime_profile_state(settings.paths)
    catalog = {
        (profile.id, profile.version): profile
        for profile in default_profile_catalog(_stage_model(accepted, "summarization"))
    }
    accepted_profile = catalog.get(
        (accepted.accepted_profile_id, accepted.accepted_profile_version)
    )
    if accepted_profile is None or accepted.catalog_version != accepted_profile.catalog_version:
        raise ValueError(
            "Accepted runtime profile state is incompatible with the bundled profile catalog; "
            "run `mena setup` again."
        )

    command_runner = runner or SubprocessCommandRunner()
    accepted_state = detect_local_installation_state(
        settings,
        runner=command_runner,
        command_identity=command_identity,
        hardware=hardware,
        profile=accepted_profile,
    )
    accepted_evaluation = evaluate_setup(accepted_profile, accepted_state)
    accepted_failure = _runtime_profile_failure(accepted, accepted_evaluation)
    if accepted_failure is None:
        return RuntimeProfileSelection(
            accepted=accepted,
            actual=build_runtime_profile_state(accepted_evaluation),
            fallback_used=False,
            current_command_identity=accepted_evaluation.state.command_identity,
            revalidation=accepted_evaluation,
        )

    fallback_profile = next((profile for profile in catalog.values() if profile.fallback), None)
    if not allow_runtime_fallback:
        raise ValueError(
            "Accepted runtime profile is unavailable: "
            f"{accepted_failure} Re-run setup or pass --allow-runtime-fallback for an explicit "
            "one-run fallback when supported."
        )
    if fallback_profile is None:
        raise ValueError(f"Accepted runtime profile is unavailable: {accepted_failure}")

    fallback_state = detect_local_installation_state(
        settings,
        runner=command_runner,
        command_identity=command_identity,
        hardware=hardware,
        profile=fallback_profile,
    )
    fallback_evaluation = evaluate_setup(fallback_profile, fallback_state)
    if not fallback_evaluation.ready:
        reasons = "; ".join(fallback_evaluation.blocking_reasons)
        raise ValueError(
            "Accepted runtime profile is unavailable and fallback is not currently viable: "
            f"{accepted_failure} Fallback blockers: {reasons}"
        )
    fallback_runtime_state = build_runtime_profile_state(fallback_evaluation)
    return RuntimeProfileSelection(
        accepted=accepted,
        actual=fallback_runtime_state,
        fallback_used=True,
        current_command_identity=fallback_evaluation.state.command_identity,
        revalidation=fallback_evaluation,
        fallback_reason=accepted_failure,
        fallback_status="used",
    )


def production_runtime_required(
    *,
    transcription_provider_factory: ProviderFactory,
    diarization_provider_factory: ProviderFactory,
    semantic_coherence_provider_factory: ProviderFactory,
) -> bool:
    return (
        transcription_provider_factory is FasterWhisperTranscriptionProvider
        or diarization_provider_factory is PyannoteCommunityDiarizationProvider
        or semantic_coherence_provider_factory is OllamaSemanticCoherenceProvider
    )


def runtime_transcription_factory(
    factory: ProviderFactory,
    runtime_state: RuntimeProfileState,
) -> ProviderFactory:
    if factory is not FasterWhisperTranscriptionProvider:
        return factory
    profile = runtime_state.selected_asr_profile

    def _factory() -> FasterWhisperTranscriptionProvider:
        return FasterWhisperTranscriptionProvider(profile=profile, local_files_only=True)

    return _factory


def runtime_diarization_factory(
    factory: ProviderFactory,
    runtime_state: RuntimeProfileState,
) -> ProviderFactory:
    if factory is not PyannoteCommunityDiarizationProvider:
        return factory
    device = stage_device(runtime_state, "diarization") or "cpu"

    def _factory() -> PyannoteCommunityDiarizationProvider:
        return PyannoteCommunityDiarizationProvider(device=device)

    return _factory


def runtime_semantic_factory(
    factory: ProviderFactory,
    *,
    settings: AppSettings,
    runtime_state: RuntimeProfileState,
) -> ProviderFactory:
    if factory is not OllamaSemanticCoherenceProvider:
        return factory
    model = _stage_model(runtime_state, "semantic-review")

    def _factory() -> OllamaSemanticCoherenceProvider:
        return OllamaSemanticCoherenceProvider(
            model=model,
            endpoint=settings.local_llm.ollama_endpoint,
        )

    return _factory


def runtime_summarization_provider(
    *,
    settings: AppSettings,
    runtime_state: RuntimeProfileState,
) -> OllamaSummarizationProvider:
    return OllamaSummarizationProvider(
        model=_stage_model(runtime_state, "summarization"),
        endpoint=settings.local_llm.ollama_endpoint,
    )


def store_runtime_profile_provenance(
    *,
    store: FileSystemArtifactStore,
    run_id: str,
    selection: RuntimeProfileSelection,
) -> ArtifactRecord:
    payload = {
        "contract": RUNTIME_PROFILE_PROVENANCE_CONTRACT,
        "run_id": run_id,
        "accepted_profile": {
            "id": selection.accepted.accepted_profile_id,
            "version": selection.accepted.accepted_profile_version,
            "catalog_version": selection.accepted.catalog_version,
            "accepted_at": selection.accepted.accepted_at,
            "product_version": selection.accepted.product_version,
        },
        "actual_profile": {
            "id": selection.actual.accepted_profile_id,
            "version": selection.actual.accepted_profile_version,
            "catalog_version": selection.actual.catalog_version,
            "product_version": selection.actual.product_version,
            "selected_asr_profile": selection.actual.selected_asr_profile.id,
            "selected_asr_device": selection.actual.selected_asr_profile.device,
            "selected_asr_checkpoint": selection.actual.selected_asr_profile.checkpoint,
            "stages": runtime_stage_policy_payload(selection.actual),
        },
        "current_product": {
            "version": selection.current_command_identity.version,
            "current_executable": str(selection.current_command_identity.current_executable),
            "plain_mena_path": None
            if selection.current_command_identity.plain_mena_path is None
            else str(selection.current_command_identity.plain_mena_path),
            "invocation_mode": selection.current_command_identity.invocation_mode,
        },
        "fallback": {
            "used": selection.fallback_used,
            "status": selection.fallback_status,
            "reason": selection.fallback_reason,
        },
        "revalidation": {
            "ready": selection.revalidation.ready,
            "blocking_reasons": list(selection.revalidation.blocking_reasons),
        },
    }
    return store.store(
        artifact_type="runtime_profile_provenance",
        name=f"{run_id}/runtime-profile-provenance.json",
        data=(json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        version=1,
        metadata={
            "contract": RUNTIME_PROFILE_PROVENANCE_CONTRACT,
            "run_id": run_id,
            "accepted_profile_id": selection.accepted.accepted_profile_id,
            "actual_profile_id": selection.actual.accepted_profile_id,
            "fallback_used": selection.fallback_used,
        },
    )


def runtime_stage_policy_payload(runtime_state: RuntimeProfileState) -> list[dict[str, str]]:
    return [
        {
            "stage": policy.stage,
            "provider": policy.provider,
            "model": policy.model,
            "device": policy.device,
            "policy": policy.policy,
        }
        for policy in runtime_state.stage_policies
    ]


def stage_device(runtime_state: RuntimeProfileState, stage: str) -> str | None:
    for policy in runtime_state.stage_policies:
        if policy.stage == stage:
            return policy.device
    return None


def _stage_model(runtime_state: RuntimeProfileState, stage: str) -> str:
    for policy in runtime_state.stage_policies:
        if policy.stage == stage:
            return policy.model
    raise ValueError(
        f"Accepted runtime profile {runtime_state.accepted_profile_id!r} "
        f"does not define stage {stage!r}."
    )


def _runtime_profile_failure(
    accepted: RuntimeProfileState,
    evaluation: SetupEvaluation,
) -> str | None:
    if not evaluation.ready:
        return "; ".join(evaluation.blocking_reasons)
    expected = accepted.selected_asr_profile
    current = evaluation.selected_asr_profile
    if (
        expected.id != current.id
        or expected.checkpoint != current.checkpoint
        or expected.device != current.device
        or expected.compute_type != current.compute_type
    ):
        return (
            "current ASR runtime selection does not match accepted setup state "
            f"({current.id}/{current.checkpoint}/{current.device}/{current.compute_type} vs "
            f"{expected.id}/{expected.checkpoint}/{expected.device}/{expected.compute_type})"
        )
    return None
