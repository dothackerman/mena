# Containing third-party diagnostics at an in-process Python adapter boundary

Date: 2026-07-29

Status: focused technical research for Mena's pyannote adapter. Primary sources only. The locally installed runtime inspected for this note uses Python 3.13, `pyannote.audio` 4.0.4, and PyTorch 2.12.0.

## Question

How can an in-process Python adapter prevent third-party warnings and incidental output from leaking into a user-facing CLI, while retaining actionable diagnostics and avoiding process-wide suppression?

## Executive conclusion

The two messages observed during Mena diarization are Python warnings, not ordinary stdout:

- pyannote's TF32 message is emitted with `warnings.warn(ReproducibilityWarning(...))`;
- the one-frame standard-deviation message originates in PyTorch's tensor reduction invoked by pyannote's statistics-pooling layer and is surfaced as a `UserWarning`.

Python warnings normally render to `sys.stderr`, but they have their own interception mechanism. Mena should therefore **capture warnings narrowly around pyannote pipeline construction/device movement/inference, classify the captured warning records inside the pyannote adapter, and expose only Mena-owned diagnostics**. It should not redirect stdout/stderr merely to handle these warnings, and it should not install a process-wide warning filter.

There is one important constraint: on Mena's current Python 3.13 runtime, `warnings.catch_warnings` temporarily changes process-global warnings state and is not concurrency-safe. The narrow in-process approach is appropriate only while diarization calls are serialized. If Mena later permits concurrent in-process provider calls, or needs guaranteed containment of native writes to file descriptors 1/2, a worker subprocess becomes the honest isolation boundary.

## Four different output mechanisms

These mechanisms look similar in a terminal but require different controls.

### 1. Python warnings

`warnings.warn` enters Python's warnings filter. If the warning is selected for display, the default `showwarning` implementation formats it and writes it to `sys.stderr`. `warnings.catch_warnings(record=True)` instead returns warning records and suppresses their normal rendering. Each record exposes at least `message`, `category`, `filename`, and `lineno`.

This is the mechanism used by pyannote's reproducibility code. In pyannote 4.0.4, `fix_reproducibility` mutates the two PyTorch TF32 settings and then calls `warnings.warn` with pyannote's `ReproducibilityWarning`. The warning therefore reports an action pyannote has already taken; it is not an independent error stream.

Sources:

- [Python 3.13 `warnings` documentation](https://docs.python.org/3.13/library/warnings.html)
- [pyannote reproducibility source](https://github.com/pyannote/pyannote-audio/blob/4.0.4/src/pyannote/audio/utils/reproducibility.py)
- [pyannote issue #1370: TF32/reproducibility rationale](https://github.com/pyannote/pyannote-audio/issues/1370)

### 2. Python logging

Python logging records pass through logger handlers. `logging.captureWarnings(True)` only redirects warnings into a logger named `py.warnings`; it is not a general third-party-output capture mechanism. It also changes warning handling for the process until disabled, so it is poorly matched to an adapter-local boundary.

PyTorch's separate `_logging` controls apply only to supported PyTorch logging components, are documented as beta, and explicitly do not control every PyTorch message. They do not classify arbitrary Python warnings from pyannote.

Sources:

- [Python 3.13 `logging.captureWarnings`](https://docs.python.org/3.13/library/logging.html#logging.captureWarnings)
- [PyTorch `torch._logging`](https://docs.pytorch.org/docs/stable/logging)

### 3. Python writes to `sys.stdout` or `sys.stderr`

`contextlib.redirect_stdout` and `redirect_stderr` temporarily replace the corresponding `sys` stream. Python documents this as a global side effect, unsuitable for library code and most threaded applications. It also does not affect subprocess output. Reentrancy does not make it thread-safe.

This mechanism can capture Python `print` calls that resolve `sys.stdout`/`sys.stderr` at write time, but it is unnecessary for the two observed warnings because `catch_warnings(record=True)` intercepts them before default rendering.

Source: [Python 3.13 `contextlib.redirect_stdout` and `redirect_stderr`](https://docs.python.org/3.13/library/contextlib.html#contextlib.redirect_stdout)

### 4. Native file-descriptor writes and subprocess output

C/C++/CUDA code may write directly to operating-system file descriptors 1 and 2, bypassing replaced `sys.stdout` and `sys.stderr` objects. Capturing those writes in-process requires file-descriptor manipulation such as `dup`/`dup2`, which is process-global and can also divert unrelated threads' output.

A subprocess provides the stronger boundary: its stdout and stderr handles can be set to pipes or `DEVNULL`, while the parent retains its own streams. Python recommends `communicate()` when both pipes are used so a full pipe cannot deadlock the child.

Sources:

- [Python 3.13 `os.dup` and `os.dup2`](https://docs.python.org/3.13/library/os.html#os.dup2)
- [Python 3.13 subprocess stream handling](https://docs.python.org/3.13/library/subprocess.html#popen-constructor)
- [Python 3.13 `Popen.communicate`](https://docs.python.org/3.13/library/subprocess.html#subprocess.Popen.communicate)

## Concurrency constraint in Mena's supported Python range

Python 3.13's `catch_warnings` implementation saves and restores the warnings filter and `showwarning` hook globally. Its documentation says simultaneous use by multiple threads has undefined behavior. Python 3.14 introduced optional context-aware warning state backed by `ContextVar`; that does not make a Python 3.11–3.13-compatible Mena implementation safe by itself.

`redirect_stdout`, `redirect_stderr`, and file-descriptor redirection have the same or stronger global-state problem. A lock can serialize Mena's own provider calls, but it cannot prevent an unrelated thread or dependency thread from emitting output during the redirected interval.

Sources:

- [Python 3.13 warning capture and thread-safety note](https://docs.python.org/3.13/library/warnings.html#testing-warnings)
- [Python 3.14 concurrent warning-context design](https://docs.python.org/3.14/library/warnings.html#concurrent-safety-of-context-managers)
- [Python `contextlib` thread-safety warning](https://docs.python.org/3.13/library/contextlib.html#contextlib.redirect_stdout)

## Recommended narrow PoC design

### Keep the two architectural concerns separate

1. Application-workflow status such as “processing completed” and “semantic review skipped” should become typed workflow notices/results rendered by the driving adapter. This is not interception; Mena owns those messages and should route them deliberately.
2. Provider-library warnings should be translated at the pyannote adapter boundary. They are dependency diagnostics and should not escape through Python's default terminal renderer.

They share the principle “only the driving adapter renders user-facing text,” but require different mechanisms and should not be implemented as a generic output-capture facility.

### Capture only the warning channel

Wrap all operations that can execute pyannote/PyTorch warning-producing code:

- module import if lazy import emits warnings;
- `Pipeline.from_pretrained`;
- device movement (`pipeline.to(...)`), where pyannote calls its reproducibility fix;
- pipeline inference.

Use `warnings.catch_warnings(record=True)` with `warnings.simplefilter("always")` inside the context. `always` matters because Mena needs to classify every warning deterministically instead of inheriting user/interpreter filters or losing a repeated warning through a module warning registry.

After the operation:

- classify by warning category first and stable origin/message details second;
- convert known warnings into Mena-owned diagnostic values;
- keep the TF32 “disabled” warning at debug/trace severity because it reports a completed safety action;
- retain the one-frame `std()` warning as a quality/telemetry diagnostic until evidence establishes whether it affects output quality;
- preserve unknown warnings as a generic provider diagnostic with category, message, source module/file, and line number rather than silently dropping them;
- let the application decide what diagnostic severity reaches a driving adapter, and let the CLI decide normal versus verbose rendering.

Do not call `warnings.filterwarnings` at import/startup scope. Do not blanket-ignore `UserWarning`: that would discard future actionable warnings from pyannote and PyTorch.

### Make serialization an explicit invariant

For the current Python support range, document and test that only one in-process pyannote operation may occupy the warning-capture context at a time. If the processing architecture already serializes GPU provider work, reuse that invariant rather than adding an unrelated global “output lock.”

This is a bounded PoC compromise, not perfect isolation. If concurrent provider execution becomes a requirement, move pyannote execution behind a worker process instead of expanding stream-redirection machinery.

### Do not intercept stdout/stderr without observed evidence

There is no need to add `redirect_stdout`/`redirect_stderr` for the warnings observed on 2026-07-29. Doing so would:

- mutate global `sys` streams;
- risk capturing unrelated application/thread output;
- still miss native file-descriptor writes;
- mix unstructured text parsing with the structured warning records Python already provides.

If a later fixture demonstrates incidental Python `print` output, capture it only as a consciously accepted serialized-PoC compromise and preserve it as an unknown provider diagnostic. If native output is demonstrated, prefer subprocess isolation over in-process `dup2`.

## Suggested adapter contract shape

The exact names should follow Mena's existing interaction/result vocabulary, but the information boundary should resemble:

```python
@dataclass(frozen=True)
class ProviderDiagnostic:
    code: str
    severity: Literal["debug", "quality_warning", "warning"]
    message: str
    origin: str | None = None


@dataclass(frozen=True)
class DiarizationResult:
    # existing fields ...
    diagnostics: tuple[ProviderDiagnostic, ...] = ()
```

Alternatively, diagnostics can be emitted through an existing application interaction/diagnostic port if they must stream while work runs. What matters is that the core sees a Mena-owned value, never pyannote warning classes or terminal strings.

Avoid making diagnostic text part of checkpoint identity or deterministic diarization output. A dependency patch can change warning wording without changing semantic diarization results.

## Tests that should accompany a ticket

1. A provider-factory warning raised during pipeline construction does not appear on process stderr and is returned/emitted as a diagnostic.
2. A warning raised during `pipeline.to(...)` is captured; the known pyannote reproducibility warning maps to the stable Mena code.
3. A warning raised during pipeline inference is captured.
4. An unknown warning is preserved rather than discarded.
5. Warning classification does not import pyannote types into the application/core contract.
6. Mena workflow notices are rendered by a fake interaction/driving adapter without writing stdout directly.
7. A CLI test proves normal mode omits debug diagnostics and diagnostic/verbose mode can render them.
8. A concurrency/architecture test or explicit invariant prevents concurrent entry into the Python 3.13 warning-capture region.

## Decision summary

For the PoC:

- use adapter-local `catch_warnings(record=True)` and classify records;
- route Mena-owned workflow notices through a typed result/interaction port;
- do not redirect stdout/stderr for the currently observed behavior;
- do not globally suppress warnings;
- preserve unknown and quality-relevant diagnostics;
- keep provider execution serialized while Python 3.11–3.13 remains supported.

Escalate to a subprocess boundary only when concurrent isolation, native-output containment, crash containment, or stronger GPU-worker lifecycle control becomes a real requirement.
