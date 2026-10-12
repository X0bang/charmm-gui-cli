# Changelog

## 0.6.1

- Completed builds independently deliver the official archive, extracted model, validation reports, available compilation artifacts and actual prepared inputs under `<creation directory>/<task name>/`.
- Internal state and immutable evidence remain at the original task location; moving or deleting an export does not invalidate reports or recovery. JSON retains internal paths and adds `delivery`.
- Add `jobs export NAME [--to DIR]` for completed legacy runs; runs without a recorded creation directory use the invocation directory.
- Refuse foreign destinations, symlinks, edited exports and stale validation evidence. Unchanged owned exports can be refreshed; interrupted partial exports are preserved and require a new destination.
- Remove successful download staging files and verified matching historical download duplicates. Failed/unrelated responses remain for diagnosis.

## 0.6.0

### Breaking changes

- `build`, `build-resume`, and `jobs resume` run in the background by default. Put `--foreground` before the command for synchronous execution and a final exit code. Background launch exit code 0 means the worker started, not that modeling passed.
- New runs and checks are written under the current working directory (`charmm-gui-runs/` and `charmm-gui-checks/`). Existing task locations and global indexes remain readable without migration.
- Default batch/sweep output also uses `charmm-gui-runs/`; standalone previews use `charmm-gui-previews/`. Batch/sweep orchestration remains synchronous.
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
