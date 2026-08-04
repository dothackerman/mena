#!/usr/bin/env python3
"""Private, bounded replay experiment for issue #210.

The command never mutates its source session. Private inputs, prompts, responses,
checkpoints, note products, and exports are written only below ``--output``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PRODUCTS = ("timeline", "action_items", "general_summary")
PROMPT_VERSION = "issue210-product-extraction-v1"
SCHEMA_VERSION = "issue210-private-checkpoints-v1"
NUM_CTX = 8192
PROMPT_BUDGET_BYTES = 6200
CHUNK_BUDGET_BYTES = 4200


class BoundExceeded(RuntimeError):
    """A declared experiment bound would be or was exceeded."""


@dataclass(frozen=True)
class Limits:
    max_calls: int = 30
    max_wall_seconds: int = 10_800
    max_retries: int = 0
    max_depth: int = 1
    timeline_capacity: int = 18
    action_capacity: int = 12
    summary_sections: int = 2


@dataclass
class CallRecord:
    index: int
    stage: str
    product: str
    prompt_bytes: int
    prompt_eval_count: int
    eval_count: int
    elapsed_seconds: float
    request_sha256: str


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def checkpoint_id(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def build_call_plan(*, chunk_count: int, section_count: int, limits: Limits) -> dict[str, Any]:
    stages = {
        "extraction": chunk_count * len(PRODUCTS),
        "semantic_grouping": 0,
        "summary_planning": 1,
        "section_generation": section_count,
        "repairs": limits.max_retries,
    }
    maximum = sum(stages.values())
    if maximum > limits.max_calls:
        raise BoundExceeded(f"planned_call_budget_exceeded: {maximum} > {limits.max_calls}")
    return {
        "stages": stages,
        "maximum_calls": maximum,
        "maximum_wall_seconds": limits.max_wall_seconds,
        "retry_count": limits.max_retries,
        "maximum_depth": limits.max_depth,
        "progress_measures": {
            "extraction": "remaining product/chunk pairs",
            "summary_planning": "one pending plan call",
            "section_generation": "remaining planned sections",
        },
    }


def chunk_segments(segments: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    used = 0
    for segment in segments:
        size = len(canonical(segment).encode())
        if size > CHUNK_BUDGET_BYTES:
            raise BoundExceeded(f"source_unit_exceeds_budget: {segment.get('id')}")
        if current and used + size > CHUNK_BUDGET_BYTES:
            chunks.append(current)
            current, used = [], 0
        current.append(segment)
        used += size
    if current:
        chunks.append(current)
    return chunks


def _valid_ids(item: dict[str, Any], source: dict[str, dict[str, Any]]) -> list[str]:
    ids = item.get("source_segment_ids")
    if not isinstance(ids, list) or not ids or any(not isinstance(sid, str) for sid in ids):
        return []
    return (
        list(dict.fromkeys(sid for sid in ids if sid in source))
        if all(sid in source for sid in ids)
        else []
    )


def normalize_timeline(
    candidates: list[dict[str, Any]], segments: list[dict[str, Any]], *, capacity: int
) -> dict[str, Any]:
    source = {str(s["id"]): s for s in segments}
    valid: list[dict[str, Any]] = []
    invalid = 0
    duplicates = 0
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for candidate in candidates:
        ids = _valid_ids(candidate, source)
        text = candidate.get("text")
        if not ids or not isinstance(text, str) or not text.strip() or len(text) > 360:
            invalid += 1
            continue
        key = (" ".join(text.casefold().split()), tuple(ids))
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        valid.append(
            {
                "text": text.strip(),
                "source_segment_ids": ids,
                "start_seconds": min(float(source[sid]["start_seconds"]) for sid in ids),
            }
        )
    valid.sort(key=lambda item: (item["start_seconds"], item["text"]))
    return {
        "items": valid[:capacity],
        "invalid_count": invalid,
        "duplicate_count": duplicates,
        "omitted_count": max(0, len(valid) - capacity),
    }


def normalize_actions(
    candidates: list[dict[str, Any]], segments: list[dict[str, Any]], *, capacity: int
) -> dict[str, Any]:
    source = {str(s["id"]): s for s in segments}
    valid: list[dict[str, Any]] = []
    invalid = 0
    duplicates = 0
    seen_sources: set[tuple[str, ...]] = set()
    for candidate in candidates:
        ids = _valid_ids(candidate, source)
        text = candidate.get("text")
        if not ids or not isinstance(text, str) or not text.strip() or len(text) > 360:
            invalid += 1
            continue
        source_key = tuple(ids)
        if source_key in seen_sources:
            duplicates += 1
            continue
        seen_sources.add(source_key)
        valid.append(
            {
                "text": text.strip(),
                "owner": candidate.get("owner")
                if isinstance(candidate.get("owner"), str)
                else None,
                "due": candidate.get("due") if isinstance(candidate.get("due"), str) else None,
                "source_segment_ids": ids,
            }
        )
    return {
        "items": valid[:capacity],
        "invalid_count": invalid,
        "duplicate_count": duplicates,
        "omitted_count": max(0, len(valid) - capacity),
    }


def render_markdown(package: dict[str, Any]) -> str:
    labels = (
        ("Zeitverlauf", "Aufgaben", "Zusammenfassung")
        if str(package.get("language", "en")).startswith("de")
        else ("Timeline", "Action items", "General summary")
    )
    timeline = "\n".join(
        f"- [{_time(float(item['start_seconds']))}] {item['text']}" for item in package["timeline"]
    )
    actions = "\n".join(
        f"- {item['text']}{_action_details(item, package.get('language', 'en'))}"
        for item in package["action_items"]
    )
    return (
        f"## {labels[0]}\n\n{timeline}\n\n## {labels[1]}\n\n{actions}\n\n"
        f"## {labels[2]}\n\n{package['general_summary']}\n"
    )


def _time(seconds: float) -> str:
    value = int(seconds)
    hours, rem = divmod(value, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def _action_details(item: dict[str, Any], language: str) -> str:
    labels = ("Verantwortlich", "Fällig") if language.startswith("de") else ("Owner", "Due")
    parts = []
    if item.get("owner"):
        parts.append(f"{labels[0]}: {item['owner']}")
    if item.get("due"):
        parts.append(f"{labels[1]}: {item['due']}")
    return f" ({'; '.join(parts)})" if parts else ""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _schema(product: str) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "text": {"type": "string", "maxLength": 360},
        "source_segment_ids": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "items": {"type": "string"},
        },
    }
    required = ["text", "source_segment_ids"]
    if product == "action_items":
        properties.update(
            {
                "owner": {"type": ["string", "null"], "maxLength": 120},
                "due": {"type": ["string", "null"], "maxLength": 120},
            }
        )
        required += ["owner", "due"]
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["items", "omitted_due_to_budget"],
        "properties": {
            "items": {
                "type": "array",
                "maxItems": 6,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": required,
                    "properties": properties,
                },
            },
            "omitted_due_to_budget": {"type": "boolean"},
        },
    }


PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["sections", "omitted_topic_ids"],
    "properties": {
        "sections": {
            "type": "array",
            "minItems": 1,
            "maxItems": 2,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "topic_ids"],
                "properties": {
                    "title": {"type": "string", "maxLength": 100},
                    "topic_ids": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 8,
                        "items": {"type": "string"},
                    },
                },
            },
        },
        "omitted_topic_ids": {"type": "array", "items": {"type": "string"}},
    },
}
SECTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text", "source_segment_ids"],
    "properties": {
        "text": {"type": "string", "maxLength": 900},
        "source_segment_ids": {
            "type": "array",
            "minItems": 1,
            "maxItems": 16,
            "items": {"type": "string"},
        },
    },
}


class Replay:
    def __init__(self, *, output: Path, model: str, limits: Limits, started: float) -> None:
        self.output, self.model, self.limits, self.started = output, model, limits, started
        self.calls: list[CallRecord] = []

    def call(
        self, *, stage: str, product: str, system: str, user: Any, schema: dict[str, Any]
    ) -> dict[str, Any]:
        if len(self.calls) >= self.limits.max_calls:
            raise BoundExceeded("call_budget_exhausted")
        if time.monotonic() - self.started > self.limits.max_wall_seconds:
            raise BoundExceeded("wall_time_exhausted")
        user_text = canonical(user)
        prompt_bytes = len(system.encode()) + len(user_text.encode())
        if prompt_bytes > PROMPT_BUDGET_BYTES:
            raise BoundExceeded(f"prompt_budget_exceeded: {prompt_bytes}")
        payload = {
            "model": self.model,
            "stream": False,
            "format": schema,
            "options": {"temperature": 0, "seed": 0, "num_ctx": NUM_CTX, "num_predict": 900},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_text},
            ],
        }
        request_bytes = json.dumps(payload, ensure_ascii=False).encode()
        request = urllib.request.Request(
            "http://127.0.0.1:11434/api/chat",
            data=request_bytes,
            headers={"content-type": "application/json"},
        )
        call_started = time.monotonic()
        with urllib.request.urlopen(request, timeout=600) as response:
            body = json.loads(response.read())
        if body.get("done_reason") == "length":
            raise BoundExceeded("oversized_model_output")
        try:
            result = json.loads(body["message"]["content"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise BoundExceeded("malformed_model_output") from exc
        record = CallRecord(
            len(self.calls) + 1,
            stage,
            product,
            prompt_bytes,
            int(body.get("prompt_eval_count", 0)),
            int(body.get("eval_count", 0)),
            round(time.monotonic() - call_started, 3),
            hashlib.sha256(request_bytes).hexdigest(),
        )
        self.calls.append(record)
        self._write("calls.json", [asdict(item) for item in self.calls])
        self._write(f"private/call-{record.index:03d}.json", {"request": payload, "response": body})
        return result

    def _write(self, relative: str, value: Any) -> None:
        path = self.output / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2))


def _find_artifact(session: Path, kind: str, pattern: str) -> Path:
    paths = sorted((session / "artifacts" / kind).rglob(pattern))
    if len(paths) != 1:
        raise BoundExceeded(f"expected_one_{kind}_artifact: {len(paths)}")
    return paths[0]


def _checkpoint(output: Path, name: str, identity_input: Any, payload: Any) -> Any:
    path = output / f"checkpoints/{name}.json"
    identity = checkpoint_id(identity_input)
    if path.exists():
        existing = json.loads(path.read_text())
        if existing.get("identity") == identity:
            return existing["payload"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"identity": identity, "payload": payload}, ensure_ascii=False, indent=2)
    )
    return payload


def _model_info(model: str) -> dict[str, Any]:
    def command(*args: str) -> str:
        try:
            return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            return "unavailable"

    return {
        "tag": model,
        "ollama_version": command("ollama", "--version"),
        "show": command("ollama", "show", model, "--modelfile"),
    }


def run(
    *, session: Path, output: Path, model: str, limits: Limits, dry_run: bool = False
) -> Path | None:
    started = time.monotonic()
    output.mkdir(parents=True, exist_ok=True)
    transcript_path = _find_artifact(session, "merged_transcript", "attempt-*.json")
    notes_paths = (
        sorted((session / "artifacts/manual_notes").rglob("*.md"))
        if (session / "artifacts/manual_notes").exists()
        else []
    )
    raw = json.loads(transcript_path.read_text())
    segments = [
        {
            "id": s["id"],
            "start_seconds": s["start_seconds"],
            "end_seconds": s["end_seconds"],
            "speaker": s.get("speaker", {}).get("label"),
            "text": s["text"],
        }
        for s in raw["segments"]
    ]
    chunks = chunk_segments(segments)
    plan = build_call_plan(
        chunk_count=len(chunks), section_count=limits.summary_sections, limits=limits
    )
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "source_session": str(session.resolve()),
        "source_session_record_sha256": sha256_file(session / "session.json"),
        "transcript": {
            "path": str(transcript_path.resolve()),
            "sha256": sha256_file(transcript_path),
        },
        "manual_notes": [
            {"path": str(path.resolve()), "sha256": sha256_file(path)} for path in notes_paths
        ],
        "code_commit": commit,
        "model": _model_info(model),
        "runtime_profile": {
            "provider": "ollama",
            "num_ctx": NUM_CTX,
            "prompt_budget_bytes": PROMPT_BUDGET_BYTES,
            "temperature": 0,
            "seed": 0,
        },
        "prompt_version": PROMPT_VERSION,
        "hardware": {"platform": platform.platform(), "processor": platform.processor()},
        "private_output_directory": str(output.resolve()),
        "limits": asdict(limits),
    }
    _checkpoint(output, "01-manifest", manifest, manifest)
    _checkpoint(
        output,
        "02-chunk-plan",
        {"transcript": manifest["transcript"], "chunk_budget": CHUNK_BUDGET_BYTES},
        {"chunks": [[s["id"] for s in chunk] for chunk in chunks], "chunk_count": len(chunks)},
    )
    (output / "dry-call-plan.json").write_text(json.dumps(plan, indent=2))
    if dry_run:
        return None
    replay = Replay(output=output, model=model, limits=limits, started=started)
    extracted: dict[str, list[dict[str, Any]]] = {product: [] for product in PRODUCTS}
    omission_flags: dict[str, int] = {product: 0 for product in PRODUCTS}
    for product in PRODUCTS:
        for index, chunk in enumerate(chunks):
            identity_input = {
                "manifest": checkpoint_id(manifest),
                "product": product,
                "chunk": index,
                "segment_ids": [s["id"] for s in chunk],
            }
            name = f"03-extract-{product}-{index:03d}"
            existing_path = output / f"checkpoints/{name}.json"
            if existing_path.exists() and json.loads(existing_path.read_text()).get(
                "identity"
            ) == checkpoint_id(identity_input):
                response = json.loads(existing_path.read_text())["payload"]
            else:
                instruction = {
                    "timeline": "substantive events, decisions, and changes",
                    "action_items": (
                        "only explicit executable commitments; never infer an owner or due date"
                    ),
                    "general_summary": "atomic substantive topic evidence",
                }[product]
                response = replay.call(
                    stage="extraction",
                    product=product,
                    system=(
                        f"Extract German meeting-note {instruction}. Ignore greetings and setup "
                        "chatter. Cite only supplied segment ids. Return at most 6 atomic items "
                        "and set omitted_due_to_budget truthfully. Return strict JSON."
                    ),
                    user={"segments": chunk},
                    schema=_schema(product),
                )
                _checkpoint(output, name, identity_input, response)
            if not isinstance(response.get("items"), list) or len(response["items"]) > 6:
                raise BoundExceeded("intermediate_contract_violation")
            extracted[product].extend(response["items"])
            omission_flags[product] += int(bool(response.get("omitted_due_to_budget")))
    _checkpoint(
        output,
        "03-extracted-candidates",
        {"manifest": checkpoint_id(manifest), "prompt": PROMPT_VERSION},
        {"products": extracted, "omission_flags": omission_flags},
    )
    timeline = normalize_timeline(
        extracted["timeline"], segments, capacity=limits.timeline_capacity
    )
    actions = normalize_actions(
        extracted["action_items"], segments, capacity=limits.action_capacity
    )
    source = {s["id"]: s for s in segments}
    facts = []
    for index, item in enumerate(extracted["general_summary"]):
        ids = _valid_ids(item, source)
        if ids and isinstance(item.get("text"), str):
            facts.append(
                {"topic_id": f"t{index + 1:03d}", "text": item["text"], "source_segment_ids": ids}
            )
    validated = {
        "timeline": timeline,
        "action_items": actions,
        "summary_facts": facts[:24],
        "summary_fact_omitted_count": max(0, len(facts) - 24),
    }
    _checkpoint(
        output,
        "04-validated-evidence",
        {"extracted": checkpoint_id(extracted), "capacities": asdict(limits)},
        validated,
    )
    plan_response = replay.call(
        stage="summary_planning",
        product="general_summary",
        system=(
            "Select and order at most two sections for a short German high-level recall summary. "
            "Use only supplied topic ids. Prefer central outcomes and decisions over detail. "
            "Return strict JSON."
        ),
        user={"topics": validated["summary_facts"]},
        schema=PLAN_SCHEMA,
    )
    known = {fact["topic_id"]: fact for fact in validated["summary_facts"]}
    sections: list[str] = []
    section_audit: list[dict[str, Any]] = []
    for section in plan_response["sections"][: limits.summary_sections]:
        selected = [known[topic_id] for topic_id in section["topic_ids"] if topic_id in known][:8]
        ids = list(dict.fromkeys(sid for fact in selected for sid in fact["source_segment_ids"]))[
            :16
        ]
        evidence = [source[sid] for sid in ids]
        generated = replay.call(
            stage="section_generation",
            product="general_summary",
            system=(
                "Write one concise German paragraph for a meeting recall summary using only the "
                "supplied original evidence and selected facts. Invent nothing, preserve "
                "uncertainty, and avoid chronology unless essential. Return strict JSON."
            ),
            user={"title": section["title"], "facts": selected, "original_evidence": evidence},
            schema=SECTION_SCHEMA,
        )
        if not _valid_ids(generated, source):
            raise BoundExceeded("provenance_invalid_section")
        sections.append(generated["text"].strip())
        section_audit.append({"plan": section, "result": generated})
    if not sections:
        raise BoundExceeded("empty_general_summary")
    package = {
        "language": "de",
        "timeline": timeline["items"],
        "action_items": actions["items"],
        "general_summary": "\n\n".join(sections),
    }
    _checkpoint(
        output,
        "05-products",
        {"validated": checkpoint_id(validated), "plan": plan_response},
        {"package": package, "summary_plan": plan_response, "section_audit": section_audit},
    )
    _checkpoint(output, "06-note-package", {"products": checkpoint_id(package)}, package)
    markdown = render_markdown(package)
    export = output / "ordinary-session-notes.md"
    export.write_text(markdown)
    _checkpoint(
        output,
        "07-markdown",
        {"package": checkpoint_id(package)},
        {"path": str(export.resolve()), "sha256": sha256_file(export)},
    )
    evaluator = {
        "visible_claims": {
            "timeline": package["timeline"],
            "action_items": package["action_items"],
            "general_summary_sections": section_audit,
        },
        "omissions": {
            "extraction_capacity_flags": omission_flags,
            "timeline": timeline["omitted_count"],
            "action_items": actions["omitted_count"],
            "summary_facts": validated["summary_fact_omitted_count"],
            "plan_topic_ids": plan_response.get("omitted_topic_ids", []),
        },
        "validation": {
            "timeline_invalid": timeline["invalid_count"],
            "action_invalid": actions["invalid_count"],
            "timeline_duplicates": timeline["duplicate_count"],
            "action_duplicates": actions["duplicate_count"],
        },
        "measurements": {
            "declared": plan,
            "actual_calls": len(replay.calls),
            "actual_elapsed_seconds": round(time.monotonic() - started, 2),
            "per_stage_calls": {
                stage: sum(call.stage == stage for call in replay.calls) for stage in plan["stages"]
            },
            "max_prompt_bytes": max(call.prompt_bytes for call in replay.calls),
            "total_prompt_eval_count": sum(call.prompt_eval_count for call in replay.calls),
            "total_eval_count": sum(call.eval_count for call in replay.calls),
        },
    }
    replay._write("evaluator-private.json", evaluator)
    replay._write("metrics-sanitized.json", evaluator["measurements"])
    return export


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="llama3.1:8b")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-calls", type=int, default=30)
    parser.add_argument("--max-wall-seconds", type=int, default=10_800)
    args = parser.parse_args()
    result = run(
        session=args.session,
        output=args.output,
        model=args.model,
        limits=Limits(max_calls=args.max_calls, max_wall_seconds=args.max_wall_seconds),
        dry_run=args.dry_run,
    )
    print(result or args.output / "dry-call-plan.json")


if __name__ == "__main__":
    main()
