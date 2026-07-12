from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from mena.artifacts import FileSystemArtifactStore
from mena.diarization import (
    DiarizationProvider,
    DiarizationProviderUnavailable,
    PyannoteCommunityDiarizationProvider,
)
from mena.mistral_llm import (
    MENA_MISTRAL_API_KEY_ENV,
    MISTRAL_SUMMARY_ALLOWLIST_POLICY,
    MISTRAL_SUMMARY_REQUEST_MODEL,
    MistralSummaryState,
)
from mena.mistral_summarization import MistralSummarizationProvider
from mena.provider_validation import ProviderValidationError
from mena.runtime_profile import (
    BoundProcessingRuntime,
    RuntimeFallbackUnavailable,
    RuntimeProfileSelection,
    SummarizationRuntimePlan,
    bind_processing_runtime,
    bind_summarization_runtime,
    run_processing_with_runtime_fallback,
    run_summarization_with_runtime_fallback,
    runtime_diarization_factory,
    runtime_fallback_stage_for_provider_failure,
    runtime_semantic_factory,
    runtime_stage_policy_payload,
    runtime_transcription_factory,
    select_runtime_profile,
    select_runtime_profile_after_stage_failure,
    select_summarization_runtime_after_stage_failure,
    select_summarization_runtime_profile,
    store_runtime_profile_provenance,
)
from mena.runtime_stewardship import RuntimeLease, RuntimeReleaseResult
from mena.semantic_coherence import OllamaSemanticCoherenceProvider, SemanticCoherenceProvider
from mena.settings import AppSettings
from mena.setup import (
    CommandCheck,
    CommandIdentity,
    HardwareFacts,
    LocalInstallationState,
    OllamaState,
    PythonSurfaceCheck,
    RuntimeFallbackAuthorization,
    RuntimeProfileState,
    SetupEvaluation,
    StageProfile,
    build_runtime_profile_state,
    default_profile_catalog,
    evaluate_setup,
    get_alpha_local_profile,
    get_mistral_summary_profile,
    mistral_summary_stage_profile,
    persist_runtime_profile_state,
)
from mena.summarization import SummarizationError, SummarizationProviderUnavailable
from mena.transcription import (
    FasterWhisperModelProfile,
    FasterWhisperTranscriptionProvider,
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
            StageProfile(
                stage="summarization",
                provider="ollama",
                model="llama3.2:3b",
                device="cpu",
                policy="local-only",
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


def _setup_state(
    *,
    settings: AppSettings,
    command_identity: CommandIdentity | None = None,
    hardware: HardwareFacts | None = None,
    ollama_model_present: bool = True,
    asr_model_present: bool | None = True,
    diarization_model_present: bool | None = True,
    pyannote_token_present: bool = False,
    python_surfaces: dict[str, bool] | None = None,
) -> LocalInstallationState:
    profile = get_alpha_local_profile(settings.local_llm.ollama_model)
    surface_presence = python_surfaces or {
        "faster_whisper": True,
        "pyannote.audio": True,
    }
    return LocalInstallationState(
        command_identity=command_identity
        or CommandIdentity(
            current_executable=Path("/home/user/.local/bin/mena"),
            plain_mena_path=Path("/home/user/.local/bin/mena"),
            version="0.1.0",
            invocation_mode="installed-product",
        ),
        runtime_paths=settings.paths,
        hardware=hardware
        or HardwareFacts(
            system="Linux",
            ram_gb=32.0,
            cuda_available=True,
            vram_gb=12.0,
            avx2_available=True,
        ),
        commands=tuple(
            CommandCheck(name=command, path=Path("/usr/bin") / command)
            for command in profile.required_commands
        ),
        playback_commands=tuple(
            CommandCheck(
                name=command,
                path=Path("/usr/bin") / command if command == "ffplay" else None,
            )
            for command in profile.playback_commands
        ),
        python_surfaces=tuple(
            PythonSurfaceCheck(
                requirement=requirement,
                installed=surface_presence[requirement.import_name],
            )
            for requirement in profile.python_surfaces
        ),
        ollama=OllamaState(
            endpoint=settings.local_llm.ollama_endpoint,
            model=settings.local_llm.ollama_model,
            command=CommandCheck("ollama", Path("/usr/bin/ollama")),
            reachable=True,
            model_present=ollama_model_present,
            detail=(
                "model present"
                if ollama_model_present
                else f"model {settings.local_llm.ollama_model} is not listed by Ollama"
            ),
        ),
        asr_model_present=asr_model_present,
        diarization_model_present=diarization_model_present,
        pyannote_token_present=pyannote_token_present,
        python_executable=Path("/usr/bin/python3"),
    )


def _runtime_profile_state_with_evaluated_fallback(
    *,
    settings: AppSettings,
    setup_state: LocalInstallationState,
) -> RuntimeProfileState:
    primary_profile = get_alpha_local_profile(settings.local_llm.ollama_model)
    fallback_profile = default_profile_catalog(settings.local_llm.ollama_model)[1]
    return build_runtime_profile_state(
        evaluate_setup(primary_profile, setup_state),
        fallback_evaluation=evaluate_setup(fallback_profile, setup_state),
        authorize_local_fallback=True,
    )


def _stage_failure_accepted_state(
    *,
    settings: AppSettings,
    setup_state: LocalInstallationState,
    authorize_local_fallback: bool,
) -> RuntimeProfileState:
    primary_profile, fallback_profile, *_ = default_profile_catalog(settings.local_llm.ollama_model)
    return build_runtime_profile_state(
        evaluate_setup(primary_profile, setup_state),
        fallback_evaluation=evaluate_setup(fallback_profile, setup_state),
        authorize_local_fallback=authorize_local_fallback,
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


class _Summarization:
    def __init__(self, **_kwargs: object) -> None:
        self.metadata = SimpleNamespace(config_id="summary", to_dict=lambda: {})

    def summarize(self, _request: object) -> object:
        raise AssertionError("not called")


class _ReleasableSummarization(_Summarization):
    def __init__(self, label: str) -> None:
        self.label = label
        self.runtime_resource_used = True
        self.metadata = SimpleNamespace(
            name="Ollama",
            model="llama3.1:8b",
            config_id=label,
            to_dict=lambda: {},
        )

    def release_runtime_resource(self, lease: RuntimeLease) -> RuntimeReleaseResult:
        return RuntimeReleaseResult(
            lease=lease,
            status="released",
            detail=f"{self.label} released",
        )


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


def test_runtime_provider_factories_use_selected_profile_state(tmp_path: Path) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    setup_state = _setup_state(settings=settings, pyannote_token_present=True)
    evaluation = evaluate_setup(get_alpha_local_profile(), setup_state)
    runtime_state = build_runtime_profile_state(evaluation)

    transcription_provider = runtime_transcription_factory(
        FasterWhisperTranscriptionProvider,
        runtime_state,
    )()
    diarization_provider = runtime_diarization_factory(
        PyannoteCommunityDiarizationProvider,
        runtime_state,
    )()
    semantic_provider = runtime_semantic_factory(
        OllamaSemanticCoherenceProvider,
        settings=settings,
        runtime_state=runtime_state,
    )()

    assert transcription_provider.profile.id == evaluation.selected_asr_profile.id
    assert transcription_provider.profile.device == "cuda"
    assert transcription_provider.local_files_only
    assert diarization_provider.device == "cuda"
    assert semantic_provider.metadata.model == settings.local_llm.ollama_model
    assert semantic_provider.metadata.endpoint == settings.local_llm.ollama_endpoint


def test_runtime_profile_selection_uses_accepted_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    setup_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = _runtime_profile_state_with_evaluated_fallback(
        settings=settings,
        setup_state=setup_state,
    )
    persist_runtime_profile_state(accepted, paths=settings.paths)
    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state",
        lambda *_args, **_kwargs: setup_state,
    )

    selection = select_runtime_profile(
        settings=settings,
        runner=None,
        command_identity=None,
        hardware=None,
        allow_runtime_fallback=False,
    )

    assert selection.accepted.accepted_profile_id == accepted.accepted_profile_id
    assert selection.actual.accepted_profile_id == accepted.accepted_profile_id
    assert not selection.fallback_used


def test_runtime_profile_selection_revalidates_accepted_model_after_config_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    setup_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted_profile = get_alpha_local_profile(settings.local_llm.ollama_model)
    accepted = build_runtime_profile_state(evaluate_setup(accepted_profile, setup_state))
    persist_runtime_profile_state(accepted, paths=settings.paths)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[ollama]
model = "mistral:7b"
""".strip(),
        encoding="utf-8",
    )
    drifted_settings = AppSettings.from_env(env={}, home=tmp_path, config_path=config_path)
    observed_models: list[str] = []

    def _detect_state(_settings: AppSettings, **kwargs: object) -> LocalInstallationState:
        profile = cast(Any, kwargs["profile"])
        model = next(stage.model for stage in profile.stages if stage.stage == "summarization")
        observed_models.append(model)
        return replace(
            _setup_state(settings=drifted_settings, pyannote_token_present=True),
            ollama=OllamaState(
                endpoint=drifted_settings.local_llm.ollama_endpoint,
                model=model,
                command=CommandCheck("ollama", Path("/usr/bin/ollama")),
                reachable=True,
                model_present=True,
                detail="model present",
            ),
        )

    monkeypatch.setattr("mena.runtime_profile.detect_local_installation_state", _detect_state)

    selection = select_runtime_profile(
        settings=drifted_settings,
        runner=None,
        command_identity=None,
        hardware=None,
        allow_runtime_fallback=False,
    )

    assert observed_models == [settings.local_llm.ollama_model]
    assert selection.actual.accepted_profile_id == accepted.accepted_profile_id


def test_runtime_profile_selection_uses_setup_authorized_local_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    accepted_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = _runtime_profile_state_with_evaluated_fallback(
        settings=settings,
        setup_state=accepted_state,
    )
    persist_runtime_profile_state(accepted, paths=settings.paths)
    stale_state = _setup_state(
        settings=settings,
        hardware=HardwareFacts(
            system="Linux",
            ram_gb=32.0,
            cuda_available=False,
            vram_gb=None,
            avx2_available=True,
        ),
        pyannote_token_present=True,
    )
    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state",
        lambda *_args, **_kwargs: stale_state,
    )

    selection = select_runtime_profile(
        settings=settings,
        runner=None,
        command_identity=None,
        hardware=None,
        allow_runtime_fallback=False,
    )

    assert selection.fallback_used
    assert selection.actual.accepted_profile_id.endswith("cpu-fallback-v1")
    assert selection.fallback_reason is not None


def test_runtime_profile_selection_requires_authorization_when_setup_did_not_authorize_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    accepted_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = build_runtime_profile_state(
        evaluate_setup(get_alpha_local_profile(), accepted_state)
    )
    payload = accepted.to_dict()
    cast(dict[str, object], payload["fallback_authorization"])["local_status"] = (
        "configured_unauthorized"
    )
    cast(dict[str, object], payload["fallback_authorization"])[
        "automatic_local_fallback_authorized"
    ] = False
    settings.paths.state_home.mkdir(parents=True)
    (settings.paths.state_home / "setup-state.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    stale_state = _setup_state(
        settings=settings,
        hardware=HardwareFacts(
            system="Linux",
            ram_gb=32.0,
            cuda_available=False,
            vram_gb=None,
            avx2_available=True,
        ),
        pyannote_token_present=True,
    )
    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state",
        lambda *_args, **_kwargs: stale_state,
    )

    with pytest.raises(ValueError, match="authorize local fallback"):
        select_runtime_profile(
            settings=settings,
            runner=None,
            command_identity=None,
            hardware=None,
            allow_runtime_fallback=False,
        )

    observed_consent: dict[str, object] = {}

    def _authorize_once(failure: str, evaluation: object) -> bool:
        observed_consent["failure"] = failure
        observed_consent["profile_id"] = cast(Any, evaluation).profile.id
        return True

    callback_selection = select_runtime_profile(
        settings=settings,
        runner=None,
        command_identity=None,
        hardware=None,
        allow_runtime_fallback=False,
        authorize_runtime_fallback=_authorize_once,
    )

    selection = select_runtime_profile(
        settings=settings,
        runner=None,
        command_identity=None,
        hardware=None,
        allow_runtime_fallback=True,
    )

    assert callback_selection.fallback_used
    assert callback_selection.actual.accepted_profile_id.endswith("cpu-fallback-v1")
    assert "current ASR runtime selection" in cast(str, observed_consent["failure"])
    assert observed_consent["profile_id"] == "linux-alpha-cpu-fallback-v1"
    assert selection.fallback_used
    assert selection.actual.accepted_profile_id.endswith("cpu-fallback-v1")


def test_runtime_profile_stage_failure_selection_allows_explicit_one_run_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    setup_state = _setup_state(settings=settings, pyannote_token_present=True)
    primary_profile, fallback_profile, *_ = default_profile_catalog(settings.local_llm.ollama_model)
    accepted = build_runtime_profile_state(
        evaluate_setup(primary_profile, setup_state),
        fallback_evaluation=evaluate_setup(fallback_profile, setup_state),
        authorize_local_fallback=False,
    )
    observed_profiles: list[tuple[str, tuple[str, ...]]] = []

    def _detect_state(_settings: AppSettings, **kwargs: object) -> LocalInstallationState:
        profile = cast(Any, kwargs["profile"])
        observed_profiles.append((profile.id, tuple(stage.stage for stage in profile.stages)))
        return setup_state

    monkeypatch.setattr("mena.runtime_profile.detect_local_installation_state", _detect_state)

    selection = select_runtime_profile_after_stage_failure(
        settings=settings,
        runner=None,
        command_identity=None,
        hardware=None,
        accepted=accepted,
        failed_stage="diarization",
        failure_reason="CUDA out of memory during pyannote diarization",
        failure_class="DiarizationProviderUnavailable",
        failed_input_artifacts=("session:artifacts/source_audio/system.wav",),
        allow_runtime_fallback=True,
    )

    assert observed_profiles == [("linux-alpha-cpu-fallback-v1", ("diarization",))]
    assert selection.fallback_used
    assert selection.fallback_status == "used_after_stage_failure"
    assert selection.fallback_authorization_source == "explicit_one_run"
    assert selection.fallback_failed_stage == "diarization"
    assert selection.fallback_failed_input_artifacts == (
        "session:artifacts/source_audio/system.wav",
    )
    assert selection.actual.accepted_profile_id == "linux-alpha-cpu-fallback-v1"


def test_runtime_profile_stage_failure_selection_rejects_unready_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    setup_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = _runtime_profile_state_with_evaluated_fallback(
        settings=settings,
        setup_state=setup_state,
    )
    missing_asr_state = _setup_state(
        settings=settings,
        asr_model_present=False,
        pyannote_token_present=True,
    )

    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state",
        lambda *_args, **_kwargs: missing_asr_state,
    )

    with pytest.raises(ValueError, match="fallback is not currently viable"):
        select_runtime_profile_after_stage_failure(
            settings=settings,
            runner=None,
            command_identity=None,
            hardware=None,
            accepted=accepted,
            failed_stage="transcription",
            failure_reason="CUDA out of memory during faster-whisper transcription",
            failure_class="TranscriptionProviderUnavailable",
            failed_input_artifacts=("session:artifacts/source_audio/local.wav",),
            allow_runtime_fallback=False,
        )


def test_runtime_profile_stage_failure_selection_uses_setup_authorized_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    setup_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = _stage_failure_accepted_state(
        settings=settings, setup_state=setup_state, authorize_local_fallback=True
    )
    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state",
        lambda *_args, **_kwargs: setup_state,
    )

    selection = select_runtime_profile_after_stage_failure(
        settings=settings,
        runner=None,
        command_identity=None,
        hardware=None,
        accepted=accepted,
        failed_stage="diarization",
        failure_reason="CUDA out of memory during pyannote diarization",
        failure_class="DiarizationProviderUnavailable",
        failed_input_artifacts=(),
        allow_runtime_fallback=False,
    )

    assert selection.fallback_authorization_source == "setup_authorized"
    assert selection.actual.accepted_profile_id == "linux-alpha-cpu-fallback-v1"


def test_runtime_profile_stage_failure_selection_accepts_interactive_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    setup_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = _stage_failure_accepted_state(
        settings=settings, setup_state=setup_state, authorize_local_fallback=False
    )
    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state",
        lambda *_args, **_kwargs: setup_state,
    )

    selection = select_runtime_profile_after_stage_failure(
        settings=settings,
        runner=None,
        command_identity=None,
        hardware=None,
        accepted=accepted,
        failed_stage="diarization",
        failure_reason="CUDA out of memory during pyannote diarization",
        failure_class="DiarizationProviderUnavailable",
        failed_input_artifacts=(),
        allow_runtime_fallback=False,
        authorize_runtime_fallback=lambda *_args: True,
    )

    assert selection.fallback_authorization_source == "interactive"


def test_runtime_profile_stage_failure_selection_raises_when_fallback_declined(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    setup_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = _stage_failure_accepted_state(
        settings=settings, setup_state=setup_state, authorize_local_fallback=False
    )
    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state",
        lambda *_args, **_kwargs: setup_state,
    )

    with pytest.raises(RuntimeFallbackUnavailable, match="fallback was declined"):
        select_runtime_profile_after_stage_failure(
            settings=settings,
            runner=None,
            command_identity=None,
            hardware=None,
            accepted=accepted,
            failed_stage="diarization",
            failure_reason="CUDA out of memory during pyannote diarization",
            failure_class="DiarizationProviderUnavailable",
            failed_input_artifacts=(),
            allow_runtime_fallback=False,
            authorize_runtime_fallback=lambda *_args: False,
        )


def test_runtime_profile_stage_failure_selection_raises_when_authorizer_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    setup_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = _stage_failure_accepted_state(
        settings=settings, setup_state=setup_state, authorize_local_fallback=False
    )
    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state",
        lambda *_args, **_kwargs: setup_state,
    )

    with pytest.raises(RuntimeFallbackUnavailable, match="Re-run setup"):
        select_runtime_profile_after_stage_failure(
            settings=settings,
            runner=None,
            command_identity=None,
            hardware=None,
            accepted=accepted,
            failed_stage="diarization",
            failure_reason="CUDA out of memory during pyannote diarization",
            failure_class="DiarizationProviderUnavailable",
            failed_input_artifacts=(),
            allow_runtime_fallback=False,
        )


def test_runtime_profile_selection_revalidates_fallback_against_fallback_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    accepted_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = build_runtime_profile_state(
        evaluate_setup(get_alpha_local_profile(), accepted_state)
    )
    persist_runtime_profile_state(accepted, paths=settings.paths)
    observed_profiles: list[str] = []

    def _detect_state(_settings: AppSettings, **kwargs: object) -> LocalInstallationState:
        profile = cast(Any, kwargs["profile"])
        observed_profiles.append(profile.id)
        return _setup_state(
            settings=settings,
            hardware=HardwareFacts(
                system="Linux",
                ram_gb=32.0,
                cuda_available=False,
                vram_gb=None,
                avx2_available=True,
            ),
            pyannote_token_present=True,
        )

    monkeypatch.setattr("mena.runtime_profile.detect_local_installation_state", _detect_state)

    selection = select_runtime_profile(
        settings=settings,
        runner=None,
        command_identity=None,
        hardware=None,
        allow_runtime_fallback=True,
    )

    assert observed_profiles == ["linux-alpha-local-v1", "linux-alpha-cpu-fallback-v1"]
    assert selection.fallback_used
    assert selection.actual.selected_asr_profile.device == "cpu"


def test_runtime_profile_selection_rejects_stale_catalog_state(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    setup_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = _runtime_profile_state_with_evaluated_fallback(
        settings=settings,
        setup_state=setup_state,
    )
    payload = accepted.to_dict()
    payload["catalog_version"] = "stale-catalog"
    settings.paths.state_home.mkdir(parents=True)
    (settings.paths.state_home / "setup-state.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="incompatible"):
        select_runtime_profile(
            settings=settings,
            runner=None,
            command_identity=None,
            hardware=None,
            allow_runtime_fallback=False,
        )


def test_runtime_profile_provenance_records_accepted_actual_and_fallback(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    setup_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = _runtime_profile_state_with_evaluated_fallback(
        settings=settings,
        setup_state=setup_state,
    )
    selection = SimpleNamespace(
        accepted=accepted,
        actual=accepted,
        current_command_identity=setup_state.command_identity,
        fallback_used=False,
        fallback_reason=None,
        fallback_status="not_needed",
        revalidation=evaluate_setup(get_alpha_local_profile(), setup_state),
    )
    store = FileSystemArtifactStore(session_id="session", session_dir=tmp_path)

    record = store_runtime_profile_provenance(
        store=store,
        run_id="run-001",
        attempt=3,
        selection=cast(Any, selection),
    )
    payload = json.loads(store.retrieve(record.id))

    assert payload["attempt"] == 3
    assert payload["accepted_profile"]["id"] == accepted.accepted_profile_id
    assert payload["actual_profile"]["id"] == accepted.accepted_profile_id
    assert payload["actual_profile"]["stages"][0]["device"] == "cuda"
    assert payload["fallback"]["used"] is False
    assert payload["fallback"]["status"] == "not_needed"
    assert payload["fallback"]["authorization"]["local_status"] == "authorized"
    assert payload["fallback"]["authorization"]["local_profile_id"].endswith("cpu-fallback-v1")
    assert payload["contract"] == "runtime-profile-provenance.v1"
    assert payload["current_product"]["version"] == setup_state.command_identity.version
    assert record.metadata["attempt"] == 3
    assert record.metadata["actual_profile_id"] == accepted.accepted_profile_id


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


def test_bind_processing_runtime_uses_resolver_owned_default_factories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DefaultTranscription(_Transcription):
        def __init__(self, **_kwargs: object) -> None:
            super().__init__("default-transcription")

    class DefaultDiarization(_Diarization):
        def __init__(self, **_kwargs: object) -> None:
            super().__init__("default-diarization")

    class DefaultSemantic(_Semantic):
        def __init__(self, **_kwargs: object) -> None:
            super().__init__()

    monkeypatch.setattr(
        "mena.runtime_profile.FasterWhisperTranscriptionProvider", DefaultTranscription
    )
    monkeypatch.setattr(
        "mena.runtime_profile.PyannoteCommunityDiarizationProvider", DefaultDiarization
    )
    monkeypatch.setattr("mena.runtime_profile.OllamaSemanticCoherenceProvider", DefaultSemantic)

    runtime = bind_processing_runtime(
        selection=_selection("accepted"),
        settings=cast(
            AppSettings,
            SimpleNamespace(local_llm=SimpleNamespace(ollama_endpoint="http://ollama")),
        ),
    )

    assert isinstance(runtime.transcription_provider, DefaultTranscription)
    assert isinstance(runtime.diarization_provider, DefaultDiarization)
    assert isinstance(runtime.semantic_coherence_provider, DefaultSemantic)


def test_bind_summarization_runtime_uses_selected_runtime_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class DefaultSummarization(_Summarization):
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)
            super().__init__()

    monkeypatch.setattr("mena.runtime_profile.OllamaSummarizationProvider", DefaultSummarization)

    provider = bind_summarization_runtime(
        selection=_selection("accepted"),
        settings=cast(
            AppSettings,
            SimpleNamespace(local_llm=SimpleNamespace(ollama_endpoint="http://ollama")),
        ),
    )

    assert isinstance(provider, DefaultSummarization)
    assert captured == {"model": "llama3.2:3b", "endpoint": "http://ollama"}


def test_bind_summarization_runtime_wraps_bound_factory_type_errors() -> None:
    def broken_factory() -> object:
        raise TypeError("missing constructor argument")

    with pytest.raises(
        ProviderValidationError,
        match="summarization provider must implement SummarizationProvider",
    ):
        bind_summarization_runtime(
            selection=_selection("accepted"),
            settings=cast(
                AppSettings,
                SimpleNamespace(local_llm=SimpleNamespace(ollama_endpoint="http://ollama")),
            ),
            summarization_provider_factory=broken_factory,
        )


def test_bind_summarization_runtime_validates_bound_injected_provider_methods() -> None:
    provider = SimpleNamespace(
        metadata=SimpleNamespace(config_id="cfg", to_dict=lambda: {}),
        summarize="not-callable",
    )

    with pytest.raises(
        ProviderValidationError,
        match="summarization provider must implement SummarizationProvider",
    ):
        bind_summarization_runtime(
            selection=_selection("accepted"),
            settings=cast(
                AppSettings,
                SimpleNamespace(local_llm=SimpleNamespace(ollama_endpoint="http://ollama")),
            ),
            summarization_provider_factory=lambda: provider,
        )


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


def _summarization_plan() -> SummarizationRuntimePlan:
    accepted_stage = StageProfile(
        stage="summarization",
        provider="Ollama",
        model="llama3.1:8b",
        device="local-service",
        policy="local-preferred",
    )
    return SummarizationRuntimePlan(
        accepted_stage=accepted_stage,
        actual_stage=accepted_stage,
        ordered_fallback=(mistral_summary_stage_profile(),),
    )


def _ollama_summary_stage(settings: AppSettings) -> StageProfile:
    return next(
        stage
        for stage in get_alpha_local_profile(settings.local_llm.ollama_model).stages
        if stage.stage == "summarization"
    )


def test_run_summarization_with_runtime_fallback_retries_and_records_provenance(
    tmp_path: Path,
) -> None:
    plan = _summarization_plan()
    primary_selection = cast(
        RuntimeProfileSelection,
        SimpleNamespace(accepted="accepted-state", actual="primary-state", summarization=plan),
    )
    fallback_selection = cast(
        RuntimeProfileSelection,
        SimpleNamespace(
            accepted="accepted-state",
            actual="fallback-state",
            summarization=replace(plan, actual_stage=plan.ordered_fallback[0]),
        ),
    )
    providers = [_ReleasableSummarization("primary"), _ReleasableSummarization("fallback")]
    calls: list[tuple[str, int]] = []
    stored: list[dict[str, object]] = []
    fallback_kwargs: dict[str, object] = {}

    def _run(provider: object, attempt: int) -> str:
        calls.append((cast(_ReleasableSummarization, provider).label, attempt))
        if attempt == 1:
            raise SummarizationProviderUnavailable("Ollama summarization provider is unavailable.")
        return cast(_ReleasableSummarization, provider).label

    def _fallback_selector(**kwargs: object) -> RuntimeProfileSelection:
        fallback_kwargs.update(kwargs)
        return fallback_selection

    result = run_summarization_with_runtime_fallback(
        settings=cast(AppSettings, SimpleNamespace()),
        store=FileSystemArtifactStore(session_id="session", session_dir=tmp_path),
        run_id="summarize-run",
        attempt=1,
        runner=None,
        command_identity=None,
        hardware=None,
        allow_runtime_fallback=False,
        authorize_remote_summarization_fallback=None,
        run=_run,
        failed_input_artifacts=("merged", "overlay"),
        summarization_provider_factory=lambda: providers.pop(0),
        runtime_profile_selector_fn=lambda **_kwargs: primary_selection,
        runtime_profile_selector_after_failure_fn=_fallback_selector,
        runtime_profile_store_fn=lambda **kwargs: stored.append(kwargs),
    )

    assert result.value == "fallback"
    assert calls == [("primary", 1), ("fallback", 2)]
    assert fallback_kwargs["accepted"] == "accepted-state"
    assert fallback_kwargs["used_plan"] is plan
    assert fallback_kwargs["failure_class"] == "SummarizationProviderUnavailable"
    assert fallback_kwargs["failed_input_artifacts"] == ("merged", "overlay")
    assert [call.get("name_suffix") for call in stored] == [None, "stage-fallback"]
    assert [release.detail for release in result.release_results] == [
        "primary released",
        "fallback released",
    ]


def test_run_summarization_with_runtime_fallback_reenters_the_ordered_ladder() -> None:
    plan = _summarization_plan()
    primary_selection = cast(
        RuntimeProfileSelection,
        SimpleNamespace(accepted="accepted-state", actual="primary-state", summarization=plan),
    )
    fallback_plan = replace(plan, actual_stage=plan.ordered_fallback[0])
    fallback_selection = cast(
        RuntimeProfileSelection,
        SimpleNamespace(
            accepted="accepted-state",
            actual="fallback-state",
            summarization=fallback_plan,
        ),
    )
    selector_plans: list[SummarizationRuntimePlan] = []
    stored_attempts: list[tuple[int, str]] = []

    def _fallback_selector(**kwargs: object) -> RuntimeProfileSelection:
        selector_plans.append(cast(SummarizationRuntimePlan, kwargs["used_plan"]))
        if len(selector_plans) == 1:
            return fallback_selection
        raise RuntimeFallbackUnavailable("no further accepted fallback provider")

    with pytest.raises(RuntimeFallbackUnavailable, match="no further accepted fallback provider"):
        run_summarization_with_runtime_fallback(
            settings=cast(AppSettings, SimpleNamespace()),
            store=cast(FileSystemArtifactStore, SimpleNamespace()),
            run_id="summarize-run",
            attempt=1,
            runner=None,
            command_identity=None,
            hardware=None,
            allow_runtime_fallback=False,
            authorize_remote_summarization_fallback=None,
            run=lambda _provider, _attempt: (_ for _ in ()).throw(
                SummarizationProviderUnavailable("provider unavailable")
            ),
            failed_input_artifacts=("merged", "overlay"),
            summarization_provider_factory=lambda: _Summarization(),
            runtime_profile_selector_fn=lambda **_kwargs: primary_selection,
            runtime_profile_selector_after_failure_fn=_fallback_selector,
            runtime_profile_store_fn=lambda **kwargs: stored_attempts.append(
                (
                    cast(int, kwargs["attempt"]),
                    cast(str, kwargs.get("name_suffix") or "initial"),
                )
            ),
        )

    assert selector_plans == [plan, fallback_plan]
    assert stored_attempts == [(1, "initial"), (2, "stage-fallback")]


def test_summarization_runtime_fallback_bind_failure_does_not_release_primary_twice(
    tmp_path: Path,
) -> None:
    plan = _summarization_plan()
    primary_selection = cast(
        RuntimeProfileSelection,
        SimpleNamespace(accepted="accepted-state", actual="primary-state", summarization=plan),
    )
    fallback_selection = cast(
        RuntimeProfileSelection,
        SimpleNamespace(
            accepted="accepted-state",
            actual="fallback-state",
            summarization=replace(plan, actual_stage=plan.ordered_fallback[0]),
        ),
    )
    provider_calls = 0
    release_attempts: list[int] = []

    def _provider_factory() -> _Summarization:
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls == 2:
            raise RuntimeError("fallback bind failed")
        return _ReleasableSummarization("primary")

    def _release(**kwargs: object) -> RuntimeReleaseResult:
        release_attempts.append(cast(int, kwargs["attempt"]))
        return RuntimeReleaseResult(
            lease=RuntimeLease(
                resource_kind="local_llm",
                stage="summarization",
                boundary="summarize",
                provider="Ollama",
                model="llama3.1:8b",
            ),
            status="released",
            detail="primary released",
        )

    with pytest.raises(RuntimeError, match="fallback bind failed"):
        run_summarization_with_runtime_fallback(
            settings=cast(AppSettings, SimpleNamespace()),
            store=FileSystemArtifactStore(session_id="session", session_dir=tmp_path),
            run_id="summarize-run",
            attempt=1,
            runner=None,
            command_identity=None,
            hardware=None,
            allow_runtime_fallback=False,
            authorize_remote_summarization_fallback=None,
            run=lambda _provider, _attempt: (_ for _ in ()).throw(
                SummarizationProviderUnavailable("provider unavailable")
            ),
            failed_input_artifacts=("merged", "overlay"),
            summarization_provider_factory=_provider_factory,
            runtime_profile_selector_fn=lambda **_kwargs: primary_selection,
            runtime_profile_selector_after_failure_fn=lambda **_kwargs: fallback_selection,
            runtime_profile_store_fn=lambda **_kwargs: None,
            terminal_release_fn=_release,
        )

    assert release_attempts == [1]


def test_run_summarization_with_runtime_fallback_observes_terminal_release_on_failure(
    tmp_path: Path,
) -> None:
    plan = _summarization_plan()
    selection = cast(
        RuntimeProfileSelection,
        SimpleNamespace(accepted="accepted-state", actual="primary-state", summarization=plan),
    )
    observed: list[RuntimeReleaseResult] = []

    def _release(**kwargs: object) -> RuntimeReleaseResult:
        lease = RuntimeLease(
            resource_kind="local_llm",
            stage=cast(str, kwargs["stage"]),
            boundary=cast(str, kwargs["boundary"]),
            provider="Ollama",
            model="llama3.1:8b",
        )
        return RuntimeReleaseResult(
            lease=lease,
            status="failed",
            detail="Ollama model release failed.",
        )

    with pytest.raises(SummarizationError, match="summary exploded"):
        run_summarization_with_runtime_fallback(
            settings=cast(AppSettings, SimpleNamespace()),
            store=FileSystemArtifactStore(session_id="session", session_dir=tmp_path),
            run_id="summarize-run",
            attempt=1,
            runner=None,
            command_identity=None,
            hardware=None,
            allow_runtime_fallback=False,
            authorize_remote_summarization_fallback=None,
            run=lambda _provider, _attempt: (_ for _ in ()).throw(
                SummarizationError("summary exploded")
            ),
            failed_input_artifacts=("merged", "overlay"),
            summarization_provider_factory=lambda: _ReleasableSummarization("primary"),
            runtime_profile_selector_fn=lambda **_kwargs: selection,
            runtime_profile_store_fn=lambda **_kwargs: None,
            terminal_release_fn=_release,
            runtime_release_observer=observed.append,
        )

    assert [result.detail for result in observed] == ["Ollama model release failed."]


def _mistral_summary_state(
    *,
    key_present: bool = True,
    reachable: bool = True,
    resolved_model: str | None = "mistral-small-2603",
    model_allowed: bool = True,
    detail: str = "resolved inside the accepted snapshot",
) -> MistralSummaryState:
    return MistralSummaryState(
        key_present=key_present,
        reachable=reachable,
        request_model=MISTRAL_SUMMARY_REQUEST_MODEL,
        resolved_model=resolved_model,
        model_allowed=model_allowed,
        policy=MISTRAL_SUMMARY_ALLOWLIST_POLICY,
        detail=detail,
    )


def _mistral_probe(state: MistralSummaryState) -> Any:
    def _probe(*, env: object) -> MistralSummaryState:
        _ = env
        return state

    return _probe


def _accepted_mistral_primary_state(
    *,
    settings: AppSettings,
    setup_state: LocalInstallationState,
) -> RuntimeProfileState:
    profile = get_mistral_summary_profile(settings.local_llm.ollama_model)
    mistral_state = replace(setup_state, mistral_summary=_mistral_summary_state())
    return build_runtime_profile_state(evaluate_setup(profile, mistral_state))


def _accepted_mistral_primary_with_local_fallback(
    *,
    settings: AppSettings,
    setup_state: LocalInstallationState,
    fallback_entry: StageProfile | None = None,
) -> RuntimeProfileState:
    mistral_primary = _accepted_mistral_primary_state(
        settings=settings,
        setup_state=setup_state,
    )
    local_fallback_entry = fallback_entry or _ollama_summary_stage(settings)
    return replace(
        mistral_primary,
        fallback_authorization=replace(
            mistral_primary.fallback_authorization,
            local_status="authorized",
            automatic_local_fallback_authorized=True,
            stage_scope=("summarization",),
        ),
        summarization_fallback_providers=(local_fallback_entry,),
    )


def _accepted_local_state_with_mistral_fallback(
    *,
    settings: AppSettings,
    setup_state: LocalInstallationState,
) -> RuntimeProfileState:
    profile = get_alpha_local_profile(settings.local_llm.ollama_model)
    return build_runtime_profile_state(
        evaluate_setup(profile, setup_state),
        summarization_fallback_providers=(mistral_summary_stage_profile(),),
    )


def _demote_remote_authorization(state: RuntimeProfileState) -> RuntimeProfileState:
    payload = state.to_dict()
    authorization = cast(dict[str, object], payload["fallback_authorization"])
    authorization["remote_status"] = "configured_unauthorized"
    authorization["remote_stage_scope"] = []
    return RuntimeProfileState.from_dict(payload)


def test_summarization_runtime_selection_uses_accepted_mistral_primary_without_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    setup_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = _accepted_mistral_primary_state(settings=settings, setup_state=setup_state)
    persist_runtime_profile_state(accepted, paths=settings.paths)
    mistral_ready = replace(setup_state, mistral_summary=_mistral_summary_state())
    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state",
        lambda *_args, **_kwargs: mistral_ready,
    )

    def _never_prompt(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("accepted primary Mistral must not prompt")

    selection = select_summarization_runtime_profile(
        settings=settings,
        runner=None,
        command_identity=None,
        hardware=None,
        allow_runtime_fallback=False,
        authorize_runtime_fallback=_never_prompt,
        authorize_remote_summarization_fallback=_never_prompt,
    )

    assert not selection.fallback_used
    assert selection.summarization is not None
    plan = selection.summarization
    assert plan.accepted_stage.provider == "Mistral"
    assert plan.actual_stage == plan.accepted_stage
    assert plan.ordered_fallback == ()
    assert plan.resolved_model == "mistral-small-2603"


def test_summarization_runtime_selection_mistral_primary_fails_closed_on_alias_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    setup_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = _accepted_mistral_primary_state(settings=settings, setup_state=setup_state)
    persist_runtime_profile_state(accepted, paths=settings.paths)
    drifted = replace(
        setup_state,
        mistral_summary=_mistral_summary_state(
            resolved_model="mistral-small-2707",
            model_allowed=False,
            detail=(
                "resolved to 'mistral-small-2707', outside the accepted snapshot; "
                f"failing closed under {MISTRAL_SUMMARY_ALLOWLIST_POLICY}"
            ),
        ),
    )
    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state",
        lambda *_args, **_kwargs: drifted,
    )

    with pytest.raises(RuntimeFallbackUnavailable, match="failing closed"):
        select_summarization_runtime_profile(
            settings=settings,
            runner=None,
            command_identity=None,
            hardware=None,
            allow_runtime_fallback=False,
        )


def test_summarization_runtime_selection_uses_accepted_local_fallback_after_mistral_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    setup_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = _accepted_mistral_primary_with_local_fallback(
        settings=settings, setup_state=setup_state
    )
    persist_runtime_profile_state(accepted, paths=settings.paths)
    mistral_down = replace(
        setup_state,
        mistral_summary=_mistral_summary_state(
            key_present=False,
            reachable=False,
            resolved_model=None,
            model_allowed=False,
            detail="MENA_MISTRAL_API_KEY is not set.",
        ),
    )
    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state",
        lambda *_args, **_kwargs: mistral_down,
    )

    selection = select_summarization_runtime_profile(
        settings=settings,
        runner=None,
        command_identity=None,
        hardware=None,
        allow_runtime_fallback=False,
    )

    assert selection.fallback_used
    assert selection.fallback_status == "used"
    assert selection.fallback_authorization_source == "setup_authorized"
    assert selection.summarization is not None
    assert selection.summarization.accepted_stage.provider == "Mistral"
    assert selection.summarization.actual_stage.provider == "Ollama"
    assert selection.actual.stage_policies == (_ollama_summary_stage(settings),)


def test_summarization_runtime_selection_revalidates_ollama_fallback_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    setup_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = _accepted_mistral_primary_with_local_fallback(
        settings=settings, setup_state=setup_state
    )
    persist_runtime_profile_state(accepted, paths=settings.paths)
    mistral_down_ollama_missing = replace(
        _setup_state(
            settings=settings,
            ollama_model_present=False,
            pyannote_token_present=True,
        ),
        mistral_summary=_mistral_summary_state(
            key_present=False,
            reachable=False,
            resolved_model=None,
            model_allowed=False,
            detail="MENA_MISTRAL_API_KEY is not set.",
        ),
    )
    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state",
        lambda *_args, **_kwargs: mistral_down_ollama_missing,
    )

    with pytest.raises(RuntimeFallbackUnavailable, match="Ollama fallback is not viable"):
        select_summarization_runtime_profile(
            settings=settings,
            runner=None,
            command_identity=None,
            hardware=None,
            allow_runtime_fallback=False,
        )


def test_summarization_runtime_selection_revalidates_ollama_fallback_entry_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    setup_state = _setup_state(settings=settings, pyannote_token_present=True)
    fallback_entry = replace(
        _ollama_summary_stage(settings),
        model="fallback-only-model",
    )
    accepted = _accepted_mistral_primary_with_local_fallback(
        settings=settings,
        setup_state=setup_state,
        fallback_entry=fallback_entry,
    )
    persist_runtime_profile_state(accepted, paths=settings.paths)
    observed_models: list[str] = []

    def _detect_state(_settings: AppSettings, **kwargs: object) -> LocalInstallationState:
        profile = cast(Any, kwargs["profile"])
        model = next(stage.model for stage in profile.stages if stage.stage == "summarization")
        observed_models.append(model)
        return replace(
            setup_state,
            mistral_summary=_mistral_summary_state(
                key_present=False,
                reachable=False,
                resolved_model=None,
                model_allowed=False,
                detail="MENA_MISTRAL_API_KEY is not set.",
            ),
            ollama=replace(
                setup_state.ollama,
                model=model,
                model_present=model == settings.local_llm.ollama_model,
                detail=(
                    "model present"
                    if model == settings.local_llm.ollama_model
                    else f"model {model} is not listed by Ollama"
                ),
            ),
        )

    monkeypatch.setattr("mena.runtime_profile.detect_local_installation_state", _detect_state)

    with pytest.raises(RuntimeFallbackUnavailable, match="fallback-only-model"):
        select_summarization_runtime_profile(
            settings=settings,
            runner=None,
            command_identity=None,
            hardware=None,
            allow_runtime_fallback=False,
        )

    assert observed_models[-1] == "fallback-only-model"


def test_summarization_runtime_selection_rejects_unauthorized_local_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    setup_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = replace(
        _accepted_mistral_primary_state(settings=settings, setup_state=setup_state),
        summarization_fallback_providers=(_ollama_summary_stage(settings),),
    )
    persist_runtime_profile_state(accepted, paths=settings.paths)
    mistral_down = replace(
        setup_state,
        mistral_summary=_mistral_summary_state(
            key_present=False,
            reachable=False,
            resolved_model=None,
            model_allowed=False,
            detail="MENA_MISTRAL_API_KEY is not set.",
        ),
    )
    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state",
        lambda *_args, **_kwargs: mistral_down,
    )

    with pytest.raises(RuntimeFallbackUnavailable, match="Ollama fallback transition"):
        select_summarization_runtime_profile(
            settings=settings,
            runner=None,
            command_identity=None,
            hardware=None,
            allow_runtime_fallback=False,
        )


def test_summarization_runtime_selection_allows_one_run_local_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    setup_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = replace(
        _accepted_mistral_primary_state(settings=settings, setup_state=setup_state),
        summarization_fallback_providers=(_ollama_summary_stage(settings),),
    )
    persist_runtime_profile_state(accepted, paths=settings.paths)
    mistral_down = replace(
        setup_state,
        mistral_summary=_mistral_summary_state(
            key_present=False,
            reachable=False,
            resolved_model=None,
            model_allowed=False,
            detail="MENA_MISTRAL_API_KEY is not set.",
        ),
    )
    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state",
        lambda *_args, **_kwargs: mistral_down,
    )

    selection = select_summarization_runtime_profile(
        settings=settings,
        runner=None,
        command_identity=None,
        hardware=None,
        allow_runtime_fallback=True,
    )

    assert selection.fallback_used
    assert selection.fallback_authorization_source == "explicit_one_run"
    assert selection.summarization is not None
    assert selection.summarization.accepted_stage.provider == "Mistral"
    assert selection.summarization.actual_stage.provider == "Ollama"


def test_summarization_runtime_selection_uses_accepted_mistral_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    ready_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = _accepted_local_state_with_mistral_fallback(
        settings=settings, setup_state=ready_state
    )
    persist_runtime_profile_state(accepted, paths=settings.paths)
    ollama_down = _setup_state(
        settings=settings,
        ollama_model_present=False,
        pyannote_token_present=True,
    )
    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state",
        lambda *_args, **_kwargs: ollama_down,
    )

    selection = select_summarization_runtime_profile(
        settings=settings,
        runner=None,
        command_identity=None,
        hardware=None,
        allow_runtime_fallback=False,
        env={},
        mistral_probe=_mistral_probe(_mistral_summary_state()),
    )

    assert selection.fallback_used
    assert selection.fallback_status == "used"
    assert selection.fallback_authorization_source == "setup_authorized"
    assert selection.fallback_reason is not None
    assert "not listed by Ollama" in selection.fallback_reason
    assert selection.summarization is not None
    plan = selection.summarization
    assert plan.accepted_stage.provider == "Ollama"
    assert plan.actual_stage.provider == "Mistral"
    assert plan.resolved_model == "mistral-small-2603"


def test_summarization_runtime_selection_does_not_remote_fallback_on_setup_guard_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    ready_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = _accepted_local_state_with_mistral_fallback(
        settings=settings, setup_state=ready_state
    )
    persist_runtime_profile_state(accepted, paths=settings.paths)
    repo_invocation_state = _setup_state(
        settings=settings,
        command_identity=CommandIdentity(
            current_executable=Path("/tmp/mena/app/.venv/bin/mena"),
            plain_mena_path=Path("/tmp/mena/app/.venv/bin/mena"),
            version="0.1.0",
            invocation_mode="repo-checkout",
        ),
        pyannote_token_present=True,
    )
    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state",
        lambda *_args, **_kwargs: repo_invocation_state,
    )

    def _mistral_probe(**_kwargs: object) -> MistralSummaryState:
        raise AssertionError("setup guard failures must not probe remote fallback")

    with pytest.raises(RuntimeFallbackUnavailable, match="setup guard failed"):
        select_summarization_runtime_profile(
            settings=settings,
            runner=None,
            command_identity=None,
            hardware=None,
            allow_runtime_fallback=False,
            env={},
            mistral_probe=_mistral_probe,
        )


def test_summarization_runtime_selection_local_authorization_never_authorizes_remote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    ready_state = _setup_state(settings=settings, pyannote_token_present=True)
    profile = get_alpha_local_profile(settings.local_llm.ollama_model)
    fallback_profile = default_profile_catalog(settings.local_llm.ollama_model)[1]
    accepted = build_runtime_profile_state(
        evaluate_setup(profile, ready_state),
        fallback_evaluation=evaluate_setup(fallback_profile, ready_state),
        authorize_local_fallback=True,
        summarization_fallback_providers=(mistral_summary_stage_profile(),),
    )
    accepted = _demote_remote_authorization(accepted)
    persist_runtime_profile_state(accepted, paths=settings.paths)
    ollama_down = _setup_state(
        settings=settings,
        ollama_model_present=False,
        pyannote_token_present=True,
    )
    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state",
        lambda *_args, **_kwargs: ollama_down,
    )

    assert accepted.fallback_authorization.local_status == "authorized"
    with pytest.raises(RuntimeFallbackUnavailable, match="not pre-authorized"):
        select_summarization_runtime_profile(
            settings=settings,
            runner=None,
            command_identity=None,
            hardware=None,
            # An explicit one-run local fallback flag must not authorize a
            # remote transition either.
            allow_runtime_fallback=True,
            env={},
            mistral_probe=_mistral_probe(_mistral_summary_state()),
        )


def test_summarization_runtime_selection_prompts_for_unauthorized_remote_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    ready_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = _demote_remote_authorization(
        _accepted_local_state_with_mistral_fallback(settings=settings, setup_state=ready_state)
    )
    persist_runtime_profile_state(accepted, paths=settings.paths)
    ollama_down = _setup_state(
        settings=settings,
        ollama_model_present=False,
        pyannote_token_present=True,
    )
    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state",
        lambda *_args, **_kwargs: ollama_down,
    )
    observed: dict[str, object] = {}

    def _authorize(reason: str, state: MistralSummaryState) -> bool:
        observed["reason"] = reason
        observed["resolved_model"] = state.resolved_model
        return True

    selection = select_summarization_runtime_profile(
        settings=settings,
        runner=None,
        command_identity=None,
        hardware=None,
        allow_runtime_fallback=False,
        authorize_remote_summarization_fallback=_authorize,
        env={},
        mistral_probe=_mistral_probe(_mistral_summary_state()),
    )

    assert selection.fallback_used
    assert selection.fallback_authorization_source == "interactive"
    assert "not listed by Ollama" in cast(str, observed["reason"])
    assert observed["resolved_model"] == "mistral-small-2603"


def test_summarization_runtime_selection_reports_unviable_mistral_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    ready_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = _accepted_local_state_with_mistral_fallback(
        settings=settings, setup_state=ready_state
    )
    persist_runtime_profile_state(accepted, paths=settings.paths)
    ollama_down = _setup_state(
        settings=settings,
        ollama_model_present=False,
        pyannote_token_present=True,
    )
    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state",
        lambda *_args, **_kwargs: ollama_down,
    )

    with pytest.raises(RuntimeFallbackUnavailable, match="MENA_MISTRAL_API_KEY"):
        select_summarization_runtime_profile(
            settings=settings,
            runner=None,
            command_identity=None,
            hardware=None,
            allow_runtime_fallback=False,
            env={},
            mistral_probe=_mistral_probe(
                _mistral_summary_state(
                    key_present=False,
                    reachable=False,
                    resolved_model=None,
                    model_allowed=False,
                    detail="MENA_MISTRAL_API_KEY is not set.",
                )
            ),
        )


def test_summarization_runtime_selection_keeps_legacy_local_only_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    setup_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = _runtime_profile_state_with_evaluated_fallback(
        settings=settings,
        setup_state=setup_state,
    )
    persist_runtime_profile_state(accepted, paths=settings.paths)
    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state",
        lambda *_args, **_kwargs: setup_state,
    )

    selection = select_summarization_runtime_profile(
        settings=settings,
        runner=None,
        command_identity=None,
        hardware=None,
        allow_runtime_fallback=False,
    )

    assert not selection.fallback_used
    assert selection.summarization is not None
    assert selection.summarization.actual_stage.provider == "Ollama"
    assert selection.summarization.ordered_fallback == ()


def test_summarization_stage_failure_selection_consumes_next_fallback_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    ready_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = _accepted_local_state_with_mistral_fallback(
        settings=settings, setup_state=ready_state
    )
    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state",
        lambda *_args, **_kwargs: ready_state,
    )
    accepted_stage = next(
        stage for stage in accepted.stage_policies if stage.stage == "summarization"
    )
    used_plan = SummarizationRuntimePlan(
        accepted_stage=accepted_stage,
        actual_stage=accepted_stage,
        ordered_fallback=accepted.summarization_fallback_providers,
    )

    selection = select_summarization_runtime_after_stage_failure(
        settings=settings,
        runner=None,
        command_identity=None,
        hardware=None,
        accepted=accepted,
        used_plan=used_plan,
        failure_reason="Ollama summarization provider is unavailable.",
        failure_class="SummarizationProviderUnavailable",
        failed_input_artifacts=("session:artifacts/merged_transcript/merged.json",),
        env={},
        mistral_probe=_mistral_probe(_mistral_summary_state()),
    )

    assert selection.fallback_used
    assert selection.fallback_status == "used_after_stage_failure"
    assert selection.fallback_failed_stage == "summarization"
    assert selection.fallback_authorization_source == "setup_authorized"
    assert selection.summarization is not None
    assert selection.summarization.actual_stage.provider == "Mistral"


def test_summarization_stage_failure_selection_can_fallback_from_mistral_to_local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    ready_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = _accepted_mistral_primary_with_local_fallback(
        settings=settings, setup_state=ready_state
    )
    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state",
        lambda *_args, **_kwargs: ready_state,
    )
    accepted_stage = next(
        stage for stage in accepted.stage_policies if stage.stage == "summarization"
    )
    used_plan = SummarizationRuntimePlan(
        accepted_stage=accepted_stage,
        actual_stage=accepted_stage,
        ordered_fallback=accepted.summarization_fallback_providers,
    )

    selection = select_summarization_runtime_after_stage_failure(
        settings=settings,
        runner=None,
        command_identity=None,
        hardware=None,
        accepted=accepted,
        used_plan=used_plan,
        failure_reason="Mistral summarization provider is unavailable.",
        failure_class="SummarizationProviderUnavailable",
        failed_input_artifacts=("session:artifacts/merged_transcript/merged.json",),
    )

    assert selection.fallback_used
    assert selection.fallback_status == "used_after_stage_failure"
    assert selection.fallback_authorization_source == "setup_authorized"
    assert selection.summarization is not None
    assert selection.summarization.accepted_stage.provider == "Mistral"
    assert selection.summarization.actual_stage.provider == "Ollama"
    assert selection.actual.stage_policies == (_ollama_summary_stage(settings),)


def test_summarization_stage_failure_selection_allows_one_run_local_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    ready_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = replace(
        _accepted_mistral_primary_state(settings=settings, setup_state=ready_state),
        summarization_fallback_providers=(_ollama_summary_stage(settings),),
    )
    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state",
        lambda *_args, **_kwargs: ready_state,
    )
    accepted_stage = next(
        stage for stage in accepted.stage_policies if stage.stage == "summarization"
    )
    used_plan = SummarizationRuntimePlan(
        accepted_stage=accepted_stage,
        actual_stage=accepted_stage,
        ordered_fallback=accepted.summarization_fallback_providers,
    )

    selection = select_summarization_runtime_after_stage_failure(
        settings=settings,
        runner=None,
        command_identity=None,
        hardware=None,
        accepted=accepted,
        used_plan=used_plan,
        failure_reason="Mistral summarization provider is unavailable.",
        failure_class="SummarizationProviderUnavailable",
        failed_input_artifacts=("session:artifacts/merged_transcript/merged.json",),
        allow_runtime_fallback=True,
    )

    assert selection.fallback_used
    assert selection.fallback_status == "used_after_stage_failure"
    assert selection.fallback_authorization_source == "explicit_one_run"
    assert selection.summarization is not None
    assert selection.summarization.accepted_stage.provider == "Mistral"
    assert selection.summarization.actual_stage.provider == "Ollama"


def test_summarization_stage_failure_selection_stops_after_the_last_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    ready_state = _setup_state(settings=settings, pyannote_token_present=True)
    accepted = _accepted_local_state_with_mistral_fallback(
        settings=settings, setup_state=ready_state
    )
    monkeypatch.setattr(
        "mena.runtime_profile.detect_local_installation_state",
        lambda *_args, **_kwargs: ready_state,
    )
    accepted_stage = next(
        stage for stage in accepted.stage_policies if stage.stage == "summarization"
    )
    used_plan = SummarizationRuntimePlan(
        accepted_stage=accepted_stage,
        actual_stage=accepted.summarization_fallback_providers[0],
        ordered_fallback=accepted.summarization_fallback_providers,
    )

    with pytest.raises(RuntimeFallbackUnavailable, match="no further accepted fallback"):
        select_summarization_runtime_after_stage_failure(
            settings=settings,
            runner=None,
            command_identity=None,
            hardware=None,
            accepted=accepted,
            used_plan=used_plan,
            failure_reason="Mistral summarization provider is unavailable.",
            failure_class="SummarizationProviderUnavailable",
            env={},
            mistral_probe=_mistral_probe(_mistral_summary_state()),
        )


def test_runtime_profile_provenance_records_summarization_plan(tmp_path: Path) -> None:
    store = FileSystemArtifactStore(session_id="session", session_dir=tmp_path)
    accepted_stage = StageProfile(
        stage="summarization",
        provider="Ollama",
        model="llama3.1:8b",
        device="local-service",
        policy="local-preferred",
    )
    selection = replace(
        _selection("accepted", fallback_used=True, fallback_status="used"),
        fallback_reason="ollama is down",
        fallback_authorization_source="setup_authorized",
        summarization=SummarizationRuntimePlan(
            accepted_stage=accepted_stage,
            actual_stage=mistral_summary_stage_profile(),
            ordered_fallback=(mistral_summary_stage_profile(),),
            resolved_model="mistral-small-2603",
        ),
    )

    record = store_runtime_profile_provenance(
        store=store,
        run_id="summarize-run",
        selection=selection,
    )

    payload = json.loads(store.retrieve(record.id).decode("utf-8"))
    summarization = payload["summarization"]
    assert summarization["accepted_provider"] == "Ollama"
    assert summarization["actual_provider"] == "Mistral"
    assert summarization["request_model"] == MISTRAL_SUMMARY_REQUEST_MODEL
    assert summarization["resolved_model"] == "mistral-small-2603"
    assert summarization["fallback_used"] is True
    assert summarization["fallback_reason"] == "ollama is down"
    assert summarization["authorization_source"] == "setup_authorized"
    assert summarization["ordered_fallback_providers"][0]["provider"] == "Mistral"


def test_bind_summarization_runtime_constructs_the_mistral_provider(tmp_path: Path) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    accepted_stage = StageProfile(
        stage="summarization",
        provider="Ollama",
        model="llama3.1:8b",
        device="local-service",
        policy="local-preferred",
    )
    selection = replace(
        _selection("accepted"),
        summarization=SummarizationRuntimePlan(
            accepted_stage=accepted_stage,
            actual_stage=mistral_summary_stage_profile(),
            ordered_fallback=(mistral_summary_stage_profile(),),
            resolved_model="mistral-small-2603",
        ),
    )

    provider = bind_summarization_runtime(
        selection=selection,
        settings=settings,
        env={MENA_MISTRAL_API_KEY_ENV: "mena-key"},
    )

    assert isinstance(provider, MistralSummarizationProvider)
    assert provider.metadata.model == MISTRAL_SUMMARY_REQUEST_MODEL
    assert provider.metadata.resolved_model == "mistral-small-2603"


def test_bind_summarization_runtime_never_consumes_the_generic_mistral_key(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    selection = replace(
        _selection("accepted"),
        summarization=SummarizationRuntimePlan(
            accepted_stage=mistral_summary_stage_profile(),
            actual_stage=mistral_summary_stage_profile(),
        ),
    )

    provider = bind_summarization_runtime(
        selection=selection,
        settings=settings,
        env={"MISTRAL_API_KEY": "generic-key"},
    )

    assert isinstance(provider, MistralSummarizationProvider)
    with pytest.raises(SummarizationError, match=MENA_MISTRAL_API_KEY_ENV):
        provider.summarize(cast(Any, SimpleNamespace()))
