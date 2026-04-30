# OpenRouter and Provider Brokers

Status: deferred provider experimentation idea

## What

Evaluate provider brokers such as OpenRouter for faster experimentation across models and providers.
The attraction is convenience and model variety, but the broker becomes part of the provider chain.

## Where It Comes From

- The customer proposed using Mistral models through OpenRouter for easier experimentation.
- The architecture review flagged that broker jurisdiction, logging, routing, retention, and contractual guarantees are separate from the underlying model family.
- The customer confirmed direct Mistral API as the PoC fallback instead.

## Current Boundary

The PoC fallback path is direct Mistral API for summarization when local execution is unavailable or insufficient.
OpenRouter-style brokers are not part of PoC architecture and should not be implemented unless promoted later after policy review.

