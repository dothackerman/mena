# Configuration File

Status: active for PoC implementation

Mena loads an optional TOML configuration file from:

```text
$XDG_CONFIG_HOME/mena/config.toml
```

If `XDG_CONFIG_HOME` is not set, the Linux PoC default is:

```text
$HOME/.config/mena/config.toml
```

Missing config files are not errors. The first schema is intentionally small and contains no secrets.

## Precedence

Settings are resolved in this deterministic order:

```text
built-in defaults < config file < environment overrides < explicit CLI arguments
```

This order resolves defaults before a session fact exists. Once `mena record`
persists a session-level fact, the Session Record becomes authoritative for
that session. Later commands must not silently override persisted session
facts with config-file or environment defaults.

Examples:

- `[summary].language` can prefill the record-time language prompt or provide a
  default for a non-interactive command that is explicitly allowed to use
  defaults.
- The language selected or confirmed during `record` is persisted in the
  Session Record and is the default language for processing and summarization.
- `process` uses the persisted session language for ASR language configuration
  and lexicon or prompt-biasing when the provider supports it.
- `summarize` uses the persisted session language for prompt and output policy
  by default. A deliberate summary-language override creates a distinct derived
  artifact with provenance; it does not rewrite the session language.

## Supported Keys

```toml
[summary]
language = "en"

[ollama]
endpoint = "http://localhost:11434"
model = "llama3.2:3b"
```

Unknown sections or keys are configuration errors. Values must have the expected TOML type.

## Environment Overrides

- `MENA_SUMMARY_LANGUAGE`
- `MENA_OLLAMA_ENDPOINT`
- `MENA_OLLAMA_MODEL`

`MENA_DATA_ROOT` remains a runtime path/session-data override and is not a config-file setting.

## Safety Rules

The Ollama endpoint must be a local HTTP(S) URL and must not contain credentials, query strings, or fragments. Do not put secrets, tokens, or meeting content in `config.toml`.
