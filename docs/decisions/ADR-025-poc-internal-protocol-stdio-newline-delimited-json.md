# ADR-025: PoC internal protocol: stdio + newline-delimited JSON

- **Type:** Architecture
- **Status:** Accepted
- **Date:** 2026-05-01
- **Superseded by:** —
- **Related:** ADR-007, ADR-011

## Context

The orchestrator and capture worker need a lightweight inter-process protocol. Heavyweight protocols add complexity without proportional benefit for a two-process PoC.

## Decision

PoC internal-protocol transport is **stdio with newline-delimited compact UTF-8 JSON**, one object per frame, with an explicit `hello` version-agreement message. `stderr` is diagnostics-only. Files carry durable/bulky data; the protocol carries references. No JSON-RPC, gRPC, or Protocol Buffers in v0.

## Consequences

Concrete realisation of §6.10 and §8.5 versioning rules. Liveness uses OS-level signals; heartbeats deferred until evidence of live-but-stuck workers. Resolves R-08.
