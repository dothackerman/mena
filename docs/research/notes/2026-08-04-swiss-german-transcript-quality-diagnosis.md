# Swiss-German transcript-quality diagnosis for the ordinary session

Date: 2026-08-04

Issue: [#210](https://github.com/dothackerman/mena/issues/210)

Status: completed text/provenance diagnosis; audio validation remains open

## Purpose and privacy boundary

This note preserves the sanitized transcript-quality investigation requested after the product-owner review of the first bounded export. It contains configuration facts, aggregate measurements, and causal conclusions. It omits transcript excerpts, participant information, session identity, private paths, and the private generated notes.

The investigation inspected persisted text and provenance only. It did not replay audio, calculate word error rate, or determine the correct wording of every disputed term.

## Language and runtime configuration

German language selection was honored:

- the session and capture plan recorded `de`;
- both channel-level ASR artifacts recorded German as the requested decode language;
- both recorded German as the detected language with probability `1.0`;
- the operative decode did not use English or automatic language detection.

The installed-product run used faster-whisper `medium` on the accepted CUDA-balanced profile with `int8_float16` computation.

The owner's configuration concern was therefore resolved: Mena explicitly supplied German to faster-whisper for this meeting.

## Aggregate transcript signals

- Meeting duration: 1,225.1 seconds.
- Merged transcript: 216 segments.
- Local microphone: 110 segments and 1,167 words; mean segment log probability `-0.374`.
- System audio: 106 segments and 964 words; mean segment log probability `-0.448`.
- Words below probability `0.5`: 120 local and 174 system.
- Words below probability `0.25`: 40 local and 38 system.
- Review queue: 261 flags across 180 distinct merged segments.
- Flag composition: 158 low-transcription-confidence, 83 speaker-overlap, and 20 low-speaker-confidence flags.

The review threshold was deliberately strict at `0.85` word probability. Flag counts are screening signals, not measured word error rate. The system-audio channel was quantitatively weaker than the local-microphone channel.

## Qualitative assessment

The transcript was noisy but broadly intelligible and coherent across long spans. Domain terminology, Swiss-German normalization, sentence boundaries, and overlapping dual-stream speech introduced visible defects.

Those defects matter for exact terminology and polish. They did not erase the central discussion structure or the closing presentation-related commitments. The source contained enough coherent evidence for materially better meaning-level notes than the first export produced.

## Export-versus-source diagnosis

The first export's failures cannot be explained by ASR alone:

- 10 of 18 timeline entries were verbatim source segments;
- 11 of 12 action entries were verbatim source segments;
- the timeline ended at 7:08 of a 20:25 meeting;
- timeline entries had only a start timestamp rather than phase intervals;
- most action entries were statements, questions, or discussion fragments rather than explicit commitments;
- the presentation outcome was present in the transcript but missing from the exported products;
- the general summary omitted central distinctions even though the transcript retained them.

This is primarily an extraction, selection, and abstraction failure, compounded by the capability of the local 8B summarization model. It is not a failure to pass the chosen language to ASR.

## Conclusion

The transcript is adequate for continued meaning-level summarization experiments, provided those experiments remain source-grounded and acknowledge terminology noise. ASR quality is not sufficient to explain the useless export.

An audio-backed Swiss-German spot check remains valuable for measuring actual errors, especially domain terms and the weaker system-audio channel. It should be a separate experiment so that ASR and summarization variables are not changed together.

The product verdict that prompted this diagnosis is preserved in [Product-owner feedback on the bounded ordinary-session export](2026-08-04-ordinary-session-owner-feedback.md). The later post-extraction abstraction experiment is preserved in [Meaning-level note-product experiment on the ordinary session](2026-08-04-meaning-level-note-product-experiment.md).
