# UI–core interaction boundary architecture

Status: research note for the Wayfinder investigation; not an accepted architecture decision.

Tracking issue: https://github.com/dothackerman/mena/issues/192

No Wayfinder map or session has been created. This note is a possible input to a future
Wayfinder session, not an implementation-ready specification or migration plan.

## Question

What architecture best fits a local-first Python application whose current human interface is a CLI, but which may later be driven by desktop, web, mobile, agent, batch, or test frontends? In particular, should application workflows call `choose`, `confirm`, `request_text`, or `notify` ports, or should frontends drive input-complete operations and explicit state transitions?

## Executive conclusion

Use **hexagonal architecture (ports and adapters) as the system-level model**, reinforced by the **Clean Architecture dependency rule** and **functional-core/imperative-shell discipline**. Treat CLI, desktop, web, mobile, agent, batch, and tests as **driving adapters** around one application core. Inside each human-facing adapter, use a presentation pattern appropriate to that toolkit (a simple controller for the CLI; MVVM/UDF for a rich GUI). Do not impose one UI pattern on the core.

For Mena, the application API should be made of typed commands/queries, typed results, and explicit operation state. A workflow must not request human input or issue presentation notifications. Multi-step human decisions should be represented as inspect/decide/apply transitions which the frontend orchestrates. Long-running work should expose typed operation state and semantic events that a frontend observes, plus explicit cancellation/stop operations; those events must not contain rendered prose or UI instructions.

“Frontend/backend” is a useful teaching analogy for dependency direction, but a misleading architecture name here. Mena is currently an in-process local application, not two deployed systems connected by a network protocol. The precise vocabulary is:

- **application core**: domain rules and application use cases;
- **driving port**: the core API offered to a caller;
- **driving adapter**: CLI, desktop, web endpoint, mobile bridge, agent tool, batch script, or test;
- **driven port**: a capability the application needs from the environment, such as storage, audio capture, clock, or model inference;
- **driven adapter**: the concrete filesystem, subprocess, audio, or provider implementation.

This recommendation is intentionally stricter than some legitimate Clean Architecture implementations. Clean Architecture permits a use case to call an output boundary implemented by a presenter. That proves a callback presenter can preserve source dependency direction; it does not prove it is the best contract for Mena. Mena's current `InteractionPort` combines human input, presentation notifications, and workflow control, making interactive assumptions part of otherwise reusable operations. Typed returns/state are the simpler contract for a local library with several prospective frontends.

## What the established patterns actually say

### Hexagonal architecture: best system-level fit

Alistair Cockburn's original Ports and Adapters description says the application should run without a UI or database and be driveable by users, programs, tests, or batch scripts. It distinguishes an application-controlling side from data-retrieval/output sides and explicitly lists GUI, HTTP, direct program calls, batch, and tests as substitutable adapters around purposeful ports ([original article](https://alistair.cockburn.us/hexagonal-architecture/)).

That is almost exactly Mena's expected topology. It does not require a server, HTTP, or a process boundary. A Python call from a CLI handler is as valid a driving adapter as an HTTP controller.

Hexagonal architecture also explains why `InteractionPort` is conceptually muddled. A human UI is normally the actor driving an application port. Storage, audio devices, subprocesses, and inference providers are driven dependencies. When a use case pauses to call `choose()` on the same UI that invoked it, the UI has been modeled simultaneously as driver and synchronous driven collaborator. This can be made to work, but it obscures ownership of the conversation and makes non-human callers script a UI dialogue rather than invoke an application capability.

### Clean Architecture: the non-negotiable dependency rule

Robert Martin's original Clean Architecture article states that source dependencies point inward; inner policy must not mention outer mechanisms or their data formats. It explicitly says business rules should remain testable without a UI and that a console UI should be replaceable by a web UI without changing them ([The Clean Architecture](https://blog.cleancoder.com/uncle-bob/2012/08/13/the-clean-architecture.html)). Controllers, presenters, views, and GUI-specific MVC belong in the interface-adapter layer; use cases orchestrate application-specific rules.

Clean Architecture also documents an important counterexample to an over-broad rule: control may flow from a use case to a presenter through a use-case-owned output interface. Thus, “the core calls a callback” is not automatically a dependency inversion violation. The decisive questions are whether the boundary is application-semantic, whether its types belong to the core, and whether it forces the use case to know the shape of a human conversation.

For Mena:

- an outbound `TranscriptionProvider` or repository port is appropriate because inference and persistence are capabilities required to complete a use case;
- a typed stream of `CaptureProgress` facts can be appropriate because it reports operation state without prescribing presentation;
- `confirm(prompt_topic)`, `choose(options)`, `request_text(topic)`, and `notify(topic)` are not appropriate application-core ports because they prescribe a UI dialogue and transfer orchestration of the use case to a human-facing adapter mid-call.

### Functional core, imperative shell: useful discipline, not the whole architecture

Gary Bernhardt's *Boundaries* advocates simple values as subsystem boundaries and connects that idea to isolated testing, concurrency, and the functional-core/imperative-shell style ([author's talk page](https://www.destroyallsoftware.com/talks/boundaries)). Applied to Mena, decisions and transformations should be expressed over typed values, while terminal I/O, filesystem calls, subprocess supervision, and device operations stay in thin imperative adapters.

This is a design discipline rather than a complete module topology. Mena necessarily has stateful orchestration, durable session mutation, audio processes, and provider calls; forcing the whole application core to be mathematically pure would add ceremony without eliminating effects. The useful rule is narrower: keep decision logic deterministic and make effects explicit behind application-owned ports.

### Application services/use cases: the stable API for every frontend

The Clean Architecture use-case layer is the correct home for application-specific orchestration, but “orchestration” means coordinating domain rules and driven capabilities—not conducting a terminal conversation. An application operation should receive enough typed intent to run until it reaches a meaningful application boundary, then return a typed outcome.

Examples:

```text
inspect_export(session, destination) -> ExportAssessment
export_summary(ApprovedExport) -> ExportResult

prepare_setup(environment) -> SetupPlan
apply_setup(AcceptedSetupPlan) -> SetupResult

review_next(session) -> ReviewItem | ReviewComplete
apply_correction(session, CorrectionAction) -> ReviewState
```

The frontend may call these in a loop. That loop is presentation/application coordination and belongs to the driving adapter or a frontend-specific controller. It can ask a person, accept an agent decision, read a batch policy, or map an HTTP request without changing the core.

### MVC, MVP, Passive View, and MVVM: frontend-local patterns

These patterns solve presentation structure, not the system-wide application boundary.

Martin Fowler's Passive View extracts application-specific behavior from widgets into a controller to improve UI testability ([Passive View](https://martinfowler.com/eaaDev/PassiveScreen.html)). His Presentation Model—now commonly called ViewModel—holds view state and presentation behavior without concrete controls, while coordinating with the domain layer ([Presentation Model](https://martinfowler.com/eaaDev/PresentationModel.html)). Microsoft's first-party MVVM guidance likewise makes the view depend on a view model, the view model depend on the model, and the model unaware of both; commands route user actions inward ([.NET MAUI MVVM](https://learn.microsoft.com/en-us/dotnet/architecture/maui/mvvm)).

These are good choices inside a future desktop/mobile frontend. They are excessive vocabulary for today's CLI and should not become core abstractions. A CLI command handler plus a command-local renderer/controller is enough. If a rich client arrives, it can add its own ViewModel without requiring the application core to expose UI-shaped state such as button enablement or localized messages.

### Reducers and explicit state machines: selective use

The Elm Architecture models an interactive program as `Model`, `View`, and `Update`, with messages driving deterministic state transitions ([official Elm guide](https://guide.elm-lang.org/architecture/)). Android's current official architecture guidance similarly recommends unidirectional data flow: user events flow toward a state holder and UI state flows back to rendering; it separates UI behavior such as navigation and snackbars from domain/data logic ([Android UI layer](https://developer.android.com/topic/architecture/ui-layer)).

This is valuable for Mena's inherently staged conversations—correction review, recording lifecycle, fallback authorization, setup planning—because it makes legal states and actions explicit and testable. It should not be universalized into one giant application reducer. Simple one-shot operations remain ordinary command/result use cases. State machines should be introduced only where the domain/application lifecycle has real states, concurrency, recovery, or invalid transitions to prevent.

The state types must be application-semantic (`CapturePrepared`, `Capturing`, `Finalizing`, `ReviewItem`, `FallbackAuthorizationRequired`), not widget-semantic (`ShowConfirmDialog`, `PrintWarning`, `PressQPrompt`). Each frontend derives its own UI state and wording.

## Interaction ownership decision

### Recommendation

The driving adapter owns the human/agent conversation. The application core owns rules, state transitions, and effects necessary to fulfill explicit intent.

Therefore application workflows must not:

- read stdin or write stdout/stderr;
- import CLI, web, desktop, or mobile modules or framework types;
- invoke generic `choose`, `confirm`, `request_text`, or UI `notify` callbacks;
- return localized/rendered prose as their primary contract;
- block while waiting for an unspecified future human decision.

Application workflows may:

- accept typed commands and policies supplied by a caller;
- return typed results, warnings, conflicts, available domain choices, and required-decision states;
- call driven ports for storage, inference, clock, audio/process control, and similar environmental capabilities;
- expose semantic progress/state from a long-running operation;
- clean up safely when an explicit stop/cancel operation or task cancellation occurs.

This division keeps UX policy where it belongs. For example, the application may determine that using a fallback model requires authorization and return `FallbackAuthorizationRequired(reason, consequences)`. The CLI may ask once, a desktop app may display a modal, a web frontend may persist a pending action, an agent may apply a pre-approved policy, and a batch invocation may fail closed. None needs to impersonate a synchronous console dialogue.

### Why not retain `InteractionPort`?

Its strongest argument is convenience: a single workflow reads like a complete user story, and tests can inject a scripted interaction. Clean Architecture's output-boundary pattern shows that dependency inversion can keep UI implementation types out of the core.

The costs dominate for Mena:

1. **It encodes temporal UI protocol inside the use case.** Every frontend must reproduce the same synchronous call-and-response order.
2. **It conflates input and output.** Requests, decisions, progress, warnings, and rendering topics share one collaborator despite different lifecycles.
3. **It weakens automation.** Batch and agent callers must provide fake conversational answers rather than complete intent or policy.
4. **It complicates interruption and resumption.** A callback stack waiting for input is not a durable state that a web/mobile client can disconnect from and resume later.
5. **Its tests prove a scripted conversation, not a stable application API.** Typed transition tests are clearer about legal states, decisions, and results.

Remove the generic interaction abstraction. Do not merely rename it to `FrontendPort`.

## Long-running capture, progress, and cancellation

Long-running operations are the main reason not to reduce every use case to a single synchronous return value. They still do not require the core to control a UI.

Recommended conceptual API:

```text
operation = capture.start(StartCapture(...))
operation.snapshot() -> CaptureState
operation.events() -> iterator/async iterator[CaptureEvent]
operation.stop(StopCapture(...)) -> CaptureResult
```

or, where persistence already provides the source of truth:

```text
capture.start(...) -> CaptureId
capture.status(CaptureId) -> CaptureState
capture.events(CaptureId, after=cursor) -> sequence[CaptureEvent]
capture.stop(CaptureId) -> CaptureResult
```

Choose the in-memory handle only if same-process lifetime is an accepted constraint. Choose identifier + persisted state if reconnect/resume across web/mobile clients or process restarts is a real requirement. Wayfinder should investigate this rather than assume network-style durability now.

Progress events are semantic facts such as `SegmentSealed`, `RouteDegraded`, `CaptureFinalizing`, or `ProviderFallbackUsed`; the frontend maps them to text, progress bars, notifications, logs, or silence. Avoid a generic `notify(message/topic)` sink because it recreates a presenter callback under another name.

Cancellation is a caller action and cleanup is an application responsibility. Python's official `asyncio` guidance specifies that cancellation raises `CancelledError` at the next opportunity, that coroutines should clean up with `try/finally`, and that cancellation should generally be propagated after cleanup ([Python task cancellation](https://docs.python.org/3/library/asyncio-task.html#task-cancellation)). Mena need not adopt `asyncio` merely for architectural purity; its existing worker/process model may warrant explicit `stop` and cooperative cancellation instead. The contract should distinguish:

- **stop**: a successful domain/application transition that finalizes usable capture;
- **cancel**: abort requested by the caller, with defined cleanup and artifact policy;
- **failure**: operation could not complete, with typed reason and recoverability;
- **process interruption**: adapter-level signal mapped into one of the above by the driving shell.

## Testability consequences

The recommended boundary gives three levels of deterministic tests:

1. Pure decision/transition tests: `(state, typed action) -> state/result`, without mocks.
2. Application-service tests with fake driven ports for storage, clocks, providers, audio/process control.
3. Adapter contract tests proving each frontend maps input to typed commands and typed outcomes/events to presentation.

This matches Cockburn's aim of running the application without a UI and Bernhardt's simple-value boundaries. CLI end-to-end tests remain valuable but stop carrying the burden of proving core workflow behavior.

## Deterministic enforcement

Mena's Python stack can enforce the structural portion with an AST/import checker in CI:

- no application/domain module imports `mena.cli` or any future frontend package;
- no application/domain module imports `argparse`, terminal UI libraries, or frontend frameworks;
- no application/domain module calls `print`, `input`, or accesses `sys.stdin/stdout/stderr`;
- no application/domain public API accepts `InteractionPort`, `ConfirmationRequest`, `TextRequest`, or `WorkflowNotice`;
- allowed dependency direction is frontend -> application -> domain, with driven adapter implementations depending on application-owned port contracts.

Static checks cannot prove that every string is non-presentational or that a type named `ProgressEvent` is semantically clean. Enforce the mechanical boundary deterministically, then use contract tests and architecture review for semantic quality. Make the CI check required for protected branches; otherwise “deterministic” only means repeatable, not merge-blocking.

## Alternatives rejected

### Network-style frontend/backend split now

Rejected as the base architecture. It introduces transport DTOs, serialization, service lifetime, authentication, versioning, and failure modes before Mena has a deployment need. A future web adapter can put HTTP around the same driving ports. Starting with a stable in-process application API preserves that option without paying distributed-system costs today.

### Clean Architecture presenter/output callbacks everywhere

Rejected as Mena's default despite being architecturally legitimate. It is useful when streaming output must be pushed through a boundary, but generic presenter callbacks encourage core-driven presentation choreography. Prefer return values, explicit state, and observable semantic operation events. Add a narrowly typed output mechanism only where measurement proves pull/return APIs inadequate.

### Universal reducer or state machine

Rejected. It would make straightforward operations indirect and could centralize unrelated workflows into a shallow event dispatcher. Use explicit states only for lifecycle-rich capabilities.

### UI-specific MVVM/MVC in the core

Rejected. These patterns belong inside a driving adapter. Sharing a ViewModel across CLI, web, and desktop would couple unlike presentation needs. Share application commands/results and domain semantics instead.

### Keep scripted interaction because tests are easy

Rejected. Scripted callbacks make the current dialogue easy to test but freeze its order into the application contract. Fakes for driven capabilities plus typed state-transition tests provide stronger reuse and clearer failure diagnosis.

## Mena-specific target shape

```text
CLI controller -----------+
Desktop ViewModel --------+--> application commands / queries / operation state
Web controller -----------+                 |
Mobile adapter -----------+                 v
Agent or batch adapter ---+         domain rules and transitions
                                            |
                                            v
                         application-owned driven ports
                         (storage, audio/process, providers, clock)
                                            |
                                            v
                              concrete infrastructure adapters
```

The Wayfinder map should next investigate, per workflow:

1. Which current `InteractionPort` calls express genuine application decisions versus presentation only?
2. Which decisions can be supplied as input policy, which require an inspect/decide/apply transition, and which represent a persistent operation state?
3. Which long-running events need pull snapshots, an iterator/stream, or durable event persistence?
4. What stop/cancel/recovery guarantees are required for capture and processing?
5. What is the smallest typed application API that supports CLI and noninteractive batch use now without blocking a future rich client?

The destination should be an approved application-boundary specification and migration map—not a premature choice of web-style transport or one universal interaction mechanism.
