# Speaker Profile Learning

Status: deferred product idea

## What

Learn recurring speaker profiles across meetings so future diarization can combine blind diarization with known speaker embeddings or enrolled identities.

## Where It Comes From

- The discovery report discussed speaker profile learning across meetings.
- The PoC requirements list speaker profile learning as `POST-POC C`.
- The requirements also mention persistent voice embeddings as a low-cost forward-compatibility measure.

## Current Boundary

PoC implementation may preserve speaker-related artifacts if already produced, but should not build cross-meeting speaker identity learning unless explicitly promoted.

