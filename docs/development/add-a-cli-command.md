# Add a CLI command

The CLI is an interface adapter. A command may parse arguments, prompt, render output, and map
application failures to exit codes; workflow and domain policy belongs in an application module.

## Recipe

1. Add `app/src/mena/cli/<command>.py` with a `register(...)` function and a `run(args,
   context)` handler.
2. Declare the command's parser explicitly in that module. Reuse the session-path or
   runtime-fallback parent parser only when the flag contract is identical.
3. Add the module's `register` function to `COMMAND_REGISTRARS` in `mena.cli`.
4. Delegate user-meaningful work to an application workflow or projection. Keep the complete
   command interaction boundary—prompts, console notices, formatting, and application-error to
   exit-code mapping—in the command module.
5. Add public-entry-point tests for parser/output/exit behavior and workflow tests for policy.
6. If the command was a placeholder, replace its explicit placeholder handler with the real
   command behavior.
7. Run `make check` from `app/`. The import-boundary gate rejects any core module importing
   `mena.cli`.

## Names at the boundary

CLI values are user-facing aliases, not necessarily durable identifiers. For example, capture
profile aliases `online` and `in-person` are parsed by `profiles.parse_capture_profile()` into the
durable values `online_dual_stream` and `in_person_single_mic`. Store the durable value; do not
persist the CLI spelling or reproduce the mapping in a command module.

## Exit codes

- `0`: success (including an explicitly unimplemented placeholder)
- `1`: command, validation, workflow, or interaction error
- `3`: accepted runtime fallback is unavailable or needs one-run authorization
- `4`: session mutation is blocked by lock contention

Preserve an existing command's wording and exit behavior during refactors. A new distinct exit
code is an application contract change and needs an explicit issue/decision, not a local choice.

## Command ownership and shared seams

`mena.cli` is the composition root and exports only `main`. Import `CliDependencies` and
`CommandContext` from `mena.cli.composition`; tests of command-private behavior import the owning
command module directly. Do not add compatibility re-exports to the package facade.

A helper stays in its command module whenever it contains command names, workflow topics,
workflow-specific messages, or command execution. Small duplication is preferable to a generic
helper bucket that obscures ownership.

Only two focused adapter-internal shared seams are permitted:

- `_console.py` owns generic terminal mechanics with no command or workflow knowledge, such as
  numbered-choice input and TTY-aware input selection.
- `_runtime_fallback.py` owns runtime-fallback interaction and runtime-stewardship notices shared
  by process, resume, and summarize.

Before adding to either seam, verify that multiple commands use the helper and that its name and
implementation contain no command-specific concepts.
