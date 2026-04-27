# Preliminary Discovery Meeting Report
## AI-Backed Personal Meeting Notes — Product Concept

---

**Document type:** Preliminary discovery meeting summary  
**Format:** Markdown — dual-purpose: human-readable and machine-readable for agent consumption  
**Date of conversation:** April 8, 2026  
**Claude knowledge cutoff:** August 2025  
**Participants:** OG (Oriol Gut, customer) · Claude Sonnet 4.6 (solutions architect role)  
**Status:** Initial draft — decisions are preliminary, revisable, and explicitly marked as such  
**Companion reference:** A separate session covered the broader AI model landscape (open vs. closed models, architecture types, model selection framework). Where this report references specific model choices, the reasoning traces back to that session. A brief summary of the relevant conclusions from that session is included in the appendix at the end of this document rather than in the main body.

---

## How to Read This Document

This document is structured for two audiences: humans revisiting a design conversation, and AI agents consuming it as context for future decisions. The following conventions apply throughout.

**Attribution markers:**

- `[OG]` — Statement, constraint, preference, or question originating from the customer. Where OG acknowledged limited technical depth on a specific point, this is noted inline.
- `[Claude]` — Analysis, suggestion, option framing, or synthesis originating from the AI. Treat with appropriate skepticism regarding recency; Claude's knowledge cutoff is August 2025.
- `[DECISION]` — A point where a choice was made. The reasoning is recorded so future agents or humans can evaluate whether the original logic still holds. Decisions are revisable.
- `[OPEN]` — A question or dependency explicitly left unresolved at the time of this conversation.
- `[⚠ cutoff risk]` — A specific claim particularly sensitive to post-August-2025 developments.
- `[OG-tech-caveat]` — A technical claim made by OG where limited technical knowledge was acknowledged or inferred; verify before treating as fact.

**What this document is not:** This is not a requirements specification. It is not an architectural diagram. It is a record of a preliminary discovery conversation between a customer with clear goals and a solutions architect. The purpose is to preserve reasoning, not just conclusions.

---

## Section 1: The Problem and the Inspiration

### 1.1 What OG Wants to Build

`[OG]` The idea is a personal AI-assisted meeting notes system — a pipeline that captures a meeting as it happens, produces a structured output (summary, action items, speaker attribution), and integrates the result into OG's personal AI assistant ecosystem. The primary integration target is OpenClaw, OG's existing self-hosted personal AI assistant.

`[OG]` The inspiration is Notion AI Meeting Notes. OG is not interested in Notion as a platform — his work does not happen in Notion, and paying for a Notion subscription specifically to access the meeting notes functionality is disproportionate. The inspiration is functional: what Notion AI does with a meeting, not how Notion does it or where.

`[OG]` The core functional requirement is that this is not the same as sending a voice memo to a chat assistant after the fact. The intent is a continuous capture process that runs during the meeting and processes the full stream of what was said.

### 1.2 Why Notion AI Is Not the Answer

`[OG]` Two primary objections, distinct from each other:

**Data sovereignty.** Notion processes audio on US cloud infrastructure, using OpenAI and Anthropic APIs (officially confirmed by Notion). For OG, this is a hard blocker. No audio, transcript, or derived data should leave OG's hardware or European/Swiss infrastructure without explicit choice. This is not primarily a cost concern — it is a data sovereignty and privacy stance, relevant to both Swiss nDSG (new Data Protection Act) and GDPR applicability to meeting participants.

**Platform mismatch.** Notion is not OG's primary workspace. The value of Notion AI Meeting Notes depends on living inside Notion — linking notes to tasks, databases, people. That integration value is absent if OG is not working in Notion.

`[DECISION]` Notion AI is eliminated as a solution. It is retained as a reference point for functional inspiration only.

### 1.3 What Notion AI Actually Does — as a Reference

`[Claude]` Based on Notion's public documentation and independent reviews, fetched during this conversation. `[⚠ cutoff risk]`

- The Notion desktop app uses OS-level audio APIs to capture microphone and system audio simultaneously
- Audio is sent to Notion's cloud servers; transcription is performed server-side using OpenAI and Anthropic APIs
- A live transcript appears in the Notion page in near-real-time during the meeting
- After the meeting ends, AI generates a structured summary with action items
- **Speaker diarization is not implemented** — no speaker labels exist; the transcript is one continuous block of text
- No bot joins the meeting; it captures whatever the computer is playing and recording
- Works for video calls (via system audio capture) and in-person meetings (via microphone)

`[Claude]` Notion's approach is architecturally simple from their perspective: all compute offloaded to cloud, no local processing required. This is exactly the tradeoff OG is rejecting.

**Key gap in Notion AI:** No speaker attribution. This is noted because OG's product concept explicitly includes it — making it a meaningful differentiation target even relative to the inspiration.

---

## Section 2: Understanding the Pipeline

### 2.1 The Critical Distinction — Streaming vs. File-Based

`[OG]` OG correctly identified early in the conversation that continuous meeting capture is architecturally different from processing a voice memo after the fact. He requested clarification on the technical difference. `[OG-tech-caveat]` OG acknowledged limited experience with audio pipeline technical details.

`[Claude]` The core distinction:

**File-based (post-processing):**
- Complete, finite audio artifact exists before processing begins
- Full context available — speaker diarization can compare voice embeddings from minute 2 with minute 47 to confirm identity
- Can process in large batches with no time constraint
- Higher accuracy for both transcription and diarization
- Stateless processing — no state management across the run
- Simple failure model — process crashes, restart on the same file

**Live streaming:**
- Hard real-time constraint — must keep pace with incoming audio
- Chunking required: audio must be cut into processable segments; speech does not respect chunk boundaries
- Diarization significantly harder — cannot look ahead, only backward; early speaker assignments are tentative
- For video calls: two audio streams must be captured simultaneously (microphone + system audio from the call platform)
- Continuous state management required across full meeting duration
- Failure risk: if the process crashes, content since the last disk flush is lost with no recovery path
- Higher resource competition during video calls — transcription competes with call software for CPU/GPU

`[OG]` OG specified that for the PoC, the target context is an in-person meeting with a single external microphone. This simplifies the signal chain significantly:
- Single audio stream — no system audio mixing required
- Better acoustic conditions than a compressed video call stream
- No competing video call software during processing
- More favorable conditions for speaker diarization accuracy

`[DECISION]` PoC scope is in-person meeting with external microphone, single audio stream. Video call scenarios and multi-source audio mixing are explicitly deferred to a later phase.

### 2.2 The Hybrid Architecture

`[Claude]` Three processing modes were presented as options:

**Option A — Fully real-time:** Transcription and diarization both run live during the meeting. Highest complexity, highest real-time compute demand, diarization accuracy limited by inability to look ahead.

**Option B — Fully post-processing:** Record the complete meeting, process everything after it ends. Simplest, most accurate, but no live transcript during the meeting.

**Option C — Hybrid:** Transcription runs in near-real-time during the meeting (producing a rolling live transcript). Diarization runs as a batch job on the complete recording after the meeting ends. The two outputs are then aligned by timestamp to produce a fully labeled transcript, which proceeds to the correction step and then LLM summarization.

`[DECISION]` The hybrid architecture was chosen. Reasoning:

- OG does not need real-time speaker labels during the meeting; the primary output is post-meeting notes
- Batch diarization on the complete recording is significantly more accurate than incremental chunked diarization
- During the meeting, only transcription runs on the GPU — VRAM pressure is low and predictable
- OOM risk during the meeting is minimized by not running diarization concurrently
- The correction step fits naturally after batch diarization, on the complete labeled transcript, before it goes to the LLM

`[OG]` OG specifically emphasized that OOM during a live meeting is a critical, unacceptable failure mode. Content since the last disk flush would be lost with no recovery path.

### 2.3 The Full Pipeline

```
DURING MEETING
──────────────
External mic
→ Audio capture (single stream)
→ Chunked recording (40-second chunks, 2-3 second overlap for boundary handling)
→ Transcription [faster-whisper medium]
→ Chunk flush to disk
   (timestamped transcript segments, speaker attribution marked "pending")
→ Repeat until meeting end

POST MEETING — BATCH DIARIZATION (opt-in)
──────────────────────────────────────────
Complete audio recording
→ Diarization pipeline [WhisperX with pyannote]
→ Speaker-labeled timeline with confidence scores
→ Merge with chunked transcript (timestamp alignment)
→ Flag low-confidence segments and overlap segments

CORRECTION STEP (opt-in)
─────────────────────────
Flagged segments presented to user (exception-first UX)
→ User reviews flagged segments with paragraph-before and paragraph-after context
→ User assigns or corrects speaker labels
→ Correction stored as diff (source transcript preserved unchanged)
→ Named speaker labels stored as voice profile anchors for future meetings

FINALIZATION
────────────
Corrected labeled transcript
→ LLM summarization (small open-weight model, low temperature, structured output)
→ Action item extraction with owner attribution where speaker is confirmed
→ Structured output → OpenClaw memory
→ Optional: push to storage (kDrive or equivalent)
```

---

## Section 3: Component Decisions

### 3.1 Transcription — faster-whisper

`[Claude]` faster-whisper is an optimized implementation of OpenAI's Whisper speech recognition model, using CTranslate2 for faster inference. It runs fully locally with no API key and no outbound calls. It is actively maintained and stable. `[⚠ cutoff risk]`

Model size selection affects both accuracy and VRAM usage:

| Model | VRAM | Notes |
|---|---|---|
| large-v3 | ~3GB | Most accurate; appropriate for hosted GPU environments |
| medium | ~2GB | Recommended for PoC on OG's hardware |
| small | ~1GB | Acceptable fallback if VRAM pressure increases |

`[OG]` OG decided on faster-whisper medium for local PoC testing, explicitly trading some accuracy for VRAM headroom on the RTX 3060 laptop (6GB VRAM). Large-v3 remains the target for a future hosted environment with a stronger GPU. This decision traces back to the model landscape conversation — the reasoning for preferring locally runnable open-weight models over cloud APIs is summarized in the appendix.

`[DECISION]` faster-whisper medium for PoC. Large-v3 for future hosted environment.

### 3.2 Diarization — pyannote via WhisperX

`[Claude]` Two components are involved:

**pyannote.audio:** The diarization engine. Takes audio, returns a timeline of "who spoke when" with anonymous speaker IDs and confidence scores. Operates on audio, not text. Works by analyzing voice characteristics (pitch, timbre, speaking rhythm) to create voice embeddings — numerical fingerprints — per speaker, then clustering audio segments by which embedding they match. `[⚠ cutoff risk]`

Requires a HuggingFace API token for the initial model download (gated model). After download, inference is fully local — no outbound calls during runtime. For strict sovereignty: download once on a trusted connection, store locally, disable auto-update. This is a setup-time dependency, not a runtime dependency.

**WhisperX:** An orchestration layer that combines faster-whisper (transcription) and pyannote (diarization) and handles the timestamp alignment between their outputs. `[⚠ cutoff risk]`

```
WhisperX
├── faster-whisper (transcription engine)
├── pyannote (diarization engine)
└── alignment logic (maps speaker timeline to transcript timestamps)
```

WhisperX constraints to be aware of:
- Maintained by a single developer; history of breaking when faster-whisper or pyannote release updates
- Pins specific dependency versions — you get whichever versions WhisperX decided on, not current
- Alignment logic is fixed; cannot be tuned without forking the project
- Large fixed dependency tree; potential version conflicts

`[DECISION]` Use faster-whisper standalone for live chunked transcription during the meeting (stable, no diarization overhead, not version-locked to WhisperX). Use WhisperX for the post-meeting batch diarization pass only. This limits WhisperX to the lower-risk post-meeting path where it is not under real-time pressure and can be re-run if it fails.

`[OPEN]` If WhisperX proves unstable or constraining during implementation, the fallback is to wire faster-whisper and pyannote directly with a custom alignment layer. This is more engineering work but gives full control over the alignment logic and dependency versions.

### 3.3 Chunking Strategy

`[OG]` OG accepted 40-second chunks as the PoC target. Rationale: more generous VRAM headroom, and longer chunks give pyannote more audio context per segment — which improves speaker embedding quality and therefore diarization accuracy.

`[OG]` OG asked whether faster-whisper and pyannote need to use the same chunk length. This is a sharp architectural observation.

`[Claude]` No — they do not need the same chunk length. They consume the same audio stream independently. The only constraint is that pyannote's chunks must cover at least the same audio as whisper's chunks (or a multiple thereof) to maintain timestamp alignment coherence. Decoupled chunk sizes are the right architecture:

```
Example:
faster-whisper: 20-second chunks → low transcript latency
pyannote:       60-second chunks → better speaker embedding quality

pyannote's 60-second window covers whisper's first three 20-second chunks.
Whisper output appears every 20 seconds.
Speaker labels back-annotate those chunks when pyannote finishes its 60-second segment.
```

`[DECISION]` For the hybrid PoC (diarization post-meeting), decoupled chunk sizing is a useful future optimization but not immediately necessary. During the meeting, transcription runs at 40-second chunks only. Post-meeting diarization runs on the complete audio. Decoupled chunk sizing is noted as the right approach if a future version moves to near-real-time diarization.

### 3.4 VRAM Management and OOM Prevention

`[OG]` Hardware: Tuxedo Polaris 15 Gen3, RTX 3060 laptop (6GB VRAM), 32GB RAM.

`[Claude]` The RTX 3060 laptop variant is the binding hardware constraint. 6GB VRAM must be managed deterministically.

**During meeting (transcription only):**

| Component | VRAM | Status |
|---|---|---|
| faster-whisper medium | ~2GB | Running |
| pyannote | 0 | Not loaded |
| Headroom | ~4GB | Available |

Comfortable. Thermal throttling under sustained load is a separate concern (see benchmarking, Section 5).

**Post-meeting (diarization batch):**

| Component | VRAM | Status |
|---|---|---|
| faster-whisper | 0 | Unloaded |
| pyannote via WhisperX | ~1-2GB | Running |
| Headroom | ~4GB | Available |

Also comfortable.

**Hard VRAM boundary enforcement pattern:**

```
load whisper → transcribe chunk → explicitly unload whisper → flush to disk → repeat
[meeting ends]
load pyannote/WhisperX → diarize complete recording → explicitly unload → proceed
```

Explicit model loading and unloading between stages. Slower due to load/unload overhead but deterministic — no surprises during a 90-minute meeting. Garbage collection is not relied upon.

`[DECISION]` Sequential loading/unloading enforced. No concurrent model loading during the meeting.

### 3.5 LLM Summarization

`[OG]` OG proposed low temperature and low top-k for the summarization LLM, reasoning that determinism is appropriate — the goal is faithful extraction of what was said, not creative generation.

`[Claude]` Correct in direction. Refinements:

- Low temperature (0.1-0.3) reduces hallucination risk but very low temperature risks making the model overly extractive — reproducing transcript fragments rather than synthesizing. A temperature of 0.2-0.3 with space for paraphrasing is a better starting point than 0.1.
- **top-p (nucleus sampling) is more appropriate than top-k for this task.** top-k cuts at a fixed candidate count regardless of probability distribution. top-p adjusts dynamically based on cumulative probability mass, producing more coherent output for generative summarization. Recommended starting configuration: temperature 0.2-0.3, top-p 0.9.
- A small quantized open-weight model (7B-8B) is sufficient for meeting summarization given clean transcript input. This is a low-complexity LLM task — the input is structured text, the output is structured extraction. Larger models do not meaningfully improve quality here.

`[DECISION]` Summarization uses a small quantized open-weight model (Qwen3 8B or equivalent) via Ollama on OG's laptop, CPU inference acceptable since this runs post-meeting with no latency pressure. Temperature 0.2-0.3, top-p 0.9 as starting configuration, to be tuned against actual output quality.

The rationale for using a locally runnable open-weight model rather than a frontier API for this task is summarized in the appendix.

---

## Section 4: Diarization Design Decisions

### 4.1 Core Diarization Parameters

`[OG]` **Speaker limit:** Maximum 4 speakers for the PoC. Diarization accuracy degrades with speaker count; 4 is well within pyannote 3.0's reliable range. `[OG]` stated this reflects OG's actual meeting patterns.

`[OG]` **Blind diarization:** No pre-enrollment of voice profiles for the PoC. The system determines speaker count and assigns anonymous labels (SPEAKER_A, SPEAKER_B, etc.). Speaker verification by enrolled profile is a future feature.

`[OG]` **Overlap handling:** Explicit overlap labeling is preferred over forced attribution. If two speakers talk simultaneously, the segment should be marked as overlap rather than assigned to either speaker. Mislabeling is more dangerous than acknowledging uncertainty. Low confidence score flagged and surfaced to the user; not silently overridden.

`[OG]` **Diarization is opt-in per meeting.** Not all meetings require speaker attribution. OG wants to monitor the diarization space for new developments and not be locked into a fixed implementation as the primary mode. The specific operating context for diarization is in-person meeting with a good external microphone and little-to-no overlapping speech. This is user context, not a technical requirement.

`[DECISION]` Blind diarization, 4-speaker limit, overlap explicit, opt-in per meeting. These are PoC parameters — all revisable.

### 4.2 The Name Introduction Convention

`[OG]` OG proposed that participants briefly state their names and goals at the start of a meeting. This is good meeting hygiene independently of any technical benefit.

`[Claude]` This convention is technically elegant. It sidesteps the hardest part of speaker verification — enrollment — by using the meeting itself as the registration session. The pipeline can:

```
Start of meeting audio
→ Detect name introduction segment ("Hi, I'm Oriol...")
→ Extract voice embedding linked to stated name
→ Store as initial voice profile anchor
→ Use as reference during post-meeting diarization alignment
→ Correction step refines and confirms
→ Profile persists for future meetings
```

A name stated by a speaker links their voice embedding to a real name with zero user friction — no separate enrollment session required.

### 4.3 Speaker Profile Learning — Long-Term Vision

`[OG]` OG raised the idea that corrections made in the correction step should feed back into improving future diarization — specifically that frequent meeting participants could be identified more accurately over time, eventually creating a blended blind/enrolled approach. OG noted: most likely he himself appears in every meeting, and he frequently meets with the same people. Identifying him and his recurring collaborators should become straightforward over time.

`[Claude]` This is technically feasible and is precisely how commercial products like Otter.ai work (US company, US cloud infrastructure — relevant here only as a technical reference, not as a product OG would use). The building blocks exist in pyannote.

**Technical mechanism:**

Diarization already produces voice embeddings per speaker. Currently in the PoC design, these are anonymous and discarded after each meeting. The correction step where OG assigns "SPEAKER_A is Oriol" creates a named label for that embedding. If labeled embeddings are stored persistently:

- A voice profile per person builds over time as a cluster of embeddings from different meetings and conditions
- Next meeting: compare incoming embeddings against stored profiles. Match above threshold → named attribution automatically. Below threshold → blind fallback
- Profile robustness increases with each confirmed meeting — different microphones, room acoustics, emotional states all contribute to a more robust cluster
- The blended blind/enrolled approach OG described is the natural end state: known speakers identified automatically, unknown speakers fall back to SPEAKER_X labeling

`[DECISION]` Speaker profile learning is **not** part of the PoC. However, the architecture must not preclude it. The correction step output — the diff mapping SPEAKER_X to real names and the associated embeddings — must be stored and not discarded, even before the matching logic is built. This is a low-cost forward-compatibility decision. Historical labeled embeddings accumulate from day one; the matching logic can be added later without needing to re-process past meetings.

`[Claude]` Privacy note for agent and human readers: voice profiles stored locally are biometric data. For a personal local system where OG controls all data, this is not a concern. If this became a multi-user or shared system at any point, explicit per-person consent handling would be required before enrolling any participant's voice. Named here for transparency; does not affect PoC design.

---

## Section 5: The Correction Step

### 5.1 Purpose and Framing

`[OG]` A human correction step should be implemented, particularly for the PoC. The correction step is where quality assurance and trust-building happen. Diarization will make mistakes; the correction step is the mechanism for catching them before they propagate into OpenClaw's memory and future meeting summaries.

`[OG]` The correction step is opt-in, not mandatory.

### 5.2 Exception-First UX

`[OG]` The UX requirement is specific: the user should primarily be presented with low-confidence segments and roughly the paragraph before and after. Reading the whole transcript, or more than that immediate context, should be opt-in. The goal is to avoid the user feeling overwhelmed by being confronted with the full transcript for review.

`[Claude]` This is the "exception-first review" pattern. The user sees only what needs attention, with just enough context to make a confident decision.

**What a flagged segment looks like in the correction interface:**

```
⚠️  LOW CONFIDENCE  [00:14:23 - 00:14:31]

... [paragraph before — always shown] ...
"So the timeline we agreed on last week still stands."

❓ "We could push it."
   Confidence: 0.41
   Likely: SPEAKER_A or SPEAKER_C
   [ SPEAKER_A ] [ SPEAKER_B ] [ SPEAKER_C ] [ SKIP ] [ OVERLAP ]

... [paragraph after — always shown] ...
"Right, that gives us more buffer before the review."

─────────────────────────────────────────────────────
⚠️  OVERLAP  [00:22:07 - 00:22:14]

... [paragraph before] ...
"Let me just add one thing."

⚠  Multiple speakers detected — not attributed
   [ ASSIGN TO SPEAKER ] [ KEEP AS OVERLAP ] [ SKIP ]

... [paragraph after] ...
"Agreed, let's move on."
```

Full transcript access is available but not the default view.

**What gets flagged** (in order of presentation priority):
1. Confidence below threshold (PoC default: 0.6, tunable)
2. Overlap segments that could not be attributed
3. Speaker change points where pyannote's certainty was low

### 5.3 Correction Data Model

`[Claude]` The correction produces a diff file — not a rewrite of the full transcript. Only the changed segments with corrected labels are stored. The source transcript is preserved unchanged; the diff is applied on top for downstream processing.

This matters for PoC reliability: if the correction logic has bugs, the source data is not corrupted. The source transcript and the correction diff are two independent artifacts.

Additionally: corrected speaker labels and their associated voice embeddings are stored as voice profile anchors for future meetings (per the profile learning decision in Section 4.3).

### 5.4 Correction UI Format — Unresolved

`[OG]` OG stated he has too little technical knowledge about what format the correction output needs to be in to determine how the feedback should be structured or processed. This is an honest and appropriate deferral — OG needs to see actual output before designing the review interface.

`[OPEN]` Three candidate channels for the correction interface:

1. **Terminal / text editor:** Generated markdown file, human edits labels manually, saved file is the corrected input. Zero UI work, works immediately, poor user experience.
2. **Simple web UI:** Generated HTML page, click to reassign labels per flagged segment, submit. Moderate UI work, much better experience.
3. **Back through OpenClaw via Telegram or WhatsApp:** System sends flagged segments to OG's chat channel, OG replies with corrections in natural language, OpenClaw skill processes the reply. Fits existing workflow; requires a skill to parse natural language corrections; most integrated with the overall system but most engineering work to implement correctly.

`[DECISION]` Correction UI format deferred until a prototype of the flagged segment output exists. OG must see actual output before designing the review interface. The diff-based data model is decided; the interface is not.

---

## Section 6: Integration — OpenClaw

### 6.1 OpenClaw as the Primary Integration Target

`[OG]` OpenClaw is OG's existing self-hosted personal AI assistant and the primary integration target for this product. OG has been running it for over a month at the time of this conversation.

`[Claude]` For readers unfamiliar with OpenClaw: it is an open-source personal AI assistant (openclaw.ai) built by Peter Steinberger and community contributors. It runs on the user's own machine, is accessible via any chat app (WhatsApp, Telegram, iMessage, Discord, Slack), has persistent memory, full system access, and a skills/plugin architecture. It is model-agnostic — supports Claude API, OpenAI API, and local models via Ollama. Not affiliated with Anthropic. `[⚠ cutoff risk]`

The meeting notes pipeline integrates with OpenClaw at two points:

- **Memory:** Finalized meeting summaries and action items are written to OpenClaw's persistent memory, making them available for future conversations and agent tasks
- **Trigger:** Starting and stopping a meeting recording is a natural OpenClaw interaction — via Telegram, WhatsApp, or another connected channel

### 6.2 Existing OpenClaw Skills Relevant to This Pipeline

`[Claude]` As of early 2026, the following skills exist in the OpenClaw ecosystem that are relevant to this pipeline `[⚠ cutoff risk]`:

- **`transcribe` skill:** Local audio transcription using faster-whisper in Docker, no API key, supports MP3, M4A, OGG, WAV, WEBM
- **`transcribee` skill:** Transcription with speaker diarization using ElevenLabs, supports local files and YouTube URLs, outputs speaker-labeled transcripts
- **`meeting-notes` skill:** Takes an audio file, produces structured meeting notes with action items and owners, action item extraction noted as effective in community testing

**What is missing in the OpenClaw ecosystem** as of this conversation: a skill that handles live continuous audio capture during a meeting. The transcription and diarization pieces exist for file-based post-processing; the live capture orchestration does not. This is the engineering gap the PoC must address.

### 6.3 Trigger and Output Destination

`[OPEN]` How OG starts and stops a recording has not been decided. Candidates: OpenClaw command via Telegram, keyboard shortcut on the laptop, companion app button, or a dedicated hardware trigger. The choice affects implementation complexity.

`[OPEN]` After correction and summarization, where does the output go? Candidates: structured markdown file to disk, directly into OpenClaw memory, push to kDrive, or a combination. Primary destination not decided.

---

## Section 7: Integration Horizon — Infomaniak and kMeet

### 7.1 OG's Position on Infomaniak

`[OG]` OG uses Infomaniak for existing hosting infrastructure. Infomaniak is watched as a possible future integration point for two specific reasons:

1. **If Infomaniak integrates meeting notes functionality into kMeet:** This would be relevant as competitive intelligence and inspiration, not necessarily as a dependency.
2. **If the MCP/agent integration surface for kMeet or calendar matures:** This would enable the meeting notes pipeline to interact with meeting metadata (calendar context, participant list from invite, kMeet room info) in an automated way.

`[OG]` OG is not building on Infomaniak kSuite services in the PoC. The watch is forward-looking.

### 7.2 Current State of Infomaniak Agentic Integration

`[Claude]` Web search was performed during this conversation to confirm OG's assessment. `[⚠ cutoff risk]`

**What exists:**
- Infomaniak published an official MCP server for kDrive (`@infomaniak/mcp-server-kdrive` on npm) — file access and management via API token
- A community-built MCP server (daniviber/infomaniak_mcp on GitHub) covers domains, email, web hosting, kDrive, Swiss Backup, VPS

**What does not exist:**
- No MCP server for kMeet
- No MCP server for Infomaniak Calendar
- No MCP server for kChat
- No orchestrated agent-accessible integration across kSuite services

`[DECISION]` OG's assessment confirmed. Infomaniak kDrive is accessible via MCP today; the rest of the kSuite ecosystem relevant to meeting notes (kMeet, calendar, kChat) is not agent-accessible. No PoC dependency on Infomaniak services. Watch the space; no timeline to act on.

---

## Section 8: Data Sovereignty — Full Requirement Statement

`[OG]` Hard requirement: no audio, transcript, speaker embedding, or derived data should leave OG's hardware or European/Swiss infrastructure without explicit choice. This is not primarily a cost concern. It reflects:

- Personal preference and values regarding data ownership
- Swiss nDSG (Neues Datenschutzgesetz, in force since September 2023) applicability
- GDPR applicability to meeting participants who may be EU residents
- The meeting context often involves confidential professional discussions

**PoC baseline:** Fully local on OG's Tuxedo laptop. Zero data leaves the machine.

**Future production:** European or Swiss hosting only. US cloud infrastructure not acceptable.

`[Claude]` Relevant European and Swiss hosting options for future consideration `[⚠ cutoff risk]`:

| Provider | Country | Relevant capabilities |
|---|---|---|
| Infomaniak | Switzerland | nDSG compliant, kDrive MCP, GPU compute available |
| Exoscale | Switzerland | Strong data residency, Zurich and Geneva regions |
| Hetzner | Germany | GDPR compliant, competitive GPU instances |
| OVHcloud | France | GDPR compliant, GPU instances |
| Scaleway | France | GDPR compliant, ML-focused GPU offerings |

**Sovereignty at each pipeline component:**

| Component | Runtime sovereignty | Setup-time note |
|---|---|---|
| faster-whisper | Fully local after model download | Whisper weights from OpenAI CDN — download once |
| pyannote / WhisperX | Fully local after model download | Requires HuggingFace token for gated model download — one time |
| Ollama / LLM | Fully local | Model weights downloaded once from model provider |
| OpenClaw | Self-hosted | No outbound calls from core; depends on configured skills |
| Voice profile storage | Local filesystem | OG-controlled |

All runtime dependencies are local. Setup-time downloads are one-time events on a trusted connection.

---

## Section 9: Existing Open-Source Alternatives

### 9.1 Meetily

`[Claude]` Meetily (meetily.ai, GitHub: Zackriya-Solutions/meeting-minutes) is the most relevant existing open-source tool identified during this conversation. `[⚠ cutoff risk]`

- **License:** MIT — free for personal use, no payment required
- **Processing:** 100% local, no cloud upload
- **Transcription:** Whisper.cpp (local)
- **Summarization:** Ollama (local LLMs) or external LLM APIs (Claude, Groq, LLaMA)
- **Storage:** SQLite local database
- **Platform:** Desktop app, cross-platform (Windows and macOS confirmed; Linux support varies)
- **Speaker diarization:** Listed as "coming soon" as of early 2026 — not yet shipped
- **Obsidian integration:** Also "coming soon"
- **Export:** PDF, DOCX, Markdown

`[OG]` OG initially questioned whether Meetily required payment. Clarified: the personal use tier is free and open-source. A Pro tier at $10/month adds hosted AI and custom templates — neither needed for OG's use case.

**Meetily's gap relative to this product concept:** No speaker diarization yet. No OpenClaw integration. No persistent memory layer connecting meetings to the assistant ecosystem.

### 9.2 Three Paths to the PoC

`[Claude]` Three implementation paths were identified:

**Path 1 — Meetily + OpenClaw post-processing (lowest effort):**  
Meetily handles live audio capture and transcription during the meeting. After the meeting, its output (transcript, summary) is fed into OpenClaw via a skill or file watch. Speaker diarization waits for Meetily to ship it. Integration is loose but works. No custom audio capture code required.

**Path 2 — Custom OpenClaw skill for live capture (fully integrated):**  
Build a skill that handles continuous audio capture during the meeting and pipes it to faster-whisper in near-real-time, chaining into the existing transcription and meeting-notes skills. Diarization runs post-meeting via WhisperX. More engineering work, fully integrated, no dependency on Meetily's roadmap.

**Path 3 — Notion AI + MCP bridge (not recommended):**  
Use Notion AI for capture and transcription, pipe the output into OpenClaw via Notion MCP. Indirect, subscription cost, US cloud processing — conflicts with sovereignty requirement.

`[DECISION]` Path 1 (Meetily + OpenClaw skills) is the PoC starting point — lowest effort to a working pipeline. Path 2 is the natural evolution once the pipeline is validated and if Meetily's roadmap does not converge with OG's needs. Path 3 is eliminated.

---

## Section 10: Benchmarking Requirements

`[Claude]` All VRAM and latency figures in this document are estimates based on published hardware specifications and model benchmarks. None have been validated on OG's actual hardware. Benchmarking is a required step before relying on the PoC in a real meeting.

**Required benchmarking scenarios:**

1. **Sustained transcription load:** Run faster-whisper medium continuously for 90 minutes. Measure: VRAM stability, inference time per chunk over time (thermal throttling indicator), GPU temperature, any OOM events. RTX 3060 laptop thermal throttling under sustained load is a known risk.

2. **Post-meeting diarization batch:** Run WhisperX/pyannote on a 60-90 minute complete recording. Measure: total processing time, VRAM peak, accuracy on known speaker audio.

3. **Chunk boundary accuracy:** Evaluate whether the 2-3 second overlap between 40-second chunks correctly handles words and sentences that span boundaries. Measure: word error rate at chunk boundaries vs. mid-chunk.

4. **Full pipeline end-to-end:** Run a complete simulated meeting cycle — capture, transcription, diarization, correction interface, summarization, OpenClaw memory write. Measure total elapsed time from meeting end to notes available.

`[OG]` Agreed benchmarking is necessary. No benchmarks have been run yet; all figures in this document are design-time estimates.

---

## Section 11: Open Questions

`[OPEN 1]` **Microphone hardware.** OG has not yet evaluated external microphone options. Three categories:
- USB microphone: simplest, no additional hardware, quality varies widely
- XLR microphone + USB audio interface: higher quality, interface appears as USB audio device to OS
- Conference/boundary microphone: designed for multi-speaker room capture (e.g. Jabra, Shure, Rode)

Key software dependency regardless of choice: a capture library must support selecting a specific audio device by name or ID. Python `sounddevice` library handles this and is cross-platform. Low-risk dependency.

`[OPEN 2]` **Correction UI format and channel.** Deferred pending a prototype of the flagged segment output. OG must see actual output before designing the review interface. Candidate channels: terminal/text editor, simple web UI, or back through OpenClaw via Telegram/WhatsApp.

`[OPEN 3]` **Chunk flush format.** Raw transcript text per chunk, or timestamped JSON with speaker labels and confidence scores? JSON is more useful for downstream processing and the correction step but more complex to implement initially.

`[OPEN 4]` **Recording trigger mechanism.** How does OG start and stop a meeting recording? OpenClaw command via chat, keyboard shortcut, companion app button, or other?

`[OPEN 5]` **Output destination.** After correction and summarization: structured markdown to disk, OpenClaw memory, kDrive push, or combination?

`[OPEN 6]` **Benchmarking.** No benchmarks run yet. All performance figures are estimates.

`[OPEN 7]` **Meetily diarization roadmap.** Diarization listed as "coming soon" in Meetily as of early 2026. If Meetily ships diarization before OG builds Path 2, it may eliminate the need to build the custom diarization integration entirely. Worth monitoring Meetily's GitHub.

`[OPEN 8]` **Infomaniak kMeet and calendar MCP.** No timeline found for Infomaniak shipping agent-accessible integration for kMeet or calendar. Monitor their developer blog and GitHub.

---

## Section 12: Decisions Log

| # | Decision | Reasoning | Origin | Revisit trigger |
|---|---|---|---|---|
| D1 | Notion AI eliminated as solution | US cloud processing, platform mismatch, sovereignty conflict | OG stated | N/A — hard requirement |
| D2 | PoC scope: in-person meeting, single external mic | Simpler signal chain, better acoustic conditions, no call software resource conflict | OG stated | If video call scenario required |
| D3 | Hybrid architecture (real-time transcription + post-meeting diarization batch) | OOM risk during meeting, better diarization accuracy, natural correction step placement | Claude proposed, OG accepted | If real-time speaker labels become a requirement |
| D4 | 40-second chunks for transcription | VRAM headroom, more pyannote context per chunk, within latency budget | OG decided | If latency requirement tightens below 40s |
| D5 | Chunk sizes for faster-whisper and pyannote can be decoupled | They consume same audio stream independently; only timestamp coherence required | Claude proposed, OG accepted | Implement when near-real-time diarization explored |
| D6 | faster-whisper medium for PoC | VRAM headroom priority on 6GB RTX 3060 | OG decided | If hosted on GPU with more VRAM |
| D7 | faster-whisper large-v3 for future hosted environment | Accuracy preferred when hardware is not the constraint | Claude proposed, OG accepted | N/A |
| D8 | WhisperX for post-meeting batch diarization only | Lower-risk usage of WhisperX; not on real-time path; can be re-run if it fails | Claude proposed, OG accepted | If WhisperX proves unstable |
| D9 | Explicit VRAM loading/unloading between stages | Deterministic memory behavior; no reliance on garbage collection | Claude proposed | N/A — implementation rule |
| D10 | Summarization: small quantized open-weight model (7-8B), Ollama, temperature 0.2-0.3, top-p 0.9 | Low-complexity LLM task; no real-time pressure; sufficient quality | Claude proposed, OG accepted | If summarization quality proves insufficient |
| D11 | Diarization opt-in per meeting | Not all meetings need it; space is evolving | OG stated | If usage patterns suggest default-on |
| D12 | 4-speaker maximum for PoC | Accuracy degrades beyond this; matches OG's meeting patterns | OG stated | If meeting size grows |
| D13 | Blind diarization for PoC | Lower complexity; speaker verification is future feature | OG stated | When profile learning is built |
| D14 | Overlap → flag explicitly, not force-attribute | Mislabeling more dangerous than uncertainty | OG stated | N/A — design principle |
| D15 | Name introduction convention (participants state name at meeting start) | Good meeting hygiene; creates voice anchor for diarization at zero friction | OG proposed | N/A — convention |
| D16 | Correction step opt-in with exception-first UX | Avoid overwhelming user; focus on flagged segments only | OG stated | N/A — UX principle |
| D17 | Correction stored as diff; source transcript preserved | Source data integrity; bug resilience | Claude proposed | N/A |
| D18 | Store labeled embeddings from correction even before profile learning is built | Forward compatibility; accumulate training data from day one | Claude proposed, OG accepted | N/A — low-cost future-proofing |
| D19 | PoC fully local; no data leaves hardware | Sovereignty requirement | OG stated | N/A — hard requirement |
| D20 | Future production: European/Swiss hosting only | Same sovereignty requirements | OG stated | N/A — hard requirement |
| D21 | Infomaniak integration deferred until kSuite MCP surface matures | Calendar, kMeet, kChat not yet agent-accessible | OG stated, confirmed by search | When Infomaniak ships calendar/kChat/kMeet MCP |
| D22 | Path 1 (Meetily + OpenClaw skills) as PoC starting point | Lowest effort path to working pipeline | Claude proposed, OG accepted | If Meetily roadmap diverges or Path 2 is preferred |
| D23 | Path 3 (Notion AI + MCP) eliminated | US cloud processing; sovereignty conflict; subscription cost | OG and Claude jointly | N/A |

---

## Appendix: Model Selection Context

This appendix summarizes the relevant conclusions from the prior session on the AI model landscape, included here as reference for decisions made in this report. It is intentionally brief — the full reasoning is in the separate session record.

**Why locally runnable open-weight models rather than frontier API calls:**

The prior session established a five-dimension framework for evaluating whether a task requires a frontier closed model or whether an open-weight alternative is sufficient. The meeting notes pipeline tasks score low on all five dimensions — short decision horizons, human-reviewed outputs, structured goals, minimal tool use, reversible actions. A small quantized open-weight model is appropriate.

**Specific model choices and their justification:**

- **faster-whisper:** Not an LLM — it is a dedicated ASR (Automatic Speech Recognition) model. The only viable option for local real-time transcription given sovereignty requirements. No meaningful alternative in the open-source local stack.
- **pyannote:** Not an LLM — it is a dedicated speaker diarization model. Same reasoning as faster-whisper.
- **Ollama + 7-8B quantized model for summarization:** LLM task, but low complexity given clean transcript input. Small quantized models (Qwen3 8B, Mistral 7B, LLaMA 3.1 8B) are sufficient. Runs on CPU post-meeting with no latency pressure.

The choice to avoid frontier API calls (Claude API, OpenAI API) for any stage of this pipeline is driven by: (1) sovereignty requirements — audio and transcript data must not leave local hardware; (2) cost optimization — open-weight models are sufficient for these tasks; (3) offline capability — the pipeline should work without internet connectivity.

---

## Document Metadata for Agent Consumption

```yaml
document_type: preliminary_discovery_meeting_report
topic: ai_backed_meeting_notes_product
version: 2.0
date: 2026-04-08
claude_knowledge_cutoff: 2025-08
participants:
  customer: OG (Oriol Gut)
  solutions_architect: Claude Sonnet 4.6
hardware_context:
  device: Tuxedo Polaris 15 Gen3
  gpu: NVIDIA RTX 3060 laptop (6GB VRAM)
  ram: 32GB
platform_integration_target: OpenClaw
sovereignty_requirement: local_or_european_swiss_only
poc_scope:
  meeting_type: in_person
  audio_input: single_external_microphone
  processing: fully_local
  diarization: opt_in
  speaker_limit: 4
key_components:
  transcription: faster_whisper_medium
  diarization: pyannote_via_whisperx
  summarization: small_quantized_open_weight_llm_via_ollama
  integration: openclaw_memory_and_skills
open_questions_count: 8
decisions_count: 23
status: draft_preliminary
next_actions:
  - microphone_hardware_selection
  - benchmarking_on_actual_hardware
  - correction_ui_prototype
  - meetily_evaluation_against_poc_requirements
companion_sessions:
  - topic: ai_model_landscape_open_vs_closed_architecture_types
    note: separate session; model selection appendix in this document summarizes relevant conclusions
```

---

*End of document. This report reflects a single conversation session. All decisions are explicitly revisable. Attribution markers throughout indicate the origin and appropriate skepticism level for each piece of information. The YAML metadata block above is intended for machine consumption by AI agents using this document as context.*
