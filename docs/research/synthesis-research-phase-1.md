# Research Phase 1 Synthesis (R-01 through R-09)

Status: synthesis baseline derived from completed dual-agent research

Metadata:

- Date: 2026-05-01
- Issue: [#1 Synthesize dual-agent research findings](https://github.com/dothackerman/mena/issues/1)
- Inputs: completed findings in [docs/research/notes/](notes/) for R-01, R-02, R-04, R-05, R-06, R-08, R-09 (R-08 is single-source; the R-09 Nemotron note is considered but rejected in the resolution — see per-task notes and §6)
- Audience: product owner (web developer; not a Linux/audio/ML systems engineer) and future coding/architecture agents
- Companion documents:
  - [Architecture Overview](../architecture/architecture-overview.md)
  - [Implementation Guardrails](../architecture/implementation-guardrails.md)
  - [Architecture Research Backlog](backlog/architecture-research-backlog.md)

This document is the authoritative phase-1 synthesis of dual-agent research (ChatGPT Deep Research + NVIDIA Nemotron-3-Nano-30B-A3B) for the first seven actionable backlog items. It exists to:

- give a non-systems reader enough background to evaluate the recommendations
- consolidate per-task recommendations from two independent research runs
- flag divergences explicitly and propose a resolution tied to mena's quality priorities
- list the architecture updates triggered by these findings
- record the resulting decision-log entries and research-backlog status changes

Where the two agents disagree, both positions are presented and the recommended resolution is anchored in §1.4 of the architecture overview (`Recording robustness > Sovereignty > Resumability > Local operability > Replaceability > Output quality > Performance > Interface convenience`).

---

## 1. How to Read This Document

The document is organised in three layers:

1. **§2 Technical Onboarding Primer.** Background a web developer needs before reading the synthesis. Skip if you already know what PipeWire, Ollama, ASR, and a session orchestrator are.
2. **§3 Per-Task Synthesis (R-01 → R-09).** One section per research task. Each section opens with a 2–3 paragraph recommendation, lists agreements between the two agents, flags divergences with a resolution, and links to the source notes.
3. **§4 Architecture Updates and Decision-Log Entries.** What changes in the architecture overview and what new entries land in the decision log.

Where a claim is grounded in a research note, the source is named inline (e.g. *(ChatGPT R-04)* or *(Nemotron R-05)*). Where the resolution depends on the architecture rather than the research, the relevant architecture section is cited.

R-03 (Future OS compatibility) and R-07 (Install / update / uninstall) are out of scope for this synthesis. They are unblocked by it but not answered here.

---

## 2. Technical Onboarding Primer

This primer assumes you know Hypertext Transfer Protocol (HTTP), Representational State Transfer (REST), processes, files, JavaScript Object Notation (JSON), command-line interfaces (CLIs), and how a frontend talks to a backend. It does not assume you have ever debugged Linux audio, served a local language model, or built a desktop process tree.

### 2.1 Linux Audio Stack 101

Linux does not have one audio system. It has a stack of layers that have evolved over twenty years. The layers most relevant to mena are:

- **ALSA — Advanced Linux Sound Architecture.** The kernel-level driver layer. ALSA exposes raw audio devices (cards, PCM streams) to user-space programs. It is universally present on every modern Linux kernel. Think of ALSA as the *hardware adapter*: it knows about your sound card and microphone, but not about "applications" or "meetings".
- **PulseAudio.** A user-space sound server that sits *above* ALSA. It introduced per-application volume control, software mixing, and the concept of routing audio between applications. It is the "old desktop standard" that most non-bleeding-edge desktops used between roughly 2008 and 2022. It still exists for legacy compatibility but is being phased out.
- **PipeWire.** The modern replacement for PulseAudio (and JACK; see below). PipeWire is also a user-space sound server, but it is built around an explicit *graph* of audio nodes, ports, and links — closer to a node-based audio editor than a simple mixer. It is the default on Fedora, Arch, and Ubuntu 22.04+. PipeWire ships a *PulseAudio compatibility layer* (`pipewire-pulse`) so existing apps that talk to PulseAudio "just work".
- **JACK — Jack Audio Connection Kit.** A separate sound server aimed at professional/low-latency audio work (musicians, digital audio workstations, or DAWs). PipeWire can speak the JACK protocol too; for mena it is mostly a footnote.

Some PipeWire vocabulary you will see throughout R-02:

- **Sink.** A virtual audio *output*. When an application "plays" sound, it plays to a sink. Speakers are a sink. Headphones are a sink. You can also create a *virtual* sink that doesn't go anywhere — useful for capture.
- **Source.** A virtual audio *input*. Microphones are sources. You can also create virtual sources.
- **Monitor.** Every sink automatically has a *monitor source* attached to it. The monitor lets you record whatever was played to the sink. So if you create a virtual sink and route the meeting app's audio into it, the *monitor of that sink* is exactly the audio the call platform produced.
- **Null sink.** A sink that doesn't go to any speaker. You play into it; nothing is heard. But you can record from its monitor. This is the standard trick for capturing system audio without it disappearing into the void or doubling out of your speakers.
- **Loopback.** A connection that copies audio from one node to another. PipeWire has a `loopback` module that creates these connections.

The *online_dual_stream* problem is exactly this: you want to record (a) what *you* say into your microphone, and (b) what the *call platform* plays out (the remote participants' voices), as **two separate streams**. The naïve approach — "record the default speaker" — pollutes the remote stream with notification beeps, music, and anything else playing on the system. The robust approach is what R-02 lays out: create a dedicated null sink for the meeting, route only the call app's audio into it, capture the *monitor* of that sink as the remote stream, and separately loop it back to your real headphones so you can still hear the call.

### 2.2 Local ML Model Serving 101

A modern large language model (LLM) ships as a directory of weight files, possibly tens of gigabytes large, alongside a tokenizer and configuration. To *use* the model you need a **runtime** that loads those weights into memory (central processing unit, or CPU; or graphics processing unit, or GPU), feeds it tokens, and reads tokens back. Some terms:

- **GGUF.** A file format for quantised model weights, primarily used by `llama.cpp` and runtimes built on top of it. Think of it as "the .zip of efficient local LLM weights".
- **Quantisation.** Compressing the model's numeric weights from 16-bit floats to lower-precision integers (8-bit, 5-bit, 4-bit, etc.). A 4-bit quantised 7B-parameter model fits in roughly 4–5 GB instead of 14 GB, with a small but usually acceptable quality cost. Q4_K_M is a common quantisation for laptops.
- **`llama.cpp`.** A C/C++ library and command-line server for running quantised LLMs. Originally Mac-focused, now broadly portable. Ships an OpenAI-compatible HTTP server (`llama-server`).
- **Ollama.** A user-friendly daemon that wraps `llama.cpp` and similar runtimes behind a single CLI and HTTP API. Installs as a system service; downloads, manages, and runs models. The "homebrew of local LLMs".
- **LM Studio.** A desktop app + headless server (`llmster`) that does roughly what Ollama does, with a GUI focus. Has its own runtime and model store.
- **vLLM.** A high-throughput LLM serving engine targeted at GPUs and server deployments. Great in a datacentre with an A100. Less appropriate for a privacy-first laptop product where most testers do not have a discrete GPU.
- **OpenAI-compatible HTTP.** A *de facto* API shape — `POST /v1/chat/completions` with messages, model, temperature, etc. Originally OpenAI's, now copied by virtually every local runtime. mena treats this as an internal adapter detail, not as the architectural boundary.

The "why local matters" argument is straightforward: mena's privacy story collapses the moment audio or transcripts leave the user's machine. Running a model locally — even a smaller one — keeps the canonical text on the user's disk. The cost is hardware burden: a 3B-parameter model is comfortable on a 16 GB laptop; a 12B-parameter model is not.

### 2.3 Speech Processing 101

The pipeline mena needs to build looks like this:

```
audio file  →  [ASR]  →  transcript  →  [diarisation overlay]  →  speaker-attributed transcript  →  …
```

- **ASR — Automatic Speech Recognition.** Speech-to-text. The current open standard for general-purpose ASR is OpenAI's *Whisper* model family (open-weight). Open runtimes for Whisper include `faster-whisper` (Python, CTranslate2-backed, GPU/CPU) and `whisper.cpp` (C++, CPU-first, minimal dependencies). Whisper has explicit support for ~99 languages, including Swiss German via the German model with prompt biasing.
- **Diarisation.** The process of figuring out *who* spoke *when*, without necessarily knowing who those people are. The output is a timeline of speaker turns labelled "Speaker 1", "Speaker 2", … . The leading open-source diarisation framework is **pyannote** (`pyannote.audio`), and its current open model is `speaker-diarization-community-1`.
- **VAD — Voice Activity Detection.** Detects whether a chunk of audio contains speech at all. Used to trim silence and to gate ASR/diarisation. Whisper-family runtimes typically use Silero VAD.
- **Hotwords / prompt biasing.** Soft mechanisms to nudge ASR towards specific vocabulary (proper nouns, jargon). Not deterministic vocabulary control — they raise probability rather than enforce it.
- **WER — Word Error Rate.** A standard ASR quality metric. Lower is better. 10% WER means roughly one in ten words is wrong.
- **DER — Diarisation Error Rate.** The diarisation analogue. Combines speaker-confusion errors, missed speech, and false alarms.

Two structural facts about meeting audio shape mena's architecture:

1. **Dual-stream beats single-stream.** If you can record the local mic and the call platform output as separate streams, the local user's speech is *structurally known* (it came from their microphone) and you do not need diarisation to figure out "did the user speak here?". You only need diarisation on the remote stream, and only when there are multiple remote speakers worth telling apart.
2. **Diarisation is hard.** Even on clean meeting audio, public DERs are 15–20%. Far-field room mics are worse. Overlap, rapid turn switches, and short backchannels ("yeah", "uh-huh") are routinely misattributed. The right product posture is to *expect correction*, not to expect the model to be authoritative.

### 2.4 Process Architecture 101

mena splits into two operating-system processes for the proof of concept (PoC):

- **Session Orchestrator Process.** The brain. Runs the user-facing CLI workflow, decides when to record/process/correct/export, manages provider selection, and is the *single writer* of the canonical Session Record (the per-meeting metadata file). For the CLI PoC this is created fresh per `mena …` command — there is no daemon.
- **Capture Worker Process.** The hand. A dedicated process that owns the live audio capture: opens audio devices, writes segments to disk, and emits status events. It exists as a separate process so that if the orchestrator crashes mid-meeting, the recording survives.

The two processes talk to each other through a small **internal protocol** — short JSON messages over standard input/output (stdio), carrying commands ("StartCapture") and events ("SegmentFlushed", "CaptureFinalized"). They do *not* shovel raw audio over this protocol; audio goes to disk and the protocol just announces "segment 17 was sealed at this path".

The architecture style mena follows is sometimes called **ports and adapters** (a.k.a. **hexagonal architecture**). The application core defines abstract *ports* (e.g. "Capture Port", "Transcription Port"), and concrete implementations called *adapters* satisfy those ports (e.g. "Linux PipeWire capture adapter", "faster-whisper transcription adapter"). The core never imports the adapter directly; it asks the policy/capability resolver for "the current transcription provider" and gets one back. The benefit: you can swap the Linux audio adapter for a macOS one, or `faster-whisper` for `whisper.cpp`, without touching the workflow code.

### 2.5 Abbreviations and Glossary

| Abbreviation / term | Expansion / definition |
| --- | --- |
| ALSA | Advanced Linux Sound Architecture — kernel-level Linux audio driver layer |
| API | Application Programming Interface |
| AHC | Agglomerative Hierarchical Clustering — a clustering technique used in some diarisation stacks |
| ARM | A non-x86 CPU architecture family (e.g. Apple Silicon, Raspberry Pi) |
| ASR | Automatic Speech Recognition — speech-to-text |
| AVX/AVX2/AVX512 | x86 CPU instruction-set extensions used by some local LLM runtimes |
| BentoML | A Python framework for serving ML models behind HTTP APIs |
| CLI | Command-Line Interface |
| Capture Port | mena's abstract contract for audio capture; satisfied by platform-specific adapters |
| Capture Profile | mena's explicit description of a capture topology (e.g. `online_dual_stream`, `in_person_single_mic`) |
| Capture Worker Process | mena process that owns live audio capture |
| CON-01 | Constraint #1 in `poc-requirements.md` — sovereignty / data residency |
| CPU | Central Processing Unit |
| CTranslate2 | Inference engine used by `faster-whisper` |
| CUDA | NVIDIA's GPU compute platform |
| DAP | Distributed Aggregation Protocol — IETF effort for privacy-preserving telemetry aggregation |
| DAW | Digital Audio Workstation |
| DER | Diarisation Error Rate — standard diarisation accuracy metric |
| Diarisation | Identifying *who* spoke *when* in an audio recording |
| DPA | Data Processing Addendum — contractual document between data controller and processor under GDPR |
| EU | European Union |
| EDPB | European Data Protection Board — EU privacy regulator |
| `faster-whisper` | Python library for running Whisper ASR on CPU/GPU via CTranslate2 |
| FFmpeg | Open-source audio/video processing library |
| GDPR | General Data Protection Regulation — EU privacy law |
| GGML / GGUF | File formats for quantised LLM weights used by `llama.cpp` |
| GPU | Graphics Processing Unit |
| Hexagonal architecture | Synonym for ports-and-adapters style |
| HPKE | Hybrid Public Key Encryption — used in OHTTP |
| INT8 | 8-bit integer quantisation |
| JACK | Pro-audio Linux sound server |
| LDP | Local Differential Privacy — adds calibrated noise to telemetry on the client device |
| LLM | Large Language Model |
| Loopback | A connection that copies audio from one PipeWire node to another |
| LSP | Language Server Protocol — IDE↔language-tool framing protocol that uses `Content-Length` headers over stdio |
| Monitor source | Auto-created PipeWire source attached to every sink; lets you record what was played to that sink |
| Null sink | A PipeWire sink whose output goes nowhere (no audible playback), but whose monitor can be recorded |
| Ollama | Local LLM daemon wrapping `llama.cpp` and similar runtimes |
| OHTTP | Oblivious HTTP — privacy-preserving HTTP transport that hides client IP from the target server |
| Orchestrator | mena's Session Orchestrator Process |
| OS | Operating System |
| OWASP | Open Web Application Security Project |
| PCM | Pulse-Code Modulation — raw uncompressed digital audio samples |
| PipeWire | Modern Linux user-space sound server, default on Fedora/Arch/Ubuntu 22.04+ |
| PoC | Proof of Concept |
| Port | In ports-and-adapters: an abstract contract the application core depends on |
| pyannote | Open-source diarisation framework |
| Quantisation | Compressing model weights to lower numeric precision to reduce memory use |
| RAM | Random Access Memory |
| RTF | Real-Time Factor — processing time / audio duration (lower is better) |
| RTTM | Rich Transcription Time Marked — a standard diarisation output file format |
| Sink | PipeWire term for a virtual audio output |
| Source | PipeWire term for a virtual audio input |
| stdio | Standard Input / Standard Output / Standard Error — the three default file descriptors a process inherits |
| TLS / mTLS | Transport Layer Security / mutual TLS |
| VAD | Voice Activity Detection — detects whether audio contains speech |
| vLLM | High-throughput GPU-targeted LLM serving engine |
| VRAM | Video RAM (GPU memory) |
| WER | Word Error Rate — ASR accuracy metric |
| `whisper.cpp` | C/C++ port of OpenAI Whisper, CPU-first, minimal dependencies |
| WirePlumber | Session manager / policy engine that runs alongside PipeWire |
| ZDR | Zero Data Retention — provider commitment not to retain request/response content beyond immediate processing |

---

## 3. Per-Task Synthesis

### 3.1 R-01 — Local Model Runtime Strategy

**Recommendation.** Adopt **Ollama as the default local-LLM runtime adapter** behind mena's summarisation and semantic-flagging Inference Port for the PoC, with **owned `llama.cpp` (`llama-server`) as a quality-validated alternative adapter** and **`faster-whisper`/`whisper.cpp` handled outside this LLM port** for ASR (see §3.3). Default model profile: a **3B–4B-parameter quantised instruct model** (e.g. Llama 3.2 3B Q4 ~2 GB, Gemma 3 4B ~3.3 GB) for the first-target laptop tier, with a **1B–3B fallback profile** for ≤8 GB RAM testers. Treat 7B as opt-in for stronger machines, and **do not** make 12B-class models the baseline.

The PoC inference architecture is two ports:

- an **Inference Port** for stage execution — workflow code asks for "structured summary" or "semantic coherence check", not for `/v1/chat/completions`
- an **Admin/Preflight Port** for runtime-specific operations such as model availability checks, resource estimation, and model preparation

OpenAI-compatible HTTP is treated as an *internal adapter detail*, not the architectural boundary. The adapter must emit a structured *failure envelope* (runtime family, runtime version, model identifier, context length, GPU offload mode, timing fields, exit code, stderr excerpt, locality flag) on every error so policy and recovery decisions have evidence.

For summarisation specifically, the architecture should adopt a **chunked map-reduce → small merge pass → targeted coherence/action-item check** strategy rather than a single 32K-token monolithic prompt. This makes the system materially more robust on weak hardware and reduces sensitivity to which runtime is underneath.

**Convergences.**

- Both agents agree the runtime decision must sit behind a clean provider port; OpenAI-compatible HTTP is the adapter shape, not the architecture.
- Both agree the PoC should be hardware-tiered: a small default model for typical laptops, a smaller fallback for ≤8 GB testers.
- Both treat Ollama as a credible Linux-friendly runtime with documented systemd setup, journal logs, GGUF imports, structured outputs, and usage timings.
- Both agree the runtime choice is *not* the main determinant of summarisation viability — model size and context discipline are.
- Both flag CUDA driver / CUDA version pinning as a real risk for any GPU path.

**Divergence — primary runtime.** ChatGPT picks **owned `llama.cpp` (`llama-server` subprocess)** as the default and Ollama as the second-best fallback, arguing for hermetic automation and explicit lifecycle control. Nemotron picks **vLLM (GPU) + Ollama (CPU/GPU)** with mTLS-fronted Docker Compose, arguing for raw throughput.

**Resolution.** Ollama is the right *PoC default*, and the divergence collapses once the PoC's hardware envelope is taken seriously. Nemotron's vLLM recommendation is anchored on A100-class GPUs and 3,000 tokens/second — that profile does not match a privacy-first Linux laptop product, and Nemotron's own analysis admits vLLM "needs an NVIDIA GPU" while no public CPU benchmark exists. ChatGPT's owned-`llama.cpp` recommendation is operationally cleaner but shifts packaging burden to the mena team. For a PoC where the dominant risk is "does this even install on a tester's laptop", Ollama's documented Linux service install, broad CPU compatibility (cpu / cpu_avx / cpu_avx2), local-only mode, and ~5-minute install path win on §1.4 priority #4 (local operability) without sacrificing #2 (sovereignty) — the runtime stays local-only by adapter contract. Owned `llama.cpp` should be implemented as a *second adapter* to confirm the port boundary really is replaceable; vLLM is not in the PoC shortlist.

**Sources.** [ChatGPT R-01](notes/r-01-local-model-runtime-strategy-findings-chatgpt-deep-research.md); [Nemotron R-01](notes/r-01-local-model-runtime-strategy-findings-nemotron-3-nano-30b-a3b.md).

---

### 3.2 R-02 — Linux Audio Capture and Routing Strategy

**Recommendation.** The PoC capture adapter is **PipeWire-first** for `online_dual_stream`. The recommended Linux topology is:

1. Enumerate active playback streams, sinks, and sources in the user session; require **explicit user confirmation** of which playback stream is the meeting audio (the CLI surfaces application binary, stream name, and media name).
2. Create a **session-scoped dedicated null sink** (a virtual sink that exists only for this capture session; not a permanent desktop reconfiguration).
3. Move *only the chosen meeting playback stream* into that null sink (using `target.object` semantics or `pactl move-sink-input`). **Do not** swap the system default sink.
4. Capture the **monitor port of that null sink** as the remote stream — this gives a clean remote artifact that tracks only what was routed through the meeting sink.
5. Create a **separate loopback** from the null sink to the user's actual playback device (speakers/headphones), so the user can still hear the call. This decouples the recording path from the physical-output path: if the headset disappears the recording survives.
6. Capture the **chosen microphone source directly** (no echo-cancel/suppression virtual node by default) as the local stream.

**Tiered fallback** when dedicated routing fails: degrade to `mic + default_sink_monitor` and mark the session as `system_mix_remote`; if system audio is impossible, fall back to `mic_only` and mark the session degraded. **Never silently retry — write degradation events to the session metadata.** Within the capture worker, separate the **real-time recorder path** (file descriptors, append-only writes, sealing, crash-bounded persistence) from the **routing supervisor path** (graph discovery, sink creation, stream movement) even if both stay in the same process for the PoC.

The supported environment for `online_dual_stream` is **PipeWire user-session desktops**; PulseAudio is treated as a compatibility path through `pipewire-pulse`; ALSA-only and headless/minimal environments are out of scope for that profile. The capture adapter should request 48 kHz capture but must **not** force the user's whole graph to a new clock rate.

`in_person_single_mic` is incremental on top of this, not a second routing architecture: it reuses device discovery, source selection, file writing, sealing, and replay, while dropping the dedicated sink, the playback-stream move, and the speaker loopback.

**Convergences.**

- Both agents pick **PipeWire + dedicated virtual sink + monitor-source capture** as the default Linux strategy.
- Both agree direct ALSA, PulseAudio loopback, and JACK are credible fallbacks but not the right PoC default — ALSA lacks per-app routing semantics, PulseAudio has known latency-spike behaviour, JACK requires real-time scheduling.
- Both agree `in_person_single_mic` is incremental, not architecturally distinct.
- Both highlight the need for diagnostics/observability (graph snapshots, `pw-dump`, `pw-mon`, journal logs) so failures are inspectable in product terms.
- Both highlight that meeting-app stream identification (especially browser-based meetings) is the least automatable part — a CLI confirmation step is the right product behaviour.

**Divergence — capture port contract.** ChatGPT recommends **retiring the "single generic capture port" assumption** if it implies "exactly one homogeneous input"; the better contract is a *capture session plan* that yields one or more canonical source artifacts plus routing metadata. Nemotron recommends **keeping the generic capture port** and configuring it with profiles that pass a device identifier such as `pipewire:zoom_virtual_mic.monitor`.

**Resolution.** The architecture overview's existing language already encodes ChatGPT's substance: §5.4 explicitly says the Capture Port carries "explicit Capture Profiles" with strongly modelled routing metadata persisted in the Session Record, and §8.1 defines `Capture Profile` and `Capture Source` with logical identity, semantic role, channel layout, diarisation policy, and selected device/route. The Capture Port abstraction does not need to be retired — it needs to be *exercised* as a multi-source contract with routing metadata. **AD-003 (one generic Capture Port with capture profiles) stands; §5.4 is enriched with an informative Linux PipeWire pattern** describing the dedicated-null-sink topology as the concrete realisation for `online_dual_stream`. The architectural rule that downstream processing must consume artifacts plus capture metadata, not make hidden assumptions about source topology, is reaffirmed.

**Sources.** [ChatGPT R-02](notes/r-02-linux-audio-capture-and-routing-strategy-findings-chatgpt-deep-research.md); [Nemotron R-02](notes/r-02-linux-audio-capture-and-routing-strategy-findings-nemotron-3-nano-30b-a3b.md).

---

### 3.3 R-04 — ASR Provider Strategy

**Recommendation.** Use the **Whisper-family local ASR strategy behind the existing Transcription Port, with `faster-whisper` as the primary provider runtime**. `large-v3-turbo` is the **PoC quality ceiling** for machines with sufficient VRAM/RAM headroom, not the universal default. Installation/preflight should detect available VRAM/RAM and select a suitable ASR profile: smaller or quantised Whisper-family checkpoints for weaker machines, `large-v3-turbo` for strong machines, and `large-v3` only as an explicit quality-reference rerun on stronger machines. Keep **`whisper.cpp`** available as the first fallback adapter for weaker or more brittle environments (CPU-only, ≤8 GB RAM, no AVX2). Treat **NVIDIA Parakeet/NeMo** as a deferred specialist comparison for an English-only future and **Vosk** as edge-only for vocabulary-driven extreme-low-spec cases.

The provider contract should persist, per transcript generation: provider name, checkpoint, quantisation, device type, decode parameters (including `condition_on_previous_text` and `hallucination_silence_threshold`), detected language, and timestamp mode. ASR output must be treated as *provisional and replayable* — reruns with alternate checkpoints or decode parameters should be first-class operations, not hidden details.

For Swiss German (REQ-F03 + scope §3.1 PoC primary language): rely on the German-trained head of `large-v3-turbo` plus **prompt biasing (`initial_prompt`)** and **hotword hints** for personal lexicon (REQ-S01). Soft biasing is acknowledged as a soft tool, not deterministic vocabulary control — the correction overlay carries the rest.

**Convergences.**

- Both agents recommend a **Whisper-family** local ASR.
- Both treat `whisper.cpp` as a strong CPU-friendly fallback.
- Both treat Vosk as a niche option for ultra-low-spec or vocabulary-driven cases.
- Both emphasise that ASR provider metadata (checkpoint, decode parameters) must be persisted with each transcript so reruns are deterministic.
- Both emphasise lexicon support through `initial_prompt` / hotwords plus the correction loop, not through model retraining.

**Divergence — primary runtime within the Whisper family.** ChatGPT picks **`faster-whisper` (medium/`large-v3-turbo` default; `large-v3` rerun)**. Nemotron picks **`whisper.cpp` (medium model)** as primary and treats Whisper as English-leaning.

**Resolution.** `faster-whisper` wins as the PoC primary because it gives the operator surface mena needs: word timestamps, Silero-based VAD filtering, initial prompts, hotword hints, explicit CPU/CUDA device selection, `local_files_only` for pre-seeded model caches, and avoidance of a separate system `ffmpeg` dependency (it uses PyAV). Those are exactly the controls the architecture's *replay-first* posture requires. `whisper.cpp`'s strengths — single static binary, low memory footprint, INT8 quantisation — make it a *better fallback* for weak/brittle machines, not a worse choice in the abstract; the right architecture is to keep both adapters available and select per machine tier. Nemotron's "Whisper is English-leaning" framing is incorrect for `large-v3` (99-language training corpus, including German); ChatGPT's reading aligns with both the upstream Whisper model card and the Swiss German PoC scope. Distil-Whisper is acknowledged as an optional English-only optimisation but explicitly **not** the baseline because the PoC may encounter multilingual or code-switched meetings before scope is frozen.

**Sources.** [ChatGPT R-04](notes/r-04-asr-provider-strategy-findings-chatgpt-deep-research.md); [Nemotron R-04](notes/r-04-asr-provider-strategy-findings-nemotron-3-nano-30b-a3b.md).

---

### 3.4 R-05 — Diarization Provider Strategy

**Recommendation.** Use **`pyannote/speaker-diarization-community-1`** as the sole local diarisation model behind the Diarization Port, with a **scenario-aware invocation strategy** rather than a uniform application:

- **`online_dual_stream`.** The local microphone is the primary attribution signal. The local user's speech is *structurally known* (it came from their mic) and does **not** require model-based diarisation. Run `community-1` only on the **remote/system stream**, and only when it adds value (i.e. multiple remote speakers). Do **not** feed the combined dual-stream into `community-1` — its model card explicitly says it ingests mono and downmixes stereo by averaging channels, which is exactly the structural separation we want to preserve.
- **`in_person_single_mic`.** Run `community-1` offline on the room-mic recording. Treat the result as an *editable hypothesis*, not authoritative. Surface uncertainty aggressively. Expect routine correction.

Persist three diarisation artifacts per session: the **regular diarisation timeline** (preserves overlap), the **exclusive diarisation projection** (single active speaker per moment, easier to align with transcript), and the **correction overlay** (separate from raw audio and raw model output).

Because `community-1` does **not** expose calibrated confidence scores, mena's product-level uncertainty layer must be built from heuristics: overlap presence, very short speaker turns, frequent speaker flipping, regular-vs-exclusive disagreement, speaker-count instability, and surrounding ASR/VAD signal quality. Confidence must be presented as a product signal, not a model signal.

When degradation is the right answer, **degrade gracefully to coarse labels** (`You`, `Remote speaker 1`, `Speaker unknown`, `Room`) rather than projecting confident-but-wrong identities. WhisperX integration is a possible reuse path because it already wraps pyannote — useful if the stack consolidates, optional otherwise.

**Convergences.**

- Both agents pick **pyannote `community-1`** as the primary local diarisation model.
- Both agree it can run locally, supports offline use, and fits a privacy-first architecture.
- Both agree DER on real meeting audio remains 15–20% even in best cases, so the architecture must assume routine human correction.
- Both agree diarisation should be optional per meeting (REQ-F14) and that overlap, short backchannels, and far-field audio are normal failure modes, not bugs.
- Both keep NeMo / Kaldi / SpeechBrain as deferred fallbacks rather than PoC defaults.

**Divergence — multichannel handling.** ChatGPT explicitly notes that `community-1` is **mono-only** (model card: stereo/multichannel input is downmixed by averaging) and recommends an asymmetric scenario-aware design. Nemotron asserts pyannote provides "multichannel support" with separate-channel diarisation yielding 5–7% DER on dual-channel data.

**Resolution.** ChatGPT's reading is correct — the model card from pyannote is unambiguous on the mono input. Nemotron's "multichannel pyannote" is unsupported by the upstream model card and contradicts the design rationale of `community-1`. **Adopt ChatGPT's scenario-aware design.** This also aligns better with mena's existing architecture: §8.1 already says local-mic carries structural attribution for `online_dual_stream`; using diarisation only on the remote stream is the architecturally consistent choice, not a workaround.

A second sub-divergence: Nemotron proposes a fixed confidence threshold of 0.6 and turn-level confidence flags. ChatGPT (correctly) notes the open-source `community-1` does not expose those scores. Resolution: build the uncertainty layer from heuristics (per ChatGPT), not from model-internal confidence.

**Sources.** [ChatGPT R-05](notes/r-05-diarization-provider-strategy-findings-chatgpt-deep-research.md); [Nemotron R-05](notes/r-05-diarization-provider-strategy-findings-nemotron-3-nano-30b-a3b.md).

---

### 3.5 R-06 — Fallback Provider Policy

**Recommendation.** Direct Mistral API summarisation fallback is **conditionally allowed for the PoC, disabled by default, and explicit at the user surface**. The allow-conditions are operator-attested *contractual* facts that the application **cannot** programmatically verify; they must be present in product configuration before the fallback may be enabled:

1. **Organisation-controlled commercial Mistral AI Studio account** (business use, processor/DPA posture applies). Personal/consumer accounts do not satisfy this.
2. **Scale-tier (or stronger commercial) subscription.** The Experiment tier is excluded — its data may be used for training unless explicitly opted out.
3. **Written Zero Data Retention approval for the specific account/workspace.** ZDR is a *support-reviewed, discretionary* request per Mistral's public documentation; it is **not** a self-service flag and **not** queryable from the API.
4. **Dedicated workspace + workspace-scoped API key** for mena, with rotation and expiry.
5. **Default EU-served path only.** Explicit US endpoint use is prohibited.
6. **Plain-text chat-completions only.** Audio transcription, file uploads, OCR, Agents, Fine-Tuning, Batch, Connectors, Libraries, and Web Search are out of scope for the fallback (all have different retention semantics).
7. **Allow-list of standard non-Labs text models.** Any model ID prefixed `labs` is rejected automatically (Labs has separate training rules in the commercial terms).
8. **No brokered access** (e.g. OpenRouter): direct Mistral API only.
9. **Per-session user consent** (explicit user action, not a silent background fallback) with a clear disclosure that only summarisation text leaves the device, audio/transcription/diarisation remain local, and the remote path depends on operator-attested provider configuration.

When the operator has **not** supplied this evidence, the product behaviour must be **"remote summarisation fallback unavailable"** with a crisp reason — *not* "best-effort remote fallback".

The application **can** technically verify a narrow perimeter: that it is calling the configured direct Mistral host (not a broker), that requests are text-only, that the path is `/v1/chat/completions`, that the model is on the internal allow-list and not `labs/*`, and that the key is syntactically valid. The application **cannot** verify plan tier, training opt-out state, ZDR approval, subprocessor roster, order-form overrides, temporary extra-EU transfers, or contractual carve-outs. Those are operator/contract documentation, not API truth.

**Residual risks** that remain even after a compliant setup must be documented in product copy, not hand-waved: ZDR does not mean zero metadata retention (account/invoice/support records have their own retention); training and handling carve-outs exist for feedback, automated moderation, and Labs models; routing is EU-default but not absolutely EU-exclusive; published DPA schedule treats "special categories of personal data" as None, which is not a safe default for arbitrary meeting transcripts.

**Convergences.**

- Both agents say **direct Mistral may be the PoC fallback** for summarisation, gated by ZDR, EU residency, training opt-out, no remote audio, and no broker access.
- Both agree the architecture must enforce text-only, plain chat-completions, and direct API key usage.
- Both agree fallback must be deactivated when policy state cannot be confirmed.

**Divergence — the verifiability of ZDR.** Nemotron asserts ZDR is **programmatically verifiable** via a `metadata.retention_mode = "zero"` response field, a `GET /v1/metadata` endpoint, and a `Mistral-Data-Retention: zero` request header that activates ZDR per request — and that ZDR is available to any account tier upon request. ChatGPT, citing Mistral's actual public help-centre, privacy-policy, commercial-terms, and DPA materials as of March/April 2026, says ZDR is **not** API-queryable: it is a **support-reviewed, discretionary** request, the default API posture is 30-day rolling retention for abuse monitoring, and ZDR scope is set at account/workspace level, not per request.

**Resolution.** ChatGPT's reading is the conservative, sourced reading and is the one mena's policy adopts. Nemotron's claims about a `/v1/metadata` endpoint, a `metadata.retention_mode` response field, and a per-request `Mistral-Data-Retention: zero` header **could not be verified against current Mistral public documentation** and conflict with ChatGPT's research; treating them as truth would expose mena to a silent compliance gap (the product would believe it had verified ZDR when it had not). The architecture therefore treats **ZDR as an operator-attested contractual predicate**, with mena's runtime check restricted to the narrow technical perimeter described above. The fallback is **disabled by default** and requires explicit operator configuration plus per-session user consent. This conservative posture is consistent with §1.4 priority #2 (sovereignty/privacy outranks convenience).

A secondary divergence: Nemotron suggests SCC-based cross-border transfers are routine and should be documented in privacy notice text. ChatGPT correctly notes the EU-default-but-not-EU-exclusive routing posture and that only EU-region users get the cleanest jurisdictional story. Resolution: the PoC fallback must use the EU-served default path and prohibit explicit US endpoint use; SCC documentation is operator-side rather than runtime configuration.

**Sources.** [ChatGPT R-06](notes/r-06-fallback-provider-policy-findings-chatgpt-deep-research.md); [Nemotron R-06](notes/r-06-fallback-provider-policy-findings-nemotron-3-nano-30b-a3b.md).

---

### 3.6 R-08 — Internal Protocol Implementation Strategy

**Recommendation.** Use **dedicated `stdio` between the Session Orchestrator Process and the Capture Worker Process for PoC v0**, with the orchestrator spawning the worker as a child process. Concretely:

- Orchestrator → worker traffic on the child's `stdin`.
- Worker → orchestrator traffic on the child's `stdout`.
- `stderr` reserved for human-oriented diagnostics; **never** mixed with protocol traffic.
- Wire format: **compact UTF-8 JSON, one top-level object per LF-terminated line.** No pretty-printing on the wire.
- Every frame is a single object with a stable envelope: `v` (protocol version), `type`, `id`, `session_id`, `payload`. The first message after spawn is `hello` carrying `protocol_major`, `protocol_minor`, `role`, and an optional build identifier — this performs the version-agreement step §6.10 already requires.
- Carry only **live coordination**: commands, responses, events, status, and *references* to durable artifacts already written to disk. Never carry raw audio, PCM chunks, large transcript payloads, or canonical session-record snapshots.
- **No heartbeats in v0.** Use OS-level peer-death signals (EOF on read, EPIPE on write, SIGPIPE, exit codes, startup/shutdown timeouts). Add heartbeats only on evidence of live-but-stuck workers that those signals miss.

**Versioning.** Incompatible changes bump the protocol major and fail the session immediately. Compatible changes within a major may add optional fields, but unknown message types are protocol errors, not silent ignores. Durable file schemas are versioned **separately** from the live protocol — the protocol exists for coordination only.

**Testing posture.** Treat the protocol boundary like a public interface even though it's private: a short normative protocol note, golden fixtures per message type and lifecycle transition, contract tests for round-trip encode/decode, unknown-version rejection, malformed JSON, truncated frames, oversize messages, missing required fields, illegal state transitions, unexpected EOF, and orderly shutdown. **At least one integration suite must spawn a real worker over actual stdio**, not an in-memory mock — Python/Node/Rust all warn that piped subprocess I/O can deadlock when output is not drained, and that class of bug is invisible to unit tests.

**Future evolution triggers.** If line-delimited JSON feels too ad hoc, move to RFC 7464 JSON Text Sequences. If richer request/response semantics are needed, move to LSP-style `Content-Length` framing. If transport topology becomes the issue, move to a Unix-domain `socketpair(AF_UNIX, SOCK_SEQPACKET, ...)` with the same JSON envelope on top. **Do not** jump to JSON-RPC, gRPC, or Protocol Buffers in v0 — none of the triggers (multiple workers, multiple languages, repeated compatibility pain, large message catalogue) is yet present.

**Convergences.** Single-source recommendation (ChatGPT only). Direction is consistent with the architecture overview's §6.10 internal-protocol rules and §8.5 schema-and-protocol-versioning rules.

**Divergences.** None — Nemotron did not produce R-08.

**Sources.** [ChatGPT R-08](notes/r-08-internal-protocol-implementation-strategy-findings-chatgpt-deep-research.md). Companion note: [local-two-process-protocol-patterns.md](notes/local-two-process-protocol-patterns.md).

---

### 3.7 R-09 — Privacy-Preserving UX Telemetry Design

**Recommendation.** **Keep automatic outbound telemetry absent in PoC v0.** Confirm AD-014 (Defer privacy-preserving UX telemetry) as the right decision. If telemetry is ever introduced in a future version, the *only* model compatible with mena's privacy-first posture is:

- **No background network submission, no third-party analytics SDKs, no persistent user/device/installation identifiers.**
- A **dedicated local-only product-health store**, with its own retention and deletion semantics, outside the canonical session-bundle namespace.
- **Local-only by default**, manually exported by explicit user action via a CLI flow that previews exactly what will be shared.
- **Closed-vocabulary metrics only.** No free-form strings. No raw exception messages, file paths, URLs, hostnames, prompt text, meeting titles, filenames, or argument strings.
- A **schema registry and privacy review gate** for every metric (declared owner, declared purpose, expiry/review date, type-checked recording, refusal to ship undeclared metrics).
- **User-legibility features**: a local viewer, a reset/delete control, an export builder with field-class preview.
- **Engineering guardrails**: automated tests/policy checks that reject telemetry fields containing raw text, paths, URLs, stable IDs, exact timestamps, or content-bearing debug artifacts. Loss of network connectivity must never degrade local operation.

**Red lines** (non-negotiable, document now):

- Never collect meeting substance — no transcript text, summary text, action items, embeddings, audio snippets, screenshots, or content-derived keywords.
- Never collect participant identity or participant-linked surrogates — no names, emails, calendar attendees, voiceprints, or any token that tracks the same person across meetings.
- Never collect free-form strings.
- Never ship crash dumps or memory-bearing diagnostics by default.
- Never use session replay, heatmaps, or full behavioural chronology.
- Never rely on hashed or pseudonymous stable IDs as a workaround — pseudonymisation is not anonymisation under EDPB and GDPR.

**Allowed categories** (only after AD-014 is unparked, with explicit user opt-in):

- Operational health and reliability (categorical/bucketed, never free-form): app version, enabled local models/backends, startup success/failure class, stage success/failure classes, storage pressure, crash class counts.
- Performance buckets (coarse ranges only, e.g. "under 1 minute / 1–5 minutes / over 5 minutes"; never exact durations).
- Closed-vocabulary feature-flow counters: `recording_started`, `recording_stopped`, `summary_requested`, `export_used`, `settings_opened`, `fallback_backend_selected`.
- Explicitly user-volunteered judgement (thumbs-up/down on a summary; small closed-list reason codes), only when surfaced via a manual export package.

**Convergences.**

- Both agents treat telemetry as a privacy-sensitive feature that must not be shipped casually.
- Both agree no transcript text, no participant identity, no free-form strings.
- Both agree on a separate telemetry storage boundary distinct from session artifacts.

**Divergence — outbound telemetry mechanics.** Nemotron recommends **enabling telemetry now** with a sophisticated pipeline: Local Differential Privacy (LDP, ε ≤ 1) noise on the client, Oblivious HTTP (OHTTP) transport, and Distributed Aggregation Protocol (DAP) on the server side. ChatGPT recommends **keeping telemetry absent** in the PoC and, if ever introduced later, restricting it to a local-only short-retention store with manual user export.

**Resolution.** ChatGPT's posture is the correct fit for mena. Nemotron's LDP+OHTTP+DAP stack is a credible *future* architecture (it is what entity-grade privacy-preserving telemetry actually looks like — Apple, Mozilla Glean, IETF DAP) but it is wildly disproportionate for a PoC whose core promise is local-first sovereignty and whose v0 explicitly defers telemetry (AD-014). Adding LDP+OHTTP+DAP to a PoC introduces a relay service, an aggregation backend, key rotation infrastructure, and a privacy-budget accounting system — all of which are net-new attack surface and operational burden, in exchange for product insight that the PoC explicitly says it does not need yet (per §11.3 architecture-debt entry on telemetry). The architecture priority order (§1.4 priority #2 sovereignty > #6 output quality / user trust) supports keeping telemetry absent until research provides a concrete model that respects local-first invariants. **Confirm AD-014 (Deferred); document the red lines now so future work cannot drift around them.**

**Sources.** [ChatGPT R-09](notes/r-09-privacy-preserving-ux-telemetry-design-findings-chatgpt-deep-research.md); [Nemotron R-09](notes/r-09-privacy-preserving-ux-telemetry-design-findings-nemotron-3-nano-30b-a3b.md).

---

## 4. Architecture Updates Triggered by This Synthesis

The following architecture updates are made as part of this synthesis. Each is recorded as a Decision Log entry in §9.1 of the architecture overview.

| ID | Topic | Architecture Section Touched | Status After Synthesis |
| --- | --- | --- | --- |
| AD-021 | PoC default LLM runtime adapter is **Ollama**, with owned `llama.cpp` (`llama-server`) as the first hardening adapter once model-dependent workflow code exists; vLLM is not in PoC scope. Default model profile: 3B–4B quantised instruct; 1B–3B fallback on ≤8 GB RAM machines. | §4.3, §8.4 (provider model), Variability Map | Confirmed |
| AD-022 | PoC primary ASR adapter is **`faster-whisper`** with hardware-selected Whisper-family profiles; `large-v3-turbo` is the quality ceiling for strong machines, `large-v3` is an explicit quality rerun, and `whisper.cpp` is the first fallback adapter. Provider metadata (checkpoint, decode parameters) is persisted with every transcript. | §8.4, §8.9 (variability) | Confirmed |
| AD-023 | PoC diarisation adapter is **`pyannote/speaker-diarization-community-1`**, invoked **scenario-aware**: not run on the local mic in `online_dual_stream` (structural attribution suffices); run on the remote/system stream only when value-adding; run on the room mic for `in_person_single_mic`. Confidence is a product-level heuristic layer, not a model signal. | §8.4, §8.1 (capture model), §8.10 (artifacts) | Confirmed |
| AD-024 | The Linux PoC capture realisation for `online_dual_stream` is **PipeWire-first with a session-scoped dedicated null sink**: explicit user confirmation of the meeting playback stream, move only that stream into the null sink, capture the null-sink monitor as the remote stream, separate loopback to the user's actual playback device, direct microphone capture as the local stream. Tiered fallback: `mic + default_sink_monitor` → `mic_only`, with degradation events written to session metadata. | §5.4 (informative addendum), §8.1 | Confirmed |
| AD-025 | The PoC internal-protocol transport is **stdio with newline-delimited compact UTF-8 JSON**, one object per frame, with an explicit `hello` version-agreement message. `stderr` is diagnostics-only. Files carry durable/bulky data; the protocol carries references. No JSON-RPC, gRPC, or Protocol Buffers in v0. | §6.10, §8.5, §8.7 | Confirmed |
| AD-026 | ZDR-bearing direct Mistral API summarisation fallback is gated by **operator-attested contractual predicates** (commercial Scale-or-stronger account, written ZDR approval, dedicated workspace, EU-served default endpoint, plain-text chat-completions only, non-Labs allow-listed models, direct API only) **plus per-session user consent**. The application verifies only the narrow technical perimeter (host, path, model allow-list, text-only payload). ZDR is **not** programmatically verifiable. Fallback is **disabled by default**; missing operator evidence yields "fallback unavailable", not best-effort remote calls. | §2.3, §8.4, §11.2 | Confirmed (refines AD-010) |
| AD-027 | UX telemetry remains **absent in PoC v0** (confirms AD-014 for the PoC horizon). When introduced later, the only architecturally compatible model is local-only short-retention storage with manual user export, no persistent identifiers, closed-vocabulary metrics, and a documented red-line list (no meeting substance, no participant identity, no free-form strings, no crash dumps by default, no session replay, no pseudonymous stable IDs). | §8.10 (UX telemetry separation), §11.3 | Deferred (red lines documented now) |

The Decision Log entries are added to [docs/architecture/architecture-overview.md §9.1](../architecture/architecture-overview.md#91-decision-log) in the same change set as this synthesis.

---

## 5. Research Backlog Status Updates

| Task | Status before | Status after | Reason |
| --- | --- | --- | --- |
| R-01 Local Model Runtime Strategy | Ready | Done | Synthesised in §3.1; AD-021 records the PoC decision. |
| R-02 Linux Audio Capture and Routing Strategy | Ready | Done | Synthesised in §3.2; AD-024 records the Linux PoC pattern. |
| R-04 ASR Provider Strategy | Ready | Done | Synthesised in §3.3; AD-022 records the PoC ASR adapter choice. |
| R-05 Diarization Provider Strategy | Ready | Done | Synthesised in §3.4; AD-023 records the scenario-aware diarisation strategy. |
| R-06 Fallback Provider Policy | Ready | Done | Synthesised in §3.5; AD-026 refines AD-010 with operator-attestation rules. |
| R-08 Internal Protocol Implementation Strategy | Ready | Done | Synthesised in §3.6; AD-025 records the stdio + line-delimited JSON decision. Single-source (ChatGPT only); a future Nemotron run is not required. |
| R-09 Privacy-Preserving UX Telemetry Design | Ready | Done | Synthesised in §3.7; AD-027 confirms AD-014 and documents red lines. |
| R-03 Future OS Compatibility Implications | Hold (waiting for R-02) | Ready | R-02 has produced provisional Linux capture guidance (PipeWire-first, dedicated null sink, profile-aware capture port). R-03 can now investigate macOS/Windows analogues against that baseline. |
| R-07 Install / Update / Uninstall Path for Local CLI Distribution | Hold (waiting for R-01, R-02, R-04, R-05) | Ready | R-01 (Ollama default), R-02 (PipeWire user-session adapter), R-04 (`faster-whisper` + `whisper.cpp`), and R-05 (`pyannote.audio`) all produce concrete dependency surfaces. R-07 can now plan packaging, model preseed, runtime install, and uninstall paths against those concrete dependencies. |

The backlog index [docs/research/backlog/architecture-research-backlog.md](backlog/architecture-research-backlog.md) is updated in the same change set.

---

## 6. Limitations and Caveats

- **Single-source items.** R-08 has only a ChatGPT note; R-09 had a Nemotron note that was rejected on resolution. Neither gap is judged severe enough to delay this synthesis: R-08's recommendation is consistent with the architecture overview's existing protocol-versioning and process-supervision rules, and R-09's resolution is to *defer* (which AD-014 already does).
- **No measured benchmarks.** Both agents' performance numbers (tokens/sec, DER, WER, latency) are drawn from third-party documentation, model cards, and published benchmarks — none from mena hardware. The validation steps each note proposes (representative-tier hardware testing, real meeting corpus, edit-burden measurement) are PoC implementation work, not synthesis work.
- **Provider/vendor truth as of April 2026.** R-01 references model sizes and runtime versions current at the time of writing. R-06 references Mistral's public legal/privacy materials current at March/April 2026. Both are time-bounded; if Mistral's terms or runtime ecosystems shift, AD-021/AD-022/AD-023/AD-026 should be revisited.
- **No code in this synthesis.** Per the issue scope, this is documentation work. No implementation has been started or implied.

---

*End of synthesis. Subsequent research promotion (R-03, R-07) and PoC implementation issues should cite this document for the underlying recommendations rather than duplicating the source notes.*
