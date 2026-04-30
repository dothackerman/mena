# Alternative Recording Triggers

Status: deferred product idea

## What

Explore alternative ways to start and stop recordings beyond CLI commands, such as keyboard shortcuts, desktop controls, OpenClaw commands, chat commands, or hardware triggers.

## Where It Comes From

- The discovery report listed several possible recording trigger mechanisms.
- The PoC requirements choose CLI interaction as the fastest testable path.
- The architecture keeps interface adapters separate from core workflow logic so triggers can be added later.

## Current Boundary

PoC implementation should use CLI commands for recording control.
Do not implement alternative triggers unless explicitly promoted.

