# mena

`mena` is a privacy-first local meeting notes project.

The repository root is the product suite and knowledge base, not the Python package root.

- project documentation lives under [docs/](docs/README.md)
- runnable software lives under [app/](app/)
- repository workflow is documented in [docs/repository-workflow.md](docs/repository-workflow.md)

## Supported PoC install path

The supported local PoC path is clone-and-run on Linux with `uv`. From a fresh checkout:

```bash
git clone <repo-url> mena
cd mena/app
uv sync --all-groups
uv run mena --version
make hooks
make check
```

For a local user-level CLI install from the checkout:

```bash
cd mena/app
uv tool install --editable .
mena --version
```

During development, prefer `uv run mena ...` from `app/` so the command uses the checkout environment.

## Optional local provider dependencies

The default package keeps core install requirements minimal. Heavy local provider surfaces are optional:

```bash
cd mena/app
uv sync --extra asr          # faster-whisper ASR adapter dependencies
uv sync --extra diarization  # pyannote diarization adapter dependencies
uv sync --all-extras         # all optional local provider dependencies
```

Expected external tools and services are not silently installed by `mena`:

- Ollama for local summarization and semantic review defaults.
- PipeWire / WirePlumber user-session audio stack for the Linux online-meeting capture path.
- Local audio playback tools such as `ffplay`, `pw-play`, `paplay`, or `aplay` where playback is used.
- ASR and diarization model downloads are setup/preflight work, not surprise recording-time downloads.

Provider adapters degrade with controlled "not installed" or unavailable errors when optional runtimes are absent.

## Runtime data and uninstall semantics

Repository files, installed application files, and user-created data are separate. Normal package uninstall removes application files only; it must not delete sessions or exports.

Linux PoC runtime locations follow the XDG-style path boundary used by `mena.settings`:

- sessions: `$XDG_DATA_HOME/mena/sessions`, or `$HOME/.local/share/mena/sessions` by default
- narrow smoke/test session override: `MENA_DATA_ROOT=<path>` writes sessions under `<path>/sessions`
- config: `$XDG_CONFIG_HOME/mena/config.toml`, or `$HOME/.config/mena/config.toml` by default
- cache/model data: `$XDG_CACHE_HOME/mena/models`, or `$HOME/.cache/mena/models` by default
- logs/state: `$XDG_STATE_HOME/mena/logs`, or `$HOME/.local/state/mena/logs` by default
- exports: `$XDG_DATA_HOME/mena/exports`, or `$HOME/.local/share/mena/exports` by default once export storage is separated from session bundles

To remove the local editable CLI install:

```bash
uv tool uninstall mena
```

Do not delete session, export, config, cache, model, or log directories as part of uninstall unless the user explicitly chooses a separate destructive cleanup.
