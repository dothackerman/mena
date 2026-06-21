"""Runtime profile selection, provider construction, and provenance."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
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
    SetupPlan,
    SetupProfile,
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
PROCESSING_REQUIRED_RUNTIME_STAGES = frozenset({"transcription", "diarization"})
PROCESSING_OPTIONAL_RUNTIME_STAGES = frozenset({"semantic-review"})
PROCESSING_RUNTIME_STAGES = PROCESSING_REQUIRED_RUNTIME_STAGES | PROCESSING_OPTIONAL_RUNTIME_STAGES
SUMMARIZATION_RUNTIME_STAGES = frozenset({"summarization"})


@dataclass(frozen=True)
class RuntimeRevalidationScope:
    stages: frozenset[str]
    readiness_ids: frozenset[str]
    command_names: tuple[str, ...]
    python_surface_imports: frozenset[str]
    requires_asr_selection_match: bool


@dataclass(frozen=True)
class RuntimeProfileSelection:
    accepted: RuntimeProfileState
    actual: RuntimeProfileState
    fallback_used: bool
    current_command_identity: CommandIdentity
    revalidation: SetupEvaluation
    fallback_reason: str | None = None
    fallback_status: str = "not_needed"
    fallback_failed_stage: str | None = None
    fallback_failed_input_artifacts: tuple[str, ...] = ()
    fallback_failure_class: str | None = None
    fallback_authorization_source: str | None = None


def select_runtime_profile(
    *,
    settings: AppSettings,
    runner: CommandRunner | None,
    command_identity: CommandIdentity | None,
    hardware: HardwareFacts | None,
    allow_runtime_fallback: bool,
    authorize_runtime_fallback: Callable[[str, SetupEvaluation], bool] | None = None,
    required_stages: frozenset[str] | None = None,
) -> RuntimeProfileSelection:
    """Select the accepted or explicit fallback runtime profile for derived artifacts."""

    scope = _runtime_revalidation_scope(required_stages)
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
    accepted_detection_profile = _profile_for_scope(accepted_profile, scope)
    accepted_state = detect_local_installation_state(
        settings,
        runner=command_runner,
        command_identity=command_identity,
        hardware=hardware,
        profile=accepted_detection_profile,
    )
    accepted_evaluation = _scope_evaluation(
        evaluate_setup(accepted_detection_profile, accepted_state),
        scope,
    )
    accepted_failure = _runtime_profile_failure(accepted, accepted_evaluation, scope)
    if accepted_failure is None:
        return RuntimeProfileSelection(
            accepted=accepted,
            actual=build_runtime_profile_state(accepted_evaluation),
            fallback_used=False,
            current_command_identity=accepted_evaluation.state.command_identity,
            revalidation=accepted_evaluation,
        )

    fallback_profile = _fallback_profile(
        catalog=catalog,
        accepted=accepted,
        allow_runtime_fallback=allow_runtime_fallback,
    )
    if fallback_profile is None:
        raise ValueError(
            "Accepted runtime profile is unavailable and no accepted fallback profile "
            f"is configured: {accepted_failure}"
        )

    fallback_detection_profile = _profile_for_scope(fallback_profile, scope)
    fallback_state = detect_local_installation_state(
        settings,
        runner=command_runner,
        command_identity=command_identity,
        hardware=hardware,
        profile=fallback_detection_profile,
    )
    fallback_evaluation = _scope_evaluation(
        evaluate_setup(fallback_detection_profile, fallback_state),
        scope,
    )
    if not fallback_evaluation.ready:
        reasons = "; ".join(fallback_evaluation.blocking_reasons)
        raise ValueError(
            "Accepted runtime profile is unavailable and fallback is not currently viable: "
            f"{accepted_failure} Fallback blockers: {reasons}"
        )
    fallback_authorized = allow_runtime_fallback or _automatic_local_fallback_authorized(
        accepted,
        scope,
    )
    if not fallback_authorized:
        if authorize_runtime_fallback is None:
            raise ValueError(
                "Accepted runtime profile is unavailable: "
                f"{accepted_failure} Re-run setup to authorize local fallback or pass "
                "--allow-runtime-fallback for an explicit one-run fallback when supported."
            )
        if not authorize_runtime_fallback(accepted_failure, fallback_evaluation):
            raise ValueError("Accepted runtime profile is unavailable and fallback was declined.")
    fallback_runtime_state = build_runtime_profile_state(fallback_evaluation)
    return RuntimeProfileSelection(
        accepted=accepted,
        actual=fallback_runtime_state,
        fallback_used=True,
        current_command_identity=fallback_evaluation.state.command_identity,
        revalidation=fallback_evaluation,
        fallback_reason=accepted_failure,
        fallback_status="used",
        fallback_authorization_source=(
            "explicit_one_run"
            if allow_runtime_fallback
            else "setup_authorized"
            if _automatic_local_fallback_authorized(accepted, scope)
            else "interactive"
        ),
    )


def select_runtime_profile_after_stage_failure(
    *,
    settings: AppSettings,
    runner: CommandRunner | None,
    command_identity: CommandIdentity | None,
    hardware: HardwareFacts | None,
    accepted: RuntimeProfileState,
    failed_stage: str,
    failure_reason: str,
    failure_class: str,
    failed_input_artifacts: tuple[str, ...],
    allow_runtime_fallback: bool,
    authorize_runtime_fallback: Callable[[str, SetupEvaluation], bool] | None = None,
) -> RuntimeProfileSelection:
    """Select the accepted fallback profile after a provider-time stage failure."""

    scope = _runtime_revalidation_scope(frozenset({failed_stage}))
    catalog = {
        (profile.id, profile.version): profile
        for profile in default_profile_catalog(_stage_model(accepted, "summarization"))
    }
    fallback_profile = _fallback_profile(
        catalog=catalog,
        accepted=accepted,
        allow_runtime_fallback=allow_runtime_fallback,
    )
    if fallback_profile is None:
        raise ValueError(
            f"Runtime stage '{failed_stage}' failed and no accepted fallback profile "
            f"is configured: {failure_reason}"
        )

    command_runner = runner or SubprocessCommandRunner()
    fallback_detection_profile = _profile_for_scope(fallback_profile, scope)
    fallback_state = detect_local_installation_state(
        settings,
        runner=command_runner,
        command_identity=command_identity,
        hardware=hardware,
        profile=fallback_detection_profile,
    )
    fallback_evaluation = _scope_evaluation(
        evaluate_setup(fallback_detection_profile, fallback_state),
        scope,
    )
    if not fallback_evaluation.ready:
        reasons = "; ".join(fallback_evaluation.blocking_reasons)
        raise ValueError(
            f"Runtime stage '{failed_stage}' failed and fallback is not currently viable: "
            f"{failure_reason} Fallback blockers: {reasons}"
        )

    authorization_source: str | None = None
    if allow_runtime_fallback:
        authorization_source = "explicit_one_run"
    elif _automatic_local_fallback_authorized(accepted, scope):
        authorization_source = "setup_authorized"
    elif authorize_runtime_fallback is None:
        raise ValueError(
            f"Runtime stage '{failed_stage}' failed: {failure_reason} Re-run setup to "
            "authorize local fallback or pass --allow-runtime-fallback for an explicit "
            "one-run fallback when supported."
        )
    elif authorize_runtime_fallback(failure_reason, fallback_evaluation):
        authorization_source = "interactive"
    else:
        raise ValueError(f"Runtime stage '{failed_stage}' failed and fallback was declined.")

    fallback_runtime_state = build_runtime_profile_state(fallback_evaluation)
    return RuntimeProfileSelection(
        accepted=accepted,
        actual=fallback_runtime_state,
        fallback_used=True,
        current_command_identity=fallback_evaluation.state.command_identity,
        revalidation=fallback_evaluation,
        fallback_reason=failure_reason,
        fallback_status="used_after_stage_failure",
        fallback_failed_stage=failed_stage,
        fallback_failed_input_artifacts=failed_input_artifacts,
        fallback_failure_class=failure_class,
        fallback_authorization_source=authorization_source,
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
            provider_options=stage_provider_options(runtime_state, "semantic-review"),
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
        provider_options=stage_provider_options(runtime_state, "summarization"),
    )


def store_runtime_profile_provenance(
    *,
    store: FileSystemArtifactStore,
    run_id: str,
    attempt: int = 1,
    selection: RuntimeProfileSelection,
    name_suffix: str = "",
) -> ArtifactRecord:
    suffix = "" if not name_suffix else f"-{name_suffix}"
    payload = {
        "contract": RUNTIME_PROFILE_PROVENANCE_CONTRACT,
        "run_id": run_id,
        "attempt": attempt,
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
            "failed_stage": getattr(selection, "fallback_failed_stage", None),
            "failed_input_artifacts": list(
                getattr(selection, "fallback_failed_input_artifacts", ())
            ),
            "failure_class": getattr(selection, "fallback_failure_class", None),
            "authorization_source": getattr(
                selection,
                "fallback_authorization_source",
                None,
            ),
            "authorization": selection.accepted.fallback_authorization.to_dict(),
        },
        "revalidation": {
            "ready": selection.revalidation.ready,
            "blocking_reasons": list(selection.revalidation.blocking_reasons),
        },
    }
    return store.store(
        artifact_type="runtime_profile_provenance",
        name=f"{run_id}/attempt-{attempt}/runtime-profile-provenance{suffix}.json",
        data=(json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        version=1,
        metadata={
            "contract": RUNTIME_PROFILE_PROVENANCE_CONTRACT,
            "run_id": run_id,
            "attempt": attempt,
            "accepted_profile_id": selection.accepted.accepted_profile_id,
            "actual_profile_id": selection.actual.accepted_profile_id,
            "fallback_used": selection.fallback_used,
        },
    )


def runtime_stage_policy_payload(runtime_state: RuntimeProfileState) -> list[dict[str, object]]:
    return [
        {
            "stage": policy.stage,
            "provider": policy.provider,
            "model": policy.model,
            "device": policy.device,
            "policy": policy.policy,
            "provider_options": dict(policy.provider_options),
        }
        for policy in runtime_state.stage_policies
    ]


def _runtime_revalidation_scope(
    required_stages: frozenset[str] | None,
) -> RuntimeRevalidationScope | None:
    if required_stages is None:
        return None
    readiness_ids = {"command:mena"}
    command_names = ["mena"]
    python_surface_imports: set[str] = set()
    if "summarization" in required_stages or "semantic-review" in required_stages:
        readiness_ids.update({"command:ollama", "ollama-model"})
        command_names.append("ollama")
    if "transcription" in required_stages:
        readiness_ids.update({"hardware", "python:faster_whisper", "asr-model"})
        python_surface_imports.add("faster_whisper")
    if "diarization" in required_stages:
        readiness_ids.update({"hardware", "python:pyannote.audio", "diarization-model"})
        python_surface_imports.add("pyannote.audio")
    return RuntimeRevalidationScope(
        stages=required_stages,
        readiness_ids=frozenset(readiness_ids),
        command_names=tuple(command_names),
        python_surface_imports=frozenset(python_surface_imports),
        requires_asr_selection_match="transcription" in required_stages,
    )


def _profile_for_scope(
    profile: SetupProfile,
    scope: RuntimeRevalidationScope | None,
) -> SetupProfile:
    if scope is None:
        return profile
    stages = tuple(stage for stage in profile.stages if stage.stage in scope.stages)
    python_surfaces = tuple(
        surface
        for surface in profile.python_surfaces
        if surface.import_name in scope.python_surface_imports
    )
    return replace(
        profile,
        stages=stages,
        python_surfaces=python_surfaces,
        required_commands=scope.command_names,
        playback_commands=(),
    )


def _scope_evaluation(
    evaluation: SetupEvaluation,
    scope: RuntimeRevalidationScope | None,
) -> SetupEvaluation:
    if scope is None:
        return evaluation
    readiness = tuple(item for item in evaluation.readiness if item.id in scope.readiness_ids)
    actions = tuple(
        action for action in evaluation.plan.actions if action.id in scope.readiness_ids
    )
    return SetupEvaluation(
        profile=evaluation.profile,
        selected_asr_profile=evaluation.selected_asr_profile,
        state=evaluation.state,
        readiness=readiness,
        plan=SetupPlan(actions=actions),
    )


def stage_device(runtime_state: RuntimeProfileState, stage: str) -> str | None:
    for policy in runtime_state.stage_policies:
        if policy.stage == stage:
            return policy.device
    return None


def stage_provider_options(runtime_state: RuntimeProfileState, stage: str) -> dict[str, object]:
    for policy in runtime_state.stage_policies:
        if policy.stage == stage:
            return dict(policy.provider_options)
    return {}


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
    scope: RuntimeRevalidationScope | None,
) -> str | None:
    if not evaluation.ready:
        return "; ".join(evaluation.blocking_reasons)
    if scope is not None and not scope.requires_asr_selection_match:
        return None
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


def _fallback_profile(
    *,
    catalog: dict[tuple[str, str], SetupProfile],
    accepted: RuntimeProfileState,
    allow_runtime_fallback: bool,
) -> SetupProfile | None:
    authorization = accepted.fallback_authorization
    if (
        authorization.local_profile_id is not None
        and authorization.local_profile_version is not None
    ):
        fallback = catalog.get(
            (authorization.local_profile_id, authorization.local_profile_version)
        )
        if fallback is None:
            raise ValueError(
                "Accepted fallback profile state is incompatible with the bundled profile catalog; "
                "run `mena setup` again."
            )
        return fallback
    if allow_runtime_fallback:
        return next((profile for profile in catalog.values() if profile.fallback), None)
    return None


def _automatic_local_fallback_authorized(
    accepted: RuntimeProfileState,
    scope: RuntimeRevalidationScope | None,
) -> bool:
    authorization = accepted.fallback_authorization
    if (
        authorization.local_status != "authorized"
        or not authorization.automatic_local_fallback_authorized
    ):
        return False
    if scope is None:
        return True
    return scope.stages.issubset(set(authorization.stage_scope))
