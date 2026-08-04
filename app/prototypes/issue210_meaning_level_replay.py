#!/usr/bin/env python3
"""Private meaning-level replay for the issue #210 ordinary session.

The experiment freezes the first replay's transcript, extracted candidates,
model, and runtime options. It changes only post-extraction selection and
abstraction. All meeting-derived output remains below ``--output``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

try:
    from prototypes.issue210_bounded_ordinary_export import (
        NUM_CTX,
        BoundExceeded,
        Limits,
        Replay,
        _find_artifact,
        _time,
        _valid_ids,
        checkpoint_id,
        sha256_file,
    )
except ModuleNotFoundError:  # Direct execution adds this file's folder to sys.path.
    from issue210_bounded_ordinary_export import (  # type: ignore[no-redef]
        NUM_CTX,
        BoundExceeded,
        Limits,
        Replay,
        _find_artifact,
        _time,
        _valid_ids,
        checkpoint_id,
        sha256_file,
    )

PROMPT_VERSION = "issue210-meaning-level-abstraction-v1"
SCHEMA_VERSION = "issue210-private-meaning-level-v1"


@dataclass(frozen=True)
class MeaningLimits:
    max_calls: int = 22
    max_wall_seconds: int = 10_800
    timeline_minimum: int = 4
    timeline_maximum: int = 8
    actions_per_chunk: int = 3
    summary_sections: int = 2


def _normalized(text: str) -> str:
    return " ".join(text.casefold().split()).strip(" .,!?:;\"'“”„")


def _all_product_source_ids(package: dict[str, Any]) -> set[str]:
    items = list(package.get("timeline", [])) + list(package.get("general_summary_sections", []))
    return {
        source_id
        for item in items
        for source_id in item.get("source_segment_ids", [])
        if isinstance(source_id, str)
    }


def evaluate_package(
    *, package: dict[str, Any], segments: list[dict[str, Any]], rubric: dict[str, Any]
) -> dict[str, Any]:
    """Evaluate the private package against an owner-authored evidence rubric."""

    timeline = package.get("timeline", [])
    actions = package.get("action_items", [])
    source_texts = {_normalized(str(segment["text"])) for segment in segments}
    timeline_has_intervals = bool(timeline) and all(
        isinstance(item.get("start_seconds"), (int, float))
        and isinstance(item.get("end_seconds"), (int, float))
        and float(item["end_seconds"]) > float(item["start_seconds"])
        for item in timeline
    )
    final_end = max(
        (float(item.get("end_seconds", item.get("start_seconds", 0.0))) for item in timeline),
        default=0.0,
    )
    required_end = float(rubric["meeting_end_seconds"]) * float(
        rubric["minimum_timeline_coverage_ratio"]
    )
    verbatim_count = sum(
        _normalized(str(item.get("text", ""))) in source_texts for item in timeline
    )
    verbatim_ratio = verbatim_count / len(timeline) if timeline else 1.0

    visible_source_ids = _all_product_source_ids(package)
    missing_topics = [
        topic["name"]
        for topic in rubric["required_topics"]
        if not visible_source_ids.intersection(topic["source_segment_ids"])
    ]

    expected_actions = rubric["explicit_actions"]
    non_commitment_action_indexes: list[int] = []
    unsupported_detail_indexes: list[int] = []
    for index, action in enumerate(actions):
        cited = set(action.get("source_segment_ids", []))
        matches = [
            expected
            for expected in expected_actions
            if cited.intersection(expected["source_segment_ids"])
        ]
        if not matches:
            non_commitment_action_indexes.append(index)
            continue
        if action.get("owner") and not any(item.get("owner_supported") for item in matches):
            unsupported_detail_indexes.append(index)
        if action.get("due") and not any(item.get("due_supported") for item in matches):
            unsupported_detail_indexes.append(index)

    checks = {
        "timeline_has_intervals": timeline_has_intervals,
        "timeline_reaches_final_phase": final_end >= required_end,
        "timeline_is_meaning_level": verbatim_ratio
        <= float(rubric["maximum_verbatim_timeline_ratio"]),
        "required_topics_are_covered": not missing_topics,
        "actions_are_explicit_commitments": not non_commitment_action_indexes,
        "owners_and_due_dates_are_supported": not unsupported_detail_indexes,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "measurements": {
            "timeline_entry_count": len(timeline),
            "timeline_final_end_seconds": final_end,
            "timeline_verbatim_ratio": round(verbatim_ratio, 4),
            "action_count": len(actions),
        },
        "missing_topics": missing_topics,
        "non_commitment_action_indexes": non_commitment_action_indexes,
        "unsupported_detail_indexes": sorted(set(unsupported_detail_indexes)),
    }


PHASE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {
        "items": {
            "type": "array",
            "maxItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "source_segment_ids"],
                "properties": {
                    "text": {"type": "string", "maxLength": 300},
                    "source_segment_ids": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 12,
                        "items": {"type": "string"},
                    },
                },
            },
        }
    },
}

ACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {
        "items": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "owner", "due", "source_segment_ids"],
                "properties": {
                    "text": {"type": "string", "maxLength": 240},
                    "owner": {"type": ["string", "null"], "maxLength": 100},
                    "due": {"type": ["string", "null"], "maxLength": 100},
                    "source_segment_ids": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 8,
                        "items": {"type": "string"},
                    },
                },
            },
        }
    },
}

TIMELINE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {
        "items": {
            "type": "array",
            "minItems": 4,
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "source_segment_ids"],
                "properties": {
                    "text": {"type": "string", "maxLength": 360},
                    "source_segment_ids": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 16,
                        "items": {"type": "string"},
                    },
                },
            },
        }
    },
}

SUMMARY_PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["sections"],
    "properties": {
        "sections": {
            "type": "array",
            "minItems": 1,
            "maxItems": 2,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "timeline_indexes"],
                "properties": {
                    "title": {"type": "string", "maxLength": 100},
                    "timeline_indexes": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 6,
                        "items": {"type": "integer", "minimum": 0, "maximum": 7},
                    },
                },
            },
        }
    },
}

SUMMARY_SECTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text", "source_segment_ids"],
    "properties": {
        "text": {"type": "string", "maxLength": 700},
        "source_segment_ids": {
            "type": "array",
            "minItems": 1,
            "maxItems": 20,
            "items": {"type": "string"},
        },
    },
}


def _load_checkpoint(path: Path) -> Any:
    data = json.loads(path.read_text())
    if "payload" not in data:
        raise BoundExceeded(f"checkpoint_missing_payload: {path.name}")
    return data["payload"]


def _candidate_sources(
    candidates: list[dict[str, Any]], source: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    ids = list(
        dict.fromkeys(
            source_id
            for candidate in candidates
            for source_id in candidate.get("source_segment_ids", [])
            if source_id in source
        )
    )
    return [source[source_id] for source_id in ids]


def _validate_generated_items(
    response: dict[str, Any], source: dict[str, dict[str, Any]], *, maximum: int
) -> list[dict[str, Any]]:
    items = response.get("items")
    if not isinstance(items, list) or len(items) > maximum:
        raise BoundExceeded("intermediate_contract_violation")
    valid = []
    for item in items:
        ids = _valid_ids(item, source)
        if not ids or not isinstance(item.get("text"), str) or not item["text"].strip():
            raise BoundExceeded("intermediate_provenance_violation")
        valid.append({**item, "text": item["text"].strip(), "source_segment_ids": ids})
    return valid


def _derive_intervals(
    items: list[dict[str, Any]], source: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    result = []
    for item in items:
        ids = item["source_segment_ids"]
        result.append(
            {
                **item,
                "start_seconds": min(
                    float(source[source_id]["start_seconds"]) for source_id in ids
                ),
                "end_seconds": max(float(source[source_id]["end_seconds"]) for source_id in ids),
            }
        )
    return sorted(result, key=lambda item: (item["start_seconds"], item["end_seconds"]))


def _deduplicate_actions(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen_sources: set[tuple[str, ...]] = set()
    for item in items:
        key = tuple(item["source_segment_ids"])
        if key not in seen_sources:
            seen_sources.add(key)
            result.append(item)
    return result


def _render(package: dict[str, Any]) -> str:
    timeline = "\n".join(
        f"- [{_time(item['start_seconds'])}–{_time(item['end_seconds'])}] {item['text']}"
        for item in package["timeline"]
    )
    actions = "\n".join(f"- {item['text']}" for item in package["action_items"])
    summary = "\n\n".join(item["text"] for item in package["general_summary_sections"])
    return (
        f"## Zeitverlauf\n\n{timeline}\n\n## Aufgaben\n\n{actions}\n\n"
        f"## Zusammenfassung\n\n{summary}\n"
    )


def build_call_plan(*, chunk_count: int, limits: MeaningLimits) -> dict[str, Any]:
    stages = {
        "phase_abstraction": chunk_count,
        "commitment_classification": chunk_count,
        "timeline_consolidation": 1,
        "summary_planning": 1,
        "summary_section_generation": limits.summary_sections,
    }
    maximum = sum(stages.values())
    if maximum > limits.max_calls:
        raise BoundExceeded(f"planned_call_budget_exceeded: {maximum} > {limits.max_calls}")
    return {
        "stages": stages,
        "maximum_calls": maximum,
        "maximum_wall_seconds": limits.max_wall_seconds,
        "progress_measures": {
            "phase_abstraction": "remaining frozen transcript chunks",
            "commitment_classification": "remaining frozen action-candidate chunks",
            "timeline_consolidation": "one pending bounded consolidation",
            "summary_planning": "one pending whole-meeting plan",
            "summary_section_generation": "remaining planned sections",
        },
    }


def run(
    *,
    session: Path,
    baseline_output: Path,
    output: Path,
    rubric_path: Path,
    model: str,
    limits: MeaningLimits,
) -> Path:
    started = time.monotonic()
    output.mkdir(parents=True, exist_ok=True)
    transcript_path = _find_artifact(session, "merged_transcript", "attempt-*.json")
    raw = json.loads(transcript_path.read_text())
    segments = [
        {
            "id": item["id"],
            "start_seconds": item["start_seconds"],
            "end_seconds": item["end_seconds"],
            "speaker": item.get("speaker", {}).get("label"),
            "text": item["text"],
        }
        for item in raw["segments"]
    ]
    source = {item["id"]: item for item in segments}
    chunk_plan = _load_checkpoint(baseline_output / "checkpoints/02-chunk-plan.json")
    frozen = _load_checkpoint(baseline_output / "checkpoints/03-extracted-candidates.json")
    chunks = chunk_plan["chunks"]
    products = frozen["products"]
    call_plan = build_call_plan(chunk_count=len(chunks), limits=limits)
    (output / "dry-call-plan.json").write_text(json.dumps(call_plan, indent=2))

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "source_session_record_sha256": sha256_file(session / "session.json"),
        "transcript_sha256": sha256_file(transcript_path),
        "frozen_candidates_sha256": sha256_file(
            baseline_output / "checkpoints/03-extracted-candidates.json"
        ),
        "rubric_sha256": sha256_file(rubric_path),
        "code_commit": subprocess.run(
            ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip(),
        "code_sha256": sha256_file(Path(__file__)),
        "model": model,
        "runtime": {"num_ctx": NUM_CTX, "temperature": 0, "seed": 0},
        "limits": asdict(limits),
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2))
    replay = Replay(
        output=output,
        model=model,
        limits=Limits(max_calls=limits.max_calls, max_wall_seconds=limits.max_wall_seconds),
        started=started,
    )

    phase_candidates: list[dict[str, Any]] = []
    action_items: list[dict[str, Any]] = []
    for index, chunk_ids in enumerate(chunks):
        chunk_set = set(chunk_ids)
        timeline_candidates = [
            item
            for item in products["timeline"]
            if chunk_set.intersection(item.get("source_segment_ids", []))
        ]
        phase_response = replay.call(
            stage="phase_abstraction",
            product="timeline",
            system=(
                "Formuliere aus den Belegen höchstens eine knappe, bedeutungsbasierte "
                "Sitzungsphase auf Deutsch. Keine wörtlichen Transkriptfragmente, Begrüssungen "
                "oder Füllsätze. Benenne besprochene Themen, Klärungen, Entscheidungen oder "
                "Ergebnisse. Zitiere nur gelieferte Segment-IDs. Leere Ausgabe ist erlaubt."
            ),
            user={
                "chunk_index": index,
                "candidates": timeline_candidates,
                "original_evidence": _candidate_sources(timeline_candidates, source),
            },
            schema=PHASE_SCHEMA,
        )
        phase_candidates.extend(_validate_generated_items(phase_response, source, maximum=1))

        action_candidates = [
            item
            for item in products["action_items"]
            if chunk_set.intersection(item.get("source_segment_ids", []))
        ]
        action_response = replay.call(
            stage="commitment_classification",
            product="action_items",
            system=(
                "Gib nur ausdrücklich zugesagte, nach dem Gespräch ausführbare Aufgaben auf "
                "Deutsch zurück. Fragen, Beobachtungen, Möglichkeiten, bereits erledigte Arbeit "
                "und blosse Gesprächsziele sind keine Aufgaben. Erfinde weder verantwortlich "
                "Person noch Termin; verwende null, wenn nicht ausdrücklich belegt. Formuliere "
                "jede Aufgabe knapp auf Bedeutungsebene und zitiere nur gelieferte Segment-IDs."
            ),
            user={
                "chunk_index": index,
                "candidates": action_candidates,
                "original_evidence": _candidate_sources(action_candidates, source),
            },
            schema=ACTION_SCHEMA,
        )
        action_items.extend(
            _validate_generated_items(action_response, source, maximum=limits.actions_per_chunk)
        )

    timeline_response = replay.call(
        stage="timeline_consolidation",
        product="timeline",
        system=(
            "Verdichte die chronologischen Zwischenphasen zu 4 bis 8 bedeutungsbasierten "
            "Sitzungsphasen auf Deutsch. Decke das ganze Gespräch einschliesslich Schlussphase "
            "ab, verbinde benachbarte Phasen mit demselben Zweck und erhalte unterschiedliche "
            "Ergebnisse. Keine wörtlichen Transkriptfragmente. Zitiere nur gelieferte Segment-IDs."
        ),
        user={"phases": phase_candidates},
        schema=TIMELINE_SCHEMA,
    )
    timeline_items = _validate_generated_items(
        timeline_response, source, maximum=limits.timeline_maximum
    )
    if len(timeline_items) < limits.timeline_minimum:
        raise BoundExceeded("too_few_timeline_phases")
    timeline = _derive_intervals(timeline_items, source)
    actions = _deduplicate_actions(action_items)

    plan_response = replay.call(
        stage="summary_planning",
        product="general_summary",
        system=(
            "Plane höchstens zwei Abschnitte für eine kurze deutsche Zusammenfassung des ganzen "
            "Gesprächs. Wähle zentrale Unterscheidungen, Entscheidungen und Ergebnisse statt "
            "Nebendetails. Verwende nur gelieferte Timeline-Indizes."
        ),
        user={
            "timeline": [{"index": index, **item} for index, item in enumerate(timeline)],
            "actions": actions,
        },
        schema=SUMMARY_PLAN_SCHEMA,
    )
    sections: list[dict[str, Any]] = []
    for section in plan_response["sections"][: limits.summary_sections]:
        selected = [
            timeline[index] for index in section["timeline_indexes"] if 0 <= index < len(timeline)
        ]
        ids = list(
            dict.fromkeys(
                source_id for item in selected for source_id in item["source_segment_ids"]
            )
        )[:20]
        generated = replay.call(
            stage="summary_section_generation",
            product="general_summary",
            system=(
                "Schreibe einen knappen deutschen Absatz als Erinnerungshilfe. Abstrahiere die "
                "Bedeutung; kopiere keine Transkriptsätze. Verwende ausschliesslich die "
                "gelieferten Originalbelege, bewahre Unsicherheit und erfinde nichts. Zitiere nur "
                "gelieferte Segment-IDs."
            ),
            user={
                "title": section["title"],
                "phases": selected,
                "original_evidence": [source[source_id] for source_id in ids],
            },
            schema=SUMMARY_SECTION_SCHEMA,
        )
        valid_ids = _valid_ids(generated, source)
        if not valid_ids or not isinstance(generated.get("text"), str):
            raise BoundExceeded("summary_provenance_violation")
        sections.append({"text": generated["text"].strip(), "source_segment_ids": valid_ids})

    package = {
        "language": "de",
        "timeline": timeline,
        "action_items": actions,
        "general_summary_sections": sections,
    }
    (output / "note-package-private.json").write_text(
        json.dumps(package, ensure_ascii=False, indent=2)
    )
    export = output / "meaning-level-session-notes.md"
    export.write_text(_render(package))
    rubric = json.loads(rubric_path.read_text())
    evaluation = evaluate_package(package=package, segments=segments, rubric=rubric)
    evaluation["experiment"] = {
        "call_plan": call_plan,
        "actual_calls": len(replay.calls),
        "actual_elapsed_seconds": round(time.monotonic() - started, 2),
        "max_prompt_bytes": max(call.prompt_bytes for call in replay.calls),
        "total_prompt_eval_count": sum(call.prompt_eval_count for call in replay.calls),
        "total_eval_count": sum(call.eval_count for call in replay.calls),
        "frozen_candidates_identity": checkpoint_id(products),
    }
    (output / "evaluator-private.json").write_text(
        json.dumps(evaluation, ensure_ascii=False, indent=2)
    )
    (output / "metrics-sanitized.json").write_text(
        json.dumps(
            {
                "passed": evaluation["passed"],
                "checks": evaluation["checks"],
                "measurements": evaluation["measurements"],
                "experiment": evaluation["experiment"],
            },
            indent=2,
        )
    )
    return export


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--baseline-output", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rubric", type=Path, required=True)
    parser.add_argument("--model", default="llama3.1:8b")
    parser.add_argument("--max-calls", type=int, default=22)
    parser.add_argument("--max-wall-seconds", type=int, default=10_800)
    args = parser.parse_args()
    result = run(
        session=args.session,
        baseline_output=args.baseline_output,
        output=args.output,
        rubric_path=args.rubric,
        model=args.model,
        limits=MeaningLimits(max_calls=args.max_calls, max_wall_seconds=args.max_wall_seconds),
    )
    print(result)


if __name__ == "__main__":
    main()
