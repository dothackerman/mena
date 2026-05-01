# Diarization Provider Strategy for the PoC

I could not inspect `docs/architecture/architecture-overview.md` or `docs/requirements/poc-requirements.md` in this session, so this note is grounded in the product brief you supplied and external technical sources.

## Bottom line

The best fit for the current PoC is a **scenario-aware local strategy**, not a single uniform diarizer applied the same way to every recording. Concretely, the strongest recommendation is to use **pyannote.audio `speaker-diarization-community-1` as the sole local diarization model provider**, but only where the capture topology does not already give you a better answer. In `online_dual_stream`, make the local microphone the primary attribution signal and use diarization only for the ambiguous remote/system side when it adds value. In `in_person_single_mic`, run offline diarization on the room-mic recording and treat the result as an editable hypothesis, not as authoritative truth. This fits the privacy constraint because `community-1` runs locally and can be used offline once downloaded, while pyannote’s premium `precision-2` path runs on remote servers and is therefore out for this PoC. It also fits your correction-first architecture because `community-1` explicitly provides an **exclusive diarization** view intended to reconcile diarization with transcription timestamps. citeturn16search0turn7view4turn6view0

In practical terms, the PoC strategy should be:

- **`online_dual_stream`**: deterministic, source-aware attribution for the local user, plus optional diarization on the remote/system stream.
- **`in_person_single_mic`**: offline mono diarization, explicit uncertainty surfacing, and routine correction.
- **All modes**: persist raw audio, regular diarization, exclusive diarization, and user correction overlays as separate artefacts.

## Why this fit is best

`community-1` aligns unusually well with the constraints you fixed. It runs locally, supports offline use after local download, can accept known or bounded speaker counts, and adds exclusive diarization specifically to make speech-to-text reconciliation easier. Its published open benchmarks are also credible enough for a PoC baseline: the current pyannote package is actively maintained, and the benchmark table for the open pipeline reports DERs such as **17.0 on AMI IHM**, **19.9 on AMI SDM**, **20.3 on AliMeeting channel 1**, and **20.2 on DIHARD 3 full**. Those are not “solved” numbers, but they are strong enough for a product architecture that already assumes confidence flagging, regeneration from raw audio, and human correction. citeturn16search0turn6view0turn8search2

The most important architectural caveat is that `community-1` is a **mono** diarizer: its model card says it ingests mono 16 kHz audio, automatically resamples other sample rates, and **downmixes stereo or multi-channel input to mono by averaging the channels**. That means you should **not** hand it combined dual-stream capture and hope it will preserve the structural separation between local mic and remote/system audio, because that separation is exactly what the model will collapse away. citeturn16search0

That caveat matters because meeting diarization research repeatedly shows that extra channel or spatial information helps. One AMI meeting paper reported **9.4% to 18.1% relative DER improvements** over an overlap-aware single-channel baseline when multichannel spatial features were used. For your PoC, the safe conclusion is not “find a more magical model”; it is “preserve capture structure in the application layer and only invoke a mono diarizer where ambiguity remains.” citeturn10search9turn16search0

## Scenario-specific behaviour

### Online dual stream

In `online_dual_stream`, the local microphone should be treated as the main source of truth for the local user. If the product only needs a robust **You vs Others** distinction, the local side may not need model-based diarization at all. The remote/system stream is the ambiguous side: if it arrives as one mixed channel, `community-1` can still separate it into session-local speaker IDs, and its exclusive diarization output can be used to reconcile those speaker turns with transcription timestamps. But that separation should be presented as **generic, session-local labels**, not as stable identities. citeturn16search0turn16search2

So the dual-stream path should be intentionally asymmetric. Use deterministic source-aware attribution for the local speaker, optional diarization for the remote mix, and preserve explicit overlap information rather than flattening everything into one final speaker label. This is especially important because the open pyannote stack is designed around both regular diarization and exclusive diarization: the former captures fuller timing information, potentially including overlap, while the latter exists precisely because transcription systems are often less precise and easier to align when only one active speaker is emitted at a time. citeturn16search0turn16search2turn16search4

### In-person single mic

`in_person_single_mic` is the hard case, and the architecture should say that plainly. Even on public meeting corpora, pyannote’s benchmark shows a quality gap between close-talk meeting audio and single distant microphone audio, and the DIHARD challenge literature makes the broader point explicit: diarization gets much harder when overlap is scored, forgiveness collars are removed, and recordings come from varied difficult microphone conditions. That is much closer to the shape of real room-mic meetings than clean headset audio is. citeturn6view0turn11search0

For this mode, the correct PoC expectation is **useful but fallible diarization**. Run it offline after robust capture, surface uncertainty aggressively, and expect correction to be routine rather than exceptional. This is also the mode where “diarization optional per meeting” is most sensible: if the room audio is clearly too poor, falling back to coarse labels or even no diarization is better than pretending the model has solved something it plainly has not. citeturn6view0turn11search0

## Failure modes to treat as normal

The architecture should treat several failure modes as **normal outputs of the problem**, not as surprising bugs. The first is **overlap collapse or overlap misassignment**: overlap-aware resegmentation remains an active research problem precisely because overlapped speech is hard, and even strong methods only report relative improvements rather than eliminating the issue. The second is **short backchannels and rapid turn switches** being attributed incorrectly; NeMo’s own diarization docs single out very short conversational turns such as “yes”, “uh-huh”, and “oh” as especially challenging. The third is **speaker split and merge behaviour**: one real speaker being fragmented into multiple synthetic IDs, or similar-sounding speakers being merged. The fourth is **speaker-count instability** in short, noisy, or far-field material. The fifth is general **far-field fragmentation**, where room noise, microphone distance, and reverberation produce tiny false turns or missed turns. citeturn10search4turn5search1turn11search0turn6view0

The key design consequence is that the PoC should not hide these cases behind silent retries, covert smoothing, or falsely precise UI. Real meeting diarization is still difficult enough that public benchmark tables on challenging corpora remain far from zero error, and challenge work continues to focus on overlap, domain mismatch, and difficult microphone conditions. In other words: these failures are part of the expected operating envelope. citeturn11search0turn10search4turn6view0

## Correction flow and artefact design

The PoC should persist at least **three diarization artefacts**. First, a **regular diarization timeline**, which remains the most faithful speaker-timing hypothesis and should keep overlap information when available. Second, an **exclusive diarization projection**, which is the safer input for word-level or turn-level transcript attribution and downstream note generation. Third, a **correction overlay**, which records user edits separately from raw audio and raw model output so the whole stack can be regenerated without losing human decisions. That separation follows directly from pyannote’s own distinction between regular and exclusive diarization and fits your raw-audio-preserving architecture very well. citeturn16search0turn16search2

This is also where the product should rely on correction instead of pretending that the model has solved the task. The open `community-1` stack does **not** provide the segmentation confidence scores or speaker confidence scores that pyannote exposes in its premium offer. So if the PoC stays local-only and open-source, confidence cannot honestly be represented as “the model already gave us a calibrated answer”. It has to be a **product-level uncertainty layer** built from heuristics and secondary signals. Good triggers include overlap presence, very short speaker turns, unusually frequent speaker flipping, disagreement between regular and exclusive projections, obvious speaker-count instability, and poor audio-quality indicators from the surrounding transcription or VAD path. citeturn8search0turn16search4

The areas where correction should be expected most heavily are clear: heavily overlapped spans, far-field single-mic meetings, ambiguous speaker counts, and any attempt to convert generic session-local labels into stable named people. The conservative product behaviour should therefore be to degrade gracefully to labels such as **You**, **Remote speaker 1**, **Speaker 2**, or **Speaker unknown** rather than attaching a confident but wrong person identity.

## Runtime, packaging, and the strongest alternative

Packaging `community-1` is manageable, but it is not zero-cost. The current pyannote package requires **Python 3.10+**, needs **ffmpeg** for audio decoding, requires acceptance of the model terms plus an access token for initial download, and can then be cloned locally for offline use. The pipeline runs on **CPU by default** and can be moved to **GPU** when available. It is therefore viable for local-only operation, but it should be treated as a post-capture processing component, not part of the time-critical recording path. citeturn6view0turn16search0

Published speed numbers should be read carefully. pyannote reports `community-1` at about **31 seconds per hour of audio on an H100** in its self-hosted benchmark table. That is useful as a best-case reference, but it is not a consumer-laptop promise. For the PoC, the safe runtime assumption is that capture must stay decoupled from diarization, and actual device validation matters more than headline benchmark speed. citeturn8search2

If the transcription stack already uses **WhisperX**, there is a maintainability advantage in reusing its existing pyannote-based diarization path instead of introducing a second diarization family. WhisperX’s own documentation describes its speaker diarization as being powered by pyannote’s community model rather than by a distinct underlying provider. That does not change the main recommendation, but it can reduce integration surface. citeturn14search0turn14search1

The **strongest alternative** is the **NeMo** diarization stack. It is strong because it stays local, offers both **offline and online Sortformer diarizers**, and also supports cascaded diarization using VAD, speaker embeddings, clustering, and MSDD-style components. If you were optimising for future low-latency diarization during live capture, or for a broader research platform, that would make it a serious contender. It lost for the current PoC because the fit is worse, not because the raw capability is obviously inferior. NeMo’s current documentation and repository state imply a heavier operational footprint: **Python 3.12+**, **PyTorch 2.6+**, broad toolkit installs or CUDA-specific variants, and an explicit recommendation to use a stable container while the repository is being reorganised. For a PoC whose design already accepts correction-assisted offline regeneration, that is too much packaging and runtime surface area to absorb before you have evidence that pyannote-based local diarization is the actual bottleneck. citeturn7view2turn7view3turn13view0

## Validation steps

Validation should mirror the two capture profiles and the public evidence that close-talk and distant/noisy meeting audio behave differently. In practice, the test matrix should include dual-stream meetings with clear local speech, dual-stream meetings with frequent local/remote interruption, quiet room-mic meetings with two to four speakers, and noisy or reverberant room-mic meetings with overlap and uneven speaker distance. Public meeting benchmarks and challenge work are already clear that far-field and difficult-condition diarization is meaningfully worse, so those conditions need to be first-class test cases rather than edge cases. citeturn6view0turn11search0

A realistic PoC validation set should check at least these scenarios:

- **Dual-stream, clean**: one local speaker, two or more remote speakers, minimal overlap.
- **Dual-stream, adversarial**: local interruptions, remote backchannels, simultaneous talk across streams.
- **Single-mic, quiet room**: two to four speakers at normal table distance.
- **Single-mic, difficult room**: laptop mic or inexpensive room mic, HVAC or café-like noise, reverberation, and multiple overlaps.

Success should be judged on **usefulness and edit burden**, not DER alone. The most important measures are the proportion of transcript words or turns that remain acceptably speaker-labelled after review, correction time per meeting, the rate of visibly wrong but high-salience speaker labels, the stability of coarse labels such as **You** versus **Others**, and the share of meetings where the system makes a sensible fallback decision instead of projecting false certainty.

If those tests show that `online_dual_stream` works well mainly because source-aware structural attribution carries much of the load, while `in_person_single_mic` still depends on visible correction, that is not a failure of the PoC. It is precisely the architecture reality the PoC should make explicit.