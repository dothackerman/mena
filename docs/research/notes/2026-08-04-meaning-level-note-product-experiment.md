# Meaning-level note-product experiment on the ordinary session

Date: 2026-08-04

Issue: [#210](https://github.com/dothackerman/mena/issues/210)

Status: completed; hypothesis not supported

## Question

Can post-extraction meaning-level selection and abstraction turn the bounded ordinary-session replay into a useful three-product note package while holding the transcript, extracted evidence, local model, and runtime settings fixed?

This experiment followed the product-owner feedback checkpoint from the first #210 replay. It did not change ASR, rerun extraction, compare models, run RC7, or change production behavior.

## Controlled design

The experiment reused the first replay's 53 timeline candidates, 54 action candidates, and 54 general-summary candidates. It kept `llama3.1:8b`, an 8,192-token context, temperature `0`, seed `0`, and the persisted German transcript fixed.

Only post-extraction processing changed:

- each of the nine frozen transcript chunks produced at most one meaning-level timeline phase;
- each chunk classified at most three explicit commitments from its frozen action candidates;
- one bounded call consolidated chunk phases into four to eight chronological phases;
- one whole-meeting plan selected at most two summary sections;
- two source-rehydrated calls drafted the summary sections;
- timestamps remained derived from cited source segments.

The dry call graph declared 22 calls and a 10,800-second wall-time ceiling. No retries or recursive reducers were allowed. All meeting-derived inputs and outputs remained in private temporary storage.

## Red-capable evaluation seam

A private owner-feedback rubric checked the saved baseline and the new package for:

- source-derived start/end intervals;
- timeline evidence reaching at least 95% of the meeting duration;
- no more than 25% verbatim timeline entries;
- coverage of five owner-relevant evidence threads spanning the initial review, the digital-tutor model, project-fed tutor content, and the final presentation outcome;
- action items backed by a human-labelled explicit-commitment evidence set;
- no unsupported owner or due-date fields.

The original export went red as expected:

- timeline ended at 428.7 seconds of a 1,225.1-second meeting;
- 10 of 18 timeline entries were verbatim source segments (55.6%);
- two required presentation threads were absent;
- 11 of 12 action entries did not match explicit-commitment evidence;
- timeline entries had no end times.

The rubric and transcript-derived evidence labels are private artifacts and are not committed.

## Result

The run completed all 22 declared calls in 160.58 seconds. The largest rendered prompt was 3,305 bytes. Provider measurements reported 13,004 prompt-evaluation tokens and 1,351 generated tokens. There were no retries or capacity failures.

Meaning-level abstraction improved surface form but did not produce an acceptable package:

| Measure | First replay | Meaning-level replay |
| --- | ---: | ---: |
| Timeline entries | 18 | 8 |
| Timeline entries equal to a source segment | 55.6% | 0% |
| Last cited timeline evidence | 428.7 s | 1,047.96 s |
| Required evidence threads missing | 2 | 3 |
| Action entries | 12 | 14 |
| Entries not matching explicit commitments | 11 | 12 |
| Evaluator verdict | Fail | Fail |

The new timeline used concise labels and carried start/end fields, but its cited intervals were narrow evidence snippets rather than meaningful phase spans. It still stopped before the final presentation resolution. The general summary selected only community purpose and the digital tutor replacing the annual course; it omitted the wider product-model distinctions and presentation outcome. Commitment classification continued to accept completed work, product statements, possibilities, and descriptive fragments as actions.

No product-owner usefulness review was requested after the automated rubric failed. The private Markdown and evaluator output remain available for diagnosis, but they are not treated as an accepted note package.

## Interpretation

The experiment falsifies the narrow hypothesis that one extra layer of local-model abstraction over frozen candidates is sufficient. It does not show that meaning-level note products are the wrong goal.

Three mechanisms remain inadequate:

1. **Coverage is prompt-requested rather than structural.** The final consolidation may still discard the last chunk. A whole-meeting product needs deterministic coverage accounting and an explicit incomplete result when required chronological regions disappear.
2. **A cited evidence span is not a phase interval.** Deriving both endpoints only from the small set of supporting segments produces narrow snippets. Phase boundaries need a separate deterministic contract tied to consecutive source regions.
3. **Positive-only action generation is too permissive on the local 8B model.** Stronger wording did not reliably distinguish commitments from statements. The next action experiment needs an explicit candidate-level keep/reject decision with rejection reasons and a deterministic acceptance gate before rendering.

The frozen candidate inventory also remains noisy. Because this run changed aggregation without first establishing an extraction reference set, the result cannot separate candidate quality from abstraction capability beyond the observed action-classification failure.

## Recommendation

Do not adopt this replay design, tune it in place, or proceed to RC7. The next bounded experiment should isolate one of two questions:

1. establish a human-labelled extraction reference over the ordinary session, then measure timeline-event and explicit-commitment candidate recall/precision before any aggregation; or
2. test deterministic whole-meeting coverage and phase-boundary mechanics using a frozen, human-approved evidence inventory.

Model comparison remains premature until identical, quality-controlled evidence reaches the abstraction calls. Audio-backed Swiss-German ASR validation remains separate and is still useful for terminology and transcript-quality decisions, but this run supplies no reason to combine it with summarization tuning.

## Private artifacts

The private run directory contains the manifest, dry call plan, 22 request/response records, note package, Markdown export, evaluator output, and sanitized metrics. These artifacts contain meeting-derived information and must not be committed or posted to GitHub.
