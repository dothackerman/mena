# Processing Worker Process

Status: deferred architecture evolution idea

## What

Consider a dedicated processing worker process for ASR, diarization, summarization, or other heavy model-backed work if resource isolation or native-runtime instability becomes a real problem.

## Where It Comes From

- The architecture discussion identified processing as the most plausible next process boundary after capture.
- The architecture review argued that this is speculative before implementation measures real GPU, memory, and runtime behavior.
- Current architecture keeps the confirmed process topology to orchestrator plus capture worker.

## Current Boundary

Do not implement a processing worker in PoC v0 unless concrete implementation evidence shows it is needed.
Keep provider and processing modules separated enough that this remains possible.

