# R-09 Privacy-Preserving UX Telemetry Design

Status: Ready

## Purpose

Determine whether and how `mena` should model UX/product telemetry without violating its privacy-first architecture.

This task should produce a decision-quality architectural recommendation, not generic analytics advice.

## Product Context

`mena` is a privacy-first local meeting notes product.

The product records, transcribes, diarizes, and summarizes meeting content locally, with strong emphasis on sovereignty and user trust.
The current architecture intentionally defers telemetry because careless telemetry design could undermine the product's core promise even if the rest of the pipeline is technically local.

The current PoC is Linux-first and CLI-first, but the question here is broader than terminal usage metrics.
The question is whether there is any telemetry model compatible with the product's privacy posture and, if so, what its boundaries should be.

## Fixed Constraints

Treat the following as fixed:

- telemetry must not expose meeting substance
- telemetry must not expose participant identity
- telemetry must not silently create meaningful user identity tracking
- telemetry does not need to exist in PoC v0 unless there is a strong architecture case for it
- if telemetry exists later, it must remain clearly separate from canonical content artifacts

## Current Architecture Baseline

Relevant baseline assumptions:

- telemetry is intentionally deferred today
- the architecture is willing to leave telemetry absent rather than ship a privacy-ambiguous design
- any future telemetry should be explicit, bounded, and non-canonical

## Research Question

What telemetry model, if any, would produce useful UX or product insight for `mena` while remaining consistent with its privacy-first architecture?

## Decision To Unblock

This research should unblock:

- whether telemetry should exist at all in or after the current PoC
- if yes, what categories of data are acceptable
- what user-control and export posture would be required

## Evaluation Criteria

Evaluate primarily in this order:

1. privacy preservation
2. risk of accidental content leakage
3. usefulness for product improvement
4. explicit user control and legibility
5. local-first operability
6. architectural containment and non-canonical status

## Required Questions To Answer

Answer at least the following:

- is there a telemetry model worth having for this product, or is the right answer to keep telemetry absent?
- what event categories, aggregates, or health signals would be useful without carrying content?
- are any identifiers justified, and if so how weak or local should they be?
- should telemetry remain local-only, be exportable on explicit user action, or never exist at all?
- what red lines should architecture document up front?
- what future architecture changes would become necessary if telemetry were ever promoted from deferred to active?

## Out Of Scope

Do not spend time on:

- marketing analytics
- ad-tech style identity models
- cloud analytics integration by default
- implementation details of instrumentation code

## Expected Output

The final research note should include:

- a recommendation on whether telemetry should exist
- allowed and disallowed telemetry categories
- identifier guidance, if any
- user control and export posture
- reasons to keep telemetry absent if that is the best recommendation

## References

Primary references:

- `docs/architecture/architecture-overview.md`
- `docs/requirements/poc-requirements.md`
