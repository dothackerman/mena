# Streaming Transcription Health Signal

Status: deferred product idea

## What

Use streaming transcription during a meeting primarily as a recording health signal so the user can see that speech is being captured and interpreted while the meeting is still running.

## Where It Comes From

- The PoC requirements list streaming transcription during the meeting as `POST-POC A`.
- The architecture review highlighted user anxiety around invisible recording and recommended at least a minimal heartbeat.
- The current architecture adopts a minimal recording heartbeat for PoC, but not streaming transcription.

## Current Boundary

PoC implementation should provide a simple recording health heartbeat such as elapsed time and flushed segment count.
It should not implement streaming transcription unless this idea is explicitly promoted.

