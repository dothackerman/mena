# mena

`mena` is a privacy-first local meeting notes project.

The repository root is the product suite and knowledge base, not the Python package root.

- project documentation lives under [docs/](docs/README.md)
- runnable software lives under [app/](app/)
- repository workflow is documented in [docs/repository-workflow.md](docs/repository-workflow.md)

## Linux alpha product install

The Linux CLI alpha product path is a versioned wheel install, not an editable checkout. The current local artifact shape is:

```bash
cd mena/app
uv build --wheel --out-dir dist
uv tool install 'dist/mena-0.1.0-py3-none-any.whl[local-providers]'
mena --version
mena setup --check
mena setup
```

Use `mena setup --check` for a non-mutating preflight. Use `mena setup` for the interactive setup flow, or `mena setup --yes` only when deliberately confirming planned automated actions non-interactively.

Plain `mena ...` means the installed product command. It should be usable without an editable checkout, repo virtualenv activation, test fixtures, or source-tree state.

## Repository/development usage

Development validation remains checkout-scoped:

```bash
git clone <repo-url> mena
cd mena/app
uv sync --all-groups
uv run mena --version
make hooks
make check
```

During development, prefer `uv run mena ...` from `app/` so the command uses the checkout environment. Use explicit scratch paths, such as `MENA_DATA_ROOT` or temporary XDG roots, for dev smoke tests. Run setup preflight through the installed product command, not repo/dev `uv run mena ...`, and do not treat repo/dev command output as proof that the installed product command works.

## Local provider setup and preflight

The first Linux alpha profile is `linux-alpha-local-v1`. It is a local-only setup profile covering transcription, diarization, summarization, and semantic review:

- transcription: `faster-whisper` with the hardware-selected Whisper profile from the current ASR adapter
- diarization: `pyannote/speaker-diarization-community-1`
- summarization: local Ollama using the configured local model, defaulting to `llama3.2:3b`
- semantic review: local Ollama using the configured local model, defaulting to `llama3.2:3b`

`mena setup --check` reports the current executable path, plain `mena` path, version, runtime paths, selected profile, hardware gate, command/package/model readiness, and planned setup actions. It does not install tools, download models, change configuration, or configure remote fallback.

After confirmation, setup may run only explicit local actions such as `ollama pull <model>` and provider model warm-up commands in the installed Python environment. It does not run `sudo`, install system packages silently, accept third-party model terms, embed credentials, or configure remote provider fallback.

Manual actions are reported when Mena cannot safely automate them, including:

- installing or starting Ollama
- installing/enabling PipeWire and WirePlumber user-session tools
- installing a local playback command such as `ffplay`, `pw-play`, `paplay`, or `aplay`
- reinstalling the wheel with `[local-providers]` if `faster-whisper` or `pyannote.audio` is missing
- accepting pyannote community model terms and exposing `HF_TOKEN` or `HUGGING_FACE_HUB_TOKEN` before model preparation

If hardware cannot be detected confidently or cannot satisfy the fixed local profile, setup stops before provider dependency/model setup and reports local provider readiness as incomplete.

## Optional local provider dependencies

The alpha product install should include the local provider extra:

```bash
uv tool install 'dist/mena-0.1.0-py3-none-any.whl[local-providers]'
```

For repo/dev environments only, optional provider surfaces can also be synced directly:

```bash
cd mena/app
uv sync --extra asr          # faster-whisper ASR adapter dependencies
uv sync --extra diarization  # pyannote diarization adapter dependencies
uv sync --all-extras         # all optional local provider dependencies
```

Expected external tools and services are not silently installed by `mena`:

- Ollama for local summarization and semantic review defaults.
- PipeWire / WirePlumber user-session audio stack for the Linux online-meeting capture path.
- Local audio playback tools where playback is used.
- ASR and diarization model preparation is setup/preflight work, not surprise recording-time setup.

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

To remove the alpha product CLI install:

```bash
uv tool uninstall mena
```

Uninstall is not cleanup. Do not delete session, export, config, cache, model, or log directories as part of uninstall unless the user explicitly chooses a separate destructive cleanup.
