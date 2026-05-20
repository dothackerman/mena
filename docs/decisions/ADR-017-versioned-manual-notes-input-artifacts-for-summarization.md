# ADR-017: Versioned manual-notes input artifacts for summarization

- **Type:** Architecture
- **Status:** Accepted
- **Date:** 2026-04-20
- **Superseded by:** —
- **Related:** —

## Context

If manual notes are edited in-place, the summarizer cannot distinguish between note versions and may produce inconsistent summaries.

## Decision

Use versioned manual-notes input artifacts for summarization.

## Consequences

Edited notes produce new notes artifact versions and new summary candidates instead of silently mutating prior summaries.
