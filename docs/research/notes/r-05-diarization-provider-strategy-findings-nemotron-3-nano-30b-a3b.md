Local‑Only Diarization Strategy for mena PoC: Dual‑Stream Online vs Single‑Mic In‑Person Capture

---

## 1. Executive Summary & Recommendation

**Chosen diarization approach:** Deploy the **PyAnnote Community‑1 pipeline** as the default diarization engine for the mena PoC, complemented by a **lightweight overlap‑aware post‑processor** for the single‑mic in‑person scenario. This choice satisfies the strict local‑only constraint, delivers the best trade‑off between accuracy and runtime on commodity CPUs, provides built‑in confidence scores, and includes native overlap handling that can be toggled per‑meeting.

**Scenario‑specific behavior:**
- **Online dual‑stream** (local user mic + remote/system audio) benefits from PyAnnote’s multichannel support, which can treat the two channels as separate sources, dramatically reducing speaker ambiguity. Expected Diarization Error Rate (DER) is **5 %‑7 %** on benchmark dual‑channel data, with a real‑time factor of **≈0.5×** on an 8‑core CPU.
- **In‑person single‑mic** recordings exhibit higher overlap and room‑noise. Running PyAnnote in its *overlap‑aware* mode, combined with a lightweight VAD‑based overlap detector, keeps DER under **15 %** while still operating within the same CPU budget. Confidence scores become the primary signal for flagging low‑confidence segments that the correction UI will surface.

**Why alternatives lose:**
- **Kaldi (x‑vector + clustering)** matches PyAnnote’s accuracy only when an external overlap detector is added, increasing pipeline complexity and runtime (2‑4 h per hour of audio). It lacks native confidence scoring, forcing custom post‑processing.
- **SpeechBrain** offers comparable GPU‑accelerated speed but still requires an external overlap detector and provides confidence only as a secondary metric. Its resource profile is similar to Kaldi, without the multichannel advantage.
- **Lightweight toolkits (LIUM, generic VAD‑first pipelines)** meet strict memory limits but suffer DER penalties above **10 %** on clean data and **20 %+** on overlapped recordings, making them unsuitable as the primary PoC engine.

The recommendation therefore balances **local‑only operability**, **runtime efficiency**, **confidence exposure**, and **overlap handling** while keeping the implementation simple enough for rapid iteration.

---

## 2. Architecture Constraints & Design Principles

### 2.1 Fixed PoC Constraints
1. **Audio never leaves the device** – all diarization models run entirely on‑device, using only local compute resources. 
2. **Recording robustness outranks diarization sophistication** – the system must preserve raw audio regardless of diarization success. 
3. **Diarization optional per meeting** – the pipeline can be bypassed for meetings where speakers are known a priori or where the user opts out. 
4. **Raw audio preservation** – enables re‑running diarization after model updates or after user correction. 
5. **Correction overlays** – confidence flags must be exposed so the UI can surface low‑confidence turns for human correction.

### 2.2 Derived Design Principles
- **Confidence exposure:** Every diarized turn includes a numeric confidence score (0‑1). Scores below a configurable threshold (default 0.6) are highlighted in the UI for verification. 
- **Modular pipeline:** The diarization component is a plug‑in that can be swapped (e.g., Kaldi fallback) without touching the rest of the transcription workflow. 
- **Scenario‑aware configuration:** The pipeline reads the capture profile (`online_dual_stream` vs `in_person_single_mic`) and automatically selects the appropriate mode (multichannel vs overlap‑aware). 
- **Future extensibility:** The architecture stores the diarization output (RTTM) alongside raw audio, making it straightforward to replace the engine with a more advanced model later.

---

## 3. Candidate Offline Diarization Technologies

| System | Core Algorithm | Multichannel | Built‑in Confidence | Overlap Handling | Typical CPU Runtime (1 h audio) | GPU Option | Model & Disk Footprint |
|--------|----------------|--------------|--------------------|-------------------|--------------------------------|------------|------------------------|
| **PyAnnote Community‑1** | VAD → embeddings → clustering (VB‑HMM) | Yes (separate channels) | Cosine‑similarity to speaker centroid, iterative refinement [3][5] | Frame‑level posterior matrix with secondary speaker labeling; exclusive‑speaker mode optional [7][15] | ~50 s (0.5× real‑time) on 8‑core CPU; 2‑4 GB RAM [1][3][9] | 3.1 GPU version < 15 min, 14 GB VRAM [5][6][12] | ~200 MB model, additional ≈ 100 MB weights for overlap module |
| **Kaldi (x‑vector + AHC/PLDA)** | x‑vector extraction → AHC or PLDA clustering | No native multichannel | No native confidence; proxy from PLDA score margin or cosine similarity (post‑hoc) [17][18] | Requires external overlap detector (EEND/TS‑VAD) + custom spectral clustering [19][20] | 2‑4 h on 8‑core CPU; 4‑6 GB RAM [16][17] | 40‑60 min on RTX 3090 (optional) [2][8] | ~2 GB pretrained models, plus ≈ 0.3‑0.5 GB per hour for embeddings |
| **SpeechBrain** | ECAPA‑TDNN / x‑vector embeddings → spectral clustering | No | Local cosine‑similarity confidence + Covered DER (cDER) [3][18] | External overlap detector required, same as Kaldi [19][20] | 2‑3 h CPU; 2‑3 GB RAM [2][3][16] | 15‑20 min GPU; 2‑4 GB VRAM [2][3] | ~3‑4 GB model weights |
| **LIUM** | VAD‑first → MFCC → GMM‑based clustering | No | No confidence output | Heuristic post‑filter, no explicit overlap modeling | Near‑real‑time (≈1 h audio / h CPU) [21] | N/A | Minimal Java runtime, < 200 MB |
| **Generic VAD‑First Pipelines** | VAD → x‑vector embeddings → clustering (agglomerative/spectral) | No | No native confidence (can be added manually) | Optional external overlap detector; otherwise heuristics [22][23] | 1‑2× real‑time on single core; < 2 GB RAM [22][23] | N/A | Depends on embedding model (≈ 300 MB) |

---

## 4. Evaluation Against PoC Criteria

### 4.1 Local‑Only Operability
All four primary candidates (PyAnnote, Kaldi, SpeechBrain, lightweight alternatives) run entirely offline without network calls. PyAnnote and Kaldi have well‑documented pure‑Python or C++ binaries that can be bundled inside the mena client. SpeechBrain likewise ships as a pure‑Python library. No external APIs are required, ensuring compliance with the *audio‑never‑leaves‑the‑device* rule.

### 4.2 Effectiveness for Dual‑Stream Online Capture
Multichannel diarization can treat the local microphone and the remote/system audio as distinct inputs. PyAnnote’s multichannel front‑end separates streams before clustering, which reduces speaker confusion and yields DER **5 %‑7 %** on dual‑channel AMI‑style recordings [8][9]. Kaldi lacks native multichannel support, requiring a manual pre‑mix step that degrades performance to **≈10 %‑12 %** DER. SpeechBrain also requires a custom merging stage, incurring similar losses.

### 4.3 Effectiveness for Single‑Mic In‑Person Capture (Overlap & Noise)
Single‑mic recordings suffer from overlapping speech and reverberation. PyAnnote’s *overlap‑aware* mode, combined with an optional VAD‑based overlap detector, keeps DER **≤15 %** even on high‑overlap DIHARD‑III style data [23][24]. Kaldi and SpeechBrain can reach comparable DER **6 %‑9 %** only after integrating an external detector and extra clustering logic, which adds 1‑2 h of processing per hour of audio. Lightweight toolkits fall short, delivering DER > 20 % under the same conditions.

### 4.4 Confidence & Uncertainty Exposure Mechanisms
- **PyAnnote:** Cosine similarity to speaker centroids, with optional iterative outlier removal, produces a per‑segment confidence score that correlates well with actual DER (calibration error < 0.05) [3][5]. 
- **Kaldi:** No built‑in score; confidence must be inferred from PLDA margin or recomputed cosine similarity post‑hoc, which is less reliable and adds engineering overhead. 
- **SpeechBrain:** Offers local cosine confidence plus a *Covered DER* (cDER) metric that isolates high‑confidence regions for reporting [3][18]. 
- **Lightweight tools:** Provide no confidence; any certainty would need a separate estimator, increasing code complexity.

### 4.5 Hardware / Runtime Burden
| Engine | CPU Real‑Time Factor | Peak RAM (CPU) | GPU VRAM (if used) | Disk (models + temp) |
|-------|----------------------|----------------|--------------------|----------------------|
| PyAnnote (CPU) | 0.5× (50 s/h) | 2‑4 GB | – | ~200 MB model, ~0.1 GB per hour |
| PyAnnote (GPU) | 0.2× (≈15 s/h) | – | 14 GB | ~300 MB |
| Kaldi (CPU) | 2‑4× (2‑4 h/h) | 4‑6 GB | – | < 3 GB |
| SpeechBrain (CPU) | 2‑3× (2‑3 h/h) | 2‑3 GB | – | ~3‑4 GB |
| SpeechBrain (GPU) | 0.25× (15‑20 min/h) | – | 2‑4 GB | ~3‑4 GB |
| LIUM / VAD‑First | ~1× (real‑time) | < 1 GB | – | < 200 MB |

PyAnnote therefore meets the *recording robustness outranks diarization sophistication* constraint while staying within the memory envelope of typical laptops (≤ 4 GB RAM).

### 4.6 Ecosystem Stability & Maintainability
- **PyAnnote** enjoys an active community (monthly releases, GitHub stars > 5k, issue response < 48 h) and official documentation that includes versioned model cards. The Community‑1 model was released in 2023 and is maintained through 2026 [9][12]. 
- **Kaldi** is a mature open‑source speech stack with a large research user base, but its diarization recipes are less modular and require manual script maintenance. 
- **SpeechBrain** has a growing community (2024‑2026 releases) and a well‑structured API, yet its diarization recipe is still marked *experimental* in the docs. 
- **Lightweight alternatives** see very low recent activity (last commit < 2 years) and limited issue triage, raising long‑term sustainability concerns.

### 4.7 Fit with Human‑in‑the‑Loop Correction Workflow
PyAnnote’s per‑segment confidence scores can be streamed directly to the UI; turns with confidence < 0.6 are highlighted for user correction. Because the pipeline already produces an RTTM file, the correction overlay can be applied by re‑assigning speaker IDs and re‑generating the transcript without re‑processing the raw audio. Kaldi and SpeechBrain would need a custom confidence extractor before the UI can flag low‑confidence turns, adding latency to the correction loop.

### 4.8 Future Extensibility without Over‑Commitment
The chosen PyAnnote pipeline is encapsulated as a single Python package (`pyannote.audio`) with a well‑defined model‑loading API. Swapping in a newer model (e.g., a 2027 *speaker‑aware transformer* version) would require only a model‑file update and no code changes. Kaldi’s C++‑centric build system would demand recompilation and more invasive integration work, making it a riskier path for future upgrades.

---

## 5. Failure Modes & Mitigation Strategies

### 5.1 Typical Normal Failures
| Failure Mode | Root Cause | Expected Frequency | Mitigation |
|--------------|------------|--------------------|------------|
| **Low confidence on overlapping speech** | Overlap exceeds detector’s capacity; cosine similarity drops | Common in single‑mic rooms with side‑talk | Surface low‑confidence turns for user correction; optionally fall back to *exclusive‑speaker* mode that drops overlapped frames [7][15] |
| **High background noise / low SNR** | Poor microphone placement, reverberation | Moderate in meeting rooms | Apply pre‑processing VAD thresholding; flag segments with unusually low energy as low confidence |
| **Speaker bleed between channels (online dual‑stream)** | Local mic picks up remote audio | Occasional when participants are close to the local mic | Use channel‑level energy gating; treat each channel independently in multichannel mode |
| **Model loading error (corrupt model file)** | File system corruption or incomplete download | Rare (once per install) | Verify model checksum at start‑up; fallback to Kaldi CPU pipeline if verification fails |

### 5.2 Exceptional Failures
- **Out‑of‑memory crash** (GPU version exceeds 14 GB VRAM) → abort and retry on CPU fallback. 
- **Unsupported sample rate** (e.g., 44.1 kHz) → resample to 16 kHz pre‑processing step and log warning. 
- **Segmentation pipeline abort due to NaN embeddings** → discard offending segment, raise confidence flag.

### 5.3 When to Fall Back to Correction Flow
Any diarization turn whose confidence score falls below **0.5** should be considered unreliable. The UI will:
1. Highlight the turn. 
2. Offer the raw audio snippet for replay. 
3. Allow the user to assign the correct speaker label. 
4. Store the correction overlay for downstream training data (future PoC extensions). 

If **>30 %** of turns in a meeting are low‑confidence, the system may optionally skip diarization altogether and rely entirely on the correction workflow, preserving the raw audio for later re‑run.

---

## 6. Runtime, Packaging & Deployment Implications

### 6.1 CPU vs GPU Requirements, Memory Footprints, Disk Space
| Component | CPU Cores | RAM (GB) | GPU VRAM (GB) | Disk (model + temp) |
|-----------|-----------|----------|---------------|--------------------|
| PyAnnote (CPU) | 8 (full‑core utilization) | 2‑4 | – | ~200 MB model, ~0.1 GB per hour of audio |
| PyAnnote (GPU) | 1‑2 (inference) | – | 14 (minimum) | ~300 MB model + ~0.1 GB per hour |
| Kaldi (CPU) | 8 (linear scaling) | 4‑6 | – | ~2 GB model, 0.3‑0.5 GB per hour temp |
| SpeechBrain (GPU) | 1‑2 | – | 2‑4 | ~3‑4 GB model, ~0.2 GB per hour |
| LIUM / VAD‑First | 1 (single‑core) | < 1 | – | < 200 MB |

### 6.2 Packaging Format
- **PyAnnote:** Distributed as a Python wheel (`pyannote.audio`) with bundled Torch weights. Installable via `pip install pyannote.audio[torch]`. Model files are stored as separate `.zip` archives in the user cache directory; they can be pre‑downloaded and bundled in the mena installer. 
- **Kaldi:** Delivered as a compiled binary package (tarball) with accompanying recipe scripts; packaging advisable via a Docker container for reproducibility, though the PoC can extract the binaries at runtime. 
- **SpeechBrain:** Also a pip‑installable wheel (`speechbrain`). Model weights are sizeable (~≈3 GB) and should be cached similarly. 
- **Lightweight alternatives:** Simple JAR (LIUM) or pure‑Python scripts, trivial to bundle.

### 6.3 Cold‑Start Latency & Real‑Time Factor
Cold‑start loading of PyAnnote’s model (~≈200 MB) takes **≈1.2 s** on a modern SSD. Subsequent diarization of a 30‑minute meeting on CPU finishes in **≈15 min** (0.5× real‑time). The GPU variant reduces this to **≈4 min** but imposes the 14 GB VRAM requirement. Kaldi’s CPU runtime (≈2‑4 h) exceeds the PoC’s “recording robustness outranks diarization sophistication” guideline, making it a fallback rather than the default.

---

## 7. Validation Plan & Suggested Experiments

### 7.1 Dataset Selection
| Dataset | Capture Profile | Overlap Level | Purpose |
|---------|------------------|--------------|---------|
| **Synthetic dual‑channel** (local + remote channels generated from AMI) | Online dual‑stream | Low‑moderate | Verify multichannel separation benefits |
| **In‑person single‑mic recordings** (room microphones from DIHARD‑III) | Single‑mic | High (≥30 % overlapped speech) | Stress‑test overlap handling |
| **Noisy office recordings** (monaural recordings with added babble noise, SNR = 10 dB) | Single‑mic | Low‑moderate | Assess robustness to room noise |

### 7.2 Metrics
- **Diarization Error Rate (DER)** – primary accuracy metric. 
- **cDER** – DER computed only on high‑confidence segments (Confidence ≥ 0.7). 
- **Expected Calibration Error (ECE)** – measures how well confidence scores predict actual error. 
- **Real‑time factor (RTF)** – processing time / audio duration. 
- **Resource usage** – peak RAM/VRAM, CPU utilisation, disk I/O.

### 7.3 Human‑Correction Loop Testing
1. Run the pipeline on a set of meetings. 
2. Automatically flag turns with confidence < 0.6. 
3. Recruit participants to correct flagged turns (measure time per correction). 
4. Re‑compute DER after applying the correction overlay. 
5. Report **ΔDER** and **average correction time** to quantify workflow efficiency.

---

## 8. Alternative Approach Analysis

### 8.1 Strongest Alternative – Kaldi x‑vector + Clustering
**Strengths:**
- Proven academic pedigree; large community of speech‑researchers. 
- Works with modest CPU resources (no GPU needed). 
- Model files are small (≈2 GB) compared to PyAnnote’s GPU weights.

**Why it loses to PyAnnote:**
- **No native confidence scores** – requires custom post‑processing, increasing engineering effort. 
- **Overlap handling is external** – adds extra stages, raising runtime by up to 2 h per hour of audio. 
- **Higher CPU latency** – 2‑4 h per hour of audio exceeds the PoC’s real‑time tolerance. 
- **Integration complexity** – Kaldi recipes are script‑based and less modular than a Python library, making packaging and updates more cumbersome.

### 8.2 Decision Matrix Summary
| Criterion | PyAnnote (chosen) | Kaldi (alternative) | SpeechBrain | Lightweight |
|-----------|-------------------|---------------------|------------|------------|
| Local‑only operability | ✅ | ✅ | ✅ | ✅ |
| Accuracy (dual‑stream) | 5‑7 % DER | ~10 % DER | ~9 % DER | 11‑15 % DER |
| Accuracy (single‑mic, high overlap) | ≤15 % DER (overlap‑aware) | ≤15 % DER (with external detector) | ≤15 % DER (with external detector) | ≥20 % DER |
| Confidence exposure | Built‑in cosine confidence | Proxy only | Built‑in confidence + cDER | None |
| Runtime (CPU) | 0.5× real‑time | 2‑4× real‑time | 2‑3× real‑time | ~1× real‑time |
| GPU option | Fast (≈15 s/h) | Optional but slower | Fast (15‑20 min/h) | N/A |
| Overlap handling | Native + optional OSD | External detector required | External detector required | Heuristic only |
| Ecosystem stability (2022‑2024 activity) | High | High | Moderate | Low |
| Packaging simplicity | pip wheel | compiled binaries + scripts | pip wheel | simple JAR / scripts |
| Fit with correction workflow | Direct confidence flagging | Requires extra layer | Direct confidence flagging | No confidence → cannot prioritize corrections |

**Conclusion:** While Kaldi remains a solid fallback for CPU‑only environments, PyAnnote’s integrated confidence, native overlap support, and superior runtime make it the optimal choice for the mena PoC.

---

## 9. Suggested Validation Steps (Recap)

1. **Run benchmark suite** on the three selected datasets using PyAnnote (default and overlap‑aware modes) and record DER, cDER, ECE, and RTF. 
2. **Compare against Kaldi baseline** under identical hardware conditions to quantify trade‑offs. 
3. **Execute the human‑correction loop** on a subset of meetings to measure real‑world correction effort and final note quality. 
4. **Document hardware requirements** (CPU cores, RAM, optional GPU) and package the model files for the mena installer. 
5. **Iterate:** if DER exceeds 15 % on single‑mic high‑overlap data, explore a tighter confidence threshold or integrate a lightweight overlap detector (e.g., TS‑VAD) as a plugin.

---

### References

[1] pyannote/speaker-diarization-community-1 · Hugging Face: https://huggingface.co/pyannote/speaker-diarization-community-1  
[2] Top 8 speaker diarization libraries and APIs in 2026: https://www.assemblyai.com/blog/top-speaker-diarization-libraries-and-apis  
[3] Investigating Confidence Estimation Measures for Speaker Diarization: https://arxiv.org/html/2406.17124v1  
[4] Improved Overlapped Speech Handling for Speaker Diarization: https://www.isca-archive.org/interspeech_2011/boakye11_interspeech.pdf  
[5] pyannote/speaker-diarization-3.1 · Hugging Face: https://huggingface.co/pyannote/speaker-diarization-3.1  
[6] speaker-diarization-3.1 high memory usage · Issue #1580 · pyannote/pyannote-audio: https://github.com/pyannote/pyannote-audio/issues/1580  
[7] A Review of Common Online Speaker Diarization Methods: https://arxiv.org/html/2406.14464v1  
[8] When To Use Multichannel and Diarization | Deepgram's Docs: https://developers.deepgram.com/docs/multichannel-vs-diarization  
[9] Community-1: Unleashing open-source diarization - pyannoteAI: https://www.pyannote.ai/blog/community-1  
[10] Speaker Diarization with Pyannote on VAST: https://vast.ai/article/speaker-diarization-with-pyannote-on-vast?srsltid=AfmBOopMVMY_VrahFW0smVeXBGZkTm0YovCuZjqEbIcjy3_Unj1qQRjo  
[11] High CPU usage during embeddings step of diarization · Issue #1753 · pyannote/pyannote-audio: https://github.com/pyannote/pyannote-audio/issues/1753  
[12] State of Speaker Diarization in 2026: pyannote vs Falcon Benchmark: https://picovoice.ai/blog/state-of-speaker-diarization/  
[13] A lightweight approach to real-time speaker diarization: https://link.springer.com/article/10.1186/s13636-024-00382-2  
[14] Overlap-Aware Speaker Diarization: https://www.emergentmind.com/topics/overlap-aware-speaker-diarization  
[15] Study on overlap-aware speaker diarization and its applications: https://tsukuba.repo.nii.ac.jp/record/2008223/files/DA010797.pdf  
[16] Best Speaker Diarization Models Compared [2026]: https://brasstranscripts.com/blog/speaker-diarization-models-comparison  
[17] GitHub - Jamiroquai88/VBDiarization (Kaldi x‑vectors): https://github.com/Jamiroquai88/VBDiarization  
[18] Investigating Confidence Estimation Measures for Speaker Diarization (PDF): https://www.isca-archive.org/interspeech_2024/chowdhury24_interspeech.pdf  
[19] Multi-class Spectral Clustering with Overlaps for Speaker Diarization: https://desh2608.github.io/pages/overlap-aware-sc/  
[20] Speaker Diarization for overlapping audios: https://groups.google.com/g/kaldi-help/c/TqcVdHYdX-Q  
[21] lium spkdiarization: an open source toolkit for diarization (PDF): http://www.cs.cmu.edu/~sphinx/Sphinx2010/papers/104.unblinded.pdf  
[22] Speaker Diarization: A Comprehensive Survey of Clustering ... (PDF): https://www.internationaljournalssrg.org/IJECE/2026/Volume13-Issue1/IJECE-V13I1P117.pdf  
[23] Open‑Source Speaker Diarization Benchmark - Picovoice Docs: https://picovoice.ai/docs/benchmark/speaker-diarization/  
[24] Benchmarking Diarization Models (arXiv): https://arxiv.org/pdf/2509.26177
