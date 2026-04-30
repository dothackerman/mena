# R-05 Diarization Provider Strategy

Status: Ready

## Purpose

Recommend the diarization approach for the current PoC architecture, taking into account that `mena` supports both dual-stream online capture and single-mic in-person capture.

This task is designed for a deep research agent with no prior product context.

## Product Context

`mena` is a privacy-first local meeting notes product.

The product records meetings locally, then processes them into structured notes.
The current PoC includes:

- `online_dual_stream`: local user mic plus remote/system audio
- `in_person_single_mic`: one room microphone containing all speakers

The product uses diarization to improve transcript usefulness and note quality, but the architecture does not assume diarization will always be perfect.
Confidence flagging and human correction are part of the design.

## Fixed Constraints

Treat the following as fixed:

- audio must not leave the machine
- diarization must therefore be local-only
- recording robustness outranks diarization sophistication
- diarization may be optional per meeting in some workflows
- raw audio is preserved so that transcripts and related artifacts can be regenerated
- correction overlays are part of the intended user workflow

## Current Architecture Baseline

Relevant baseline assumptions:

- in `online_dual_stream`, local user speech may be structurally easier to attribute because the local microphone is separate
- remote participants and `in_person_single_mic` are more likely to produce ambiguity, overlap, and confidence failures
- architecture must not hide diarization uncertainty; it should expose confidence and support correction
- later speaker-learning ideas are explicitly out of current PoC scope

## Research Question

Which diarization approach best fits the current PoC architecture, especially given the difference between `online_dual_stream` and `in_person_single_mic`?

## Decision To Unblock

This research should unblock:

- the PoC diarization strategy
- architecture expectations around scenario-specific quality limits
- the design assumptions for correction, overlap handling, and confidence surfacing

## Evaluation Criteria

Evaluate primarily in this order:

1. local-only operability
2. diarization usefulness under the two current capture profiles
3. quality under overlap, poor microphones, and room noise
4. ability to expose confidence and uncertainty clearly
5. hardware/runtime burden
6. ecosystem stability and maintainability
7. fit with correction-assisted workflow
8. future extensibility without overcommitting the current PoC

## Required Questions To Answer

Answer at least the following:

- what diarization approach is best for the current PoC?
- how does the recommendation behave differently for `online_dual_stream` versus `in_person_single_mic`?
- what failure modes should the architecture expect as normal rather than exceptional?
- where should the product rely on correction flow instead of pretending the model can solve the problem fully?
- what are the runtime and packaging implications?
- what is the strongest alternative and why did it lose?

## Out Of Scope

Do not spend time on:

- cross-session speaker learning
- long-term identity graphs
- remote diarization
- telemetry design

## Expected Output

The final research note should include:

- a recommended diarization strategy
- scenario-specific expectations and caveats
- major failure modes
- implications for correction workflow and artifacts
- implications for hardware/runtime burden
- suggested validation steps with realistic meeting conditions

## References

Primary references:

- `docs/architecture/architecture-overview.md`
- `docs/requirements/poc-requirements.md`
