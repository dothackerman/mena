"""Console for the CLI adapter."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import TypeVar

from mena.interaction import (
    ChoiceRequest,
)

ChoiceT = TypeVar("ChoiceT")


def _prompt_choice(
    *,
    header: str,
    label: str,
    choices: tuple[ChoiceT, ...],
    formatter: Callable[[ChoiceT], str],
    input_fn: Callable[[str], str],
    default_index: int = 1,
) -> ChoiceT:
    if default_index < 1 or default_index > len(choices):
        raise ValueError(f"Invalid default {label} selection '{default_index}'.")
    print(header)
    for index, choice in enumerate(choices, start=1):
        print(f"  {index}. {formatter(choice)}")
    answer = input_fn(f"Select {label} [{default_index}]: ").strip() or str(default_index)
    if not answer.isdigit():
        raise ValueError(f"Invalid {label} selection '{answer}'.")
    selected_index = int(answer)
    if selected_index < 1 or selected_index > len(choices):
        raise ValueError(f"Invalid {label} selection '{answer}'.")
    return choices[selected_index - 1]


def _choice_default_index(request: ChoiceRequest) -> int:
    if request.default_option_id is None:
        return 1
    for index, option in enumerate(request.options, start=1):
        if option.option_id == request.default_option_id:
            return index
    raise ValueError(f"Unknown default option '{request.default_option_id}'.")


def _resolve_console_input(input_fn: Callable[[str], str] | None) -> Callable[[str], str] | None:
    if input_fn is not None:
        return input_fn
    if sys.stdin.isatty():
        return input
    return None
