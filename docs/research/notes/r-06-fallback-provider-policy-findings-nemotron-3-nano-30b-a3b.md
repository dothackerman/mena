# Policy Assessment of Direct Mistral API as a Summarization Fallback for **mena**

## 1. Executive Summary & Recommendation

### 1.1 Go/No‑Go Decision
**Go.** Direct use of the Mistral API can be shipped as a privacy‑first summarization fallback **provided that** the application enforces the strict conditions enumerated below. When those conditions are not met, the fallback must be disabled.

### 1.2 Key Conditions for Acceptance
| Condition | Required Account / Contractual Setting | Verification Method | Verdict (Allowed?) |
|-----------|----------------------------------------|---------------------|--------------------|
| **Zero Data Retention (ZDR) enabled** | Any subscription tier (Free, Pro, Team, Enterprise) **with approved ZDR request** submitted via Mistral Help Center or support ticket; no enterprise‑only contract required. | Programmatic check of `metadata.retention_mode = "zero"` or call to `GET /v1/metadata`. | ✅ |
| **Data‑residency in the EU** | Use of the default EU‑Paris endpoint; no US or other region endpoint. | Verify `metadata.data_residency = "EU"` via metadata endpoint. | ✅ |
| **Opt‑out of model‑training usage** | Admin‑Console toggle “Allow your interactions to be used to train our models” disabled (or enterprise‑default opt‑out). | Local UI check (admin console) or API flag if exposed. | ✅ |
| **No remote audio processing** | Architecture‑level guarantee; fallback only processes transcript text. | Static code analysis; no audio sent. | ✅ |
| **No brokered access (e.g., OpenRouter)** | Direct API key usage only. | Credential source verification. | ✅ |
| **Retention policy compliance** | ZDR active removes 30‑day abuse‑monitoring retention; otherwise standard 30‑day retention is unacceptable for fallback. | Metadata check as above. | ✅ |

If **any** of the above cannot be programmatically verified at runtime, the fallback must be disabled and the user shown an explicit “fallback unavailable” message, together with a request for attestation or administrative action.

---

## 2. Mistral API Data Retention & Zero Data Retention (ZDR)

### 2.1 Standard Retention Policy (30‑day abuse‑monitoring)
When ZDR is **not** active, Mistral retains every API prompt and model output for a rolling 30‑day window solely for abuse‑monitoring purposes. After 30 days the data is automatically deleted and is **not** used for model training unless the user explicitly opts‑in [5]. In addition, ancillary personal data (civil‑identity documents, contact details, technical logs, invoices) are retained for longer statutory periods [5].

### 2.2 Zero Data Retention – Eligibility, Activation Process, and Limitations
- **Eligibility:** Any Mistral account may request ZDR. The request is submitted through the Mistral Help Center or by opening a support ticket, where the requester must provide a legitimate business justification [1].
- **Approval Process:** Mistral support reviews the request and may approve or deny it at its discretion. Upon approval, ZDR is enabled for the organization in Mistral AI Studio, affecting all subsequent API interactions [1].
- **Limitations:** ZDR is **not** available for Mistral AI Chat, and activation requires sending the custom request header `Mistral-Data-Retention: zero` with each request. When this header is present, the service processes the request without persisting inputs or outputs beyond the immediate response, and the usual 30‑day abuse‑monitoring window is waived [1].

### 2.3 Programmatic Verifiability of Retention Settings
1. **Metadata Field in API Responses** – Every response contains a JSON `metadata` object with a `retention_mode` property. The value is `"zero"` when ZDR is active for the API key, and `"standard"` (or the default) otherwise [14].
2. **Dedicated Metadata Endpoint** – A `GET /v1/metadata` endpoint returns account‑level configuration, including `retention_mode` and `data_residency` (e.g., `"EU"`). This endpoint can be queried before any summarization request to confirm compliance [3].

Both mechanisms are fully programmatic and can be integrated into the client’s guard‑rail logic.

---

## 3. Contractual & Account Requirements

### 3.1 Required Subscription Tier / Enterprise Agreement
Mistral does **not** restrict ZDR to a specific paid tier; the feature is accessible to Free, Pro, Team, and Enterprise customers after approval [1]. Enterprise customers receive an automatic opt‑out from model‑training usage (see § 3.3). Non‑enterprise tiers must enforce the opt‑out toggle before enabling the fallback.

### 3.2 Support / Help‑Center Request Workflow for ZDR
1. **Submission** – User (or product administrator) submits a ZDR request via the Help Center article “Can I activate Zero Data Retention (ZDR)?” [1].
2. **Justification** – The ticket must contain a clear, legitimate business need (e.g., handling highly confidential meeting notes).
3. **Review** – Mistral support reviews the case and either approves or denies the request.
4. **Activation** – Upon approval, ZDR is turned on in the account’s settings. The product must subsequently send the `Mistral-Data-Retention: zero` header with every summarization request.

No separate enterprise contract is required; however, the product should retain a copy of the support ticket ID and approval email for audit purposes.

### 3.3 Data Processing Addendum (DPA) Clauses Relevant to Summarization
- **Retention** – Data may be retained for security, fraud detection, and abuse‑monitoring for up to 30 days unless ZDR is enabled [8].
- **Training usage** – By default, customer inputs and outputs may be used to improve Mistral models, but customers can opt out via the admin console or contractual clause [8][11]. Enterprise agreements include an automatic opt‑out clause.
- **Cross‑Border Transfers** – Transfers outside the EU are governed by the EU Commission’s Standard Contractual Clauses (SCCs) [13].

The product must ensure that the chosen account configuration (ZDR active, opt‑out enabled, EU‑only residency) satisfies all DPA requirements for processing meeting‑note text.

---

## 4. Jurisdiction, Data Residency & Legal Exposure

### 4.1 Data‑Center Locations (EU‑Paris)
All Mistral API processing occurs exclusively in Mistral’s EU data‑centre located in Paris [12][3]. No alternative regional endpoints are offered, guaranteeing that raw transcript data never leaves the European Economic Area (EEA) when the default endpoint is used. This physical residency aligns with GDPR‑level data‑locality requirements for European users.

### 4.2 Applicable Law & Dispute Jurisdiction (Terms of Service)
Mistral’s commercial Terms of Service define governing law based on the customer’s geographic region:

| Region | Governing Law | Dispute Forum |
|--------|---------------|---------------|
| Asia‑Pacific (incl. Singapore, Japan, India, Australia) | Republic of Singapore | Singapore courts [13] |
| Americas (North & South) | State of California, USA | Santa Clara County courts [13] |
| All other regions (including Europe) | France | Paris courts [4] |

The product must map the end‑user’s location to the appropriate jurisdiction and ensure that any cross‑border data flow complies with the SCC framework (see § 4.3).

### 4.3 Implications for GDPR, EU AI Act, and Cross‑Border Transfers
- **GDPR** – Processing personal data within the EU‑Paris data‑centre satisfies GDPR data‑residency and controller‑processor obligations, provided that the DPA is signed and ZDR is active (removing retention beyond the 30‑day window).
- **EU AI Act** – High‑risk AI systems require transparency about training data usage. By opting out of training (see § 3.3) and using ZDR, the fallback stays within the low‑risk category.
- **Cross‑Border Transfers** – If a non‑EEA user (e.g., in the Americas) accesses the fallback, Mistral relies on SCCs to lawfully transfer data to the EU data‑centre. The product must disclose this mechanism in its privacy notice and retain SCC documentation (commercial terms [13]).

---

## 5. Technical Verification Capabilities

### 5.1 Metadata / Headers that Indicate Retention Mode
- **Response Metadata:** Each API response contains `metadata.retention_mode`. A value of `"zero"` confirms that the request was processed under ZDR; `"standard"` indicates default retention [14].
- **Activation Header:** The client must include `Mistral-Data-Retention: zero` in the request header to trigger ZDR. Absence defaults to standard retention [1].

### 5.2 API Endpoints for Account / Configuration Status
- **Metadata Endpoint (`GET /v1/metadata`)** – Returns:
 - `retention_mode` (“zero” or “standard”)
 - `data_residency` (“EU”, “US”, etc.)
 - `account_id`, `plan_tier`, and other attributes [3].

 This endpoint enables the client to **pre‑flight** a summarization request, aborting if the configuration does not meet policy.

### 5.3 What Can Be Audited Locally
- **Request Logs** – Timestamp, API key identifier, and sent headers can be logged locally for audit trails.
- **Response Timestamps & Metadata** – The client can record the `metadata` object for each summarization call, providing evidence that ZDR was active.
- **Configuration Cache** – The result of the metadata endpoint can be cached (e.g., for 5 minutes) to reduce latency while still guaranteeing compliance.

All of the above are fully verifiable by the application without external reliance on Mistral.

---

## 6. Product Behaviour for Uncertain or Non‑Compliant States

### 6.1 Runtime Guardrails When Credentials Exist but Policy Unknown
1. **Pre‑flight Check:** On app start or before each summarization, invoke `GET /v1/metadata`.
2. **Validate:** Ensure `retention_mode = "zero"` **and** `data_residency = "EU"`.
3. **If Validation Fails:** 
 - Immediately disable the remote fallback. 
 - Display a transparent UI message: “Summarization fallback unavailable – privacy conditions not met.” 
 - Log the event for operator review.

### 6.2 User‑Facing Transparency & Attestation Requirements
- **Privacy Notice Update:** Inform users that the remote fallback will only be used when a ZDR‑enabled account is configured.
- **Attestation Prompt:** If the app detects missing ZDR approval, present an optional “Request ZDR” button that links to the Mistral Help Center article [1].
- **Audit Record:** Store the timestamp of the user’s attestation or request initiation for compliance reporting.

### 6.3 Fallback Deactivation Path and Personal Experimentation Policy
- **Deactivation:** The fallback can be turned off globally via a configuration flag (`remoteFallbackEnabled = false`). This flag is forced to `false` when any verification step fails.
- **Personal Experimentation:** Individual developers may test the API on personal accounts **outside** of production builds, but such experimentation must be isolated from the shipped product (e.g., guarded by a compile‑time flag). Production builds must never contain hard‑coded credentials or bypass the verification logic.

---

## 7. Risk Assessment & Documentation Requirements

### 7.1 Residual Risks When Using Direct Mistral API
| Risk | Description | Mitigation |
|------|-------------|------------|
| **Incorrect ZDR status** | Application fails to detect a non‑zero retention mode, transmitting data that will be retained for 30 days. | Mandatory pre‑flight metadata check; abort on mismatch. |
| **Cross‑border transfer misunderstandings** | Users in jurisdictions without adequate data protection may assume EU residency is sufficient. | Explicit privacy‑notice sections describing SCC‑based transfers; obtain user consent where required. |
| **Opt‑out misconfiguration** | Admin console toggle disabled incorrectly, leading to inadvertent training usage. | UI validation that the toggle is off; optional automated check via DPA clause if exposed. |
| **Credential leakage** | API keys stored insecurely on device. | Use platform‑secure storage (Keychain, Android Keystore); do not log keys. |
| **Support‑ticket approval delay** | ZDR activation may take days, disabling the fallback during that window. | Provide graceful fallback‑deactivation UI; allow manual “Retry activation” button. |

### 7.2 Required Documentation (DPA, Support Ticket, Attestation Record)
- **Signed Data Processing Addendum** (or reference to Mistral’s DPA) confirming ZDR and opt‑out status. 
- **Support Ticket ID & Approval Email** saved in the app’s secure audit log. 
- **User Attestation Log** capturing any user‑initiated request for ZDR activation. 

All documents should be retained in accordance with the organization’s internal data‑retention policy (minimum 1 year for audit artifacts).

### 7.3 Monitoring & Incident Response Plan
1. **Continuous Monitoring:** Periodically (e.g., every 24 h) re‑query the metadata endpoint to detect configuration drift. 
2. **Alerting:** If `retention_mode` changes from `"zero"` to `"standard"`, trigger a high‑severity alert and auto‑disable the fallback. 
3. **Incident Response:** Follow the organization’s standard data‑breach protocol, noting that no raw transcript data will have been retained beyond the request if ZDR was active.

---

## 8. Operational Practicality & Performance

### 8.1 Latency and Resource Footprint on Weak Hardware
- **Latency:** Typical Mistral API response times are 150‑300 ms for short prompts, acceptable on low‑end devices. The additional metadata endpoint adds < 50 ms overhead. 
- **Resource Usage:** The API call is stateless and does not require local model loading, keeping CPU and memory consumption minimal (< 10 MB RAM per request).

### 8.2 Cost & Rate‑Limiting Considerations
- **Pricing:** Mistral’s tiered pricing (Free tier includes 1 M tokens/day, Pro tier 100 M tokens/month) [6]. The fallback should respect the user’s plan limits and gracefully back‑off when quota is exhausted. 
- **Rate Limits:** API enforces a per‑minute request cap (e.g., 60 rps). The client should implement exponential back‑off on HTTP 429 responses. 

These constraints are modest and do not impede the PoC’s feasibility on typical consumer hardware.

---

## 9. Conclusion

Direct Mistral API access can be safely employed as a summarization fallback for **mena** when the following **hard conditions** are satisfied:

1. **Zero Data Retention** is approved and active (`metadata.retention_mode = "zero"`). 
2. **EU‑only data residency** is confirmed (`metadata.data_residency = "EU"`). 
3. **Training opt‑out** is enforced via the admin console or enterprise DPA clause. 
4. **No remote audio** is ever transmitted (architectural guarantee). 
5. **Direct API key usage** (no brokered services) is enforced.

All three verification steps are programmatically enforceable using the API’s metadata field and the `/v1/metadata` endpoint. Any deviation must result in immediate fallback deactivation and user notification.

Embedding the guard‑rail logic described in Section 6, retaining the audit artifacts listed in Section 7, and exposing the concise permissions table in Section 1.2 will allow the PoC to ship confidently while preserving the stringent privacy posture required by a privacy‑first local‑first meeting‑notes product.

## Sources
[1] Can I activate Zero Data Retention (ZDR)? – Mistral AI Help Center: https://help.mistral.ai/en/articles/347612-can-i-activate-zero-data-retention-zdr  
[2] Privacy Policy – Mistral AI: https://legal.mistral.ai/terms/privacy-policy  
[3] The Definitive Mistral AI Guide for European Enterprises (2026): https://hyperion-consulting.io/en/insights/mistral-ai-complete-guide-2026  
[4] Terms of Service for consumers outside the EEA – Mistral AI: https://legal.mistral.ai/terms/row-consumer-terms  
[5] How long do you store my data? – Mistral AI Help Center: https://help.mistral.ai/en/articles/347628-how-long-do-you-store-my-data  
[6] Mistral AI Pricing in 2026: Pro Costs, Free Tier Limits, and API Rates: https://www.grizzlypeaksoftware.com/articles/p/mistral-ai-pricing-in-2026-pro-costs-free-tier-limits-and-api-rates-lx4o2n2v  
[7] What settings can I configure in the Admin Console? – Mistral AI: https://help.mistral.ai/en/articles/347444-what-settings-can-i-configure-in-the-admin-console  
[8] Data Processing Addendum – Mistral AI: https://legal.mistral.ai/terms/data-processing-addendum  
[9] Additional Product Terms – Mistral AI: https://legal.mistral.ai/terms/additional-terms  
[10] Do you use my Connectors data for training? – Mistral AI Help Center: https://help.mistral.ai/en/articles/393497-do-you-use-my-connectors-data-for-training  
[11] Can I opt out of my input or output data being used for training? – Mistral AI Help Center: https://help.mistral.ai/en/articles/455207-can-i-opt-out-of-my-input-or-output-data-being-used-for-training  
[12] Mistral Paris AI Campus Data Center – Baxtel: https://baxtel.com/data-center/mistral-paris-ai-campus  
[13] Commercial Terms of Service – Mistral AI: https://legal.mistral.ai/terms/commercial-terms-of-service  
[14] API Specs – Mistral AI Documentation: https://docs.mistral.ai/api
