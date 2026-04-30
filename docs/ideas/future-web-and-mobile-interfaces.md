# Future Web and Mobile Interfaces

Status: deferred product direction

## What

Consider future web and mobile interfaces for reviewing sessions, inspecting outputs, and possibly controlling recording workflows.
These interfaces would be adapters over the same core session and artifact model rather than separate products.

## Where It Comes From

- The customer mentioned future product formats including web, desktop, and mobile.
- `REQ-U01` requires the architecture to separate the CLI from core logic so other interfaces can replace or complement it.
- The architecture overview references future web, desktop, mobile, and agent adapters as interface directions.

## Current Boundary

The CLI PoC must not assume terminal-specific behavior in core services.
No web or mobile implementation work should be derived from this idea unless explicitly promoted.

