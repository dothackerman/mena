# ADR-034: Three-product note package with per-product generation contracts

- **Type:** Architecture
- **Status:** Accepted
- **Date:** 2026-07-03
- **Superseded by:** —
- **Related:** #111, #112, #108, #107, #32, #14, ADR-010, ADR-021, ADR-033, PDR-003

## Context

The current summarization contract is monolithic: one LLM request fills one `summary.v1` artifact with four fields (timeline, important_points, action_items, general_summary), and export renders that artifact to Markdown.

Evidence from #107 and #108 showed this shape is a quality liability on local models:

- One combined large schema measurably degrades small-model content quality; constrained decoding (`format`) guarantees shape, never content.
- A long real session (#108) produced schema-valid but contextually wrong output — the artifact looked successful while the notes described the wrong meeting.
- The prompt-lab work found that separate, specialized passes per output kind recover substantially more of the real meeting content than one combined response.
- Timeline entries degraded into bare dates and fabricated calendar dates appeared, because timestamps were model-authored free text.

Separately, #32 asked whether timeline and general summary are redundant, and `important_points` showed no distinct user job next to a good timeline and summary in the rc5/rc7/UAT evidence.

## Decision

### Three note products, `important_points` removed

Structured notes are a **note package** of three **note products**, each with its own generation prompt and its own small output schema:

1. **Timestamped timeline** — chronological record of meeting events
2. **Action items** — actual tasks, with optional owner and due date
3. **General summary** — high-level takeaway for quick recall

`important_points` is removed from the generation and export contract. It had no distinct user job: chronology belongs to the timeline, tasks to action items, recall to the general summary. Reintroducing it requires a new product decision (PDR), not a schema tweak.

This resolves #32 as **keep and differentiate**: the timeline is the chronological, segment-grounded record; the general summary is the non-chronological takeaway. They share no format and no job.

### One note-package artifact per summarize run

A summarize run persists **one note-package artifact** containing the three product sections. Each product section carries its own request provenance (model, effective options such as `num_ctx`/`temperature`/`seed`, and response token counts per #108's request discipline).

Rejected alternatives: three independent per-product artifacts (creates a which-versions-belong-together resolution problem for export) and per-product artifacts plus a package manifest (most flexible, over-engineered for PoC). Consequence of the choice: a note product cannot be regenerated or accepted independently — a rerun produces a whole new package.

### Per-product port call; provider-owned execution strategy

The summarization port becomes **per-product**: the summarize workflow asks the bound provider to generate each note product as a separate call with a small per-product schema.

- The **workflow** owns package assembly, acceptance, and persistence.
- The **provider** owns *how* a product is generated behind that call. One-shot execution now (#112); chunked map/reduce later (#108) without changing the port or the workflow. Provider-owned strategy selection is the seam for one-shot vs reducer execution.
- Summarization remains a single route-bearing LLM text stage: the capability resolver binds **one provider per summarize run**, and all three product calls go to that provider. There is no per-product routing.

### Timestamped timeline is segment-grounded

A timeline entry cites the merged-transcript segment IDs it came from. The entry's timestamp is **derived by lookup** from the cited segments' `start_seconds`; it is never model-authored text. Fabricated timestamps become structurally impossible rather than a semantic check, per #108's structural-grounding preference.

Known risk: the #112 one-shot slice on `llama3.1:8b` must reliably echo segment IDs. If it cannot, the fallback is to revisit this section — not to silently ship model-authored timestamps under the same contract.

### Export is deterministic stitching

Export renders an accepted note package to Markdown by deterministic stitching of the three products. **Export never calls an LLM.** All LLM judgment happens at generation time and is persisted in the package.

Export renders only the note-package contract. Existing `summary.v1` artifacts remain on disk untouched (ADR-012), but exporting such a session requires re-running summarize; export fails loudly and clearly on a session that has only a `summary.v1` artifact. The `summary.v1` render path is retired rather than carried as a second renderer.

### Quality gates deferred

Runtime content-quality gates (small-talk dominance, manual-note coverage, task-vs-topic-label checks) are **deferred to a separate low-priority spike**. The package contract carries the grounding data (segment citations, per-request provenance) that future gates need, but no gate blocks acceptance in this slice.

## Consequences

- #112 implements the first slice: three one-shot product generations in the extracted `summarize_workflow.py` behind the interaction port, local baseline `llama3.1:8b`.
- The `important_points` field, its schema constraints, and its Markdown section disappear from the new contract; #32 can be closed with the keep-and-differentiate rationale once this ADR is accepted.
- Per-product schemas make regression tests and reuse checks per-product, while acceptance stays package-atomic.
- Old sessions need one re-summarize before they can export under the new contract; this is acceptable for the PoC user base.
- If the segment-citation requirement proves infeasible for the one-shot local baseline, the timeline-grounding section of this ADR must be explicitly revisited (new or amended decision), because the fabricated-timestamp defense depends on it.
