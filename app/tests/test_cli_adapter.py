from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import mena.cli as cli
from mena.cli import _console as console
from mena.cli import _runtime_fallback as runtime_fallback
from mena.cli import correct as correct_command
from mena.cli import inspect as inspect_command
from mena.cli import processing as processing_command
from mena.cli import record as record_command
from mena.cli import setup as setup_command
from mena.cli import summarize as summarize_command
from mena.interaction import (
    ChoiceOption,
    ChoiceRequest,
    ConfirmationRequest,
    TextRequest,
    WorkflowNotice,
)
from mena.settings import AppSettings
from mena.setup_workflow import SetupMessageEvent, SetupWorkflowResult


def test_cli_package_exports_only_supported_entry_point() -> None:
    assert cli.__all__ == ["main"]


def test_console_record_interaction_reports_adapter_notices(
    capsys: pytest.CaptureFixture[str],
) -> None:
    interaction = record_command.ConsoleRecordInteraction(lambda _prompt: "q")

    for notice in (
        WorkflowNotice(topic="record.session_created", context={"session_id": "session-1"}),
        WorkflowNotice(topic="record.session_record", context={"record_path": "/tmp/session.json"}),
        WorkflowNotice(topic="record.capture_not_started.discovery_unavailable"),
        WorkflowNotice(topic="record.capture_not_started.error", context={"error": "no input"}),
        WorkflowNotice(topic="record.capture_not_started.noninteractive_stop"),
        WorkflowNotice(topic="record.consent_notice"),
        WorkflowNotice(topic="record.stop_prompt_started"),
        WorkflowNotice(topic="record.stop_prompt_continues"),
        WorkflowNotice(
            topic="record.segment_flushed",
            context={"source_id": "local_mic", "index": 2},
        ),
        WorkflowNotice(
            topic="record.capture_status",
            context={"elapsed_seconds": 7.8, "sealed_segment_count": 3},
        ),
        WorkflowNotice(
            topic="record.capture_route_event",
            context={"event": "recovered", "message": "route restored"},
        ),
        WorkflowNotice(
            topic="record.capture_route_event",
            context={
                "event": "degraded",
                "source_id": "system_audio",
                "code": "dedicated_route_runtime_failed",
                "message": "Selected playback stream ended",
                "selected_sources": ["local_mic", "system_audio"],
            },
        ),
        WorkflowNotice(
            topic="record.capture_route_event",
            context={
                "event": "degraded",
                "source_id": "system_audio",
                "code": "dedicated_route_unavailable",
                "message": "Dedicated route unavailable",
                "selected_sources": ["local_mic", "system_audio"],
            },
        ),
        WorkflowNotice(
            topic="record.capture_route_event",
            context={"event": "lost", "message": "route missing"},
        ),
        WorkflowNotice(topic="record.worker_teardown_interrupted"),
        WorkflowNotice(topic="record.stop_interrupted"),
        WorkflowNotice(topic="record.capture_failed", context={"error": "worker failed"}),
        WorkflowNotice(topic="record.capture_finalized"),
        WorkflowNotice(
            topic="record.process_handoff",
            context={"record_path": "/tmp/session.json"},
        ),
        WorkflowNotice(
            topic="record.finalized_status_error",
            context={"status": "failed", "error": "finalization failed"},
        ),
        WorkflowNotice(topic="record.finalized_status", context={"status": "recorded"}),
    ):
        interaction.notify(notice)

    output = capsys.readouterr().out
    assert "Created session session-1" in output
    assert "Capture recovered: route restored" in output
    assert (
        "Selected playback stream ended during capture; "
        "local microphone capture may have continued. "
        "Selected playback stream ended"
    ) in output
    assert "Capture degraded (system audio): Dedicated route unavailable" in output
    assert "Capture degraded: route missing" in output
    assert "Next command: mena process" in output
    assert "Capture finalized as failed: finalization failed" in output

    with pytest.raises(ValueError, match="Unsupported record notice topic"):
        interaction.notify(WorkflowNotice(topic="record.unknown"))


def test_console_record_interaction_reports_handoff_playback_loss_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    record_path = tmp_path / "session.json"
    record_path.write_text(
        json.dumps(
            {
                "capture_plan": {"selected_sources": ["local_mic", "system_audio"]},
                "capture_metadata": {
                    "sources": [
                        {
                            "source_id": "local_mic",
                            "segment_count": 1,
                        },
                        {
                            "source_id": "system_audio",
                            "segment_count": 1,
                        },
                    ]
                },
                "degradations": [
                    {
                        "event": "degraded",
                        "code": "dedicated_route_runtime_failed",
                        "source_id": "system_audio",
                        "message": "Dedicated meeting route failed during capture.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    interaction = record_command.ConsoleRecordInteraction(lambda _prompt: "q")
    interaction.notify(
        WorkflowNotice(
            topic="record.process_handoff",
            context={"record_path": str(record_path)},
        )
    )

    output = capsys.readouterr().out
    assert (
        "Finalization note: selected playback stream ended during recording; "
        "local microphone segments were recorded."
    ) in output


def test_console_record_interaction_handoff_does_not_overstate_unrecorded_mic(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    record_path = tmp_path / "session.json"
    record_path.write_text(
        json.dumps(
            {
                "capture_plan": {"selected_sources": ["local_mic", "system_audio"]},
                "capture_metadata": {
                    "sources": [
                        {
                            "source_id": "system_audio",
                            "segment_count": 1,
                        }
                    ]
                },
                "degradations": [
                    {
                        "event": "degraded",
                        "code": "dedicated_route_runtime_failed",
                        "source_id": "system_audio",
                        "message": "Dedicated meeting route failed during capture.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    interaction = record_command.ConsoleRecordInteraction(lambda _prompt: "q")
    interaction.notify(
        WorkflowNotice(
            topic="record.process_handoff",
            context={"record_path": str(record_path)},
        )
    )

    output = capsys.readouterr().out
    assert (
        "Finalization note: selected playback stream ended during recording; "
        "local microphone capture was selected, but no local microphone segments were recorded."
    ) in output


def test_console_record_interaction_reports_playback_loss_without_other_source(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    record_path = tmp_path / "session.json"
    record_path.write_text(
        json.dumps(
            {
                "capture_plan": {"selected_sources": ["system_audio"]},
                "capture_metadata": {
                    "sources": [
                        {
                            "source_id": "system_audio",
                            "segment_count": 1,
                        }
                    ]
                },
                "degradations": [
                    {
                        "event": "degraded",
                        "code": "dedicated_route_runtime_failed",
                        "source_id": "system_audio",
                        "message": "Dedicated meeting route failed during capture.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    interaction = record_command.ConsoleRecordInteraction(lambda _prompt: "q")

    interaction.notify(
        WorkflowNotice(
            topic="record.capture_route_event",
            context={
                "event": "degraded",
                "source_id": "system_audio",
                "code": "dedicated_route_runtime_failed",
                "message": "Selected playback stream ended",
                "selected_sources": ["system_audio"],
            },
        )
    )
    interaction.notify(
        WorkflowNotice(
            topic="record.process_handoff",
            context={"record_path": str(record_path)},
        )
    )

    output = capsys.readouterr().out
    assert (
        "Selected playback stream ended during capture; no other selected source was active. "
        "Selected playback stream ended"
    ) in output
    assert (
        "Finalization note: selected playback stream ended during recording; "
        "no other selected source was active."
    ) in output


def test_console_record_interaction_rejects_noninteractive_choices_and_text() -> None:
    interaction = record_command.ConsoleRecordInteraction(None)

    with pytest.raises(ValueError, match="pass --mic"):
        interaction.choose(
            ChoiceRequest(
                topic="record.capture_route.microphone",
                options=(ChoiceOption(option_id="mic-1", label="Mic"),),
            )
        )

    with pytest.raises(ValueError, match="pass --playback-stream"):
        interaction.choose(
            ChoiceRequest(
                topic="record.capture_route.playback_stream",
                options=(ChoiceOption(option_id="playback-1", label="Call"),),
            )
        )

    with pytest.raises(
        ValueError,
        match="Multiple meeting audio output routes were discovered; pass --output-sink.",
    ):
        interaction.choose(
            ChoiceRequest(
                topic="record.capture_route.output_sink",
                options=(ChoiceOption(option_id="sink-1", label="Headphones"),),
            )
        )

    with pytest.raises(RuntimeError, match="interactive terminal"):
        interaction.request_text(TextRequest(topic="record.stop_command"))
    with pytest.raises(ValueError, match="Unsupported record confirmation topic"):
        interaction.confirm(ConfirmationRequest(topic="record.unsupported"))
    with pytest.raises(ValueError, match="Unsupported record text topic"):
        record_command.ConsoleRecordInteraction(lambda _prompt: "q").request_text(
            TextRequest(topic="record.unsupported")
        )

    with pytest.raises(ValueError, match="Unsupported record choice topic"):
        record_command._record_choice_prompt_metadata("record.capture_route.unknown")


def test_prompt_and_runtime_adapter_helpers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    choices = (
        ChoiceOption(option_id="one", label="One"),
        ChoiceOption(option_id="two", label="Two"),
    )

    assert (
        console._prompt_choice(
            header="Choices:",
            label="choices",
            choices=choices,
            formatter=lambda choice: choice.label,
            input_fn=lambda _prompt: "",
            default_index=2,
        ).option_id
        == "two"
    )
    with pytest.raises(ValueError, match="Invalid default"):
        console._prompt_choice(
            header="Choices:",
            label="choices",
            choices=choices,
            formatter=lambda choice: choice.label,
            input_fn=lambda _prompt: "1",
            default_index=3,
        )
    with pytest.raises(ValueError, match="Invalid choices selection 'x'"):
        console._prompt_choice(
            header="Choices:",
            label="choices",
            choices=choices,
            formatter=lambda choice: choice.label,
            input_fn=lambda _prompt: "x",
        )
    with pytest.raises(ValueError, match="Invalid choices selection '3'"):
        console._prompt_choice(
            header="Choices:",
            label="choices",
            choices=choices,
            formatter=lambda choice: choice.label,
            input_fn=lambda _prompt: "3",
        )

    def explicit(prompt: str) -> str:
        return prompt

    assert console._resolve_console_input(explicit) is explicit
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    assert console._resolve_console_input(None) is input
    assert isinstance(
        runtime_fallback._resolve_runtime_fallback_interaction(explicit),
        runtime_fallback.ConsoleProcessingInteraction,
    )
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))
    noninteractive = runtime_fallback._resolve_runtime_fallback_interaction(None)
    assert isinstance(noninteractive, runtime_fallback.ConsoleProcessingInteraction)
    assert (
        noninteractive.confirm(
            ConfirmationRequest(
                topic="runtime_fallback.one_run",
                context={
                    "accepted_failure": "no gpu",
                    "fallback_evaluation": SimpleNamespace(
                        profile=SimpleNamespace(id="fallback"),
                        selected_asr_profile=SimpleNamespace(
                            checkpoint="small",
                            device="cpu",
                        ),
                    ),
                },
            )
        )
        is False
    )

    settings = AppSettings.from_env(env={}, home=tmp_path)
    assert (
        record_command._effective_record_data_root(tmp_path / "data", settings) == tmp_path / "data"
    )
    assert record_command._format_process_handoff_command(tmp_path / "data") == (
        f"MENA_DATA_ROOT={tmp_path / 'data'} mena process"
    )
    assert record_command._format_process_handoff_command(None) == "mena process"
    assert processing_command._resolve_process_session_path(
        str(tmp_path / "session.json"), settings
    ) == (tmp_path / "session.json")
    with pytest.raises(ValueError, match="no recorded sessions"):
        processing_command._resolve_process_session_path(None, settings)


def test_record_choice_and_data_root_adapter_branches(
    tmp_path: Path,
) -> None:
    prompts: list[str] = []

    def _input(prompt: str) -> str:
        prompts.append(prompt)
        return ""

    interaction = record_command.ConsoleRecordInteraction(_input)
    selected = interaction.choose(
        ChoiceRequest(
            topic="record.capture_route.output_sink",
            options=(
                ChoiceOption(option_id="one", label="One"),
                ChoiceOption(option_id="two", label="Two"),
            ),
            default_option_id="two",
        )
    )

    assert selected.option_id == "two"
    assert prompts == ["Select meeting audio output [2]: "]
    assert record_command._record_choice_prompt_metadata(
        "record.capture_route.playback_stream"
    ) == (
        "playback stream",
        "--playback-stream",
        "Playback streams:",
        "playback streams",
    )
    assert record_command._record_choice_prompt_metadata("record.capture_route.output_sink") == (
        "meeting audio output",
        "--output-sink",
        "Where do you hear the meeting?",
        "meeting audio output",
    )

    invalid_selection = record_command.ConsoleRecordInteraction(lambda _prompt: "x")
    with pytest.raises(ValueError, match="Invalid meeting audio output selection 'x'"):
        invalid_selection.choose(
            ChoiceRequest(
                topic="record.capture_route.output_sink",
                options=(
                    ChoiceOption(option_id="one", label="One"),
                    ChoiceOption(option_id="two", label="Two"),
                ),
                default_option_id=None,
            )
        )
    assert record_command._resolve_record_profile(None, input_fn=lambda _prompt: "2").id.value == (
        "in_person_single_mic"
    )

    xdg_home = tmp_path / "xdg"
    settings = AppSettings.from_env(env={"XDG_DATA_HOME": str(xdg_home)}, home=tmp_path)

    assert record_command._effective_record_data_root(None, settings) is None


def test_main_reports_adapter_cancellations_and_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)

    assert (
        cli.main(
            ["record"], settings=settings, input_fn=lambda _prompt: (_ for _ in ()).throw(EOFError)
        )
        == 1
    )
    assert "Recording setup cancelled." in capsys.readouterr().out

    monkeypatch.setattr(
        summarize_command.workflow,
        "resolve_summarize_session_path",
        lambda **_kwargs: tmp_path,
    )
    monkeypatch.setattr(
        summarize_command.workflow,
        "run_summarization_command",
        lambda **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt),
    )
    assert cli.main(["summarize", str(tmp_path)], settings=settings) == 1
    assert "Summarization cancelled." in capsys.readouterr().out

    monkeypatch.setattr(
        inspect_command,
        "_run_inspect_command",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("manifest missing")),
    )
    assert cli.main(["inspect", str(tmp_path)], settings=settings) == 1
    assert "Cannot inspect session: manifest missing" in capsys.readouterr().out

    def _raise_processing_interrupted(**_kwargs: object) -> int:
        raise KeyboardInterrupt

    assert (
        cli.main(
            ["process", str(tmp_path)],
            settings=settings,
            processing_command_runner=_raise_processing_interrupted,
        )
        == 1
    )
    assert "Processing workflow interrupted." in capsys.readouterr().out


def test_console_correction_and_processing_reject_unsupported_topics() -> None:
    correction = correct_command.ConsoleCorrectionInteraction(lambda _prompt: "no")
    processing_interaction = runtime_fallback.ConsoleProcessingInteraction(lambda _prompt: "no")

    with pytest.raises(ValueError, match="Unsupported correction choice topic"):
        correction.choose(
            ChoiceRequest(
                topic="correction.unsupported",
                options=(ChoiceOption(option_id="skip", label="Skip"),),
            )
        )
    with pytest.raises(ValueError, match="Unsupported correction confirmation topic"):
        correction.confirm(ConfirmationRequest(topic="correction.unsupported"))
    assert not correction.confirm(ConfirmationRequest(topic="correction.overlap_mark"))
    with pytest.raises(ValueError, match="Unsupported correction text topic"):
        correction.request_text(TextRequest(topic="correction.unsupported"))
    assert correction.request_text(TextRequest(topic="correction.text_edit")) == "no"
    with pytest.raises(ValueError, match="Unsupported correction notice topic"):
        correction.notify(WorkflowNotice(topic="correction.unsupported"))

    assert correct_command._correction_action_option_id("play") == "playback"
    assert correct_command._correction_action_option_id("reassign") == "speaker_reassignment"
    assert correct_command._correction_action_option_id("text") == "text_edit"
    assert correct_command._correction_action_option_id("overlap") == "overlap_mark"
    assert correct_command._correction_action_option_id("?") is None

    with pytest.raises(ValueError, match="Unsupported processing choice topic"):
        processing_interaction.choose(
            ChoiceRequest(
                topic="processing.unsupported",
                options=(ChoiceOption(option_id="skip", label="Skip"),),
            )
        )
    with pytest.raises(ValueError, match="Unsupported processing text topic"):
        processing_interaction.request_text(TextRequest(topic="processing.unsupported"))
    with pytest.raises(ValueError, match="Unsupported processing notice topic"):
        processing_interaction.notify(WorkflowNotice(topic="processing.unsupported"))


def test_setup_cli_interaction_and_command_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    interaction = setup_command._CliSetupInteraction(lambda _prompt: "yes")

    assert interaction.confirm(ConfirmationRequest(topic="setup.primary_actions"))
    assert interaction.confirm(ConfirmationRequest(topic="setup.fallback_actions"))
    with pytest.raises(ValueError, match="Unsupported setup confirmation request"):
        interaction.confirm(ConfirmationRequest(topic="setup.unknown"))
    with pytest.raises(ValueError, match="Unsupported setup choice request"):
        interaction.choose(
            ChoiceRequest(
                topic="setup.choice",
                options=(ChoiceOption(option_id="one", label="One"),),
            )
        )
    with pytest.raises(ValueError, match="Unsupported setup text request"):
        interaction.request_text(TextRequest(topic="setup.text"))
    interaction.notify(WorkflowNotice(topic="setup.notice"))

    def _run_setup_workflow(**_kwargs: object) -> SetupWorkflowResult:
        return SetupWorkflowResult(
            exit_code=7,
            events=(SetupMessageEvent("setup message"),),
        )

    monkeypatch.setattr(setup_command, "run_setup_workflow", _run_setup_workflow)

    assert (
        setup_command._run_setup_command(
            check_only=False,
            assume_yes=True,
            authorize_local_fallback=False,
            settings=AppSettings.from_env(env={}, home=tmp_path),
            input_fn=lambda _prompt: "yes",
            runner=None,
            command_identity=None,
            hardware=None,
        )
        == 7
    )
    assert "setup message" in capsys.readouterr().out
