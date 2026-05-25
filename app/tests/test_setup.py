from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest

from mena import __version__
from mena.settings import AppSettings, RuntimePaths
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
    SetupActionResult,
    SetupEvaluation,
    SetupPlan,
    SetupProfile,
    SubprocessCommandRunner,
    _detect_avx2,
    _detect_nvidia_vram_gb,
    _parse_ollama_model_names,
    default_profile_catalog,
    detect_command_identity,
    detect_local_installation_state,
    evaluate_setup,
    execute_setup_plan,
    final_provider_readiness,
    format_setup_report,
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
    ollama_call_index = runner.run_calls.index(("ollama", "pull", "llama3.2:3b"))
    assert runner.run_envs[ollama_call_index]["OLLAMA_HOST"] == "http://localhost:11434"
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


def test_subprocess_runner_wraps_which_and_command_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = SubprocessCommandRunner()

    def _which(command: str) -> str | None:
        return "/bin/echo" if command == "echo" else None

    monkeypatch.setattr("mena.setup.shutil.which", _which)
    assert runner.which("echo") == Path("/bin/echo")
    assert runner.which("missing") is None

    monkeypatch.setattr(
        "mena.setup.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=3, stdout="out", stderr="err"),
    )
    failed = runner.run(("tool", "arg"))
    assert failed.exit_code == 3
    assert failed.stdout == "out"
    assert failed.stderr == "err"

    def _timeout(*_args: object, **_kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd=("slow",), timeout=1.0, output="partial")

    monkeypatch.setattr("mena.setup.subprocess.run", _timeout)
    timed_out = runner.run(("slow",), timeout_seconds=1.0)
    assert timed_out.exit_code == 124
    assert timed_out.stdout == "partial"
    assert timed_out.stderr == "Command timed out."

    def _os_error(*_args: object, **_kwargs: object) -> object:
        raise OSError("not executable")

    monkeypatch.setattr("mena.setup.subprocess.run", _os_error)
    os_error = runner.run(("broken",))
    assert os_error.exit_code == 127
    assert os_error.stderr == "not executable"


def test_detect_command_identity_distinguishes_product_dev_and_direct_invocations() -> None:
    runner = FakeRunner(paths={"mena": Path("/opt/mena/bin/mena")})

    dev = detect_command_identity(
        runner=runner,
        current_executable=Path("/repo/app/.venv/bin/mena"),
        version="0.1.0",
    )
    product = detect_command_identity(
        runner=runner,
        current_executable=Path("/opt/mena/bin/mena"),
        version="0.1.0",
    )
    direct = detect_command_identity(
        runner=FakeRunner(paths={}),
        current_executable=Path("/usr/bin/python3"),
        version="0.1.0",
    )

    assert dev.invocation_mode == "repo/dev"
    assert product.invocation_mode == "installed-product"
    assert direct.invocation_mode == "direct-python/test"
    assert product.plain_mena_path == Path("/opt/mena/bin/mena")

    repo_venv = detect_command_identity(
        runner=FakeRunner(paths={"mena": Path("/repo/app/.venv/bin/mena")}),
        current_executable=Path("mena"),
        version="0.1.0",
    )
    external_venv_product = detect_command_identity(
        runner=FakeRunner(paths={"mena": Path("/home/user/.venv/bin/mena")}),
        current_executable=Path("mena"),
        version="0.1.0",
    )
    site_packages_module = detect_command_identity(
        runner=FakeRunner(paths={}),
        current_executable=Path("/home/user/.venv/lib/python3.13/site-packages/mena/__main__.py"),
        version="0.1.0",
    )

    assert repo_venv.current_executable == Path("/repo/app/.venv/bin/mena")
    assert repo_venv.invocation_mode == "repo/dev"
    assert external_venv_product.current_executable == Path("/home/user/.venv/bin/mena")
    assert external_venv_product.invocation_mode == "installed-product"
    assert site_packages_module.invocation_mode == "direct-python/test"


@pytest.mark.parametrize(
    ("current_executable", "plain_mena_path", "expected_mode"),
    [
        (Path("/repo/app/.venv/bin/mena"), Path("/repo/app/.venv/bin/mena"), "repo/dev"),
        (Path("/home/user/.venv/bin/mena"), Path("/home/user/.venv/bin/mena"), "installed-product"),
        (
            Path("/home/user/.venv/lib/python3.13/site-packages/mena/__main__.py"),
            None,
            "direct-python/test",
        ),
        (Path("/opt/mena/bin/python"), Path("/opt/mena/bin/mena"), "direct-python/test"),
    ],
)
def test_detect_command_identity_boundary_matrix_for_setup_modes(
    current_executable: Path,
    plain_mena_path: Path | None,
    expected_mode: str,
) -> None:
    runner = FakeRunner(paths={} if plain_mena_path is None else {"mena": plain_mena_path})

    identity = detect_command_identity(
        runner=runner,
        current_executable=current_executable,
        version="0.1.0",
    )

    assert identity.invocation_mode == expected_mode
    assert identity.current_executable == current_executable
    assert identity.plain_mena_path == plain_mena_path


def test_repo_dev_invocation_does_not_satisfy_product_command_readiness() -> None:
    profile = get_alpha_local_profile()
    state = _state(
        command_identity=CommandIdentity(
            current_executable=Path("/repo/app/.venv/bin/mena"),
            plain_mena_path=Path("/repo/app/.venv/bin/mena"),
            version="0.1.0",
            invocation_mode="repo/dev",
        )
    )

    evaluation = evaluate_setup(profile, state)

    assert not evaluation.ready
    assert _readiness(evaluation, "command:mena").ready is False
    assert "current invocation mode is repo/dev" in _readiness(evaluation, "command:mena").detail
    assert _action(evaluation.plan, "command:mena").mode == "manual"


def test_detect_local_installation_state_uses_runner_env_and_configured_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(
        env={"MENA_OLLAMA_ENDPOINT": "http://127.0.0.1:11435"},
        home=tmp_path,
    )
    profile = get_alpha_local_profile()
    runner = FakeRunner(
        results={
            ("ollama", "list"): CommandRun(
                ("ollama", "list"),
                0,
                stdout="NAME ID SIZE MODIFIED\nllama3.2:3b abc 2 GB now\n",
            )
        }
    )

    monkeypatch.setattr(
        "mena.setup._module_installed",
        lambda import_name, **_kwargs: import_name == "faster_whisper",
    )

    state = detect_local_installation_state(
        settings,
        runner=runner,
        command_identity=CommandIdentity(
            current_executable=Path("/bin/mena"),
            plain_mena_path=Path("/bin/mena"),
            version="0.1.0",
            invocation_mode="installed-product",
        ),
        hardware=HardwareFacts(
            system="Linux",
            ram_gb=32.0,
            cuda_available=False,
            vram_gb=None,
            avx2_available=True,
        ),
        env={"HF_TOKEN": "token"},
        python_executable=Path("/python"),
    )

    assert tuple(check.name for check in state.commands) == profile.required_commands
    assert state.runtime_paths == settings.paths
    assert state.ollama.model_present
    assert state.pyannote_token_present
    assert state.asr_model_present is True
    assert state.diarization_model_present is False
    assert [check.installed for check in state.python_surfaces] == [True, False]
    assert state.python_executable == Path("/python")
    assert runner.run_calls[0] == ("ollama", "list")
    assert runner.run_envs[0]["OLLAMA_HOST"] == "http://127.0.0.1:11435"
    assert any(call[1] == "-c" and "faster_whisper" in call[2] for call in runner.run_calls)


def test_detect_local_installation_state_probes_surfaces_with_target_interpreter(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    runner = FakeRunner()

    detect_local_installation_state(
        settings,
        runner=runner,
        command_identity=CommandIdentity(
            current_executable=Path("/bin/mena"),
            plain_mena_path=Path("/bin/mena"),
            version="0.1.0",
            invocation_mode="installed-product",
        ),
        hardware=HardwareFacts(
            system="Linux",
            ram_gb=32.0,
            cuda_available=False,
            vram_gb=None,
            avx2_available=True,
        ),
        python_executable=Path("/target/python"),
    )

    assert any(
        call[0] == "/target/python"
        and call[1] == "-c"
        and "importlib.util.find_spec('faster_whisper')" in call[2]
        for call in runner.run_calls
    )
    assert any(
        call[0] == "/target/python"
        and call[1] == "-c"
        and "importlib.util.find_spec('pyannote.audio')" in call[2]
        for call in runner.run_calls
    )


def test_diarization_cache_probe_clears_ambient_tokens_when_env_has_none(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    runner = FakeRunner()

    detect_local_installation_state(
        settings,
        runner=runner,
        command_identity=CommandIdentity(
            current_executable=Path("/bin/mena"),
            plain_mena_path=Path("/bin/mena"),
            version="0.1.0",
            invocation_mode="installed-product",
        ),
        hardware=HardwareFacts(
            system="Linux",
            ram_gb=32.0,
            cuda_available=False,
            vram_gb=None,
            avx2_available=True,
        ),
        env={},
        python_executable=Path("/target/python"),
    )

    pyannote_call_index = next(
        index
        for index, call in enumerate(runner.run_calls)
        if len(call) > 2 and "Pipeline.from_pretrained" in call[2]
    )
    assert runner.run_envs[pyannote_call_index]["HF_TOKEN"] == ""
    assert runner.run_envs[pyannote_call_index]["HUGGING_FACE_HUB_TOKEN"] == ""


def test_detection_helpers_parse_cpu_gpu_and_ollama_outputs(tmp_path: Path) -> None:
    cpuinfo = tmp_path / "cpuinfo"
    cpuinfo.write_text("flags\t: fpu sse avx2 sse4\n", encoding="utf-8")
    tail_cpuinfo = tmp_path / "tail-cpuinfo"
    tail_cpuinfo.write_text("flags\t: fpu sse avx2\n", encoding="utf-8")
    empty_cpuinfo = tmp_path / "empty-cpuinfo"
    empty_cpuinfo.write_text("", encoding="utf-8")
    runner = FakeRunner(
        paths={"nvidia-smi": Path("/usr/bin/nvidia-smi")},
        results={
            (
                "nvidia-smi",
                "--query-gpu=memory.total",
                "--format=csv,noheader,nounits",
            ): CommandRun(
                (
                    "nvidia-smi",
                    "--query-gpu=memory.total",
                    "--format=csv,noheader,nounits",
                ),
                0,
                stdout="8192\nnot-a-number\n4096\n",
            )
        },
    )

    assert _detect_avx2(cpuinfo) is True
    assert _detect_avx2(tail_cpuinfo) is True
    assert _detect_avx2(empty_cpuinfo) is None
    assert _detect_avx2(tmp_path / "missing") is None
    assert _detect_nvidia_vram_gb(FakeRunner(paths={})) is None
    assert _detect_nvidia_vram_gb(runner) == 8.0
    assert _parse_ollama_model_names("NAME ID\nllama3.2:3b abc\n\nother:1b def\n") == frozenset(
        {"llama3.2:3b", "other:1b"}
    )


def test_evaluation_reports_manual_actions_for_playback_and_python_surfaces() -> None:
    profile = get_alpha_local_profile()
    state = _state(
        playback_present=False,
        python_surfaces={"faster_whisper": False, "pyannote.audio": False},
        asr_model_present=False,
        diarization_model_present=False,
    )

    evaluation = evaluate_setup(profile, state)

    assert _action(evaluation.plan, "playback-command").mode == "manual"
    assert _action(evaluation.plan, "python:faster_whisper").mode == "manual"
    assert _action(evaluation.plan, "python:pyannote.audio").mode == "manual"
    assert "local provider extra" in _readiness(evaluation, "python:faster_whisper").detail
    assert "faster-whisper must be installed" in _readiness(evaluation, "asr-model").detail
    assert f"mena-{__version__}" in _readiness(evaluation, "python:faster_whisper").detail


def test_unreachable_ollama_requires_manual_start_before_pull() -> None:
    profile = get_alpha_local_profile()
    state = _state(ollama_model_present=False, ollama_reachable=False)

    evaluation = evaluate_setup(profile, state)

    action = _action(evaluation.plan, "ollama-model")
    assert action.mode == "manual"
    assert action.command == ()
    assert "Start the configured local Ollama service" in action.detail


def test_evaluation_reports_hardware_detection_failures_without_setup_actions() -> None:
    profile = get_alpha_local_profile()
    cases = [
        (
            HardwareFacts(
                system="Darwin",
                ram_gb=32.0,
                cuda_available=False,
                vram_gb=None,
                avx2_available=True,
            ),
            "Linux is required",
        ),
        (
            HardwareFacts(
                system="Linux",
                ram_gb=None,
                cuda_available=False,
                vram_gb=None,
                avx2_available=True,
            ),
            "RAM could not be detected",
        ),
        (
            HardwareFacts(
                system="Linux",
                ram_gb=32.0,
                cuda_available=False,
                vram_gb=None,
                avx2_available=None,
            ),
            "Neither sufficient CUDA VRAM nor AVX2",
        ),
        (
            HardwareFacts(
                system="Linux",
                ram_gb=32.0,
                cuda_available=False,
                vram_gb=None,
                avx2_available=False,
            ),
            "needs CUDA with enough VRAM or an AVX2-capable CPU",
        ),
    ]

    for hardware, message in cases:
        evaluation = evaluate_setup(profile, _state(hardware=hardware))
        assert not evaluation.ready
        assert evaluation.plan.actions == ()
        assert message in evaluation.blocking_reasons[0]


def test_execute_plan_records_failed_and_unknown_action_modes() -> None:
    plan = SetupPlan(
        actions=(
            SetupAction(
                id="automated",
                label="Automated action",
                mode="automated",
                detail="run it",
                command=("tool",),
            ),
            SetupAction(
                id="custom",
                label="Custom action",
                mode="custom",
                detail="unknown",
            ),
        )
    )
    runner = FakeRunner(results={("tool",): CommandRun(("tool",), 2, stderr="failed")})

    results = execute_setup_plan(plan, runner=runner)

    assert results[0].status == "failed"
    assert results[0].detail == "failed"
    assert results[1].status == "skipped"
    assert "Unknown setup action mode" in results[1].detail


def test_format_setup_report_includes_action_results() -> None:
    profile = get_alpha_local_profile()
    evaluation = evaluate_setup(profile, _state(ollama_model_present=False))
    report = format_setup_report(
        evaluation,
        action_results=(
            SetupActionResult(
                action_id="ollama-model",
                label="Pull Ollama model llama3.2:3b",
                status="failed",
                detail="network unavailable",
                command=("ollama", "pull", "llama3.2:3b"),
            ),
        ),
        final_ready=False,
    )

    assert "Setup action results:" in report
    assert "network unavailable" in report
    assert "command: ollama pull llama3.2:3b" in report
    assert report.endswith("Local provider readiness: incomplete\n")


class FakeRunner:
    def __init__(
        self,
        *,
        results: dict[tuple[str, ...], CommandRun] | None = None,
        paths: dict[str, Path] | None = None,
    ) -> None:
        self.results = results or {}
        self.paths = paths
        self.run_calls: list[tuple[str, ...]] = []
        self.run_envs: list[dict[str, str]] = []

    def which(self, command: str) -> Path | None:
        if self.paths is not None:
            return self.paths.get(command)
        return Path("/usr/bin") / command

    def run(
        self,
        args: Sequence[str],
        *,
        timeout_seconds: float = 30.0,
        env: Mapping[str, str] | None = None,
    ) -> CommandRun:
        command = tuple(args)
        self.run_calls.append(command)
        self.run_envs.append(dict(env or {}))
        return self.results.get(command, CommandRun(command, 0, stdout="ok"))


def _state(
    *,
    command_identity: CommandIdentity | None = None,
    hardware: HardwareFacts | None = None,
    missing_commands: set[str] | None = None,
    python_surfaces: dict[str, bool] | None = None,
    playback_present: bool = True,
    ollama_model_present: bool = True,
    ollama_reachable: bool = True,
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
            path=Path("/usr/bin") / command if playback_present and command == "ffplay" else None,
        )
        for command in profile.playback_commands
    )
    return LocalInstallationState(
        command_identity=command_identity
        or CommandIdentity(
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
            reachable="ollama" not in missing and ollama_reachable,
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
