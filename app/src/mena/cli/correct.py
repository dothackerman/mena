"""``mena correct`` parser and console handler."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from typing import cast

from mena.artifacts import (
    ArtifactStoreError,
)
from mena.cli.command import bind_handler
from mena.cli.composition import CommandContext
from mena.correction_workflow import CorrectionWorkflowError, run_correction_workflow
from mena.interaction import (
    ChoiceOption,
    ChoiceRequest,
    ConfirmationRequest,
    TextRequest,
    WorkflowNotice,
)
from mena.session_lock import SessionLockContention


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    *,
    session_parent: argparse.ArgumentParser,
    **_: object,
) -> None:
    parser = subparsers.add_parser(
        "correct",
        parents=[session_parent],
        help="Review and correct flagged transcript segments.",
    )
    bind_handler(parser, run)


def run(args: argparse.Namespace, context: CommandContext) -> int:
    dependencies = context.dependencies
    try:
        run_correction_workflow(
            session_path=Path(args.session_path),
            now=dependencies.now,
            interaction=ConsoleCorrectionInteraction(dependencies.input_fn),
            playback_player=dependencies.playback_player,
        )
    except SessionLockContention as exc:
        print(f"Cannot run correction workflow: {exc}")
        return 4
    except (ArtifactStoreError, CorrectionWorkflowError) as exc:
        print(f"Cannot run correction workflow: {exc}")
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Correction workflow cancelled.")
        return 1
    return 0


class ConsoleCorrectionInteraction:
    """Console adapter for correction workflow interaction topics."""

    def __init__(self, input_fn: Callable[[str], str] | None) -> None:
        self._input_fn = input if input_fn is None else input_fn

    def choose(self, request: ChoiceRequest) -> ChoiceOption:
        if request.topic != "correction.action":
            raise ValueError(f"Unsupported correction choice topic '{request.topic}'.")
        options = {option.option_id: option for option in request.options}
        while True:
            raw = (
                self._input_fn(
                    "Action [p]lay, [s]kip, [r]eassign speaker, [t]ext edit, or [o]verlap: "
                )
                .strip()
                .lower()
            )
            option_id = _correction_action_option_id(raw)
            if option_id is not None and option_id in options:
                return options[option_id]
            print("Invalid action. Use [p]lay, [s]kip, [r]eassign, [t]ext, or [o]verlap.")

    def confirm(self, request: ConfirmationRequest) -> bool:
        if request.topic != "correction.overlap_mark":
            raise ValueError(f"Unsupported correction confirmation topic '{request.topic}'.")
        while True:
            raw = self._input_fn("Mark overlap? [y/N]: ").strip().lower()
            if raw in {"", "n", "no"}:
                return False
            if raw in {"y", "yes"}:
                return True
            print("Please answer y or n.")

    def request_text(self, request: TextRequest) -> str:
        if request.topic == "correction.speaker_assignment":
            return self._input_fn("Choose existing speaker number or enter a new speaker label: ")
        if request.topic == "correction.text_edit":
            return self._input_fn("Corrected text: ")
        raise ValueError(f"Unsupported correction text topic '{request.topic}'.")

    def notify(self, notice: WorkflowNotice) -> None:
        topic = notice.topic
        context = notice.context
        if topic == "correction.speaker_assignment.invalid_number":
            print("Choose a valid number from the list.")
        elif topic == "correction.speaker_assignment.empty":
            print("Enter a speaker id or a label.")
        elif topic == "correction.playback_failed":
            print(f"Playback failed: {context['error']}")
        elif topic == "correction.playback_completed":
            print(
                "Playback completed for "
                f"{context['source_id']} "
                f"{cast(float, context['start_seconds']):.2f}-"
                f"{cast(float, context['end_seconds']):.2f}s."
            )
        elif topic == "correction.completed":
            print(
                "Correction completed. "
                f"reviewed={context['reviewed']}, "
                f"skipped={context['skipped']}, "
                f"corrected={context['corrected']}."
            )
        else:
            raise ValueError(f"Unsupported correction notice topic '{topic}'.")


def _correction_action_option_id(raw: str) -> str | None:
    if raw in {"p", "play", "playback"}:
        return "playback"
    if raw in {"s", "skip"}:
        return "skip"
    if raw in {"r", "speaker", "reassign", "speaker_reassignment"}:
        return "speaker_reassignment"
    if raw in {"t", "text", "text_edit", "edit"}:
        return "text_edit"
    if raw in {"o", "overlap", "overlap_mark"}:
        return "overlap_mark"
    return None
