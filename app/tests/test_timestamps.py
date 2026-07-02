from datetime import UTC, datetime, timedelta, timezone

import pytest

from mena.timestamps import (
    NaiveTimestampError,
    parse_utc_timestamp,
    utc_timestamp,
    utc_timestamp_for_filename,
)


def test_utc_timestamp_formats_utc_with_z_suffix_and_second_precision() -> None:
    value = datetime(2026, 5, 8, 5, 30, 0, 123456, tzinfo=UTC)

    assert utc_timestamp(value) == "2026-05-08T05:30:00Z"


def test_utc_timestamp_normalizes_offset_to_utc() -> None:
    value = datetime(2026, 5, 8, 7, 30, 0, tzinfo=timezone(timedelta(hours=2)))

    assert utc_timestamp(value) == "2026-05-08T05:30:00Z"


def test_utc_timestamp_for_filename_removes_colons() -> None:
    value = datetime(2026, 5, 8, 5, 30, 0, tzinfo=UTC)

    assert utc_timestamp_for_filename(value) == "2026-05-08T053000Z"


def test_parse_utc_timestamp_accepts_z_suffix() -> None:
    parsed = parse_utc_timestamp("2026-05-08T05:30:00Z")

    assert parsed == datetime(2026, 5, 8, 5, 30, 0, tzinfo=UTC)


def test_parse_utc_timestamp_normalizes_offset_to_utc() -> None:
    parsed = parse_utc_timestamp("2026-05-08T07:30:00+02:00")

    assert parsed == datetime(2026, 5, 8, 5, 30, 0, tzinfo=UTC)
    assert parsed.tzinfo == UTC


def test_parse_utc_timestamp_rejects_naive_date_time() -> None:
    with pytest.raises(NaiveTimestampError):
        parse_utc_timestamp("2026-05-08T05:30:00")


def test_parse_utc_timestamp_rejects_invalid_value() -> None:
    with pytest.raises(ValueError, match="Invalid isoformat"):
        parse_utc_timestamp("not-a-timestamp")


def test_parse_utc_timestamp_round_trips_utc_timestamp() -> None:
    value = datetime(2026, 5, 8, 5, 30, 0, tzinfo=UTC)

    assert parse_utc_timestamp(utc_timestamp(value)) == value
