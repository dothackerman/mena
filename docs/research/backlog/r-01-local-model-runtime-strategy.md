# R-01 Local Model Runtime Strategy

Status: Open

## Question

- how should local model-backed stages be accessed on user machines?
- is local summarization viable on the first target laptop and on weaker representative tester machines?

## Current Options To Evaluate

- direct library integration
- `Ollama`
- `LM Studio`
- other suitable local runtime approaches

## Why It Matters

- affects installation complexity
- affects runtime operability
- affects portability to weaker tester machines
- affects how local and fallback providers are abstracted behind ports

