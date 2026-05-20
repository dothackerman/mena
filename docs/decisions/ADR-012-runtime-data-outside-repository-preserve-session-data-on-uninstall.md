# ADR-012: Runtime data outside repository, preserve session data on uninstall

- **Type:** Architecture
- **Status:** Accepted
- **Date:** 2026-04-20
- **Superseded by:** —
- **Related:** —

## Context

Mixing development checkout, installed application, and user-owned data creates conflicts during updates and risks data loss during uninstall.

## Decision

Keep runtime data outside the repository and preserve user session data on uninstall.

## Consequences

Development checkout, installed application, and user-owned data remain separate.
