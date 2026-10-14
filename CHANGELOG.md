# Changelog

## Unreleased

- Add read-only, model/TER/chain/segid-separated residue inventories to prechecks and prepared-input manifests. Bilingual summaries show bounded chain composition and COMPND declarations without identifying domains from names or deleting fusion/antibody chains.
- Identify unsupported nucleic-acid residues explicitly, keep nucleic-acid/carbohydrate name hints out of peptide residue/gap counts and avoid suggesting peptide terminal patches for them. Show LINK endpoints and possible glycan annotations before removal advice across single/component input preparation.
- Reject same-named ligand instances and ambiguous component selectors across TER boundaries instead of merging them; report bounded instance labels and explicit selection routes, retaining actual component-configuration arguments. Reinforce ligand formal-charge review without choosing a pH state or changing the defined chemistry.

## 0.8.2

### Improvements

- Diagnose possible embedded duplicate ligand poses by bounded, unfitted, element-preserving heavy-coordinate matching; retain the blocking heterogen decision and chemical-identity caveat, with explicit removal/complex routes and protection against name-wide removal of unmatched copies.
- Validate PDB column boundaries before serial/residue interpretation, retain physical source line numbers after explicit model selection, and report unsupported hybrid-36/column overflow without truncation. A malformed unselected model no longer blocks a valid explicitly selected model; original inspection failures remain recorded.
- Separate task records/process locks/owned files from managed build, worker, delivery and completed-preview orchestration. Preserve historical module entry points while enforcing no upper-layer imports from chemistry, HTTP and storage.
- Extract strict run/configuration and archive-receipt primitives without bypassing legacy validation or creating receipts from read-only consumers; cover real inherited-lock offline workers through new and historical module paths.
- Use canonical subpackage imports throughout implementation and regression mocks; centralize historical import targets without making internal code depend on them.
- Move build/update libraries out of command adapters and isolate progress, summaries and reports in `presentation/`; keep original top-level module identities compatible.
- Share task locking and strict YAML loading below orchestration; delivery no longer imports CLI report helpers. Add fresh-process alias blocking and package-boundary checks.
- Fix `inspect` command dispatch, which referenced a missing import despite successful module/handler smoke tests; cover actual read-only archive/configuration/status/report invocations.

### Validation scope

Local regressions cover bilingual input diagnostics, original-file preservation, explicit model selection, insertion codes, bounded duplicate-pose matching, canonical package boundaries, historical import identities and offline workers. Source and wheel distributions are tested separately. This release does not add hybrid-36/mmCIF import, chemical-identity inference or automatic embedded-ligand removal; no new live provider submission or scientific-equivalence acceptance is claimed.

## 0.8.1

### Compatibility changes

- New unnamed batch/sweep containers are created in the invocation directory with type/config-derived names; `--name` selects a named container and `--out` remains authoritative. Existing batch directories retain their paths.
- CLI batch/sweep dry-runs retain local plans without writing global names/history/latest-run; children are registered only when explicitly advanced. The library's default initialization remains backward compatible.

### Improvements

- Poll on-demand archive compression initially every 20–30 seconds, back off to 120 seconds and honor longer server Retry-After/durable retry times without replaying POSTs.
- Show bounded byte/total progress in foreground, batch queues, jobs watch/status; unknown or content-encoded totals are not guessed. Keep modern-task response/normalization staging private and clear active viewer state on interruptions.
- Document measured local conversion counterexamples and scientific gates without enabling unverified engine-selection parameters or changing the default official conversion route.
- Accept build-style nested version-2 batch options, merge section/chain overrides and retain version-1 manifests and configuration fingerprints.
- Add explicit pre-upload local-failure retries with preserved attempt diagnostics; reject unknown/remote submissions and existing partial/foreign directories.
- Share one stable batch timeline across child polling rounds, deduplicate stage/link records, retain plain queue/child logs, show aggregate counts and preserve completed elapsed time.
- Defer local preparation/naming/registration lock contention and adopt complete prepared inputs when the lock is released, without submitting an incomplete child.
- Add bilingual TTY summaries for batch/sweep/status/preview while retaining pipe/forced-JSON output and concrete interruption resume commands.

### Validation scope

Local regressions cover nested batch configuration, dry-run index isolation, explicit local retries, lock contention, durable GET-only archive scheduling, streamed byte progress and private download staging. Offline conversion research does not enable local force-field conversion in the modeling workflow. No new live provider submission or macOS end-to-end modeling acceptance is claimed by these tests.

## 0.8.0

### Compatibility changes

- Default modeling input preparation now removes recognized crystallographic waters, resolves complete alternate conformations by occupancy and converts MSE to MET, with repair evidence. Use the explicit input options to review or change these policies; original input files are not rewritten.
- Additional pre-upload safety checks can reject structures previously accepted locally, including geometric backbone breaks, unsupported chemistry and excessively close protein–ligand contacts.
- New prepared manifests include additive source/selection/repair evidence. Existing task records and uncertain POST recovery remain readable and are not automatically resealed or resubmitted.

### Improvements

- Add explicit 1-based ligand record selection for multi-record SDF/mol2 with bounded name/score diagnostics; propagate the choice through check, direct/YAML builds, batches and sweeps without automatically selecting the best score.
- Normalize supported V3000/mol2 ligands with chemical-graph/stereo round-trip checks, original source hashes, atom-name mappings and coordinate-roundoff evidence. Keep mol2 partial charges distinct from formal charges and require review; reject unsupported chemical semantics instead of dropping them.
- Preserve declared chirality and depicted 2D double-bond E/Z definitions during conversion and hydrogen addition; copying old wedge/parity codes onto changed atom neighborhoods could previously invert or lose stereochemistry. PDBQT remains unsupported with actionable input guidance.

- Add explicit `login --remember` with allowlisted OS credential backends, a bounded memory-only `CHARMM_GUI_PASSWORD_COMMAND`, local `logout`, JWT lifetime diagnostics and startup expiry warnings.
- Renew API and website sessions together under shared file locks, retain pair receipts and the original credential backend, roll back failed publication, and defer repeated failed renewal. Never replay modeling POSTs; GET-only reconcile does not log in automatically.
- Follow recorded custom Cookie destinations, preserve credential formats/permissions, protect paired sessions from standalone web-login overwrites, and install the optional authentication dependencies with the isolated installer.

- Share crystallographic-water removal, complete residue-level occupancy-ranked altloc selection, MSE-to-MET conversion, explicit heterogen/additive removal and model selection across check, direct/YAML builds, components, batches and sweeps. Preserve original files, source hashes and detailed repairs in prepared manifests.
- Report heterogen identities, metal donor atoms within 2.8 Å and nearest cofactor contacts; create intentionally incomplete retained-component drafts with chemical/charge/topology requirements.
- Block geometric backbone breaks, unknown protein residues, unsupported CGenFF elements, reserved ligand names and protein–ligand contacts below 2.2 Å before upload. Do not bypass LINK coordination/covalent-linkage records.
- Recognize modified amino acids in structural inventories without inventing missing numbering gaps; bound duplicate-serial diagnostics and show compact bilingual repair summaries.

### Validation scope

Local regression tests cover shared input repairs, chemical-format conversion, selected-record uploads, paired renewal concurrency, credential redaction and no-POST-replay recovery using fixtures and mocked service responses. Real OS credential vaults, a twelve-hour official-service session and a new end-to-end modeling/simulation-equivalence run remain unverified; this release does not complete all audit remediation items. PDBQT import, general modified-residue/protonation mapping and specialized metal/covalent workflows remain unsupported.

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
