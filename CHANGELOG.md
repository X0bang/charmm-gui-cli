# Changelog

## 0.7.3

- Store new task-local metadata paths relative to the task root; resolve historical single-directory absolute paths without rewriting or resealing evidence.
- Discover moved named tasks by UUID, add identity-checked `jobs relink`, and reject ambiguous copies, mismatched identities and active workers.
- Preserve validation and compiled-artifact checks after relocation, including moved preparation sources/children and existing owned exports. Uncertain POSTs are never replayed.

### Validation scope

Regression tests cover moved single-directory tasks, preparation reuse, identity checks and historical records using local fixtures and mocked remote requests. Moving active workers or batch/sweep container directories is not supported. This release does not claim a new live modeling run or completion of all audit remediation items.

## 0.7.2

- Preserve bounded, credential-redacted structured API diagnostics across uncertain submissions, recovery records, logs and terminal summaries. Remote-check failures recommend a concrete local query rather than repeating the failed GET.
- Model-building GET requests use observable, command-scoped exponential backoff for up to 20 minutes; respect Retry-After, preserve short standalone queries and never replay POSTs or bypass authentication/TLS failures.
- Explain stale validation by the specific missing/changed archive, input, model, configuration, lineage or compilation evidence. Require stored evidence to match its own fingerprint; restore a missing owned model from its original archive without overwriting edited models or losing report annotations.
- Restore `doctor` command dispatch and test all registered command handlers.
- Include per-chain terminal overrides in preparation seals; verify two-chain upload and source certification with mocked website/API calls.
- Defer busy task/source locks instead of permanently failing a sweep condition; recover historical lock-only failures and preserve verified preparation reuse.
- Preserve edited `report.txt` and `README.txt` as exclusive `.user-edited` backups when regenerating summaries. Model and input-settings edits remain protected.
- Populate written reports with confirmed source/build IDs and compilation status; round displayed values and remove repeated HTML headings.
- Show terminal job outcomes and H:MM:SS elapsed time; omit outdated remote stages after download/validation. Localize batch/sweep recovery guidance and use the original sweep command.
- Correct C-terminal help, distinguish absent local chains from server-form failures, and use login-specific EOF guidance.

### Validation scope

Regression tests cover local provenance, locking, reports, mocked server diagnostics and simulated network outages. This release does not claim a new live CHARMM-GUI modeling run or completion of all audit remediation items.

## 0.7.1

- Fix single-directory previews and verify extracted model bytes before reusing cached validation or showing a pass. Read historical reports with their original validation version.
- Extract into an owned temporary tree and atomically publish the complete model; interruptions no longer leave an unrecoverable partial `model/`.
- Protect generated reports, README and requested/applied settings against edited-file overwrites. Explicit exports also include root `report.txt` and distinguish requested/applied parameters; legacy filename-like task names remain exportable.
- Preserve safe recognized API validation diagnostics; record execution failures independently of pipeline stages and honor non-retryable recovery guidance.
- Validate modeling options before reserving task directories; roll back failed new-layout local preparation without touching existing tasks or submission intents.
- Add an offline common-lipid catalog with explicit unknown-lipid opt-in, per-chain terminal patches, Gly/Pro residue checks and safe protein serial-overflow repair.
- Show precheck error/review findings and full TTY evidence, clarify unsupported-format actions and implicit hydrogens; allow global options after commands and show help with no arguments.
- Handle malformed release metadata without tracebacks and make release publication retries resume existing private drafts. Correct the usage guide's output paths.

## 0.7.0

### Breaking changes

- New CLI builds use `<invocation directory>/<task name>/` (or the exact `--out` directory), with private state in `.charmm-gui/` and a visible `run.log`. No second task directory is created under `charmm-gui-runs/`.
- The archive and extracted model are authoritative single copies at `charmm-gui.tgz` and `model/`; retain them and `.charmm-gui/` to keep reports verifiable. Completion writes `report.txt`, `README.txt`, and requested/applied input parameters without copying large artifacts.
- Task indexes move to the XDG data directory, separate from sessions; old indexes and task layouts remain readable without migration.
- Updates read published GitHub Releases and their checksums, not the main-branch download directory. Older updaters need a one-time source reinstall. Release archives are built on tag publication and are no longer tracked in the repository.

### Refactoring

- Separate command handlers, pipeline orchestration, remote access, chemistry checks, task storage, and bilingual catalogs into subpackages; preserve old Python imports as compatibility aliases.
- Resolve bare task names through the index before inspecting a same-named local delivery directory. Explicit task-root paths resolve their private working directory.
- Reuse extracted model bytes across validation/compiler upgrades only after comparing them with the original archive. Preserve submission intents, receipts, session formats and no-replay semantics.
- New batch/sweep child tasks use the same single-directory layout; batch containers and legacy advanced API runs remain compatible.

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
