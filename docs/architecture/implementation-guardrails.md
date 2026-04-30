# Implementation Guardrails

Status: active for PoC implementation planning

This document explains how implementation issues should interpret the architecture overview.
It prevents coding agents from treating every architectural concept as immediate application code.

## Core Principle

Preserve the architecture boundaries, but implement the smallest useful PoC slice behind each boundary.

In short:

- be lavish in the meta-system
- be ruthless in the application surface

## Meta-System: Invest Early

The project should over-invest in feedback loops and scaffolding because agent-driven implementation depends on fast, deterministic verification.

Expected early scaffolding includes:

- formatter and linter setup
- type checking where the chosen stack supports it
- local `check` or equivalent command that mirrors CI
- remote CI on pull requests
- unit tests
- integration tests
- contract tests at provider and protocol boundaries
- provider fakes for deterministic workflow tests
- replayable fixtures that do not contain private meeting content
- issue templates that require architecture and verification references
- documentation conventions for implementation-facing decisions

These are not considered PoC overengineering.
They make the codebase safer for coding agents and humans.

## Application Surface: Keep Minimal

The PoC should not implement unused application machinery just because the architecture names a concept.

Examples:

- Keep provider ports, but the first policy resolver may be a small stage-policy function.
- Keep protocol versioning and command/event discipline, but start with the minimum messages needed by the capture worker.
- Keep artifact lifecycle rules, but only implement states that are queried by real code.
- Keep process-separation criteria, but do not add extra worker processes without evidence.
- Keep deferred ideas in `docs/ideas/`, but do not implement them unless explicitly promoted.

Unused application surface creates maintenance cost and gives coding agents extra paths to implement, test, and accidentally break.

## Issue Authoring Rule

Every implementation issue should name the slice it wants, not the whole architecture concept.

An issue should include:

- relevant requirements
- relevant architecture sections
- relevant implementation guardrails
- relevant research notes or backlog items, if any
- explicit in-scope work
- explicit out-of-scope work
- verification expectations

Example:

```markdown
Architecture references:
- docs/architecture/architecture-overview.md §8.4 Provider Capability and Policy Model
- docs/architecture/implementation-guardrails.md §Application Surface: Keep Minimal

Implementation interpretation:
- Preserve the provider port boundary.
- For PoC v0, implement provider selection as a small stage-policy function.
- Do not build a generic multi-provider resolver subsystem yet.
```

## Promotion Rule

Deferred ideas, research findings, or architecture options become implementation scope only when one of the following happens:

- a user explicitly promotes the item
- an implementation issue names it as in scope
- a research outcome updates the architecture or implementation plan
- a failing test, spike, or real PoC run proves the extra surface is needed

If none of those is true, keep the implementation smaller.

## Drift Management

The architecture overview is a reference manual.
Its length is intentional, but it creates drift risk.

Before cutting implementation issues for a new area:

- cite the relevant architecture sections
- check for contradictions in nearby sections
- prefer updating the architecture once over repeating corrections across issues
- keep issues small enough that agents read only the cited sections

