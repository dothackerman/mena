---
title: Implement one-shot three-product note package
type: feat
status: completed
date: 2026-07-03
issue: 112
---

# Implement one-shot three-product note package

## Overview

Replace the current monolithic one-shot meeting summary (a single LLM call that
returns `timeline` + `important_points` + `action_items` + `general_summary` in
one large schema) with **three independent one-shot LLM products**, each with its
own small prompt and output schema:

- **timeline** — timestamped chronological events (array of strings)
- **action items** — tasks as objects (`text`, `owner`, `due`)
- **general summary** — concise overview (string)

`important_points` is removed from both generation and export. The user-facing
flow is unchanged (`mena summarize` then `mena export`); the change is internal
to the summarization provider, the summary artifact contract, and the markdown
renderer. This slice also switches the summarization Ollama baseline to
`llama3.1:8b` (per #107) and applies the #108 LLM request discipline (small
per-product schemas, `temperature: 0` + fixed `seed`, explicit `num_ctx` with a
loud token-budget hard-fail, explicit output language, and persisted
`prompt_eval_count`/`eval_count`).

**Scope guard:** Reducer/chunking strategy selection is out of scope (that is
#113). All three products run one-shot. Semantic quality gates beyond auditable
contract checks are out of scope (that is #115/#114).

**Isolation:** Work happens in a **new git worktree on a new branch for issue
112** (e.g. `issue-112`), separate from the current `issue-102` worktree. Do not
build this in `cli.py` — the summarization path was extracted to
`summarize_workflow.py` behind the interaction port (#117/#118); build here.

## Problem Statement / Motivation

Refs #108, #14. For long real meetings the single monolithic prompt produces
schema-valid-but-wrong summaries: it over-weights low-signal late small talk and
misses the meeting body, then export faithfully renders bad notes. Two root
causes are addressed by this slice:

1. **One monolithic multi-field schema degrades small-model content.**
   Constrained decoding (`format`) guarantees shape, never content; a large
   multi-field schema measurably hurts a small local model. Splitting into three
   small per-product schemas is the #108-recommended fix.
2. **Silent context truncation.** Ollama's default `num_ctx` is small and it
   truncates oversized prompts *from the beginning*, so the model never sees the
   meeting body. Setting `num_ctx` explicitly and hard-failing on over-budget
   prompts (instead of silently truncating) is the first line of defense.

## Proposed Solution

### Three-product provider

`OllamaSummarizationProvider.summarize()` performs **three separate `/api/chat`
calls**, one per product, each with:

- its own small `format` JSON schema (timeline / action-items / general-summary),
- deterministic `options`: `temperature: 0`, fixed `seed`, explicit `num_ctx`,
- an explicit output-language instruction in the prompt,
- a pre-send token-budget check that hard-fails when the estimated prompt tokens
  exceed a threshold (~85%) of `num_ctx`.

The three product results are assembled into a single `SummaryResult` (now
without `important_points`). Per-product request options and response token
counts are captured for provenance.

`FakeSummarizationProvider` keeps returning a canned `SummaryResult` (no LLM),
so the full summarize→export path stays testable without private UAT content.

### Contract change (summary artifact v2)

Bump `SUMMARY_SCHEMA_VERSION` 1 → 2. The v2 `summary` artifact drops
`important_points` and adds a per-product request-provenance block recording
effective options and token counts. `PROMPT_POLICY` is bumped to a new
note-package policy id so the reuse/skip cache correctly invalidates old
monolithic summaries (the stage `config_id` changes with prompt policy +
provider options, so stale cached summaries are not reused).

### Export change

`format_summary_markdown` drops the "Important points" section and renders only
timeline, action items, general summary. The export artifact contract string /
version is bumped so old exports do not silently match new ones in the
reuse-dedup path.

### Model baseline switch

The summarization stage baseline becomes `llama3.1:8b` via the setup profile
catalog (the effective model reaches the provider through
`_stage_model(runtime_state, "summarization")`, not the provider default). Only
the **summarization** stage moves; `semantic-review` stays on the existing
default unless we decide otherwise (see Open Decisions).

## Technical Approach

### Key files

- `app/src/mena/summarization.py:56` — `SUMMARY_RESPONSE_SCHEMA` (monolith) →
  split into three per-product schemas; `SUMMARY_SCHEMA_VERSION` (`:46`) → 2;
  `PROMPT_POLICY` (`:53`) → new note-package id; `OLLAMA_DETERMINISTIC_OPTIONS`
  (`:89`) → add `seed` + `num_ctx`.
- `app/src/mena/summarization.py:184` — `SummaryResult` drop `important_points`;
  `:274` `OllamaSummarizationProvider.summarize()` → three calls; `:351`
  `SummaryArtifact` drop `important_points`, add request-provenance; validators
  at `:393-396`, `:204-207` remove `important_points` non-empty checks.
- `app/src/mena/summary_markdown.py:24` — remove important-points section and
  labels (`:86`, `:95`).
- `app/src/mena/export.py:20-22` — bump export contract/version.
- `app/src/mena/local_llm.py:129` — add a helper to read
  `prompt_eval_count`/`eval_count` from an Ollama response envelope.
- `app/src/mena/setup.py:418` — summarization stage model → `llama3.1:8b`
  (new `OLLAMA_SUMMARIZATION_DEFAULT_MODEL` constant, likely in `local_llm.py`).
- Tests: `test_summarization.py`, `test_summary_markdown.py`, `test_export.py`,
  `test_summarize_workflow.py`, `test_setup.py` (profile catalog model),
  `test_local_llm.py` (token-count helper).

### Per-product schemas (illustrative)

```python
# summarization.py
TIMELINE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["timeline"],
    "properties": {"timeline": {"type": "array", "items": {"type": "string"}, "minItems": 1}},
}
ACTION_ITEMS_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["action_items"],
    "properties": {"action_items": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["text", "owner", "due"],
        "properties": {
            "text": {"type": "string", "minLength": 1},
            "owner": {"anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}]},
            "due": {"anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}]},
        }}}},
}
GENERAL_SUMMARY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["general_summary"],
    "properties": {"general_summary": {"type": "string", "minLength": 1}},
}
```

### Request discipline (per product)

```python
# summarization.py — token budget + options
OLLAMA_SUMMARIZATION_NUM_CTX = 8192          # explicit; never rely on default
OLLAMA_SUMMARIZATION_SEED = 0
TOKEN_BUDGET_FRACTION = 0.85

def _estimate_prompt_tokens(text: str) -> int:
    return (len(text) + 3) // 4            # conservative chars/4

def _guard_token_budget(prompt: str, num_ctx: int) -> None:
    if _estimate_prompt_tokens(prompt) > int(num_ctx * TOKEN_BUDGET_FRACTION):
        raise SummarizationTokenBudgetError(...)   # loud, do not send

options = {"temperature": 0, "seed": OLLAMA_SUMMARIZATION_SEED,
           "num_ctx": OLLAMA_SUMMARIZATION_NUM_CTX}
```

`SummarizationTokenBudgetError(SummarizationError)` is a new non-retryable error
so the ledger records a clear `summarization_failed` state rather than a silent
truncated success.

### Provenance block (summary artifact v2)

Each of the three product calls records `{ "product": ..., "model": ...,
"num_ctx": ..., "temperature": 0, "seed": ..., "prompt_eval_count": ...,
"eval_count": ... }`. Persist as a `request_records` list on `SummaryResult` /
`SummaryArtifact` and surface the ids in the persisted artifact metadata. This
satisfies "effective request options and response token counts are recorded in
provenance."

### Output language

Each product prompt sets the output language explicitly (small local models drift
to English). A lightweight, deterministic output guard (`_assert_output_language`)
then asserts on the generated note package itself: for a non-English request it
raises `SummarizationContractError` when the combined output reads as English
drift (several distinct English function words at meaningful density, using a
stopword set curated to avoid German homographs). Robust statistical language
detection is **not** added (no new heavy dependency). The German-fixture acceptance is
verified in tests via a Fake provider returning German content and asserting the
export renders German section labels (already driven by `policy.language` in
`summary_markdown.py`) and that policy language threads end-to-end. See Open
Decisions for how strict the runtime guard should be.

## System-Wide Impact

- **Interaction graph:** `run_summarization_command` → `run_summarization_stage`
  → `provider.summarize()` (now 3 calls) → `persist_summary_artifact` → ledger
  `mark_completed`. `mena export` → `export_summary_markdown` →
  `format_summary_markdown`. No change to the interaction port surface.
- **Error propagation:** new `SummarizationTokenBudgetError` flows through the
  existing `except (SummarizationError, ...)` block in `run_summarization_stage`
  (`:618`) and is non-retryable. Ollama unavailability path unchanged.
- **State lifecycle / reuse:** bumping `PROMPT_POLICY`, provider `options`, and
  `SUMMARY_SCHEMA_VERSION` changes `stage_config_id`, so old v1 summaries are not
  reused (correct). Export contract bump prevents stale export dedup matches.
  Old on-disk v1 summary artifacts will fail v2 `from_dict` — export must treat
  a v1/incompatible artifact as "not a valid current summary" and skip it, not
  crash (verify `resolve_summary_markdown_input` behavior).
- **API surface parity:** `semantic_coherence.py` is a *separate* Ollama product
  and is out of scope; do not change its schema. Only the summarization product
  changes.
- **Integration scenarios:** (1) full summarize→export on English fixture; (2)
  same on German fixture with language assertion; (3) over-budget prompt →
  loud failure, no artifact; (4) reuse: second summarize run reuses v2 artifact
  deterministically; (5) provenance: token counts + options present in artifact.

## Acceptance Criteria

- [x] `mena summarize` produces timeline, action-items, and general-summary
      products through **three distinct one-shot** LLM prompts/schemas.
- [x] The summarization Ollama baseline for this path is `llama3.1:8b`.
- [x] `mena export` renders the three-product package with **no**
      `important_points` section.
- [x] Summary/export reuse and provenance stay deterministic enough for
      regression tests (stable artifact names, dedup, config-id invalidation).
- [x] A test covers a complete summarize→export path using a Fake provider and
      fixtures — no private UAT transcript content.
- [x] Effective request options (`model`, `num_ctx`, `temperature`, `seed`) and
      response token counts (`prompt_eval_count`, `eval_count`) are recorded in
      provenance for each product.
- [x] Oversized prompts hard-fail against the configured `num_ctx` budget
      instead of being silently truncated (new token-budget error + test).
- [x] Output language matches the session/export language on the German fixture
      path (test asserts German export labels + threaded policy language).
- [x] Full suite green: `uv run pytest` (or repo runner) and lint/type checks.

## Implementation Phases

**Status: complete.** All phases landed; 860 tests pass, coverage 90.47%,
ruff + mypy clean. Model-baseline decision: bumped the shared
`OLLAMA_DEFAULT_MODEL` to `llama3.1:8b` (the local baseline threads through the
setup profile into both summarization and semantic-review); semantic-review
moves with it, which is acceptable per #107.

### Phase 1 — Contract + renderer (no LLM)
1. `SummaryResult` / `SummaryArtifact`: drop `important_points`, bump schema to
   v2, add `request_records` provenance. Update `from_dict`/`to_dict`/validators.
2. `summary_markdown.py`: remove important-points section + labels.
3. `export.py`: bump export contract/version; confirm graceful skip of
   incompatible (v1) artifacts.
4. Update `test_summary_markdown.py`, `test_export.py`, contract tests in
   `test_summarization.py`. Suite green for the non-LLM path.

### Phase 2 — Three-call provider + request discipline
5. Split schema into three; rewrite `OllamaSummarizationProvider.summarize()` to
   three calls; add `seed` + `num_ctx` options and token-budget guard + error.
6. Add token-count helper in `local_llm.py`; capture per-product provenance.
7. Bump `PROMPT_POLICY` to note-package id; keep `SUPPORTED_PROMPT_POLICIES`
   accepting it (decide whether to keep legacy ids for read-compat).
8. Tests: three-call behavior, token-budget hard-fail, provenance token counts,
   language instruction present. Use a stub HTTP/`post_ollama_json` seam.

### Phase 3 — Model baseline + end-to-end
9. `OLLAMA_SUMMARIZATION_DEFAULT_MODEL = "llama3.1:8b"`; wire into
   `default_profile_catalog` summarization stage; update `test_setup.py`.
10. End-to-end `test_summarize_workflow.py`: full summarize→export on English and
    German fixtures via Fake provider; assert no important_points, correct
    labels, provenance present.
11. Full suite + lint/type. Run `ce-simplify-code`, then review.

## Dependencies & Risks

- **Blocked by:** none (former blockers #111, #118 closed 2026-07-03).
- **Risk — reuse cache staleness:** if `config_id` inputs are not all bumped,
  old v1 summaries could be reused. Mitigation: bump prompt policy + provider
  options + schema version together; add a reuse regression test.
- **Risk — v1 artifacts on disk crash export.** Mitigation: export must skip
  incompatible artifacts, not raise. Add a test.
- **Risk — language assertion over-reach.** Robust detection needs a dependency;
  keep the runtime guard minimal and lean on fixture tests. Flagged below.
- **Risk — `num_ctx: 8192` memory** on the target machine. Value is a policy
  constant; easy to tune. Not verified against live hardware in this slice.

## Open Decisions (resolve during work; pick sensible defaults if unclear)

1. **Semantic-review model:** keep on the old default (recommended, in-scope AC
   is summarization-only) or also move to `llama3.1:8b`? Default: keep.
2. **num_ctx value:** default `8192`. Confirm against #107 report if it named a
   value.
3. **Legacy prompt-policy read-compat:** keep old ids in
   `SUPPORTED_PROMPT_POLICIES` for reading old artifacts, or hard-cut? Default:
   keep for read, write only the new id.
4. **Runtime language guard strictness:** RESOLVED — implemented a conservative
   heuristic English-drift guard on the generated output (`_assert_output_language`)
   in addition to fixture-test coverage, so AC #7 is asserted at runtime, not only
   in tests. Thresholds are tuned to avoid false positives on embedded English
   technical nouns.
5. **Provenance shape:** `request_records` list on the artifact (recommended) vs.
   separate provenance artifact. Default: inline list on the summary artifact.

## Sources & References

- Issue #112 (this slice); tracking bug #108; research spike #107; epic #14.
- Ollama structured outputs / context length / options:
  https://docs.ollama.com/capabilities/structured-outputs ,
  https://docs.ollama.com/context-length , https://docs.ollama.com/api/generate
- Current monolith: `app/src/mena/summarization.py:56` (schema), `:274`
  (provider), `:184`/`:351` (result/artifact).
- Renderer: `app/src/mena/summary_markdown.py:20`.
- Export: `app/src/mena/export.py:38`.
- Runtime model binding: `app/src/mena/runtime_profile.py:578`,
  `app/src/mena/setup.py:418`.
