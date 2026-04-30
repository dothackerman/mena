# OpenClaw Adapter

Status: deferred integration idea

## What

Provide an OpenClaw adapter or command surface so `mena` can be triggered or consumed from the customer's personal AI assistant ecosystem.
This may include starting/stopping recordings, sending outputs into memory, or exposing session artifacts to agent workflows.

## Where It Comes From

- The original discovery report identified OpenClaw as a major integration direction.
- The PoC requirements later narrowed scope so `mena` stands alone and OpenClaw is out of scope.
- The architecture discussion clarified that the CLI overlaps with a future OpenClaw adapter but is not equivalent to the product architecture.

## Current Boundary

OpenClaw is not a PoC runtime dependency.
No implementation issue should assume OpenClaw unless this idea is explicitly promoted.

