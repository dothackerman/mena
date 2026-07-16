# Bounded long-input generation strategies: primary-source review

**Scope.** This note supports Mena issue [#199](https://github.com/dothackerman/mena/issues/199), Phase 1 only. It compares established strategies for generating grounded products from inputs that can grow beyond a model context window. It contains no private fixture content or experimental results.

**Source policy.** The evidence below is limited to original papers, authors' project pages, and official framework documentation. Architectural conclusions labelled **Mena inference** are deductions from those sources and issue #199's invariants, not claims made by the cited authors.

## Executive conclusion

No established strategy gets boundedness, fidelity, chronology, and graceful degradation merely by “chunking.” A production algorithm needs two separate controls:

1. a **mechanical budget invariant** proving that every prompt plus reserved output fits before dispatch; and
2. an **information contract** defining what may be compressed, what provenance survives, how conflicts are represented, and what happens when the declared output itself cannot fit.

The strongest default for Mena is product-specific:

- **Timeline:** evidence-first extraction into atomic, source-addressed events; deterministic validation, stable chronological ordering, grouping, and conservative deduplication; then bounded local wording/merging. Do not recursively summarize away event evidence.
- **Action items:** evidence-first extraction into atomic candidates with actor/action/status/due-date fields and source spans; deterministic exact/near-duplicate grouping; bounded synthesis only within groups. Contradictions remain explicit alternatives rather than being “resolved” by prose generation.
- **General summary:** budgeted hierarchical reduction is appropriate, but each intermediate should retain source references and declared coverage. Source-context augmentation at later levels is preferable to a pure summary-of-summaries tree.
- **Rolling refinement:** useful only as a deliberately order-sensitive adjunct (for example, updating a small narrative overview), not as Mena's canonical evidence accumulator.

This recommendation is deliberately stricter than framework defaults. Official frameworks establish that tree and refine modes are common orchestration patterns, but they do not by themselves establish bounded intermediate size, monotonic progress, provenance, or acceptable failure behavior.

## Common invariant and progress measure

Let:

- `C` = provider/model context limit under the effective runtime options;
- `R` = reserved maximum output tokens;
- `H(p)` = tokens for fixed instructions, schema, separators, and metadata for product `p`;
- `X` = tokens in the candidate evidence/intermediate batch;
- `M` = explicit safety margin for tokenizer/counting mismatch.

Every call must satisfy:

```text
H(p) + X + R + M <= C
```

The actual serialized request must be measured with the same tokenizer/budget arithmetic used by provider preflight. Output limits are not evidence that the input fits. OpenAI's official response-length guidance likewise distinguishes an output-token cap from prompting for a desired length; an output cap controls generation but does not solve input packing ([OpenAI, “Controlling the length of model responses”](https://help.openai.com/en/articles/5072518-controlling-the-length-of-openai-model-responses)).

For recursive reduction, define the level measure as the pair `(total_serialized_tokens, item_count)`. A level succeeds only if the next level is strictly smaller in at least one component and never larger in the other beyond a declared envelope. More simply, require a configured contraction ratio or a hard per-item bound and fan-in `>= 2`. If a model produces an oversized/non-contracting intermediate, retrying without a finite policy is not progress; return a controlled `non_contracting_reduction` failure after the declared attempt limit.

For evidence-first aggregation, progress is structural: the finite source chunk cursor advances exactly once, then deterministic stages transform a finite candidate set. If synthesis batches are needed, each batch is consumed exactly once. Termination does not depend on an LLM choosing to become concise.

## Strategy comparison

| Property | Budgeted hierarchy/tree | Evidence extraction + deterministic aggregation | Iterative/rolling refinement | Product-specific hybrid |
|---|---|---|---|---|
| Call-bound invariant | Pack each node from measured children under the common inequality | Pack source chunks for extraction; pack only validated evidence groups for synthesis | Pack prior state + next source chunk under the inequality; state needs a hard bound | Apply the corresponding invariant independently per product/stage |
| Termination | Yes, only with enforced contraction/fan-in or explicit failure | Yes: finite source scan plus finite deterministic transforms | Yes for a finite scan, but not safe without a bounded state and finite retry policy | Yes if every constituent stage declares progress |
| Provenance | Easily weakened at every abstraction level unless references are contractual | Strongest: source IDs/spans are first-class and can be validated before generation | Weak: early provenance tends to be rewritten or omitted from the rolling state | Keep evidence-first provenance; allow abstraction only where the product tolerates it |
| Order | Preserved only if child order and merge semantics are explicit | Stable sort by source-derived time/position is deterministic | Intrinsically order-sensitive; later chunks can overwrite/reframe earlier state | Timeline uses deterministic chronology; summary can preserve section order or group by topic intentionally |
| Dedup/conflicts | Model-mediated at merge nodes; results can differ with tree shape | Deterministic keys/grouping; unresolved conflicts remain represented | Repeated rewriting can silently collapse or choose between claims | Deterministic first, bounded model-assisted comparison only within candidate groups |
| Principal quality risk | information/hallucination amplification through recursive abstraction | extraction recall and schema errors; synthesis can still misstate evidence | anchoring, recency, and accumulated distortion | More engineering/contracts, but localizes each risk |
| Calls/latency | Approximately `N + ceil(N/f) + ...`; parallel within levels, sequential across depth | `N` parallel extraction calls plus optional bounded group/product synthesis | About `N` sequential calls; poor wall-clock scaling | Different call graph per product; shared extraction may be possible only if contracts truly overlap |
| Explicit degradation | reduce detail/coverage class or fail when contraction cannot preserve contract | emit validated partial/overflow result with observable omitted-count/reason, or fail | freeze/drop state fields only under declared policy, otherwise fail | Product-specific partial/failure status; never truncate silently |

### 1. Budgeted hierarchical/tree reduction

**What is established.** Summ^N splits long input and performs multi-stage coarse summarization before final fine-grained generation. Its authors explicitly claim arbitrary input length can be handled by increasing the number of stages while keeping model input size fixed, and report gains on meeting, dialogue, narrative, and government-report datasets ([Zhang et al., ACL 2022](https://aclanthology.org/2022.acl-long.112/); [authors' code](https://github.com/psunlpgroup/Summ-N)). OpenAI's book-summarization work used the same broad recursive decomposition—summarize sections, then summarize summaries—and describes it as scaling to books of unbounded length ([OpenAI, 2021](https://openai.com/index/summarizing-books/)).

Official LlamaIndex documentation exposes `tree_summarize`: it recursively treats answers as new chunks until one answer remains. It also documents `simple_summarize` as truncating inputs that do not fit, a useful warning that “single call” convenience is incompatible with issue #199's no-silent-loss invariant ([LlamaIndex response modes](https://developers.llamaindex.ai/python/framework/module_guides/deploying/query_engine/response_modes/)). LangChain's official `ReduceDocumentsChain` similarly exposes a `token_max` used when grouping documents, plus `collapse_max_retries`; its API warns that unlimited collapse retries can loop indefinitely ([LangChain `ReduceDocumentsChain`](https://python.langchain.com/api_reference/langchain/chains/langchain.chains.combine_documents.reduce.ReduceDocumentsChain.html)).

**Boundedness and termination.** A tree is bounded only if the implementation measures each packed node and reserves output. “Recursively summarize until one remains” proves decreasing item count when fan-in is at least two, but does not prove decreasing token size: a model can emit children whose aggregate is not smaller. Mena therefore needs schema and token bounds on every intermediate plus a non-contraction failure. With `N` leaf chunks and effective fan-in `f >= 2`, depth is at most `ceil(log_f N)` when each node contracts successfully, and total calls are roughly `N + N/f + N/f² + …` (or fewer if leaf extraction already exists).

**Fidelity and accumulated error.** Recent original work directly tests the weakness: Ou and Lapata report that recursive merging can amplify hallucinations, and that augmenting merge stages with source context consistently outperformed plain hierarchical merging on legal and narrative datasets; refinement worked best with extractive source selection ([Ou & Lapata, ACL Findings 2025](https://arxiv.org/abs/2502.00977)). This supports source-addressed intermediates and rehydrating selected raw evidence at merge time, rather than trusting only earlier abstractive prose.

**Chronology, deduplication, contradictions.** A tree preserves chronology only if leaves are contiguous and every merge retains child order. Topic-based regrouping can improve coherence but destroys timeline order unless timestamps remain machine-readable. Deduplication and contradiction handling delegated to each merge are tree-shape dependent: two duplicates in distant branches are not compared until late, after each may have been paraphrased. A bounded typed intermediate should therefore carry stable evidence IDs, time/position ranges, and conflict state across levels.

**Best fit.** General summary, where deliberate salience compression is the product. It is a poor canonical representation for timeline/action items because “coarse summary” is permitted to omit exactly the low-frequency event or commitment those products must preserve.

### 2. Evidence extraction, deterministic aggregation, bounded synthesis

**What is established.** Extract-then-abstract is a long-standing long-document pattern. Chen and Bansal's original model first selects salient sentences and then rewrites them, separating evidence selection from surface realization ([Chen & Bansal, ACL 2018](https://aclanthology.org/P18-1063/)). HERA more recently segments long documents, retrieves passages about the same event, and reorders them before generation; the authors report improvements in ROUGE, BERTScore, and faithfulness without fine-tuning ([Li et al., 2025](https://arxiv.org/abs/2502.00448)). These papers do not implement Mena's exact typed event/action pipeline, but they support the architectural separation between evidence selection/packaging and final wording.

Atomicity and citations are also empirically motivated. FActScore decomposes long-form output into atomic facts and checks each against a reliable source, because a single output commonly mixes supported and unsupported claims ([Min et al., EMNLP 2023](https://aclanthology.org/2023.emnlp-main.741/)). ALCE evaluates citation correctness and completeness separately from fluency; even its best evaluated systems lacked complete citation support half the time on ELI5, showing that asking a model to “include citations” is not a sufficient provenance guarantee ([Gao et al., EMNLP 2023](https://aclanthology.org/2023.emnlp-main.398/); [official code](https://github.com/princeton-nlp/ALCE)).

**Mena inference: bounded contract.** Extract one atomic candidate per independently verifiable event/action, with at least:

```text
candidate_id, product_kind, normalized_fields,
source_chunk_id, source_start/end (or stable utterance IDs),
source_time/position, evidence_text_or_hash,
confidence/ambiguity flags
```

Validate types, source ranges, timestamp derivation, and product-specific required fields deterministically. Reject or quarantine invalid candidates; never repair invented provenance with another model call.

**Ordering and chronology.** Preserve source order as an immutable tiebreaker. Timeline aggregation can stable-sort by validated source-derived timestamp, then source position. HERA's finding that event-related evidence can be scattered and that packaging/reordering improves generation supports grouping related evidence, but grouping for synthesis must not overwrite the canonical chronological key.

**Deduplication and contradictions.** Use exact normalized keys first. For fuzzy candidates, generate bounded comparison groups with blocking keys (overlapping time window, actor/entity, action fingerprint), not an all-pairs prompt. A model may label `same`, `related`, `contradictory`, or `distinct`, but deterministic code applies the label while retaining all contributing source IDs. Contradictions should become explicit competing values/claims; the generator is not an authority that may choose one silently.

**Failure/degradation.** This strategy moves the capacity problem from raw text to potentially unbounded evidence. A meeting could contain more legitimate events/actions than the final product budget. The correct response is a declared product policy: paginated/sectioned output, a visible `partial` result with included/omitted counts and selection rule, or controlled `evidence_exceeds_product_capacity`. Arbitrary top-`k` or tail truncation violates no-silent-loss. Extraction recall remains the major irrecoverable risk, so evaluation must include product-specific fact recall, not merely factual precision.

**Quality/cost.** Source chunks extract in parallel. Deterministic aggregation is cheap and reproducible. Optional model comparisons/synthesis are limited to small groups. This usually costs more calls than a one-shot prompt but fewer sequential stages than rolling refinement, and it gives much stronger observability. The final prose quality can be weaker if synthesis is overly local; bounded adjacent-group or section synthesis can restore coherence without making provenance global and fragile.

### 3. Iterative/rolling refinement

**What is established.** LlamaIndex's official `refine` mode makes one LLM call per retrieved chunk: the first creates an answer and every later call updates it; `compact` concatenates as many chunks as fit first to reduce the number of calls ([LlamaIndex response modes](https://developers.llamaindex.ai/python/framework/module_guides/deploying/query_engine/response_modes/)). This is an established orchestration pattern, not a proof of faithful long-document aggregation.

**Boundedness.** Each call can fit if both the new chunk and prior state have hard bounds. Without a bounded state schema/output limit, the state can grow until the next call fails. With a fixed state budget, the finite chunk cursor proves termination in `N` calls; retries must be capped.

**Order and error accumulation.** Refine is intrinsically non-commutative: permuting chunks changes every subsequent state. Earlier evidence can be compressed repeatedly; later evidence has the final opportunity to reframe the answer. Long-context experiments independently show that relevant information position materially changes model performance, with beginning/end advantages and middle degradation ([Liu et al., TACL 2024](https://doi.org/10.1162/tacl_a_00638); [paper preprint](https://arxiv.org/abs/2307.03172)). Although that study evaluates information within a long prompt rather than rolling state, it rules out assuming position neutrality. Rolling refinement adds another recency/anchoring pathway and should be benchmarked under chunk-order perturbations.

**Provenance, duplicates, contradictions.** Unless the rolling state is a typed evidence ledger, every rewrite can delete citations, merge duplicates opaquely, or resolve contradictions without trace. If it *is* a typed evidence ledger, the design has effectively become evidence-first aggregation—except sequential and harder to parallelize.

**Quality/cost.** It can maintain a coherent single voice and incorporate local context, but calls are sequential and wall-clock latency grows linearly with chunks. Its chief advantage is avoiding a large final fan-in; its chief weakness is making the latest generated state the sole bottleneck for all previous information.

**Best fit.** A secondary bounded narrative state whose loss is acceptable and observable. Not the source of truth for events or commitments.

### 4. Product-specific hybrids

The literature supports combinations rather than one universal reducer. Ou and Lapata's results favor hierarchical merging enriched with extracted source context; HERA separates semantic segmentation, event grouping, ordering, and generation; frameworks expose tree and refine as different response semantics rather than interchangeable optimizations.

**Mena inference:** keep product strategy behind independent core interfaces and keep provider mechanics below them:

- **Core product logic:** candidate/intermediate schemas; source references; stable ordering; validation; dedup/conflict states; packing policy; progress measure; degradation/failure status; final structural checks.
- **Provider mechanics:** token counting for the effective model/options; request serialization; output reservation; generation invocation; provider error normalization.

The core asks whether a serialized proposed call fits through a provider budget service, but the provider must not choose which evidence to discard. This preserves product independence and makes “bounded by construction” testable with a fake budget service.

## Required observable failure modes

At minimum, distinguish:

- `source_unit_exceeds_budget`: one indivisible source unit cannot be processed under the current contract;
- `intermediate_contract_violation`: malformed, overlong, or provenance-invalid model output;
- `non_contracting_reduction`: a hierarchy did not shrink under the configured finite attempts;
- `evidence_exceeds_product_capacity`: valid evidence cannot fit the declared final product contract;
- `partial_by_policy`: an explicitly configured, deterministic coverage cap was applied, with included/omitted counts and rule;
- `provider_budget_mismatch`: provider/tokenizer accounting disagreed with preflight.

Partial extraction should carry processed/total source-unit counts. No failure should be converted into a fluent “best effort” note without machine-readable incompleteness.

## Benchmark implications for Phase 2

Beyond issue #199's controls, the primary-source evidence suggests these discriminating tests:

1. **Order perturbation:** preserve source timestamps but reverse or permute chunk delivery. Timeline/action canonical records should remain invariant; rolling prose probably will not.
2. **Middle-position probes:** place checklist evidence at early/middle/late positions to expose positional loss, motivated by Liu et al.
3. **Duplicate distance:** repeat semantically equivalent evidence in adjacent and distant chunks; verify retained provenance and stable merge results.
4. **Contradiction preservation:** inject sanitized checklist claims that conflict across chunks; score whether both sources and conflict state survive.
5. **Adversarial non-contraction:** force maximum-length intermediate outputs; require finite, observable failure rather than recursion.
6. **Capacity cliff:** increase valid candidates beyond final output capacity; verify the configured explicit degradation rather than truncation.
7. **Citation completeness and correctness:** score separately, following ALCE's distinction; use atomic fact support/precision in the spirit of FActScore, plus independent recall because FActScore itself is precision-focused.

## Sources

- Yusen Zhang et al., **“Summ^N: A Multi-Stage Summarization Framework for Long Input Dialogues and Documents,”** ACL 2022. [ACL Anthology](https://aclanthology.org/2022.acl-long.112/) · [authors' repository](https://github.com/psunlpgroup/Summ-N)
- OpenAI, **“Summarizing books with human feedback,”** 2021. [Official project page](https://openai.com/index/summarizing-books/)
- Litu Ou and Mirella Lapata, **“Context-Aware Hierarchical Merging for Long Document Summarization,”** Findings of ACL 2025. [arXiv](https://arxiv.org/abs/2502.00977)
- Taiji Li et al., **“HERA: Improving Long Document Summarization using Large Language Models with Context Packaging and Reordering,”** 2025. [arXiv](https://arxiv.org/abs/2502.00448)
- Yen-Chun Chen and Mohit Bansal, **“Fast Abstractive Summarization with Reinforce-Selected Sentence Rewriting,”** ACL 2018. [ACL Anthology](https://aclanthology.org/P18-1063/)
- Nelson F. Liu et al., **“Lost in the Middle: How Language Models Use Long Contexts,”** TACL 2024. [TACL DOI](https://doi.org/10.1162/tacl_a_00638) · [arXiv](https://arxiv.org/abs/2307.03172)
- Sewon Min et al., **“FActScore: Fine-grained Atomic Evaluation of Factual Precision in Long Form Text Generation,”** EMNLP 2023. [ACL Anthology](https://aclanthology.org/2023.emnlp-main.741/)
- Tianyu Gao et al., **“Enabling Large Language Models to Generate Text with Citations,”** EMNLP 2023. [ACL Anthology](https://aclanthology.org/2023.emnlp-main.398/) · [authors' repository](https://github.com/princeton-nlp/ALCE)
- LlamaIndex, **“Response Modes.”** [Official documentation](https://developers.llamaindex.ai/python/framework/module_guides/deploying/query_engine/response_modes/)
- LangChain, **`ReduceDocumentsChain` API reference.** [Official documentation](https://python.langchain.com/api_reference/langchain/chains/langchain.chains.combine_documents.reduce.ReduceDocumentsChain.html)
- OpenAI, **“Controlling the length of OpenAI model responses.”** [Official documentation](https://help.openai.com/en/articles/5072518-controlling-the-length-of-openai-model-responses)

## Evidence boundaries

- Published results use different models, datasets, prompts, and metrics; they motivate designs and risks but do not predict Mena's private-fixture ranking. Phase 2 must decide that empirically.
- “Arbitrary” or “unbounded” input claims in recursive-summarization work mean context-window scalability through additional stages, not guaranteed retention of all evidence or bounded total cost.
- Atomic facts and citations improve inspectability; neither guarantees extraction recall or citation correctness. Validation and product-specific recall checks remain necessary.
- Deterministic ordering/deduplication, explicit conflict states, monotonic progress checks, and named degradation errors are architectural recommendations derived for issue #199. They are not turnkey features established by the cited frameworks.
