# Ollama runtime mechanics check for #113 (note-product strategy)

Intended repo path: `docs/research/notes/2026-07-06-ollama-runtime-mechanics-for-note-product-strategy.md`

Date: 2026-07-06; local observation updated 2026-07-08

Status: verification note against current official Ollama documentation and a local Ollama smoke check, required by #113 and the #108 "re-read Ollama docs before coding" blocker. No private session content.

## Scope

Issue #113 requires verifying current official Ollama behavior before coding around context limits or structured-output assumptions: structured output usage, context length configuration, practical token/context budgeting, model support, and observable runtime metadata such as the `ollama ps` processor split.

## Evidence Sources

The environment used for #113 implementation could not reach `docs.ollama.com` (network policy), so the same documentation was read from its source of truth, the official `ollama/ollama` repository (`main` branch, fetched 2026-07-06):

- `docs/capabilities/structured-outputs.mdx` (renders as docs.ollama.com/capabilities/structured-outputs)
- `docs/context-length.mdx` (renders as docs.ollama.com/context-length)
- `docs/faq.mdx`
- `docs/api.md` (API reference for `/api/generate`, `/api/chat`)
- `docs/cli.mdx`

Local observed behavior was checked on 2026-07-08 with Ollama client `0.30.7` and locally installed `llama3.2:3b` (`a80c4f17acd5`):

- `/api/chat` accepted a JSON Schema `format` request with `options.num_ctx: 8192` and returned schema-shaped content `{"ok": true}`.
- The response included `prompt_eval_count: 54` and `eval_count: 6`, matching the documented per-call token-count fields Mena persists in `summary.v3`.
- `ollama ps` after the request showed `llama3.2:3b`, `PROCESSOR` = `100% GPU`, `CONTEXT` = `8192`, and an `UNTIL` lease deadline, confirming the documented runtime-observation columns on this dev machine.

This smoke check validates the assumptions #113 relies on without using private session content. It does not validate long-session quality or the target `llama3.1:8b` policy model under real load; #114 owns fixture benchmarking.

## Findings

### Structured outputs

- `format` on `/api/chat` accepts either `"json"` or a full JSON schema; constrained decoding guarantees shape only. The 2026-07-08 local smoke check confirmed that a full JSON Schema `format` request produces schema-shaped content.
- The docs explicitly recommend prompt grounding in addition to `format`: "It is ideal to also pass the JSON schema as a string in the prompt to ground the model's response." This answers the #108 open question — `format` alone is *not* the recommended usage.
- The docs recommend `temperature: 0` ("Lower the temperature (e.g., set it to 0) for more deterministic completions"), matching the existing #112 request discipline.

Consequence for #113: every summarization call (one-shot, map, reduce) now embeds its response JSON schema in the system prompt in addition to `format`.

### Context length

- Defaults are now VRAM-tiered: under 24 GiB VRAM → 4k context; 24–48 GiB → 32k; 48 GiB and above → 256k (`context-length.mdx`). The FAQ separately states a 4096-token default. Either way, the default on the PoC target class of machine is small; never rely on it.
- Context length can be set per server (`OLLAMA_CONTEXT_LENGTH=... ollama serve`), per model (Modelfile `PARAMETER num_ctx`), or per request (`options.num_ctx`). Mena keeps the per-request `options.num_ctx` (currently 8192) so behavior does not depend on host configuration.
- Memory grows with context length; parallel serving multiplies allocation (`OLLAMA_NUM_PARALLEL`, default 1, scales required RAM by parallelism × context length). Flash Attention and K/V cache quantization change the memory curve. `num_ctx: 8192` therefore remains a policy constant that must be validated against the target machine (open since #112).

### Truncation and budgeting

- The API reference documents an explicit `truncate` parameter only for `/api/embed`. For `/api/chat` / `/api/generate` there is no documented refuse-on-overflow option; oversized prompts are truncated server-side (observed from the head in #108's failure class). Mena's pre-send hard-fail budget guard therefore remains the first line of defense.
- `prompt_eval_count` (tokens in the prompt as evaluated) and `eval_count` (tokens generated) are documented response fields. Persisting them per call makes truncation detectable after the fact: a `prompt_eval_count` pinned near `num_ctx` while the sender-side estimate was much larger indicates server-side truncation.
- Mena has no preflight tokenizer for Ollama models, so prompt estimates count UTF-8 bytes — a hard upper bound for byte-fallback BPE tokenizers. The overshoot is deliberate: it routes long inputs to the reducer strategy earlier rather than risking truncation.

### Runtime observability

- `ollama ps` lists running models with `SIZE`, `PROCESSOR` (CPU/GPU split), `CONTEXT` (allocated context length), and `UNTIL`. The 2026-07-08 local smoke check observed these columns after an 8192-context request, with `PROCESSOR` = `100% GPU` and `CONTEXT` = `8192`.
- `keep_alive: 0` unloads a model immediately (used by Mena's runtime-stewardship release path); `keep_alive: -1` pins it.

### Model support

- The docs recommend using a model's maximum context and avoiding CPU offload for best performance, but do not publish a per-model context matrix in these pages; `ollama ps` (`CONTEXT` column) and the server log remain the practical verification that the requested `num_ctx` was actually allocated for the selected model on the user's hardware.

## Implications applied in #113

1. Schema-in-prompt grounding added to all note-product calls (one-shot, map, reduce).
2. Strategy routing (one-shot vs reducer) driven by the same byte-estimate budget arithmetic as the pre-send hard-fail guard; both live behind `note_product_strategy.py`.
3. Every call persists `prompt_eval_count`/`eval_count` in a per-call `request_records` entry (`summary.v3`), so truncation and cost stay auditable per map/reduce call, not only per product.
4. `num_ctx` stays an explicit per-request option; nothing relies on server or Modelfile defaults.

## Follow-up verification

- `ollama ps` during a real summarize run with the selected summarization model: confirm `CONTEXT` shows 8192 and `PROCESSOR` is not unexpectedly CPU-heavy.
- Compare persisted `prompt_eval_count` values against the byte estimates in provenance for one long session: values pinned at/near `num_ctx` with much larger estimates would indicate the guard fraction needs lowering.
- If the target machine cannot allocate 8192 context on GPU, revisit `OLLAMA_SUMMARIZATION_NUM_CTX` (policy constant) rather than letting Ollama fall back silently.
