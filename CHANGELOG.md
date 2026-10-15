# Changelog

## Unreleased

- Add an offline, bounded full-precision CMAP comparison gate with ordered-cell, malformed-input and mutation regressions; it does not enable local force-field conversion. Document the second-system ordering counterexample, corrected atomic/compiled-parameter checks, limited-template restrained single-point results and remaining provider/production gates.
- Consolidate historical installation/testing records here and describe only current formats, task recovery, platform coverage and remaining live/scientific gates in the guides.

## 0.8.7

### Fixes and improvements

- Add read-only execution diagnostics for legacy and current tasks, separating an active local lease from old provider stages. Classify explicit remote failures correctly, distinguish failed requests from model failures, expose uncertain/unreadable submissions without rewriting intents and keep all historical JSON state/category fields intact.
- Label unqueried remote stages as last recorded, avoid treating missing workers or incomplete validation records as running/passed, preserve current versus timestamped prior-worker errors and distinguish time since a stale start from actual duration. New diagnostic fields and messages are bilingual with concrete task commands.
- Localize common task-index/name/registration errors while retaining legacy exception strings; shell-quote unnamed recovery paths and classify newly saved remote_failure records as failed_remote.
- Add explicit index-only `jobs forget --missing` plans for uniquely identified unavailable directories, retaining hashed backups and refusing existing paths or ambiguous records. Missing-directory archival does not establish process or remote completion.
- Add recoverable `jobs clean` / `jobs restore` with read-only default plans, whole-task non-replacing moves, retained index backups and pre-move identity receipts. No permanent deletion or copy/delete fallback is used; quarantine does not reclaim disk space.
- Protect active/uncertain tasks, preparation dependents and unfinished batch members. Completed child rows and source certificates resolve retained locations by UUID and physical directory identity without changing proof bytes or repeating submissions. Restore conflicts support an explicit new path without name registration.
- Allow explicitly re-registering an unindexed named local plan from its existing consistent identity, without modeling or bypassing damaged matching index backups. Clarify current paired renewal, adaptive archive polling and macOS installation documentation.

### Validation scope

English and Chinese regressions cover execution diagnostics, submission-evidence preservation, recoverable task quarantine/restoration and preparation reuse with tamper refusal. Source and wheel distributions are checked separately. Quarantine retains the task data and does not reclaim disk space; uncertain submissions are not retried. Local force-field conversion, production simulation and completion of ongoing scan acceptance are not claimed by this release.

## 0.8.6

### Fixes and improvements

- Add explicit local jobs forget plans and --yes index archival without deleting task data or touching remote jobs. Retain hashed index backups for identity-checked jobs relink restoration; refuse active or uncertain submissions and coordinate concurrent index writers.
- Place automatic single-directory task PNG previews under visible previews/ and reference the verified model in place instead of copying another large PDB. Preserve old task/cache and explicit standalone-copy behavior; reports still verify archive, coordinate and image hashes.
- Distinguish remote pending queue states from unprepared batch/sweep rows. Submitted children retain their active slot and existing inputs; matching durable job records recover the specific historical staging misclassification without retrying uploads or uncertain POSTs.
- Publish the detached worker PID under an inherited startup gate before returning, preventing launcher/worker PID mismatches and late parent writes over fast-worker completion records. Preserve inherited locks and historical worker entry points.
- Add bounded best-effort Linux desktop notifications via optional notify-send, keeping macOS notifications and headless operation independent of modeling results.
- Show the user task root instead of .charmm-gui in terminal build/dry-run summaries, correct singular residue wording and clarify report-edit backups versus protected model/configuration/export files.

### Validation scope

English and Chinese local regression suites cover queue recovery, detached-worker startup, visible previews, notifications and recoverable index archival. Source and wheel distributions are checked separately. Repeated live observation confirmed that queued scan children retain their original job IDs and preparation reuse without repeated uploads; final modeling, archive acceptance and GROMACS compilation of these ongoing runs remain pending. Index archival does not remove model files or reclaim their disk space, and directory quarantine is not included in this release.

## 0.8.5

### Fixes and improvements

- Accept literal PDB Reader state catalogues terminated by JavaScript ASI before a new declaration or at script end, as observed in the live form. Retain JSON-only decoding and reject expression continuations or duplicate catalogues; no website JavaScript is executed and state proof gates remain unchanged.

- Verify internal mapped-SMILES stereo on map-neutral copies, preserving index correspondence and explicit E/Z representation. Prevent transport atom labels from changing R/S or E/Z comparisons while still rejecting true inversions, invalid mappings and map-only pseudo-stereo; retain actual molecule metadata/coordinates unchanged.

- Shorten default help to common workflows and add --help-all for complete advanced/compatibility command discovery. Document polling units/budgets, queue foreground behavior and correct N/C terminal examples; keep legacy wait flags and namespaces compatible.
- Diagnose recognizable option typos and invalid option values with command-specific help routes, without printing argv values or raw argparse messages. Cover flag-shaped credential values, inline values, abbreviations and literal -- separators.

- Localize shared YAML/schema/numeric/lipid checks, missing input routes and task metadata diagnostics while retaining legacy machine-readable messages and recovery categories. Show parser line/column locations without configuration values; bind configuration errors to actual files and local recheck commands.
- Report filesystem targets and permission/read-only/space causes without exception bodies, including saved local check/build failures. Distinguish foreign occupied paths from resumable tasks, preserve existing outputs and report C-terminal patch choices without suggesting N-terminal values.
- Add public-command privacy/actionability regressions and refresh the canonical-path localization backlog; remaining chemistry, batch/sweep and detailed validation messages are not yet fully migrated.

### Validation scope

Local English and Chinese regressions cover these fixes, including strict literal-catalogue boundaries and map-neutral stereochemistry checks. Source and wheel distributions are checked separately. The catalogue compatibility fix was confirmed against a saved live form, but fresh end-to-end provider acceptance of converted ligands and explicit protein states remains pending. No change to submission recovery, automatic POST retry, docking or force-field conversion is introduced.

## 0.8.4

### Improvements

- Add locale-independent semantic parameter summaries alongside unchanged raw requested/applied JSON. Show per-chain requested/submitted terminal patches, Gly/Pro fallback changes, lipid fractions and API-recorded settings in terminal, text/HTML reports and deliveries.
- Distinguish response records, uncertain submissions, copied historical values, adjustment-only and missing/conflicting records; reject foreign job IDs or plan/config disagreements as confirmed settings without changing submission/recovery behavior. Bound chain rendering while retaining complete machine-readable rows.

- Restore ligand PDBQT using verified Meeko SMILES/index/H-parent metadata, user-supplied serial-mapped SMILES or a unique complete CONECT correspondence. Preserve every original coordinate atom, separate partial from formal charges and support explicit MODEL selection across CLI/YAML/batch/sweep without ranking poses or guessing bonds by distance.
- Carry explicit mapped chemical/stereo declarations through 3D SDF staging and hydrogen addition; verify chiral/E/Z state, graph, charge, valence hydrogens and coordinate round-trips. Standard chemical installations include the tested Meeko 0.8/gemmi/scipy dependencies. No new provider submission or docking is implied by local conversion support.

- Submit explicit protein-state and disulfide requests through validated, literal-catalogue PDB Reader controls, preserving established nonconflicting default rows and recording the exact repeated form pairs sent.
- Verify actual same-job source PDB/PSF atom identities, CHARMM36 state-distinguishing types/charges, proton attachment and explicit disulfide bonds before membrane submission. Bind proof receipts to sealed inputs, saved POST evidence and source reuse; interrupted proof GETs resume without repeating POSTs.
- Require the requested states in both PDB Reader and final-assembly archive files before accepting a model; retain verification across task moves and completed-source-independent child validation. Show verified state counts in summaries and reports.

### Validation scope

Local and mocked-transport regressions cover explicit protein-state evidence, interrupted GET recovery, PDBQT chemistry/stereo preservation, parameter-record provenance and legacy JSON compatibility. Source and wheel distributions are tested separately. Fresh live provider acceptance of the new protein-state and converted-ligand paths remains pending; this release does not claim completion of the audit, automatic pH selection, docking or force-field-conversion equivalence.

## 0.8.3

### Improvements

- Record explicit Amber/CHARMM residue-state intent separately from uploaded base residues: histidine names map to HSD/HSE/HSP; ASH/GLH/LYN retain ASPP/GLUP/LSN patch requests instead of using patch names as RESI templates. Preserve coordinates, document unambiguous LYN hydrogen renaming and identify only unique explicit-CYX disulfide candidates.
- Include residue-state recipes in preparation identities and add literal-JSON/validated-control form adapters without executing website JavaScript. Remote submission/acceptance of these newly recorded state recipes remains temporarily gated until actual PDB/PSF verification is integrated; this development checkpoint is not complete remote state support.
- Add read-only, model/TER/chain/segid-separated residue inventories to prechecks and prepared-input manifests. Bilingual summaries show bounded chain composition and COMPND declarations without identifying domains from names or deleting fusion/antibody chains.
- Identify unsupported nucleic-acid residues explicitly, keep nucleic-acid/carbohydrate name hints out of peptide residue/gap counts and avoid suggesting peptide terminal patches for them. Show LINK endpoints and possible glycan annotations before removal advice across single/component input preparation.
- Reject same-named ligand instances and ambiguous component selectors across TER boundaries instead of merging them; report bounded instance labels and explicit selection routes, retaining actual component-configuration arguments. Reinforce ligand formal-charge review without choosing a pH state or changing the defined chemistry.

### Validation scope

Local regressions cover chain inventories, linked-component diagnostics, explicit residue-state normalization, preserved input coordinates and originals, preparation identity separation, literal form adapters and refusal of unverified remote state application. Source and wheel packages are validated separately. These state records support local review and dry-runs only: remote modeling of inputs with an explicit state recipe is refused pending actual PDB/PSF verification. This release does not claim new live provider acceptance, automatic pH selection or completion of all audit remediation items.

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

## Historical validation records retained from installation and testing guides

The following records describe earlier checkpoints, not current feature support or current verification status. Current test procedures and remaining gates are in [docs/TESTING.md](docs/TESTING.md).

### Archived installation verification records

2026-10-11 的 0.5.1 发行代码已在宿主全新隔离依赖环境运行 546 项全部通过；Ubuntu 22.04 / Python 3.10.12 从公开源包以非 root 用户全新安装，运行 546 项，521 通过、25 因未分发私有结构等条件明确跳过。跨目录帮助、版本及 doctor 通过。源码白名单 88 文件、wheel 31 模块、RECORD 哈希和 GPL 元数据核对完成。真实双浓度扫描、多组分最终验收及 GROMACS 编译在宿主完成；容器无账户和 GROMACS，不把安装检查当作远端建模实测。

2026-10-11 的 0.5.1 压缩恢复与 TER 边界修复候选，已在 Ubuntu 22.04 / Python 3.10.12 中从源码以非 root 用户全新隔离安装，doctor 通过；公开候选源包及新安装依赖环境各运行 529 项测试，504 通过、25 因未分发私有结构等条件明确跳过。宿主 Python 3.13 的新隔离安装环境运行 529 项全部通过。旧安装和失败源任务均保留。官网网络恢复后，原真实双浓度扫描也已通过最终模型及 GROMACS 验收；这与无凭据容器的安装测试是不同证据。

2026-10-11 的 0.5.0 扩展版已重新在新建 Ubuntu 22.04 / Python 3.10.12 容器中以非 root 用户一键安装，RDKit 2026.9.1、NumPy 2.2.6、Pillow 12.3.0 及基础依赖成功安装；doctor 和报告帮助通过。公开源包全套 512 项测试，487 通过、25 明确跳过，退出码 0。宿主 Ubuntu 22.04.5 的 Python 3.13.13 新隔离安装也成功，512 项全部通过。用于下载依赖的可达镜像并不证明 CHARMM-GUI 连接可用；本轮官网/API 的 TLS 连接失败、真实扫描和多配体验收尚未完成。

2026-10-02 已在本地 `nvidia/cuda:12.0.0-base-ubuntu22.04` 容器中验证：容器报告 Ubuntu 22.04.3 LTS；先验证缺少 Python 的明确提示，再仅在容器内安装 Python 3.10.12/venv，然后以非 root 用户运行发布源包中的 `bash install.sh`。安装到该用户的默认 `~/.local`，RDKit 2026.3.6 和其他依赖安装成功；`--version`、`--help`、`doctor` 通过。再次安装也通过，命令链接切换到新目录，原版本目录完整保留。此容器没有 GROMACS，doctor 正确将其报告为可选项。

同一 Ubuntu 22.04 / Python 3.10 容器对不含用户测试结构的 0.4.0 代码源码包执行 `python -m unittest discover -s tests -q`：325 项测试，300 项通过、25 项按缺少私有结构等条件跳过，退出码 0。宿主完整 325 项全部通过，其中包含发布/安装器测试（wheel RECORD 哈希、源码白名单、pip 离线装本地 wheel、拒绝未知入口覆盖等）。

0.4.0 以非 root 用户重新运行一键安装，`doctor`、全局帮助和批量帮助通过；未配置登录凭据时，从其他工作目录执行两项合成蛋白/SDF 的 `batch --dry-run`，结果均为 `inputs_ready`，生成 JSON/CSV 汇总。旧 0.3.0 安装保留。宿主已升级到 0.4.0，并从其他目录无 token 恢复两个真实归档的批次，返回两项 `validated` 和 GROMACS 编译通过。

0.5.0 在同一 Ubuntu 22.04 / Python 3.10 容器完成公开源码包全套 485 项（460 通过、25 明确跳过）及非 root 一键升级。独立 `check`/PNG 与 `sweep --dry-run` 合成输入用例无需认证；doctor 验证 NumPy/Pillow/RDKit，扫描帮助通过。宿主全套 485 项通过，并检查源码白名单、wheel RECORD/许可证和安装后的 30 个模块内容一致。此前安装、候选包和失败产物不删除。

这些结果验证 Ubuntu 22.04 用户级安装、CLI/依赖检查和离线自动化测试，不是容器内远端建模或 GROMACS 验收；科学建模结果见项目验收记录。发布后的新增改动应重新执行相同检查。

### Archived testing and scientific acceptance records

#### 完整性与验收记录

本页区分代码/安装验收、真实科学产物验收和批量调度测试；不以模拟测试替代实际建模结果。

##### 当前单目录修复的验收范围

`test_audit_fixes`、`test_single_directory`、`test_delivery` 覆盖新布局预览、模型字节变化使缓存失效、解压中断重试、历史报告读取、保护用户编辑、旧任务导出、本地失败同名重试、提交证据禁止清理、失败状态与恢复提示、全局参数位置、按链末端与别名冲突，以及旧任务使用目录之外脂质时的恢复兼容。

发布工作流另以离线 `gh` 替身核对不存在 Release、已有草稿、已正式发布三条分支：补齐草稿，不覆盖正式资产。更新器和打包测试检查类型异常与新增子包包含情况。

边界：脂质目录仅覆盖一部分常用名称，不是完整官网名单。API 只透出已识别的安全诊断，其余错误不显示原始正文。mmCIF、V3000/MOL2 仍需先转换；altloc、金属/辅因子保留需要用户明确选择，未新增自动处理。传统非隐藏目录的 Python 初始化入口保留失败内容；新 CLI 布局仅撤销本次本地准备，不清理已有任务或远端提交证据。这轮未提交官网建模，也未在真实 macOS 或 GitHub 验证发布。

##### 0.6.1 完成交付与旧任务导出

分支 `feat/task-delivery`。新增完成后的独立交付与 `jobs export NAME [--to DIR]`，不迁移或修改内部归档、验收报告、提交意图和任务索引。新任务记录创建目录；旧任务的首次显式导出使用调用目录。

完整 653 项在英文/Python 3.13 和中文/Python 3.10.12 下分别全部通过。新增 21 项回归用例，并在既有成功下载用例中增加临时文件清理断言；中英文帮助与文案占位符一致性检查通过。

`test_delivery` 使用真实小型 tgz、真实归档来源绑定及哈希证据；化学验收通过记录是测试 fixture，不是新的科学建模验收。覆盖创建目录持久化、换目录恢复后自动交付、旧任务导出、精确 --to、全部产物、独立 inode、外来目录与链接拒绝、重复导出、保护用户修改、移动交付目录后报告仍可用、内部模型与归档一致性、过期/未完成/活动任务拒绝、报告更新和部分导出保留，以及失败验收仍明确标为失败。

真实旧任务 `mdfa-components-ter-051` 的 0.5.1 成功验收产物已使用新 `jobs export` 导出到隔离的临时交付目录，原验收版本保留。归档、模型、输入和可用编译产物均独立复制，匹配的历史下载临时副本被清理，68 字节失败响应和空的失败 staging 保留；原归档和验收报告不删除。本次是旧产物的交付验证，不是新的远端建模或科学验收。

旧报告允许仅客户端版本不同；原指纹、归档/输入/参数/编译证据仍须匹配。此规则只用于交付，不更改 `jobs report` 原有的当前版本验收缓存判定。

下载测试覆盖成功清理 `.download-*`/`.normalized-*`、精确压缩长度尾部、旧临时副本匹配清理、错误响应和其他文件保留；不做全目录递归删除。JSON 保留内部路径和已有字段，仅添加 `delivery`；完成终端摘要隐藏内部文件路径。

限制：导出需要额外磁盘空间，内部证据与交付模型各保留一份；已有被用户挪走的内部证据仍需先恢复，不能凭空重建。部分导出保留用于诊断，重试须使用新 `--to`。此次未新建远端 CHARMM-GUI 任务，也未在真实 macOS 完成建模验收。

##### 0.6.0 输入与后台任务验收

开发分支：`feat/robust-inputs-and-background-runs`。宿主 Linux/Python 3.13；传输层使用 mock，不把本次软件回归当成新一次远端科学建模验收。完整 632 项通过（英文/Python 3.13、中文/Python 3.10.12），包括平台预览打开器、恢复参数前检和终端 JSON 兼容测试。此前中文、英文环境的 631 项均通过。Python 3.10 依赖安装在临时目录；三个安装器失败路径测试显式模拟 ensurepip 可用以隔离环境前提，缺失 ensurepip 的专门测试仍保留。

隔离安装器在新的临时安装前缀真实安装 0.6.0 及化学依赖，跨目录 `--version`、中文 `jobs watch -h`、`doctor` 均通过。未改动主安装、会话或已有模型。源码包与 wheel 由公开文件白名单生成；真实 Ubuntu 22.04 安装仍需 python3-venv。

| 要求 | 实现与验收 |
| --- | --- |
| 1.1 重复序号 | 自动重编号蛋白，保留原文件；歧义 CONECT 或重复非蛋白序号拒绝，显示编号。 |
| 1.2 元素列 | ATOM 名称推断及指定单原子离子/金属别名；未知元素给出行号和原子名。 |
| 1.3 空链 ID | segid 末字符映射，冲突顺序分配，没有 segid 时 A；写入准备报告与 check.json。 |
| 1.4 末端补丁 | 共用 GLYP/PROP 白名单；NTER 保留或按官网选项回退；异常列出链、末端、请求和选项；调整保存在 manifest 和最终报告。 |
| 1.5 无 HETATM | check 终端提示检查是否需要金属/辅因子。 |
| 2.1 POST | 默认 900 秒可配置，GET 90 秒不变；等待心跳不重发 POST。 |
| 2.2 后台 | 本地准备/登录检查后 detached Popen；锁由子进程继承，前台最终退出码；macOS 命令模拟测试。 |
| 2.3 查看 | watch/list/status；watch 中断不向工作进程发信号，状态显示运行时间与最近五条阶段记录。 |
| 2.4 核对 | 已接收/需确认/未知三种页面；所有请求均 GET；退休意图归档而非删除。 |
| 3.1 服务端错误 | 仅安全诊断节点或错误行，去除脚本/控件并脱敏，截断 300 字；带快照和官网链接。 |
| 3.2 配置路径 | 不存在/无权限/目录分别报错，包含路径与当前目录；缺失给出模板 curl 命令。 |
| 3.3 名称 | 拒绝路径和 .pdb/.sdf/.yaml/.yml/.mol2/.cif，提供任务名示例。 |
| 3.4 下一步 | next_step 不再为空；无专门提示的旧错误回落到命令帮助。 |
| 4.1 摘要 | check 双语终端摘要、修复记录、配体化学信息与预览；非 TTY/--json 保留序列化。 |
| 4.2 日志 | 非 TTY 重复轮询只记录一次阶段；TTY 刷新不写入日志，无 CR/ANSI；五分钟轮询心跳。 |
| 4.3 文案 | 终端不再反复显示科学免责声明；科学字段与历史 JSON 展示值保留。 |
| 4.4 输出目录 | 新任务、批次、扫描、预检与独立预览在当前目录，可 --out 覆盖；旧目录和索引继续可读；--open 平台打开器最佳努力执行。 |

专项测试：`test_robust_inputs`、`test_post_heartbeat`、`test_background`、`test_run_logging`、`test_output_locations`、`test_reconcile`，以及扩展的 `test_precheck`。原同步建模测试显式使用 `--foreground`；旧 JSON next_step 为 null/空的断言按本次要求更新为非空操作。原科学正确性字段与 JSON 字段名称不变，新状态信息只添加字段。

调整与边界：

- 为避免 PID 重用误判，使用操作系统 flock 的存活状态，而非只检查 PID。陈旧锁复用，不执行删除。
- upload 响应未保存任何 ID 时不能可靠自动识别源任务，需要 `--source-jobid` 人工关联；无法判断不允许清理意图。
- GET 返回原表单不证明服务器永远未接收 POST。只有页面符合原步骤且用户显式确认，才归档意图，后续 resume 仍由用户发起。
- 元素推断不是金属配位/辅因子参数化；原有受支持组分和化学定义限制保持不变。
- 启动前检查 API JWT 的本地格式/有效期，并以官网上传表单 GET 检查网页登录。全新任务尚无 job ID 时，没有调用虚构的 API 用户查询接口；不能据此保证未过期 JWT 未被服务端撤销。
- 本轮未在真实 macOS 或独立 Ubuntu 22.04 主机完成端到端验收，也未重新创建 CHARMM-GUI 远端任务或完成新的 GROMACS 科学体系验证。平台调用、状态页和 POST 心跳采用 mock；真实子进程验证继承锁在父进程关闭后仍有效。

运行全套：`python -m unittest discover -s tests -q`。安装包使用公开文件白名单，不包括账户、会话、模型、测试私有结构或本地工作记录。

##### 0.5.2 发布说明

双语输出与更新功能合并至主分支，0.5.2 源码包、wheel 与校验和同次发布到 download；旧版安装包保留。以下记录区分开发专项测试、候选包隔离升级与既有真实建模验收，不将软件更新验收视为重新完成远端建模。

##### 0.5.2 更新命令开发验收

`update --check` 只检查公开仓库；`update` 固定同一 Git 提交的版本、源码包与 SHA256SUMS，验证并调用隔离安装器。新增 26 项测试覆盖数字版本比较、未发布/重复校验和、网络及响应正文脱敏、包大小、路径穿越/链接/重复成员拒绝、安装位置识别、并发锁、安装失败和健康检查失败保留入口、中断提示、双语摘要和 JSON 兼容。中英文环境完整 597 项分别通过。系统 Python 3.10 的更新及双语专项 50 项通过，不代表该缺依赖环境全套通过。

隔离目录中实际从公开仓库下载/校验/安装 0.5.1，再通过模拟发布传输层升级到本地构建的 0.5.2 源包；依赖安装、健康检查和入口切换均真实执行，旧入口文件仍存在，新命令版本及 doctor 通过，跨目录公开仓库版本检查通过。第二次升级使用候选包的模拟传输，不是已公开发布 0.5.2 的证据。未改动主安装、账户会话、已有任务或模型，未调用 CHARMM-GUI 建模接口；更新及测试产物保留。

##### 双语输出开发分支

`feat/i18n-output`：新增环境字符串语言检测、双语高频错误及恢复指导、登录 404 凭据错误识别、TTY 摘要和强制 JSON、双语帮助/终端进度/报告标签。宿主 Python 3.13 完整 571 项在中文与英文环境下分别全部通过；原有测试继续覆盖提交防重、会话文件和模型验收。新增测试核验 JSON 序列化逐字兼容、文案占位符一致、语言优先级、凭据与响应正文不回显。只读查询真实已有任务核验终端摘要，未登录、上传或创建新任务。

系统 Python 3.10 新增的 25 项语言/错误/输出/帮助测试通过；该解释器缺少 NumPy、RDKit、pip 和 ensurepip，尝试全套测试未通过，不计入完整平台验收。没有因此修改系统依赖或声称 Python 3.10 全套通过。既有发布安装包不变，本节记录开发分支代码而不是新发行包。

##### 0.5.1 真实接口恢复复测

2026-10-11 网络恢复后，重新登录并恢复原扫描任务，没有重复上传或新建扫描替代任务。两个已有任务先报告 `done`、归档未生成；第一次下载触发官网按需压缩，返回等待提示并进入 `compressing`。新增精确提示识别、持久化 120 秒下载重试时间、批量活动名额保留以及中文恢复指导。未知文本、HTML、损坏 gzip 仍拒绝；只重试原任务下载 GET，不重发建模 POST。实际压缩超过官网提示的 1–2 分钟，两个归档随后成功下载，并用 0.5.1 对原任务重新验收：批次 `complete`、2/2 `validated`、0 失败，GROMACS 2024.4 `grompp -maxwarn 0` 均通过。

真实扫描只有一个 Web 前处理源、两个独立 API clone；第二项没有 preparation 上传目录，具有核验过的前处理复用证书。各下载收据绑定各自任务 ID、官方归档根与 SHA256；两项均有服务端盐浓度直接证据，不由离子数反推浓度。

| 检查 | KCl 0.10 M | KCl 0.15 M |
| --- | --- | --- |
| PDB/GRO/PSF 原子数 | 367359 | 367353 |
| 上叶 / 下叶 POPC | 479 / 463 | 479 / 463 |
| 水分子 | 78576 | 78526 |
| POT / CLA | 144 / 145 | 216 / 217 |
| 配体重原子 RMSD / 最大位移 Å | 0.246094 / 0.808987 | 0.215222 / 0.528388 |
| bent-improper 警告 | 437 | 424 |

两项 CGenFF parameter / charge penalties 为 1.400 / 1.442，均生成 3 张 1400×1008 的证据绑定 PNG，并导出离线 HTML 报告。均保留最小化达到步数上限与 CHARMM warning level 1 的审查项；`review_required=true`，未执行本地 MD，不宣称充分收敛或科学正确。真实任务编号、模型及私人输入不公开。

压缩与 TER 修复候选回归：宿主 529 项全部通过；Ubuntu 22.04 / Python 3.10.12 候选源包运行 529 项，504 通过、25 因私有结构未分发等条件明确跳过。此前压缩候选的新隔离安装环境运行 523 项，498 通过、25 明确跳过，非 root 一键安装、版本与 doctor 通过。最终发行包验收见下文；候选记录不代替最终包验收。

真实公开 4ZP0 输入采用 DXC A500 底物及完整 LDA A501 去垢剂，不称双药理配体。以 CCD 核验键级、逐原子电荷和 DXC 全部 10 个立体中心，补氢后 44 个组分重原子及全部蛋白重原子坐标零移动。明确排除另一个不完整 LDA 副本和晶体水，保留原工程构建体及六处侧链缺失作为审查项。本地预检通过但要求审阅；首次真实上传因输出缺少 TER 边界，官网将同链号非共价组分归为蛋白工程残基，程序在找不到所需化学字段时安全停止。旧源及响应保留，不伪造字段或声称远端多组分通过。

修复后各非共价组分前添加 TER，保留蛋白内部断链、chain/segment 边界、SSBOND 和 CONECT；不根据残基编号缺口猜断链。实际全部 3074 条原子记录和两份 SDF 与旧准备输出逐字节一致，仅补边界。显式创建修正后的新任务，完成两个组分的真实 CGenFF 参数化并提交脂膜构建。已在确认仅轮询 GET 的阶段执行 Ctrl-C，退出码 130、父任务和 bilayer 均持久化 interrupted，随后同名恢复；源/建膜 ID 与既有提交意图哈希未变。

最终多组分归档完成自动验收及 GROMACS 2024.4 `grompp -maxwarn 0`，状态 `validated`。PDB/GRO/PSF 共 112229 原子；283 POPC（上叶 145 / 下叶 138）、22657 水、40 POT / 46 CLA，KCl 0.10 M 有直接构建证据。DXC 与 LDA 各一份，重原子图和元素完整；共同 392 个蛋白 CA 拟合后，DXC RMSD / 最大位移 0.207112 / 0.501979 Å，LDA 0.190000 / 0.470476 Å。CGenFF parameter / charge penalties 分别为 0.600 / 2.204 和 0 / 0。

官网改写 LDA 原子名，首次严格验收安全失败。新版仅凭同一可信归档的 step1 原坐标、PSF 元素和唯一完整对应恢复命名映射，再核验最终图与位姿；未修改输入、最终坐标或阈值。旧失败报告保留，重验未上传或重新提交。独立化学审查核验 DXC 十个立体中心、两组拓扑净电荷及最终 PDB/GRO 舍入一致性。生成三张真实坐标 PNG；205 条 bent-improper、最小化步数上限和 warning level 1 仍要求人工复核，没有执行本地 MD。

0.5.1 发行代码全套：宿主原开发环境和新隔离安装环境各 546 项全部通过；Ubuntu 22.04 / Python 3.10.12 非 root 新安装环境运行公开源包 546 项，521 通过、25 明确跳过。版本、跨目录帮助、doctor 通过。公开源包 88 个白名单文件、wheel 31 个模块与发布源码一致，RECORD 哈希和 GPL-3.0-only 元数据核验通过；无凭据、私人结构、模型或内部记录。多组分离线 HTML 独立审计通过：三张嵌入 PNG、当前验收指纹、计数、逐组位姿、参数评分与全部警告一致，无脚本或外部资源。

##### 0.5.0 新流程验收

2026-10-11 扩展回归：宿主全套 512 项全部通过，新增中文恢复引导、TLS/代理暂停、Ctrl-C 提交防重、空间邻域预检，以及终端/单文件 HTML 报告。真实 CrtW 输入新增空间检查：最近蛋白重原子距离 2.752673 Å，4 Å 内接触 25 个蛋白残基，低于 2.0 Å 的跨蛋白/配体重原子对为 0；这是启发式辅助指标，不是完整物理冲突或膜取向认证。旧真实归档生成离线 HTML，保留原模型和警告。

同日新建 Ubuntu 22.04 / Python 3.10.12 容器，以非 root 用户从公开源包一键安装，并运行全套 512 项：487 通过，25 因私有结构未分发等条件明确跳过，退出码 0。doctor、报告帮助通过；容器无账户、无 GROMACS，不以此替代远端科学建模实测。宿主使用新安装和更新的 RDKit/NumPy 再运行 512 项全部通过；检查公开包 85 个源码文件、31 个包模块与工作区一致、wheel RECORD/GPL 元数据及隐私白名单。

真实双盐浓度扫描已建立一个前处理源、两个独立建膜任务，并核对第二项没有重复上传。2026-10-11 无存活监控进程，旧本地状态不代表当前远端状态；会话过期后重新登录受到本机 HTTPS/TLS 握手中断阻碍，尚未取得两个最终归档，不能宣称扫描端到端通过。公开双有机配体膜受体候选 4MQT（IXO + 2CU）已调研，但结构有工程改造、缺失段和缺失侧链，不可静默修补或把断链直接连接；尚未完成本机下载/远端提交，多组分仍为实验性。网络失败不视为模型科学验收失败。

宿主 485 项测试全部通过。Ubuntu 22.04.3 / Python 3.10.12 的公开源码包运行 485 项，460 项通过、25 项因未分发私有结构等条件明确跳过。非 root 一键升级、doctor 和扫描帮助通过；旧安装与测试产物均保留。

新增覆盖独立预检与简短摘要、真实坐标 PNG、前处理封存/复用与扫描限流、逐组分化学与位姿、多组分 HTTP 表单发现、最终归档任务谱系和不可变下载收据。更换归档、任务 ID、收据或提取坐标不能复用旧成功报告；预览不得拿另一任务的文件当作当前最终模型。

真实 CrtW 输入已完成离线 `check`，320 个蛋白残基、配体 40 个碳重原子与 56 氢，生成输入三视图。此前原始复合物建膜归档用本版重新核验任务谱系、膜叶、盐、位姿和 GROMACS，验收通过但有 CHARMM 最小化审查项。最终 PDB/GRO/PSF 均为 367350 原子，PDB/GRO 原子名称与顺序一致，最大单轴坐标差 0.005 Å，符合 GRO 精度舍入；生成最终输入三视图和未改变的模型 PDB，并检查 PNG 尺寸、内容、标签与缓存哈希。

多配体/有机辅因子的本地化学准备、逐组拓扑/共同蛋白拟合位姿和 HTTP 编排由合成结构及模拟服务器验证，**尚未完成真实多组分远端端到端验收**。可信自定义拓扑自动上传、重复同名副本、金属配位和多组分前处理复用仍明确拒绝。没有执行本地最小化、平衡或生产模拟。

##### 0.4.0 项目完整性检查

| 范围 | 验证方式与当前结果 |
| --- | --- |
| 安装与发行 | 标准 wheel、源码 tar.gz、SHA256SUMS；白名单排除凭据、用户 PDB、runs、虚拟环境 |
| Ubuntu 22.04 | 实际 Ubuntu 22.04.3 / Python 3.10.12 容器，非 root 用户一键安装、全依赖、重复升级及 doctor/help/version 通过 |
| 跨目录登录 | 裸 login 同时保存两种会话；换工作目录访问同一真实 job 成功；旧 cwd token 不覆盖全局新登录 |
| 原始输入 | 独立 SDF/PDB、复合物自动拆分、明确原子名映射/唯一 SMILES 图映射；错误和歧义输入拒绝 |
| 自动任务管理 | 自动路径、命名任务、只读状态/报告、最近任务恢复、提交防重、证据绑定缓存 |
| 传输与恢复 | GET 有限重试和 Retry-After；认证/访问限制/临时网络/未知提交分型；POST 不自动重放 |
| 批量调度 | 全员本地准备、1–4 生命周期活跃名额、启动间隔、进程锁、清单指纹和 JSON/CSV 汇总 |
| 下载完整性 | gzip CRC/长度、有效 tar、精确字节数尾部兼容；截断/错误尾部/多 gzip 成员拒绝 |
| 静态验收 | GROMACS 拓扑/原子顺序/配体连接、HMR 元素识别、盐浓度直接证据、相对位姿、上下叶实际数量和比例 |
| 质量审查项 | CGenFF penalty、CHARMM 正常结束和最小化阶段/警告；将流程通过与科学收敛分开 |
| 隐私与保留 | 密码不持久化；会话 0600；不删除旧安装/任务/失败产物；不分发私有测试结构 |

0.4.0 历史宿主全套：325 项测试全部通过，包含本地私有 CrtW 结构和 RDKit。新增回归覆盖 pending 任务名额、跨重启限流、认证暂停、未知 POST 不重发、过期验证报告、编译产物篡改以及失败编译报告的终态缓存。

Ubuntu 22.04 / Python 3.10 公开源包测试：325 项，300 项通过、25 项明确跳过，退出码 0。非 root 安装、两项无凭据合成输入批次预检和跨目录 CLI 检查通过，见 [安装记录](docs/INSTALL.md)。源包不包含私有结构；相关用例明确跳过，其余合成输入和传输模拟测试执行。安装和单元测试没有向远端创建建模任务。

宿主已实际运行 `bash install.sh` 安装隔离版本，并验证跨工作目录使用、`-h`、`doctor`、原始复合物本地准备和归档恢复验收。旧安装、失败产物和历史报告均保留。

```bash
python -m unittest discover -s tests -v
```

发行包不含私有 test-dataset，集成用例会明确 skip，而不是伪造通过；合成结构测试仍执行。无需账户或网络运行单元测试。

##### 已完成的真实体系

从独立蛋白 PDB/配体 SDF 自动完成前处理及建膜，真实任务编号不公开。最终归档 SHA-256：

`6c145037bb1ecf3f37923966b2a1cc17135d9bccf6ebc87aa73f4631c1c8d833`

367489 原子，942 POPC，78620 水，143 POT、144 CLA，实际构建流为 KCl 0.10 M。配体 40 个碳重原子、56 氢、97 条含氢键及重原子图保留。最终配体在蛋白拟合后的 RMSD 为 0.307872 Å；GROMACS 2024.4 `grompp -maxwarn 0` 成功，0 WARNING、1 NOTE。

0.4.0 对该归档复核后，实际膜头基/残基与装填记录一致：上叶 479 POPC，下叶 463 POPC。CGenFF parameter penalty 1.400、charge penalty 1.442；CHARMM 正常终止，检测到四段 0–100 次最小化记录、两次显式步数上限退出、460 条 bent-improper 警告。验收状态为 `passed_with_warnings`，要求科学审查而不是宣称充分收敛。运行记录和模型不随公开源码包分发。

旧 NaCl 0.15 M 反例虽能通过 GROMACS 编译，却被 KCl 0.10 M 验收正确拒绝。详细实测、CGenFF penalty 和警告边界见 [RESEARCH.md](RESEARCH.md)。

##### 原始复合物独立流程复测

已通过直接原始复合物命令进行本地准备，随后零路径 `build-resume` 自动完成真实上传、CGenFF 参数化、建膜与最终下载；真实任务编号不公开。这不是复制旧任务结果，也未手工编辑自动生成配置。

最终归档 SHA-256：`93760128b1e53c51d2f482609db31932067504d8fd8622e4a0c9b6f0a01751ba`。367350 原子、上叶 479/下叶 463 POPC、KCl 0.10 M；同一蛋白拟合变换下配体 RMSD 0.206126 Å，最大位移 0.618284 Å。GROMACS 输入编译与全套最终检查通过。CGenFF penalties 同上；CHARMM 日志中有 495 条 bent-improper 警告，最终 GRMS 3.64207，正常终止不等于收敛。原记录曾误用另一个独立模型的 367489 原子计数；此处按本归档 PDB/GRO/PSF 一致的计数更正。

##### 批次与中断恢复实测

在同一批次中分别从独立蛋白/SDF、原始复合物 PDB 完成本地输入准备，再接入上述两个已经完成的远端任务及其归档。将父流程保持在下载后的未同步状态，使用明确不存在的 token 路径执行 `batch-resume --grompp`，两个项目均恢复为 `validated`、`gromacs_compiled: true`，批次 `complete`，自动生成 JSON/CSV 汇总。没有重新上传、重新提交或要求登录。

此测试验证真实归档、本地状态同步、两种输入组合、队列恢复和编译验收；并发名额、限流、认证失效、GET 网络恢复和未知 POST 的队列行为由模拟测试覆盖。**本轮未另行提交两个新的远端任务来证明全新的批量上传链路。** 单体系真实上传/参数化/建膜链路已由上述两个独立模型验证。

归档、原始输入、配置、工具版本、编译器身份或 TPR/编译日志变化后，旧报告不得继续显示有效通过；恢复会重新验收。GROMACS 检查失败且失败日志未变时保留失败终态，避免批次无意义地重复编译。

##### 不能从通过结果推导的结论

GROMACS 编译不等于已执行 MD。CHARMM 服务端短最小化正常结束，但达到设定步数且仍有 bent improper 警告，不能称充分收敛。可解析警告涉及 POPC，部分大原子编号溢出不能可靠定位。体系仍需研究者审核质子化、取向、配体参数、HMR/时间步长及完整平衡过程。

Ubuntu 22.04 是实际测试平台；其他 Ubuntu 22+ 发行版的兼容依据是 Python 3.10+ 和隔离 venv，不能把它写成每个发行版/CPU 架构都已实测。未在容器内进行真实 CHARMM-GUI 建模或 GROMACS 科学验收。
