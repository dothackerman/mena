from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mena.settings import AppSettings
from mena.setup import (
    CommandCheck,
    CommandIdentity,
    HardwareFacts,
    LocalInstallationState,
    OllamaState,
    PythonSurfaceCheck,
    RuntimeProfileState,
    build_runtime_profile_state,
    evaluate_setup,
    get_alpha_local_profile,
    persist_runtime_profile_state,
)


@dataclass(frozen=True)
class LocalRuntimeCapabilities:
    hardware: HardwareFacts | None = None
    asr_model_present: bool | None = True
    diarization_model_present: bool | None = True
    pyannote_token_present: bool = True


@dataclass(frozen=True)
class AcceptedLocalRuntime:
    installation_state: LocalInstallationState
    accepted_profile_state: RuntimeProfileState


def arrange_accepted_local_runtime(
    *,
    settings: AppSettings,
    capabilities: LocalRuntimeCapabilities | None = None,
) -> AcceptedLocalRuntime:
    """Evaluate and persist one accepted local runtime through production contracts."""
    capabilities = capabilities or LocalRuntimeCapabilities()
    profile = get_alpha_local_profile(settings.local_llm.ollama_model)
    installation = LocalInstallationState(
        command_identity=CommandIdentity(
            current_executable=Path("/home/user/.local/bin/mena"),
            plain_mena_path=Path("/home/user/.local/bin/mena"),
            version="0.1.0",
            invocation_mode="installed-product",
        ),
        runtime_paths=settings.paths,
        hardware=capabilities.hardware
        or HardwareFacts(
            system="Linux", ram_gb=32.0, cuda_available=True, vram_gb=12.0, avx2_available=True
        ),
        commands=tuple(
            CommandCheck(name=name, path=Path("/usr/bin") / name)
            for name in profile.required_commands
        ),
        playback_commands=tuple(
            CommandCheck(name=name, path=Path("/usr/bin") / name if name == "ffplay" else None)
            for name in profile.playback_commands
        ),
        python_surfaces=tuple(
            PythonSurfaceCheck(requirement=requirement, installed=True)
            for requirement in profile.python_surfaces
        ),
        ollama=OllamaState(
            endpoint=settings.local_llm.ollama_endpoint,
            model=settings.local_llm.ollama_model,
            command=CommandCheck("ollama", Path("/usr/bin/ollama")),
            reachable=True,
            model_present=True,
            detail="model present",
        ),
        asr_model_present=capabilities.asr_model_present,
        diarization_model_present=capabilities.diarization_model_present,
        pyannote_token_present=capabilities.pyannote_token_present,
        python_executable=Path("/usr/bin/python3"),
    )
    accepted = build_runtime_profile_state(evaluate_setup(profile, installation))
    persist_runtime_profile_state(accepted, paths=settings.paths)
    return AcceptedLocalRuntime(installation, accepted)
