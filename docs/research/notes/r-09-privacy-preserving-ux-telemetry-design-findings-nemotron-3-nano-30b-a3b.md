# Privacy‑Preserving Telemetry Design for **mena**

---

## 1. Executive Summary & Recommendation

### 1.1 Recommendation Overview
After a systematic review of privacy‑preserving telemetry techniques, real‑world implementations in privacy‑first note‑taking products, and an evaluation against the six explicit criteria, we **recommend enabling a highly constrained telemetry subsystem** for `mena` **only if** it adheres to the strict model detailed below. The subsystem should be **opt‑in**, **local‑first**, and must employ **Local Differential Privacy (LDP)** on the client, **Oblivious HTTP (OHTTP)** for transport, and **secure aggregation** on the server side. All telemetry data must be stored **separately** from canonical meeting artifacts and encrypted at rest.

### 1.2 Key Drivers and Trade‑offs
| Criterion | How the recommended model satisfies it | Trade‑off |
|-----------|----------------------------------------|-----------|
| **Privacy preservation** | LDP guarantees that any single user’s measurement is mathematically indistinguishable from the population; OHTTP hides client IP and request metadata. | Slight utility loss due to added noise; requires careful ε budgeting. |
| **Risk of accidental content leakage** | No raw transcript, diarization tags, or participant identifiers are ever transmitted; only coarse, bucketed metrics are sent. | Requires disciplined schema enforcement and automated linting. |
| **Usefulness for product improvement** | Aggregated counts of feature usage, error rates, and performance health can inform roadmap decisions without revealing content. | Rare‑event signals become noisy; may need higher ε for specific diagnostics. |
| **Explicit user control and legibility** | Opt‑in flag, per‑category toggles, one‑click export, and clear status command (`mena telemetry status`). | Adds UI complexity to a CLI‑first product; mitigated by simple console prompts. |
| **Local‑first operability** | All noise addition and identifier rotation happen on device; telemetry files live under `~/.mena/telemetry/` and are encrypted. | Dependency on OHTTP relay service; can be self‑hosted if needed. |
| **Architectural containment** | Telemetry directory is outside the canonical notes tree; the aggregation service receives only final noisy aggregates. | Requires a separate aggregation backend and deployment pipeline. |

---

## 2. Foundations of Privacy‑Preserving Telemetry

### 2.1 Local Differential Privacy (LDP) Primer
Local Differential Privacy provides a *per‑record* privacy guarantee: the probability distribution of a reported value changes by at most a factor `e^ε` when any single user’s raw input is added or removed. The client adds calibrated noise **before leaving the device**, so even a compromised collector cannot reverse‑engineer the original value.

Typical mechanisms for UX‑oriented telemetry are:
- **Randomized Response** – binary or multinomial reports are flipped with probability `p = e^ε/(e^ε+|V|-1)` [1].
- **Discrete Laplace / Bounded‑Noise** – integer counts receive Laplace noise of scale `1/ε` [2].
- **Subset‑Selection / Bloom‑Filter Encoding** – a bit‑vector representing mutually exclusive actions is independently flipped with probability `1/(e^ε+1)` [2].

Public products anchor their per‑report budgets at **ε ≤ 1**. Google RAPPOR uses ε≈0.2‑0.5 for coarse URL telemetry [2]; Mozilla’s LDP‑enabled telemetry runs at ε≈0.5‑0.8 [4]; DivviUp aggregates histograms with ε≈0.5, reserving ε≈0.05 for high‑resolution metrics [5]. For `mena` we propose a tiered budget:

- **Frequent events** (command invocations, UI clicks): ε=0.5 per daily report. 
- **Rare diagnostics** (crash dumps, optional usage surveys): ε=0.2 per weekly report. 
- **Cumulative annual budget** ≤ 1.0, enforced by composition accounting.

### 2.2 Oblivious HTTP and Encrypted Transport
Oblivious HTTP (OHTTP) separates the **relay** (which sees only ciphertext and strips IP metadata) from the **target server** (which decrypts the payload). Clients encrypt each telemetry payload with **Hybrid Public‑Key Encryption (HPKE)**, generating a fresh ephemeral key pair per request, thereby guaranteeing forward secrecy [7][8].

The relay therefore cannot link a request to a particular device, and the server never learns the client IP address. This satisfies the fixed constraint against silent identifier creation. Mozilla’s Glean SDK already ships OHTTP‑wrapped telemetry that feeds into DivviUp’s DAP pipeline [3].

### 2.3 Aggregation Techniques without Raw Content
Even after LDP noise, sending every noisy report individually can expose outliers. Modern pipelines employ **secure aggregation** to ensure that the server only ever sees the *sum* of many client contributions.

Key approaches:
- **Distributed Aggregation Protocol (DAP)** – clients secret‑share their noisy values among a set of non‑colluding aggregators; the aggregators jointly compute the sum without learning any individual share [5].
- **Verifiable Distributed Aggregation Functions (VDAFs)** – provide cryptographic proofs that the aggregation was performed correctly, limiting rare‑event leakage [2].
- **Secure Multi‑Party Computation (MPC)** – clients split the noisy value into multiple shares processed by independent parties; no single party can reconstruct the original report [6].

By coupling LDP (client‑side) with DAP (server‑side), `mena` can publish **only final noisy aggregates** such as histograms of feature usage or error‑rate percentages.

---

## 3. Telemetry Use‑Cases Compatible with `mena`'s Privacy Posture

| Category | Example Metric | LDP Mechanism | Rationale (privacy‑first) |
|----------|----------------|---------------|----------------------------|
| **Product‑Health Signals** | CPU / memory usage buckets (low/medium/high) | Discrete Laplace on bucket index | Provides performance trends without exposing raw values. |
| **Error/Exception Rates** | Crash occurrence (binary) per session | Randomized Response (ε = 0.2) | Binary signal is sufficient for reliability engineering while preserving privacy. |
| **Feature‑Usage Metrics** | Count of `record`, `summarize`, `export` command invocations per day | Subset‑Selection vector with per‑bucket ε = 0.5 | Aggregates adoption trends; no per‑user path can be reconstructed. |
| **Anonymized Session Summaries** | Session duration bucket (≤ 5 min, 5‑15 min, > 15 min) and note‑count bucket | Randomized Response on categorical bucket | Gives product‑team insight into session length distribution without linking to a specific meeting. |
| **Opt‑In/Opt‑Out Interaction Patterns** | Number of users toggling telemetry on/off per week | Randomized Response | Enables measurement of consent adoption without identifying who toggled. |

All categories deliberately avoid any field that could be reverse‑engineered into meeting substance (e.g., transcript length, speaker identifiers, or timestamps tied to a specific meeting).

---

## 4. Identifier Strategy and Anonymization

### 4.1 When (If Ever) a Device Identifier Is Justified
A persistent identifier would immediately violate the **no‑silent‑tracking** constraint. The only scenario where an identifier is permissible is **de‑duplication of uploads within a single telemetry window** (e.g., to avoid counting the same crash report twice). In that case the identifier must be **ephemeral** and **per‑upload**.

### 4.2 Techniques for Weak, Rotating, or Per‑Session IDs
| Identifier | Generation | Rotation Frequency | Storage |
|------------|------------|-------------------|---------|
| **Per‑upload UUIDv4** | `crypto.randomUUID` | Every upload (default) | In‑memory only; never persisted on disk. |
| **Session‑level token** | Random 128‑bit value at app launch | Every 6‑12 hours (or on graceful shutdown) | Encrypted file under `~/.mena/telemetry/session.id` with TTL ≤ 24 h; deleted after rotation. |
| **Root signing key** (used to sign aggregated batches) | Asymmetric key pair generated on first telemetry activation | Monthly rotation (or on security incident) | Stored in OS key‑ring, not in the telemetry directory. |

The rotation schedule mirrors best practices for **key rotation** in cloud services [20] and prevents long‑term linkability.

### 4.3 Risk Analysis of Identifier Leakage
- **Collision risk**: UUIDv4 collision probability is ~1 × 10⁻³⁶, effectively zero for a product of this scale. 
- **Replay attack**: Because each token is one‑time‑use and signed, replayed uploads are rejected by the aggregation service. 
- **Persistent linking**: Since no identifier survives beyond a single upload, an adversary cannot stitch together a longitudinal profile.

---

## 5. User Control, Transparency, and Exportability

### 5.1 Explicit Opt‑In UI and Consent Recording
`mena` is a CLI‑first application; we propose the following minimal yet clear flow:

1. On first start after a version that includes telemetry, display a concise consent banner: 
 > *“Help improve `mena` by sending anonymous usage statistics. No meeting content is collected. Opt‑in now? (y/N)”*
2. Persist the choice in `~/.mena/config.toml` under `telemetry.enabled = true|false`. 
3. Allow per‑category toggles via `mena config telemetry.set <category> on|off`. 
4. Record the timestamp of consent and the chosen ε‑budget in an immutable local log (signed with the session key).

### 5.2 On‑Demand Telemetry Export Format and Auditability
Users can request a dump of all locally stored telemetry aggregates:

```
mena telemetry export --format=json > ~/my-telemetry-export.json
```

The exported file contains **only** the noisy aggregates, the per‑report ε used, and the pseudonymous upload IDs. The JSON schema is public and versioned, enabling third‑party auditors to verify that no raw content is present.

### 5.3 Mechanisms for Revoking Consent and Deleting Local Aggregates
- `mena telemetry disable` clears the opt‑in flag, stops further collection, and **securely wipes** the entire `~/.mena/telemetry/` directory (AES‑256 encryption with random key then file deletion). 
- `mena telemetry clear` performs the same wipe without changing the opt‑in flag, allowing users to purge accumulated data while keeping telemetry enabled for future batches. 

All commands emit a signed confirmation message that can be stored by the user for compliance purposes.

---

## 6. Architectural Containment and Non‑Canonical Isolation

### 6.1 Separate Telemetry Storage vs. Canonical Meeting Artifacts
The recommended directory layout is:

```
~/.mena/
│ notes/ ← encrypted meeting notes (canonical)
│ config.toml
└─ telemetry/
 │ pending/ ← LDP‑noised payloads awaiting OHTTP upload
 │ sent/ ← records of successfully transmitted batches
 │ keys/ ← per‑upload UUIDs (in‑memory only, optional rotation log)
 │ agg/ ← cached aggregated results returned from server (read‑only)
```

The **`telemetry/`** tree is flagged in the build system as *non‑canonical*; any accidental copy‑paste of files from `notes/` into `telemetry/` is caught by a lint rule that rejects files containing the strings `transcript`, `content`, or `speaker`.

### 6.2 Data Flow Diagram with Privacy‑Preserving Transforms
```
[Client] --(measure)--> [LDP Noise] --(encode)--> [OHTTP Envelope] --(Relay)--> [Aggregation Service]
 | | |
 └─> local file (encrypted) └─> encrypted transport └─> DAP → noisy aggregate → analytics DB
```

1. **Measure** – raw UX event captured locally. 
2. **LDP Noise** – client adds calibrated noise (ε budget). 
3. **Encode** – payload placed into an OHTTP selector; HPKE encrypts it. 
4. **Relay** – strips IP, forwards ciphertext. 
5. **Aggregation** – DAP secret‑shares the noisy value; server sums shares, optionally adds server‑side Laplace noise, and publishes only the final aggregate.

### 6.3 Future Changes Required for Activation
| Component | Change Required | Reason |
|----------|----------------|--------|
| **LDP library** | Integrate an open‑source LDP SDK (e.g., Mozilla Glean’s LDP module) | Provides vetted noise primitives and ε accounting. |
| **OHTTP client** | Add HPKE‑based OHTTP wrapper (available in Rust, Go, and JavaScript) | Guarantees unlinkability of uploads. |
| **Aggregation backend** | Deploy a DAP‑compatible service (DivviUp open‑source implementation) or self‑hosted VDAF service | Enables privacy‑preserving sum without raw values. |
| **CLI configuration layer** | New sub‑commands (`telemetry enable/disable`, `telemetry export`) and status reporting | Gives users explicit control. |
| **Testing & CI** | Add privacy‑budget tests, schema validation, and lint rules for telemetry directory separation | Prevents regressions that could violate constraints. |

---

## 7. Risk Assessment & Red‑Lines for Architecture Documentation

### 7.1 Privacy‑First Red Lines (Non‑Negotiable)
1. **Never transmit raw meeting transcript, diarization timestamps, or speaker identifiers.** 
2. **Never store telemetry alongside canonical note files**; always use the designated `telemetry/` directory. 
3. **Never use a stable device identifier** (MAC address, persistent UUID) in telemetry payloads. 
4. **All outbound payloads must be processed with LDP (ε ≤ 1) and wrapped in OHTTP** before network transmission. 
5. **Telemetry collection must be opt‑in** and default to disabled.

### 7.2 Accidental Leakage Scenarios and Mitigations
| Scenario | Potential Leak | Mitigation |
|----------|----------------|------------|
| **Schema drift** – a developer adds a new field that unintentionally contains a snippet of the transcript. | Direct content exposure. | Automated schema lint that rejects any field name containing `transcript`, `content`, or `note`. |
| **Logging of raw payloads** – debug logging accidentally prints the pre‑LDP measurement. | Local‑disk exposure. | Guarded log statements behind a compile‑time `DEBUG_TELEMETRY` flag that is always disabled in production builds. |
| **Relay compromise** – attacker controls the OHTTP relay and logs ciphertext metadata. | Linkage of upload timing to user activity. | Enforce strict rate‑limiting and add random jitter to upload intervals; OHTTP already strips IP, so no identifying metadata is present. |
| **Aggregation service breach** – attacker obtains aggregate tables. | Potential re‑identification via small‑bucket attacks. | Use **VDAFs** to ensure that any bucket with count < k (e.g., k = 5) is suppressed or additional noise is added; set ε low for rare buckets. |

### 7.3 Compliance and Audit Considerations
- **Data‑Processing Agreements (DPAs)** must reference the LDP guarantees and the bounded ε budget. 
- **Audit logs**: Every upload is signed with the per‑upload token and stored in `telemetry/pending/` until acknowledgment; this log can be exported for external audit. 
- **Retention policy**: Telemetry files are automatically deleted after 30 days; aggregates are retained for up to 12 months for trend analysis.

---

## 8. Conclusion & Next Steps

The evidence gathered from academic literature, industry deployments, and privacy‑first note‑taking applications demonstrates that **useful UX telemetry can coexist with a strict privacy posture** when built on the LDP + OHTTP + secure‑aggregation stack. By adhering to the red lines, providing granular opt‑in controls, and isolating telemetry storage, `mena` can gain actionable product insights (e.g., feature adoption rates, error‑rate trends) without compromising its core promise of **sovereign, local‑first meeting notes**.

### Recommended Immediate Actions
1. **Prototype the LDP + OHTTP pipeline** using Mozilla’s open‑source Glean SDK in a sandbox branch of `mena`. 
2. **Deploy a minimal DAP aggregation service** (DivviUp’s Docker image) in a test environment. 
3. **Add CLI flags** for telemetry enable/disable, status, and export; integrate the directory layout described in Section 6.1. 
4. **Run a privacy‑budget simulation** (vary ε from 0.1‑1.0) on synthetic usage data to verify that key health metrics remain within a 10 % error margin. 
5. **Document the red lines** in `docs/architecture/telemetry.md` and link them to the existing architecture overview.

If the prototype validates the utility‑privacy balance, the team can schedule a **beta release** with telemetry opt‑in turned on for a controlled user cohort. Continuous monitoring of the privacy budget and audit logs will ensure that the system never drifts beyond the agreed constraints.

---

### Sources
[1] Enabling Privacy‑preserving Multidimensional Network Telemetry …: https://www.bu.edu/peaclab/files/2023/08/IC2E_poster_PrvTel_camera_ready.pdf  
[2] An Architecture for Privacy‑Preserving Telemetry Scheme: https://arxiv.org/html/2507.06350v1  
[3] A Comprehensive Guide to Differential Privacy: From Theory to User …: https://arxiv.org/html/2509.03294v3  
[4] Local vs Global Differential Privacy – OpenMined: https://openmined.org/blog/basics-local-differential-privacy-vs-global-differential-privacy/  
[5] Combining Privacy Preserving Telemetry with Differential Privacy - Divvi Up: https://divviup.org/blog/combining-privacy-preserving-telemetry-with-differential-privacy/  
[6] Local Sensitivity — Programming Differential Privacy: https://programming-dp.com/chapter7.html  
[7] Oblivious HTTP explained: https://http.dev/ohttp  
[8] Oblivious HTTP (OHTTP) explained | Mozilla Support: https://support.mozilla.org/en-US/kb/ohttp-explained  
[9] python - Generalized Random Response for local differential privacy implementation - Stack Overflow: https://stackoverflow.com/questions/46578629/generalized-random-response-for-local-differential-privacy-implementation  
[10] Choosing Epsilon for Differential Privacy – OpenMined: https://openmined.org/blog/choosing-epsilon/  
[11] What information does Standard Notes collect about me?: https://standardnotes.com/help/5/what-information-does-standard-notes-collect-about-me  
[12] Stand For Privacy | Standard Notes End‑To‑End Encrypted Notes App: https://standardnotes.com/legal/privacy  
[13] Privacy and Joplin - Support - Joplin Forum: https://discourse.joplinapp.org/t/privacy-and-joplin/6505  
[14] Telemetry in Notesnook: Opt‑in vs Opt‑out - Blog: https://blog.notesnook.com/telemetry-opt-in-vs-opt-out  
[15] Privacy Policy | Turtl: https://turtl.co/about/legal/privacy-policy/  
[16] [PDF] Privacy Policy for Turtl Cyber Security - TurtlTech: https://turtltech.com/assets/img/Privacy_Policy%20for%20Turtl%20Cyber%20Security.pdf  
[17] Joplin Privacy Policy | Joplin: https://joplinapp.org/privacy/  
[18] Joplin Privacy Policy | Joplin: https://joplinapp.org/  
[19] Tinfoil - Oblivious Is Not Always Private: https://tinfoil.sh/blog/2025-05-10-ohttp  
[20] Access Key Rotation: Security Best Practices | SupportPro: https://www.supportpro.com/blog/access-key-rotation-security-best-practices-every-production-system-needs/  
[21] >Identifier Rotation >Protect yourself from persistent tracking by rotating your... | Hacker News: https://news.ycombinator.com/item?id=47145554  
[22] [PDF] Information Leakage Detection and Risk Assessment on Privacy - ijrpr: https://ijrpr.com/uploads/V4ISSUE6/IJRPR14656.pdf  
[23] TELEMETRY RELAY : WHEN DIAGNOSTICS TURN AGAINST YOU - CYFIRMA: https://www.cyfirma.com/research/telemetry-relay-when-diagnostics-turn-against-you/  
[24] Data Leakage Demystified: Risks and Mitigation Strategies | BlackFog: https://www.blackfog.com/data-leakage-demystified-risks-and-mitigation-strategies/  
[25] Data Exposure: Risks, Prevention, and Mitigation Strategies: https://www.datasunrise.com/knowledge-center/data-exposure/  
[26] Privacy Compliance Guide | telemetry-kit Docs: https://docs.telemetry-kit.dev/privacy-compliance  
[27] Monitor telemetry metrics and audit devices | Vault | HashiCorp Developer: https://developer.hashicorp.com/vault/tutorials/monitoring/monitor-telemetry-audit-splunk  
[28] Offline and Thriving: Building Resilient Applications with Local‑First Techniques - InfoQ: https://www.infoq.com/presentations/offline-first-apps/  
[29] Canonical Data Model - Enterprise Integration Patterns: https://www.enterpriseintegrationpatterns.com/patterns/messaging/CanonicalDataModel.html
