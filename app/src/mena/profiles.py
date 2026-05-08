"""Capture profile names supported by the PoC CLI."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CaptureProfileId(StrEnum):
    """Durable architecture-facing capture profile identifiers."""

    ONLINE_DUAL_STREAM = "online_dual_stream"
    IN_PERSON_SINGLE_MIC = "in_person_single_mic"


class CaptureProfileCliName(StrEnum):
    """User-facing capture profile names for the first PoC CLI slice."""

    ONLINE = "online"
    IN_PERSON = "in-person"


@dataclass(frozen=True)
class CaptureProfileSelection:
    """Capture profile selected through the CLI."""

    id: CaptureProfileId
    cli_name: CaptureProfileCliName


_PROFILE_SELECTIONS_BY_CLI_NAME = {
    CaptureProfileCliName.ONLINE.value: CaptureProfileSelection(
        id=CaptureProfileId.ONLINE_DUAL_STREAM,
        cli_name=CaptureProfileCliName.ONLINE,
    ),
    CaptureProfileCliName.IN_PERSON.value: CaptureProfileSelection(
        id=CaptureProfileId.IN_PERSON_SINGLE_MIC,
        cli_name=CaptureProfileCliName.IN_PERSON,
    ),
}


def parse_capture_profile(value: str) -> CaptureProfileSelection:
    """Parse a CLI profile value into a durable architecture profile selection."""

    try:
        return _PROFILE_SELECTIONS_BY_CLI_NAME[value]
    except KeyError as exc:
        valid_profiles = ", ".join(capture_profile_choices())
        message = f"Unknown capture profile '{value}'. Valid profiles: {valid_profiles}."
        raise ValueError(message) from exc


def capture_profile_choices() -> tuple[str, ...]:
    """Return profile values in the order they should appear in CLI help."""

    return tuple(profile.value for profile in CaptureProfileCliName)
