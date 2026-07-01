# Mena #107 Spike: Local summary export quality

Intended repo path: `docs/research/notes/2026-06-30-local-summary-export-quality-spike.md`

Original spike date: 2026-06-30

Updated: 2026-07-01 after completing the issue-listed candidate model sweep.

Status: draft from local spike evidence. This draft intentionally excludes raw transcript text, manual-note text, model output text, and private session content.

## Scope

Issue #107 asks whether Mena can produce trustworthy German meeting-note exports from a real long-session fixture using local Ollama models and prompt/pipeline changes.

Input fixture:

- Installed-product UAT session: `c575b55f-352f-47b9-96c5-affc7d5f29d7`
- Merged transcript: 832 segments, 45,969 transcript characters
- Manual notes: 918 characters
- Language: German

Private input and model-output artifacts stayed in local UAT scratch space. This report records only sanitized result categories, runtime facts, and quality-gate outcomes.

## Evidence Sources

Existing prompt lab:

- Runner: `/home/oriol/.openclaw/workspace/product-manager/uat/mena/session-c575b55f-2026-06-30/prompt-lab/prompt_lab.py`
- Report: `/home/oriol/.openclaw/workspace/product-manager/uat/mena/session-c575b55f-2026-06-30/prompt-lab/REPORT.md`
- Best-direction run noted there: `runs/20260630T173422Z/04-chunked_8min_coverage_reduce/`

Completed #107 candidate sweep:

- Sweep summary: `/home/oriol/.openclaw/workspace/product-manager/uat/mena/session-c575b55f-2026-06-30/issue-107-model-lab/sweeps/20260701T073609Z/summary.md`
- Sanitized JSON: `/home/oriol/.openclaw/workspace/product-manager/uat/mena/session-c575b55f-2026-06-30/issue-107-model-lab/sweeps/20260701T073609Z/summary.json`
- Raw prompts, private fixture references, and raw model responses remain local and are not copied into the repo.

Candidate models tested:

- `qwen3:4b`
- `gemma3:4b`
- `granite3.3:8b`
- `llama3.1:8b`
- `phi4-mini`
- `mistral-nemo:12b`

Strategies tested for each candidate:

- `current_one_shot_default`: current Mena one-shot payload, `temperature=0`
- `current_one_shot_numctx32768`: current Mena one-shot payload, `temperature=0`, `num_ctx=32768`
- `revised_one_shot_plain`: revised lower-overhead one-shot prompt, `temperature=0`
- `chunked_8min_combined_reduce`: 8-minute chunks, one combined final reducer, `temperature=0`
- `chunked_8min_field_reducers`: 8-minute chunks, separate final reducers for timeline, important points, action items, and general summary, `temperature=0`

The automated score is a local coverage heuristic over five fixture-specific core topics. It is not a general semantic quality score, but it is useful for comparing failure modes across the same fixture.

## Summary Matrix

| Model | Strategy | Result |
| --- | --- | --- |
| `qwen3:4b` | Current one-shot | Valid JSON, 0/5 core coverage, tail/travel dominated, 31.5s. |
| `qwen3:4b` | Current one-shot, `num_ctx=32768` | Invalid JSON, 0/5 core coverage, ended at `length` after filling 32,767 prompt tokens, 53.5s. |
| `qwen3:4b` | Revised one-shot | Valid JSON, 0/5 core coverage, 19.5s. |
| `qwen3:4b` | Chunked combined reducer | Valid JSON, 3/5 core coverage, 561.6s. |
| `qwen3:4b` | Chunked field reducers | Valid JSON, 5/5 core coverage, 718.6s, `ollama ps` showed 100% GPU / 4096 context. |
| `gemma3:4b` | Current one-shot | Invalid JSON, 0/5 core coverage, ended at `length`, 29.7s. |
| `gemma3:4b` | Current one-shot, `num_ctx=32768` | Invalid JSON, 0/5 core coverage, ended at `length` after filling 32,767 prompt tokens, 20.5s. |
| `gemma3:4b` | Revised one-shot | Invalid JSON, 0/5 core coverage, ended at `length`, 6.9s. |
| `gemma3:4b` | Chunked combined reducer | Valid JSON, 3/5 core coverage, 76.4s. |
| `gemma3:4b` | Chunked field reducers | Valid JSON, 4/5 core coverage, 88.8s, `ollama ps` showed 100% GPU / 4096 context. |
| `granite3.3:8b` | Current one-shot | Valid JSON, 0/5 core coverage, tail/travel dominated, 65.4s. |
| `granite3.3:8b` | Current one-shot, `num_ctx=32768` | Invalid JSON, 0/5 core coverage, ended at `length` after filling 32,767 prompt tokens, 86.1s. |
| `granite3.3:8b` | Revised one-shot | Valid JSON, 2/5 core coverage, tail/travel dominated, 42.2s. |
| `granite3.3:8b` | Chunked combined reducer | Valid JSON, 3/5 core coverage, 506.5s. |
| `granite3.3:8b` | Chunked field reducers | Valid JSON, 3/5 core coverage, 523.1s. |
| `llama3.1:8b` | Current one-shot | Valid JSON, 0/5 core coverage, tail/travel dominated, 23.4s. |
| `llama3.1:8b` | Current one-shot, `num_ctx=32768` | Invalid JSON, 0/5 core coverage, ended at `length` after filling 32,767 prompt tokens, 76.0s. |
| `llama3.1:8b` | Revised one-shot | Valid JSON, 3/5 core coverage, 22.4s. |
| `llama3.1:8b` | Chunked combined reducer | Valid JSON, 5/5 core coverage, 329.7s, `ollama ps` showed 25%/75% CPU/GPU / 4096 context. |
| `llama3.1:8b` | Chunked field reducers | Timed out during the field-reducer path after 676.1s. |
| `phi4-mini` | Current one-shot | Valid JSON, 0/5 core coverage, 18.3s. |
| `phi4-mini` | Current one-shot, `num_ctx=32768` | Invalid JSON, 0/5 core coverage, ended at `length` after filling 32,767 prompt tokens, 46.9s. |
| `phi4-mini` | Revised one-shot | Valid JSON, 1/5 core coverage, 8.0s. |
| `phi4-mini` | Chunked combined reducer | Valid JSON, 3/5 core coverage, but included fabricated 2023 dates, 59.3s. |
| `phi4-mini` | Chunked field reducers | Valid JSON, 3/5 core coverage, 62.9s. |
| `mistral-nemo:12b` | Current one-shot | Valid JSON, 0/5 core coverage, 193.5s. |
| `mistral-nemo:12b` | Current one-shot, `num_ctx=32768` | Invalid JSON, 0/5 core coverage, ended at `length` after filling 32,767 prompt tokens, 110.0s. |
| `mistral-nemo:12b` | Revised one-shot | Valid JSON, 2/5 core coverage, 77.7s. |
| `mistral-nemo:12b` | Chunked combined reducer | Valid JSON, 3/5 core coverage, 822.9s. |
| `mistral-nemo:12b` | Chunked field reducers | Timed out at the 300s cap, `ollama ps` showed 48%/52% CPU/GPU / 4096 context. |

## Research Questions

### 1. Which local models look trustworthy for German exports?

No model is trustworthy as a long-session one-shot summarizer for this fixture.

The best quality/runtime candidate for PoC long-session export is `llama3.1:8b` with the 8-minute chunked combined reducer. It reached 5/5 core coverage with valid JSON in 329.7s and did not show travel dominance or fabricated 2023 dates in the sanitized checks.

The fastest viable fallback is `gemma3:4b` with chunked field reducers. It reached 4/5 core coverage with valid JSON in 88.8s on 100% GPU. That is not strong enough for blind acceptance, but it is promising if paired with quality gates and a retry/escalation path.

`qwen3:4b` reached 5/5 core coverage only with chunked field reducers, but at 718.6s. Its one-shot results were poor, and its runtime makes it a weak default despite the high field-reducer score.

`granite3.3:8b`, `phi4-mini`, and `mistral-nemo:12b` did not justify default use from this fixture. They either capped out at 3/5 core coverage, fabricated dates, or took too long for the quality achieved.

### 2. Does `options.num_ctx` materially improve one-shot quality?

No.

Every candidate model failed the `num_ctx=32768` one-shot strategy:

- `qwen3:4b`: invalid JSON, `done_reason=length`
- `gemma3:4b`: invalid JSON, `done_reason=length`
- `granite3.3:8b`: invalid JSON, `done_reason=length`
- `llama3.1:8b`: invalid JSON, `done_reason=length`
- `phi4-mini`: invalid JSON, `done_reason=length`
- `mistral-nemo:12b`: invalid JSON, `done_reason=length`

All six evaluated 32,767 prompt tokens and produced no usable summary. The product conclusion is clear: context sizing is necessary policy metadata, but it is not a fix for long-session one-shot summarization.

### 3. Is one-shot summarization good enough for PoC export quality?

No, not for long sessions like this fixture.

Default one-shot outputs either failed JSON validity, missed the core meeting content, or over-weighted low-signal tail/travel chatter. Revised one-shot prompting helped `llama3.1:8b` and `mistral-nemo:12b` somewhat, but the best revised one-shot still reached only 3/5 core coverage.

One-shot can remain a guarded path for short sessions only if Mena adds prompt-size preflight and quality gates. For long sessions, chunking should be required.

### 4. Should timeline, important points, action items, and summary become separate passes?

Yes, but not as an unconditional default for every model.

Separate field reducers improved `qwen3:4b` from 3/5 to 5/5 and improved `gemma3:4b` from 3/5 to 4/5. They did not improve `granite3.3:8b` or `phi4-mini`, and they timed out for `llama3.1:8b` and `mistral-nemo:12b`.

The implementation lesson is:

- Use chunking as the required long-session structure.
- Keep the reducer architecture capable of field-specific passes.
- Run action-item and date/timeline validation separately from prose-summary acceptance.
- Do not make every model run every field reducer unless runtime budgets and quality gates justify it.

### 5. What quality gates are required before accepting a summary artifact?

Mena should reject or quarantine schema-valid summaries that fail product-quality gates.

Required gates for this class of meeting:

- Manual-note coverage: major manual-note topics must be represented or explicitly marked unsupported.
- Low-signal dominance: greetings, setup chatter, travel chatter, and farewells must not dominate unless they are the meeting's substantive content.
- Date discipline: due dates must come from transcript/manual notes, not transcript timestamps or model inference.
- Action task shape: action items must be executable tasks with grounded owner/due fields where stated; topic labels are not action items.
- Timeline shape: timeline entries must be chronological topic events, not bare calendar dates.
- Language preservation: output should preserve the selected meeting/export language.
- Runtime tolerance: local runs must finish within a PoC-tolerable budget; timeouts should not produce accepted artifacts.

## Recommendation

Do not ship a model-only, prompt-only, or `num_ctx`-only fix for #107.

Recommended PoC direction:

1. Add prompt-size/context-budget preflight before local summarization.
2. Route long sessions to chunked summarization before the prompt approaches the usable model context window.
3. Use `temperature=0` for deterministic export generation.
4. Prefer `llama3.1:8b` with an 8-minute chunked combined reducer as the first PoC long-session candidate when local runtime around 5 to 6 minutes is acceptable.
5. Use `gemma3:4b` with chunked field reducers as the faster fallback candidate, guarded by coverage/date/action quality gates.
6. Keep field-specific reducers or validators in the design, especially for action items, dates, and timeline entries.
7. Reject or quarantine schema-valid summaries that fail quality gates; schema validity is not enough.

## Follow-up Issues

Suggested implementation issues:

1. Add prompt-size/context-budget preflight for local summarization.
2. Implement chunked local summarization for long sessions.
3. Add summary quality gates for coverage, low-signal dominance, date discipline, action shape, timeline shape, language, and runtime.
4. Add a model-policy configuration for default local summary model, fallback model, context policy, timeout, and chunk size.
5. Add optional field-focused reduce/validation passes for timeline, action items, important points, and general summary.
6. Add a repeatable sanitized model benchmark harness that records model tag, `ollama ps` processor split, options, elapsed time, schema validity, timeout/error state, and quality-gate verdicts without committing private transcript or model output text.

Suggested research follow-up:

1. Re-run the benchmark after quality gates exist, because the current score is fixture-specific and should become a real gate verdict.
2. Test short-session thresholds separately so Mena can keep a fast one-shot path where it is actually safe.
3. Re-test the top candidates on at least one more German session fixture before treating the model policy as stable.

## Bottom Line

The completed candidate sweep supports the same product direction more strongly: export-worthy long-session summaries need chunking and acceptance gates. The best current long-session candidate is `llama3.1:8b` with chunked combined reduction; `gemma3:4b` is the faster guarded fallback. Larger context alone made every candidate fail the one-shot export path.
