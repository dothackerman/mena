import hashlib
import json
import subprocess
import sys
import wave
from importlib.resources import files
from pathlib import Path
from typing import cast

from jsonschema import Draft202012Validator, FormatChecker

from mena.session_integrity import verify_session_bundle

APP_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = APP_ROOT / "scripts" / "create-session-from-audio.py"
SESSION_ID = "11111111-1111-4111-8111-111111111111"
CREATED_AT = "2026-05-25T13:00:00Z"


def test_create_online_uat_session_from_audio(tmp_path: Path) -> None:
    local_wav = tmp_path / "local.wav"
    system_wav = tmp_path / "system.wav"
    data_root = tmp_path / "uat-data"
    _write_wav(local_wav)
    _write_wav(system_wav)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--profile",
            "online",
            "--data-root",
            str(data_root),
            "--name",
            "Kai fixture",
            "--session-id",
            SESSION_ID,
            "--created-at",
            CREATED_AT,
            "--local-mic",
            str(local_wav),
            "--system-audio",
            str(system_wav),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    session_dir = data_root / "sessions" / f"{SESSION_ID}_2026-05-25T130000Z"
    record_path = session_dir / "session.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))

    assert f"Session record: {record_path}" in result.stdout
    assert f"Next: mena process {record_path}" in result.stdout
    assert record["id"] == SESSION_ID
    assert record["name"] == "Kai fixture"
    assert record["profile"] == "online_dual_stream"
    assert record["language"] == "en"
    assert record["status"] == "recorded"
    _validate_session_record_schema(record)
    assert record["capture_metadata"]["segment_count"] == 2
    assert {segment["source_id"] for segment in record["capture_metadata"]["segments"]} == {
        "local_mic",
        "system_audio",
    }
    assert {
        segment["index"]
        for segment in record["capture_metadata"]["segments"]
        if segment["source_id"] == "local_mic"
    } == {1}
    assert {
        (
            segment["route_id"],
            segment["route_label"],
            segment["capture_mode"],
        )
        for segment in record["capture_metadata"]["segments"]
    } == {(None, None, None)}
    assert {
        (
            source["route_id"],
            source["route_label"],
            source["capture_mode"],
        )
        for source in record["capture_metadata"]["sources"]
    } == {(None, None, None)}
    assert verify_session_bundle(record_path).ok

    for segment in record["capture_metadata"]["segments"]:
        copied = session_dir / segment["path"]
        assert copied.is_file()
        payload = copied.read_bytes()
        assert segment["byte_count"] == len(payload)
        assert segment["sha256"] == hashlib.sha256(payload).hexdigest()


def test_create_in_person_uat_session_from_room_audio(tmp_path: Path) -> None:
    room_wav = tmp_path / "room.wav"
    data_root = tmp_path / "uat-data"
    _write_wav(room_wav, channels=1)

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--profile",
            "in-person",
            "--data-root",
            str(data_root),
            "--session-id",
            SESSION_ID,
            "--created-at",
            CREATED_AT,
            "--room-mic",
            str(room_wav),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    record_path = data_root / "sessions" / f"{SESSION_ID}_2026-05-25T130000Z" / "session.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))

    assert record["profile"] == "in_person_single_mic"
    assert record["capture_metadata"]["channels"] == 1
    _validate_session_record_schema(record)
    assert record["capture_metadata"]["segment_count"] == 1
    assert record["capture_metadata"]["segments"][0]["source_id"] == "room_mic"
    assert verify_session_bundle(record_path).ok


def test_create_uat_session_rejects_mismatched_online_audio_shape(tmp_path: Path) -> None:
    local_wav = tmp_path / "local.wav"
    system_wav = tmp_path / "system.wav"
    _write_wav(local_wav, sample_rate=16_000)
    _write_wav(system_wav, sample_rate=48_000)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--profile",
            "online",
            "--data-root",
            str(tmp_path / "uat-data"),
            "--local-mic",
            str(local_wav),
            "--system-audio",
            str(system_wav),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "All source WAV files must use the same sample rate." in result.stderr
    sessions_dir = tmp_path / "uat-data" / "sessions"
    assert not sessions_dir.exists() or list(sessions_dir.iterdir()) == []


def test_create_uat_session_accepts_mismatched_online_input_lengths(tmp_path: Path) -> None:
    local_wav = tmp_path / "local.wav"
    system_wav = tmp_path / "system.wav"
    data_root = tmp_path / "uat-data"
    _write_wav(local_wav, frame_count=1_600)
    _write_wav(system_wav, frame_count=3_200)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--profile",
            "online",
            "--data-root",
            str(data_root),
            "--local-mic",
            str(local_wav),
            "--system-audio",
            str(system_wav),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    record_path = next((data_root / "sessions").iterdir()) / "session.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["capture_metadata"]["segment_count"] == 2
    durations = {
        segment["source_id"]: segment["duration_seconds"]
        for segment in record["capture_metadata"]["segments"]
    }
    assert durations == {"local_mic": 0.1, "system_audio": 0.2}


def test_create_uat_session_rejects_online_stereo_sources(tmp_path: Path) -> None:
    local_wav = tmp_path / "local.wav"
    system_wav = tmp_path / "system.wav"
    _write_wav(local_wav, channels=2)
    _write_wav(system_wav, channels=2)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--profile",
            "online",
            "--data-root",
            str(tmp_path / "uat-data"),
            "--local-mic",
            str(local_wav),
            "--system-audio",
            str(system_wav),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "online fixtures must use one of these channel counts per source: 1." in result.stderr


def test_create_uat_session_accepts_in_person_stereo_source(tmp_path: Path) -> None:
    room_wav = tmp_path / "room.wav"
    data_root = tmp_path / "uat-data"
    _write_wav(room_wav, channels=2)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--profile",
            "in-person",
            "--data-root",
            str(data_root),
            "--room-mic",
            str(room_wav),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    record_path = next((data_root / "sessions").iterdir()) / "session.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["capture_metadata"]["channels"] == 2


def test_create_uat_session_splits_audio_by_actual_length(
    tmp_path: Path,
) -> None:
    room_wav = tmp_path / "room.wav"
    data_root = tmp_path / "uat-data"
    _write_wav(room_wav, channels=1, frame_count=16_000 * 31)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--profile",
            "in-person",
            "--data-root",
            str(data_root),
            "--room-mic",
            str(room_wav),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    record_path = next((data_root / "sessions").iterdir()) / "session.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["capture_metadata"]["segment_count"] == 2
    assert [segment["duration_seconds"] for segment in record["capture_metadata"]["segments"]] == [
        30.0,
        1.0,
    ]


def test_create_uat_session_rejects_truncated_wav_payload(tmp_path: Path) -> None:
    room_wav = tmp_path / "room.wav"
    _write_wav(room_wav, channels=2)
    room_wav.write_bytes(room_wav.read_bytes()[:-2])

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--profile",
            "in-person",
            "--data-root",
            str(tmp_path / "uat-data"),
            "--room-mic",
            str(room_wav),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "WAV file does not contain declared PCM frame data" in result.stderr


def test_create_uat_session_rejects_empty_wav_without_traceback(tmp_path: Path) -> None:
    room_wav = tmp_path / "room.wav"
    room_wav.write_bytes(b"")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--profile",
            "in-person",
            "--data-root",
            str(tmp_path / "uat-data"),
            "--room-mic",
            str(room_wav),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert f"Not a supported PCM WAV file: {room_wav.resolve()}" in result.stderr
    assert "Traceback" not in result.stderr


def test_create_uat_session_rejects_wrong_profile_inputs(tmp_path: Path) -> None:
    room_wav = tmp_path / "room.wav"
    _write_wav(room_wav)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--profile",
            "online",
            "--data-root",
            str(tmp_path / "uat-data"),
            "--room-mic",
            str(room_wav),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "--local-mic is required for --profile online." in result.stderr


def _write_wav(
    path: Path,
    *,
    sample_rate: int = 16_000,
    channels: int = 1,
    frame_count: int | None = None,
) -> None:
    frame_count = frame_count or sample_rate // 10
    frame = b"\x00\x00" * channels
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(frame * frame_count)


def _validate_session_record_schema(record: dict[str, object]) -> None:
    Draft202012Validator(
        _load_session_record_schema(),
        format_checker=FormatChecker(),
    ).validate(record)


def _load_session_record_schema() -> dict[str, object]:
    schema_path = files("mena.schemas").joinpath("session-record.v1.schema.json")
    return cast(dict[str, object], json.loads(schema_path.read_text(encoding="utf-8")))
