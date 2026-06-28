# mena — PoC Requirements Specification

---

**Product name:** mena  
**Document type:** Requirements specification — Proof of Concept  
**Date:** April 14, 2026  
**Version:** 1.0  
**Customer:** OG (Oriol Gut)  
**Requirements session:** Claude Opus 4.6 (requirements engineer role), April 14, 2026  
**Status:** Draft for customer review  
**Companion documents:**
- *Preliminary Discovery Meeting Report — AI-Backed Personal Meeting Notes* (April 8, 2026, Claude Sonnet 4.6, solutions architect role) — referenced as **[DISC]** throughout this document
- *Meeting Recording: Dual-Stream Architecture Notes* — referenced as **[AUDIO]** throughout this document

---

## How to Read This Document

This document specifies *what* the mena PoC must do. It does not specify *how*. Implementation decisions belong to the architect and developer. Where a requirement traces back to the discovery meeting or a decision made during the requirements session, the origin is marked.

**Conventions:**

- `[REQ-Fnn]` — Functional requirement
- `[REQ-Rnn]` — Reliability requirement
- `[REQ-Unn]` — Usability requirement
- `[REQ-Pnn]` — Performance requirement
- `[REQ-Snn]` — Supportability requirement
- `[CON-nn]` — Constraint
- `[SCOPE-OUT]` — Explicitly excluded from PoC scope
- `[POST-POC]` — Stated roadmap intent, not a commitment

Traceability references use the format `[DISC §n.n]` for the discovery document and `[AUDIO §section]` for the audio experiment notes.

---

## 1. Purpose

mena is a personal, privacy-first meeting notes product. It records meetings, transcribes and diarizes the audio, supports interactive human correction, and produces structured meeting notes enhanced by optional manual user notes. mena runs locally by default. Recording, transcription, diarization, audio, speaker data, and speaker-sensitive artifacts remain local-only; LLM-dependent text stages may use an approved direct ZDR Mistral route only under the accepted setup/runtime policy.

The PoC validates that this pipeline works end-to-end for online meetings on Linux, producing meeting notes that the user considers useful and trustworthy.

---

## 2. System Context

### 2.1 What Is Inside the System Boundary

- Audio capture (local microphone and system audio as separate streams)
- Audio recording to disk with crash-safe segmented writes
- Post-meeting transcription with configurable language and lexicon support
- Post-meeting speaker diarization on the remote audio stream
- Transcript merging (local + remote streams, aligned by timestamp)
- Interactive correction of flagged transcript segments
- Summarization using the corrected transcript and optional manual notes
- Structured output generation (markdown)

### 2.2 What Is Outside the System Boundary

- The video call platform (Zoom, kMeet, Google Meet, or any other). mena captures audio at the OS level; it has no integration with any call platform.
- Consent for recording. The user is responsible for obtaining participant consent before recording. mena displays a disclaimer but does not manage consent.
- Manual notes. The user creates and maintains these independently. mena reads them as input to the summarization step if provided.
- Any non-European external service or cloud infrastructure. The approved direct ZDR Mistral route for LLM-dependent text stages is within the system boundary only when allowed by CON-01/CON-03 and the accepted setup/runtime policy.

### 2.3 Actors

**Primary actor:** The user — the person running mena on their machine, participating in the meeting, and reviewing the output.

**Secondary actors:** Meeting participants — their voices are captured on the remote audio stream. They are data subjects but do not interact with mena.

---

## 3. PoC Scope

### 3.1 In Scope

- Online meetings where the user participates via headset from their own machine
- Post-meeting processing (transcription, diarization, correction, summarization)
- CLI-based interaction (start, stop, correction, output)
- Linux operating system
- Swiss German as primary supported language, with language selection at recording start
- Up to 3 remote speakers (plus the local user)
- Meeting duration up to 90 minutes

### 3.2 Out of Scope

`[SCOPE-OUT 1]` Integration with any agent harness, personal assistant, or external tool (including OpenClaw). mena stands on its own. *Evolved from [DISC §6] where OpenClaw was the primary integration target.*

`[SCOPE-OUT 2]` Streaming or live transcription during the meeting. The PoC is fully post-meeting processing. *Traces to [DISC §2.2], hybrid architecture decision retained but real-time display removed from PoC.*

`[SCOPE-OUT 3]` Auto-detection of meeting language.

`[SCOPE-OUT 4]` Speaker profile learning across meetings. *Per [DISC §4.3], the architecture must not preclude this, but the matching logic is not built for the PoC.*

`[SCOPE-OUT 5]` Custom summary templates or output format configuration.

`[SCOPE-OUT 6]` In-person meeting scenario (single microphone, no channel split).

`[SCOPE-OUT 7]` Consent management by the product beyond a startup disclaimer.

`[SCOPE-OUT 8]` Video call platform API integration. mena is meeting-tool-agnostic by design.

`[SCOPE-OUT 9]` Cross-platform support. PoC is Linux only.

`[SCOPE-OUT 10]` More than 3 remote speakers (4 total including the user). *Per [DISC §4.1].*

### 3.3 Post-PoC Priorities (Stated Intent)

`[POST-POC A]` Streaming transcription during the meeting — primarily as a recording health signal so the user can verify capture is working. Equal priority with POST-POC B.

`[POST-POC B]` In-person meeting support (single microphone, all speakers in one stream, full diarization required). Equal priority with POST-POC A.

`[POST-POC C]` Speaker profile learning across meetings (blended blind/enrolled diarization). *Per [DISC §4.3].*

`[POST-POC D]` Cross-platform support — contingent on whether mena becomes a commercial product.

These priorities reflect the customer's thinking as of April 14, 2026 and are expected to evolve based on PoC learnings.

---

## 4. Use Case: Online Meeting with Post-Meeting Notes

### 4.1 Preconditions

- The user has a working headset connected to their Linux machine
- mena is installed and configured
- A meeting is about to start on any video call platform
- The user has obtained verbal consent from participants to record

### 4.2 Main Flow

**Step 1 — Start recording.**
The user starts mena via CLI, specifying the meeting language. mena displays a one-to-two line consent disclaimer reminding the user that consent is their responsibility. mena begins capturing two audio streams: the local microphone input (the user's voice) and the system audio monitor (everything output by the call platform). Streams are recorded with segmented disk writes for crash safety.

**Step 2 — Meeting in progress.**
The user participates in the meeting normally. Optionally, the user takes notes in a separate markdown file. mena is invisible during this phase — it records, nothing more.

**Step 3 — Stop recording.**
The user stops mena via CLI. The complete audio recording is finalized on disk.

**Step 4 — Post-meeting processing.**
mena processes the recording automatically:
- The local microphone channel is the user's voice; the system audio channel contains the remote participants
- The local channel is transcribed with all speech attributed to the user (no diarization needed)
- The remote channel is transcribed and diarized to identify and label other participants
- Both transcription outputs are merged by timestamp into a single speaker-attributed transcript
- Segments with low transcription confidence, low diarization confidence, or speaker overlap are flagged

**Step 5 — Interactive correction.**
mena presents flagged segments to the user one at a time in the CLI. Each flagged segment shows:
- A rough timestamp
- The text of the flagged segment
- The preceding and following context (roughly a paragraph each)
- The reason for flagging (low transcription confidence, low speaker confidence, overlap)
- For speaker attribution flags: the candidate speakers

The user can:
- Correct the speaker assignment
- Edit the transcribed text
- Listen back to the original audio at the flagged timestamp
- Skip the flag
- Mark a segment as overlap

When all flags are processed (or the user exits early), the corrected transcript is finalized. The source transcript is preserved unchanged; corrections are stored as a separate layer applied on top.

**Step 6 — Summarization.**
mena takes the corrected transcript and, if the user provides a path to their manual notes file, the manual notes as inputs. mena generates a structured summary containing, in this order:
1. A timeline of the meeting — a chronological account of what was discussed
2. Most important points — key takeaways, weighted by alignment with manual notes where provided
3. Action items — extracted from the transcript and reconciled with manual notes where provided
4. A general summary of the meeting

`[OPEN]` Whether the general summary (item 4) creates redundancy with the timeline (item 1) is an open question. Both may cover similar ground at different levels of abstraction. This needs to be evaluated against actual output once the pipeline produces real results. The resolution may be to merge them, to differentiate them by purpose (chronological record vs. high-level takeaway), or to make the general summary optional.

**Step 7 — Output.**
mena produces a well-formatted markdown file as the primary deliverable, containing the generated summary. Separately available to the user (as individual files or clearly separated sections):
- The raw corrected transcript with speaker labels and timestamps
- The user's original manual notes (unmodified)

All three artifacts are transparently accessible. The generated summary is the primary working output; the transcript and notes exist for traceability and the user's discretion.

### 4.3 Postcondition

The user has the meeting notes on their local machine. Recording, transcription, diarization, audio, speaker data, and speaker-sensitive artifacts have remained local-only. If an approved direct ZDR Mistral route was used for LLM-dependent text stages, the resulting artifacts record that route use for provenance.

### 4.4 Alternative Flows

**4.4a — No manual notes provided.**
Step 6 proceeds with the corrected transcript only. The summary is generated without manual note influence. The output contains no manual notes section.

**4.4b — Diarization skipped.**
The user chooses not to diarize (e.g., a 1-on-1 call where speaker identity is obvious from context). Step 4 transcribes both streams but does not run diarization on the remote stream. Speaker labels default to "Local" and "Remote." Step 5 may still flag low-confidence transcription segments. *Per [DISC §4.1], diarization is opt-in per meeting.*

**4.4c — Processing failure mid-pipeline.**
Transcription or diarization fails partway through processing. The recording on disk is unaffected. The user can re-run processing from the point of failure without reprocessing already completed segments.

**4.4d — Recording interrupted by system failure.**
The system crashes, the laptop loses power, or the audio device disconnects during recording. All audio segments flushed to disk prior to the failure are preserved. The maximum data loss is limited to the current unflushed segment. The user is informed of the interruption and can process the partial recording.

---

## 5. Functional Requirements

`[REQ-F01]` **Dual-stream audio capture.** mena shall capture the local microphone input and the system audio monitor as two separate channels simultaneously. *Traces to [AUDIO §Online Meetings].*

`[REQ-F02]` **Segmented recording.** mena shall write audio to disk in segments (not as a single file written at recording end) to limit data loss in case of failure.

`[REQ-F03]` **Language selection.** The user shall specify the meeting language when starting a recording. The language selection determines which transcription model configuration and lexicon are loaded.

`[REQ-F04]` **Consent disclaimer.** mena shall display a one-to-two line disclaimer upon starting a recording, stating that the user is responsible for obtaining consent from meeting participants.

`[REQ-F05]` **Channel-aware processing.** mena shall recognize that the local microphone captures the user's voice and the system audio captures remote participants. The user's speech shall be attributed to the user without diarization. Diarization (when enabled) shall apply only to the remote participants' audio. *Traces to [AUDIO §Better Approach].*

`[REQ-F06]` **Transcript merging.** mena shall merge the local and remote transcripts into a single speaker-attributed transcript, aligned by timestamp.

`[REQ-F07]` **Confidence-based flagging.** mena shall flag transcript segments with low transcription confidence, low speaker attribution confidence, or detected speaker overlap for human review.

`[REQ-F08]` **Semantic coherence flagging.** mena shall flag transcript segments where the transcribed text is semantically incoherent in context, regardless of transcription confidence scores. This addresses a class of errors where the transcription model produces acoustically plausible but contextually nonsensical text — particularly relevant for dialectal input (e.g., Swiss German transcribed as standard German) but applicable to any language. The mechanism shall be language-agnostic. The frequency of such errors is expected to vary by language and dialect distance from the transcription model's training data.

`[REQ-F09]` **Interactive correction.** mena shall present flagged segments to the user one at a time, showing the flagged text, surrounding context, timestamp, flag reason, and candidate speakers where applicable. The user shall be able to correct, skip, or reassign each flag. *Traces to [DISC §5.2].*

`[REQ-F10]` **Audio playback during correction.** The user shall be able to listen to the original audio at the timestamp of a flagged segment during the correction step.

`[REQ-F11]` **Source transcript preservation.** Corrections shall be stored as a separate layer. The original uncorrected transcript shall be preserved unchanged. *Traces to [DISC §5.3], decision D17.*

`[REQ-F12]` **Summarization with manual notes.** mena shall accept an optional path to the user's manual notes file. When provided, the summarization step shall use both the corrected transcript and the manual notes as input. Manual notes influence importance weighting and action item extraction.

`[REQ-F13]` **Structured output.** The primary output shall be a well-formatted markdown file containing, in this order: (1) a meeting timeline, (2) most important points weighted by manual notes where provided, (3) action items, and (4) a general summary. The relationship between the timeline and the general summary — specifically whether they create redundancy — is an open question to be resolved against actual output. The raw corrected transcript and the user's manual notes (unmodified) shall be separately available as distinct artifacts.

`[REQ-F14]` **Diarization opt-in.** Diarization shall be optional per meeting. When disabled, speaker labels default to "Local" and "Remote." *Traces to [DISC §4.1], decision D11.*

`[REQ-F15]` **Speaker limit.** When diarization is enabled, the system shall support up to 3 remote speakers (4 total including the local user). *Traces to [DISC §4.1], decision D12.*

`[REQ-F16]` **Overlap handling.** When speaker overlap is detected, the segment shall be flagged explicitly rather than force-attributed to a single speaker. *Traces to [DISC §4.1], decision D14.*

`[REQ-F17]` **Resumable processing.** If processing fails, the user shall be able to resume from the point of failure without reprocessing already completed segments. The recording shall remain intact regardless of processing failures.

`[REQ-F18]` **Voice embedding storage.** Corrected speaker labels and their associated voice embeddings shall be stored persistently, even though speaker profile matching is not part of the PoC. This is a low-cost forward-compatibility measure. *Traces to [DISC §4.3], decision D18.*

---

## 6. Non-Functional Requirements

### 6.1 Reliability

`[REQ-R01]` **Recording robustness.** Recording is the most robust component of the system. No failure in any downstream component (transcription, diarization, summarization, correction) shall affect, corrupt, or cause loss of the audio recording. This is the single non-negotiable quality attribute of the product. *This requirement takes precedence over all other non-functional requirements in case of conflict.*

`[REQ-R02]` **Bounded data loss on recording failure.** In the event of a system crash, power loss, or audio device disconnection during recording, the maximum data loss shall be limited to the current unflushed audio segment. All previously flushed segments shall be intact and processable.

`[REQ-R03]` **Recording isolation.** The recording process shall be architecturally isolated from all processing logic. Recording writes to disk; processing reads from disk. These are not the same operation and shall not share failure modes.

### 6.2 Usability

`[REQ-U01]` **CLI interface for PoC.** All user interaction for the PoC (start, stop, correction, output retrieval) shall be through a command-line interface. The CLI is chosen because it is the fastest path to a testable product, not because it is the long-term interface. The architecture shall separate the user interface from the core logic such that replacing the CLI with a different interface (web UI, GUI, chat-based) does not require changes to the core pipeline.

`[REQ-U02]` **Exception-first correction.** The correction step shall present only flagged segments by default, not the full transcript. The user reviews exceptions, not the entire output. Full transcript access is available but not the default. *Traces to [DISC §5.2].*

### 6.3 Performance

`[REQ-P01]` **Local acceleration and accepted runtime route.** For local processing, mena shall use GPU acceleration when a compatible GPU is available. CPU may serve as an accepted local fallback when GPU is unavailable or has insufficient resources. For LLM-dependent text stages only, the accepted setup/runtime policy may instead choose or fall back to an approved direct ZDR Mistral route per CON-01 and CON-03. Processing shall use accepted stage-specific runtime profiles and fallback authorization, not ad hoc provider selection during processing.

`[REQ-P02]` **Meeting duration.** mena shall support continuous recording of meetings up to 90 minutes without degradation.

`[REQ-P03]` **Processing time.** No formal latency requirement for the PoC. Post-meeting processing takes as long as it takes. The requirement is that processing completes without manual intervention between automated steps (transcription, diarization, merging, summarization), except when the accepted setup/runtime policy requires explicit fallback authorization. The correction step remains the expected interactive review pause.

### 6.4 Supportability

`[REQ-S01]` **Configurable lexicon.** The user shall be able to provide a personal lexicon (word list) to improve transcription accuracy for domain-specific or dialectal vocabulary (e.g., Swiss German terms).

---

## 7. Constraints

`[CON-01]` **Data sovereignty.** All processing shall occur locally on the user's machine by default. No audio, transcript, speaker embedding, or derived data shall be processed on infrastructure outside European jurisdiction or under non-European legal authority (e.g., US CLOUD Act, FISA). For LLM-dependent text stages (semantic coherence flagging and summarization), direct ZDR Mistral is an acceptable approved route when the provider, account/workspace posture, and infrastructure satisfy the European-jurisdiction and Zero Data Retention constraints. The route may be accepted as primary, as part of an ordered fallback chain, or as the only viable route when no local LLM profile is viable. A US company operating European data centers does not satisfy this constraint unless the contractual and legal structure genuinely isolates the European operation from non-European legal reach. The recording, transcription, and diarization pipeline remains strictly local regardless. *Traces to [DISC §8].*

`[CON-02]` **Target platform.** PoC targets Linux only. *Cross-platform support is [SCOPE-OUT 9] / [POST-POC D].*

`[CON-03]` **No non-European service dependencies at runtime.** Recording, transcription, and diarization shall not require internet connectivity or external API calls. Semantic coherence flagging and summarization prefer local processing by default but may use the approved direct ZDR Mistral route per CON-01 when the accepted stage-specific runtime profile and authorization policy allow it. Model weights and dependencies are downloaded at install time, not at runtime.

`[CON-04]` **Meeting-tool agnostic.** mena captures audio at the operating system level. It shall not depend on any specific video call platform.

---

## 8. Definition of Done — PoC

The PoC is complete when:

1. **The primary use case runs end to end.** A real online meeting is recorded, transcribed, diarized, corrected, and summarized, producing a markdown output that the user considers useful and trustworthy.

2. **The correction step works.** Flagged segments are presented interactively, the user can correct them, and the corrections are reflected in the final output.

3. **Recording survives a simulated failure.** Processing is intentionally interrupted mid-run (e.g., process killed during transcription). The recording is confirmed intact on disk. Processing resumes from the point of failure and completes successfully.

4. **The user is satisfied with the output.** This is a human judgment, not a metric. The user (initially OG, then invited testers) considers the final meeting notes accurate enough, structured enough, and useful enough to replace their current manual process.

---

## 9. Scope Evolution Log

This section records how scope decisions evolved between the discovery meeting (April 8, 2026) and this requirements session (April 14, 2026). The purpose is traceability, not contradiction — the customer's thinking narrowed through conversation.

| Topic | Discovery doc position | Requirements session position | Reasoning |
|---|---|---|---|
| PoC meeting scenario | In-person, single external mic [DISC §2.1] | Online meeting, headset, dual-stream audio | Reflects the customer's realistic first usage scenario |
| OpenClaw integration | Primary integration target [DISC §6] | Out of scope; product stands alone [SCOPE-OUT 1] | Narrower PoC boundary; OpenClaw wrapper is personal convenience, not product scope |
| PoC starting path | Path 1: Meetily + OpenClaw [DISC §9.2] | Custom pipeline; no dependency on Meetily | Follows from product standing on its own |
| Diarization approach | Full diarization on mixed audio | Split-channel: diarization only on remote stream | Dual-stream capture means local speaker is known; only remote stream needs diarization [AUDIO] |
| Correction UI | Three candidates: terminal, web UI, OpenClaw chat [DISC §5.4] | CLI (terminal) | Fastest path to working product; same surface as recording trigger |
| Data sovereignty | Strictly local, no data leaves hardware [DISC §8] | Local default; approved direct ZDR Mistral route acceptable for LLM-dependent text stages | Some users may lack a viable local LLM profile or may accept remote LLM processing for summarization and semantic coherence flagging; sovereignty is about jurisdiction, legal posture, ZDR, and explicit authorization, not only physical location |

---

## 10. Decisions Made During This Session

| # | Decision | Reasoning |
|---|---|---|
| RS-D1 | PoC scenario is online meeting with headset | Reflects the customer's realistic first use case |
| RS-D2 | Product stands alone; no agent harness dependency | Cleaner PoC boundary; wrappers are user's concern |
| RS-D3 | Consent is user responsibility; product shows disclaimer only | Sufficient for PoC with known testers; consent as product feature is post-PoC |
| RS-D4 | Language specified by user at recording start; no auto-detect | Simpler and more reliable for PoC |
| RS-D5 | Swiss German transcription with personal lexicon is sufficient for PoC | Tested sufficiently for personal use; hardening is post-PoC |
| RS-D6 | CLI for all interaction (trigger, correction, output) | Fastest path; correction and trigger in same surface |
| RS-D7 | 90-minute meeting duration cap for PoC | Realistic meeting length; longer meetings have natural break points |
| RS-D8 | No formal processing time requirement | Takes as long as it takes; benchmarking will establish actuals |
| RS-D9 | Local GPU preferred; CPU may be an accepted local fallback; LLM-dependent text stages may use an accepted direct ZDR Mistral route | Preserve local acceleration for capable machines while allowing stage-specific accepted runtime routes and ordered fallback chains |
| RS-D10 | Recording robustness is the single non-negotiable quality attribute | No downstream failure may affect the recording |
| RS-D11 | Linux only for PoC | Cross-platform is a commercial product decision |
| RS-D12 | Diarization only on remote channel; local speaker is known | Dual-stream architecture gives structural speaker separation |
| RS-D13 | Streaming transcription is post-PoC, serves as recording health signal | Important for trust but not required to validate the core pipeline |
| RS-D14 | Semantic coherence flagging is language-agnostic, not Swiss German-specific | The mechanism applies to any language; error frequency varies by dialect distance from the transcription model's training data |
| RS-D15 | Sovereignty constraint refined: local default; approved direct ZDR Mistral route acceptable for LLM-dependent text stages | Direct ZDR Mistral may be accepted as primary, fallback, or only viable route for summarization and semantic coherence flagging; local fallback authorization never authorizes a later remote transition; US providers with EU data centers do not satisfy the constraint unless jurisdictionally isolated |

---

## Document Metadata for Agent Consumption

```yaml
document_type: poc_requirements_specification
product_name: mena
cli_command: mena
version: "1.0"
date: 2026-04-14
customer: OG (Oriol Gut)
requirements_engineer: Claude Opus 4.6
status: draft_for_customer_review
companion_documents:
  discovery_meeting:
    title: "Preliminary Discovery Meeting Report — AI-Backed Personal Meeting Notes"
    date: 2026-04-08
    role: reasoning_record_and_design_exploration
  audio_experiment:
    title: "Meeting Recording: Dual-Stream Architecture Notes"
    role: technical_feasibility_evidence
poc_scope:
  meeting_type: online
  audio_capture: dual_stream_local_mic_and_system_audio
  processing: local_default_post_meeting
  diarization: opt_in_remote_channel_only
  speaker_limit: 4_total_including_local_user
  max_duration_minutes: 90
  language_selection: manual
  interface: cli
  target_platform: linux
  primary_output: structured_markdown
sovereignty: local_default_direct_zdr_mistral_route_for_llm_dependent_text_stages_when_accepted
functional_requirements_count: 18
non_functional_requirements_count: 7
constraints_count: 4
scope_exclusions_count: 10
decisions_count: 15
definition_of_done_criteria_count: 4
post_poc_priorities:
  - streaming_transcription_as_health_signal
  - in_person_meeting_support
  - speaker_profile_learning
  - cross_platform_support
```

---

*End of document. This requirements specification reflects the customer's stated needs as of April 14, 2026. It builds on the discovery meeting of April 8, 2026 and records all scope evolutions explicitly. All requirements are traceable to their origin. The document is structured for both human review and machine consumption.*
