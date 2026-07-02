"""Shared stage policy lookup helpers for setup and runtime resolution."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol


class StagePolicy(Protocol):
    @property
    def stage(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def device(self) -> str: ...


def stage_model(
    policies: Iterable[StagePolicy],
    stage: str,
    profile_label: str,
) -> str:
    for policy in policies:
        if policy.stage == stage:
            model = policy.model
            if not isinstance(model, str):
                raise ValueError(f"{profile_label} stage {stage!r} model is invalid.")
            return model
    raise ValueError(f"{profile_label} does not define stage {stage!r}.")


def stage_device(
    policies: Iterable[StagePolicy],
    stage: str,
    *,
    hardware_selected_device: str | None = None,
) -> str | None:
    for policy in policies:
        if policy.stage == stage:
            device = policy.device
            if not isinstance(device, str):
                raise ValueError(f"Stage {stage!r} device is invalid.")
            if device == "hardware-selected":
                return hardware_selected_device
            return device
    return hardware_selected_device
