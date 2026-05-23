# ADR-028: Installation profiles, local setup state, and preflight separation

- **Type:** Architecture
- **Status:** Accepted
- **Date:** 2026-05-23
- **Superseded by:** -
- **Related:** #16, #68, ADR-010, ADR-012, ADR-021, ADR-022, ADR-023, ADR-026, ADR-027

## Context

Mena needs an install and setup path that can start small for the Linux CLI PoC, but not paint the product into a one-off installer design.

The same machine may host:

- a repository checkout used for development
- an installed product CLI used like a future user would use it
- local model runtimes and downloaded model assets
- configuration and user-owned session data

Future product direction may include company-controlled model recommendations, hardware-aware setup choices, administrator policy, re-setup after hardware upgrades, and privacy-preserving product telemetry. Those are not PoC implementation scope. The architecture still needs a stable boundary now so issue #68 does not encode ad hoc installer behavior that blocks those directions later.

The immediate PoC need is narrower: one Linux CLI alpha install path with one fixed local provider/model profile, local-only provider setup, explicit user confirmation before dependency/model installation, and no remote fallback configuration.

## Decision

Mena distinguishes three concepts:

1. **Profile catalog**
   - The product-supported setup definitions controlled by Mena.
   - A profile defines the intended provider/runtime/model/dependency set, stage coverage, storage expectations, policy constraints, and minimum machine capability.
   - A profile is not inferred from whatever happens to be installed on the machine.
   - For the PoC, the catalog may contain only one bundled static local profile.

2. **Local installation state**
   - What the current machine actually has installed, downloaded, configured, and reachable.
   - This includes executable paths, runtime versions, model cache presence, provider availability, configuration files, and platform capabilities.
   - Local state may satisfy, partially satisfy, or fail to satisfy a profile.
   - Local state is not itself a product recommendation.

3. **User or administrator choice**
   - The selected setup path among profiles that are both policy-allowed and viable on the detected machine.
   - The user or administrator may confirm or decline setup changes.
   - The product must not offer an impossible or policy-disallowed setup as a selectable option.

Install/preflight flow must separate:

- profile lookup
- hardware and local-state evaluation
- planned-change presentation
- user confirmation
- mutation of dependencies, models, configuration, or caches
- post-setup verification

Preflight evaluation must be callable independently from setup mutation. Setup mutation must occur only after presenting the planned changes and receiving explicit confirmation.

For issue #68, implementation should introduce the first fixed local profile and enough structure that profile catalog, local installation state, and user/admin choice are not mixed. It must not implement the future multi-profile recommendation system, telemetry-driven profile updates, broad hardware matrix, doctor command, re-setup UX, or remote provider selection.

Remote fallback remains stage-specific and policy-constrained. #29 covers only summarization fallback. Remote ASR or diarization would require separate product and architecture decisions.

## Consequences

Positive:

- The PoC installer can remain small without becoming a one-off script that hard-codes future product policy.
- Future hardware-aware setup can add profiles without rewriting workflow or provider code.
- Future doctor and re-setup flows can reuse evaluation and local-state inspection instead of reverse-engineering installer side effects.
- The product can explain why a setup is unavailable instead of offering unsupported choices.
- Provider policy remains centralized rather than scattered through install scripts.

Tradeoffs:

- #68 must define a minimal profile representation before multiple profiles exist.
- Preflight code needs a clearer boundary between inspection and mutation than a simple install script would need.
- The first implementation must be careful not to overbuild a generic recommendation engine.

Deferred:

- Multiple local profiles.
- Hardware-aware recommendation UX.
- Administrator-managed profile policy.
- Profile catalog distribution or update mechanism outside code releases.
- Doctor and re-setup commands.
- Privacy-preserving telemetry and any use of telemetry to improve recommendations.
- Remote ASR or diarization setup.
