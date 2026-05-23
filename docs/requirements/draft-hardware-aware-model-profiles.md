---
status: elicitation
phase: elicitation
hierarchy: business-requirement
babok-type: business
# Phase transitions tracked in commit history
# Do not change status without completing full chain (analysis → specification → commitment)
created: 2026-05-23
author: PM (Kai)
traces-to: none
# When committed, will trace to: docs/requirements/poc-model-config-local.md
---

# Hardware-Aware Model Profiles for Mena

## Status

`[ELICITATION — NOT YET ANALYZED]`

## BABOK Classification

**Requirement type:** Business requirement
**Hierarchy level:** High-level organizational need
**Traceability:** Will decompose into stakeholder and solution requirements when committed

## Context

Mena ships multiple AI models (ASR, diarization, summarization). Each model has hardware requirements (VRAM, RAM, CPU/GPU tradeoffs). Users do not know which models their machine can run effectively. From a product perspective, Mena should intelligently match hardware to optimal models, learn from usage, and improve recommendations over time without manual user intervention.

## Vision (Raw Customer Input)

Administrator-configurable model recommendation profiles:
- "Fully local but only 8GB VRAM" → specific model set
- "Fully local and 16GB VRAM" → different model set
- Telemetry measures model performance per profile
- Version updates improve model selection based on aggregate data

## Current Understanding (PM Notes)

- Business value: competitive differentiation, improved user experience
- Business risk: telemetry must be privacy-preserving per Mena principles
- Business assumption: hardware landscape will diversify (laptops, workstations, cloud-edge)
- Open question: is this a product differentiator or table stakes?

## Known Sub-Components (Not Yet Requirements)

1. Hardware detection (auto-detect VRAM, RAM, GPU)
2. Model recommendation engine (map hardware → model set)
3. Telemetry collection (performance metrics per profile)
4. Update mechanism (version bumps shift recommendations)
5. Admin configuration (who configures? OG as user? OG as developer?)

## Blocking Questions for Analysis

1. Is hardware detection automatic or user-reported?
2. What telemetry leaves the machine vs. stays local?
3. Who is the "administrator" in a single-user local product?
4. What is the update cadence? Auto or manual opt-in?
5. Regulatory implications of telemetry (GDPR, Swiss privacy law)?

## Relationship to PoC

**This business requirement is NOT in PoC scope.**

PoC slice (separate requirement): document which models work on the known-capable machine (Tuxedo). See `poc-model-config-local.md`.

## Next Step

- [ ] Requirements analysis session with OG
- [ ] Research privacy-preserving telemetry patterns
- [ ] Competitive analysis: how do other local AI products handle this?

---

# Phase Transition Log

| Date | Phase | Notes | Author |
|------|-------|-------|--------|
| 2026-05-23 | Elicitation | Initial capture from OG session | Kai |
