# ADR-027: UX telemetry absent in PoC v0

- **Type:** Architecture
- **Status:** Deferred
- **Date:** 2026-05-01
- **Superseded by:** —
- **Related:** ADR-014

## Context

AD-014 deferred telemetry; this decision confirms that deferral for the entire PoC horizon and documents the architectural red lines for any future telemetry.

## Decision

UX telemetry remains **absent in PoC v0** (confirms ADR-014 for the PoC horizon). When introduced later, the only architecturally compatible model is local-only short-retention storage with manual user export, no persistent identifiers, closed-vocabulary metrics, and the documented red-line list (no meeting substance, no participant identity, no free-form strings, no crash dumps by default, no session replay, no pseudonymous stable IDs).

## Consequences

Resolves R-09. Heavy machinery (LDP/OHTTP/DAP) is not adopted for the PoC; if true remote telemetry is ever introduced it becomes a separate architecture decision.
