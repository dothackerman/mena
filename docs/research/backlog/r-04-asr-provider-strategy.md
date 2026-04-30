# R-04 ASR Provider Strategy

Status: Ready

## Purpose

Recommend the ASR approach for the current PoC architecture under the product's local-only and reliability constraints.

This brief is written for a deep research agent starting from zero.

## Product Context

`mena` is a privacy-first local meeting notes product.

The current PoC:

- records meeting audio locally
- transcribes locally
- diarizes locally
- supports correction of uncertain segments
- produces structured meeting notes

The product is CLI-first and Linux-first for the PoC, but the CLI is not the architectural center.
Recorded meetings are processed after capture, and the current PoC does not require strict low-latency post-processing.

## Fixed Constraints

Treat the following as fixed:

- audio must not leave the machine
- ASR must therefore be local-only
- recording robustness outranks ASR elegance
- transcripts are durable artifacts and may be regenerated from source audio
- human correction is an intended part of the workflow
- weaker representative tester machines matter, not only the first target laptop
- architecture uses provider ports rather than direct workflow coupling to one engine

## Current Architecture Baseline

Relevant baseline assumptions:

- raw audio is canonical
- correction overlays preserve user edits separately from generated transcript output
- semantic coherence checks may exist later, but they do not replace ASR quality
- processing latency matters less than correctness, local operability, and testability

This task exists because the architecture needs a practical PoC ASR recommendation rather than a generic speech-model survey.

## Research Question

Which ASR approach best satisfies the current PoC architecture priorities for local meeting transcription, and what tradeoffs does it impose on quality, runtime burden, and maintainability?

## Decision To Unblock

This research should unblock:

- the PoC ASR strategy
- the shortlist of realistic fallback local ASR approaches
- the architecture assumptions around quality risk and runtime burden

## Evaluation Criteria

Evaluate primarily in this order:

1. local-only operability
2. transcription quality for real meeting audio
3. Linux operability and automation friendliness
4. hardware burden across stronger and weaker tester machines
5. maintainability and ecosystem stability
6. compatibility with replay/testing and correction workflows
7. language and lexicon support
8. performance for post-meeting processing

## Required Questions To Answer

Answer at least the following:

- what ASR approach is best for the current PoC?
- what is the strongest alternative and why did it lose?
- what assumptions does the recommendation make about GPU, CPU fallback, drivers, or packaging?
- how likely is the approach to remain maintainable over the near-term PoC horizon?
- how well does it support user-supplied lexicon or domain vocabulary adaptation?
- what quality risks should architecture absorb through correction, replay, or testing rather than trying to solve in model choice alone?

## Out Of Scope

Do not spend time on:

- remote ASR providers
- diarization selection except where necessary to explain ASR integration consequences
- final packaging strategy
- broad speech AI market mapping unrelated to the PoC decision

## Expected Output

The final research note should include:

- a clear recommendation
- structured comparison against realistic alternatives
- quality and runtime tradeoffs
- major operational risks
- implications for provider abstraction and testing
- suggested validation steps on representative hardware

## References

Primary references:

- `docs/architecture/architecture-overview.md`
- `docs/requirements/poc-requirements.md`
