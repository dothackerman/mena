# Documentation Structure

This repository separates documentation by role so agents do not have to guess what may be changed and what should be treated as input.

## Folders

### `requirements/`

- source inputs from earlier discussions
- discovery and requirements artifacts

### `architecture/`

- evolving architecture synthesis
- decisions, open questions, tradeoffs, and diagrams

### `research/notes/`

- completed research outputs

### `research/backlog/`

- pending research tasks derived from architecture work

### `testing/`

- repeatable manual or live-environment test protocols
- privacy-preserving scenario definitions and evidence expectations

### `ideas/`

- deferred ideas and future directions
- not implementation scope unless explicitly promoted later

## Practical Rule

- `requirements/` = read-first, edit rarely
- `architecture/` = working synthesis
- `research/notes/` = evidence outputs
- `research/backlog/` = pending investigation
- `testing/` = reusable test method, not captured runtime data
- `ideas/` = deferred, non-binding, do not use for normal coding scope

## Primary Documents

- [Repository Workflow](repository-workflow.md)
- [Architecture Overview](architecture/architecture-overview.md)
- [Implementation Guardrails](architecture/implementation-guardrails.md)
- [Architecture Research Backlog](research/backlog/architecture-research-backlog.md)
- [PoC Requirements](requirements/poc-requirements.md)
- [Discovery Meeting Report](requirements/discovery-meeting-report.md)
- [UAT Scenarios](testing/uat/SCENARIOS.md)
- [UAT Protocol](testing/uat/PROTOCOL.md)
- [Live PipeWire Test Protocol](testing/live-pipewire/README.md)
- [Ideas Index](ideas/README.md)
