"""Shared UTC timestamp formatting helpers."""

from __future__ import annotations

from datetime import UTC, datetime


def utc_timestamp(value: datetime | None = None) -> str:
    current = value or datetime.now(UTC)
    return current.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utc_timestamp_for_filename(value: datetime | None = None) -> str:
    return utc_timestamp(value).replace(":", "")


class NaiveTimestampError(ValueError):
    """The value parsed as a date-time but carries no timezone."""


def parse_utc_timestamp(value: str) -> datetime:
    """Parse an RFC 3339 date-time into an aware UTC datetime.

    Raises NaiveTimestampError for a valid but timezone-less date-time and
    ValueError for anything that does not parse at all.
    """
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise NaiveTimestampError(f"timestamp must include a timezone: {value!r}")
    return parsed.astimezone(UTC)
