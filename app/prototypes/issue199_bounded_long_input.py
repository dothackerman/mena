#!/usr/bin/env python3
"""PROTOTYPE for #199: compare bounded long-input aggregation strategies.

Reads the private RC7 fixture, writes raw outputs only to a local UAT directory,
and emits a separate sanitized metrics file suitable for the research report.
This is throwaway research code, not a production implementation.
"""

from __future__ import annotations

import argparse
import json
import re
import time
import unicodedata
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FIXTURE = Path("/home/oriol/.local/share/mena/sessions/c575b55f-352f-47b9-96c5-affc7d5f29d7_2026-06-30T130940Z")
TRANSCRIPT = FIXTURE / "artifacts/merged_transcript/4a45ee69-8173-4198-994d-a09f63bd8857/144f943e-7a6af3fc/17cb058d1294/attempt-001.json"
NOTES = FIXTURE / "artifacts/manual_notes/raw/2026-06-30_Meeting_Alex-ddab7d4cb233.md"
DEFAULT_OUT = Path("/home/oriol/.openclaw/workspace/product-manager/uat/mena/session-c575b55f-2026-06-30/issue-199-strategy-lab")
PRODUCTS = ("timeline", "action_items", "general_summary")
NUM_CTX = 8192
PROMPT_BUDGET = int(NUM_CTX * 0.85)
MAX_BATCH_BYTES = 4600

SCHEMAS: dict[str, dict[str, Any]] = {
    "timeline": {"type": "object", "additionalProperties": False, "required": ["timeline"], "properties": {"timeline": {"type": "array", "maxItems": 8, "items": {"type": "object", "additionalProperties": False, "required": ["text", "source_segment_ids"], "properties": {"text": {"type": "string", "maxLength": 300}, "source_segment_ids": {"type": "array", "maxItems": 8, "items": {"type": "string"}}}}}}},
    "action_items": {"type": "object", "additionalProperties": False, "required": ["action_items"], "properties": {"action_items": {"type": "array", "maxItems": 8, "items": {"type": "object", "additionalProperties": False, "required": ["text", "owner", "due", "source_segment_ids"], "properties": {"text": {"type": "string", "maxLength": 300}, "owner": {"type": ["string", "null"]}, "due": {"type": ["string", "null"]}, "source_segment_ids": {"type": "array", "maxItems": 8, "items": {"type": "string"}}}}}}},
    "general_summary": {"type": "object", "additionalProperties": False, "required": ["general_summary", "source_segment_ids"], "properties": {"general_summary": {"type": "string", "maxLength": 900}, "source_segment_ids": {"type": "array", "maxItems": 24, "items": {"type": "string"}}}},
}


@dataclass
class Call:
    prompt_eval_count: int
    eval_count: int
    elapsed: float
    prompt_bytes: int


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def post(*, model: str, system: str, user: Any, schema: dict[str, Any], timeout: float) -> tuple[dict[str, Any], Call]:
    user_text = compact(user)
    prompt_bytes = len(system.encode()) + len(user_text.encode())
    if prompt_bytes > PROMPT_BUDGET:
        raise RuntimeError(f"preflight refused {prompt_bytes} > {PROMPT_BUDGET}")
    payload = {"model": model, "stream": False, "format": schema, "options": {"temperature": 0, "seed": 0, "num_ctx": NUM_CTX, "num_predict": 1200}, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user_text}]}
    req = urllib.request.Request("http://localhost:11434/api/chat", data=json.dumps(payload).encode(), headers={"content-type": "application/json"})
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = json.loads(response.read())
    elapsed = time.monotonic() - started
    if body.get("done_reason") == "length":
        raise RuntimeError("model response reached length limit")
    result = json.loads(body["message"]["content"])
    return result, Call(int(body["prompt_eval_count"]), int(body["eval_count"]), elapsed, prompt_bytes)


def load() -> tuple[list[dict[str, Any]], str]:
    raw = json.loads(TRANSCRIPT.read_text())
    segments = [{"id": s["id"], "start_seconds": s["start_seconds"], "end_seconds": s["end_seconds"], "speaker": s["speaker"]["label"], "text": s["text"]} for s in raw["segments"]]
    return segments, NOTES.read_text()


def chunks(segments: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    result: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    used = 0
    for segment in segments:
        size = len(compact(segment).encode())
        if current and used + size > 5000:
            result.append(current); current = []; used = 0
        current.append(segment); used += size
    if current: result.append(current)
    return result


def map_product(product: str, chunk: list[dict[str, Any]], model: str, timeout: float) -> tuple[dict[str, Any], Call]:
    instructions = {
        "timeline": "Extract up to 8 substantive chronological events. Cite only supplied segment ids.",
        "action_items": "Extract only explicit executable tasks, not topics. Do not infer owners or dates. Cite supplied segment ids.",
        "general_summary": "Summarize the substantive content of this chunk concisely and cite supporting segment ids.",
    }[product]
    return post(model=model, timeout=timeout, schema=SCHEMAS[product], system=f"German meeting-note extraction. {instructions} Ignore greetings, travel chatter, setup and farewells unless substantive. Return German strings and strict schema JSON.", user={"chunk": chunk})


def batches(items: list[Any]) -> list[list[Any]]:
    out: list[list[Any]] = []
    current: list[Any] = []
    used = 0
    for item in items:
        size = len(compact(item).encode())
        if size > MAX_BATCH_BYTES:
            raise RuntimeError(f"single intermediate exceeds batch bound: {size}")
        if current and used + size > MAX_BATCH_BYTES:
            out.append(current); current = []; used = 0
        current.append(item); used += size
    if current: out.append(current)
    return out


def hierarchical(product: str, mapped: list[dict[str, Any]], notes: str, model: str, timeout: float, calls: list[Call]) -> tuple[dict[str, Any], int]:
    nodes = mapped
    depth = 0
    while len(nodes) > 1:
        groups = batches(nodes)
        if len(groups) >= len(nodes):
            raise RuntimeError("non-shrinking hierarchy")
        next_nodes = []
        for group in groups:
            result, call = post(model=model, timeout=timeout, schema=SCHEMAS[product], system=f"Merge these consecutive German {product} candidates. Preserve grounding, chronology and contradictions; deduplicate only equivalent claims. Return strict schema JSON.", user={"candidates": group, "manual_notes": notes if depth == 0 else None})
            next_nodes.append(result); calls.append(call)
        nodes = next_nodes; depth += 1
    return nodes[0], depth


def norm(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c))


def evidence_first(product: str, mapped: list[dict[str, Any]], notes: str, model: str, timeout: float, calls: list[Call]) -> tuple[dict[str, Any], int]:
    if product in ("timeline", "action_items"):
        candidates = [item for payload in mapped for item in payload.get(product, [])]
        valid = []
        seen = set()
        for item in candidates:
            ids = item.get("source_segment_ids") or []
            key = norm(str(item.get("text", "")))
            if ids and key and key not in seen:
                seen.add(key); valid.append(item)
        if product == "timeline":
            valid.sort(key=lambda x: min(segment_order.get(i, 10**9) for i in x["source_segment_ids"]))
        return {product: valid}, 0
    atoms = [{"text": payload["general_summary"], "source_segment_ids": payload["source_segment_ids"]} for payload in mapped]
    nodes = atoms
    depth = 0
    while len(nodes) > 1:
        groups = batches(nodes)
        if len(groups) >= len(nodes): raise RuntimeError("non-shrinking evidence synthesis")
        next_nodes = []
        for group in groups:
            result, call = post(model=model, timeout=timeout, schema=SCHEMAS[product], system="Synthesize these ordered, source-cited German evidence summaries into one concise German meeting overview. Preserve competing facts; do not invent content. Return strict schema JSON.", user={"evidence": group, "manual_notes": notes if depth == 0 else None})
            next_nodes.append({"text": result["general_summary"], "source_segment_ids": result["source_segment_ids"]}); calls.append(call)
        nodes = next_nodes; depth += 1
    return {"general_summary": nodes[0]["text"], "source_segment_ids": nodes[0]["source_segment_ids"]}, depth


def evaluate(result: dict[str, Any]) -> dict[str, Any]:
    text = compact(result); n = norm(text)
    checks = {
        "digital_tutor": "digital tutor" in n,
        "module_structure": "modul" in n,
        "certification_practice": "zertifiz" in n and "praxis" in n,
        "mentor_advisor": "mentor" in n and any(x in n for x in ("berater", "beratung", "advisor")),
        "overview_graphic_action": any(x in n for x in ("grafik", "visualis", "ubersicht")) and any(x in n for x in ("2026-07-01", "01.07", "1. juli")),
    }
    return {"fact_recall": sum(checks.values()), "fact_recall_total": len(checks), "checks": checks, "german_markers": sum(n.count(x) for x in (" der ", " die ", " und ", " ist ")), "fabricated_2023": bool(re.search(r"\b2023[-.]", text)), "output_bytes": len(text.encode())}


def run(strategy: str, model: str, timeout: float, raw_dir: Path) -> dict[str, Any]:
    segments, notes = load(); chunked = chunks(segments); all_calls: list[Call] = []
    products: dict[str, Any] = {}; metrics: dict[str, Any] = {}
    started = time.monotonic()
    for product in PRODUCTS:
        mapped = []
        for chunk in chunked:
            value, call = map_product(product, chunk, model, timeout); mapped.append(value); all_calls.append(call)
        if strategy == "hierarchical": value, depth = hierarchical(product, mapped, notes, model, timeout, all_calls)
        else: value, depth = evidence_first(product, mapped, notes, model, timeout, all_calls)
        products[product] = value
        metrics[product] = {**evaluate(value), "map_calls": len(mapped), "reduction_depth": depth}
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / f"{strategy}-private-output.json").write_text(json.dumps(products, ensure_ascii=False, indent=2))
    return {"strategy": strategy, "model": model, "num_ctx": NUM_CTX, "prompt_budget": PROMPT_BUDGET, "chunk_count": len(chunked), "elapsed_seconds": round(time.monotonic()-started, 2), "call_count": len(all_calls), "max_prompt_bytes": max(c.prompt_bytes for c in all_calls), "max_prompt_eval_count": max(c.prompt_eval_count for c in all_calls), "total_prompt_eval_count": sum(c.prompt_eval_count for c in all_calls), "total_eval_count": sum(c.eval_count for c in all_calls), "all_calls_fit_preflight": all(c.prompt_bytes <= PROMPT_BUDGET for c in all_calls), "products": metrics}


segment_order: dict[str, int] = {}


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--model", default="gemma3:4b"); parser.add_argument("--timeout", type=float, default=300); parser.add_argument("--out", type=Path, default=DEFAULT_OUT); parser.add_argument("--strategy", action="append", choices=("hierarchical", "evidence_first"))
    args = parser.parse_args(); global segment_order
    segments, _ = load(); segment_order = {s["id"]: i for i, s in enumerate(segments)}
    timestamp = time.strftime("%Y%m%dT%H%M%S"); raw_dir = args.out / timestamp
    results = [run(s, args.model, args.timeout, raw_dir) for s in (args.strategy or ["hierarchical", "evidence_first"])]
    sanitized = args.out / f"sanitized-{timestamp}.json"; sanitized.parent.mkdir(parents=True, exist_ok=True); sanitized.write_text(json.dumps(results, indent=2)); print(sanitized)


if __name__ == "__main__": main()
