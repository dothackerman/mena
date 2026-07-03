from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from mena.interaction import ScriptedInteraction, ScriptedResponse
from mena.settings import AppSettings
from mena.setup import (
    CommandCheck,
    CommandIdentity,
    CommandRun,
    HardwareFacts,
    LocalInstallationState,
    OllamaState,
    PythonSurfaceCheck,
    SetupProfile,
    get_alpha_local_profile,
    load_runtime_profile_state,
)
from mena.setup_detection import SetupDetector
from mena.setup_workflow import (
    SetupMessageEvent,
    SetupReportEvent,
    SetupWorkflowOptions,
    run_setup_workflow,
)


def test_setup_workflow_decline_does_not_mutate_or_persist(tmp_path: Path) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    state = _setup_state(settings=settings, ollama_model_present=False)
    runner = FakeRunner()
    interaction = ScriptedInteraction(
        (ScriptedResponse("confirm", "setup.primary_actions", False),)
    )

    result = run_setup_workflow(
        settings=settings,
        options=SetupWorkflowOptions(),
        interaction=interaction,
        runner=runner,
        detector=FakeDetector(primary_states=[state]),
    )

    assert result.exit_code == 1
    assert runner.run_calls == []
    assert not (settings.paths.state_home / "setup-state.json").exists()
    assert "Setup declined; local provider readiness is incomplete." in _messages(result.events)


def test_setup_workflow_executes_primary_actions_then_persists_ready_state(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    initial = _setup_state(
        settings=settings,
        ollama_model_present=False,
        asr_model_present=False,
        diarization_model_present=False,
        pyannote_token_present=True,
    )
    ready = _setup_state(settings=settings, pyannote_token_present=True)
    runner = FakeRunner()

    result = run_setup_workflow(
        settings=settings,
        options=SetupWorkflowOptions(assume_yes=True),
        interaction=ScriptedInteraction(),
        runner=runner,
        detector=FakeDetector(primary_states=[initial, ready], fallback_states=[ready]),
    )

    assert result.exit_code == 0
    assert ("ollama", "pull", "llama3.2:3b") in runner.run_calls
    assert (settings.paths.state_home / "setup-state.json").is_file()
    accepted = load_runtime_profile_state(settings.paths)
    assert accepted.fallback_authorization.local_status == "configured_unauthorized"
    assert accepted.fallback_authorization.automatic_local_fallback_authorized is False
    assert any(isinstance(event, SetupReportEvent) for event in result.events)
    messages = _messages(result.events)
    assert "Setup is complete. Mena is ready to record." in messages
    assert "Next command: mena record" in messages
    assert "Optional local fallback policy: not enabled; rerun setup with" in messages


def test_setup_workflow_executes_fallback_actions_before_authorizing(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    primary_ready = _setup_state(settings=settings, pyannote_token_present=True)
    fallback_missing_asr = _setup_state(
        settings=settings,
        asr_model_present=False,
        pyannote_token_present=True,
    )
    fallback_ready = _setup_state(settings=settings, pyannote_token_present=True)
    runner = FakeRunner()

    result = run_setup_workflow(
        settings=settings,
        options=SetupWorkflowOptions(assume_yes=True, authorize_local_fallback=True),
        interaction=ScriptedInteraction(),
        runner=runner,
        detector=FakeDetector(
            primary_states=[primary_ready],
            fallback_states=[fallback_missing_asr, fallback_ready],
        ),
    )

    accepted = load_runtime_profile_state(settings.paths)
    assert result.exit_code == 0
    assert any(call[1] == "-c" and "WhisperModel('small'" in call[2] for call in runner.run_calls)
    assert accepted.fallback_authorization.local_status == "authorized"
    assert accepted.fallback_authorization.automatic_local_fallback_authorized is True
    assert "Automatic local runtime fallback authorized for:" in _messages(result.events)
    assert "Setup is complete. Mena is ready to record." in _messages(result.events)
    assert "Optional local fallback policy: enabled" in _messages(result.events)


def test_setup_workflow_fallback_decline_persists_unavailable_authorization(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    primary_ready = _setup_state(settings=settings, pyannote_token_present=True)
    fallback_missing_asr = _setup_state(
        settings=settings,
        asr_model_present=False,
        pyannote_token_present=True,
    )
    runner = FakeRunner()
    interaction = ScriptedInteraction(
        (ScriptedResponse("confirm", "setup.fallback_actions", False),)
    )

    result = run_setup_workflow(
        settings=settings,
        options=SetupWorkflowOptions(authorize_local_fallback=True),
        interaction=interaction,
        runner=runner,
        detector=FakeDetector(
            primary_states=[primary_ready],
            fallback_states=[fallback_missing_asr],
        ),
    )

    accepted = load_runtime_profile_state(settings.paths)
    assert result.exit_code == 0
    assert runner.run_calls == []
    assert accepted.fallback_authorization.local_status == "unavailable"
    assert accepted.fallback_authorization.automatic_local_fallback_authorized is False
    assert "Fallback setup actions declined" in _messages(result.events)


class FakeDetector(SetupDetector):
    def __init__(
        self,
        *,
        primary_states: Sequence[LocalInstallationState],
        fallback_states: Sequence[LocalInstallationState] = (),
    ) -> None:
        self.primary_states = list(primary_states)
        self.fallback_states = list(fallback_states)

    def detect(
        self,
        settings: AppSettings,
        *,
        runner: object,
        profile: SetupProfile | None = None,
    ) -> LocalInstallationState:
        _ = settings, runner
        states = (
            self.fallback_states
            if profile is not None and profile.fallback
            else self.primary_states
        )
        if not states:
            raise AssertionError("No scripted setup state remains.")
        return states.pop(0)


class FakeRunner:
    def __init__(self) -> None:
        self.run_calls: list[tuple[str, ...]] = []
        self.run_envs: list[dict[str, str]] = []

    def which(self, command: str) -> Path | None:
        return Path("/usr/bin") / command

    def run(
        self,
        args: Sequence[str],
        *,
        timeout_seconds: float = 30.0,
        env: Mapping[str, str] | None = None,
    ) -> CommandRun:
        _ = timeout_seconds
        command = tuple(args)
        self.run_calls.append(command)
        self.run_envs.append(dict(env or {}))
        return CommandRun(command, 0, stdout="ok")


def _setup_state(
    *,
    settings: AppSettings,
    command_identity: CommandIdentity | None = None,
    hardware: HardwareFacts | None = None,
    ollama_model_present: bool = True,
    asr_model_present: bool | None = True,
    diarization_model_present: bool | None = True,
    pyannote_token_present: bool = False,
) -> LocalInstallationState:
    profile = get_alpha_local_profile(settings.local_llm.ollama_model)
    surface_presence = {
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


def _messages(events: Sequence[object]) -> str:
    return "\n".join(event.text for event in events if isinstance(event, SetupMessageEvent))
