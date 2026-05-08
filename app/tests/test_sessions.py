import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mena.profiles import CaptureProfileId, parse_capture_profile
from mena.sessions import create_session


def test_create_session_uses_uuid_and_timestamp_directory(tmp_path: Path) -> None:
    now = datetime(2026, 5, 8, 5, 30, 0, tzinfo=UTC)

    created_session = create_session(
        profile=parse_capture_profile("online"),
        name=None,
        data_root=tmp_path,
        now=now,
    )

    assert created_session.bundle_path.parent == tmp_path / "sessions"
    assert created_session.bundle_path.name.endswith("_2026-05-08T053000Z")
    assert created_session.record_path == created_session.bundle_path / "session.json"


def test_create_session_writes_minimal_record(tmp_path: Path) -> None:
    now = datetime(2026, 5, 8, 5, 30, 0, tzinfo=UTC)

    created_session = create_session(
        profile=parse_capture_profile("online"),
        name="Demo",
        data_root=tmp_path,
        now=now,
    )

    record = json.loads(created_session.record_path.read_text(encoding="utf-8"))
    assert record == {
        "created_at": "2026-05-08T05:30:00Z",
        "id": created_session.record.id,
        "name": "Demo",
        "profile": "online_dual_stream",
        "status": "created",
        "version": 1,
    }


def test_parse_capture_profile_maps_cli_name_to_architecture_profile() -> None:
    profile = parse_capture_profile("in-person")

    assert profile.id is CaptureProfileId.IN_PERSON_SINGLE_MIC


def test_parse_capture_profile_rejects_unknown_profile() -> None:
    with pytest.raises(ValueError, match="Valid profiles: online, in-person"):
        parse_capture_profile("room")
