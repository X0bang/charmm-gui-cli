# Package boundaries

Python implementation imports use canonical subpackages, not historical top-level aliases.

- `commands/`: command adapters exposing `execute(args)`. Shared command variants may re-export the same handler. The only service imported from `cli.py` is `emit`; authentication, configuration and task operations come from their owning modules.
- `pipeline/`: build/batch/sweep orchestration, prepared-source reuse, acceptance, worker launch/execution, evidence-bound previews, final delivery and updates.
- `remote/`: API/website requests, retry policy, archive transport and GET-only reconciliation.
- `chem/`: coordinate/chemistry normalization, validation and coordinate rendering; completed-task preview orchestration lives in `pipeline.preview`.
- `store/`: task paths, indexes and durable records (`tasks`), process-lock metadata (`workers`), owned files/model hashes (`result_files`), strict YAML/configuration loading, run records and archive ownership/receipts. These modules do not import the pipeline, CLI or commands.
- `presentation/`: progress labels, terminal summaries, stable JSON summaries and escaped reports. Views do not import the pipeline, remote requests or command handlers.
- `locales/`: bilingual message catalog, language selection and help text.

`_compat.MODULE_ALIASES` is the single registry for historical Python imports such as `charmm_gui_cli.auth`. Small legacy shims consult it only when explicitly imported. They return the same module objects as canonical paths; there is no global import hook. Internal implementation and regression mocks use canonical paths. Existing top-level imports and the historical background-worker module entry point remain available.

Implementation libraries previously located under `commands/` now live in `pipeline.build_command`, `pipeline.updater`, `presentation.reporting` and `presentation.terminal_output`. Task-management, background execution and delivery orchestration use `pipeline.managed`, `pipeline.background` and `pipeline.delivery`; the former `store.managed/background/delivery` modules are explicit compatibility shims, not internal dependencies. Integrations should use canonical paths or the retained top-level compatibility imports.

The direction is from command/CLI adapters to orchestration and lower-level chemistry, transport and storage. Lower layers cannot import orchestration, commands or CLI. Read-only views cannot depend on orchestration or remote requests. Compatibility shims are excluded only as explicitly registered legacy entry points; the canonical runtime rejects any internal reliance on them.

## Regression checks

`tests/test_package_architecture.py` verifies canonical imports statically and in a fresh process that rejects every legacy import. Another fresh process denies all upper-layer imports while exercising pure task records and locks. Tests also check actual read-only CLI dispatch, module identity compatibility, command handlers, presentation boundaries and shared lock/YAML semantics.

`tests/test_layer_boundaries.py` exercises all three real background-module entry points using an inherited lock and a synthetic offline cached-validation task; HTTP is forbidden. It checks strict provenance/configuration behavior independently of the pipeline. These are process, storage and software-compatibility checks, not a new remote modeling or scientific-equivalence acceptance. Full workflow tests continue to cover submission intents, interrupted GETs and uncertain POSTs.
