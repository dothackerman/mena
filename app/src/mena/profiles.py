"""Capture profile names supported by the PoC CLI."""

from __future__ import annotations

from enum import StrEnum


class CaptureProfileName(StrEnum):
    """User-facing capture profile names for the first PoC CLI slice."""

    ONLINE = "online"
    IN_PERSON = "in-person"


def parse_capture_profile(value: str) -> CaptureProfileName:
    """Parse a CLI profile value into the canonical profile enum."""

    try:
        return CaptureProfileName(value)
    except ValueError as exc:
        valid_profiles = ", ".join(profile.value for profile in CaptureProfileName)
        message = f"Unknown capture profile '{value}'. Valid profiles: {valid_profiles}."
        raise ValueError(message) from exc


def capture_profile_choices() -> tuple[str, ...]:
    """Return profile values in the order they should appear in CLI help."""

    return tuple(profile.value for profile in CaptureProfileName)
