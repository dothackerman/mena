# PDR-001: Automatic correction overlay selection

- **Type:** Product
- **Status:** Accepted
- **Date:** 2026-05-20
- **Superseded by:** —
- **Related:** #14, #13, PR #52, ADR-010 (provider ports), ADR-023 (diarisation scenario-aware)

## Context

PR #52 implements correction overlays as a first-class artifact type. The system now supports multiple correction overlay artifacts per session — a user can rerun correction and produce a new overlay each time.

Downstream stages (summarization, export) need a single correction overlay to work from. The question is: should selecting the active overlay be a user-facing decision, or should the product apply a deterministic rule?

Multiple correction runs are expected during PoC as a testing and iteration artifact. The PoC has a single user. There are no acceptance criteria requiring historical correction access or comparison between correction runs.

## Decision

**For PoC: the latest completed correction overlay is the active overlay.** Downstream processing (summarization, export, integrity verification) reads the correction overlay with the most recent `completed_at` timestamp.

No user-facing selection at PoC scope. If a user reruns correction, the new version replaces the old one for all downstream stages.

## Consequences

**Positive:**
- Deterministic behavior — no unpredictable state in the pipeline
- Matches user mental model ("I'm re-correcting because I found mistakes — use the new version")
- No additional UI surface for a scenario that rarely occurs in practice
- Simpler implementation — no selection state to manage

**Negative:**
- Previous correction overlays are not accessible to downstream stages once a newer one exists
- A user cannot compare summaries generated from different correction passes
- If the user accidentally completes a worse correction, the previous "good" one is no longer active downstream (though the artifact still exists in the session bundle)

**Out of scope for PoC:**
- Cross-session correction history
- User-facing correction comparison
- Correction rollback UI

**Future consideration:**
- If user selection between correction runs becomes a product requirement, this PDR should be superseded by a new one that defines the selection UX.
