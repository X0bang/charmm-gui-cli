# Package boundaries

Python implementation imports use canonical subpackages, not historical top-level aliases.

- `commands/`: command adapters exposing `execute(args)`. Shared command variants may re-export the same handler. The only service imported from `cli.py` is `emit`; authentication, configuration and task operations come from their owning modules.
- `pipeline/`: build/batch/sweep orchestration, prepared-source reuse, acceptance and updates.
- `remote/`: API/website requests, retry policy, archive transport and GET-only reconciliation.
- `chem/`: coordinate/chemistry normalization, validation and rendering.
- `store/`: task paths, records, indexes, locking, strict YAML loading and existing task-management/delivery entry points.
- `presentation/`: progress labels, terminal summaries, stable JSON summaries and escaped reports. Views do not import the pipeline, remote requests or command handlers.
- `locales/`: bilingual message catalog, language selection and help text.

`_compat.MODULE_ALIASES` is the single registry for historical Python imports such as `charmm_gui_cli.auth`. Small legacy shims consult it only when explicitly imported. They return the same module objects as canonical paths; there is no global import hook. Internal implementation and regression mocks use canonical paths. Existing top-level imports and the historical background-worker module entry point remain available.

Implementation libraries previously located under `commands/` now live in `pipeline.build_command`, `pipeline.updater`, `presentation.reporting` and `presentation.terminal_output`. Those former command-directory paths were implementation details; integrations should use these canonical paths or the retained top-level compatibility imports.

## Regression checks

`tests/test_package_architecture.py` verifies canonical imports statically and in a fresh process that rejects every legacy import. It also checks actual read-only CLI dispatch, module identity compatibility, command handlers, presentation boundaries and shared lock/YAML semantics. Full workflow tests continue to cover submission intents, interrupted GETs and uncertain POSTs.

## Remaining separation

This is not yet a completely one-way package graph. Task management and worker orchestration in `store.managed`/`store.background` still call pipeline services; `chem.pose_validation` still uses pipeline archive-provenance verification. These require further extraction of orchestration and evidence primitives, without changing historical task records or weakening validation. They are not treated as solved by the import-shim checks.
