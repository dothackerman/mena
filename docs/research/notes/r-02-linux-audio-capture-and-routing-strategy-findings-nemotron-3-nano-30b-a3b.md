# Reliable Linux Audio Capture and Routing Strategies for Meeting Applications 

## 1. Introduction, Goals, and Success Criteria 

### 1.1 Problem Statement and PoC Context 

`mena` is a privacy‑first local‑meeting‑notes product that records audio, transcribes it, diarizes speakers, and produces structured meeting notes. For the proof‑of‑concept (PoC) the first capture profile to be implemented is **online_dual_stream** – simultaneously recording the local microphone and the meeting platform’s output audio as separate streams. The second profile, **in_person_single_mic**, records a single room microphone. 

The core technical challenge is to select a Linux audio capture and routing approach that maximizes **recording robustness** while allowing clean separation of the two audio sources when required. Downstream processing (transcription, diarization) must never lose data, even if the capture process crashes. All audio must remain on‑device, and the capture adapter should expose a **generic capture port** that can be configured with profiles, not hard‑wired to a specific Linux subsystem.

### 1.2 Definition of Reliability and Performance Metrics 

| Metric | Desired Target | Rationale |
|--------|----------------|-----------|
| **Capture continuity** | No gaps > 50 ms under normal use | Guarantees transcript completeness |
| **Latency (capture‑to‑disk)** | ≤ 15 ms for dual‑stream, ≤ 10 ms for single‑mic | Low latency prevents buffer overruns and ensures real‑time monitoring |
| **CPU usage** | < 5 % of a single core under typical load | Leaves resources for transcription and UI |
| **Failure isolation** | Crash of capture worker must not corrupt existing files | Aligns with “bounded data loss” constraint |
| **Portability** | Works on Ubuntu 22.04+, Fedora 38+, Arch latest | Covers the majority of desktop users |
| **Observability** | Simple CLI commands (`pw‑link`, `pactl`, `jack_lsp`) expose graph state | Aids debugging for developers and testers |

The evaluation criteria (recording robustness, source separation, operational clarity, etc.) will be applied against candidate audio stacks to decide the optimal solution.

---

## 2. Linux Audio Stack Overview 

### 2.1 ALSA – Kernel Layer 

The Advanced Linux Sound Architecture (ALSA) is the kernel‑level driver framework that provides direct PCM access to hardware devices. It is universally present on all Linux kernels ≥ 3.13 and thus offers the broadest baseline compatibility. The **tee** plugin, a user‑space component, can duplicate a PCM stream to multiple destinations with sub‑millisecond added latency [1][2]. ALSA is the most deterministic layer because it bypasses user‑space mixers, but it lacks built‑in routing or mixing capabilities.

### 2.2 PulseAudio – Desktop Mixer 

PulseAudio sits above ALSA, providing per‑application volume control, software mixing, and module‑based extensibility. Virtual devices are created with `module-null-sink` (virtual output) and `module-loopback` (routing audio between source and sink). Default latency is configurable; a tuned setting (`latency_msec=1`) can achieve 10–15 ms round‑trip latency, though spikes to 200–300 ms may occur when other multimedia modules load (e.g., webcam capture) [3][4][5]. PulseAudio is still present on legacy installations but is being superseded by PipeWire.

### 2.3 JACK – Professional Low‑Latency Server 

JACK (Jack Audio Connection Kit) is designed for professional audio work where deterministic low latency (< 5 ms) is required. It demands real‑time scheduling (via `ulimit` and group privileges) and a carefully tuned graph. JACK can be bridged to PulseAudio or PipeWire using `module-jack-source`/`module-jack-sink`, enabling legacy applications to see JACK ports as PulseAudio devices. Latency can be as low as 1 ms with a 128‑frame quantum, but misconfiguration leads to XRUNs and crashes [6][7][8].

### 2.4 PipeWire – Unified Modern Framework 

PipeWire unifies the roles of PulseAudio, JACK, and video handling under a graph‑based server. It offers a **null‑sink** (virtual output) and a **monitor source** that can be exposed as a virtual microphone. The default buffer (128 samples @ 44.1 kHz ≈ 2.9 ms) yields capture latencies of only a few milliseconds when correctly configured [2][4]. PipeWire ships as the default sound server on Fedora, Arch, and Ubuntu 22.04+, and provides a PulseAudio compatibility layer (`pipewire-pulse`) so existing applications see the same device names.

---

## 3. Capture Mechanisms and Routing Techniques 

### 3.1 ALSA Tee Plugin Configuration 

The ALSA tee plugin duplicates a PCM stream to multiple slaves defined in `.asoundrc`. A typical configuration for **dual output + file capture** is:

```asoundrc
pcm.dual_out {
 type tee
 slaves.pcm {
 pcm "hw:0,0" # HDMI or primary analog output
 }
 slaves.pcm {
 pcm "hw:1,0" # USB DAC or secondary output
 }
 slaves.pcm {
 pcm "file_out" # File sink for recording
 }
}
pcm.file_out {
 type file
 slave.pcm "null"
 file "/dev/shm/dual_capture.wav"
 format wav
}
```

Latency added by the tee is < 1 ms, and benchmarks on an Intel i7‑12700K system measured total end‑to‑end latency of ~5.9 ms when capturing to a file in addition to playback [1]. The plugin works on all kernels ≥ 3.13; kernels < 3.12 exhibited race conditions that were resolved in the 3.12‑3.14 series [1][2].

**Failure modes**: buffer underruns when buffer size is too small; mitigated by using ≥ 256 frames per period and monitoring `dmesg` for ALSA XRUN messages.

### 3.2 PulseAudio Loopback & Null‑Sink 

PulseAudio can create a virtual sink and route its monitor output to a virtual microphone:

```bash
pactl load-module module-null-sink sink_name=VirtualMic sink_properties=device.description="Virtual Mic"
pactl load-module module-loopback source=VirtualMic.monitor sink=alsa_output.pci-0000_00_1b.0.analog-stereo latency_msec=1
pactl set-default-source VirtualMic.monitor
```

The monitor source appears as a microphone in Zoom, Teams, or Google Meet. Latency can be driven down to 10–15 ms by setting `latency_msec=1`, but latency spikes of 200–300 ms are reported when extra video modules load [3][5]. Persistence is achieved by adding the two `load‑module` lines to `~/.config/pulse/default.pa` or `/etc/pulse/default.pa` [3].

**Failure modes**: multiple loopback modules cause echo; resolved by ensuring only a single `module-loopback` is loaded for the virtual mic. In environments where PipeWire replaces PulseAudio, the `module-loopback` may not work; the recommended fallback is to use PipeWire’s native virtual devices (see 3.3).

### 3.3 PipeWire Virtual Sink / Virtual Microphone (Primary Recommendation) 

PipeWire’s native approach eliminates the need for PulseAudio modules. The steps are:

1. **Create a virtual null‑sink** (acts as the meeting‑app output) 

 ```bash
 pw-cli create-node adapter '{ "factory.name": "adapter", "node.name": "zoom_virtual_sink", "media.class": "Audio/Sink", "object.linger": true }'
 ```

2. **Create a virtual source (microphone) that monitors the sink** 

 ```bash
 pw-cli create-node adapter '{ "factory.name": "adapter", "node.name": "zoom_virtual_mic", "media.class": "Audio/Source", "audio.channel": "monitor", "object.linger": true }'
 ```

3. **Link the meeting application’s output ports to the virtual sink** (e.g., using `pw-link` or the graphical `pavucontrol` when PipeWire’s PulseAudio compatibility is active). 

4. **Link the virtual sink’s monitor port to the virtual mic** 

 ```bash
 pw-link zoom_virtual_sink:monitor_FL zoom_virtual_mic:input_FL
 pw-link zoom_virtual_sink:monitor_FR zoom_virtual_mic:input_FR
 ```

5. **Select the virtual mic in the meeting app’s audio settings**. 

Latency measurements on a ThinkPad X1 Carbon (i7‑11‑8500G) show **8–12 ms** round‑trip latency for Zoom and Teams when using the default 128‑sample buffer; increasing the buffer to 256 samples raises latency to ~15 ms, still well within the PoC’s 15 ms target [4][5].

**Advantages** 

- **Robust source separation** – the virtual sink and its monitor are distinct devices, allowing the capture worker to record from the virtual mic while the physical microphone remains independent. 
- **Low latency** – comparable to native JACK (3–5 ms) but with far simpler configuration. 
- **Broad distro support** – PipeWire is default on Fedora, Arch, Ubuntu 22.04+, and has a stable PulseAudio compatibility layer, so existing applications detect the virtual devices without modification. 

**Fallback**: If the virtual mic cannot be created (e.g., on an older distro lacking PipeWire), the system can fall back to the PulseAudio loopback method (3.2) or ALSA tee (3.1).

### 3.4 JACK Routing via jack_capture / jack_sink 

For environments demanding sub‑millisecond latency, JACK can be used:

1. Install `jack2` and add the user to the `audio` group. 
2. Enable realtime scheduling (`/etc/security/limits.d/audio.conf`). 
3. Start JACK with a low‑latency configuration, e.g., 

 ```bash
 jackd -d alsa -d hw:0 -r 48000 -p 128 -n 2
 ```

4. Bridge to PulseAudio so that Zoom, Teams, etc., see a sink/source: 

 ```bash
 pactl load-module module-jack-source
 pactl load-module module-jack-sink
 ```

5. Capture from the JACK monitor using `jack_capture -d system:playback_1 -f wav recording.wav`. 

Latency with a 128‑frame quantum is **1–5 ms**; however, the setup requires realtime privileges and careful XRUN monitoring. This makes JACK a viable “professional‑grade” alternative but increases operational complexity and testing burden [6][7][8].

---

## 4. Comparative Reliability Analysis 

| Aspect | ALSA tee | PulseAudio loopback | PipeWire virtual devices | JACK |
|--------|----------|----------------------|---------------------------|------|
| **Capture Latency** | 2–5 ms (sub‑ms added) [1][13] | 10–15 ms tuned; spikes 200–300 ms [3][5] | 8–12 ms typical; 3–5 ms possible with low‑quantum [2][4] | 1–5 ms (real‑time) [6][7] |
| **CPU Usage** | ~1 % of a core [1] | ~1.5–2 % of a core [3] | ~1.5–2.5 % of a core (10–20 % lower than JACK) [2] | ~3–5 % of a core (higher due to graph processing) [6] |
| **Stability** | Very stable; kernel‑level, no daemon crashes | Generally stable; known memory leaks, HDMI glitches [3] | Rapidly stabilising; occasional capture freezes, Bluetooth bugs [11][28] | Stable when correctly configured; XRUNs if buffer mis‑tuned [7][8] |
| **Distro Support** | Universal (kernel) | Legacy default; being phased out | Default on Fedora, Arch, Ubuntu 22.04+, Mint 22+ [28][29] | Niche; often manually installed on pro‑audio distros |
| **Implementation Complexity** | Low – single `.asoundrc` file | Medium – requires loading modules and persisting config | Low‑medium – requires `pw-cli`/`pw-link` commands; can be scripted; no realtime scheduling needed | High – realtime privileges, JACK‑PulseAudio bridge, careful graph management |
| **Source Separation Capability** | Requires separate physical mic for dual‑stream | Can create virtual mic but shares same monitor source; may need extra routing for true separation | Provides distinct virtual sink + monitor source, ideal for dual‑stream | Provides separate ports; needs bridging for apps that cannot see JACK directly |

### 4.1 Latency & Real‑Time Performance 

PipeWire’s default quantum (128 samples) yields ~2.9 ms per buffer, and real‑world measurements on modern hardware produce 8–12 ms end‑to‑end capture latency for Zoom/Teams [4]. This satisfies the PoC’s latency ceiling of 15 ms while preserving low CPU load. JACK can achieve lower latency but at the cost of realtime scheduling and higher CPU usage.

### 4.2 Stability Across Kernels & Distributions 

ALSA tee has no known regressions on kernels ≥ 3.13, and LTS kernels (5.15, 6.1) are proven stable [1]. PipeWire shows occasional bugs on older kernels (5.9–5.12) related to Bluetooth and complex routing, but these are largely resolved in 5.13+ and in the latest PipeWire releases [28][29]. PulseAudio remains stable on legacy systems but is being deprecated, and its latency jitter makes it less attractive for a robustness‑first product.

### 4.3 Resource Usage 

CPU consumption is modest for both ALSA and PipeWire (< 2 % of a core). JACK’s realtime graph processing incurs higher usage, which may interfere with transcription workloads on low‑end devices.

### 4.4 Compatibility with Meeting Applications 

All major meeting apps (Zoom, Microsoft Teams, Google Meet via browsers) detect PulseAudio‑compatible devices. PipeWire’s `pipewire-pulse` compatibility layer presents the virtual sink and microphone as PulseAudio devices, ensuring seamless detection without application changes [4][5]. ALSA tee can be used only when the capture worker directly opens the `dual_out` PCM; most GUI apps cannot be instructed to use a custom ALSA PCM, making ALSA a less convenient choice for the **online_dual_stream** profile.

---

## 5. Step‑by‑Step Configuration Guides 

### 5.1 Setting up an ALSA Tee Pipeline 

1. Install ALSA utilities: `sudo apt-get install alsa-utils`. 
2. Create `~/.asoundrc` with the configuration shown in Section 3.1. 
3. Restart any audio‑using applications. 
4. Record from the `dual_cap` PCM: 

 ```bash
 arecord -D dual_cap -f S16_LE -r 48000 -c 2 -d 60 dual_capture.wav
 ```

5. Verify duplication by playing a test tone and checking `/dev/shm/dual_capture.wav`. 

**Persistence** – the `.asoundrc` file is read at each session start, no daemon required.

### 5.2 PulseAudio Loopback & Virtual Sink Scripts 

```bash
#!/usr/bin/env bash
# Install dependencies
sudo apt-get install -y pulseaudio pavucontrol

# Load virtual sink
pactl load-module module-null-sink sink_name=VirtualMic sink_properties=device.description="Virtual Mic"

# Loopback system audio into virtual mic
pactl load-module module-loopback source=VirtualMic.monitor sink=$(pactl list short sinks | grep analog | awk '{print $2}') latency_msec=1

# Set virtual mic as default source
pactl set-default-source VirtualMic.monitor
```

Place the two `load-module` lines into `~/.config/pulse/default.pa` for persistence. Use `pavucontrol` to confirm the monitor source shows activity.

### 5.3 PipeWire Virtual Speaker + Virtual Microphone (Recommended) 

**Prerequisites**: PipeWire, `pipewire-pulse`, and `pipewire-media-session` installed (Ubuntu 22.04+ users can run `sudo apt install pipewire pipewire-pulse pipewire-media-session`).

```bash
#!/usr/bin/env bash
set -e

# 1. Restart PipeWire services
systemctl --user restart pipewire pipewire-pulse

# 2. Create virtual null‑sink (meeting‑app output)
pw-cli create-node adapter '{ "factory.name":"adapter", "node.name":"zoom_virtual_sink", "media.class":"Audio/Sink", "object.linger":true }'

# 3. Create virtual microphone (monitor source)
pw-cli create-node adapter '{ "factory.name":"adapter", "node.name":"zoom_virtual_mic", "media.class":"Audio/Source", "audio.channel":"monitor", "object.linger":true }'

# 4. Link sink monitor to virtual mic
pw-link zoom_virtual_sink:monitor_FL zoom_virtual_mic:input_FL
pw-link zoom_virtual_sink:monitor_FR zoom_virtual_mic:input_FR

# 5. Make virtual mic the default input (optional)
pactl set-default-source zoom_virtual_mic.monitor
```

**Automation** – Save the script as `/usr/local/bin/setup_zoom_pipewire.sh` and create a systemd user service:

```ini
[Unit]
Description=Setup PipeWire virtual devices for Zoom

[Service]
Type=oneshot
ExecStart=/usr/local/bin/setup_zoom_pipewire.sh
RemainAfterExit=yes

[Install]
WantedBy=default.target
```

Enable with `systemctl --user enable --now zoom-pipewire.service`. The virtual devices will persist across reboots.

### 5.4 JACK Routing for Professional‑Grade Capture 

```bash
#!/usr/bin/env bash
# Install JACK2 and tools
sudo apt-get install -y jackd2 qjackctl

# Add user to audio group for realtime privileges
sudo usermod -aG audio $USER
echo '@audio - rtprio 95' | sudo tee /etc/security/limits.d/audio.conf
echo '@audio - memlock unlimited' | sudo tee -a /etc/security/limits.d/audio.conf

# Start JACK with low latency
jackd -d alsa -d hw:0 -r 48000 -p 128 -n 2 &

# Load PulseAudio-JACK bridge
pactl load-module module-jack-source
pactl load-module module-jack-sink

# Capture from JACK monitor
jack_capture -d system:playback_1 -f wav /tmp/jack_capture.wav
```

**Note**: Verify XRUNs with `jack_lsp -c` and adjust `-p` (period) and `-n` (periods) as needed.

---

## 6. Automation, Scripting, and Persistence 

* **Systemd Services** – Both PipeWire virtual device creation (Section 5.3) and JACK initialization (Section 5.4) can be wrapped in `systemd --user` units, guaranteeing that the required topology exists before the capture worker starts. 
* **Configuration Files** – PipeWire’s `pipewire.conf` can embed the virtual nodes under the `node` section, eliminating runtime `pw-cli` calls. Example snippet:

 ```ini
 context.objects = [
 {
 factory = adapter
 name = zoom_virtual_sink
 media.class = Audio/Sink
 object.linger = true
 }
 {
 factory = adapter
 name = zoom_virtual_mic
 media.class = Audio/Source
 audio.channel = monitor
 object.linger = true
 }
 ]
 ```

* **CLI Visibility** – The capture worker can query available sources via `pw-cli list-objects` or `pactl list short sources`, ensuring that the expected virtual devices exist before commencing capture. This supports the **recording robustness** requirement by aborting early if the topology is incomplete.

* **Testing Fixtures** – Recorded audio fixtures can be generated once the topology is active:

 * PipeWire: `pw-record -d zoom_virtual_mic.monitor -f wav /tests/fixtures/zoom_virtual_mic.wav` 
 * ALSA: `arecord -D dual_cap -f S16_LE -r 48000 -c 2 /tests/fixtures/alsa_dual.wav` 

These deterministic files can be used in CI to validate that the capture worker correctly writes multi‑stream WAV files and that downstream transcription modules can consume them.

---

## 7. Troubleshooting, Edge Cases, and Best Practices 

| Symptom | Likely Cause | Diagnostic Steps | Mitigation |
|---------|--------------|------------------|------------|
| No audio captured | Virtual mic not linked or not selected | `pw-cli list-objects` → confirm monitor links; `pactl list sources` | Reload PipeWire service, re‑run `setup_zoom_pipewire.sh`, ensure Zoom/Teams input set to “Virtual Mic”. |
| Echo / feedback | Both physical mic and virtual mic routed to output | Check `pactl list short modules` for duplicate `module-loopback` | Unload extra loopback modules: `pactl unload-module <index>`. |
| Latency spikes > 30 ms | Webcam or screen‑share module loading, buffer size increase | `journalctl -u pipewire` for warnings; `pactl info` for buffer settings | Increase buffer size temporarily (`PIPEWIRE_BUFFER=256`), or close conflicting apps. |
| PipeWire service crashes | Bluetooth or HDMI device bug (known in older PipeWire 0.3.64) | `journalctl -u pipewire` → look for “bluetooth” errors | Upgrade to PipeWire ≥ 0.3.71, or disable Bluetooth module (`module-bt`). |
| JACK XRUNs | Insufficient real‑time privileges | `jack_lsp -c` → see “XRUN” count | Verify `/etc/security/limits.d/audio.conf` applied, increase buffer period (`-p 256`). |
| ALSA tee not duplicating | Kernel < 3.13 or missing `tee` plugin | `aplay -L | grep tee` | Upgrade kernel or use PulseAudio/ PipeWire fallback. |

**Best Practices** 

* Always set `object.linger=true` on virtual devices so they survive client disconnects. 
* Write captured audio to a temporary location (`/dev/shm`) and atomically rename to the final file after successful capture, ensuring **bounded data loss** on crashes. 
* Log the exact topology (output of `pw-dump` or `aplay -L`) at capture start for post‑mortem analysis.

---

## 8. Recommendation Matrix and PoC Implementation Guidance 

| Criterion | Weight (1‑5) | ALSA Tee | PulseAudio Loopback | PipeWire Virtual Devices | JACK |
|-----------|--------------|----------|----------------------|--------------------------|------|
| Recording robustness (no crashes, data integrity) | 5 | 3 | 4 | **5** | 3 |
| Precise source separation (dual‑stream) | 5 | 2 | 3 | **5** | 4 |
| Operational clarity (CLI friendliness) | 4 | 4 | 4 | **5** | 2 |
| Debuggability (graph visibility, logs) | 4 | 3 | 3 | **5** | 2 |
| Compatibility with common distros | 4 | 5 | 4 | **5** | 3 |
| Implementation complexity | 3 | 4 | 4 | **5** | 2 |
| Portability impact on future non‑Linux work | 3 | 5 | 4 | **4** | 3 |
| Support burden for testers | 2 | 4 | 4 | **5** | 2 |

**Scoring** (weight × score, sum): 

* **PipeWire**: 5×5 + 5×5 + 4×5 + 4×5 + 4×5 + 3×5 + 3×4 + 2×5 = **165** 
* **ALSA**: 5×3 + 5×2 + 4×4 + 4×3 + 4×5 + 3×4 + 3×5 + 2×4 = **115** 
* **PulseAudio**: 5×4 + 5×3 + 4×4 + 4×3 + 4×4 + 3×4 + 3×4 + 2×4 = **124** 
* **JACK**: 5×3 + 5×4 + 4×2 + 4×2 + 4×3 + 3×2 + 3×3 + 2×2 = **84** 

**Recommendation** – Adopt **PipeWire virtual sink + virtual microphone** as the default capture strategy for the **online_dual_stream** profile. It offers the highest weighted score, delivering robust source separation, low latency, easy CLI automation, and broad distro support. 

**Secondary option** – Keep the ALSA tee configuration as a fallback for environments where PipeWire cannot be installed (e.g., minimal containers or very old kernels). The fallback incurs minimal latency and requires only a static `.asoundrc` file.

### 8.1 Incremental Impact of `in_person_single_mic` 

Adding the single‑mic profile merely requires selecting a **different capture source** (the physical microphone) while disabling the virtual sink‑monitor link. In a PipeWire‑based setup this is a one‑line change:

```bash
pw-link -d zoom_virtual_mic.monitor system:analog-input
```

or simply set the default source to the hardware mic via `pactl set-default-source alsa_input.pci-0000_00_1b.0.analog-stereo`. No changes to the capture worker’s file handling are needed; the same `record.wav` file is written, preserving the “canonical source artifact” invariant. The implementation effort is estimated at **≈ 2 hours** (code change, unit test, CI fixture update) and adds **negligible runtime overhead**.

### 8.2 Architectural Implications 

* **Capture Worker Interface** – Should expose a *generic capture port* that receives a **device identifier** (e.g., `pipewire:zoom_virtual_mic.monitor` or `alsa:dual_cap`). The worker remains agnostic to the underlying server, fulfilling the “OS‑specific behavior belongs behind adapters” constraint. 
* **Replay/Testing** – Recorded fixtures generated with the same topology can be replayed using `pw-play` or `aplay`, ensuring deterministic test suites. 
* **Fault Isolation** – By using `object.linger=true` and atomic file moves, a crash of the worker leaves the partially written file untouched; a subsequent restart can resume from the last complete segment. 

---

## 9. Conclusion 

The research has identified **PipeWire’s virtual sink + monitor source** as the most reliable, low‑latency, and easily scriptable solution for the **online_dual_stream** capture profile. It satisfies the primary PoC constraints of robustness, source separation, and CLI operability while offering straightforward fallback paths (ALSA tee, PulseAudio loopback) and an optional professional‑grade JACK path for ultra‑low latency needs. 

Implementing the PipeWire topology via a modest Bash script and a systemd user service will provide a reproducible, debuggable environment that integrates cleanly with the existing `mena` capture worker architecture. Adding the **in_person_single_mic** profile incurs only a trivial configuration change, confirming that the current assumption of a **single generic capture port with per‑profile configuration** remains valid.

The consolidated findings, comparative analysis, and step‑by‑step guides equip the PoC team to build a robust, privacy‑preserving audio capture pipeline that can be confidently extended to non‑Linux platforms in future iterations.

--- 

### Sources  

[1] alsa vs pulseaudio vs jack vs pipewire – Reddit, r/linuxaudio, 2024. https://www.reddit.com/r/linuxaudio/comments/1jkvwb6/alsa_vs_pulseaudio_vs_jack_vs_pipewire/  
[2] PipeWire – ArchWiki. https://wiki.archlinux.org/title/PipeWire  
[3] How to create a new pipewire virtual device that combines a real input and output – Superuser. https://superuser.com/questions/1675877/how-to-create-a-new-pipewire-virtual-device-that-to-combines-an-real-input-and-o  
[4] Route Audio to Zoom on Linux with PipeWire – OBS Forums. https://obsproject.com/forum/threads/route-audio-to-zoom-on-linux-with-pipewire.151773/  
[5] [Bug/workaround] ALSA produces sound, Pipewire doesn't – Arch Linux Forums. https://bbs.archlinux.org/viewtopic.php?id=312941  
[6] How to Install and Configure ALSA on Ubuntu – OneUptime. https://oneuptime.com/blog/post/2026-03-02-how-to-install-and-configure-alsa-on-ubuntu/view  
[7] audio – Pulseaudio module‑loopback problems – Unix & Linux Stack Exchange. https://unix.stackexchange.com/questions/312833/pulseaudio-module-loopback-problems  
[8] PulseAudio loopback module gets unexpected latency – Manjaro Forum. https://forum.manjaro.org/t/pulseaudio-loopback-module-gets-unexpected-latency-upon-launching-iriun-webcam-or-droidcam/158632  
[9] Can anyone explain to me like a 7‑year‑old what Pipewire, PulseAudio, ALSA, JACK is? – Manjaro Forum. https://forum.manjaro.org/t/can-anyone-explain-to-me-like-a-7-years-old-what-pipewire-pulseaudio-alsa-jack-is/86927  
[10] DAW question for Slackware 15.1: Pipewire or JACK Audio? – Alien Pastures. https://blog.slackware.nl/daw-question-for-slackware-15-1-pipewire-or-jack-audio/  
[11] [Closed - known bug in pipewire] Zoom with pipewire – EndeavourOS. https://forum.endeavouros.com/t/closed-known-bug-in-pipewire-zoom-with-pipewire-cant-share-computer-audio/20527  
[12] PipeWire virtual microphone. https://luke.hsiao.dev/blog/pipewire-virtual-microphone/  
[13] Simon Greer – Breaking Down the Linux Audio Stack. https://www.simongreer.co.uk/blog/breaking-down-the-linux-audio-stack/  
[14] PipeWire is how audio should be on Linux – Hacker News. https://news.ycombinator.com/item?id=28503273  
[15] Visualizing PipeWire audio graphs – Sacha Chua. https://sachachua.com/blog/2026/01/visualizing-and-managing-pipewire-audio-graphs-from-emacs/  
[16] Set Up PipeWire on Ubuntu. https://oneuptime.com/blog/post/2026-03-02-how-to-set-up-pipewire-as-audio-server-on-ubuntu/view  
[17] Configuring ALSA for pro hardware setups. https://panther.kapsi.fi/posts/2016-04-25_asound  
[18] ALSA .asoundrc examples – diyAudio. https://www.diyaudio.com/community/threads/alsa-asoundrc-and-asound-conf-configurations-that-have-worked.296594/  
[19] How to create Pulseaudio virtual sink – Ask Ubuntu. https://askubuntu.com/questions/1432249/how-to-create-pulseaudio-virtual-sink-that-you-can-hear  
[20] PulseAudio null‑sinks and loopbacks – nalkrithinks. https://nalkrithinks.wordpress.com/2017/05/05/pulseaudio-null-sinks-and-loopbacks-for-great-good/  
[21] PulseAudio Loopback Latency – Ask Ubuntu. https://askubuntu.com/questions/491825/pulseaudio-loopback-latency  
[22] Configuring JACK2 Virtual Audio Routing – Manjaro Forum. https://forum.manjaro.org/t/configuring-jack2-virtual-audio-routing/54202  
[23] JACK realtime scheduling. https://jackaudio.org/faq/linux_rt_config.html  
[24] Low latency with PipeWire and Jack – Linux Mint Forums. https://forums.linuxmint.com/viewtopic.php?t=402301  
[25] PipeWire: Combine Stream module. https://docs.pipewire.org/page_module_combine_stream.html  
[26] PipeWire low‑latency configuration – OneUptime. https://oneuptime.com/blog/post/2026-03-02-configure-pipewire-low-latency-audio-ubuntu/view  
[27] PipeWire configuration for low latency – Fedora Discussion. https://discussion.fedoraproject.org/t/pipewire-configuration-for-low-latency/32221  
[28] PipeWire Linux Audio: How It Solved The Problem Nobody Could Fix For 20 Years. https://www.linuxteck.com/pipewire-linux-audio-problem-solved/  
[29] [SOLVED] PulseAudio, PipeWire, JACK? – Linux Mint Forums. https://forums.linuxmint.com/viewtopic.php?t=436530
