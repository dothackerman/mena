# ADR-016: Include both online_dual_stream and in_person_single_mic in CLI PoC scope

- **Type:** Architecture
- **Status:** Accepted
- **Date:** 2026-04-20
- **Superseded by:** —
- **Related:** ADR-003, ADR-024

## Context

Both capture scenarios validate the capture profile abstraction and ensure the architecture does not accidentally exclude in-person use.

## Decision

Include both `online_dual_stream` and `in_person_single_mic` in CLI PoC scope.

## Consequences

Build order is online first, in-person second; both use capture profiles rather than separate pipelines. Supersedes [SCOPE-OUT 6] and the in-person clause of [POST-POC B] in `docs/requirements/poc-requirements.md` v1.0.
