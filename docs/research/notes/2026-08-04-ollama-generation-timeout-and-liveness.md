# Ollama generation timeout and liveness mechanics

Date: 2026-08-04

Status: focused primary-source research prompted by a real `mena summarize` failure on the accepted local `llama3.1:8b` route. No private meeting content is included.

## Question

Does Ollama provide a built-in "semantic timeout" that Mena can use instead of a fixed wall-clock timeout, and what should timeout mean for local LLM summarization?

## Finding

Ollama has no documented or source-visible "semantic timeout" for generation. It exposes several separate mechanisms that must not be conflated:

- model-driven termination through end-of-sequence or configured stop strings;
- a generated-token ceiling through `num_predict`;
- streamed NDJSON response chunks that can serve as observable generation progress;
- final `done` and `done_reason` fields;
- cancellation inherited from the HTTP request context;
- `OLLAMA_LOAD_TIMEOUT`, which detects a stalled **model load**, not slow inference;
- `keep_alive`, which controls post-request model residency, not request duration.

Mena therefore owns the liveness policy for summarization. Ollama supplies progress and termination signals, but not a task-aware timeout policy.

## Terminology

### Wall-clock deadline

Cancel a request after a fixed elapsed duration whether or not it is progressing. Mena's current 60-second `urllib` timeout behaves this way for its non-streaming request: no response body is visible until Ollama has completed the whole generation. This is brittle because queueing, model load, prompt evaluation, model size, hardware placement, output length, and token throughput all affect elapsed time.

### Inactivity or stall timeout

Cancel only after no observable progress for a bounded interval. With Ollama streaming, each received NDJSON generation chunk can reset an inter-chunk timer. This is a liveness check, not a quality check.

There is an important blind interval: Ollama does not document heartbeat frames during queueing, model loading, or prompt evaluation. A streaming client may therefore see no chunk before the first generated token. Time-to-first-token and inter-token inactivity need different, phase-aware treatment; one small idle timer is still unsafe.

### Token or output budget

`num_predict` bounds how many output tokens Ollama may generate. When the limit is exhausted, Ollama reports `done_reason: "length"`. This bounds output and work more directly than elapsed time, but it does not prove semantic completeness and does not detect a loaded runner that has stopped making progress.

### Semantic completion

Semantic completion is application-level evidence that the requested product is complete. For Mena that requires, at minimum:

1. Ollama reaches a normal terminal condition (`done: true`, normally `done_reason: "stop"` rather than `"length"`); and
2. the accumulated structured output satisfies the product JSON schema and Mena's stronger product contract, including required grounding and non-empty fields where applicable.

"Semantic timeout" is therefore misleading terminology. Semantics determine whether a completed response is acceptable; timeout determines whether execution is live. Elapsed time cannot establish semantic completion.

## Ollama evidence

- The [official streaming API documentation](https://docs.ollama.com/api/streaming) says generation endpoints stream NDJSON by default and shows partial chunks followed by a final `done: true` chunk with `done_reason: "stop"`. Streaming is recommended for long generations; non-streaming returns one JSON object only after completion.
- The [official chat API reference](https://docs.ollama.com/api/chat) exposes `stream`, structured `format`, `options`, and `keep_alive`, and reports final timing/token metrics including `total_duration`, `load_duration`, `prompt_eval_count`, `prompt_eval_duration`, `eval_count`, and `eval_duration`.
- The [official Modelfile reference](https://docs.ollama.com/modelfile) defines `num_predict` as the maximum number of tokens to predict and `stop` as generation stop sequences.
- Ollama's [`api.Options` source](https://github.com/ollama/ollama/blob/8edecb5c69cd1115ba7b5b8c96d4b061803ccd14/api/types.go#L564-L585) contains `NumPredict` and `Stop`; its [default options](https://github.com/ollama/ollama/blob/8edecb5c69cd1115ba7b5b8c96d4b061803ccd14/api/types.go#L1090-L1105) set `NumPredict` to `-1`.
- Ollama's runner maps termination to [`stop` or `length`](https://github.com/ollama/ollama/blob/8edecb5c69cd1115ba7b5b8c96d4b061803ccd14/llm/server.go#L248-L267).
- The generation handler passes the [HTTP request context into the runner](https://github.com/ollama/ollama/blob/8edecb5c69cd1115ba7b5b8c96d4b061803ccd14/server/routes.go#L650-L730), and the runner [aborts when that client context is cancelled](https://github.com/ollama/ollama/blob/8edecb5c69cd1115ba7b5b8c96d4b061803ccd14/llm/llama_server.go#L1480-L1510). A Mena client timeout therefore cancels useful ongoing inference.
- Ollama's [streaming writer](https://github.com/ollama/ollama/blob/8edecb5c69cd1115ba7b5b8c96d4b061803ccd14/server/routes.go#L2079-L2120) emits a line when a response arrives; it does not add heartbeat frames.
- Ollama's [`OLLAMA_LOAD_TIMEOUT` implementation](https://github.com/ollama/ollama/blob/8edecb5c69cd1115ba7b5b8c96d4b061803ccd14/envconfig/config.go) explicitly describes model-load stall detection, defaulting to five minutes. It is not a generation timeout.
- The [official keep-alive documentation](https://docs.ollama.com/faq#how-can-i-keep-a-model-loaded-in-memory-or-make-it-unload-immediately) describes how long a model remains loaded after a request. It cannot rescue an active request from a client deadline.

## Mena evidence

The 2026-08-04 failure was not provider unavailability:

- Ollama accepted the request, loaded `llama3.1:8b`, evaluated a 2,156-token prompt, and generated 517 tokens at roughly 10 tokens/second.
- At exactly 60 seconds, Mena's client cancelled the still-progressing request.
- Mena then classified the timeout as `SummarizationProviderUnavailable` because `post_ollama_json()` maps every `OSError` to provider unavailability.

Earlier local summaries did succeed, but under a materially different workload. Three persisted `summary.v1` runs from 2026-06-29/30 used `llama3.2:3b`, made one monolithic request, and completed in five to six seconds. Current `summary.v3` uses `llama3.1:8b` and separate timeline, action-item, and general-summary product generation. Splitting improves product contracts and input budgeting; it does not guarantee that any individual structured product response completes inside 60 seconds.

## Recommendation

### Immediate release fix

Replace the 60-second limit with a generous emergency wall-clock ceiling measured in minutes, and classify expiry explicitly as a timeout rather than provider unavailability. This is deliberately a containment fix: it restores useful behavior quickly but does not provide good progress semantics.

Do not expose the existing arbitrary number as a user-facing tuning requirement. The accepted runtime profile should work on the hardware setup accepted.

### Correct adapter behavior

1. Request `stream: true` and incrementally parse/accumulate Ollama's NDJSON chunks.
2. Use separate liveness phases:
   - a generous time-to-first-token ceiling covering queue, load, and prompt evaluation;
   - an inter-chunk inactivity watchdog reset by each received chunk;
   - optionally, a very generous absolute emergency ceiling to contain pathological runs.
3. Set a product-specific `num_predict` budget. Treat `done_reason: "length"` as incomplete output, never success.
4. Require `done: true`, a normal stop reason, valid JSON-schema output, and Mena product-contract validation before accepting a product.
5. Preserve the model lease across the timeline, action-item, and general-summary calls, then unload at the stage boundary. `keep_alive`/lease management improves later-call latency but remains separate from timeout policy.
6. Persist failure classification and observed phase/progress: time to first chunk, last-chunk time, chunk/output-token count, and terminal reason. Report `timed out waiting for first output` or `generation stalled after N output tokens`, not `provider unavailable`.

Streaming progress cannot detect semantically repetitive output that continues producing tokens. The output-token budget contains that failure class; final contract validation determines whether the result is usable. These controls are complementary rather than interchangeable.

## Architectural boundary

Provider adapters own provider-specific streaming and progress mechanics. Core summarization/runtime orchestration owns retry/fallback policy and user-facing failure semantics. The note-product strategy continues to own one-shot versus reducer mechanics. None of these concerns belongs in the CLI adapter.
