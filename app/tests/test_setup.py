from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import cast

import pytest

from mena.mistral_llm import (
    MISTRAL_SUMMARY_ALLOWLIST_POLICY,
    MISTRAL_SUMMARY_REQUEST_MODEL,
    MistralSummaryState,
)
from mena.settings import AppSettings, RuntimePaths
from mena.setup import (
    ALPHA_LOCAL_PROFILE_ID,
    FALLBACK_LOCAL_PROFILE_ID,
    MISTRAL_SUMMARY_PROFILE_ID,
    PYANNOTE_COMMUNITY_CHECKPOINT,
    RUNTIME_PROFILE_CATALOG_VERSION,
    CommandCheck,
    CommandIdentity,
    CommandRun,
    HardwareFacts,
    LocalInstallationState,
    OllamaState,
    PythonSurfaceCheck,
    ReadinessItem,
    RuntimeProfileState,
    SetupAction,
    SetupActionResult,
    SetupEvaluation,
    SetupPlan,
    SetupProfile,
    SubprocessCommandRunner,
    _detect_avx2,
    _detect_nvidia_vram_gb,
    _parse_ollama_model_names,
    _pyannote_cache_check_command,
    build_runtime_profile_state,
    default_profile_catalog,
    detect_command_identity,
    detect_local_installation_state,
    evaluate_setup,
    execute_setup_plan,
    final_provider_readiness,
    format_setup_report,
    get_alpha_local_profile,
    get_mistral_summary_profile,
    load_runtime_profile_state,
    local_llm_stage_model,
    mistral_summary_stage_profile,
    persist_runtime_profile_state,
)


def test_profile_catalog_exposes_fixed_linux_alpha_local_profile() -> None:
    profile = get_alpha_local_profile("llama3.2:3b")

    assert profile.id == ALPHA_LOCAL_PROFILE_ID
    assert profile.version
    assert profile.catalog_version == RUNTIME_PROFILE_CATALOG_VERSION
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


def test_profile_catalog_exposes_supported_cpu_fallback_profile() -> None:
    fallback = default_profile_catalog("llama3.2:3b")[1]

    assert fallback.id == FALLBACK_LOCAL_PROFILE_ID
    assert fallback.fallback
    assert fallback.catalog_version == RUNTIME_PROFILE_CATALOG_VERSION
    assert fallback.minimum_cuda_vram_gb == 0.0
    assert _stage_model(fallback, "transcription") == "small"


def test_cpu_fallback_profile_requires_cpu_capable_hardware() -> None:
    fallback = default_profile_catalog("llama3.2:3b")[1]
    evaluation = evaluate_setup(
        fallback,
        _state(
            hardware=HardwareFacts(
                system="Linux",
                ram_gb=32.0,
                cuda_available=True,
                vram_gb=24.0,
                avx2_available=False,
            ),
            asr_model_present=True,
            diarization_model_present=True,
        ),
    )

    assert not evaluation.ready
    assert "requires an AVX2-capable CPU" in evaluation.blocking_reasons[0]


def test_evaluation_is_pure_and_reports_ready_state_without_actions() -> None:
    profile = get_alpha_local_profile()
    state = _state(asr_model_present=True, diarization_model_present=True)

    evaluation = evaluate_setup(profile, state)

    assert evaluation.ready
    assert evaluation.plan.actions == ()
    assert evaluation.blocking_reasons == ()


def test_runtime_profile_state_persists_under_runtime_state_home(tmp_path: Path) -> None:
    profile = get_alpha_local_profile()
    state = _state(runtime_paths=_runtime_paths(tmp_path), asr_model_present=True)
    evaluation = evaluate_setup(profile, state)
    fallback_evaluation = evaluate_setup(default_profile_catalog()[1], state)

    accepted = build_runtime_profile_state(
        evaluation,
        fallback_evaluation=fallback_evaluation,
        authorize_local_fallback=True,
    )
    state_path = persist_runtime_profile_state(accepted, paths=state.runtime_paths)
    loaded = load_runtime_profile_state(state.runtime_paths)

    assert state_path == state.runtime_paths.state_home / "setup-state.json"
    assert loaded.accepted_profile_id == profile.id
    assert loaded.catalog_version == RUNTIME_PROFILE_CATALOG_VERSION
    assert loaded.evidence_digest == accepted.evidence_digest
    assert loaded.selected_asr_profile.id == evaluation.selected_asr_profile.id
    assert loaded.fallback_authorization.local_status == "authorized"
    assert loaded.fallback_authorization.local_profile_id == FALLBACK_LOCAL_PROFILE_ID
    assert loaded.fallback_authorization.automatic_local_fallback_authorized is True
    assert set(loaded.fallback_authorization.stage_scope) == {"transcription", "diarization"}
    assert loaded.fallback_authorization.remote_status == "not_configured"


def test_runtime_profile_state_requires_explicit_local_fallback_authorization() -> None:
    profile = get_alpha_local_profile()
    state = _state(asr_model_present=True, diarization_model_present=True)
    evaluation = evaluate_setup(profile, state)
    fallback_evaluation = evaluate_setup(default_profile_catalog()[1], state)

    accepted = build_runtime_profile_state(
        evaluation,
        fallback_evaluation=fallback_evaluation,
    )

    assert accepted.fallback_authorization.local_status == "configured_unauthorized"
    assert accepted.fallback_authorization.local_profile_id == FALLBACK_LOCAL_PROFILE_ID
    assert accepted.fallback_authorization.automatic_local_fallback_authorized is False
    assert (
        "automatic local fallback was not authorized by setup"
        in accepted.fallback_authorization.blocking_reasons
    )


def test_runtime_profile_state_does_not_authorize_unevaluated_local_fallback() -> None:
    profile = get_alpha_local_profile()
    evaluation = evaluate_setup(profile, _state(asr_model_present=True))

    accepted = build_runtime_profile_state(evaluation)

    assert accepted.fallback_authorization.local_status == "configured_unauthorized"
    assert accepted.fallback_authorization.local_profile_id == FALLBACK_LOCAL_PROFILE_ID
    assert accepted.fallback_authorization.automatic_local_fallback_authorized is False


def test_runtime_profile_state_records_unavailable_evaluated_local_fallback() -> None:
    profile = get_alpha_local_profile()
    fallback_profile = default_profile_catalog()[1]
    state = _state(
        asr_model_present=True,
        diarization_model_present=True,
        hardware=HardwareFacts(
            system="Linux",
            ram_gb=32.0,
            cuda_available=True,
            vram_gb=12.0,
            avx2_available=False,
        ),
    )
    evaluation = evaluate_setup(
        profile,
        state,
    )
    fallback_evaluation = evaluate_setup(
        fallback_profile,
        state,
    )

    accepted = build_runtime_profile_state(
        evaluation,
        fallback_evaluation=fallback_evaluation,
    )

    assert accepted.fallback_authorization.local_status == "unavailable"
    assert accepted.fallback_authorization.local_profile_id == FALLBACK_LOCAL_PROFILE_ID
    assert accepted.fallback_authorization.automatic_local_fallback_authorized is False
    assert "requires an AVX2-capable CPU" in accepted.fallback_authorization.blocking_reasons[0]


def test_runtime_profile_state_rejects_incomplete_setup() -> None:
    profile = get_alpha_local_profile()
    evaluation = evaluate_setup(profile, _state(missing_commands={"ollama"}))

    with pytest.raises(ValueError, match="ready setup evaluation"):
        build_runtime_profile_state(evaluation)


def test_runtime_profile_state_load_rejects_invalid_json(tmp_path: Path) -> None:
    paths = _runtime_paths(tmp_path)
    paths.state_home.mkdir(parents=True)
    (paths.state_home / "setup-state.json").write_text("{not-json", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid JSON"):
        load_runtime_profile_state(paths)


def test_runtime_profile_state_load_rejects_invalid_utf8(tmp_path: Path) -> None:
    paths = _runtime_paths(tmp_path)
    paths.state_home.mkdir(parents=True)
    (paths.state_home / "setup-state.json").write_bytes(b"\xff\xfe")

    with pytest.raises(ValueError, match="not valid UTF-8"):
        load_runtime_profile_state(paths)


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
    assert ("ollama", "pull", "llama3.1:8b") in runner.run_calls
    ollama_call_index = runner.run_calls.index(("ollama", "pull", "llama3.1:8b"))
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
                stdout="NAME ID SIZE MODIFIED\nllama3.1:8b abc 2 GB now\n",
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


def test_detect_local_installation_state_skips_probes_outside_scoped_profile(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    base_profile = get_alpha_local_profile()
    scoped_profile = replace(
        base_profile,
        stages=tuple(
            stage
            for stage in base_profile.stages
            if stage.stage in {"transcription", "diarization"}
        ),
        required_commands=("mena",),
        playback_commands=(),
    )
    runner = FakeRunner()

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
        profile=scoped_profile,
        python_executable=Path("/target/python"),
    )

    assert ("ollama", "list") not in runner.run_calls
    assert state.ollama.model_present is False
    assert state.ollama.detail == "Ollama was not evaluated for this scoped runtime profile."
    assert state.asr_model_present is True
    assert state.diarization_model_present is True


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


def test_diarization_cache_probe_validates_selected_device(
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
            cuda_available=True,
            vram_gb=12.0,
            avx2_available=True,
        ),
        env={"HF_TOKEN": "token"},
        python_executable=Path("/target/python"),
    )

    pyannote_call = next(
        call for call in runner.run_calls if len(call) > 2 and "Pipeline.from_pretrained" in call[2]
    )
    assert "__import__('torch').device('cuda')" in pyannote_call[2]
    assert "getattr(pipeline, 'to', None)" in pyannote_call[2]


def test_pyannote_cache_probe_success_path_does_not_raise_none(tmp_path: Path) -> None:
    pyannote_package = tmp_path / "pyannote" / "audio"
    pyannote_package.mkdir(parents=True)
    (tmp_path / "pyannote" / "__init__.py").write_text("", encoding="utf-8")
    (pyannote_package / "__init__.py").write_text(
        "\n".join(
            (
                "class Pipeline:",
                "    @classmethod",
                "    def from_pretrained(cls, *_args, **_kwargs):",
                "        return cls()",
                "    def to(self, device):",
                "        self.device = device",
                "        return self",
            )
        ),
        encoding="utf-8",
    )
    (tmp_path / "torch.py").write_text(
        "def device(value):\n    return value\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        _pyannote_cache_check_command(Path(sys.executable), device="cuda"),
        check=False,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


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
    assert "[local-providers]" in _readiness(evaluation, "python:faster_whisper").detail
    assert "[asr]" in _readiness(evaluation, "python:faster_whisper").detail
    assert "dist/mena-" not in _readiness(evaluation, "python:faster_whisper").detail
    assert "faster-whisper must be installed" in _readiness(evaluation, "asr-model").detail


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
    runtime_paths: RuntimePaths | None = None,
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
        runtime_paths=runtime_paths or _runtime_paths(Path("/home/user")),
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
            model="llama3.1:8b",
            command=CommandCheck(
                "ollama", None if "ollama" in missing else Path("/usr/bin/ollama")
            ),
            reachable="ollama" not in missing and ollama_reachable,
            model_present=ollama_model_present,
            detail=(
                "model present"
                if ollama_model_present
                else "model llama3.1:8b is not listed by Ollama"
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


def _mistral_state(
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


def test_profile_catalog_exposes_the_mistral_summary_profile() -> None:
    profile = get_mistral_summary_profile("llama3.2:3b")

    assert profile.id == MISTRAL_SUMMARY_PROFILE_ID
    assert profile.catalog_version == RUNTIME_PROFILE_CATALOG_VERSION
    assert profile.fallback is False
    summarization = next(stage for stage in profile.stages if stage.stage == "summarization")
    assert summarization.provider == "Mistral"
    assert summarization.model == MISTRAL_SUMMARY_REQUEST_MODEL
    assert summarization.device == "remote-api"
    semantic = next(stage for stage in profile.stages if stage.stage == "semantic-review")
    assert semantic.provider == "Ollama"
    assert semantic.model == "llama3.2:3b"
    assert summarization == mistral_summary_stage_profile()


def test_local_llm_stage_model_is_provider_aware() -> None:
    mistral_profile = get_mistral_summary_profile("llama3.2:3b")

    assert local_llm_stage_model(mistral_profile.stages, "default-model") == "llama3.2:3b"
    assert local_llm_stage_model((), "default-model") == "default-model"
    assert (
        local_llm_stage_model(get_alpha_local_profile("llama3.1:8b").stages, "default")
        == "llama3.1:8b"
    )


def test_evaluate_setup_probes_mistral_readiness_for_the_mistral_profile() -> None:
    profile = get_mistral_summary_profile()
    state = replace(_state(), mistral_summary=_mistral_state())

    evaluation = evaluate_setup(profile, state)

    item = _readiness(evaluation, "mistral-summary-model")
    assert item.ready
    assert "resolved inside the accepted snapshot" in item.detail
    # Semantic review remains local, so the Ollama model is still evaluated.
    assert _readiness(evaluation, "ollama-model").ready


def test_evaluate_setup_requires_the_mena_scoped_key_before_accepting_mistral() -> None:
    profile = get_mistral_summary_profile()
    state = replace(
        _state(),
        mistral_summary=_mistral_state(
            key_present=False,
            reachable=False,
            resolved_model=None,
            model_allowed=False,
            detail="MENA_MISTRAL_API_KEY is not set.",
        ),
    )

    evaluation = evaluate_setup(profile, state)

    item = _readiness(evaluation, "mistral-summary-model")
    assert not item.ready
    action = _action(evaluation.plan, "mistral-summary-model")
    assert action.mode == "manual"
    assert "MENA_MISTRAL_API_KEY" in action.detail
    assert "never persisted" in action.detail


def test_evaluate_setup_fails_closed_on_mistral_alias_drift() -> None:
    profile = get_mistral_summary_profile()
    state = replace(
        _state(),
        mistral_summary=_mistral_state(
            resolved_model="mistral-small-2707",
            model_allowed=False,
            detail="resolved outside the accepted snapshot; failing closed",
        ),
    )

    evaluation = evaluate_setup(profile, state)

    item = _readiness(evaluation, "mistral-summary-model")
    assert not item.ready
    action = _action(evaluation.plan, "mistral-summary-model")
    assert MISTRAL_SUMMARY_ALLOWLIST_POLICY in action.detail


def test_evaluate_setup_reports_unevaluated_mistral_readiness() -> None:
    profile = get_mistral_summary_profile()

    evaluation = evaluate_setup(profile, _state())

    item = _readiness(evaluation, "mistral-summary-model")
    assert not item.ready
    assert "not evaluated" in item.detail


def test_detect_local_installation_state_probes_mistral_only_for_mistral_profiles(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    runner = _FakeRunner()
    probes: list[Mapping[str, str]] = []

    def _probe(*, env: Mapping[str, str]) -> MistralSummaryState:
        probes.append(env)
        return _mistral_state()

    local_state = detect_local_installation_state(
        settings,
        runner=runner,
        command_identity=_identity(),
        hardware=_hardware(),
        profile=get_alpha_local_profile(),
        env={"MENA_MISTRAL_API_KEY": "mena-key"},
        mistral_probe=_probe,
    )
    mistral_state = detect_local_installation_state(
        settings,
        runner=runner,
        command_identity=_identity(),
        hardware=_hardware(),
        profile=get_mistral_summary_profile(),
        env={"MENA_MISTRAL_API_KEY": "mena-key"},
        mistral_probe=_probe,
    )

    assert local_state.mistral_summary is None
    assert mistral_state.mistral_summary is not None
    assert mistral_state.mistral_summary.model_allowed
    assert len(probes) == 1


def test_runtime_profile_state_round_trips_the_ordered_fallback_list(tmp_path: Path) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    state = replace(
        _state(runtime_paths=settings.paths, pyannote_token_present=True),
        mistral_summary=_mistral_state(),
    )
    accepted = build_runtime_profile_state(
        evaluate_setup(get_alpha_local_profile(), state),
        summarization_fallback_providers=(mistral_summary_stage_profile(),),
    )

    persist_runtime_profile_state(accepted, paths=settings.paths)
    loaded = load_runtime_profile_state(settings.paths)

    assert loaded.summarization_fallback_providers == (mistral_summary_stage_profile(),)
    assert loaded.fallback_authorization.remote_status == "authorized"
    assert loaded.fallback_authorization.remote_stage_scope == ("summarization",)
    evidence = loaded.evidence
    mistral_evidence = cast(Mapping[str, object], evidence["mistral_summary"])
    assert mistral_evidence["resolved_model"] == "mistral-small-2603"
    assert "mena-key" not in (settings.paths.state_home / "setup-state.json").read_text(
        encoding="utf-8"
    )


def test_runtime_profile_state_defaults_to_an_empty_fallback_list(tmp_path: Path) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    state = _state(runtime_paths=settings.paths, pyannote_token_present=True)
    accepted = build_runtime_profile_state(evaluate_setup(get_alpha_local_profile(), state))

    payload = accepted.to_dict()
    assert payload["summarization_fallback_providers"] == []
    # Legacy persisted states have no fallback-provider field at all.
    del payload["summarization_fallback_providers"]
    loaded = RuntimeProfileState.from_dict(payload)

    assert loaded.summarization_fallback_providers == ()
    assert loaded.fallback_authorization.remote_status == "not_configured"


def test_runtime_profile_state_rejects_non_dict_evidence(tmp_path: Path) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    state = _state(runtime_paths=settings.paths, pyannote_token_present=True)
    accepted = build_runtime_profile_state(evaluate_setup(get_alpha_local_profile(), state))
    payload = accepted.to_dict()
    payload["evidence"] = MappingProxyType({})

    with pytest.raises(
        ValueError,
        match="Runtime profile state field 'evidence' must be a JSON object\\.",
    ):
        RuntimeProfileState.from_dict(payload)


def test_runtime_profile_state_rejects_non_summarization_fallback_entries(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    state = _state(runtime_paths=settings.paths, pyannote_token_present=True)

    with pytest.raises(ValueError, match="must be summarization stage entries"):
        build_runtime_profile_state(
            evaluate_setup(get_alpha_local_profile(), state),
            summarization_fallback_providers=(
                replace(mistral_summary_stage_profile(), stage="diarization"),
            ),
        )


def _identity() -> CommandIdentity:
    return CommandIdentity(
        current_executable=Path("/home/user/.local/bin/mena"),
        plain_mena_path=Path("/home/user/.local/bin/mena"),
        version="0.1.0",
        invocation_mode="installed-product",
    )


def _hardware() -> HardwareFacts:
    return HardwareFacts(
        system="Linux",
        ram_gb=32.0,
        cuda_available=True,
        vram_gb=12.0,
        avx2_available=True,
    )


class _FakeRunner:
    def which(self, command: str) -> Path | None:
        return Path("/usr/bin") / command

    def run(
        self,
        args: Sequence[str],
        *,
        timeout_seconds: float = 30.0,
        env: Mapping[str, str] | None = None,
    ) -> CommandRun:
        _ = timeout_seconds, env
        command = tuple(str(arg) for arg in args)
        if command[:2] == ("ollama", "list"):
            return CommandRun(command, 0, stdout="NAME\nllama3.1:8b latest")
        return CommandRun(command, 0, stdout="ok")


def test_mistral_primary_profile_does_not_require_ollama() -> None:
    profile = get_mistral_summary_profile()
    assert "ollama" not in profile.required_commands

    state = replace(
        _state(
            missing_commands={"ollama"},
            ollama_reachable=False,
            ollama_model_present=False,
        ),
        mistral_summary=_mistral_state(),
    )

    evaluation = evaluate_setup(profile, state)

    assert evaluation.ready
    item = _readiness(evaluation, "ollama-model")
    assert item.ready
    assert "semantic review skipped" in item.detail
    assert all(action.id != "command:ollama" for action in evaluation.plan.actions)
    assert not evaluation.plan.has_required_actions


def test_mistral_primary_profile_offers_optional_semantic_model_pull() -> None:
    profile = get_mistral_summary_profile()
    state = replace(
        _state(ollama_model_present=False),
        mistral_summary=_mistral_state(),
    )

    evaluation = evaluate_setup(profile, state)

    assert evaluation.ready
    action = _action(evaluation.plan, "ollama-model")
    assert action.required is False
    assert action.mode == "automated"
    assert action.command == ("ollama", "pull", state.ollama.model)


def test_detection_probes_ollama_on_path_when_it_is_an_optional_dependency(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    runner = _FakeRunner()

    state = detect_local_installation_state(
        settings,
        runner=runner,
        command_identity=_identity(),
        hardware=_hardware(),
        profile=get_mistral_summary_profile(),
        env={"MENA_MISTRAL_API_KEY": "mena-key"},
        mistral_probe=lambda *, env: _mistral_state(),
    )

    # `ollama` is not a required command for the Mistral-primary profile, but
    # the optional semantic-review stage still detects it directly from PATH.
    assert state.ollama.command.present
    assert state.ollama.model_present
