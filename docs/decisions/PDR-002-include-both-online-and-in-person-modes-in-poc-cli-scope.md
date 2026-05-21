# PDR-002: Include both online meeting and in-person single-mic modes in PoC CLI scope

- **Type:** Product
- **Status:** Accepted
- **Date:** 2026-05-21
- **Superseded by:** —
- **Related:** #55, #56, PR #60, ADR-003, ADR-016 (superseded), ADR-023

## Context

The PoC CLI must demonstrate that mena works for the two primary meeting scenarios users encounter: online meetings (dual audio stream) and in-person meetings (single microphone). Originally, scope documents treated in-person capture as a post-PoC concern. During CLI PoC planning, we determined that both scenarios should be reachable within the PoC to validate the user experience end-to-end and to ensure the product does not accidentally exclude in-person use.

ADR-016 originally captured this as an architecture concern. On review, the actual decision is product scope: what the user can do with the CLI PoC. The architectural implication is narrower — implementation must stay capture-profile-driven rather than hardcoding online/two-source processing.

## Decision

**The PoC CLI scope includes both `online_dual_stream` and `in_person_single_mic` meeting modes.**

- Build order may still be online first, then in-person.
- Both modes are first-class within PoC scope; neither is deferred to post-PoC.
- The in-person single-mic mode supersedes the in-person clause of [POST-POC B] in `docs/requirements/poc-requirements.md` v1.0 and [SCOPE-OUT 6] from the same document.

## Consequences

**Positive:**
- Users can evaluate mena for both common meeting types during the PoC period.
- The product signal is stronger: "works for my meetings" rather than "works for Zoom calls only."
- Capture-profile abstraction (ADR-003) is validated against two real scenarios, reducing risk of a late-breaking in-person redesign.

**Negative:**
- Slightly more CLI surface to design and test (scenario selection, capture profile switching).
- In-person mode introduces ambient noise, speaker count uncertainty, and microphone placement variability — harder to deliver polished output than a clean dual stream.

**Out of scope (Architect):**
- Merge timing of the two modes into the codebase.
- Whether in-person mode ships in the same binary or requires a separate invocation path.
- Specific capture adapter implementation for single-mic diarisation (ADR-023 covers scenario-aware invocation).

**Related architecture note:**
- ADR-016 is superseded by this PDR. The architectural requirement — "keep it capture-profile-driven" — is preserved as an implementation constraint, not a product decision.
