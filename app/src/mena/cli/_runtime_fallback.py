"""Runtime Fallback for the CLI adapter."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import cast

from mena.interaction import (
    ChoiceOption,
    ChoiceRequest,
    ConfirmationRequest,
    InteractionPort,
    TextRequest,
    WorkflowNotice,
)
from mena.runtime_stewardship import (
    RUNTIME_STEWARDSHIP_RELEASE_NOTICE_TOPIC,
    RuntimeReleaseResult,
)
from mena.setup import (
    SetupEvaluation,
)


def _print_runtime_stewardship_release_notice(result: RuntimeReleaseResult) -> None:
    if result.status in {"failed", "not_supported"}:
        print(f"Runtime stewardship warning: {result.detail}")
    else:
        print(f"Runtime stewardship: {result.detail}")


class ConsoleProcessingInteraction:
    """Console adapter for processing workflow interaction topics."""

    def __init__(self, input_fn: Callable[[str], str] | None) -> None:
        self._input_fn = input_fn

    @property
    def runtime_fallback_confirmation_available(self) -> bool:
        return self._input_fn is not None

    def choose(self, request: ChoiceRequest) -> ChoiceOption:
        raise ValueError(f"Unsupported processing choice topic '{request.topic}'.")

    def confirm(self, request: ConfirmationRequest) -> bool:
        if request.topic != "runtime_fallback.one_run":
            raise ValueError(f"Unsupported processing confirmation topic '{request.topic}'.")
        if self._input_fn is None:
            return False
        accepted_failure = str(request.context["accepted_failure"])
        fallback_evaluation = cast(SetupEvaluation, request.context["fallback_evaluation"])
        print(f"Accepted runtime profile is unavailable: {accepted_failure}")
        print(
            "One-run local runtime fallback is ready: "
            f"{fallback_evaluation.profile.id} using "
            f"{fallback_evaluation.selected_asr_profile.checkpoint}/"
            f"{fallback_evaluation.selected_asr_profile.device}."
        )
        answer = self._input_fn("Use local runtime fallback for this run? [y/N] ")
        return answer.strip().lower() in {"y", "yes"}

    def request_text(self, request: TextRequest) -> str:
        raise ValueError(f"Unsupported processing text topic '{request.topic}'.")

    def notify(self, notice: WorkflowNotice) -> None:
        if notice.topic != RUNTIME_STEWARDSHIP_RELEASE_NOTICE_TOPIC:
            raise ValueError(f"Unsupported processing notice topic '{notice.topic}'.")
        _print_runtime_stewardship_release_notice(
            cast(RuntimeReleaseResult, notice.context["result"])
        )


def _resolve_runtime_fallback_input(
    input_fn: Callable[[str], str] | None,
) -> Callable[[str], str] | None:
    """Choose the interactive runtime-fallback prompt, guarded by a TTY check.

    An explicit ``input_fn`` (tests, embedding) is honored as-is. Otherwise the
    real ``input`` builtin is used only when stdin is a TTY; on a non-TTY stdin we
    return ``None`` so an unauthorized runtime fallback raises the actionable
    ``RuntimeFallbackUnavailable`` ("pass --allow-runtime-fallback") instead of
    ``input()`` raising an opaque ``EOFError``.
    """

    if input_fn is not None:
        return input_fn
    return input if sys.stdin.isatty() else None


def _resolve_runtime_fallback_interaction(
    input_fn: Callable[[str], str] | None,
) -> InteractionPort:
    reader = _resolve_runtime_fallback_input(input_fn)
    return ConsoleProcessingInteraction(reader)
