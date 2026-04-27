# Bounded Research Note: Local Two-Process Protocol Patterns

Status: bounded research input for architecture drafting

Question in scope:

- local two-process application
- one capture-oriented process
- one orchestration/workflow process
- artifact-first persistence
- explicit internal command/event protocol
- CLI-first PoC now, future adapters later

## Actionable Conclusions

### 1. Recommended Naming Pattern

Use names tied to responsibility and authority, not vague centrality.

Recommended terminology:

- `Workflow Host Process`
  - owns user intent, session orchestration, review/export workflow, and policy decisions
- `Capture Worker Process`
  - owns capture state, segment flushing, recording warnings/errors, and source-artifact finalization facts

Recommended protocol role names:

- `host`
- `capture worker`

Avoid:

- `Main App Process`
- `Backend`
- `Daemon`
- `Server`

unless the implementation truly behaves like one of those concepts.

Reason:

- common local-tooling patterns such as LSP and DAP name one side as the tool/host/client and the other side as a specialized server/adapter, keeping the role names responsibility-driven rather than architecture-theater-driven

### 2. Recommended Contract Shape

Use a small `command + event` protocol with explicit ownership boundaries.

Recommended contract structure:

- `initialize / capabilities exchange`
  - lets each side declare supported features without breaking older peers
- `commands`
  - sent by the host when intent must change
  - examples: `StartCapture`, `StopCapture`, `GetCaptureStatus`, `ShutdownCaptureWorker`
- `responses`
  - only for commands that require acknowledgment or data
- `events`
  - sent by the capture worker when facts happen asynchronously
  - examples: `CaptureReady`, `CaptureStarted`, `SegmentFlushed`, `CaptureWarning`, `CaptureError`, `CaptureStopped`, `CaptureFinalized`
- `heartbeat`
  - include only because capture is long-running and stall-sensitive
  - treat heartbeat as liveness signal plus timeout policy, not as a generic event spam channel

Ownership boundary:

- `Workflow Host Process` is authoritative for:
  - user intent
  - requested lifecycle transitions
  - workflow state outside capture
- `Capture Worker Process` is authoritative for:
  - whether capture actually started
  - what artifacts were flushed
  - capture health and failure facts
  - whether source artifacts were finalized

Guardrails:

- pass `artifact references`, not audio payloads, through the protocol
- keep durable state in artifacts + session records, not only in process memory
- use explicit event codes/reasons for warnings and failures
- make stop/finalize commands idempotent

### 3. Keep Schema-First Local Protocols Simple Until a Real Boundary Appears

Stay simple first when these are true:

- same machine
- one host and one worker
- artifacts live on disk, not on the wire
- message count is small and domain-specific
- debugging and manual inspection matter
- the boundary is still evolving

Recommended first shape:

- schema-first JSON messages
- optionally JSON-RPC-style request/response + notifications
- transport later chosen between `stdio` or local socket based on implementation convenience

`protobuf` or `gRPC` becomes justified when one or more of these become true:

- multiple independent clients or adapters need the same stable contract
- cross-language generated types become more valuable than human readability
- long-lived background services or multi-session connections become a hard requirement
- structured streaming semantics and standardized health checks matter more than low setup cost
- the boundary may become remote, shared, or productized beyond one local PoC

Current recommendation for `mena`:

- use a simple schema-first local protocol first
- defer `protobuf/gRPC` to research unless the architecture later adds more independent peers or cross-language pressure
- if `protobuf` is later adopted, keep live RPC schemas separate from long-term storage schemas

### 4. Pitfalls Web Architects Often Miss in Local CLI/Desktop Tools

- `stdout` is not a free-for-all if used as a protocol transport
  - protocol traffic and human-readable logs must be separated
- local processes do not get API discipline for free
  - without an explicit protocol, teams drift into hidden shared-state coupling
- durable artifacts and live coordination are different concerns
  - do not infer liveness from files alone
  - do not send large/binary artifacts over the control protocol
- signal handling matters
  - `Ctrl-C`, parent death, duplicate stop requests, and half-failed shutdown paths need explicit behavior
- device and routing changes are normal, not edge cases
  - disappearing audio devices and polluted system-output capture need first-class warnings/errors
- local data layout matters early
  - repo files, runtime files, caches, logs, and user meeting data must not collapse into one directory tree
- local protocol and schema versioning matter just like HTTP API versioning
  - “it is all on one machine” does not protect against format drift

## Recommended Terminology to Reuse in `mena`

- `Workflow Host Process`
- `Capture Worker Process`
- `host command`
- `capture event`
- `capability exchange`
- `source-artifact reference`
- `capture health`
- `finalization fact`

## Sources

- JSON-RPC 2.0 Specification: https://www.jsonrpc.org/specification
- Language Server Protocol overview: https://microsoft.github.io/language-server-protocol/
- Debug Adapter Protocol overview: https://microsoft.github.io/debug-adapter-protocol/overview.html
- gRPC core concepts: https://grpc.io/docs/what-is-grpc/core-concepts/
- gRPC health checking: https://grpc.io/docs/guides/health-checking/
- Protocol Buffers best practices: https://protobuf.dev/best-practices/dos-donts/
