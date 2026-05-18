"""Manual notes ingestion boundary."""

from __future__ import annotations

import re
from hashlib import sha256
from pathlib import Path

from mena.artifacts import ArtifactRecord, ArtifactStore
from mena.summarization import MANUAL_NOTES_ARTIFACT_TYPE, MANUAL_NOTES_CONTRACT

MANUAL_NOTES_ARTIFACT_VERSION = 1


class ManualNotesIngestionError(RuntimeError):
    """Raised when a user-provided manual notes file cannot be ingested."""


def ingest_manual_notes_file(
    *,
    store: ArtifactStore,
    notes_path: str | Path | None,
) -> ArtifactRecord | None:
    """Persist an optional UTF-8 notes file as a raw manual-notes artifact."""

    if notes_path is None:
        return None

    path = Path(notes_path)
    data = _read_utf8_notes(path)
    digest = sha256(data).hexdigest()
    existing = _existing_manual_notes_artifact(store=store, sha256_digest=digest)
    if existing is not None:
        return existing

    return store.store(
        artifact_type=MANUAL_NOTES_ARTIFACT_TYPE,
        name=_available_manual_notes_artifact_name(
            store=store,
            path=path,
            sha256_digest=digest,
        ),
        data=data,
        version=MANUAL_NOTES_ARTIFACT_VERSION,
        metadata={"contract": MANUAL_NOTES_CONTRACT},
    )


def _read_utf8_notes(path: Path) -> bytes:
    if not path.exists():
        raise ManualNotesIngestionError(f"Manual notes file does not exist: {path}")
    if not path.is_file():
        raise ManualNotesIngestionError(f"Manual notes path must be a file: {path}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ManualNotesIngestionError(f"Manual notes file could not be read: {path}") from exc
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ManualNotesIngestionError(f"Manual notes file must be valid UTF-8: {path}") from exc
    return data


def _existing_manual_notes_artifact(
    *,
    store: ArtifactStore,
    sha256_digest: str,
) -> ArtifactRecord | None:
    for record in store.list(artifact_type=MANUAL_NOTES_ARTIFACT_TYPE):
        if (
            record.sha256 == sha256_digest
            and record.metadata.get("contract") == MANUAL_NOTES_CONTRACT
            and store.verify(record.id).ok
        ):
            return record
    return None


def _available_manual_notes_artifact_name(
    *,
    store: ArtifactStore,
    path: Path,
    sha256_digest: str,
) -> Path:
    candidate = _manual_notes_artifact_name(path=path, sha256_digest=sha256_digest)
    used_paths = {
        Path(record.path) for record in store.list(artifact_type=MANUAL_NOTES_ARTIFACT_TYPE)
    }
    if _manual_notes_record_path(candidate) not in used_paths:
        return candidate

    for index in range(2, 1000):
        next_candidate = candidate.with_stem(f"{candidate.stem}-{index}")
        if _manual_notes_record_path(next_candidate) not in used_paths:
            return next_candidate
    raise ManualNotesIngestionError("Could not allocate a manual notes artifact name.")


def _manual_notes_artifact_name(*, path: Path, sha256_digest: str) -> Path:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", path.stem).strip("-._") or "notes"
    suffix = path.suffix if path.suffix.lower() in {".md", ".markdown", ".txt"} else ".txt"
    return Path("raw") / f"{stem}-{sha256_digest[:12]}{suffix}"


def _manual_notes_record_path(name: Path) -> Path:
    return Path("artifacts") / MANUAL_NOTES_ARTIFACT_TYPE / name
