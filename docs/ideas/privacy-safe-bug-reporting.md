# Privacy-Safe Bug Reporting

Status: deferred product-support idea

## What

Design a future user-facing bug-report flow that can share useful diagnostics without leaking meeting content.
This may include local log review, redaction, explicit user consent, export bundles, and support workflows.

## Where It Comes From

- The architecture review flagged diagnostics as separate from telemetry.
- The customer clarified that a company-facing bug-report flow is not needed for the PoC because logs remain on the developer/tester machine.
- The architecture now requires logs to avoid meeting substance by mechanism, not discipline.

## Current Boundary

PoC implementation may create local privacy-safe structured logs.
It should not build a support upload or bug-report sending flow unless promoted later.

