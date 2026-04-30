# R-06 Fallback Provider Policy

Status: Open

## Question

- what exact account, contract, retention, routing, and policy conditions make direct Mistral API acceptable as the PoC summarization fallback under `CON-01`?
- how should `mena` verify or configure that a ZDR-bearing access path is active before allowing fallback?

## Current Architecture Baseline

- direct Mistral API is the confirmed PoC fallback candidate for summarization only
- fallback is allowed only through a Zero Data Retention access path that satisfies `CON-01`
- default-retention API access is not an architecture-compliant product fallback, even if used only for summarization

## Current External Facts To Validate

As of April 30, 2026, official Mistral AI pages state:

- API data is not used for model training.
- API input and output are retained for a rolling 30-day abuse-monitoring period unless Zero Data Retention is activated.
- Zero Data Retention for Mistral AI Studio can be requested through the Help Center or support and is approved at Mistral's discretion.

Sources:

- Mistral AI Privacy Policy: https://legal.mistral.ai/terms/privacy-policy
- Mistral AI Privacy documentation: https://docs.mistral.ai/admin/security-access/privacy
- Mistral AI Help Center, ZDR: https://help.mistral.ai/en/articles/347612-can-i-activate-zero-data-retention-zdr

## Critical Distinction

- model family, direct provider, and brokered access path are separate policy concerns
- brokered access paths such as OpenRouter are deferred to ideas and must not be treated as equivalent to direct Mistral API access
- a French/EU provider is not automatically sufficient if the selected access path retains request or response content

## Why It Matters

- affects policy enforcement
- affects tester operability on weaker machines
- affects what the user may or may not configure directly

## Open Validation Tasks

- confirm the Mistral account, workspace, contract, or support-approved setting needed for ZDR
- define what the application can verify automatically versus what must be configured or attested by the user
- define the failure behavior when credentials exist but ZDR status is unknown
- decide whether any non-ZDR personal experimentation is allowed as an explicit out-of-architecture exception, or prohibited entirely
