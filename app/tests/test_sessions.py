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
    append_session_degradation,
    append_session_failure,
    create_session,
    default_data_root,
    read_session_record,
    update_session_capture_metadata,
    update_session_capture_plan,
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


def test_create_session_defaults_to_runtime_sessions_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(DATA_ROOT_ENV_VAR, raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    now = datetime(2026, 5, 8, 5, 30, 0, tzinfo=UTC)

    created_session = create_session(
        profile=parse_capture_profile("online"),
        name=None,
        now=now,
    )

    assert created_session.bundle_path.parent == tmp_path / "xdg-data" / "mena" / "sessions"
    assert created_session.record_path == created_session.bundle_path / "session.json"


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
        "capture_plan": None,
        "created_at": "2026-05-08T05:30:00Z",
        "degradations": [],
        "failures": [],
        "id": created_session.record.id,
        "language": "en",
        "name": "Demo",
        "profile": "online_dual_stream",
        "recording": {
            "finalization": {
                "completed_at": None,
                "error": None,
                "status": "not_started",
            },
            "started_at": None,
            "stop_reason": None,
            "stopped_at": None,
        },
        "status": "created",
        "version": 1,
    }


def test_create_session_writes_record_matching_v1_schema(tmp_path: Path) -> None:
    created_session = create_session(
        profile=parse_capture_profile("in-person"),
        language="de-CH",
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
    assert record["language"] == "de-CH"


def test_create_session_rejects_empty_language(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Session language"):
        create_session(
            profile=parse_capture_profile("online"),
            language=" ",
            data_root=tmp_path,
            now=datetime(2026, 5, 8, 5, 30, 0, tzinfo=UTC),
        )


def test_append_session_degradation_persists_schema_valid_fact(tmp_path: Path) -> None:
    created_session = create_session(
        profile=parse_capture_profile("online"),
        data_root=tmp_path,
        now=datetime(2026, 5, 8, 5, 30, 0, tzinfo=UTC),
    )
    degradation = {
        "event": "degraded",
        "code": "route_restoration_incomplete",
        "message": "Capture completed, but route cleanup needs operator review.",
        "source_id": None,
        "occurred_at": "2026-05-08T05:31:00Z",
        "elapsed_seconds": None,
        "segment_index": None,
        "route_id": None,
        "route_label": None,
        "capture_mode": None,
    }

    record = append_session_degradation(created_session.record_path, degradation)
    validator = Draft202012Validator(
        _load_session_record_schema(),
        format_checker=FormatChecker(),
    )

    assert record["degradations"] == [degradation]
    assert read_session_record(created_session.record_path)["degradations"] == [degradation]
    validator.validate(record)


def test_append_session_failure_persists_schema_valid_fact(tmp_path: Path) -> None:
    created_session = create_session(
        profile=parse_capture_profile("online"),
        data_root=tmp_path,
        now=datetime(2026, 5, 8, 5, 30, 0, tzinfo=UTC),
    )
    failure = {
        "code": "capture_failed",
        "message": "Worker exited with status 3.",
        "source_id": None,
        "occurred_at": "2026-05-08T05:31:00Z",
        "recoverable": False,
    }

    record = append_session_failure(created_session.record_path, failure)
    validator = Draft202012Validator(
        _load_session_record_schema(),
        format_checker=FormatChecker(),
    )

    assert record["failures"] == [failure]
    assert read_session_record(created_session.record_path)["failures"] == [failure]
    validator.validate(record)


def test_update_session_record_writes_atomically(tmp_path: Path) -> None:
    created_session = create_session(
        profile=parse_capture_profile("online"),
        data_root=tmp_path,
        now=datetime(2026, 5, 8, 5, 30, 0, tzinfo=UTC),
    )
    record_path = created_session.record_path

    update_session_discovery(
        record_path,
        {
            "version": 1,
            "profile": "online_dual_stream",
            "status": "available",
            "sources": [],
            "sinks": [],
            "playback_streams": [],
            "error": None,
        },
    )

    assert not (record_path.parent / f".{record_path.name}.tmp").exists()
    record = read_session_record(record_path)
    discovery = record["discovery"]
    assert isinstance(discovery, dict)
    assert discovery["status"] == "available"


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
        "sources": [
            {
                "id": 31,
                "route_id": "pipewire:node:31",
                "name": "alsa_input.headset",
                "description": "Headset Mic",
                "media_class": "Audio/Source",
                "active": True,
                "node_name": "alsa_input.headset",
                "object_serial": "131",
                "pw_record_target": "alsa_input.headset",
            }
        ],
        "sinks": [
            {
                "id": 42,
                "route_id": "pipewire:node:42",
                "name": "alsa_output.speakers",
                "description": "Speakers",
                "media_class": "Audio/Sink",
                "active": True,
                "node_name": "alsa_output.speakers",
                "object_serial": "142",
                "pw_record_target": "alsa_output.speakers",
            }
        ],
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
        "language": "en",
        "recording": _not_started_recording(),
        "capture_plan": None,
        "degradations": [],
        "failures": [],
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
        "language": "en",
        "recording": _not_started_recording(),
        "capture_plan": None,
        "degradations": [],
        "failures": [],
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


def test_update_session_capture_plan_writes_schema_valid_session_record(tmp_path: Path) -> None:
    created_session = create_session(
        profile=parse_capture_profile("online"),
        data_root=tmp_path,
        now=datetime(2026, 5, 8, 5, 30, 0, tzinfo=UTC),
    )
    capture_plan = {
        "version": 1,
        "profile": "online_dual_stream",
        "language": "en",
        "segment_seconds": 30,
        "selected_sources": ["local_mic", "system_audio"],
        "routes": [
            {
                "role": "microphone",
                "source_id": "local_mic",
                "route_id": "pipewire:node:31",
                "label": "Headset Mic",
                "backend": "pipewire",
                "backend_name": "alsa_input.headset",
                "backend_target": "alsa_input.headset",
                "node_name": "alsa_input.headset",
                "object_serial": "131",
                "sink_input_id": None,
                "required": True,
            },
            {
                "role": "playback_stream",
                "source_id": "system_audio",
                "route_id": "pulse:sink-input:151",
                "label": "Meeting Stream",
                "backend": "pulse",
                "backend_name": "meeting-playback",
                "backend_target": "151",
                "node_name": None,
                "object_serial": None,
                "sink_input_id": "151",
                "required": True,
            },
            {
                "role": "output_sink",
                "source_id": None,
                "route_id": "pipewire:node:42",
                "label": "Speakers",
                "backend": "pipewire",
                "backend_name": "alsa_output.speakers",
                "backend_target": "alsa_output.speakers",
                "node_name": "alsa_output.speakers",
                "object_serial": "142",
                "sink_input_id": None,
                "required": True,
            },
        ],
        "status": "planned",
    }

    record = update_session_capture_plan(created_session.record_path, capture_plan)
    validator = Draft202012Validator(
        _load_session_record_schema(),
        format_checker=FormatChecker(),
    )

    assert record["capture_plan"] == capture_plan
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
        "profile": "online_dual_stream",
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
                "channel_layout": "mono",
                "sample_format": "pcm_s16le",
                "byte_count": 44,
                "duration_seconds": 30.0,
                "started_at": None,
                "ended_at": None,
                "sha256": "0" * 64,
                "route_id": "pipewire:node:31",
                "route_label": "Headset Mic",
                "capture_mode": "selected_microphone",
                "status": "sealed",
                "format": "wav",
            }
        ],
        "sources": [
            {
                "source_id": "local_mic",
                "route_id": "pipewire:node:31",
                "route_label": "Headset Mic",
                "capture_mode": "selected_microphone",
                "channel_layout": "mono",
                "segment_count": 1,
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


def _not_started_recording() -> dict[str, object]:
    return {
        "started_at": None,
        "stopped_at": None,
        "stop_reason": None,
        "finalization": {
            "status": "not_started",
            "completed_at": None,
            "error": None,
        },
    }
