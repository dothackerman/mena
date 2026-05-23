from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from mena.settings import RuntimePaths
from mena.setup import (
    ALPHA_LOCAL_PROFILE_ID,
    PYANNOTE_COMMUNITY_CHECKPOINT,
    CommandCheck,
    CommandIdentity,
    CommandRun,
    HardwareFacts,
    LocalInstallationState,
    OllamaState,
    PythonSurfaceCheck,
    ReadinessItem,
    SetupAction,
    SetupEvaluation,
    SetupPlan,
    SetupProfile,
    default_profile_catalog,
    evaluate_setup,
    execute_setup_plan,
    final_provider_readiness,
    get_alpha_local_profile,
)


def test_profile_catalog_exposes_fixed_linux_alpha_local_profile() -> None:
    profile = get_alpha_local_profile("llama3.2:3b")

    assert profile.id == ALPHA_LOCAL_PROFILE_ID
    assert profile.version
    assert profile.requires_linux
    assert profile.required_commands == ("mena", "uv", "ollama", "pw-dump", "pw-cli", "wpctl")
    assert profile.playback_commands == ("ffplay", "pw-play", "paplay", "aplay")
    assert profile.minimum_ram_gb == 16.0
    assert profile.stages == default_profile_catalog("llama3.2:3b")[0].stages
    assert {stage.stage for stage in profile.stages} == {
        "transcription",
        "diarization",
        "summarization",
        "semantic-review",
    }
    assert _stage_model(profile, "diarization") == PYANNOTE_COMMUNITY_CHECKPOINT
    assert _stage_model(profile, "summarization") == "llama3.2:3b"


def test_evaluation_is_pure_and_reports_ready_state_without_actions() -> None:
    profile = get_alpha_local_profile()
    state = _state(asr_model_present=True, diarization_model_present=True)

    evaluation = evaluate_setup(profile, state)

    assert evaluation.ready
    assert evaluation.plan.actions == ()
    assert evaluation.blocking_reasons == ()


def test_evaluation_distinguishes_missing_required_commands() -> None:
    profile = get_alpha_local_profile()
    state = _state(missing_commands={"ollama"})

    evaluation = evaluate_setup(profile, state)

    assert not evaluation.ready
    assert _readiness(evaluation, "command:ollama").ready is False
    assert "Install and start Ollama locally" in _readiness(evaluation, "command:ollama").detail
    assert _action(evaluation.plan, "command:ollama").mode == "manual"


def test_unsupported_hardware_stops_before_provider_setup_actions() -> None:
    profile = get_alpha_local_profile()
    state = _state(
        hardware=HardwareFacts(
            system="Linux",
            ram_gb=8.0,
            cuda_available=False,
            vram_gb=None,
            avx2_available=True,
        ),
        missing_commands={"ollama"},
        asr_model_present=False,
        diarization_model_present=False,
    )

    evaluation = evaluate_setup(profile, state)

    assert not evaluation.ready
    assert evaluation.plan.actions == ()
    assert "At least 16 GB RAM" in evaluation.blocking_reasons[0]


def test_mutation_path_uses_runner_and_records_action_results() -> None:
    runner = FakeRunner(
        results={
            ("ollama", "pull", "llama3.2:3b"): CommandRun(
                ("ollama", "pull", "llama3.2:3b"),
                0,
                stdout="pulled",
            )
        }
    )
    plan = SetupPlan(
        actions=(
            SetupAction(
                id="ollama-model",
                label="Pull Ollama model llama3.2:3b",
                mode="automated",
                detail="Download the model.",
                command=("ollama", "pull", "llama3.2:3b"),
            ),
            SetupAction(
                id="command:pw-dump",
                label="Install command pw-dump",
                mode="manual",
                detail="Install PipeWire.",
            ),
        )
    )

    results = execute_setup_plan(plan, runner=runner)

    assert runner.run_calls == [("ollama", "pull", "llama3.2:3b")]
    assert results[0].status == "installed"
    assert results[0].detail == "pulled"
    assert results[1].status == "manual_action_required"


def test_final_provider_readiness_can_complete_after_successful_automated_actions() -> None:
    profile = get_alpha_local_profile()
    state = _state(
        ollama_model_present=False,
        asr_model_present=False,
        diarization_model_present=False,
        pyannote_token_present=True,
    )
    evaluation = evaluate_setup(profile, state)
    runner = FakeRunner()

    results = execute_setup_plan(evaluation.plan, runner=runner)

    assert {result.status for result in results} == {"installed"}
    assert final_provider_readiness(evaluation, results)
    assert ("ollama", "pull", "llama3.2:3b") in runner.run_calls
    assert any(call[1] == "-c" and "faster_whisper" in call[2] for call in runner.run_calls)
    assert any(call[1] == "-c" and "pyannote.audio" in call[2] for call in runner.run_calls)


def test_manual_action_keeps_provider_readiness_incomplete() -> None:
    profile = get_alpha_local_profile()
    state = _state(
        python_surfaces={"faster_whisper": False, "pyannote.audio": True},
        asr_model_present=False,
        diarization_model_present=False,
        pyannote_token_present=False,
    )
    evaluation = evaluate_setup(profile, state)

    results = execute_setup_plan(evaluation.plan, runner=FakeRunner())

    assert any(result.status == "manual_action_required" for result in results)
    assert not final_provider_readiness(evaluation, results)


class FakeRunner:
    def __init__(
        self,
        *,
        results: dict[tuple[str, ...], CommandRun] | None = None,
    ) -> None:
        self.results = results or {}
        self.run_calls: list[tuple[str, ...]] = []

    def which(self, command: str) -> Path | None:
        return Path("/usr/bin") / command

    def run(self, args: Sequence[str], *, timeout_seconds: float = 30.0) -> CommandRun:
        command = tuple(args)
        self.run_calls.append(command)
        return self.results.get(command, CommandRun(command, 0, stdout="ok"))


def _state(
    *,
    hardware: HardwareFacts | None = None,
    missing_commands: set[str] | None = None,
    python_surfaces: dict[str, bool] | None = None,
    ollama_model_present: bool = True,
    asr_model_present: bool | None = True,
    diarization_model_present: bool | None = True,
    pyannote_token_present: bool = False,
) -> LocalInstallationState:
    profile = get_alpha_local_profile()
    missing = missing_commands or set()
    surface_presence = python_surfaces or {
        "faster_whisper": True,
        "pyannote.audio": True,
    }
    commands = tuple(
        CommandCheck(
            name=command,
            path=None if command in missing else Path("/usr/bin") / command,
        )
        for command in profile.required_commands
    )
    playback_commands = tuple(
        CommandCheck(
            name=command,
            path=Path("/usr/bin") / command if command == "ffplay" else None,
        )
        for command in profile.playback_commands
    )
    return LocalInstallationState(
        command_identity=CommandIdentity(
            current_executable=Path("/home/user/.local/bin/mena"),
            plain_mena_path=Path("/home/user/.local/bin/mena"),
            version="0.1.0",
            invocation_mode="installed-product",
        ),
        runtime_paths=_runtime_paths(Path("/home/user")),
        hardware=hardware
        or HardwareFacts(
            system="Linux",
            ram_gb=32.0,
            cuda_available=True,
            vram_gb=12.0,
            avx2_available=True,
        ),
        commands=commands,
        playback_commands=playback_commands,
        python_surfaces=tuple(
            PythonSurfaceCheck(
                requirement=requirement,
                installed=surface_presence[requirement.import_name],
            )
            for requirement in profile.python_surfaces
        ),
        ollama=OllamaState(
            endpoint="http://localhost:11434",
            model="llama3.2:3b",
            command=CommandCheck(
                "ollama", None if "ollama" in missing else Path("/usr/bin/ollama")
            ),
            reachable="ollama" not in missing,
            model_present=ollama_model_present,
            detail=(
                "model present"
                if ollama_model_present
                else "model llama3.2:3b is not listed by Ollama"
            ),
        ),
        asr_model_present=asr_model_present,
        diarization_model_present=diarization_model_present,
        pyannote_token_present=pyannote_token_present,
        python_executable=Path("/usr/bin/python3"),
    )


def _runtime_paths(home: Path) -> RuntimePaths:
    return RuntimePaths(
        data_home=home / ".local/share/mena",
        sessions_dir=home / ".local/share/mena/sessions",
        exports_dir=home / ".local/share/mena/exports",
        config_home=home / ".config/mena",
        cache_home=home / ".cache/mena",
        model_cache_dir=home / ".cache/mena/models",
        state_home=home / ".local/state/mena",
        log_dir=home / ".local/state/mena/logs",
    )


def _stage_model(profile: SetupProfile, stage: str) -> str:
    for item in profile.stages:
        if item.stage == stage:
            return item.model
    raise AssertionError(f"missing stage {stage}")


def _readiness(evaluation: SetupEvaluation, readiness_id: str) -> ReadinessItem:
    for item in evaluation.readiness:
        if item.id == readiness_id:
            return item
    raise AssertionError(f"missing readiness {readiness_id}")


def _action(plan: SetupPlan, action_id: str) -> SetupAction:
    for item in plan.actions:
        if item.id == action_id:
            return item
    raise AssertionError(f"missing action {action_id}")
