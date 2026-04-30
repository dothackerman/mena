# R-03 Future OS Compatibility Implications

Status: Hold

## Hold Reason

This task depends on the outcome of `R-02 Linux Audio Capture and Routing Strategy`.

The core question is not "what are generic cross-platform audio concerns?"
The real question is "what future cross-platform implications follow from the Linux capture approach we actually choose?"

Running this task before `R-02` would likely produce abstract guidance rather than decision-quality architecture input.

## Dependency

Wait for:

- `R-02 Linux Audio Capture and Routing Strategy`

## Activation Rule

Start this task only after `R-02` produces at least a provisional recommendation for:

- the Linux audio stack assumptions
- the preferred dual-stream capture/routing pattern
- the expected capture adapter boundary

## Intended Future Question

Once activated, this task should answer:

- which parts of the chosen Linux capture approach create future macOS and Windows constraints?
- which elements can remain behind adapters cleanly?
- which parts of the current capture abstraction should be adjusted now to reduce later portability pain?
