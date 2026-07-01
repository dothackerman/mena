# Mena #107 Spike: Local summary export quality

Intended repo path: `docs/research/notes/2026-06-30-local-summary-export-quality-spike.md`

Date: 2026-06-30

Status: draft from local spike evidence. This draft intentionally excludes raw transcript, manual-note text, model output text, and private session details.

## Scope

Issue #107 asks whether Mena can produce trustworthy German meeting-note exports from a real long-session fixture using local Ollama models and prompt/pipeline changes.

Input fixture:

- Installed-product UAT session: `c575b55f-352f-47b9-96c5-affc7d5f29d7`
- Merged transcript: 832 segments, 45,969 transcript characters
- Manual notes: 918 characters
- Language: German

Private input and model-output artifacts stayed in local UAT scratch space. This report records only sanitized result categories and runtime facts.

## Evidence Sources

Existing prompt lab:

- Runner: `/home/oriol/.openclaw/workspace/product-manager/uat/mena/session-c575b55f-2026-06-30/prompt-lab/prompt_lab.py`
- Report: `/home/oriol/.openclaw/workspace/product-manager/uat/mena/session-c575b55f-2026-06-30/prompt-lab/REPORT.md`
- Best-direction run noted there: `runs/20260630T173422Z/04-chunked_8min_coverage_reduce/`

New #107 model checks:

- Output root: `/home/oriol/.openclaw/workspace/product-manager/uat/mena/session-c575b55f-2026-06-30/issue-107-model-lab/runs/`
- Model newly pulled and tested: `qwen3:4b`
- Already installed model in prior lab: `llama3.2:3b`

Candidate-model sweep limitation:

- Only `llama3.2:3b` and `qwen3:4b` have evidence here.
- `gemma3:4b`, `granite3.3:8b`, `llama3.1:8b`, `phi4-mini`, and `mistral-nemo:12b` were not tested in this pass.

## Summary Matrix

| Model | Strategy | Options | Result |
| --- | --- | --- | --- |
| `llama3.2:3b` | Current Mena one-shot JSON payload | `temperature: 0` | Schema-valid but contextually wrong. Tail/small-talk content dominated and fabricated calendar dates appeared. |
| `llama3.2:3b` | Prompt-only one-shot variants | `temperature: 0` | Still failed. Wording changes, salience instructions, and manual-note repositioning did not recover substantive topic coverage. |
| `llama3.2:3b` | Plain-section one-shot payload | `temperature: 0` | Still failed despite reducing prompt size. This suggests JSON shape overhead is not the only problem. |
| `llama3.2:3b` | 8-minute chunked map/reduce | `temperature: 0` | First approach to recover substantive topic coverage. Still needs validation because reducer quality varied across runs. |
| `qwen3:4b` | Current Mena one-shot JSON payload | `temperature: 0`; default context | Schema-valid but failed. It hit only 1/5 core coverage criteria, remained tail/small-talk dominated, and evaluated only 1,636 prompt tokens. |
| `qwen3:4b` | Current Mena one-shot JSON payload | `temperature: 0`, `num_ctx: 8192` | Invalid result. The model consumed the 8,191-token window and returned empty content with `done_reason=length`. |
| `qwen3:4b` | Current Mena one-shot JSON payload | `temperature: 0`, `num_ctx: 32768` | Invalid result. The model consumed the 32,767-token window, returned empty content with `done_reason=length`, and required a CPU/GPU split. |
| `qwen3:4b` | Plain-section one-shot payload | `temperature: 0`; default context | Timed out at 240 seconds before producing a response. |
| `qwen3:4b` | 8-minute chunked map/reduce | `temperature: 0`; default context | Schema-valid but failed coverage and fabricated dates. Reduce response completed, but it did not meet export-quality criteria. |

## Research Questions

### 1. Which local models look trustworthy for German exports?

No new default model is justified from this evidence.

`qwen3:4b` should not become the default for this fixture. It failed the exact Mena one-shot payload, failed explicit `num_ctx` one-shot tests, timed out on the revised one-shot plain-section prompt, and produced a schema-valid but unacceptable chunked reduce output.

`llama3.2:3b` is also not trustworthy as a long-session one-shot summarizer. It is only promising when used inside a chunked pipeline, and even then the reducer requires quality gates before the artifact can be accepted as export-worthy.

### 2. Does `options.num_ctx` materially improve one-shot quality?

Not for this long-session fixture.

The default-context qwen run evaluated only a small tail slice of the prompt and produced a tail-dominated summary. Raising `num_ctx` to 8,192 and 32,768 did not improve quality; both explicit-context runs ended with no assistant content because the prompt consumed the available context.

The product conclusion is narrower than "never use `num_ctx`":

- Mena should treat context sizing as part of local summarization policy.
- Mena should measure prompt size before dispatch.
- Mena should not rely on `num_ctx` alone to make long-session one-shot summarization safe.
- For long sessions, the policy should route to chunking before the request approaches the model's usable context budget.

### 3. Is one-shot summarization good enough for PoC export quality?

No, not for long sessions like this fixture.

One-shot failures were not just formatting failures. They were trust failures: schema-valid output could ignore substantive meeting content, over-weight low-signal tail content, invent calendar dates, and turn action items into unusable labels.

For short sessions, one-shot may remain acceptable behind a prompt-size and quality-gate policy. For long sessions, chunking should be required.

### 4. Should timeline, important points, action items, and summary become separate passes?

Recommended, but as follow-up evidence rather than a final committed implementation shape.

The combined-schema reducer can recover coverage, but its failures are field-specific:

- action items can become topic labels instead of tasks;
- due dates can be missing or fabricated;
- timeline entries can become repeated bare dates rather than chronological topic events;
- general summary can be acceptable while action/timeline fields are not.

That pattern argues for separate field-focused reducers or validators. The next implementation spike should compare:

- one combined reduce pass with strict post-validation;
- separate reducers for timeline, important points, action items, and general summary;
- hybrid extraction, where action items and dates are extracted separately before the prose summary is generated.

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

Do not ship a model-only or prompt-only fix for #107.

Recommended PoC direction:

1. Keep the current local model as an interim short-session/default baseline only if guarded by prompt-size checks and quality gates.
2. Add a long-session chunked summarization policy.
3. Use manual notes as a coverage checklist during reduction.
4. Add post-generation quality gates before summary artifacts can feed Markdown export.
5. Treat `num_ctx` as a configurable runtime policy parameter, not the primary fix.
6. Continue model comparison after the pipeline/gates exist, because model quality without gates is not enough to protect user trust.

## Follow-up Issues

Suggested implementation issues:

1. Add prompt-size/context-budget preflight for local summarization.
2. Implement chunked local summarization for long sessions.
3. Add summary quality gates for coverage, low-signal dominance, date discipline, action shape, and timeline shape.
4. Add field-focused reduce experiments for timeline/action/important-points/general-summary outputs.
5. Add a repeatable sanitized model benchmark harness that records model tag, runtime state, options, elapsed time, schema validity, and quality-gate verdicts without committing private transcript or model output text.

Suggested research follow-up:

1. Complete the remaining candidate-model sweep once the benchmark harness can reuse the same quality gates.
2. Re-test 8B and 12B candidates only after confirming local runtime/GPU behavior is acceptable for PoC use.

## Bottom Line

The spike supports a product decision: export-worthy long-session summaries need a pipeline and acceptance gates, not just a better prompt, a larger context window, or a different small local model.
