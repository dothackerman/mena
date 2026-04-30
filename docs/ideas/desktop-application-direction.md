# Desktop Application Direction

Status: deferred product direction

## What

Explore a desktop application as the likely product direction after the CLI PoC.
The desktop app may provide a focused UI for recording, device/profile confirmation, correction, playback, exports, and local data management without requiring a large web-style application surface.

## Where It Comes From

- The customer repeatedly stated that the CLI is only the PoC interface and that a desktop app is a realistic next product direction.
- The architecture overview keeps the core independent from the CLI and treats OS-specific behavior behind adapters.
- The local data and retention discussion highlighted future desktop packaging, install, update, and uninstall concerns.

## Current Boundary

The current implementation should keep core workflow logic interface-independent.
It should not build desktop UI functionality unless this idea is explicitly promoted.

