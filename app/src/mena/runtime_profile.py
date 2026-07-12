"""Runtime profile selection, provider construction, and provenance."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, cast

from mena.artifacts import ArtifactRecord, FileSystemArtifactStore
from mena.diarization import (
    DIARIZATION_STAGE,
    DiarizationProvider,
    DiarizationProviderUnavailable,
    PyannoteCommunityDiarizationProvider,
)
from mena.local_llm import OLLAMA_DEFAULT_MODEL
from mena.mistral_llm import (
    MistralSummaryState,
    check_mistral_summary_readiness,
    load_mena_mistral_api_key,
)
from mena.mistral_summarization import MistralSummarizationProvider
from mena.ollama_summarization import OllamaSummarizationProvider
from mena.provider_validation import require_provider, require_provider_instance
from mena.runtime_stewardship import (
    RuntimeReleaseResult,
    record_terminal_runtime_release,
)
from mena.semantic_coherence import OllamaSemanticCoherenceProvider, SemanticCoherenceProvider
from mena.settings import AppSettings
from mena.setup import (
    MISTRAL_STAGE_PROVIDER,
    OLLAMA_STAGE_PROVIDER,
    CommandIdentity,
    CommandRunner,
    HardwareFacts,
    MistralSummaryProbe,
    RuntimeProfileState,
    SetupEvaluation,
    SetupPlan,
    SetupProfile,
    StageProfile,
    SubprocessCommandRunner,
    build_runtime_profile_state,
    default_profile_catalog,
    detect_local_installation_state,
    evaluate_setup,
    load_runtime_profile_state,
    local_llm_stage_model,
)
from mena.stage_policy import stage_device as lookup_stage_device
from mena.stage_policy import stage_model as lookup_stage_model
from mena.summarization import SummarizationProvider, SummarizationProviderUnavailable
from mena.transcription import (
    TRANSCRIPTION_STAGE,
    FasterWhisperTranscriptionProvider,
    TranscriptionProvider,
    TranscriptionProviderUnavailable,
)

ProviderFactory = Callable[[], Any]
ProviderFactoryInput = ProviderFactory | None
RuntimeProfileStore = Callable[..., Any]
ProcessingRuntimeRunner = Callable[["BoundProcessingRuntime"], Any]
SummarizationRuntimeRunner = Callable[[SummarizationProvider, int], Any]
FailedInputArtifactsResolver = Callable[[str], tuple[str, ...]]
TerminalRuntimeRelease = Callable[..., RuntimeReleaseResult | None]
RuntimeReleaseObserver = Callable[[RuntimeReleaseResult], None]
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
class SummarizationRuntimePlan:
    """Resolved summarization provider decision for one run (ADR-033).

    ``accepted_stage`` is the accepted primary from ``stage_policies``;
    ``actual_stage`` is the provider actually bound for this run — the primary,
    or one entry from the accepted ordered fallback list. ``resolved_model``
    carries readiness-time model-resolution evidence for remote providers.
    """

    accepted_stage: StageProfile
    actual_stage: StageProfile
    ordered_fallback: tuple[StageProfile, ...] = ()
    resolved_model: str | None = None


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
    summarization: SummarizationRuntimePlan | None = None


@dataclass(frozen=True)
class ResolvedSummarizationFallback:
    plan: SummarizationRuntimePlan
    authorization_source: str
    actual: RuntimeProfileState | None = None
    revalidation: SetupEvaluation | None = None


@dataclass(frozen=True)
class BoundProcessingRuntime:
    selection: RuntimeProfileSelection
    transcription_provider: TranscriptionProvider
    diarization_provider: DiarizationProvider
    semantic_coherence_provider: SemanticCoherenceProvider


@dataclass(frozen=True)
class ProcessingRuntimeRunResult:
    value: Any
    runtime: BoundProcessingRuntime


@dataclass(frozen=True)
class SummarizationRuntimeRunResult:
    value: Any
    selection: RuntimeProfileSelection
    provider: SummarizationProvider
    release_results: tuple[RuntimeReleaseResult, ...] = ()


class RuntimeFallbackUnavailable(ValueError):
    """Raised when a runtime stage failed and no authorized fallback can run.

    A ``ValueError`` subclass so existing ``except ValueError`` handlers keep
    catching it, while command handlers can catch it specifically to signal a
    recoverable "authorize the fallback and retry" outcome with a distinct exit
    code.
    """


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

    accepted = load_runtime_profile_state(settings.paths)
    scope = _runtime_revalidation_scope(required_stages, accepted.stage_policies)
    catalog = {
        (profile.id, profile.version): profile
        for profile in default_profile_catalog(_accepted_local_llm_model(accepted))
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

    fallback_runtime_state, fallback_evaluation, authorization_source = (
        _resolve_authorized_fallback(
            settings=settings,
            runner=command_runner,
            command_identity=command_identity,
            hardware=hardware,
            accepted=accepted,
            scope=scope,
            catalog=catalog,
            allow_runtime_fallback=allow_runtime_fallback,
            authorize_runtime_fallback=authorize_runtime_fallback,
            authorize_argument=accepted_failure,
            error_type=ValueError,
            no_profile_message=(
                "Accepted runtime profile is unavailable and no accepted fallback profile "
                f"is configured: {accepted_failure}"
            ),
            not_viable_prefix=(
                "Accepted runtime profile is unavailable and fallback is not currently "
                f"viable: {accepted_failure}"
            ),
            unauthorized_message=(
                "Accepted runtime profile is unavailable: "
                f"{accepted_failure} Re-run setup to authorize local fallback or pass "
                "--allow-runtime-fallback for an explicit one-run fallback when supported."
            ),
            declined_message=("Accepted runtime profile is unavailable and fallback was declined."),
        )
    )
    return RuntimeProfileSelection(
        accepted=accepted,
        actual=fallback_runtime_state,
        fallback_used=True,
        current_command_identity=fallback_evaluation.state.command_identity,
        revalidation=fallback_evaluation,
        fallback_reason=accepted_failure,
        fallback_status="used",
        fallback_authorization_source=authorization_source,
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
    revalidation_stages: frozenset[str] | None = None,
) -> RuntimeProfileSelection:
    """Select the accepted fallback profile after a provider-time stage failure.

    ``revalidation_stages`` controls which stages are re-probed and carried into
    the resulting runtime state. A CUDA resource failure in one GPU stage means
    the shared GPU is unreliable, so callers pass every GPU-coupled stage (not
    just ``failed_stage``) to keep all rebuilt providers configured from the
    fallback profile rather than from a default. Defaults to the failed stage.
    """

    scope = _runtime_revalidation_scope(
        revalidation_stages or frozenset({failed_stage}),
        accepted.stage_policies,
    )
    catalog = {
        (profile.id, profile.version): profile
        for profile in default_profile_catalog(_accepted_local_llm_model(accepted))
    }
    fallback_runtime_state, fallback_evaluation, authorization_source = (
        _resolve_authorized_fallback(
            settings=settings,
            runner=runner,
            command_identity=command_identity,
            hardware=hardware,
            accepted=accepted,
            scope=scope,
            catalog=catalog,
            allow_runtime_fallback=allow_runtime_fallback,
            authorize_runtime_fallback=authorize_runtime_fallback,
            authorize_argument=failure_reason,
            error_type=RuntimeFallbackUnavailable,
            no_profile_message=(
                f"Runtime stage '{failed_stage}' failed and no accepted fallback profile "
                f"is configured: {failure_reason}"
            ),
            not_viable_prefix=(
                f"Runtime stage '{failed_stage}' failed and fallback is not currently viable: "
                f"{failure_reason}"
            ),
            unauthorized_message=(
                f"Runtime stage '{failed_stage}' failed: {failure_reason} Re-run setup to "
                "authorize local fallback or pass --allow-runtime-fallback for an explicit "
                "one-run fallback when supported."
            ),
            declined_message=(f"Runtime stage '{failed_stage}' failed and fallback was declined."),
        )
    )
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


AuthorizeRemoteSummarizationFallback = Callable[[str, MistralSummaryState], bool]


def select_summarization_runtime_profile(
    *,
    settings: AppSettings,
    runner: CommandRunner | None,
    command_identity: CommandIdentity | None,
    hardware: HardwareFacts | None,
    allow_runtime_fallback: bool,
    authorize_runtime_fallback: Callable[[str, SetupEvaluation], bool] | None = None,
    authorize_remote_summarization_fallback: AuthorizeRemoteSummarizationFallback | None = None,
    env: Mapping[str, str] | None = None,
    mistral_probe: MistralSummaryProbe | None = None,
) -> RuntimeProfileSelection:
    """Select the summarization runtime: accepted primary, then the ordered fallback list.

    Runtime consumes only the accepted primary from ``stage_policies`` plus the
    accepted ordered ``summarization_fallback_providers`` list; it never invents
    fallback from provider identity (ADR-033). Using the accepted primary never
    prompts. A fallback transition prompts only when it was not pre-authorized
    by setup, and local fallback authorization never authorizes a remote
    transition.
    """

    accepted = load_runtime_profile_state(settings.paths)
    accepted_stage = _summarization_stage_policy(accepted)
    ordered = accepted.summarization_fallback_providers

    if accepted_stage.provider != MISTRAL_STAGE_PROVIDER and not ordered:
        # Local primary with an empty fallback list: the pre-#29 shape and
        # behavior, including the legacy scoped local-profile fallback ladder.
        selection = select_runtime_profile(
            settings=settings,
            runner=runner,
            command_identity=command_identity,
            hardware=hardware,
            allow_runtime_fallback=allow_runtime_fallback,
            authorize_runtime_fallback=authorize_runtime_fallback,
            required_stages=SUMMARIZATION_RUNTIME_STAGES,
        )
        actual_stage = _summarization_stage_policy(selection.actual)
        return replace(
            selection,
            summarization=SummarizationRuntimePlan(
                accepted_stage=accepted_stage,
                actual_stage=actual_stage,
                ordered_fallback=(),
            ),
        )

    scope = _runtime_revalidation_scope(SUMMARIZATION_RUNTIME_STAGES, accepted.stage_policies)
    evaluation = _scoped_accepted_evaluation(
        settings=settings,
        runner=runner,
        command_identity=command_identity,
        hardware=hardware,
        accepted=accepted,
        scope=scope,
        mistral_probe=mistral_probe,
    )
    accepted_failure = _runtime_profile_failure(accepted, evaluation, scope)
    if accepted_failure is None:
        return RuntimeProfileSelection(
            accepted=accepted,
            actual=build_runtime_profile_state(evaluation),
            fallback_used=False,
            current_command_identity=evaluation.state.command_identity,
            revalidation=evaluation,
            summarization=SummarizationRuntimePlan(
                accepted_stage=accepted_stage,
                actual_stage=accepted_stage,
                ordered_fallback=ordered,
                resolved_model=_mistral_resolved_model(evaluation),
            ),
        )

    if not _summarization_provider_revalidation_failed(evaluation, accepted_stage):
        raise RuntimeFallbackUnavailable(
            "Accepted summarization runtime setup guard failed; re-run setup before "
            f"using an accepted remote fallback: {accepted_failure}"
        )

    fallback = _resolve_summarization_fallback(
        settings=settings,
        runner=runner,
        command_identity=command_identity,
        hardware=hardware,
        accepted=accepted,
        accepted_stage=accepted_stage,
        ordered=ordered,
        failure_reason=accepted_failure,
        authorize_remote_summarization_fallback=authorize_remote_summarization_fallback,
        env=env,
        mistral_probe=mistral_probe,
    )
    return RuntimeProfileSelection(
        accepted=accepted,
        actual=fallback.actual or accepted,
        fallback_used=True,
        current_command_identity=(
            fallback.revalidation.state.command_identity
            if fallback.revalidation is not None
            else evaluation.state.command_identity
        ),
        revalidation=fallback.revalidation or evaluation,
        fallback_reason=accepted_failure,
        fallback_status="used",
        fallback_authorization_source=fallback.authorization_source,
        summarization=fallback.plan,
    )


def select_summarization_runtime_after_stage_failure(
    *,
    settings: AppSettings,
    runner: CommandRunner | None,
    command_identity: CommandIdentity | None,
    hardware: HardwareFacts | None,
    accepted: RuntimeProfileState,
    used_plan: SummarizationRuntimePlan,
    failure_reason: str,
    failure_class: str,
    failed_input_artifacts: tuple[str, ...] = (),
    authorize_remote_summarization_fallback: AuthorizeRemoteSummarizationFallback | None = None,
    env: Mapping[str, str] | None = None,
    mistral_probe: MistralSummaryProbe | None = None,
) -> RuntimeProfileSelection:
    """Select the next accepted summarization fallback after a provider-time failure."""

    remaining = _remaining_summarization_fallback(used_plan)
    if not remaining:
        raise RuntimeFallbackUnavailable(
            "Summarization stage failed and no further accepted fallback provider "
            f"is configured: {failure_reason}"
        )
    scope = _runtime_revalidation_scope(SUMMARIZATION_RUNTIME_STAGES, accepted.stage_policies)
    evaluation = _scoped_accepted_evaluation(
        settings=settings,
        runner=runner,
        command_identity=command_identity,
        hardware=hardware,
        accepted=accepted,
        scope=scope,
        mistral_probe=mistral_probe,
    )
    fallback = _resolve_summarization_fallback(
        settings=settings,
        runner=runner,
        command_identity=command_identity,
        hardware=hardware,
        accepted=accepted,
        accepted_stage=used_plan.accepted_stage,
        ordered=remaining,
        failure_reason=failure_reason,
        authorize_remote_summarization_fallback=authorize_remote_summarization_fallback,
        env=env,
        mistral_probe=mistral_probe,
    )
    plan = replace(fallback.plan, ordered_fallback=used_plan.ordered_fallback)
    return RuntimeProfileSelection(
        accepted=accepted,
        actual=fallback.actual or accepted,
        fallback_used=True,
        current_command_identity=(
            fallback.revalidation.state.command_identity
            if fallback.revalidation is not None
            else evaluation.state.command_identity
        ),
        revalidation=fallback.revalidation or evaluation,
        fallback_reason=failure_reason,
        fallback_status="used_after_stage_failure",
        fallback_failed_stage="summarization",
        fallback_failed_input_artifacts=failed_input_artifacts,
        fallback_failure_class=failure_class,
        fallback_authorization_source=fallback.authorization_source,
        summarization=plan,
    )


def _resolve_summarization_fallback(
    *,
    settings: AppSettings,
    runner: CommandRunner | None,
    command_identity: CommandIdentity | None,
    hardware: HardwareFacts | None,
    accepted: RuntimeProfileState,
    accepted_stage: StageProfile,
    ordered: tuple[StageProfile, ...],
    failure_reason: str,
    authorize_remote_summarization_fallback: AuthorizeRemoteSummarizationFallback | None,
    env: Mapping[str, str] | None,
    mistral_probe: MistralSummaryProbe | None,
) -> ResolvedSummarizationFallback:
    """Resolve the first viable, authorized entry of the accepted ordered list.

    Raises ``RuntimeFallbackUnavailable`` with every per-entry reason when no
    entry can run. ``--allow-runtime-fallback`` style one-run authorization is
    deliberately not consulted here: it satisfies the local fallback contract
    (ADR-031) and never authorizes a remote provider transition.
    """

    if not ordered:
        raise RuntimeFallbackUnavailable(
            "Accepted summarization provider is unavailable and the accepted ordered "
            f"fallback-provider list is empty: {failure_reason}"
        )
    probe = mistral_probe or check_mistral_summary_readiness
    probe_env: Mapping[str, str] = os.environ if env is None else env
    reasons: list[str] = []
    for entry in ordered:
        if entry.provider == OLLAMA_STAGE_PROVIDER:
            local = _resolve_local_summarization_fallback(
                settings=settings,
                runner=runner,
                command_identity=command_identity,
                hardware=hardware,
                accepted=accepted,
                accepted_stage=accepted_stage,
                entry=entry,
                ordered=ordered,
            )
            if isinstance(local, ResolvedSummarizationFallback):
                return local
            reasons.append(local)
            continue
        if entry.provider != MISTRAL_STAGE_PROVIDER:
            reasons.append(
                f"accepted fallback provider {entry.provider!r} is not supported by this runtime"
            )
            continue
        state = probe(env=probe_env)
        if not (state.key_present and state.reachable and state.model_allowed):
            reasons.append(f"Mistral fallback is not viable: {state.detail}")
            continue
        authorization = accepted.fallback_authorization
        if (
            authorization.remote_status == "authorized"
            and "summarization" in authorization.remote_stage_scope
        ):
            source = "setup_authorized"
        elif authorize_remote_summarization_fallback is not None and (
            authorize_remote_summarization_fallback(failure_reason, state)
        ):
            source = "interactive"
        else:
            reasons.append(
                "Mistral fallback transition is not pre-authorized; re-run setup to "
                "accept Mistral summarization fallback or confirm interactively"
            )
            continue
        return ResolvedSummarizationFallback(
            plan=SummarizationRuntimePlan(
                accepted_stage=accepted_stage,
                actual_stage=entry,
                ordered_fallback=ordered,
                resolved_model=state.resolved_model,
            ),
            authorization_source=source,
        )
    raise RuntimeFallbackUnavailable(
        "Accepted summarization provider is unavailable and no accepted fallback "
        f"provider can run: {failure_reason} Fallback blockers: {'; '.join(reasons)}"
    )


def _resolve_local_summarization_fallback(
    *,
    settings: AppSettings,
    runner: CommandRunner | None,
    command_identity: CommandIdentity | None,
    hardware: HardwareFacts | None,
    accepted: RuntimeProfileState,
    accepted_stage: StageProfile,
    entry: StageProfile,
    ordered: tuple[StageProfile, ...],
) -> ResolvedSummarizationFallback | str:
    catalog = default_profile_catalog(_accepted_local_llm_model(accepted))
    local_profile = next(
        (
            profile
            for profile in catalog
            if _summarization_provider(profile.stages) == OLLAMA_STAGE_PROVIDER
            and not profile.fallback
        ),
        None,
    )
    if local_profile is None:
        return "Ollama fallback is not supported by this runtime"
    scope = _runtime_revalidation_scope(SUMMARIZATION_RUNTIME_STAGES, (entry,))
    command_runner = runner or SubprocessCommandRunner()
    detection_profile = _profile_for_scope(local_profile, scope)
    state = detect_local_installation_state(
        settings,
        runner=command_runner,
        command_identity=command_identity,
        hardware=hardware,
        profile=detection_profile,
    )
    evaluation = _scope_evaluation(evaluate_setup(detection_profile, state), scope)
    if not evaluation.ready:
        return f"Ollama fallback is not viable: {'; '.join(evaluation.blocking_reasons)}"
    if not _automatic_local_fallback_authorized(accepted, scope):
        return (
            "Ollama fallback transition is not pre-authorized; re-run setup to accept "
            "local summarization fallback"
        )
    return ResolvedSummarizationFallback(
        plan=SummarizationRuntimePlan(
            accepted_stage=accepted_stage,
            actual_stage=entry,
            ordered_fallback=ordered,
        ),
        authorization_source="setup_authorized",
        actual=build_runtime_profile_state(evaluation),
        revalidation=evaluation,
    )


def _remaining_summarization_fallback(
    plan: SummarizationRuntimePlan,
) -> tuple[StageProfile, ...]:
    if plan.actual_stage == plan.accepted_stage:
        return plan.ordered_fallback
    for index, entry in enumerate(plan.ordered_fallback):
        if entry == plan.actual_stage:
            return plan.ordered_fallback[index + 1 :]
    return ()


def _scoped_accepted_evaluation(
    *,
    settings: AppSettings,
    runner: CommandRunner | None,
    command_identity: CommandIdentity | None,
    hardware: HardwareFacts | None,
    accepted: RuntimeProfileState,
    scope: RuntimeRevalidationScope | None,
    mistral_probe: MistralSummaryProbe | None = None,
) -> SetupEvaluation:
    catalog = {
        (profile.id, profile.version): profile
        for profile in default_profile_catalog(_accepted_local_llm_model(accepted))
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
    detection_profile = _profile_for_scope(accepted_profile, scope)
    state = detect_local_installation_state(
        settings,
        runner=command_runner,
        command_identity=command_identity,
        hardware=hardware,
        profile=detection_profile,
        mistral_probe=mistral_probe,
    )
    return _scope_evaluation(evaluate_setup(detection_profile, state), scope)


def _summarization_stage_policy(state: RuntimeProfileState) -> StageProfile:
    for policy in state.stage_policies:
        if policy.stage == "summarization":
            return policy
    raise ValueError(
        f"Accepted runtime profile {state.accepted_profile_id!r} does not define "
        "a summarization stage."
    )


def _summarization_provider(stage_policies: tuple[StageProfile, ...]) -> str:
    for policy in stage_policies:
        if policy.stage == "summarization":
            return policy.provider
    return OLLAMA_STAGE_PROVIDER


def _accepted_local_llm_model(accepted: RuntimeProfileState) -> str:
    return local_llm_stage_model(accepted.stage_policies, OLLAMA_DEFAULT_MODEL)


def _mistral_resolved_model(evaluation: SetupEvaluation) -> str | None:
    mistral = evaluation.state.mistral_summary
    return None if mistral is None else mistral.resolved_model


def _summarization_provider_revalidation_failed(
    evaluation: SetupEvaluation,
    accepted_stage: StageProfile,
) -> bool:
    failed_ids = {item.id for item in evaluation.readiness if not item.ready}
    if not failed_ids:
        return False
    if accepted_stage.provider == OLLAMA_STAGE_PROVIDER:
        return failed_ids <= {"command:ollama", "ollama-model"}
    if accepted_stage.provider == MISTRAL_STAGE_PROVIDER:
        return failed_ids <= {"mistral-summary-model"}
    return False


def resolve_processing_runtime(
    *,
    settings: AppSettings,
    store: FileSystemArtifactStore,
    run_id: str,
    attempt: int,
    transcription_provider_factory: ProviderFactoryInput = None,
    diarization_provider_factory: ProviderFactoryInput = None,
    semantic_coherence_provider_factory: ProviderFactoryInput = None,
    runner: CommandRunner | None,
    command_identity: CommandIdentity | None,
    hardware: HardwareFacts | None,
    allow_runtime_fallback: bool,
    authorize_runtime_fallback: Callable[[str, SetupEvaluation], bool] | None = None,
    runtime_selection_fn: Callable[..., RuntimeProfileSelection] = select_runtime_profile,
    runtime_profile_store_fn: RuntimeProfileStore | None = None,
) -> BoundProcessingRuntime:
    """Resolve and bind production processing providers for the accepted runtime profile."""

    if runtime_profile_store_fn is None:
        runtime_profile_store_fn = store_runtime_profile_provenance
    selection = runtime_selection_fn(
        settings=settings,
        runner=runner,
        command_identity=command_identity,
        hardware=hardware,
        allow_runtime_fallback=allow_runtime_fallback,
        authorize_runtime_fallback=authorize_runtime_fallback,
        required_stages=PROCESSING_REQUIRED_RUNTIME_STAGES,
    )
    runtime_profile_store_fn(
        store=store,
        run_id=run_id,
        attempt=attempt,
        selection=selection,
    )
    return bind_processing_runtime(
        selection=selection,
        settings=settings,
        transcription_provider_factory=transcription_provider_factory,
        diarization_provider_factory=diarization_provider_factory,
        semantic_coherence_provider_factory=semantic_coherence_provider_factory,
    )


def bind_processing_runtime(
    *,
    selection: RuntimeProfileSelection,
    settings: AppSettings,
    transcription_provider_factory: ProviderFactoryInput = None,
    diarization_provider_factory: ProviderFactoryInput = None,
    semantic_coherence_provider_factory: ProviderFactoryInput = None,
    semantic_coherence_provider: SemanticCoherenceProvider | None = None,
) -> BoundProcessingRuntime:
    """Construct providers bound to a selected runtime profile."""

    transcription_provider = require_provider(
        runtime_transcription_factory(
            transcription_provider_factory,
            selection.actual,
        ),
        TranscriptionProvider,
        "transcription",
    )
    diarization_provider = require_provider(
        runtime_diarization_factory(
            diarization_provider_factory,
            selection.actual,
        ),
        DiarizationProvider,
        "diarization",
    )
    semantic_provider = (
        semantic_coherence_provider
        if semantic_coherence_provider is not None
        else require_provider(
            runtime_semantic_factory(
                semantic_coherence_provider_factory,
                settings=settings,
                runtime_state=selection.accepted,
            ),
            SemanticCoherenceProvider,
            "semantic_coherence",
        )
    )
    semantic_provider = require_provider_instance(
        semantic_provider,
        SemanticCoherenceProvider,
        "semantic_coherence",
    )
    return BoundProcessingRuntime(
        selection=selection,
        transcription_provider=transcription_provider,
        diarization_provider=diarization_provider,
        semantic_coherence_provider=semantic_provider,
    )


def run_processing_with_runtime_fallback(
    *,
    runtime: BoundProcessingRuntime,
    run: ProcessingRuntimeRunner,
    settings: AppSettings,
    store: FileSystemArtifactStore,
    run_id: str,
    attempt: int,
    runner: CommandRunner | None,
    command_identity: CommandIdentity | None,
    hardware: HardwareFacts | None,
    allow_runtime_fallback: bool,
    authorize_runtime_fallback: Callable[[str, SetupEvaluation], bool] | None,
    transcription_provider_factory: ProviderFactoryInput = None,
    diarization_provider_factory: ProviderFactoryInput = None,
    semantic_coherence_provider_factory: ProviderFactoryInput = None,
    failed_input_artifacts: FailedInputArtifactsResolver,
    runtime_profile_selector_after_failure_fn: Callable[..., RuntimeProfileSelection] = (
        select_runtime_profile_after_stage_failure
    ),
    runtime_profile_store_fn: RuntimeProfileStore | None = None,
) -> ProcessingRuntimeRunResult:
    """Run processing once, with one resolver-owned fallback retry when policy allows it."""

    if runtime_profile_store_fn is None:
        runtime_profile_store_fn = store_runtime_profile_provenance
    try:
        return ProcessingRuntimeRunResult(value=run(runtime), runtime=runtime)
    except (TranscriptionProviderUnavailable, DiarizationProviderUnavailable) as exc:
        if runtime.selection.fallback_used:
            raise
        failed_stage = runtime_fallback_stage_for_provider_failure(exc)
        fallback_selection = runtime_profile_selector_after_failure_fn(
            settings=settings,
            runner=runner,
            command_identity=command_identity,
            hardware=hardware,
            accepted=runtime.selection.accepted,
            failed_stage=failed_stage,
            failure_reason=str(exc),
            failure_class=exc.__class__.__name__,
            failed_input_artifacts=failed_input_artifacts(failed_stage),
            allow_runtime_fallback=allow_runtime_fallback,
            authorize_runtime_fallback=authorize_runtime_fallback,
            revalidation_stages=PROCESSING_REQUIRED_RUNTIME_STAGES,
        )
        runtime_profile_store_fn(
            store=store,
            run_id=run_id,
            attempt=attempt,
            selection=fallback_selection,
            name_suffix="stage-fallback",
        )
        fallback_runtime = bind_processing_runtime(
            selection=fallback_selection,
            settings=settings,
            transcription_provider_factory=transcription_provider_factory,
            diarization_provider_factory=diarization_provider_factory,
            semantic_coherence_provider_factory=semantic_coherence_provider_factory,
            semantic_coherence_provider=runtime.semantic_coherence_provider,
        )
        return ProcessingRuntimeRunResult(value=run(fallback_runtime), runtime=fallback_runtime)


def run_summarization_with_runtime_fallback(
    *,
    settings: AppSettings,
    store: FileSystemArtifactStore,
    run_id: str,
    attempt: int,
    runner: CommandRunner | None,
    command_identity: CommandIdentity | None,
    hardware: HardwareFacts | None,
    allow_runtime_fallback: bool,
    authorize_remote_summarization_fallback: AuthorizeRemoteSummarizationFallback | None,
    run: SummarizationRuntimeRunner,
    failed_input_artifacts: tuple[str, ...],
    summarization_provider_factory: ProviderFactoryInput = None,
    env: Mapping[str, str] | None = None,
    mistral_probe: MistralSummaryProbe | None = None,
    runtime_profile_selector_fn: Callable[..., RuntimeProfileSelection] = (
        select_summarization_runtime_profile
    ),
    runtime_profile_selector_after_failure_fn: Callable[..., RuntimeProfileSelection] = (
        select_summarization_runtime_after_stage_failure
    ),
    runtime_profile_store_fn: RuntimeProfileStore | None = None,
    terminal_release_fn: TerminalRuntimeRelease = record_terminal_runtime_release,
    runtime_release_observer: RuntimeReleaseObserver | None = None,
) -> SummarizationRuntimeRunResult:
    """Run summarization with resolver-owned provider selection and fallback."""

    if runtime_profile_store_fn is None:
        runtime_profile_store_fn = store_runtime_profile_provenance
    release_results: list[RuntimeReleaseResult] = []
    selection = runtime_profile_selector_fn(
        settings=settings,
        runner=runner,
        command_identity=command_identity,
        hardware=hardware,
        allow_runtime_fallback=allow_runtime_fallback,
        authorize_remote_summarization_fallback=authorize_remote_summarization_fallback,
        env=env,
        mistral_probe=mistral_probe,
    )
    runtime_profile_store_fn(
        store=store,
        run_id=run_id,
        attempt=attempt,
        selection=selection,
    )
    provider = bind_summarization_runtime(
        selection=selection,
        settings=settings,
        summarization_provider_factory=summarization_provider_factory,
        env=env,
    )
    current_attempt = attempt
    released_attempts: set[int] = set()
    value: Any | None = None
    try:
        while True:
            try:
                value = run(provider, current_attempt)
                break
            except SummarizationProviderUnavailable as exc:
                used_plan = selection.summarization
                if used_plan is None:
                    raise
                fallback_selection = runtime_profile_selector_after_failure_fn(
                    settings=settings,
                    runner=runner,
                    command_identity=command_identity,
                    hardware=hardware,
                    accepted=selection.accepted,
                    used_plan=used_plan,
                    failure_reason=str(exc),
                    failure_class=exc.__class__.__name__,
                    failed_input_artifacts=failed_input_artifacts,
                    authorize_remote_summarization_fallback=(
                        authorize_remote_summarization_fallback
                    ),
                    env=env,
                    mistral_probe=mistral_probe,
                )
                fallback_attempt = current_attempt + 1
                runtime_profile_store_fn(
                    store=store,
                    run_id=run_id,
                    attempt=fallback_attempt,
                    selection=fallback_selection,
                    name_suffix="stage-fallback",
                )
                released = terminal_release_fn(
                    store=store,
                    run_id=run_id,
                    attempt=current_attempt,
                    provider=provider,
                    stage="summarization",
                    boundary="summarize",
                )
                released_attempts.add(current_attempt)
                if released is not None:
                    release_results.append(released)
                    if runtime_release_observer is not None:
                        runtime_release_observer(released)
                fallback_provider = bind_summarization_runtime(
                    selection=fallback_selection,
                    settings=settings,
                    summarization_provider_factory=summarization_provider_factory,
                    env=env,
                )
                provider = fallback_provider
                selection = fallback_selection
                current_attempt = fallback_attempt
    finally:
        if current_attempt not in released_attempts:
            released = terminal_release_fn(
                store=store,
                run_id=run_id,
                attempt=current_attempt,
                provider=provider,
                stage="summarization",
                boundary="summarize",
            )
            if released is not None:
                release_results.append(released)
                if runtime_release_observer is not None:
                    runtime_release_observer(released)
    return SummarizationRuntimeRunResult(
        value=value,
        selection=selection,
        provider=provider,
        release_results=tuple(release_results),
    )


def runtime_fallback_stage_for_provider_failure(
    exc: TranscriptionProviderUnavailable | DiarizationProviderUnavailable,
) -> str:
    if isinstance(exc, TranscriptionProviderUnavailable):
        return TRANSCRIPTION_STAGE
    return DIARIZATION_STAGE


def _resolve_authorized_fallback(
    *,
    settings: AppSettings,
    runner: CommandRunner | None,
    command_identity: CommandIdentity | None,
    hardware: HardwareFacts | None,
    accepted: RuntimeProfileState,
    scope: RuntimeRevalidationScope | None,
    catalog: dict[tuple[str, str], SetupProfile],
    allow_runtime_fallback: bool,
    authorize_runtime_fallback: Callable[[str, SetupEvaluation], bool] | None,
    authorize_argument: str,
    error_type: type[ValueError],
    no_profile_message: str,
    not_viable_prefix: str,
    unauthorized_message: str,
    declined_message: str,
) -> tuple[RuntimeProfileState, SetupEvaluation, str]:
    """Resolve, viability-check, and authorize a scoped CPU fallback profile.

    Shared by ``select_runtime_profile`` and ``select_runtime_profile_after_stage_failure``
    so the resolution and authorization ladder cannot drift between them. Raises
    ``error_type`` with the caller's message when no fallback is configured, the
    fallback is not currently viable, or authorization is missing/declined.
    Returns ``(fallback_runtime_state, fallback_evaluation, authorization_source)``.
    """

    fallback_profile = _fallback_profile(
        catalog=catalog,
        accepted=accepted,
        allow_runtime_fallback=allow_runtime_fallback,
    )
    if fallback_profile is None:
        raise error_type(no_profile_message)

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
        raise error_type(f"{not_viable_prefix} Fallback blockers: {reasons}")

    if allow_runtime_fallback:
        authorization_source = "explicit_one_run"
    elif _automatic_local_fallback_authorized(accepted, scope):
        authorization_source = "setup_authorized"
    elif authorize_runtime_fallback is None:
        raise error_type(unauthorized_message)
    elif authorize_runtime_fallback(authorize_argument, fallback_evaluation):
        authorization_source = "interactive"
    else:
        raise error_type(declined_message)

    fallback_runtime_state = build_runtime_profile_state(fallback_evaluation)
    return fallback_runtime_state, fallback_evaluation, authorization_source


def production_runtime_required(
    *,
    transcription_provider_factory: ProviderFactoryInput = None,
    diarization_provider_factory: ProviderFactoryInput = None,
    semantic_coherence_provider_factory: ProviderFactoryInput = None,
) -> bool:
    transcription_provider_factory = default_transcription_provider_factory(
        transcription_provider_factory
    )
    diarization_provider_factory = default_diarization_provider_factory(
        diarization_provider_factory
    )
    semantic_coherence_provider_factory = default_semantic_coherence_provider_factory(
        semantic_coherence_provider_factory
    )
    return (
        transcription_provider_factory is FasterWhisperTranscriptionProvider
        or diarization_provider_factory is PyannoteCommunityDiarizationProvider
        or semantic_coherence_provider_factory is OllamaSemanticCoherenceProvider
    )


def runtime_transcription_factory(
    factory: ProviderFactoryInput,
    runtime_state: RuntimeProfileState,
) -> ProviderFactory:
    factory = default_transcription_provider_factory(factory)
    if factory is not FasterWhisperTranscriptionProvider:
        return factory
    profile = runtime_state.selected_asr_profile

    def _factory() -> FasterWhisperTranscriptionProvider:
        return FasterWhisperTranscriptionProvider(profile=profile, local_files_only=True)

    return _factory


def runtime_diarization_factory(
    factory: ProviderFactoryInput,
    runtime_state: RuntimeProfileState,
) -> ProviderFactory:
    factory = default_diarization_provider_factory(factory)
    if factory is not PyannoteCommunityDiarizationProvider:
        return factory
    device = stage_device(runtime_state, "diarization") or "cpu"

    def _factory() -> PyannoteCommunityDiarizationProvider:
        return PyannoteCommunityDiarizationProvider(device=device)

    return _factory


def runtime_semantic_factory(
    factory: ProviderFactoryInput,
    *,
    settings: AppSettings,
    runtime_state: RuntimeProfileState,
) -> ProviderFactory:
    factory = default_semantic_coherence_provider_factory(factory)
    if factory is not OllamaSemanticCoherenceProvider:
        return factory
    model = _stage_model(runtime_state, "semantic-review")

    def _factory() -> OllamaSemanticCoherenceProvider:
        return OllamaSemanticCoherenceProvider(
            model=model,
            endpoint=settings.local_llm.ollama_endpoint,
        )

    return _factory


def bind_summarization_runtime(
    *,
    selection: RuntimeProfileSelection,
    settings: AppSettings,
    summarization_provider_factory: ProviderFactoryInput = None,
    env: Mapping[str, str] | None = None,
) -> SummarizationProvider:
    provider = runtime_summarization_provider(
        settings=settings,
        runtime_state=selection.actual,
        summarization_provider_factory=summarization_provider_factory,
        plan=selection.summarization,
        env=env,
    )
    return cast(
        SummarizationProvider,
        require_provider_instance(provider, SummarizationProvider, "summarization"),
    )


def runtime_summarization_provider(
    *,
    settings: AppSettings,
    runtime_state: RuntimeProfileState,
    summarization_provider_factory: ProviderFactoryInput = None,
    plan: SummarizationRuntimePlan | None = None,
    env: Mapping[str, str] | None = None,
) -> SummarizationProvider:
    factory = default_summarization_provider_factory(summarization_provider_factory)
    if factory is not OllamaSummarizationProvider:
        return cast(
            SummarizationProvider,
            require_provider(factory, SummarizationProvider, "summarization"),
        )
    stage = plan.actual_stage if plan is not None else None
    provider_name = (
        stage.provider
        if stage is not None
        else _summarization_provider(runtime_state.stage_policies)
    )
    if provider_name == MISTRAL_STAGE_PROVIDER:
        return _mistral_summarization_provider(plan=plan, env=env)
    return OllamaSummarizationProvider(
        model=stage.model if stage is not None else _stage_model(runtime_state, "summarization"),
        endpoint=settings.local_llm.ollama_endpoint,
    )


def _mistral_summarization_provider(
    *,
    plan: SummarizationRuntimePlan | None,
    env: Mapping[str, str] | None,
) -> SummarizationProvider:
    key_env: Mapping[str, str] = os.environ if env is None else env
    return MistralSummarizationProvider(
        api_key=load_mena_mistral_api_key(key_env),
        resolved_model=None if plan is None else plan.resolved_model,
    )


def default_transcription_provider_factory(factory: ProviderFactoryInput = None) -> ProviderFactory:
    return FasterWhisperTranscriptionProvider if factory is None else factory


def default_diarization_provider_factory(factory: ProviderFactoryInput = None) -> ProviderFactory:
    return PyannoteCommunityDiarizationProvider if factory is None else factory


def default_semantic_coherence_provider_factory(
    factory: ProviderFactoryInput = None,
) -> ProviderFactory:
    return OllamaSemanticCoherenceProvider if factory is None else factory


def default_summarization_provider_factory(factory: ProviderFactoryInput = None) -> ProviderFactory:
    return OllamaSummarizationProvider if factory is None else factory


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
    summarization_plan = cast(
        "SummarizationRuntimePlan | None",
        getattr(selection, "summarization", None),
    )
    if summarization_plan is not None:
        plan = summarization_plan
        payload["summarization"] = {
            "accepted_provider": plan.accepted_stage.provider,
            "accepted_model": plan.accepted_stage.model,
            "actual_provider": plan.actual_stage.provider,
            "actual_model": plan.actual_stage.model,
            "request_model": plan.actual_stage.model,
            "resolved_model": plan.resolved_model,
            "fallback_used": plan.actual_stage != plan.accepted_stage,
            "fallback_reason": selection.fallback_reason,
            "authorization_source": selection.fallback_authorization_source,
            "ordered_fallback_providers": [
                {
                    "stage": entry.stage,
                    "provider": entry.provider,
                    "model": entry.model,
                    "device": entry.device,
                    "policy": entry.policy,
                }
                for entry in plan.ordered_fallback
            ],
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


def _runtime_revalidation_scope(
    required_stages: frozenset[str] | None,
    stage_policies: tuple[StageProfile, ...] = (),
) -> RuntimeRevalidationScope | None:
    if required_stages is None:
        return None
    readiness_ids = {"command:mena"}
    command_names = ["mena"]
    python_surface_imports: set[str] = set()
    needs_ollama = "semantic-review" in required_stages
    if "summarization" in required_stages:
        if _summarization_provider(stage_policies) == MISTRAL_STAGE_PROVIDER:
            readiness_ids.add("mistral-summary-model")
        else:
            needs_ollama = True
    if needs_ollama:
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
    return lookup_stage_device(runtime_state.stage_policies, stage)


def _stage_model(runtime_state: RuntimeProfileState, stage: str) -> str:
    return lookup_stage_model(
        runtime_state.stage_policies,
        stage,
        f"Accepted runtime profile {runtime_state.accepted_profile_id!r}",
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
