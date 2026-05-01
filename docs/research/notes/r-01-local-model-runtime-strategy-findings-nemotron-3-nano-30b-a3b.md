# Local Model Runtime Strategy for the MENA PoC Architecture

**Executive Recommendation** – Deploy **vLLM** on any machine that possesses an NVIDIA GPU, with **Ollama + BentoML** as a universal fallback for CPU‑only or otherwise weaker tester machines. This combination satisfies all fixed constraints (local‑only processing, Linux‑first, privacy‑first), delivers the highest throughput where hardware permits, and preserves a clean provider‑runtime port that can be swapped out in later development stages.

---

## 1. Context and Objectives

MENA records meetings locally, then transcribes, diarizes, and optionally runs a local LLM to produce structured notes. The PoC is:

- **Linux‑first** and **CLI‑first**.
- **Local‑first** – audio, transcription, diarization, and any summarization must run on the user’s machine.
- **Robustness‑first** – recording reliability outranks implementation convenience.

The research question is: *Which local runtime strategy should MENA use for model‑backed stages during the PoC, and how viable is local summarization on the primary laptop and on weaker representative tester machines?*

---

## 2. Survey of Viable Local Runtimes

| Runtime | Architecture & Core Features | Typical Hardware | GPU Throughput* | CPU‑Only Throughput* | API Compatibility | Installation Effort (Linux) |
|--------|------------------------------|-------------------|----------------|----------------------|-------------------|-----------------------------|
| **vLLM** | High‑throughput OpenAI‑compatible server; CUDA kernels, prefix‑caching. | NVIDIA GPU workstation/server (A100, RTX 4090, etc.) | ~3 000 tokens / s for an 8 B model (DeepSeek‑R1‑Distill‑Llama‑8B, FP16)[13][14] | No published CPU‑only benchmark. | OpenAI `/v1/chat/completions` out‑of‑the‑box[37] | Requires CUDA 12.x and Docker; moderate. |
| **Ollama + BentoML** | Ollama serves GGML models (CPU or GPU); BentoML adds a thin service layer that can expose an OpenAI‑compatible wrapper. | Any Linux laptop (CPU‑only or GPU). | 55 – 130 tokens / s on an A100 (FP16 / Q4KM quant)[15] | 7 – 30 tokens / s on an Intel i7, 16 GB RAM[23] | Proprietary Ollama HTTP (port 11434); can be wrapped to OpenAI‑compatible via BentoML[7] | Single‑binary install or Docker; low. |
| **NVIDIA Triton** | Multi‑framework inference server; dynamic model loading, hot‑swap, enterprise‑grade observability. | GPU‑enabled workstation or cluster. | Comparable to vLLM on the same GPU tier (additional orchestration overhead)[6][27] | Limited CPU‑only support. | gRPC/HTTP; OpenAI‑compatible via external proxy. | Higher complexity (Helm charts, model repo)[6] |
| **NVIDIA NIM (NeMo)** | Containerized runtime for large models (up to 120 B) with FP8/BF16 acceleration; OpenAI‑compatible endpoint built‑in. | NVIDIA H100/A100/RTX 4090. | Similar to vLLM on the same GPU tier[29][30] | Not intended for CPU‑only. | OpenAI‑compatible out‑of‑the‑box[29] | Simple Docker run; GPU‑only. |
| **MNN‑LLM (Alibaba)** | Edge‑optimized inference engine; models converted to MNN format; ultra‑low memory footprint. | ARM CPUs/GPU (mobile, embedded). | Benchmarks focus on mobile; not measured on high‑end GPUs[31][32] | Demonstrated on iPhone A11 and Snapdragon 835 with competitive latency[31] | Custom RPC (no native OpenAI API). | Source build & model conversion; higher effort. |

\*Throughput figures are drawn from the verified sources listed at the end of the report.

**Key observations**

- **Throughput superiority** – vLLM on an A100 delivers roughly 3 k tokens / s, 20‑30× higher than Ollama on the same hardware[13][14][15]. 
- **CPU‑only viability** – Only Ollama (via its GGML backend) provides usable performance on machines without a GPU[23]. 
- **Dynamic model management** – Triton offers the most mature hot‑swap via Model‑Control‑Mode POLL[27]; vLLM can reload models through its `/v1/models` endpoint but incurs a brief pause[37]. 
- **Licensing compliance** – All candidates are either open‑source or free‑tier, satisfying the PoC’s licensing constraint[1][2][13][14][15][16][17][18].

---

## 3. Performance, Resource, and Cost Benchmarks

**Data Sources** – GPU‑throughput numbers for vLLM are taken from the DatabaseMart benchmark pages[13][14]. Ollama GPU‑throughput (FP16 and Q4KM‑quantized) as well as CPU‑only throughput are taken from the Ollama performance comparison article[15] and the Medium hardware‑benchmark post[23] respectively. Cost considerations use publicly known hardware pricing and cloud‑API pricing.

### 3.1 GPU‑Enabled Scenario (Primary Laptop)

| Runtime | Model (8 B) | Tokens / s | TTFT (ms) | Per‑token latency (ms) |
|--------|------------|-----------|-----------|------------------------|
| **vLLM** | DeepSeek‑R1‑Distill‑Llama‑8B (FP16) | ~3 000 total (≈1 540 in + 2 552 out) | 328 | 18 |
| **Ollama** (FP16) | 8 B GGML | ~55 | — | 18 |
| **Ollama** (quantized) | 8 B GGML (Q4KM) | ~130 | — | 7‑8 |

### 3.2 CPU‑Only Scenario (Weaker Tester Machines)

| Runtime | Hardware (Intel i7, 16 GB RAM) | Tokens / s | Avg latency per token (ms) |
|--------|--------------------------------|-----------|----------------------------|
| **Ollama** (7 B Llama, GGML) | i7‑12th gen, no GPU | 7‑30 | 120‑150 |

*No public CPU‑only benchmark exists for vLLM; this is a validation gap.*

### 3.3 Cost Perspective

Running vLLM on a single A100 incurs a one‑time hardware cost (≈ $2 500) that is amortized over the PoC’s lifespan; inference thereafter is free, making it cheaper than a cloud API priced at ≈ $0.02 per request after a few hundred requests. Ollama adds no hardware cost on existing laptops but offers lower throughput, which is acceptable for batch summarization where latency is not a hard requirement.

---

## 4. Deployment & Orchestration

### 4.1 Docker‑Compose Quick‑Start

- **Ollama + BentoML** – The BentoML blog provides a two‑service `docker‑compose.yml` that launches Ollama on port 11434 and a BentoML service on port 3000, exposing an HTTP API that can be wrapped to the OpenAI schema[7]. 
- **vLLM + Nginx TLS** – A typical compose file runs vLLM (`vllm/vllm-openai:latest`) and an Nginx reverse‑proxy configured for mutual TLS. The vLLM documentation shows how to expose the OpenAI‑compatible endpoint[37], and Elastic’s “Connect to your own LLM using vLLM” guide illustrates a comparable Docker‑Compose layout[40].

Both stacks start with a single `docker compose up -d`, meeting the CLI‑first, headless‑automation requirement.

### 4.2 Kubernetes & Helm (Future Scaling)

- **NVIDIA Triton** – A OneUptime article demonstrates a Helm chart that deploys Triton with a persistent model repository and enables Model‑Control‑Mode POLL for dynamic loading[6]. 
- **BentoML Service** – BentoML can also be deployed as a K8s Deployment with an Ingress that terminates TLS, preserving the same provider‑runtime abstraction at scale.

### 4.3 Dynamic Model Loading & Hot‑Swap

| Runtime | Hot‑Swap Mechanism | Zero‑Downtime Guarantee |
|--------|-------------------|--------------------------|
| **Triton** | Model‑Control‑Mode POLL watches a directory; explicit `POST /v2/repository/load` API triggers load/unload[27]. | Yes – other models remain online while a new model loads. |
| **vLLM** | `POST /v1/models` with a new model name reloads the server; workers restart briefly[37]. | Partial – short pause during reload. |
| **Ollama** | Requires container restart; no native hot‑swap. | No. |
| **NIM** | New container image with updated model tag; rolling restart via orchestration. | Depends on deployment strategy. |

---

## 5. Security, Privacy, and Governance

All three runtimes can be secured with **mutual TLS (mTLS)**, ensuring that only authorized agents can invoke the inference service.

### 5.1 Certificate Authority (CA) Generation

1. Generate a private CA: `openssl genrsa -out ca.key 4096` and self‑sign it. 
2. Issue server and client certificates signed by this CA. Detailed step‑by‑step commands are documented in the Ollama security guide[19], the vLLM security page[21], and the NVIDIA mTLS guide[41].

### 5.2 Runtime‑Specific mTLS Configuration

| Runtime | Server‑Side TLS Setup |
|--------|------------------------|
| **Ollama** | Deploy an Nginx reverse‑proxy on port 443; enable `ssl_verify_client on;` and point to the CA certificate (`ssl_client_certificate`)[19][20]. |
| **vLLM** | Either start vLLM with `--tls-key`, `--tls-cert`, `--tls-ca`, and `--tls-client-auth required` (native support)[21], or place it behind an Nginx proxy with the same `ssl_verify_client` settings[21]. |
| **Triton** | Launch with `--grpc-use-ssl-mutual`, `--grpc-server-cert`, `--grpc-server-key`, and `--grpc-root-cert` (or the HTTP equivalents)[41]. |

All configurations should emit audit logs (OpenTelemetry for vLLM, Nginx access logs for Ollama, Triton’s built‑in logging) that record the client certificate subject, satisfying the PoC’s traceability priority.

### 5.3 Data Sovereignty

Because every stage—recording, transcription, diarization, and summarization—runs entirely on the user’s device, no raw audio or derived notes leave the machine. This fully satisfies MENA’s privacy‑first mandate and eliminates cross‑border data‑transfer concerns.

---

## 6. Licensing, Installation, and Support Burden

| Runtime | License | Install Steps (Linux) | Ongoing Maintenance |
|--------|---------|-----------------------|----------------------|
| **vLLM** | Apache 2.0 | 1. Install NVIDIA driver & CUDA 12.x.<br>2. `docker pull vllm/vllm-openai:latest`.<br>3. (Optional) generate CA & certs[19][21].<br>4. `docker compose up -d`. | Pull newer Docker image; update CUDA driver as needed. |
| **Ollama + BentoML** | Ollama AGPL + BentoML Apache 2.0 | 1. `curl -fsSL | sh` (binary) or Docker pull.<br>2. Build BentoML service (`bentoml build`).<br>3. `docker compose up -d` using the compose file from the BentoML blog[7]. | Re‑pull Ollama binary/Docker image; rebuild BentoML when the service code changes. |
| **Triton** | Apache 2.0 | Install NVIDIA driver, Docker, then apply the Helm chart from OneUptime[6]. | Helm chart upgrades; model repository management. |
| **NIM** | NVIDIA EULA (free for NVIDIA hardware) | `docker run --gpus all nvcr.io/nim/llama3:latest` as described in the NeMo quick‑start guide[30]. | Pull newer container version. |
| **MNN‑LLM** | Apache 2.0 | Clone the GitHub repository[8]，build from source, convert models with `mnnconvert` (see the arXiv paper[31]). | Re‑compile for new OS/library versions; reconvert models. |

All installations are compatible with a standard Ubuntu 22.04 LTS environment. The only runtime that adds appreciable friction on CPU‑only machines is vLLM (requires CUDA), which is why it is designated as the primary option only for GPU‑capable hosts.

---

## 7. Decision Matrix & Recommendation

| Evaluation Criterion (priority) | vLLM (GPU) | Ollama + BentoML (CPU/GPU) | Triton | NIM | MNN‑LLM |
|----------------------------------|------------|---------------------------|--------|-----|--------|
| **Local operability & failure clarity** | ✅ Deterministic OpenAI API, clear error codes. | ✅ Runs everywhere; graceful fallback on CPU. | ✅ Enterprise‑grade logging, but more components increase failure surface. | ✅ Containerized, but GPU‑only. | ✅ Edge‑focused, not needed for PoC. |
| **Suitability for weaker hardware** | ❌ Needs NVIDIA GPU. | ✅ Works on CPU‑only laptops (7‑30 t/s). | ❌ GPU‑only. | ❌ GPU‑only. | ✅ Designed for ARM/edge, but beyond PoC scope. |
| **Automation & CLI‑first test harness** | ✅ OpenAI‑compatible endpoint; `curl`/`openai` client works. | ✅ BentoML abstracts Ollama behind HTTP; still scriptable. | ✅ gRPC/HTTP usable but extra proxy config. | ✅ Simple Docker run, but GPU‑only. | ❓ Custom RPC may require extra tooling. |
| **Replaceability behind provider/runtime port** | ✅ Standard OpenAI schema matches the `LocalLLMProvider` abstraction. | ✅ BentoML provides a clean service layer; underlying model server can be swapped. | ✅ Configurable backends via model repo. | ✅ OpenAI‑compatible endpoint built‑in. | ✅ Could wrap, but extra effort. |
| **Installation & upgrade burden (Linux)** | Moderate – driver & CUDA version matching. | Low – single binary or Docker image; minimal dependencies. | High – Helm, GPU driver, model repository. | Moderate – pull newer container. | High – source build & model conversion. |
| **Observability & debuggability** | Good – OpenTelemetry integration, logging. | Good – BentoML tracing; Nginx logs for TLS. | Excellent – Triton telemetry dashboards. | Good – container logs. | Limited – requires custom instrumentation. |
| **Future cross‑platform implications** | Strong – vLLM now supports Windows/macOS (future‑proof). | Strong – Ollama provides binaries for all major OSes. | Moderate – primarily Linux/GPU. | Moderate – NVIDIA‑only. | Strong – edge devices. |
| **Performance for summarization** | ★★ (≈ 3 k t/s) – far exceeds PoC needs. | ★ (55‑130 t/s on GPU; slower on CPU) – sufficient for batch summarization. | ★★ (comparable to vLLM on GPU). | ★ (high‑end GPU only). | ★ (edge‑only workloads). |

### Primary Recommendation – **vLLM (GPU‑first)**

- **Deploy on any machine that has an NVIDIA GPU (A100, RTX 4090, etc.).**
- **Why:** Highest throughput, deterministic OpenAI‑compatible API, clean separation via the provider‑runtime port, and native support for hot‑swap via `/v1/models`.
- **Assumptions:** 
 - The primary laptop includes a supported NVIDIA GPU and ≥ 16 GB RAM. 
 - CUDA 12.x drivers are kept in sync with the Docker image. 
- **Risks & Mitigations:** 
 - *GPU dependency* – automatic fallback to Ollama + BentoML on machines lacking a GPU (environment variable `RUNTIME=ollama`). 
 - *CUDA driver drift* – pin Docker image to a specific CUDA version; CI pipeline validates driver‑image compatibility. 
 - *Memory footprint* – monitor RAM usage (≈ 12‑16 GB for an 8 B model) and ensure the PoC laptop meets this requirement.

### Secondary Recommendation – **Ollama + BentoML (CPU‑agnostic fallback)**

- **Deploy on all tester machines, including those without a GPU.**
- **Why:** Works on any Linux laptop, modest installation effort, and provides a service layer that preserves a clean provider‑runtime boundary.
- **Assumptions:** 
 - Summarization latency is not a hard PoC constraint; batch processing is acceptable. 
 - A 7‑8 B GGML model fits comfortably in the available RAM (≈ 4‑6 GB). 
- **Risks & Mitigations:** 
 - *Lower throughput* – acceptable for batch summarization; can be mitigated by quantizing models (as shown in the Ollama benchmark)[15]. 
 - *Model size limits* – Ollama’s GGML backend caps models at ~30 B parameters; the chosen 7‑8 B model is well within this bound.

---

## 8. Architectural Implications

1. **Provider Interface (`LocalLLMProvider`)** – Define a generic interface exposing `generate(messages, options) → Completion`. Both vLLM and Ollama adapters implement this, allowing core workflow code to remain runtime‑agnostic. 
2. **Runtime Selection Logic** – At container start, read an environment variable (`RUNTIME`). If set to `vllm`, launch the vLLM stack; otherwise launch the Ollama + BentoML stack. This fulfills the CLI‑first, headless‑automation requirement for the test harness. 
3. **Observability Hook** – Embed OpenTelemetry middleware in the BentoML service and enable vLLM’s built‑in tracing. Export metrics to a local Prometheus‑Grafana stack for debugging and performance regression testing. 
4. **Dynamic Model Updates** – For vLLM, expose the `/v1/models` reload endpoint; for Triton (future scaling) rely on Model‑Control‑Mode POLL. This keeps the PoC architecture flexible for later model upgrades. 
5. **Security Boundary** – All inbound traffic passes through a TLS‑terminating reverse proxy (Nginx) that enforces mTLS, ensuring only authorized test agents (certified with the shared CA) can invoke the LLM service. This aligns with the PoC’s “recording robustness outranks implementation neatness” priority.

---

## 9. Implementation Blueprint

1. **Repository Layout** – Create a `mena-runtime/` repo with two Docker‑Compose files (`docker-compose.vllm.yml` and `docker-compose.ollama.yml`). 
2. **Runtime Selector** – Add a `runtime_selector.sh` script that reads `RUNTIME` and invokes the appropriate compose file. 
3. **Provider Adapters** – Implement `vllm_adapter.py` and `ollama_adapter.py` conforming to the `LocalLLMProvider` interface. 
4. **TLS Assets** – Store generated CA, server, and client certificates in a `certs/` directory (git‑ignored). Dockerfiles copy the server certs; test scripts mount client certs at runtime. 
5. **Metrics** – Enable Prometheus exporters (`vllm_exporter`, `bentoml_exporter`) and configure Grafana dashboards to monitor throughput, latency, and error rates per runtime. 
6. **CI Integration** – In the CI pipeline, run `runtime_selector.sh RUNTIME=vllm` for GPU runners and `RUNTIME=ollama` for CPU‑only runners, then execute the full end‑to‑end test suite.

---

## 10. Validation & Next Steps – Detailed Plan

| Step | Description | Success Metric |
|------|-------------|----------------|
| **CPU‑Only vLLM Benchmark** | Build a Docker image that runs vLLM with the `--cpu` flag on an Intel i5 test machine; record tokens / s and latency for a 7 B model. | ≥ 5 t/s (acceptable baseline). |
| **Memory Footprint Profiling** | Use `nvidia‑smi` and `top` to log VRAM/RAM usage during a 500‑token generation run on both runtimes. | vLLM ≤ 16 GB RAM; Ollama ≤ 5 GB VRAM (GPU) or ≤ 6 GB RAM (CPU). |
| **End‑to‑End Summarization Timing** | Run the full pipeline (audio → transcription → diarization → summarization) on a 30‑minute meeting file; measure total wall‑clock time. | ≤ 5 min on GPU laptop; ≤ 12 min on CPU laptop. |
| **Usability Test on Weak Machines** | Provide a “start‑PoC” script to five external testers; collect feedback on installation time, error messages, and runtime selection. | ≥ 80 % report “installation easy”. |
| **mTLS Failure‑Mode Testing** | Attempt connections with missing, expired, or mismatched client certificates; verify that the server returns 401/403 and logs the attempt. | All unauthorized attempts are rejected and logged. |

Completing these steps will provide quantitative evidence that the chosen primary and secondary runtimes meet all PoC constraints and will inform any necessary adjustments before finalizing the architecture.

---

## 11. Risks, Disqualifiers, and Open Questions

| Risk | Impact | Mitigation |
|------|--------|------------|
| **GPU availability on all PoC machines** | vLLM cannot start; fallback required. | Automatic runtime selection (Ollama + BentoML) based on hardware detection. |
| **CUDA driver incompatibility** | Runtime crashes; delays. | Pin Docker image to a specific CUDA version; CI validates driver‑image compatibility. |
| **Model size exceeding GPU memory** | OOM errors on laptops with ≤ 12 GB VRAM. | Use quantized models (as shown in the Ollama benchmark) to keep memory ≈ 4 GB[15]. |
| **Certificate expiry** | Service downtime. | Automate cert rotation (short‑lived certs renewed every 30 days). |
| **Licensing audit for NVIDIA NIM** | Potential compliance breach if used on non‑NVIDIA hardware. | Restrict NIM deployment to machines with NVIDIA GPUs; document policy. |
| **Edge‑device relevance (MNN‑LLM)** | May be unnecessary for current PoC scope. | Keep MNN‑LLM as a research placeholder for future ultra‑lightweight agents. |

---

## 12. Conclusion

- **Primary runtime:** **vLLM** on GPU‑enabled machines – delivers unmatched throughput, deterministic OpenAI‑compatible API, and clean provider abstraction. 
- **Secondary fallback:** **Ollama + BentoML** – guarantees operability on any CPU‑only Linux laptop, with minimal installation overhead and the same provider‑runtime boundary.

Together they satisfy the PoC’s fixed constraints, prioritize recording robustness and sovereignty, and provide a clear migration path to more advanced serving stacks (Triton, NIM) as the product evolves.

--- 

## Sources

1. Run LLMs Locally: Hardware Tiers, Tools Compared & Setup Guide – BigData Boutique[1]  
2. How to Run and Customize LLMs Locally with Ollama[2]  
3. MENARA – Middle East and North Africa Regional Architecture[3]  
4. GRAPH‑BASED PROOF OF CONCEPT IN CLOSED LOOP … – ITU[4]  
5. Use a Local Inference Server — NVIDIA NemoClaw Developer Guide[5]  
6. How to Implement Multi‑Model Servers with Triton Inference Server on Kubernetes[6]  
7. Running Local LLMs with Ollama: 3 Levels from Laptop to Cluster‑Scale Distributed Inference[7]  
8. GitHub – alibaba/MNN (MNN inference engine)[8]  
9. Ollama vs. vLLM: A deep dive into performance benchmarking – Red Hat Developer[9]  
10. vLLM vs Ollama: Key differences, performance, and how to run them – Northflank[10]  
11. 10 AI Model Deployment Platforms to Consider in 2025[11]  
12. Noema – Local AI Assistant[12]  
13. Optimizing vLLM Performance on A100 80GB – DatabaseMart (first page)[13]  
14. Optimizing vLLM Performance on A100 80GB – DatabaseMart (second page)[14]  
15. Comparing LLMs performance on Ollama on 16GB VRAM GPU[15]  
16. Hacker News discussion on Llama 2 latency[16]  
17. ollama (reference URL)[17]  
18. ai.example.com (reference URL)[18]  
19. Secure Self‑Hosted AI — Security & Best Practices for Ollama[19]  
20. Server Authentication Setup – Ollama Docs[20]  
21. Security – vLLM (TLS documentation)[21]  
22. How to Configure Mutual TLS (mTLS) Authentication for Client Verification[22]  
23. Local Large language models hardware benchmarking — Ollama benchmarks[23]  
24. 6 Surprising Facts About Running Llama3 8B on NVIDIA A100 PCIe 80GB[24]  
25. Benchmarking LLMs on Dual Nvidia A100 GPUs with Ollama – LinkedIn post[25]  
26. Inference Protocols and APIs — NVIDIA Triton Inference Server[26]  
27. Model Management — NVIDIA Triton Inference Server[27]  
28. NVIDIA Triton Inference Server – User Guide Index[28]  
29. NVIDIA NIM Support Matrix (2026)[29]  
30. Quickstart with NeMo‑Run — NVIDIA NeMo Framework User Guide[30]  
31. MNN‑LLM: A Generic Inference Engine for Fast Large Language Model Deployment on Mobile Devices[31]  
32. Alibaba’s Mobile Neural Network – Fritz AI[32]  
33. ollama:11434 (reference URL)[33]  
34. How to Set Up a Local LLM Server with LM Studio & Ollama[34]  
35. localhost:8000 – Model endpoint[35]  
36. localhost:8000 – API endpoint[36]  
37. OpenAI‑Compatible Server — vLLM[37]  
38. How to Implement vLLM with OpenAI‑Compatible API[38]  
39. localhost:8000 (reference URL)[39]  
40. Connect to your own LLM using vLLM (air‑gapped environments) – Elastic Docs[40]  
41. Mutual TLS (mTLS) Configuration – NVIDIA Docs[41]  
42. Inference Protocols and APIs — NVIDIA Triton Inference Server (archived)[42]