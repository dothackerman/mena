# ADR-035: Provider-owned note-product execution strategy behind the capability resolver

- **Type:** Architecture
- **Status:** Accepted
- **Date:** 2026-07-06
- **Superseded by:** —
- **Related:** #113, #108, #120, ADR-010, ADR-021, ADR-034

## Context

ADR-034 made note-product generation per-product and named provider-owned execution strategy (one-shot now, chunked map/reduce later) as the seam for long-input handling. Separately, #120 landed the capability resolver: the single module that turns a stage plus accepted runtime-profile state into a bound, authorized provider, owning eligibility, authorization, fallback re-selection, and route provenance.

Implementing the reducer path (#113) forces the question of where one-shot vs reducer selection lives. A second selection mechanism next to the resolver — or a resolver that reasons about model context windows — would blur the Provider Adapter Boundary and re-create the per-command provider selection the resolver was built to remove.

Evidence from #108 also fixed the routing input: Ollama truncates oversized prompts silently, so the decision must come from explicit token-budget arithmetic, not from guessing by transcript length or duration.

## Decision

The capability resolver and the provider split the seam as policy vs mechanics:

- **Resolver owns provider policy.** Which summarization provider profile serves the stage, whether it is authorized, when to fall back, and route provenance. One provider is bound per summarize run; all product calls go to it (ADR-034). Nothing about `one_shot` vs `reducer` is visible to, or decided by, the resolver or the summarize workflow.
- **Provider owns strategy mechanics.** Behind the per-product call, the bound provider selects the note-product execution strategy from its own model/context constraints. Selection is budget arithmetic, shared with the pre-send hard-fail guard so routing and the guard cannot disagree: a one-shot prompt within the `num_ctx` token budget runs one-shot; anything larger runs as chunked map/reduce. The pure arithmetic (strategy selection, chunk planning) lives in `note_product_strategy.py`; the Ollama adapter owns the request mechanics.
- **Strategy is provenance, not policy.** Each provider call is persisted as one `summary.v3` request record carrying strategy, call kind (`one_shot`/`map`/`reduce`), chunk coordinates, and `prompt_eval_count`/`eval_count`; the artifact metadata names each product's strategy. Reuse identity continues to come from prompt policy plus effective provider options, not from which strategy executed.
- **No second selection mechanism.** Anything that chooses *between providers* belongs to the resolver; anything that chooses *how one provider executes* belongs to the provider. Future providers (for example direct Mistral, ADR-033) may select different strategies behind the same seam without resolver changes.

Reducer shape for Ollama: per-product map calls over consecutive transcript chunks (specialized candidate-extraction prompts, empty results allowed), then one per-product reduce call that merges candidates plus manual notes into the final product schema. Failures are loud: a single over-budget segment, a reduce prompt that can never fit, or an all-empty timeline candidate set with no manual notes to ground the reduction abort the run rather than truncate or fabricate. Manual notes are legitimate reduce-step grounding — the one-shot strategy already generates notes-informed timelines, and strategy selection must stay invisible in acceptance behavior, so the reducer may not be stricter than one-shot for the same inputs.

## Consequences

- Timeline, action items, and general summary remain stable note-product processes to the workflow and export; strategy changes never touch the summarize workflow, the resolver, or export.
- Long meetings cost more local compute: per product, one map call per chunk plus a reduce call (three specialized passes per chunk rather than one shared extraction). This is a deliberate quality-over-compute trade following the #108 prompt-lab evidence that specialized passes recover more real meeting content; a shared map pass would also break clean per-product provenance.
- Hierarchical (multi-level) reduction is intentionally not built; inputs whose candidates exceed the reduce budget fail loudly. Building it requires new evidence, not a silent extension.
- The `summary.v2` → `summary.v3` contract bump means older sessions need one re-summarize before export, matching the ADR-034 precedent.
