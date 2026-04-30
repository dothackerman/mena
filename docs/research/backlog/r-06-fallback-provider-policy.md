# R-06 Fallback Provider Policy

Status: Open

## Question

- is direct Mistral API acceptable as the PoC summarization fallback under sovereignty constraints, and what policy limits apply?

## Current PoC Fallback Candidate

- Mistral models through direct Mistral API access for summarization only

## Critical Distinction

- model family, direct provider, and brokered access path are separate policy concerns
- brokered access paths such as OpenRouter are deferred to ideas and must not be treated as equivalent to direct Mistral API access

## Why It Matters

- affects policy enforcement
- affects tester operability on weaker machines
- affects what the user may or may not configure directly

