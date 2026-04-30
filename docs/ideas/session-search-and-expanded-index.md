# Session Search and Expanded Index

Status: deferred product idea

## What

Expand the global index beyond a rebuildable session-list projection into richer session discovery, search, cross-session references, or analytics.

## Where It Comes From

- The architecture review flagged that "global index" was referenced without a narrow definition.
- The architecture now narrows the PoC global index to session discovery fields only.
- Richer indexing remains a possible product direction once there are enough sessions to justify it.

## Current Boundary

The PoC global index should support `list`, `show`, and `resume`.
It must not become full-text search, cross-session entity storage, or analytics unless this idea is promoted later.

