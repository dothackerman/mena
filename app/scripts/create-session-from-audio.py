#!/usr/bin/env python3
"""Create a Mena UAT session bundle from existing WAV files.

This is a narrow #40 helper for PM/UAT fixture preparation. It is intentionally
not a product CLI command: use it to seed an isolated scratch data root, then run
the installed `mena` product command against the generated session record.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import sys
import wave
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

APP_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = APP_ROOT / "src"
if SRC_ROOT.is_dir():
    sys.path.insert(0, str(SRC_ROOT))

PROFILE_ONLINE = "online"
PROFILE_IN_PERSON = "in-person"
PROFILE_ID_BY_CLI = {
    PROFILE_ONLINE: "online_dual_stream",
    PROFILE_IN_PERSON: "in_person_single_mic",
}
EXPECTED_CHANNELS_BY_PROFILE = {
    PROFILE_ONLINE: {1},
    PROFILE_IN_PERSON: {1, 2},
}
COPY_CHUNK_SIZE = 1024 * 1024
UAT_CHUNK_COUNT = 6
UAT_CHUNK_SECONDS = 30


@dataclass(frozen=True)
class WavInfo:
    sample_rate: int
    channels: int
    sample_width: int
    frame_count: int
    pcm_payload: bytes

    @property
    def duration_seconds(self) -> float:
        return self.frame_count / self.sample_rate


@dataclass(frozen=True)
class SourceInput:
    source_id: str
    source_path: Path


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        session_path = create_session_from_audio(args)
    except UatSessionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"Created Mena UAT session: {session_path.parent}")
    print(f"Session record: {session_path}")
    print(f"Next: mena process {session_path}")
    return 0


def create_session_from_audio(args: argparse.Namespace) -> Path:
    profile_id = PROFILE_ID_BY_CLI[args.profile]
    created_at = _parse_created_at(args.created_at)
    session_id = _parse_session_id(args.session_id)
    source_inputs = _source_inputs(args)

    data_root = cast(Path, args.data_root).expanduser().resolve()
    bundle_path = data_root / "sessions" / _bundle_directory_name(session_id, created_at)
    if bundle_path.exists():
        raise UatSessionError(f"Session bundle already exists: {bundle_path}")
    bundle_path.mkdir(parents=True)

    try:
        segments = _copy_capture_segments(bundle_path=bundle_path, source_inputs=source_inputs)
        _validate_audio_shape(profile=args.profile, segments=segments)
        capture_metadata = _capture_metadata(segments)
        session_record = {
            "version": 1,
            "id": str(session_id),
            "created_at": _format_timestamp(created_at),
            "name": args.name,
            "status": "recorded",
            "profile": profile_id,
            "language": args.language,
            "recording": {
                "started_at": _format_timestamp(created_at),
                "stopped_at": _format_timestamp(created_at),
                "stop_reason": "user_stop",
                "finalization": {
                    "status": "finalized",
                    "completed_at": _format_timestamp(created_at),
                    "error": None,
                },
            },
            "capture_plan": None,
            "degradations": [],
            "failures": [],
            "capture_metadata": capture_metadata,
        }
        record_path = bundle_path / "session.json"
        _write_json(record_path, session_record)
    except Exception:
        shutil.rmtree(bundle_path, ignore_errors=True)
        raise

    return record_path


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=("Create an isolated Mena session bundle from WAV fixtures for #40 PM/UAT.")
    )
    parser.add_argument("--profile", choices=tuple(PROFILE_ID_BY_CLI), required=True)
    parser.add_argument(
        "--data-root",
        type=Path,
        required=True,
        help="Scratch data root. The session is written under <data-root>/sessions/.",
    )
    parser.add_argument("--name", default="Mena UAT synthetic fixture")
    parser.add_argument("--language", default="en")
    parser.add_argument("--local-mic", type=Path, help="WAV for online local microphone audio.")
    parser.add_argument("--system-audio", type=Path, help="WAV for online remote/system audio.")
    parser.add_argument("--room-mic", type=Path, help="WAV for in-person room microphone audio.")
    parser.add_argument(
        "--created-at",
        help="Optional UTC timestamp, e.g. 2026-05-25T13:00:00Z.",
    )
    parser.add_argument("--session-id", help="Optional UUID for reproducible fixture paths.")
    return parser.parse_args(argv)


def _source_inputs(args: argparse.Namespace) -> tuple[SourceInput, ...]:
    if args.profile == PROFILE_ONLINE:
        _require(args.local_mic, "--local-mic is required for --profile online.")
        _require(args.system_audio, "--system-audio is required for --profile online.")
        _reject(args.room_mic, "--room-mic is only valid for --profile in-person.")
        return (
            SourceInput(source_id="local_mic", source_path=args.local_mic),
            SourceInput(source_id="system_audio", source_path=args.system_audio),
        )

    _require(args.room_mic, "--room-mic is required for --profile in-person.")
    _reject(args.local_mic, "--local-mic is only valid for --profile online.")
    _reject(args.system_audio, "--system-audio is only valid for --profile online.")
    return (SourceInput(source_id="room_mic", source_path=args.room_mic),)


def _copy_capture_segments(
    *,
    bundle_path: Path,
    source_inputs: tuple[SourceInput, ...],
) -> list[dict[str, object]]:
    segments: list[dict[str, object]] = []
    for source in source_inputs:
        wav_path = source.source_path.expanduser().resolve()
        info = _read_wav_info(wav_path)
        target_dir = bundle_path / "capture" / source.source_id
        target_dir.mkdir(parents=True, exist_ok=True)
        for index in range(1, UAT_CHUNK_COUNT + 1):
            target_path = target_dir / f"{source.source_id}_{index:06d}.wav"
            chunk_payload = _chunk_wav_bytes(info=info, chunk_index=index)
            target_path.write_bytes(chunk_payload)
            segments.append(
                {
                    "source_id": source.source_id,
                    "index": index,
                    "path": target_path.relative_to(bundle_path).as_posix(),
                    "sample_rate": info.sample_rate,
                    "channels": info.channels,
                    "channel_layout": _channel_layout(info.channels),
                    "sample_format": f"pcm_s{info.sample_width * 8}le",
                    "byte_count": len(chunk_payload),
                    "sha256": hashlib.sha256(chunk_payload).hexdigest(),
                    "duration_seconds": float(UAT_CHUNK_SECONDS),
                    "started_at": None,
                    "ended_at": None,
                    "status": "sealed",
                    "format": "wav",
                    "_frame_count": info.sample_rate * UAT_CHUNK_SECONDS,
                    "_duration_seconds": float(UAT_CHUNK_SECONDS),
                }
            )
    return segments


@dataclass(frozen=True)
class CopyResult:
    byte_count: int
    sha256: str


def _copy_and_hash(source_path: Path, target_path: Path) -> CopyResult:
    digest = hashlib.sha256()
    byte_count = 0
    with source_path.open("rb") as source_file, target_path.open("wb") as target_file:
        for chunk in iter(lambda: source_file.read(COPY_CHUNK_SIZE), b""):
            byte_count += len(chunk)
            digest.update(chunk)
            target_file.write(chunk)
    return CopyResult(byte_count=byte_count, sha256=digest.hexdigest())


def _read_wav_info(path: Path) -> WavInfo:
    if not path.is_file():
        raise UatSessionError(f"WAV file does not exist: {path}")
    try:
        with wave.open(str(path), "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_rate = wav_file.getframerate()
            sample_width = wav_file.getsampwidth()
            frame_count = wav_file.getnframes()
            payload = wav_file.readframes(frame_count)
    except (EOFError, wave.Error) as exc:
        raise UatSessionError(f"Not a supported PCM WAV file: {path}") from exc

    if sample_rate <= 0:
        raise UatSessionError(f"WAV file sample rate must be positive: {path}")
    if sample_width != 2:
        raise UatSessionError(f"WAV file must be 16-bit PCM for current UAT fixtures: {path}")
    if frame_count <= 0:
        raise UatSessionError(f"WAV file contains no audio frames: {path}")
    expected_byte_count = frame_count * channels * sample_width
    if len(payload) != expected_byte_count:
        raise UatSessionError(f"WAV file does not contain declared PCM frame data: {path}")
    return WavInfo(
        sample_rate=sample_rate,
        channels=channels,
        sample_width=sample_width,
        frame_count=frame_count,
        pcm_payload=payload,
    )


def _chunk_wav_bytes(*, info: WavInfo, chunk_index: int) -> bytes:
    frames_per_chunk = info.sample_rate * UAT_CHUNK_SECONDS
    bytes_per_frame = info.channels * info.sample_width
    bytes_per_chunk = frames_per_chunk * bytes_per_frame
    start = (chunk_index - 1) * bytes_per_chunk
    payload = _repeat_or_slice(info.pcm_payload, start=start, byte_count=bytes_per_chunk)
    output = BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(info.channels)
        wav_file.setsampwidth(info.sample_width)
        wav_file.setframerate(info.sample_rate)
        wav_file.writeframes(payload)
    return output.getvalue()


def _repeat_or_slice(payload: bytes, *, start: int, byte_count: int) -> bytes:
    if not payload:
        raise UatSessionError("WAV file contains no PCM payload.")
    if start + byte_count <= len(payload):
        return payload[start : start + byte_count]
    repeated = payload * math.ceil((start + byte_count) / len(payload))
    return repeated[start : start + byte_count]


def _validate_audio_shape(*, profile: str, segments: list[dict[str, object]]) -> None:
    sample_rates = {segment["sample_rate"] for segment in segments}
    channels = {segment["channels"] for segment in segments}
    if len(sample_rates) > 1:
        raise UatSessionError("All source WAV files must use the same sample rate.")
    if len(channels) > 1:
        raise UatSessionError("All source WAV files must use the same channel count.")
    expected_channels = EXPECTED_CHANNELS_BY_PROFILE[profile]
    if not channels.issubset(expected_channels):
        allowed = ", ".join(str(channel) for channel in sorted(expected_channels))
        raise UatSessionError(
            f"{profile} fixtures must use one of these channel counts per source: {allowed}."
        )


def _capture_metadata(segments: list[dict[str, object]]) -> dict[str, object]:
    public_segments = []
    max_duration = 0.0
    for segment in segments:
        segment = dict(segment)
        duration = segment.pop("_duration_seconds")
        segment.pop("_frame_count", None)
        if not isinstance(duration, float):
            raise AssertionError("Internal duration marker should be a float.")
        max_duration = max(max_duration, duration)
        public_segments.append(segment)

    return {
        "version": 1,
        "profile": _profile_for_segments(public_segments),
        "sample_rate": public_segments[0]["sample_rate"],
        "channels": public_segments[0]["channels"],
        "segment_seconds": UAT_CHUNK_SECONDS,
        "segment_count": len(public_segments),
        "segments": public_segments,
        "sources": _source_summaries(public_segments),
    }


def _profile_for_segments(segments: list[dict[str, object]]) -> str:
    source_ids = {segment["source_id"] for segment in segments}
    if source_ids == {"room_mic"}:
        return "in_person_single_mic"
    return "online_dual_stream"


def _source_summaries(segments: list[dict[str, object]]) -> list[dict[str, object]]:
    summaries: list[dict[str, object]] = []
    for source_id in dict.fromkeys(str(segment["source_id"]) for segment in segments):
        source_segments = [segment for segment in segments if segment["source_id"] == source_id]
        summaries.append(
            {
                "source_id": source_id,
                "route_id": None,
                "route_label": None,
                "channel_layout": source_segments[0]["channel_layout"],
                "segment_count": len(source_segments),
            }
        )
    return summaries


def _channel_layout(channels: int) -> str:
    if channels == 1:
        return "mono"
    if channels == 2:
        return "stereo"
    return f"{channels}_channels"


def _parse_created_at(value: str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    normalized = value.removesuffix("Z") + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise UatSessionError(f"Invalid --created-at timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise UatSessionError("--created-at must include a timezone, preferably Z.")
    return parsed.astimezone(UTC)


def _parse_session_id(value: str | None) -> UUID:
    if value is None:
        return uuid4()
    try:
        return UUID(value)
    except ValueError as exc:
        raise UatSessionError(f"Invalid --session-id UUID: {value}") from exc


def _bundle_directory_name(session_id: UUID, created_at: datetime) -> str:
    timestamp = _format_timestamp(created_at).replace(":", "")
    return f"{session_id}_{timestamp}"


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _require(value: object | None, message: str) -> None:
    if value is None:
        raise UatSessionError(message)


def _reject(value: object | None, message: str) -> None:
    if value is not None:
        raise UatSessionError(message)


class UatSessionError(Exception):
    """Raised when the fixture cannot become a valid Mena UAT session."""


if __name__ == "__main__":
    raise SystemExit(main())
