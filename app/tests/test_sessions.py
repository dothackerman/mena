import json
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from mena.profiles import CaptureProfileId, parse_capture_profile
from mena.sessions import (
    SESSION_RECORD_VERSION,
    create_session,
    default_data_root,
    read_session_record,
    update_session_capture_metadata,
    update_session_discovery,
)
from mena.settings import DATA_ROOT_ENV_VAR


def test_session_record_v1_schema_is_valid() -> None:
    Draft202012Validator.check_schema(_load_session_record_schema())


def test_session_record_version_matches_v1_schema_const() -> None:
    schema = _load_session_record_schema()
    properties = cast(dict[str, object], schema["properties"])
    version = cast(dict[str, object], properties["version"])

    assert version["const"] == SESSION_RECORD_VERSION


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


def test_create_session_writes_record_matching_v1_schema(tmp_path: Path) -> None:
    created_session = create_session(
        profile=parse_capture_profile("in-person"),
        name="Schema check",
        data_root=tmp_path,
        now=datetime(2026, 5, 8, 5, 30, 0, tzinfo=UTC),
    )
    record = json.loads(created_session.record_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(
        _load_session_record_schema(),
        format_checker=FormatChecker(),
    )

    validator.validate(record)


def test_update_session_discovery_writes_schema_valid_session_record(tmp_path: Path) -> None:
    created_session = create_session(
        profile=parse_capture_profile("online"),
        data_root=tmp_path,
        now=datetime(2026, 5, 8, 5, 30, 0, tzinfo=UTC),
    )
    discovery = {
        "version": 1,
        "profile": "online_dual_stream",
        "status": "available",
        "sources": [],
        "sinks": [],
        "playback_streams": [],
        "error": None,
    }

    record = update_session_discovery(created_session.record_path, discovery)
    validator = Draft202012Validator(
        _load_session_record_schema(),
        format_checker=FormatChecker(),
    )

    assert record["discovery"] == discovery
    assert read_session_record(created_session.record_path)["discovery"] == discovery
    validator.validate(record)


def test_session_record_schema_rejects_malformed_discovery_route() -> None:
    schema = _load_session_record_schema()
    record = {
        "version": 1,
        "id": "00000000-0000-0000-0000-000000000001",
        "created_at": "2026-05-08T05:30:00Z",
        "name": None,
        "status": "created",
        "profile": "online_dual_stream",
        "discovery": {
            "version": 1,
            "profile": "online_dual_stream",
            "status": "available",
            "sources": [{"route_id": "pipewire:node:31"}],
            "sinks": [],
            "playback_streams": [],
            "error": None,
        },
    }

    validator = Draft202012Validator(schema, format_checker=FormatChecker())

    with pytest.raises(ValidationError, match="'id' is a required property"):
        validator.validate(record)


def test_session_record_schema_rejects_extra_discovery_fields() -> None:
    record = {
        "version": 1,
        "id": "00000000-0000-0000-0000-000000000001",
        "created_at": "2026-05-08T05:30:00Z",
        "name": None,
        "status": "created",
        "profile": "online_dual_stream",
        "discovery": {
            "version": 1,
            "profile": "online_dual_stream",
            "status": "available",
            "sources": [],
            "sinks": [],
            "playback_streams": [],
            "error": None,
            "extra": True,
        },
    }
    validator = Draft202012Validator(_load_session_record_schema(), format_checker=FormatChecker())

    with pytest.raises(ValidationError, match="Additional properties are not allowed"):
        validator.validate(record)


def test_update_session_capture_metadata_writes_schema_valid_session_record(
    tmp_path: Path,
) -> None:
    created_session = create_session(
        profile=parse_capture_profile("online"),
        data_root=tmp_path,
        now=datetime(2026, 5, 8, 5, 30, 0, tzinfo=UTC),
    )
    capture_metadata = {
        "version": 1,
        "sample_rate": 48000,
        "channels": 1,
        "segment_seconds": 30,
        "segment_count": 1,
        "segments": [
            {
                "source_id": "local_mic",
                "index": 1,
                "path": "/tmp/local_mic_000001.wav",
                "sample_rate": 48000,
                "channels": 1,
                "byte_count": 44,
                "sha256": "0" * 64,
                "status": "sealed",
                "format": "wav",
            }
        ],
    }

    record = update_session_capture_metadata(created_session.record_path, capture_metadata)
    validator = Draft202012Validator(
        _load_session_record_schema(),
        format_checker=FormatChecker(),
    )

    assert record["capture_metadata"] == capture_metadata
    validator.validate(record)


def test_default_data_root_uses_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DATA_ROOT_ENV_VAR, str(tmp_path / "data-root"))

    assert default_data_root() == tmp_path / "data-root"


def test_parse_capture_profile_maps_cli_name_to_architecture_profile() -> None:
    profile = parse_capture_profile("in-person")

    assert profile.id is CaptureProfileId.IN_PERSON_SINGLE_MIC


def test_parse_capture_profile_rejects_unknown_profile() -> None:
    with pytest.raises(ValueError, match="Valid profiles: online, in-person"):
        parse_capture_profile("room")


def _load_session_record_schema() -> dict[str, object]:
    schema_path = files("mena.schemas").joinpath("session-record.v1.schema.json")
    return cast(dict[str, object], json.loads(schema_path.read_text(encoding="utf-8")))
