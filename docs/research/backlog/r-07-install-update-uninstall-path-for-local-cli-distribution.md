# R-07 Install / Update / Uninstall Path for Local CLI Distribution

Status: Hold

## Hold Reason

This task depends materially on upstream research outcomes.

A useful install/update/uninstall recommendation requires at least provisional clarity on:

- local model runtime strategy
- Linux capture and audio stack assumptions
- ASR runtime burden
- diarization runtime burden

Running this task too early would likely produce generic packaging advice detached from the actual runtime and dependency shape of the product.

## Dependencies

Wait for at least provisional results from:

- `R-01 Local Model Runtime Strategy`
- `R-02 Linux Audio Capture and Routing Strategy`
- `R-04 ASR Provider Strategy`
- `R-05 Diarization Provider Strategy`

## Activation Rule

Start this task only after the upstream tasks have narrowed:

- runtime family choices
- native dependency expectations
- model asset and hardware expectations
- Linux packaging constraints that materially affect install and uninstall behavior

## Intended Future Question

Once activated, this task should answer:

- what should the local install, upgrade, and uninstall path look like for a Linux CLI product used on the same machine as development and testing?
- how should repo checkout, installed application, user data, caches, models, and logs be separated?
- what support burden and failure modes follow from the chosen runtime and audio stack recommendations?
