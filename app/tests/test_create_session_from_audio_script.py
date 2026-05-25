import hashlib
import json
import subprocess
import sys
import wave
from pathlib import Path

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
    assert record["capture_metadata"]["segment_count"] == 2
    assert {segment["source_id"] for segment in record["capture_metadata"]["segments"]} == {
        "local_mic",
        "system_audio",
    }
    assert verify_session_bundle(record_path).ok

    for segment in record["capture_metadata"]["segments"]:
        copied = session_dir / segment["path"]
        payload = copied.read_bytes()
        assert copied.is_file()
        assert segment["byte_count"] == len(payload)
        assert segment["sha256"] == hashlib.sha256(payload).hexdigest()


def test_create_in_person_uat_session_from_room_audio(tmp_path: Path) -> None:
    room_wav = tmp_path / "room.wav"
    data_root = tmp_path / "uat-data"
    _write_wav(room_wav, channels=2)

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
    assert record["capture_metadata"]["channels"] == 2
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


def _write_wav(path: Path, *, sample_rate: int = 16_000, channels: int = 1) -> None:
    frame_count = sample_rate // 10
    frame = b"\x00\x00" * channels
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(frame * frame_count)
