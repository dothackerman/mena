# R-08 Internal Protocol Implementation Strategy

Status: Open

## Question

- which concrete transport and encoding approach best fits the private internal protocol?

## Current Candidates

- `stdio`
- local socket
- JSON-based schema messages
- stronger schema tooling later if justified

## Why It Matters

- affects process supervision
- affects contract testing
- affects implementation complexity

