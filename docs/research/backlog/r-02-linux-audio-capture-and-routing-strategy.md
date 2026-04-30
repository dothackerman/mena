# R-02 Linux Audio Capture and Routing Strategy

Status: Ready

## Purpose

Determine the most reliable Linux approach for local meeting audio capture and controlled capture-source routing for the current PoC.

This task is optimized for a deep research agent that knows nothing about `mena` beyond what is stated here.

## Product Context

`mena` is a privacy-first local meeting notes product.

The current PoC architecture supports two capture profiles:

- `online_dual_stream`: capture the local user's microphone and the meeting platform's output audio as separate streams
- `in_person_single_mic`: capture one room microphone containing all speakers

The product flow is:

- record meeting audio locally
- preserve recording safely as the canonical source artifact
- transcribe locally
- diarize locally
- support human correction
- generate structured meeting notes

The first implementation target is Linux and the first UI is a CLI, but the architecture must avoid baking Linux-specific behavior into the core pipeline.

## Fixed Constraints

Treat the following as fixed:

- recording robustness is the highest product priority
- downstream failures must not corrupt or lose already captured audio
- bounded data loss is required on crash or failure
- audio must not leave the machine
- OS-specific capture behavior belongs behind adapters
- the current build order is `online_dual_stream` first, `in_person_single_mic` second
- the architecture currently assumes one generic capture port with capture profiles unless evidence proves that assumption wrong

Do not optimize for a fancy user experience over reliability and debuggability.

## Current Architecture Baseline

Relevant baseline assumptions:

- a `Session Orchestrator Process` coordinates the session
- a `Capture Worker Process` owns real-time audio capture
- files hold durable artifacts
- internal protocol is only for live coordination
- raw audio is sealed and treated as the canonical source artifact

This research is meant to inform the Linux capture adapter design and the realism of the current capture-profile assumption.

## Research Question

What Linux audio capture and routing approach best supports reliable `online_dual_stream` capture, and what is the true incremental complexity of adding `in_person_single_mic` afterward?

## Decision To Unblock

This research should unblock:

- the PoC Linux capture/routing strategy
- the initial capture adapter boundary
- the decision whether both current capture profiles should remain under one capture abstraction

## Evaluation Criteria

Evaluate options primarily in this order:

1. recording robustness under device churn, user error, and process failure
2. ability to capture local mic and remote/system audio as distinct streams when needed
3. operational clarity for a CLI-driven product
4. debuggability and observability on Linux
5. compatibility with common Linux audio stacks and realistic desktop setups
6. implementation complexity
7. portability impact on future non-Linux work
8. support burden for testers

## Required Questions To Answer

Answer at least the following:

- what Linux audio stack assumptions are safe to make for the PoC?
- what concrete capture/routing pattern is most reliable for `online_dual_stream`?
- what fallback behavior should exist when perfect routing is unavailable?
- which failure modes matter most in practice?
- how much extra implementation and runtime complexity does `in_person_single_mic` actually introduce?
- does one generic capture port with capture profiles still look like the right architectural move after researching the Linux reality?
- what testing and fixture strategy should architecture anticipate for this area?

## Comparison Scope

Consider the relevant Linux audio layers and operational patterns, for example:

- PipeWire-centered setups
- PulseAudio compatibility layers where relevant
- ALSA-level considerations where they materially affect the recommendation
- system audio monitor capture approaches
- device selection and routing strategies
- session-scoped versus system-scoped routing choices

You do not need to exhaustively catalogue every tool in the Linux audio ecosystem.
Focus on decision-quality comparison for this product.

## Out Of Scope

Do not spend time on:

- final macOS or Windows design
- live transcript UX
- meeting platform API integrations
- unrelated audio production workflows

## Expected Output

The final research note should include:

- the recommended Linux capture/routing approach
- the second-best alternative
- the main failure modes and mitigations
- implications for the capture worker boundary
- implications for replay/testing
- a clear assessment of whether `in_person_single_mic` is merely incremental or materially different

## References

Primary references:

- `docs/architecture/architecture-overview.md`
- `docs/requirements/poc-requirements.md`

This task should remain useful even if the agent does not read those files.
