# R-01 Local Model Runtime Strategy

Status: Ready

## Purpose

Determine how `mena` should access local model-backed stages on user machines during the current PoC architecture phase.

This task is intended for a deep research agent starting from zero product knowledge.
The task must therefore carry enough product and architecture context to support a high-quality recommendation without requiring the agent to reconstruct the whole repo.

## How To Read This Task

This is a bounded research brief, not an implementation issue.

The research output should:

- recommend a decision-quality runtime strategy for the current PoC architecture baseline
- explain tradeoffs against realistic alternatives
- surface assumptions and validation gaps clearly
- avoid turning open architecture questions into silent facts

The output should be written for later use by human and architecture agents and placed under `docs/research/notes/`.

## Product Context

`mena` is a privacy-first local meeting notes product.

The current product intent is:

- record meetings locally
- transcribe audio locally
- diarize audio locally
- support human correction of uncertain segments
- produce structured meeting notes

The current PoC is:

- Linux-first
- CLI-first
- local-first
- focused on recorded meeting processing rather than fully live meeting assistance

Current PoC capture profiles:

- `online_dual_stream`: local microphone plus system audio from the meeting platform
- `in_person_single_mic`: one room microphone capturing all participants

The architecture already assumes that the CLI is only an interface adapter.
Core workflow logic must remain independent from CLI behavior, concrete provider implementations, and OS-specific details.

## Fixed Constraints

Treat the following as fixed for this research task unless the brief explicitly asks you to challenge them:

- audio recording, transcription, and diarization are local-only stages
- Linux is the first implementation and test platform
- runtime data must live outside the source repository
- the architecture must remain compatible with weaker tester machines, not only the first target laptop
- model and provider choices must sit behind ports and policy/capability resolution
- recording robustness outranks implementation neatness and interface convenience
- the current PoC has no strict latency target for post-meeting processing
- summarization is local-preferred; remote fallback policy is handled separately by `R-06`

Do not recommend a solution that assumes remote ASR or remote diarization.

## Current Architecture Baseline

The current architecture baseline relevant to this task is:

- the product uses an artifact-first pipeline
- raw audio is the canonical source artifact
- workflow code must not depend directly on one local runtime product
- local model-backed stages may include summarization and semantic coherence checks
- the architecture has not yet chosen between direct library integration, `Ollama`, `LM Studio`, or another local runtime family
- provider access must remain automatable and testable for agent-driven development

This task is not being asked in the abstract.
It exists because the architecture needs a current recommendation for how local model-backed stages should be accessed during PoC implementation.

## Research Question

Which local runtime strategy should `mena` use for local model-backed stages in the current PoC architecture baseline, and how viable is local summarization on the first target laptop and on weaker representative tester machines?

## Decision To Unblock

This research should unblock:

- the default PoC strategy for accessing local LLM-backed stages
- the shortlist of acceptable fallback local runtime patterns if the first choice proves unsuitable on weaker hardware
- the architectural shape of the local runtime adapter boundary

## Evaluation Criteria

Evaluate options primarily in this order:

1. local operability and failure clarity
2. suitability for weaker representative hardware
3. ease of automation from a CLI-first product and agent-driven test harness
4. replaceability behind provider/runtime ports
5. installation and upgrade burden on Linux
6. observability and debuggability
7. future cross-platform implications
8. performance for post-meeting summarization and related local LLM stages

When useful, discuss the broader architecture priority order that already exists:

1. recording robustness / recoverability
2. sovereignty / privacy
3. resumability / traceability
4. local operability
5. replaceability / evolvability
6. output quality / user trust
7. performance / processing time
8. interface convenience

Your recommendation must not optimize one low-priority criterion at the expense of a higher-priority one without stating that clearly.

## Options To Evaluate

At minimum evaluate:

- direct library integration inside the application process or tightly owned subprocesses
- `Ollama`
- `LM Studio`
- other credible local runtime approaches that materially deserve comparison

If you add a new candidate, explain why it belongs in the comparison and why it is more relevant than simply expanding the list indefinitely.

## Required Questions To Answer

Answer at least the following:

- what runtime strategy is best for the current PoC and why?
- what is the second-best option and why did it lose?
- what assumptions does the recommendation make about hardware, drivers, packaging, or operator skill?
- how well does each option support headless automation, scripted control, deterministic invocation, and failure inspection?
- which option best preserves a clean provider/runtime port boundary?
- what are the installation and update implications for Linux users?
- how risky is each option for weaker tester machines?
- what validation work should still be done before architecture treats the recommendation as settled?

## Out Of Scope

Do not spend research time on:

- remote provider policy questions
- choosing the final ASR engine
- choosing the final diarization engine
- broad consumer benchmarking unrelated to the current PoC architecture
- implementation details for packaging or installers beyond the implications needed for runtime comparison

## Expected Output

The final research note should include:

- a short executive recommendation
- a decision matrix or equivalent structured comparison
- the recommended option
- the second-best option
- major risks and disqualifiers
- implications for architecture boundaries
- implications for installation and support burden
- suggested next validation steps

## References

Primary references for this task:

- `docs/architecture/architecture-overview.md`
- `docs/architecture/implementation-guardrails.md`
- `docs/requirements/poc-requirements.md`

Use them only as supporting context.
This task should still be answerable as a self-sufficient brief.
