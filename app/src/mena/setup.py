"""Linux alpha setup profile, preflight evaluation, and setup actions."""

from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from mena import __version__
from mena.local_llm import OLLAMA_DEFAULT_MODEL
from mena.settings import CONFIG_FILENAME, AppSettings, RuntimePaths
from mena.transcription import (
    FasterWhisperModelProfile,
    HardwareSnapshot,
    select_faster_whisper_profile,
)

ALPHA_LOCAL_PROFILE_ID = "linux-alpha-local-v1"
ALPHA_LOCAL_PROFILE_VERSION = "2026.05"
PYANNOTE_COMMUNITY_CHECKPOINT = "pyannote/speaker-diarization-community-1"
REQUIRED_COMMANDS = ("mena", "uv", "ollama", "pw-dump", "pw-cli", "wpctl")
PLAYBACK_COMMANDS = ("ffplay", "pw-play", "paplay", "aplay")
MINIMUM_RAM_GB = 16.0
MINIMUM_CUDA_VRAM_GB = 5.0


@dataclass(frozen=True)
class CommandRun:
    """Result of one command executed by a setup runner."""

    args: tuple[str, ...]
    exit_code: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class CommandRunner(Protocol):
    """Small command abstraction used by detection and mutation."""

    def which(self, command: str) -> Path | None:
        """Return a command path if it is available on PATH."""

    def run(
        self,
        args: Sequence[str],
        *,
        timeout_seconds: float = 30.0,
        env: Mapping[str, str] | None = None,
    ) -> CommandRun:
        """Run a command and capture its result."""


class SubprocessCommandRunner:
    """Real command runner used by the CLI."""

    def which(self, command: str) -> Path | None:
        path = shutil.which(command)
        return None if path is None else Path(path)

    def run(
        self,
        args: Sequence[str],
        *,
        timeout_seconds: float = 30.0,
        env: Mapping[str, str] | None = None,
    ) -> CommandRun:
        command = tuple(str(arg) for arg in args)
        command_env = None if env is None else {**os.environ, **env}
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env=command_env,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.output if isinstance(exc.output, str) else ""
            if not stdout and isinstance(exc.stdout, str):
                stdout = exc.stdout
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            return CommandRun(command, 124, stdout=stdout, stderr=stderr or "Command timed out.")
        except OSError as exc:
            return CommandRun(command, 127, stderr=str(exc))
        return CommandRun(
            command,
            completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


@dataclass(frozen=True)
class StageProfile:
    """Provider/model policy for one pipeline stage in the fixed alpha profile."""

    stage: str
    provider: str
    model: str
    policy: str


@dataclass(frozen=True)
class PythonSurfaceRequirement:
    """Python package surface required by the fixed local profile."""

    label: str
    import_name: str
    install_extra: str


@dataclass(frozen=True)
class SetupProfile:
    """Product-supported setup profile from the bundled catalog."""

    id: str
    version: str
    name: str
    stages: tuple[StageProfile, ...]
    python_surfaces: tuple[PythonSurfaceRequirement, ...]
    required_commands: tuple[str, ...]
    playback_commands: tuple[str, ...]
    minimum_ram_gb: float
    minimum_cuda_vram_gb: float
    requires_linux: bool = True


@dataclass(frozen=True)
class CommandCheck:
    """Presence check for one executable."""

    name: str
    path: Path | None

    @property
    def present(self) -> bool:
        return self.path is not None


@dataclass(frozen=True)
class PythonSurfaceCheck:
    """Presence check for one optional Python provider surface."""

    requirement: PythonSurfaceRequirement
    installed: bool


@dataclass(frozen=True)
class HardwareFacts:
    """Hardware facts needed for the first Linux alpha local profile."""

    system: str
    ram_gb: float | None
    cuda_available: bool
    vram_gb: float | None
    avx2_available: bool | None


@dataclass(frozen=True)
class CommandIdentity:
    """Executable identity reported by setup and smoke verification."""

    current_executable: Path
    plain_mena_path: Path | None
    version: str
    invocation_mode: str


@dataclass(frozen=True)
class OllamaState:
    """Detected local Ollama state for the configured local LLM model."""

    endpoint: str
    model: str
    command: CommandCheck
    reachable: bool
    model_present: bool
    detail: str


@dataclass(frozen=True)
class LocalInstallationState:
    """What the current machine actually has installed, configured, and reachable."""

    command_identity: CommandIdentity
    runtime_paths: RuntimePaths
    hardware: HardwareFacts
    commands: tuple[CommandCheck, ...]
    playback_commands: tuple[CommandCheck, ...]
    python_surfaces: tuple[PythonSurfaceCheck, ...]
    ollama: OllamaState
    asr_model_present: bool | None
    diarization_model_present: bool | None
    pyannote_token_present: bool
    python_executable: Path


@dataclass(frozen=True)
class ReadinessItem:
    """One readiness check result."""

    id: str
    label: str
    ready: bool
    detail: str


@dataclass(frozen=True)
class SetupAction:
    """A planned setup action shown before mutation."""

    id: str
    label: str
    mode: str
    detail: str
    command: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    required: bool = True


@dataclass(frozen=True)
class SetupPlan:
    """Planned changes required to satisfy the selected profile."""

    actions: tuple[SetupAction, ...]

    @property
    def has_required_actions(self) -> bool:
        return any(action.required for action in self.actions)


@dataclass(frozen=True)
class SetupEvaluation:
    """Pure evaluation of local state against one setup profile."""

    profile: SetupProfile
    selected_asr_profile: FasterWhisperModelProfile
    state: LocalInstallationState
    readiness: tuple[ReadinessItem, ...]
    plan: SetupPlan

    @property
    def ready(self) -> bool:
        return all(item.ready for item in self.readiness)

    @property
    def blocking_reasons(self) -> tuple[str, ...]:
        return tuple(item.detail for item in self.readiness if not item.ready)


@dataclass(frozen=True)
class SetupActionResult:
    """Result of one setup action after user confirmation."""

    action_id: str
    label: str
    status: str
    detail: str
    command: tuple[str, ...] = ()


def default_profile_catalog(ollama_model: str = OLLAMA_DEFAULT_MODEL) -> tuple[SetupProfile, ...]:
    """Return the bundled product-supported setup profile catalog."""

    return (
        SetupProfile(
            id=ALPHA_LOCAL_PROFILE_ID,
            version=ALPHA_LOCAL_PROFILE_VERSION,
            name="Linux alpha local-only profile",
            stages=(
                StageProfile(
                    stage="transcription",
                    provider="faster-whisper",
                    model="hardware-selected Whisper profile",
                    policy="local-only",
                ),
                StageProfile(
                    stage="diarization",
                    provider="pyannote.audio",
                    model=PYANNOTE_COMMUNITY_CHECKPOINT,
                    policy="local-only",
                ),
                StageProfile(
                    stage="summarization",
                    provider="Ollama",
                    model=ollama_model,
                    policy="local-preferred, no setup-time remote fallback",
                ),
                StageProfile(
                    stage="semantic-review",
                    provider="Ollama",
                    model=ollama_model,
                    policy="local-preferred, remote fallback not configured by setup",
                ),
            ),
            python_surfaces=(
                PythonSurfaceRequirement(
                    label="faster-whisper",
                    import_name="faster_whisper",
                    install_extra="asr",
                ),
                PythonSurfaceRequirement(
                    label="pyannote.audio",
                    import_name="pyannote.audio",
                    install_extra="diarization",
                ),
            ),
            required_commands=REQUIRED_COMMANDS,
            playback_commands=PLAYBACK_COMMANDS,
            minimum_ram_gb=MINIMUM_RAM_GB,
            minimum_cuda_vram_gb=MINIMUM_CUDA_VRAM_GB,
        ),
    )


def get_alpha_local_profile(ollama_model: str = OLLAMA_DEFAULT_MODEL) -> SetupProfile:
    """Return the first fixed local profile used by the Linux alpha setup flow."""

    return default_profile_catalog(ollama_model=ollama_model)[0]


def detect_command_identity(
    *,
    runner: CommandRunner | None = None,
    current_executable: Path | None = None,
    version: str = __version__,
) -> CommandIdentity:
    """Detect the current command path and the plain installed `mena` path."""

    command_runner = runner or SubprocessCommandRunner()
    current_arg = current_executable or Path(sys.argv[0])
    current = current_arg.expanduser()
    plain_mena_path = command_runner.which("mena")
    if not current.is_absolute():
        current_command = command_runner.which(str(current))
        if current_command is not None:
            current = current_command
    resolved_current = _resolve_path(current)
    resolved_plain = None if plain_mena_path is None else _resolve_path(plain_mena_path)
    return CommandIdentity(
        current_executable=resolved_current,
        plain_mena_path=resolved_plain,
        version=version,
        invocation_mode=_invocation_mode(resolved_current, resolved_plain),
    )


def detect_hardware(runner: CommandRunner | None = None) -> HardwareFacts:
    """Detect minimal hardware facts without mutating host state."""

    command_runner = runner or SubprocessCommandRunner()
    vram_gb = _detect_nvidia_vram_gb(command_runner)
    return HardwareFacts(
        system=platform.system(),
        ram_gb=_detect_ram_gb(),
        cuda_available=vram_gb is not None,
        vram_gb=vram_gb,
        avx2_available=_detect_avx2(),
    )


def detect_local_installation_state(
    settings: AppSettings,
    *,
    runner: CommandRunner | None = None,
    command_identity: CommandIdentity | None = None,
    hardware: HardwareFacts | None = None,
    env: Mapping[str, str] | None = None,
    python_executable: Path | None = None,
) -> LocalInstallationState:
    """Inspect local setup state without installing or downloading anything."""

    command_runner = runner or SubprocessCommandRunner()
    current_env = os.environ if env is None else env
    identity = command_identity or detect_command_identity(runner=command_runner)
    profile = get_alpha_local_profile(settings.local_llm.ollama_model)
    detected_hardware = hardware or detect_hardware(command_runner)
    selected_asr_profile = _select_asr_profile(detected_hardware)
    resolved_python = python_executable or Path(sys.executable)
    commands = tuple(
        CommandCheck(name=command, path=command_runner.which(command))
        for command in profile.required_commands
    )
    playback_commands = tuple(
        CommandCheck(name=command, path=command_runner.which(command))
        for command in profile.playback_commands
    )
    surfaces = tuple(
        PythonSurfaceCheck(
            requirement=requirement,
            installed=_module_installed(
                requirement.import_name,
                runner=command_runner,
                python_executable=resolved_python,
            ),
        )
        for requirement in profile.python_surfaces
    )
    ollama = _detect_ollama_state(
        settings=settings,
        command=_command_check(commands, "ollama"),
        runner=command_runner,
    )
    asr_model_present = _detect_asr_model_present(
        runner=command_runner,
        python_executable=resolved_python,
        profile=selected_asr_profile,
        python_surfaces=surfaces,
    )
    diarization_model_present = _detect_diarization_model_present(
        runner=command_runner,
        python_executable=resolved_python,
        python_surfaces=surfaces,
        env=current_env,
    )
    return LocalInstallationState(
        command_identity=identity,
        runtime_paths=settings.paths,
        hardware=detected_hardware,
        commands=commands,
        playback_commands=playback_commands,
        python_surfaces=surfaces,
        ollama=ollama,
        asr_model_present=asr_model_present,
        diarization_model_present=diarization_model_present,
        pyannote_token_present=bool(
            current_env.get("HF_TOKEN") or current_env.get("HUGGING_FACE_HUB_TOKEN")
        ),
        python_executable=resolved_python,
    )


def evaluate_setup(
    profile: SetupProfile,
    state: LocalInstallationState,
) -> SetupEvaluation:
    """Compare one profile with local state and produce a non-mutating setup plan."""

    selected_asr_profile = _select_asr_profile(state.hardware)
    readiness: list[ReadinessItem] = []
    actions: list[SetupAction] = []

    hardware_ready = _hardware_ready(profile, state.hardware)
    readiness.append(hardware_ready)
    if not hardware_ready.ready:
        return SetupEvaluation(
            profile=profile,
            selected_asr_profile=selected_asr_profile,
            state=state,
            readiness=tuple(readiness),
            plan=SetupPlan(actions=()),
        )

    _evaluate_required_commands(profile, state, readiness, actions)
    _evaluate_playback_commands(state, readiness, actions)
    _evaluate_python_surfaces(state, readiness, actions)
    _evaluate_ollama_model(state, readiness, actions)
    _evaluate_asr_model(state, selected_asr_profile, readiness, actions)
    _evaluate_diarization_model(state, readiness, actions)

    return SetupEvaluation(
        profile=profile,
        selected_asr_profile=selected_asr_profile,
        state=state,
        readiness=tuple(readiness),
        plan=SetupPlan(actions=tuple(actions)),
    )


def execute_setup_plan(
    plan: SetupPlan,
    *,
    runner: CommandRunner | None = None,
) -> tuple[SetupActionResult, ...]:
    """Execute confirmed automated setup actions and record manual actions explicitly."""

    command_runner = runner or SubprocessCommandRunner()
    results: list[SetupActionResult] = []
    for action in plan.actions:
        if action.mode == "manual":
            results.append(
                SetupActionResult(
                    action_id=action.id,
                    label=action.label,
                    status="manual_action_required",
                    detail=action.detail,
                    command=action.command,
                )
            )
            continue
        if action.mode != "automated":
            results.append(
                SetupActionResult(
                    action_id=action.id,
                    label=action.label,
                    status="skipped",
                    detail=f"Unknown setup action mode: {action.mode}",
                    command=action.command,
                )
            )
            continue
        action_env = dict(action.env) if action.env else None
        run = command_runner.run(
            action.command,
            timeout_seconds=900.0,
            env=action_env,
        )
        detail = run.stderr.strip() or run.stdout.strip() or action.detail
        results.append(
            SetupActionResult(
                action_id=action.id,
                label=action.label,
                status="installed" if run.ok else "failed",
                detail=detail,
                command=run.args,
            )
        )
    return tuple(results)


def final_provider_readiness(
    evaluation: SetupEvaluation,
    results: Sequence[SetupActionResult] = (),
) -> bool:
    """Return whether readiness is complete after optional setup action results."""

    if evaluation.ready:
        return True
    action_ids = {action.id for action in evaluation.plan.actions if action.required}
    result_by_action = {result.action_id: result for result in results}
    if not action_ids:
        return False
    for action_id in action_ids:
        result = result_by_action.get(action_id)
        if result is None or result.status != "installed":
            return False
    non_action_failures = [
        item
        for item in evaluation.readiness
        if not item.ready and not _readiness_has_action(item.id, action_ids)
    ]
    return not non_action_failures


def format_setup_report(
    evaluation: SetupEvaluation,
    *,
    action_results: Sequence[SetupActionResult] = (),
    final_ready: bool | None = None,
) -> str:
    """Format setup/preflight output for a Linux alpha user."""

    identity = evaluation.state.command_identity
    paths = evaluation.state.runtime_paths
    hardware = evaluation.state.hardware
    provider_ready = evaluation.ready if final_ready is None else final_ready
    lines = [
        "Mena Linux alpha setup preflight",
        f"Profile: {evaluation.profile.id} ({evaluation.profile.version})",
        f"Profile name: {evaluation.profile.name}",
        f"Selected ASR profile: {evaluation.selected_asr_profile.id} "
        f"({evaluation.selected_asr_profile.checkpoint}, "
        f"{evaluation.selected_asr_profile.device}/"
        f"{evaluation.selected_asr_profile.compute_type})",
        f"Current executable: {identity.current_executable}",
        f"Plain mena on PATH: {identity.plain_mena_path or '(not found)'}",
        f"Invocation mode: {identity.invocation_mode}",
        f"Version: {identity.version}",
        "Runtime paths:",
        f"  config: {paths.config_home / CONFIG_FILENAME}",
        f"  sessions: {paths.sessions_dir}",
        f"  exports: {paths.exports_dir}",
        f"  models/cache: {paths.model_cache_dir}",
        f"  logs/state: {paths.log_dir}",
        "Hardware:",
        f"  system: {hardware.system}",
        f"  RAM: {_format_gb(hardware.ram_gb)}",
        f"  CUDA: {'yes' if hardware.cuda_available else 'no'}",
        f"  VRAM: {_format_gb(hardware.vram_gb)}",
        f"  AVX2: {_format_bool(hardware.avx2_available)}",
        "Readiness:",
    ]
    for item in evaluation.readiness:
        status = "ok" if item.ready else "missing"
        lines.append(f"  [{status}] {item.label}: {item.detail}")

    lines.append("Planned setup actions:")
    if not evaluation.plan.actions:
        lines.append("  none")
    for action in evaluation.plan.actions:
        command = _format_action_command(action.command, action.env)
        lines.append(f"  [{action.mode}] {action.label}: {action.detail}{command}")

    if action_results:
        lines.append("Setup action results:")
        for result in action_results:
            command = "" if not result.command else f" command: {' '.join(result.command)}"
            lines.append(f"  [{result.status}] {result.label}: {result.detail}{command}")

    readiness = "complete" if provider_ready else "incomplete"
    lines.append(f"Local provider readiness: {readiness}")
    return "\n".join(lines) + "\n"


def _evaluate_required_commands(
    profile: SetupProfile,
    state: LocalInstallationState,
    readiness: list[ReadinessItem],
    actions: list[SetupAction],
) -> None:
    for command_name in profile.required_commands:
        check = _command_check(state.commands, command_name)
        if command_name == "mena":
            _evaluate_mena_command(state, check, readiness, actions)
            continue
        if check.present:
            readiness.append(
                ReadinessItem(
                    id=f"command:{command_name}",
                    label=f"command {command_name}",
                    ready=True,
                    detail=str(check.path),
                )
            )
            continue
        detail = _manual_command_detail(command_name)
        readiness.append(
            ReadinessItem(
                id=f"command:{command_name}",
                label=f"command {command_name}",
                ready=False,
                detail=detail,
            )
        )
        actions.append(
            SetupAction(
                id=f"command:{command_name}",
                label=f"Install command {command_name}",
                mode="manual",
                detail=detail,
            )
        )


def _evaluate_mena_command(
    state: LocalInstallationState,
    check: CommandCheck,
    readiness: list[ReadinessItem],
    actions: list[SetupAction],
) -> None:
    if check.present and state.command_identity.invocation_mode == "installed-product":
        readiness.append(
            ReadinessItem(
                id="command:mena",
                label="command mena",
                ready=True,
                detail=str(check.path),
            )
        )
        return
    if state.command_identity.invocation_mode == "installed-product":
        detail = _manual_command_detail("mena")
    else:
        detail = (
            "Run setup through the plain installed product `mena`; "
            f"current invocation mode is {state.command_identity.invocation_mode}."
        )
    readiness.append(
        ReadinessItem(
            id="command:mena",
            label="command mena",
            ready=False,
            detail=detail,
        )
    )
    actions.append(
        SetupAction(
            id="command:mena",
            label="Use installed product command mena",
            mode="manual",
            detail=detail,
        )
    )


def _evaluate_playback_commands(
    state: LocalInstallationState,
    readiness: list[ReadinessItem],
    actions: list[SetupAction],
) -> None:
    present = tuple(check for check in state.playback_commands if check.present)
    if present:
        readiness.append(
            ReadinessItem(
                id="playback-command",
                label="local playback command",
                ready=True,
                detail=", ".join(f"{check.name}={check.path}" for check in present),
            )
        )
        return
    detail = "Install at least one local playback command: ffplay, pw-play, paplay, or aplay."
    readiness.append(
        ReadinessItem(
            id="playback-command",
            label="local playback command",
            ready=False,
            detail=detail,
        )
    )
    actions.append(
        SetupAction(
            id="playback-command",
            label="Install local playback command",
            mode="manual",
            detail=detail,
        )
    )


def _evaluate_python_surfaces(
    state: LocalInstallationState,
    readiness: list[ReadinessItem],
    actions: list[SetupAction],
) -> None:
    for check in state.python_surfaces:
        requirement = check.requirement
        if check.installed:
            readiness.append(
                ReadinessItem(
                    id=f"python:{requirement.import_name}",
                    label=f"Python surface {requirement.label}",
                    ready=True,
                    detail="installed",
                )
            )
            continue
        detail = _provider_surface_repair_detail(requirement)
        readiness.append(
            ReadinessItem(
                id=f"python:{requirement.import_name}",
                label=f"Python surface {requirement.label}",
                ready=False,
                detail=detail,
            )
        )
        actions.append(
            SetupAction(
                id=f"python:{requirement.import_name}",
                label=f"Install Python surface {requirement.label}",
                mode="manual",
                detail=detail,
            )
        )


def _evaluate_ollama_model(
    state: LocalInstallationState,
    readiness: list[ReadinessItem],
    actions: list[SetupAction],
) -> None:
    if state.ollama.model_present:
        readiness.append(
            ReadinessItem(
                id="ollama-model",
                label=f"Ollama model {state.ollama.model}",
                ready=True,
                detail=state.ollama.detail,
            )
        )
        return
    readiness.append(
        ReadinessItem(
            id="ollama-model",
            label=f"Ollama model {state.ollama.model}",
            ready=False,
            detail=state.ollama.detail,
        )
    )
    if state.ollama.command.present and state.ollama.reachable:
        actions.append(
            SetupAction(
                id="ollama-model",
                label=f"Pull Ollama model {state.ollama.model}",
                mode="automated",
                detail="Download the configured local LLM model with Ollama.",
                command=("ollama", "pull", state.ollama.model),
                env=_action_env({"OLLAMA_HOST": state.ollama.endpoint}),
            )
        )
    elif state.ollama.command.present:
        actions.append(
            SetupAction(
                id="ollama-model",
                label="Start Ollama before pulling local model",
                mode="manual",
                detail=(
                    "Start the configured local Ollama service before setup can pull "
                    f"{state.ollama.model} from {state.ollama.endpoint}."
                ),
            )
        )


def _evaluate_asr_model(
    state: LocalInstallationState,
    selected_asr_profile: FasterWhisperModelProfile,
    readiness: list[ReadinessItem],
    actions: list[SetupAction],
) -> None:
    if state.asr_model_present is True:
        readiness.append(
            ReadinessItem(
                id="asr-model",
                label=f"faster-whisper model {selected_asr_profile.checkpoint}",
                ready=True,
                detail="model cache verified",
            )
        )
        return
    if not _python_surface_installed(state, "faster_whisper"):
        detail = "faster-whisper must be installed before its ASR model can be prepared."
    else:
        detail = (
            "Verify or download the selected faster-whisper model before "
            "recording-time transcription."
        )
    readiness.append(
        ReadinessItem(
            id="asr-model",
            label=f"faster-whisper model {selected_asr_profile.checkpoint}",
            ready=False,
            detail=detail,
        )
    )
    if _python_surface_installed(state, "faster_whisper"):
        actions.append(
            SetupAction(
                id="asr-model",
                label=f"Warm faster-whisper model {selected_asr_profile.checkpoint}",
                mode="automated",
                detail="Instantiate faster-whisper once so its model assets are available.",
                command=_faster_whisper_warm_command(state.python_executable, selected_asr_profile),
            )
        )


def _evaluate_diarization_model(
    state: LocalInstallationState,
    readiness: list[ReadinessItem],
    actions: list[SetupAction],
) -> None:
    if state.diarization_model_present is True:
        readiness.append(
            ReadinessItem(
                id="diarization-model",
                label=f"pyannote model {PYANNOTE_COMMUNITY_CHECKPOINT}",
                ready=True,
                detail="model cache verified",
            )
        )
        return
    if not _python_surface_installed(state, "pyannote.audio"):
        detail = "pyannote.audio must be installed before its diarization model can be prepared."
    elif not state.pyannote_token_present:
        detail = (
            "Accept the pyannote community-1 terms and expose HF_TOKEN or "
            "HUGGING_FACE_HUB_TOKEN before setup can prepare the model."
        )
    else:
        detail = "Verify or download the pyannote community-1 model before diarization."
    readiness.append(
        ReadinessItem(
            id="diarization-model",
            label=f"pyannote model {PYANNOTE_COMMUNITY_CHECKPOINT}",
            ready=False,
            detail=detail,
        )
    )
    if _python_surface_installed(state, "pyannote.audio") and state.pyannote_token_present:
        actions.append(
            SetupAction(
                id="diarization-model",
                label=f"Warm pyannote model {PYANNOTE_COMMUNITY_CHECKPOINT}",
                mode="automated",
                detail="Instantiate the pyannote community-1 pipeline once.",
                command=_pyannote_warm_command(state.python_executable),
            )
        )
    elif _python_surface_installed(state, "pyannote.audio"):
        actions.append(
            SetupAction(
                id="diarization-model",
                label="Prepare pyannote access",
                mode="manual",
                detail=detail,
            )
        )


def _hardware_ready(profile: SetupProfile, hardware: HardwareFacts) -> ReadinessItem:
    if profile.requires_linux and hardware.system != "Linux":
        return ReadinessItem(
            id="hardware",
            label="hardware and OS",
            ready=False,
            detail=f"Linux is required for this alpha profile; detected {hardware.system}.",
        )
    if hardware.ram_gb is None:
        return ReadinessItem(
            id="hardware",
            label="hardware and OS",
            ready=False,
            detail="RAM could not be detected confidently.",
        )
    if hardware.ram_gb < profile.minimum_ram_gb:
        return ReadinessItem(
            id="hardware",
            label="hardware and OS",
            ready=False,
            detail=(
                f"At least {profile.minimum_ram_gb:g} GB RAM is required; "
                f"detected {_format_gb(hardware.ram_gb)}."
            ),
        )
    gpu_ok = (
        hardware.cuda_available
        and hardware.vram_gb is not None
        and hardware.vram_gb >= profile.minimum_cuda_vram_gb
    )
    cpu_ok = hardware.avx2_available is True
    if not gpu_ok and not cpu_ok:
        if hardware.avx2_available is None:
            detail = "Neither sufficient CUDA VRAM nor AVX2 CPU fallback could be detected."
        else:
            detail = "This profile needs CUDA with enough VRAM or an AVX2-capable CPU fallback."
        return ReadinessItem(
            id="hardware",
            label="hardware and OS",
            ready=False,
            detail=detail,
        )
    return ReadinessItem(
        id="hardware",
        label="hardware and OS",
        ready=True,
        detail="Linux local profile hardware gate passed.",
    )


def _detect_ollama_state(
    *,
    settings: AppSettings,
    command: CommandCheck,
    runner: CommandRunner,
) -> OllamaState:
    if not command.present:
        return OllamaState(
            endpoint=settings.local_llm.ollama_endpoint,
            model=settings.local_llm.ollama_model,
            command=command,
            reachable=False,
            model_present=False,
            detail="Ollama command is not installed.",
        )
    endpoint_env = _action_env({"OLLAMA_HOST": settings.local_llm.ollama_endpoint})
    result = runner.run(
        ("ollama", "list"),
        timeout_seconds=15.0,
        env=dict(endpoint_env),
    )
    if not result.ok:
        detail = result.stderr.strip() or result.stdout.strip() or "ollama list failed."
        return OllamaState(
            endpoint=settings.local_llm.ollama_endpoint,
            model=settings.local_llm.ollama_model,
            command=command,
            reachable=False,
            model_present=False,
            detail=detail,
        )
    models = _parse_ollama_model_names(result.stdout)
    model_present = settings.local_llm.ollama_model in models
    detail = (
        "model present"
        if model_present
        else f"model {settings.local_llm.ollama_model} is not listed by Ollama"
    )
    return OllamaState(
        endpoint=settings.local_llm.ollama_endpoint,
        model=settings.local_llm.ollama_model,
        command=command,
        reachable=True,
        model_present=model_present,
        detail=detail,
    )


def _detect_ram_gb() -> float | None:
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        return None
    if not isinstance(pages, int) or not isinstance(page_size, int):
        return None
    if pages <= 0 or page_size <= 0:
        return None
    return pages * page_size / (1024**3)


def _detect_avx2(cpuinfo_path: Path = Path("/proc/cpuinfo")) -> bool | None:
    try:
        cpuinfo = cpuinfo_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    if not cpuinfo.strip():
        return None
    flags = cpuinfo.lower().split()
    return "avx2" in flags


def _detect_nvidia_vram_gb(runner: CommandRunner) -> float | None:
    if runner.which("nvidia-smi") is None:
        return None
    result = runner.run(
        (
            "nvidia-smi",
            "--query-gpu=memory.total",
            "--format=csv,noheader,nounits",
        ),
        timeout_seconds=10.0,
    )
    if not result.ok:
        return None
    values: list[float] = []
    for line in result.stdout.splitlines():
        try:
            values.append(float(line.strip()) / 1024)
        except ValueError:
            continue
    return max(values) if values else None


def _module_installed(
    import_name: str,
    *,
    runner: CommandRunner | None = None,
    python_executable: Path | None = None,
) -> bool:
    if runner is not None and python_executable is not None:
        code = (
            "import importlib.util, sys; "
            f"sys.exit(0 if importlib.util.find_spec({import_name!r}) is not None else 1)"
        )
        return runner.run((str(python_executable), "-c", code), timeout_seconds=30.0).ok
    try:
        return importlib.util.find_spec(import_name) is not None
    except (ImportError, AttributeError, ValueError):
        return False


def _select_asr_profile(hardware: HardwareFacts) -> FasterWhisperModelProfile:
    return select_faster_whisper_profile(
        HardwareSnapshot(
            cuda_available=hardware.cuda_available,
            vram_gb=hardware.vram_gb,
            ram_gb=hardware.ram_gb,
            avx2_available=hardware.avx2_available,
        )
    )


def _detect_asr_model_present(
    *,
    runner: CommandRunner,
    python_executable: Path,
    profile: FasterWhisperModelProfile,
    python_surfaces: Sequence[PythonSurfaceCheck],
) -> bool | None:
    if not _python_surface_installed_checks(python_surfaces, "faster_whisper"):
        return False
    result = runner.run(
        _faster_whisper_cache_check_command(python_executable, profile),
        timeout_seconds=120.0,
    )
    return result.ok


def _detect_diarization_model_present(
    *,
    runner: CommandRunner,
    python_executable: Path,
    python_surfaces: Sequence[PythonSurfaceCheck],
    env: Mapping[str, str],
) -> bool | None:
    if not _python_surface_installed_checks(python_surfaces, "pyannote.audio"):
        return False
    token = env.get("HF_TOKEN") or env.get("HUGGING_FACE_HUB_TOKEN")
    command_env = {
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_TOKEN": token or "",
        "HUGGING_FACE_HUB_TOKEN": token or "",
    }
    result = runner.run(
        _pyannote_cache_check_command(python_executable),
        timeout_seconds=120.0,
        env=command_env,
    )
    return result.ok


def _command_check(checks: Sequence[CommandCheck], command_name: str) -> CommandCheck:
    for check in checks:
        if check.name == command_name:
            return check
    return CommandCheck(name=command_name, path=None)


def _python_surface_installed(state: LocalInstallationState, import_name: str) -> bool:
    return _python_surface_installed_checks(state.python_surfaces, import_name)


def _python_surface_installed_checks(
    checks: Sequence[PythonSurfaceCheck],
    import_name: str,
) -> bool:
    return any(check.requirement.import_name == import_name and check.installed for check in checks)


def _manual_command_detail(command_name: str) -> str:
    if command_name == "mena":
        return (
            "Install the versioned alpha wheel so plain `mena` is available on PATH; "
            "repo/dev `uv run mena` is not the product install."
        )
    if command_name == "uv":
        return "Install uv for the documented wheel-based alpha install and repair path."
    if command_name == "ollama":
        return "Install and start Ollama locally; setup will not configure remote fallback."
    if command_name in {"pw-dump", "pw-cli", "wpctl"}:
        return "Install/enable the PipeWire and WirePlumber user-session audio tools."
    return f"Install required command {command_name}."


def _provider_surface_repair_detail(requirement: PythonSurfaceRequirement) -> str:
    return (
        "Reinstall the current versioned alpha wheel with the local provider extra, "
        "for example: uv tool install --reinstall "
        f"'dist/mena-{__version__}-py3-none-any.whl[{requirement.install_extra}]', "
        "or reinstall with '[local-providers]' to prepare all local provider surfaces."
    )


def _parse_ollama_model_names(output: str) -> frozenset[str]:
    names: set[str] = set()
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.lower().startswith("name "):
            continue
        names.add(stripped.split()[0])
    return frozenset(names)


def _faster_whisper_warm_command(
    python_executable: Path,
    profile: FasterWhisperModelProfile,
) -> tuple[str, ...]:
    code = (
        "from faster_whisper import WhisperModel; "
        f"WhisperModel({profile.checkpoint!r}, "
        f"device={profile.device!r}, "
        f"compute_type={profile.compute_type!r}, "
        "local_files_only=False)"
    )
    return (str(python_executable), "-c", code)


def _faster_whisper_cache_check_command(
    python_executable: Path,
    profile: FasterWhisperModelProfile,
) -> tuple[str, ...]:
    code = (
        "from faster_whisper import WhisperModel; "
        f"WhisperModel({profile.checkpoint!r}, "
        f"device={profile.device!r}, "
        f"compute_type={profile.compute_type!r}, "
        "local_files_only=True)"
    )
    return (str(python_executable), "-c", code)


def _pyannote_warm_command(python_executable: Path) -> tuple[str, ...]:
    code = (
        "import inspect, os; "
        "from pyannote.audio import Pipeline; "
        "token = os.environ.get('HF_TOKEN') or os.environ.get('HUGGING_FACE_HUB_TOKEN'); "
        "kwargs = {}; "
        "params = inspect.signature(Pipeline.from_pretrained).parameters; "
        "kwargs.update({'use_auth_token': token} if token and 'use_auth_token' in params else {}); "
        "kwargs.update({'token': token} if token and 'token' in params else {}); "
        f"Pipeline.from_pretrained({PYANNOTE_COMMUNITY_CHECKPOINT!r}, **kwargs)"
    )
    return (str(python_executable), "-c", code)


def _pyannote_cache_check_command(python_executable: Path) -> tuple[str, ...]:
    code = (
        "import inspect, os; "
        "os.environ.setdefault('HF_HUB_OFFLINE', '1'); "
        "os.environ.setdefault('TRANSFORMERS_OFFLINE', '1'); "
        "from pyannote.audio import Pipeline; "
        "token = os.environ.get('HF_TOKEN') or os.environ.get('HUGGING_FACE_HUB_TOKEN'); "
        "kwargs = {}; "
        "params = inspect.signature(Pipeline.from_pretrained).parameters; "
        "kwargs.update({'use_auth_token': token} if token and 'use_auth_token' in params else {}); "
        "kwargs.update({'token': token} if token and 'token' in params else {}); "
        f"Pipeline.from_pretrained({PYANNOTE_COMMUNITY_CHECKPOINT!r}, **kwargs)"
    )
    return (str(python_executable), "-c", code)


def _readiness_has_action(readiness_id: str, action_ids: set[str]) -> bool:
    return readiness_id in action_ids


def _resolve_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _invocation_mode(current: Path, plain_mena: Path | None) -> str:
    repo_dev_path = _looks_like_repo_dev_path(current)
    if plain_mena is not None and current == plain_mena and not repo_dev_path:
        return "installed-product"
    if repo_dev_path:
        return "repo/dev"
    return "direct-python/test"


def _looks_like_repo_dev_path(path: Path) -> bool:
    parts = path.parts
    for index, part in enumerate(parts):
        if part == ".venv" and index > 0 and parts[index - 1] == "app":
            return True
    return False


def _action_env(env: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((key, value) for key, value in env.items() if value))


def _format_action_command(
    command: Sequence[str],
    env: Sequence[tuple[str, str]] = (),
) -> str:
    if not command:
        return ""
    env_prefix = "" if not env else " env: " + " ".join(f"{key}={value}" for key, value in env)
    return f"{env_prefix} command: {' '.join(command)}"


def _format_gb(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.1f} GB"


def _format_bool(value: bool | None) -> str:
    if value is None:
        return "unknown"
    return "yes" if value else "no"
