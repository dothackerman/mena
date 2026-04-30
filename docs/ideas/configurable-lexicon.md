# Configurable Lexicon

Status: deferred after PoC v0

## What

Allow the user to provide personal or domain-specific word lists that improve transcription accuracy.
This is especially relevant for Swiss German terms, abbreviations, names, project vocabulary, and recurring domain language.

## Where It Comes From

- `docs/requirements/poc-requirements.md` includes configurable lexicon support in `REQ-S01`.
- The architecture review flagged that the lexicon currently lacks an architectural home.
- The customer decided during architecture review follow-up that lexicon can be deferred until after the PoC.

## Current Boundary

The PoC architecture may leave a future Transcription Port input for lexicon support, but implementation should not build lexicon handling unless explicitly promoted.

