from datetime import UTC, datetime, timedelta, timezone

from mena.timestamps import utc_timestamp, utc_timestamp_for_filename


def test_utc_timestamp_formats_utc_with_z_suffix_and_second_precision() -> None:
    value = datetime(2026, 5, 8, 5, 30, 0, 123456, tzinfo=UTC)

    assert utc_timestamp(value) == "2026-05-08T05:30:00Z"


def test_utc_timestamp_normalizes_offset_to_utc() -> None:
    value = datetime(2026, 5, 8, 7, 30, 0, tzinfo=timezone(timedelta(hours=2)))

    assert utc_timestamp(value) == "2026-05-08T05:30:00Z"


def test_utc_timestamp_for_filename_removes_colons() -> None:
    value = datetime(2026, 5, 8, 5, 30, 0, tzinfo=UTC)

    assert utc_timestamp_for_filename(value) == "2026-05-08T053000Z"
