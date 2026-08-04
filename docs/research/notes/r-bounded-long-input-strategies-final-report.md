# Bounded long-input generation strategies: final report

> **Status correction (2026-08-04).** Despite the historical filename, this is
> the report for #199 experiment 1, not a final strategy-selection verdict. The
> completed comparison below falsified plain hierarchy and exposed the output-
> capacity problem in unconstrained evidence retention, but the two arms did
> not solve the same final-product problem and therefore cannot select a
> winner. A later source-aware prototype also failed operationally before
> producing a package. Issue #207 continues the research with a user-visible,
> bounded baseline before controlled aggregation comparisons.

**Issue.** [#199](https://github.com/dothackerman/mena/issues/199) asks which bounded long-input strategy Mena should use for each `summary.v3` note product before implementing the real #114 note package work.

**Privacy boundary.** This report uses only sanitized benchmark metrics plus private local inspection of generated outputs. It does not quote or publish the RC7 meeting transcript, manual notes, private generated note products, or Gemma partial-run data.

**Benchmark run.** The authoritative completed run was `llama3.1:8b` through Ollama with `num_ctx=8192`, a prototype prompt budget of `6963`, and 28 source chunks. The prototype is in [`app/prototypes/issue199_bounded_long_input.py`](../../../app/prototypes/issue199_bounded_long_input.py). It compared:

1. **Budgeted hierarchy:** chunk-level product extraction followed by bounded recursive merge.
2. **Evidence-first aggregation:** chunk-level product extraction into atomic records for timeline/action items, deterministic ordering/deduplication for those products, and bounded synthesis for the general summary.

## Experiment 1 interpretation

The evidence supports testing a **product-specific hybrid**, not implementing
one universal reducer as a settled production choice:

- **Timeline:** evidence-first extraction into atomic, source-addressed events, followed by deterministic validation, chronological ordering, deduplication, and explicit capacity handling.
- **Action items:** evidence-first extraction into atomic candidates with owner/due/status/source fields, deterministic duplicate grouping, explicit conflict representation, and explicit capacity handling.
- **General summary:** bounded hierarchical reduction is acceptable only for summary prose, but merge stages should carry source references and coverage metadata, and should rehydrate selected source/evidence context rather than reducing pure prose summaries.

The Llama run reinforced the source-review conclusion: chunking alone is not an architecture. The system needs mechanical prompt-budget invariants plus product-specific information contracts. The exact extraction and aggregation contracts remain hypotheses for #207 to test.

## Sanitized Results

| Strategy | Elapsed | Calls | Max prompt bytes | Max prompt eval tokens | Total prompt eval tokens | Total eval tokens | All calls fit preflight |
|---|---:|---:|---:|---:|---:|---:|---|
| Budgeted hierarchy | 2918.77s | 109 | 5736 | 1535 | 150661 | 27078 | yes |
| Evidence-first | 1802.43s | 89 | 5334 | 1535 | 128285 | 20041 | yes |

| Strategy | Product | Probe recall | Fabricated 2023 date | Output bytes | Reduction depth |
|---|---|---:|---|---:|---:|
| Budgeted hierarchy | Timeline | 0/5 | no | 736 | 3 |
| Budgeted hierarchy | Action items | 0/5 | no | 963 | 3 |
| Budgeted hierarchy | General summary | 0/5 | no | 312 | 2 |
| Evidence-first | Timeline | 3/5 | no | 32173 | 0 |
| Evidence-first | Action items | 2/5 | no | 22422 | 0 |
| Evidence-first | General summary | 0/5 | no | 978 | 2 |

The probe recall checks were deliberately narrow canaries: digital tutor, module structure, certification/practice, mentor/advisor, and overview-graphic action evidence. They should not be treated as a full quality score. They are enough to falsify plain hierarchical merging as the default for timeline/action items on this fixture.

Private local inspection matched the metrics:

- The hierarchy produced compact outputs that looked superficially usable but had over-compressed away the specific probe facts.
- Evidence-first timeline/action products preserved far more source-addressed atomic records and retained several probe facts.
- Evidence-first also exposed the next required production boundary: valid evidence can exceed a human-readable final product size, so Mena needs an explicit final-capacity policy instead of dumping every candidate into the note package.
- Neither prototype is production-ready for general-summary quality; the summary path needs source-aware bounded reduction, not plain recursive prose compression.

### Session 2 source-aware prototype: controlled operational failure

The later commit `2c5fcac` attempted the #199 Candidate C source-aware
hierarchy. It retained private request and response artifacts locally and did
not publish their content. The sanitized operational facts recoverable from
the call index and run log are:

| Result | Value |
|---|---:|
| Source chunks | 28 |
| Completed model calls | 368 |
| Elapsed model-call time | 15,669.745s (about 4h21m) |
| Prompt evaluation tokens | 475,092 |
| Generated tokens | 183,608 |
| Maximum preflight prompt bytes | 6,417 |
| Terminal result | `non_contracting_reduction` |
| Complete three-product package | no |

Timeline and action-item intermediate/final artifacts were produced privately,
and general-summary extraction completed, but general-summary consolidation
failed its contraction check before package assembly. Candidate D was not run.
The valid conclusion is operational rather than qualitative: a graph can keep
every individual request within context while remaining unusably expensive,
and a per-call contraction check does not replace a predeclared whole-run call
graph, wall-time envelope, checkpoints, and resumability.

## Architectural Implications

### 1. Keep note products independent

OG accepted sequential execution; parallelism is not the architectural requirement. The requirement is that timeline, action items, and general summary remain independently generated products assembled into one deterministic atomic package. A failed or partial product must be visible as product status, not hidden by a fluent package-level summary.

### 2. Enforce bounded calls by construction

Every model call must be packed against the effective provider budget before dispatch:

```text
fixed instructions + serialized evidence/intermediates + reserved output + margin <= context budget
```

The prototype proved this is feasible for the fixture: every Llama call fit the preflight budget. The earlier RC7 failure still matters because it showed the current #113-style single reduce fan-in can exceed the budget even after chunk mapping.

### 3. Define product-level evidence capacity

Evidence-first aggregation moved the bottleneck from raw transcript size to valid evidence count. That is the right bottleneck, but it still needs a declared policy:

- include all candidates only when they fit the product contract;
- otherwise produce paginated/sectioned output, a deterministic partial result with included/omitted counts and rule, or a controlled `evidence_exceeds_product_capacity` failure;
- never silently truncate or let the model choose what disappears.

### 4. Use source-aware summary reduction

General summary can tolerate salience compression, so a tree is appropriate there. But the source review and this benchmark both argue against pure summary-of-summaries. Merge stages should carry source ids, coverage metadata, and optionally selected raw/evidence context so that later levels can recover support instead of amplifying earlier omissions.

## Hypothesis carried into #207

Prototype and evaluate a bounded generation substrate plus product-specific
strategy modules before proposing production work for #114:

- **Provider budget service:** effective context, tokenizer/request measurement, output reservation, safety margin, and normalized `provider_budget_mismatch`.
- **Source chunker:** stable source-unit ids, transcript/manual-note references, and no source-unit splitting that destroys citation addressability.
- **Timeline strategy:** extract typed event candidates; validate source ids/timestamps; stable-sort by source-derived chronology; group duplicates; preserve conflicts; apply final-capacity policy.
- **Action-item strategy:** extract typed task candidates; validate owner/due/status/source fields; group duplicates by actor/action/time evidence; preserve unresolved alternatives; apply final-capacity policy.
- **General-summary strategy:** map chunk summaries with source coverage; recursively reduce bounded batches; require contraction or fail with `non_contracting_reduction`; rehydrate selected evidence/source context at merge levels.
- **Package assembler:** assemble product outputs atomically with per-product status, provenance, strategy metadata, prompt-budget evidence, and explicit incompleteness/failure states.

Minimum observable failure modes should include `source_unit_exceeds_budget`, `intermediate_contract_violation`, `non_contracting_reduction`, `evidence_exceeds_product_capacity`, `partial_by_policy`, and `provider_budget_mismatch`.

## Evidence Base

This report depends on the Phase 1 source review in [`r-bounded-long-input-strategies-source-review.md`](r-bounded-long-input-strategies-source-review.md), especially:

- Summ^N's staged summarization claim for long inputs, with the caveat that scalable context-window handling is not the same as guaranteed evidence retention ([Zhang et al., ACL 2022](https://aclanthology.org/2022.acl-long.112/)).
- OpenAI's book-summarization decomposition, which supports recursive work decomposition but not Mena's provenance contract by itself ([OpenAI, 2021](https://openai.com/index/summarizing-books/)).
- LlamaIndex `tree_summarize`, `refine`, `compact`, and truncating `simple_summarize` response modes as framework examples of common orchestration semantics and their risks ([LlamaIndex response modes](https://developers.llamaindex.ai/python/framework/module_guides/deploying/query_engine/response_modes/)).
- LangChain `ReduceDocumentsChain` controls such as `token_max` and `collapse_max_retries`, including the risk of unbounded collapse retry loops ([LangChain API reference](https://python.langchain.com/api_reference/langchain/chains/langchain.chains.combine_documents.reduce.ReduceDocumentsChain.html)).
- Context-aware hierarchical merging evidence that source augmentation can outperform pure hierarchical merging ([Ou and Lapata, 2025](https://arxiv.org/abs/2502.00977)).
- HERA's separation of segmentation, event-related evidence packaging, ordering, and generation ([Li et al., 2025](https://arxiv.org/abs/2502.00448)).
- Extract-then-abstract precedent from Chen and Bansal ([ACL 2018](https://aclanthology.org/P18-1063/)).
- Atomic fact and citation evaluation results showing why citations and fact support must be checked structurally rather than requested as prose style ([FActScore](https://aclanthology.org/2023.emnlp-main.741/), [ALCE](https://aclanthology.org/2023.emnlp-main.398/)).
- Long-context position sensitivity from "Lost in the Middle", which cautions against order-sensitive rolling refinement as a canonical evidence accumulator ([TACL 2024](https://doi.org/10.1162/tacl_a_00638)).
- OpenAI response-length guidance distinguishing output caps from input/context packing ([OpenAI help](https://help.openai.com/en/articles/5072518-controlling-the-length-of-openai-model-responses)).

## Non-goals

- This report does not choose UI presentation for oversized evidence products.
- This report does not promote the throwaway prototype into production code.
- This report does not select a winning #199 candidate or unblock #114.
- This report does not publish private meeting content or generated private note products.
- This report does not authorize a GitHub PR, issue creation, or push.
