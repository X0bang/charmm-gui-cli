# Changelog

## 0.6.0

### Breaking changes

- `build`, `build-resume`, and `jobs resume` run in the background by default. Put `--foreground` before the command for synchronous execution and a final exit code. Background launch exit code 0 means the worker started, not that modeling passed.
- New runs and checks are written under the current working directory (`charmm-gui-runs/` and `charmm-gui-checks/`). Existing task locations and global indexes remain readable without migration.
- Task names ending in structure/configuration file extensions are rejected. Error `next_step` is always populated; previously empty/null values now contain an action.

### Improvements

- Safe PDB normalization: duplicate protein serials, inferable missing elements, missing chain IDs; original files are preserved and repairs are recorded.
- Shared `GLYP`/`PROP` terminal patch support and server-option fallback for `NTER`.
- Website POST read timeout defaults to 900 seconds with 30-second wait heartbeats; GET timeouts and no-replay semantics are unchanged.
- Private detached workers, process locks, authoritative logs, and `jobs watch/list/status`; optional macOS sleep inhibition and completion notifications.
- Deduplicated timestamped progress, TTY spinner, concise localized check summaries, relative output paths, and `check --open`.
- Safe, bounded server diagnostics with snapshot and official job links; precise configuration path errors.
- GET-only `jobs reconcile`, including explicit confirmation and preserved retired intents. Lost upload IDs require manual association.

### Validation scope

Transport tests use fixtures and mocks. Real macOS sleep/notification behavior and a new live CHARMM-GUI end-to-end modeling run are not claimed by these tests.
