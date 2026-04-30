# R-08 Internal Protocol Implementation Strategy

Status: Ready

## Purpose

Choose the concrete transport and encoding approach for the private internal protocol between orchestrator and worker processes in the current PoC architecture.

This task is for a deep research agent starting from zero.

## Product Context

`mena` is a privacy-first local meeting notes product.

The current architecture includes at least:

- a `Session Orchestrator Process`
- a `Capture Worker Process`

The orchestrator manages session lifecycle, policy, workflow, and canonical session record updates while it holds session authority.
The capture worker owns real-time audio capture and emits facts, events, and artifact references.

The product is local and not a distributed system.
This protocol is private, local, and internal to the application.

## Fixed Constraints

Treat the following as fixed:

- files are for durable artifacts
- the protocol is for live coordination only
- raw audio should not be transported through the protocol
- protocol and durable schemas must be versioned from day one
- the design should stay small and testable
- process separation exists for clear reasons such as capture ownership and failure isolation, not because the product wants a service mesh

## Current Architecture Baseline

Current candidate directions include:

- `stdio`
- local sockets
- JSON-like message schemas
- stronger schema tooling later if justified

The architecture wants enough rigor for parallel agent implementation and contract testing, but not gratuitous complexity.

## Research Question

Which transport and message-shape strategy best fits the PoC's internal protocol, given the need for reliability, testability, observability, and small-system simplicity?

## Decision To Unblock

This research should unblock:

- the PoC protocol transport baseline
- the initial message encoding and versioning approach
- the line between what is durable file state and what is live protocol state

## Evaluation Criteria

Evaluate primarily in this order:

1. implementation simplicity appropriate to the current product
2. reliability and failure clarity
3. observability and local debugging ease
4. contract-test friendliness
5. compatibility with supervisor and worker lifecycle handling
6. evolvability without premature framework commitment

## Required Questions To Answer

Answer at least the following:

- what protocol transport should PoC v0 use?
- what encoding and framing strategy should it use?
- what minimal versioning contract should exist from day one?
- what should not go through the protocol?
- what testing posture should architecture adopt for protocol boundaries?
- what would justify moving to a stronger protocol framework later?

## Out Of Scope

Do not spend time on:

- multi-machine communication
- public APIs
- generic enterprise RPC framework comparison unless strongly justified by the actual problem

## Expected Output

The final research note should include:

- the recommended transport and encoding baseline
- the second-best alternative
- versioning guidance
- testing implications
- concrete triggers for future protocol evolution

## References

Primary references:

- `docs/architecture/architecture-overview.md`
- `docs/architecture/implementation-guardrails.md`
