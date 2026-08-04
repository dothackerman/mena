# Bounded, source-grounded meeting summarization for a local 8B model

Date: 2026-08-04

Scope: established algorithmic patterns that can turn a transcript longer than the model context into a readable meeting-note package while guaranteeing bounded model calls. This note uses primary research sources only. It does not inspect private meeting content. Statements labelled **recommendation** are architectural deductions for Mena, not claims made by the cited authors.

## Short answer

Mena should not replace its current one-level `map -> concatenate everything -> reduce` with an unconstrained recursive summarizer. It should introduce a **bounded evidence graph**:

1. extract small, typed, source-addressed evidence records from bounded transcript windows;
2. deterministically partition those records by product semantics (time for timeline, commitment identity for actions, topic for narrative);
3. recursively consolidate only fixed-size batches, preserving source references and enforcing contraction at every level;
4. for narrative prose, build a bounded content plan and re-hydrate it with selected original transcript spans before writing;
5. assemble structured products deterministically, using the model only where semantic fusion or prose is needed.

This combines four supported patterns: fixed-context multi-stage summarization, explicit content/budget separation, source-aware hierarchical merging, and plan-conditioned generation. It makes completion mechanically predictable; quality on Mena's local model still has to be established experimentally.

## What research supports

### 1. Fixed-context hierarchy solves the context-length problem, not automatically the fidelity problem

SummN splits long inputs, summarizes them through as many stages as necessary, and keeps the language-model input size fixed. The authors report improvements on AMI, ICSI, and QMSum meeting datasets as well as long-document datasets. This is direct evidence that multi-stage processing is a legitimate meeting-summarization pattern, rather than a Mena-specific workaround. ([Zhang et al., ACL 2022](https://aclanthology.org/2022.acl-long.112/))

However, context-aware hierarchical merging research finds that recursive merging can amplify hallucinations. It improves the hierarchy by bringing relevant original input back into later stages: replacing intermediate summaries with source context, refining summaries against supporting source evidence, or aligning summaries to the input through citations. Its analysis reports that refinement works especially well when extractive selection identifies relevant input. ([Ou and Lapata, Findings of ACL 2025](https://aclanthology.org/2025.findings-acl.289/))

**Recommendation:** use hierarchy as the capacity mechanism, but never treat an intermediate abstraction as sufficient evidence for a final claim. Preserve source segment IDs and selectively reintroduce original spans during semantic consolidation and final prose generation.

### 2. Separate content selection from output budget

FactorSum separates (a) generation of partial views over subsets of a long document from (b) combining selected views under explicit content guidance and a final length budget. The reported gains are attributed partly to flexible budget adaptation and shorter-context partial views. ([Fonseca, Ziser, and Cohen, EMNLP 2022](https://aclanthology.org/2022.emnlp-main.426/))

Extract-then-abstract is also an established topic-focused pattern: Deutsch and Roth evaluate a two-step system that first selects a small set of relevant sentences, then summarizes those selected sentences. ([Deutsch and Roth, EMNLP-IJCNLP 2019](https://aclanthology.org/D19-1386/))

**Recommendation:** do not ask each chunk to write a miniature final report. Ask it for capped evidence records. Decide later which records merit the scarce final-output budget. This prevents verbose map outputs from defining the reducer's workload.

### 3. Atomic evidence is a better internal currency than prose summaries

FActScore evaluates long-form generation by decomposing it into atomic facts and testing whether each fact is supported by a reliable source. Although it is an evaluation method rather than a meeting summarizer, it supplies the useful unit: one independently checkable claim. ([Min et al., EMNLP 2023](https://aclanthology.org/2023.emnlp-main.741/)) FactCC likewise jointly predicts factual consistency and extracts supporting source spans, and its human evaluation found those spans useful for verification. ([Kryscinski et al., EMNLP 2020](https://aclanthology.org/2020.emnlp-main.750/))

Sub-sentential extraction research also finds that full sentences introduce unnecessary and redundant material, while smaller units can be competitive under automatic and human evaluation. ([Zhou, Wei, and Zhou, COLING 2020](https://aclanthology.org/2020.coling-main.492/))

**Recommendation:** define a compact record such as `{kind, claim, actor?, status?, due?, start_segment, end_segment, confidence}`. Require exactly one claim per record, verbatim source addresses, hard field lengths, a hard records-per-window cap, and an explicit `omitted_due_to_budget` signal. The schema is Mena's design; the cited work supports atomic claims and supporting spans as grounding units.

### 4. Retrieve and re-hydrate rather than carrying all source text upward

RAPTOR recursively embeds, clusters, and summarizes text into a tree, then retrieves information across abstraction levels at inference time. Its controlled experiments show benefits over retrieving only short contiguous chunks for questions requiring holistic understanding. ([Sarthi et al., ICLR 2024](https://openreview.net/forum?id=GN921JHCRw))

**Recommendation:** Mena need not implement general-purpose RAG. It can use the narrower idea deterministically: cluster or group evidence first, preserve every group's source-span references, then retrieve only the highest-value original spans for that group when consolidating or writing. This gives a later call both global direction (the group/plan) and local evidence (original spans) within a fixed context.

### 5. Plan before writing the narrative

Entity-plan research generates an ordered content plan before the summary and finds improvements in entity specificity and faithfulness, including on the SAMSum dialogue dataset. Removing hallucinated entities from the plan further improves faithfulness. ([Narayan et al., TACL 2021](https://aclanthology.org/2021.tacl-1.88/)) More recent highlight-guided work uses selected source sentences as a content plan; its two-stage variant performs better on long, information-dense documents and improves factual consistency while preserving relevance. ([Du et al., IJCNLP-AACL 2025](https://aclanthology.org/2025.ijcnlp-short.27/))

**Recommendation:** general-summary generation should be `bounded topic plan -> evidence retrieval per planned section -> bounded section drafting -> deterministic section assembly`, not repeated free-form compression of accumulated prose.

## Proposed bounded algorithm

Let `B` be the maximum prompt-token budget after system instructions and output reserve. Let `R` be the maximum tokens in one evidence record, `M` the maximum records emitted per extraction window, `K >= 2` the maximum records or child nodes accepted by a consolidation call, and `Q` the maximum output tokens of one consolidated node.

### Mechanical invariants

- Token-count every fully rendered request before dispatch; no request may exceed `B`.
- Reject any record exceeding `R`, or apply an explicitly represented selection policy that records what was omitted and why; accept at most `M` records per window. Never truncate record content invisibly.
- Pack consolidation inputs by rendered token count as well as count; every group must contain at least two nodes unless it is carried forward unchanged.
- A consolidation of two or more nodes must emit exactly one node no larger than `Q`, with the union of retained source references and an explicit loss/omission field.
- Each level therefore reduces node count from `n` to at most `ceil(n / K)` when groups are full. A hard fallback pairwise grouping guarantees progress, and the run terminates in at most `ceil(log2 n)` consolidation levels.
- Set run-level ceilings for extraction calls, consolidation calls, drafting calls, depth, retries, and wall time before execution. Fail with a capacity report rather than silently dropping input.
- Validate structured output locally. A malformed or non-contracting response gets one repair attempt; after that, fall back to deterministic carry-forward or mark the affected product incomplete.

These bounds are an architectural construction based on the fixed-context hierarchy in SummN; the exact constants must be calibrated on Mena's hardware and model.

### Product-specific projections

#### Timeline

Extract atomic event/decision/change records with timestamp ranges and source IDs. Sort by source time locally. Coalesce adjacent records in bounded chronological batches; retain multiple records when they express distinct events. Deduplicate first by overlapping source ranges and normalized claim similarity, using the model only for ambiguous semantic merges. Assemble Markdown chronologically without a final all-events model call.

Why: chronology already provides the partitioning key. A global prose reducer wastes context and can reorder facts.

#### Action items

Extract only explicit commitment evidence into typed records: action, owner if stated, due date if stated, status, and source IDs. Deterministically partition by normalized owner/action tokens, then use bounded semantic consolidation inside candidate duplicate groups. Preserve conflicting owners/dates as conflicts instead of asking the model to guess. Render the final table/list deterministically.

Why: action-item detection is an information-extraction problem before it is a summarization problem; early work specifically formulates detection of action-item regions in meeting recordings. ([Morgan et al., SIGdial 2006](https://aclanthology.org/W06-1314/))

#### General summary

Extract topic-tagged atomic evidence. Create bounded topic clusters using embeddings or inexpensive lexical/time adjacency, then build a capped ordered content plan from cluster representatives. For each planned section, select a fixed evidence budget plus original source spans, draft the section independently, and assemble sections locally. Optionally run a final style pass only if the assembled text itself fits `B`; correctness must not depend on that pass.

Why: this combines FactorSum's content/budget separation, source-aware merging, retrieval across abstraction levels, and plan-conditioned generation.

## Experiment sequence

1. **Predict the run before spending model calls.** Build the private replay harness, declare the per-stage call graph and wall-time envelope, and checkpoint every completed stage. Refuse a run whose dry plan can exceed its declared limits.
2. **Produce the first user-visible baseline on the ordinary failing session.** Acceptance: every call fits, the graph terminates, all three products export, and budgets/call counts are reported. The longest private fixture is a later stress input rather than a blocker to seeing this first export.
3. **Stop for product-owner feedback.** Review the normal Markdown as a user before opening the transcript/evidence audit view. Record what is wrong, missing, noisy, useful, or too burdensome; do not begin autonomous quality tuning before this feedback determines the next hypothesis.
4. **Validate and freeze extraction before aggregation comparisons.** Extraction recall is irrecoverable, so compare any material extraction choices—especially shared versus product-specific extraction—before treating one evidence inventory as fixed experimental input.
5. **Ablate aggregation on the same frozen extraction records:** (A) plain hierarchy as a negative/control baseline, (B) the same hierarchy with source-span re-hydration, and (C) the same source-grounded inputs with a bounded content plan. Keep grouping, output envelopes, runtime options, and deterministic assembly constant wherever the tested variable permits.
6. **Perform independent human review on user-visible output:** unsupported claim count, missed critical item count, duplicate count, incorrect owner/date count, chronology errors, usefulness/readability, total runtime, and calls. Automatic overlap scores are secondary because Mena has no gold reference for real sessions.
7. **Stress the selected baseline on the longest private fixture.** Completion or controlled failure must stay inside a predeclared capacity envelope; the result determines whether #114 can resume and under which supported session envelope.
8. **Model comparison last.** Once identical bounded requests reach the model, compare the current 8B model against alternatives. Until then, model and algorithm effects are confounded.

## Research recommendation for #207

Prototype a source-aware bounded evidence graph with deterministic product-specific assembly; this recommendation does not select a production architecture or amend ADR-034/ADR-035. Build the smallest experimental vertical slice in this order: timeline, action items, then planned general summary. Timeline provides the easiest capacity proof; action items test typed extraction and conflict preservation; the general summary is the only product that needs substantial prose generation.

Do not ship naive recursive prose summarization as the fix. It guarantees fit only if output caps are enforced, while research shows repeated abstraction can amplify factual errors. The acceptance bar is not merely "no context exception"; it is a readable export whose claims remain traceable to original transcript spans within a fixed run budget.
