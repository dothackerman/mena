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
from pathlib import Path
from uuid import UUID, uuid4

PROFILE_ONLINE = "online"
PROFILE_IN_PERSON = "in-person"
PROFILE_ID_BY_CLI = {
    PROFILE_ONLINE: "online_dual_stream",
    PROFILE_IN_PERSON: "in_person_single_mic",
}
COPY_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class WavInfo:
    sample_rate: int
    channels: int
    frame_count: int

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

    data_root = args.data_root.expanduser().resolve()
    bundle_path = data_root / "sessions" / _bundle_directory_name(session_id, created_at)
    if bundle_path.exists():
        raise UatSessionError(f"Session bundle already exists: {bundle_path}")
    bundle_path.mkdir(parents=True)

    try:
        segments = _copy_capture_segments(bundle_path=bundle_path, source_inputs=source_inputs)
        _validate_shared_audio_shape(segments)
        capture_metadata = _capture_metadata(segments)
        session_record = {
            "version": 1,
            "id": str(session_id),
            "created_at": _format_timestamp(created_at),
            "name": args.name,
            "status": "created",
            "profile": profile_id,
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
        target_path = target_dir / f"{source.source_id}_000001.wav"
        copy_result = _copy_and_hash(wav_path, target_path)
        segments.append(
            {
                "source_id": source.source_id,
                "index": 1,
                "path": target_path.relative_to(bundle_path).as_posix(),
                "sample_rate": info.sample_rate,
                "channels": info.channels,
                "byte_count": copy_result.byte_count,
                "sha256": copy_result.sha256,
                "status": "sealed",
                "format": "wav",
                "_frame_count": info.frame_count,
                "_duration_seconds": info.duration_seconds,
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
    except (EOFError, wave.Error) as exc:
        raise UatSessionError(f"Not a supported PCM WAV file: {path}") from exc

    if sample_width != 2:
        raise UatSessionError(f"WAV file must be 16-bit PCM for current UAT fixtures: {path}")
    if frame_count <= 0:
        raise UatSessionError(f"WAV file contains no audio frames: {path}")
    return WavInfo(sample_rate=sample_rate, channels=channels, frame_count=frame_count)


def _validate_shared_audio_shape(segments: list[dict[str, object]]) -> None:
    sample_rates = {segment["sample_rate"] for segment in segments}
    channels = {segment["channels"] for segment in segments}
    frame_counts = {segment["_frame_count"] for segment in segments}
    if len(sample_rates) > 1:
        raise UatSessionError("All source WAV files must use the same sample rate.")
    if len(channels) > 1:
        raise UatSessionError("All source WAV files must use the same channel count.")
    if len(frame_counts) > 1:
        raise UatSessionError("Online source WAV files must have the same frame count.")


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
        "sample_rate": public_segments[0]["sample_rate"],
        "channels": public_segments[0]["channels"],
        "segment_seconds": max(1, math.ceil(max_duration)),
        "segment_count": len(public_segments),
        "segments": public_segments,
    }


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
