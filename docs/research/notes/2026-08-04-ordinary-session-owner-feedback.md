# Product-owner feedback on the bounded ordinary-session export

Date: 2026-08-04

Issue: [#210](https://github.com/dothackerman/mena/issues/210)

Status: preserved product evidence

## Purpose and privacy boundary

This note preserves the product-owner verdict that selected the work following the first bounded ordinary-session export. It is a sanitized record: no transcript text, participant information, session identifier, local path, generated private note content, or evaluator detail is included.

The private feedback and export remain outside the repository. This note is the durable source for the product expectation and should be read before designing or evaluating another ordinary-session summarization experiment.

## Overall verdict

The exported note package was not valuable enough to use instead of rereading the meeting source.

No obvious invented event was noticed during the initial review, but absence of obvious invention did not make the notes useful. The dominant problems were poor selection, weak abstraction, incomplete coverage, and transcript-like wording.

## Timestamped timeline

The timeline was judged useless. It looked like stitched transcript fragments rather than a meaning-level account of how the meeting progressed.

The expected product shape is:

- a concise sequence of recognizable meeting phases;
- each phase expressed at meaning level rather than copied from a speaker utterance;
- approximate start and end times for every phase;
- coverage spanning the whole meeting rather than filling a fixed capacity from the beginning;
- inclusion of material transitions and outcomes from the closing part of the meeting;
- enough abstraction to recall what happened without rereading the transcript.

The first export failed that expectation. It stopped early, carried only start timestamps, and omitted the final presentation-related resolution. Future timeline experiments should treat this product expectation as their user-visible acceptance seam.

## Action items

Most rendered entries were not actions. Statements, questions, possibilities, and discussion fragments were promoted into the action list. The wording also exposed transcription-like errors.

The expected product contains only explicit, executable commitments. An owner or due date must remain absent unless the meeting states it. A schema-valid list of source-grounded fragments does not satisfy this expectation.

## General summary

The general summary was judged useless because it missed the meeting's core insights and meaningful closing outcome. Generic agreement statements and isolated details did not provide a reliable recall aid.

## Accuracy and transcript concerns

The owner noticed terminology inaccuracies consistent with Swiss-German/domain transcription errors but had not replayed the audio. It was therefore not yet possible to attribute every wording defect to ASR or summarization.

The selected meeting language was German, so the owner requested verification that ASR had received explicit German configuration rather than English or automatic language detection.

## Decision produced by this feedback

The immediate decision was to stop summarization tuning and inspect transcript configuration and quality first. That investigation is preserved in [Swiss-German transcript-quality diagnosis for the ordinary session](2026-08-04-swiss-german-transcript-quality-diagnosis.md).

The later diagnosis found that the transcript was noisy but retained enough meeting meaning for materially better notes. The subsequent meaning-level replay is recorded separately in [Meaning-level note-product experiment on the ordinary session](2026-08-04-meaning-level-note-product-experiment.md). Neither later artifact supersedes this owner verdict: product usefulness remains a human judgment, and this note defines what the first export failed to provide.

## Continuing constraint

Any follow-up experiment claiming to improve the timeline must put a complete, normal user-visible timeline in front of the product owner and ask whether it now functions as a meeting-phase recall aid. Automatic evidence or coverage checks can reject obvious failures, but they cannot replace that review.
