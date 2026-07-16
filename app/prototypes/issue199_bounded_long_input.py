#!/usr/bin/env python3
"""PROTOTYPE for #199 Session 2: compare two complete bounded pipelines.

Question: can source-aware hierarchy or clustered evidence with a validated
content plan produce useful complete note packages without any unbounded model
call? Private source, prompts, intermediates, and products stay in the local UAT
directory. Only the separately emitted sanitized metrics may be committed.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import re
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from mena.note_package import SummaryActionItem, SummaryResult, SummaryTimelineEntry
from mena.summary_markdown import format_summary_markdown

FIXTURE = Path(
    "/home/oriol/.local/share/mena/sessions/c575b55f-352f-47b9-96c5-affc7d5f29d7_2026-06-30T130940Z"
)
TRANSCRIPT = (
    FIXTURE
    / "artifacts/merged_transcript"
    / "4a45ee69-8173-4198-994d-a09f63bd8857"
    / "144f943e-7a6af3fc"
    / "17cb058d1294"
    / "attempt-001.json"
)
NOTES = FIXTURE / "artifacts/manual_notes/raw/2026-06-30_Meeting_Alex-ddab7d4cb233.md"
DEFAULT_OUT = Path(
    "/home/oriol/.openclaw/workspace/product-manager/uat/mena/session-c575b55f-2026-06-30/issue-199-session-2"
)
PRODUCTS = ("timeline", "action_items", "general_summary")
NUM_CTX = 8192
PROMPT_BUDGET = int(NUM_CTX * 0.85)
MAX_PACKED_BYTES = 4200
MAX_PLAN_GROUPS = 14

ITEM_PROPERTIES = {
    "text": {"type": "string", "maxLength": 360},
    "owner": {"type": ["string", "null"]},
    "due": {"type": ["string", "null"]},
    "source_segment_ids": {"type": "array", "maxItems": 12, "items": {"type": "string"}},
    "coverage": {"type": "array", "maxItems": 8, "items": {"type": "string"}},
    "conflict": {"type": ["string", "null"], "maxLength": 240},
}


def product_schema(product: str, *, final: bool = False) -> dict[str, Any]:
    if product == "general_summary" and final:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["general_summary", "source_segment_ids"],
            "properties": {
                "general_summary": {"type": "string", "maxLength": 1800},
                "source_segment_ids": ITEM_PROPERTIES["source_segment_ids"],
            },
        }
    required = ["text", "source_segment_ids", "coverage", "conflict"]
    if product == "action_items":
        required += ["owner", "due"]
    properties = {key: ITEM_PROPERTIES[key] for key in required}
    key = product if product != "general_summary" else "summary_facts"
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [key],
        "properties": {
            key: {
                "type": "array",
                "maxItems": 18 if final else 10,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": required,
                    "properties": properties,
                },
            }
        },
    }


PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["selected_group_ids", "selection_reasons", "omitted_group_ids", "omission_reason"],
    "properties": {
        "selected_group_ids": {
            "type": "array",
            "maxItems": MAX_PLAN_GROUPS,
            "items": {"type": "string"},
        },
        "selection_reasons": {
            "type": "array",
            "maxItems": MAX_PLAN_GROUPS,
            "items": {"type": "string", "maxLength": 180},
        },
        "omitted_group_ids": {"type": "array", "items": {"type": "string"}},
        "omission_reason": {"type": ["string", "null"], "maxLength": 240},
    },
}


@dataclass
class Call:
    index: int
    prompt_eval_count: int
    eval_count: int
    elapsed_seconds: float
    prompt_bytes: int
    request_sha256: str


CALL_DIR: Path | None = None
CALLS: list[Call] = []


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def post(
    *, model: str, system: str, user: Any, schema: dict[str, Any], timeout: float
) -> dict[str, Any]:
    user_text = compact(user)
    prompt_bytes = len(system.encode()) + len(user_text.encode())
    if prompt_bytes > PROMPT_BUDGET:
        raise RuntimeError(f"preflight refused {prompt_bytes} > {PROMPT_BUDGET}")
    payload = {
        "model": model,
        "stream": False,
        "format": schema,
        "options": {"temperature": 0, "seed": 0, "num_ctx": NUM_CTX, "num_predict": 1200},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user_text}],
    }
    request_bytes = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(
        "http://localhost:11434/api/chat",
        data=request_bytes,
        headers={"content-type": "application/json"},
    )
    started = time.monotonic()
    body = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                body = json.loads(response.read())
            break
        except (http.client.RemoteDisconnected, TimeoutError, urllib.error.URLError):
            if attempt == 3:
                raise
            time.sleep(attempt)
    assert body is not None
    if body.get("done_reason") == "length":
        raise RuntimeError("model response reached length limit")
    result = json.loads(body["message"]["content"])
    call = Call(
        len(CALLS) + 1,
        int(body["prompt_eval_count"]),
        int(body["eval_count"]),
        round(time.monotonic() - started, 3),
        prompt_bytes,
        hashlib.sha256(request_bytes).hexdigest(),
    )
    CALLS.append(call)
    assert CALL_DIR is not None
    (CALL_DIR / f"call-{call.index:04d}.json").write_text(
        json.dumps(
            {"request": payload, "response": body, "preflight": asdict(call)},
            ensure_ascii=False,
            indent=2,
        )
    )
    return result


def load() -> tuple[list[dict[str, Any]], str]:
    raw = json.loads(TRANSCRIPT.read_text())
    return (
        [
            {
                "id": s["id"],
                "start_seconds": s["start_seconds"],
                "end_seconds": s["end_seconds"],
                "speaker": s["speaker"]["label"],
                "text": s["text"],
            }
            for s in raw["segments"]
        ],
        NOTES.read_text(),
    )


def chunks(segments: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    out: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    used = 0
    for segment in segments:
        size = len(compact(segment).encode())
        if current and used + size > 5000:
            out.append(current)
            current = []
            used = 0
        current.append(segment)
        used += size
    if current:
        out.append(current)
    return out


def key_for(product: str) -> str:
    return product if product != "general_summary" else "summary_facts"


def map_product(
    product: str, chunk: list[dict[str, Any]], model: str, timeout: float
) -> dict[str, Any]:
    instruction = {
        "timeline": "substantive chronological events",
        "action_items": "only explicit executable commitments; never infer owner or due date",
        "general_summary": "atomic substantive topic facts",
    }[product]
    return post(
        model=model,
        timeout=timeout,
        schema=product_schema(product),
        system=(
            f"Extract German meeting-note {instruction}. Ignore greetings, travel, "
            "setup, and farewells unless substantive. Every item must cite only supplied "
            "segment ids, name coverage concepts, and preserve uncertainty or "
            "contradiction explicitly. Return strict schema JSON."
        ),
        user={"chunk": chunk},
    )


def packed_groups(items: list[Any], *, max_bytes: int = MAX_PACKED_BYTES) -> list[list[Any]]:
    out: list[list[Any]] = []
    current: list[Any] = []
    used = 0
    for item in items:
        size = len(compact(item).encode())
        if size > max_bytes:
            raise RuntimeError(f"single intermediate exceeds bound: {size}")
        if current and used + size > max_bytes:
            out.append(current)
            current = []
            used = 0
        current.append(item)
        used += size
    if current:
        out.append(current)
    return out


def cited_source(
    items: list[dict[str, Any]], source: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    ids = list(
        dict.fromkeys(
            sid for item in items for sid in item.get("source_segment_ids", []) if sid in source
        )
    )
    selected: list[dict[str, Any]] = []
    used = 0
    for sid in ids:
        segment = source[sid]
        size = len(compact(segment).encode())
        if selected and used + size > 2800:
            break
        selected.append(segment)
        used += size
    return selected


def candidate_c(
    product: str,
    mapped: list[dict[str, Any]],
    source: dict[str, dict[str, Any]],
    notes: str,
    model: str,
    timeout: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    key = key_for(product)
    nodes = [item for payload in mapped for item in payload[key]]
    depth = 0
    level_counts = [len(nodes)]
    while len(nodes) > (1 if product == "general_summary" else 12):
        groups = packed_groups(nodes, max_bytes=2400)
        if len(groups) >= len(nodes):
            raise RuntimeError("non_contracting_reduction")
        next_nodes: list[dict[str, Any]] = []
        for group in groups:
            evidence = cited_source(group, source)
            result = post(
                model=model,
                timeout=timeout,
                schema=product_schema(product),
                system=(
                    f"Source-aware German {product} merge. Merge these candidates using "
                    "rehydrated original evidence. Preserve citations, chronology, "
                    "conflicts, uncertainty, and coverage. Deduplicate only equivalent "
                    "claims. Contract to at most 10 items. Return strict JSON."
                ),
                user={
                    "candidates": group,
                    "original_source": evidence,
                    "manual_notes": notes if depth == 0 else None,
                },
            )
            next_nodes.extend(result[key])
        if len(compact(next_nodes).encode()) >= len(compact(nodes).encode()) and len(
            next_nodes
        ) >= len(nodes):
            raise RuntimeError("non_contracting_reduction")
        nodes = next_nodes
        depth += 1
        level_counts.append(len(nodes))
    if product == "general_summary":
        final = post(
            model=model,
            timeout=timeout,
            schema=product_schema(product, final=True),
            system=(
                "Write one coherent German meeting overview from these source-aware "
                "facts and selected original evidence. Preserve uncertainty, avoid "
                "repetition and low-signal chatter, invent nothing. Return strict JSON."
            ),
            user={
                "facts": nodes,
                "original_source": cited_source(nodes, source),
                "manual_notes": notes,
            },
        )
    else:
        final = {product: nodes}
    return final, {
        "reduction_depth": depth,
        "level_item_counts": level_counts,
        "max_intermediate_bytes": max(len(compact(mapped).encode()), len(compact(nodes).encode())),
    }


def norm(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c)
    )


def fingerprint(item: dict[str, Any]) -> str:
    words = re.findall(r"[a-z0-9]{4,}", norm(str(item.get("text", ""))))
    return "-".join(words[:3]) or "unspecified"


def deterministic_groups(
    product: str, mapped: list[dict[str, Any]], order: dict[str, int]
) -> list[dict[str, Any]]:
    items = [
        item
        for payload in mapped
        for item in payload[key_for(product)]
        if item.get("source_segment_ids")
    ]
    buckets: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        buckets.setdefault(fingerprint(item), []).append(item)
    groups = []
    for index, (_, members) in enumerate(buckets.items(), 1):
        ids = list(dict.fromkeys(sid for member in members for sid in member["source_segment_ids"]))
        groups.append(
            {
                "group_id": f"g{index:03d}",
                "members": members,
                "source_segment_ids": ids,
                "first_source_order": min((order.get(sid, 10**9) for sid in ids), default=10**9),
                "duplicate_count": max(0, len(members) - 1),
                "conflicts": [m["conflict"] for m in members if m.get("conflict")],
            }
        )
    return sorted(groups, key=lambda g: (g["first_source_order"], g["group_id"]))


def candidate_d(
    product: str,
    mapped: list[dict[str, Any]],
    source: dict[str, dict[str, Any]],
    order: dict[str, int],
    notes: str,
    model: str,
    timeout: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    groups = deterministic_groups(product, mapped, order)
    compact_groups = [
        {
            "group_id": g["group_id"],
            "member_texts": [m["text"] for m in g["members"]],
            "source_segment_ids": g["source_segment_ids"],
            "duplicate_count": g["duplicate_count"],
            "conflicts": g["conflicts"],
        }
        for g in groups
    ]
    plan_inputs = compact_groups
    if len(compact(plan_inputs).encode()) > MAX_PACKED_BYTES:
        plan_inputs = [
            {
                "group_id": g["group_id"],
                "member_texts": g["member_texts"][:2],
                "source_segment_ids": g["source_segment_ids"][:4],
                "duplicate_count": g["duplicate_count"],
                "conflicts": g["conflicts"][:2],
            }
            for g in compact_groups
        ]
    batches = packed_groups(plan_inputs)
    proposals = []
    for batch in batches:
        proposals.append(
            post(
                model=model,
                timeout=timeout,
                schema=PLAN_SCHEMA,
                system=(
                    f"Create a compact German {product} coverage-plan proposal. Select "
                    "substantive groups, retain conflicts, suppress only duplicates and "
                    "low-signal chatter. State omitted groups and why. Return strict JSON."
                ),
                user={"groups": batch, "manual_notes": notes},
            )
        )
    selected = list(
        dict.fromkeys(gid for proposal in proposals for gid in proposal["selected_group_ids"])
    )[:MAX_PLAN_GROUPS]
    known = {g["group_id"] for g in groups}
    selected = [gid for gid in selected if gid in known]
    omitted = [gid for gid in known if gid not in selected]
    plan = {
        "selected_group_ids": selected,
        "omitted_group_ids": omitted,
        "omission_reason": "bounded plan capacity, duplicate suppression, or low-signal selection"
        if omitted
        else None,
        "proposal_count": len(proposals),
    }
    chosen = [g for g in groups if g["group_id"] in selected]
    evidence = cited_source([m for g in chosen for m in g["members"]], source)
    if product == "general_summary":
        final = post(
            model=model,
            timeout=timeout,
            schema=product_schema(product, final=True),
            system=(
                "Generate one coherent German meeting overview from the validated "
                "coverage plan and selected original evidence. Cover selected groups, "
                "preserve uncertainty, avoid repetition, invent nothing. Return strict JSON."
            ),
            user={"plan": plan, "groups": chosen, "original_source": evidence},
        )
    else:
        final = post(
            model=model,
            timeout=timeout,
            schema=product_schema(product, final=True),
            system=(
                f"Generate a concise exportable German {product} product from the "
                "validated coverage plan and selected original evidence. Preserve source "
                "ids and unresolved conflicts; do not invent owner, due date, or facts. "
                "Return strict JSON."
            ),
            user={"plan": plan, "groups": chosen, "original_source": evidence},
        )
    return final, {
        "group_count": len(groups),
        "selected_group_count": len(selected),
        "omitted_group_count": len(omitted),
        "duplicate_count": sum(g["duplicate_count"] for g in groups),
        "conflict_count": sum(bool(g["conflicts"]) for g in groups),
        "plan_proposal_count": len(proposals),
        "max_intermediate_bytes": max((len(compact(g).encode()) for g in groups), default=0),
    }


def package(products: dict[str, Any], source: dict[str, dict[str, Any]]) -> SummaryResult:
    timeline = tuple(
        SummaryTimelineEntry(
            text=item["text"],
            source_segment_ids=tuple(item["source_segment_ids"]),
            start_seconds=min(
                source[sid]["start_seconds"] for sid in item["source_segment_ids"] if sid in source
            ),
        )
        for item in products["timeline"]["timeline"]
        if any(sid in source for sid in item["source_segment_ids"])
    )
    actions = tuple(
        SummaryActionItem(text=item["text"], owner=item.get("owner"), due=item.get("due"))
        for item in products["action_items"]["action_items"]
    )
    return SummaryResult(
        timeline=timeline,
        action_items=actions,
        general_summary=products["general_summary"]["general_summary"],
    )


def evaluate(value: dict[str, Any]) -> dict[str, Any]:
    text = compact(value)
    normalized = norm(text)
    checks = {
        "digital_tutor": "digital tutor" in normalized,
        "module_structure": "modul" in normalized,
        "certification_practice": "zertifiz" in normalized and "praxis" in normalized,
        "mentor_advisor": "mentor" in normalized
        and any(x in normalized for x in ("berater", "beratung", "advisor")),
        "overview_graphic_action": any(x in normalized for x in ("grafik", "visualis", "ubersicht"))
        and any(x in normalized for x in ("2026-07-01", "01.07", "1. juli")),
    }
    key = next(iter(value))
    item_count = len(value[key]) if isinstance(value[key], list) else 1
    return {
        "fact_recall": sum(checks.values()),
        "fact_recall_total": len(checks),
        "checks": checks,
        "fabricated_2023": bool(re.search(r"\b2023[-.]", text)),
        "output_bytes": len(text.encode()),
        "item_count": item_count,
    }


def run(strategy: str, model: str, timeout: float, raw_dir: Path) -> dict[str, Any]:
    global CALL_DIR, CALLS
    segments, notes = load()
    source = {s["id"]: s for s in segments}
    order = {s["id"]: i for i, s in enumerate(segments)}
    chunked = chunks(segments)
    strategy_dir = raw_dir / strategy
    strategy_dir.mkdir(parents=True)
    CALL_DIR = strategy_dir / "calls"
    CALL_DIR.mkdir()
    CALLS = []
    products: dict[str, Any] = {}
    metrics: dict[str, Any] = {}
    started = time.monotonic()
    for product in PRODUCTS:
        print(f"{strategy}: {product}: extracting {len(chunked)} chunks", flush=True)
        mapped = [map_product(product, chunk, model, timeout) for chunk in chunked]
        (strategy_dir / f"{product}-mapped-private.json").write_text(
            json.dumps(mapped, ensure_ascii=False, indent=2)
        )
        final, details = (
            candidate_c(product, mapped, source, notes, model, timeout)
            if strategy == "candidate_c"
            else candidate_d(product, mapped, source, order, notes, model, timeout)
        )
        products[product] = final
        metrics[product] = {**evaluate(final), **details}
        (strategy_dir / f"{product}-final-private.json").write_text(
            json.dumps(final, ensure_ascii=False, indent=2)
        )
    result = package(products, source)
    (strategy_dir / "note-package-private.json").write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2)
    )
    markdown = format_summary_markdown(result)
    (strategy_dir / "export-private.md").write_text(markdown)
    (strategy_dir / "call-index-private.json").write_text(
        json.dumps([asdict(c) for c in CALLS], indent=2)
    )
    return {
        "strategy": strategy,
        "model": model,
        "num_ctx": NUM_CTX,
        "prompt_budget": PROMPT_BUDGET,
        "chunk_count": len(chunked),
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "call_count": len(CALLS),
        "max_prompt_bytes": max(c.prompt_bytes for c in CALLS),
        "max_prompt_eval_count": max(c.prompt_eval_count for c in CALLS),
        "total_prompt_eval_count": sum(c.prompt_eval_count for c in CALLS),
        "total_eval_count": sum(c.eval_count for c in CALLS),
        "all_calls_fit_preflight": all(c.prompt_bytes <= PROMPT_BUDGET for c in CALLS),
        "rendered_markdown_bytes": len(markdown.encode()),
        "products": metrics,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="llama3.1:8b")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--strategy", action="append", choices=("candidate_c", "candidate_d"))
    args = parser.parse_args()
    timestamp = time.strftime("%Y%m%dT%H%M%S")
    raw_dir = args.out / timestamp
    raw_dir.mkdir(parents=True)
    results = []
    for strategy in args.strategy or ["candidate_c", "candidate_d"]:
        result = run(strategy, args.model, args.timeout, raw_dir)
        results.append(result)
        sanitized = args.out / f"sanitized-{timestamp}.json"
        sanitized.write_text(json.dumps(results, indent=2))
        print(sanitized, flush=True)


if __name__ == "__main__":
    main()
