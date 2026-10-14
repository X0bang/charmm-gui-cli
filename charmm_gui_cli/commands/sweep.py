"""sweep command handler."""

import charmm_gui_cli.store.tasks as _task_store



def execute(args):
    from charmm_gui_cli.cli import emit
    from charmm_gui_cli.store.auth import atomic_write
    from pathlib import Path
    from charmm_gui_cli.pipeline import sweep, batch as batching
    pass
    if args.command == "sweep":
        if args.name is not None:
            _task_store.validate_name(args.name)
        directory = args.out or _task_store.new_batch_path(args.name, kind="sweep", source=args.config)
    else:
        directory = args.directory
    from ..pipeline.batch_progress import BatchReporter
    reporter = None if args.command == "sweep-status" else BatchReporter(Path(directory) / "batch")
    try:
        if args.command == "sweep":
            prepared = sweep.prepare(args.config, directory, register=False, progress=reporter)
            reporter.update(prepared["batch"])
            if args.dry_run:
                reporter.close()
                emit(prepared, command=args.command, dry_run=True)
                return 2 if prepared.get("failed") else 0
        return _advance(args, directory, reporter)
    except KeyboardInterrupt:
        import shlex
        from ..locales.i18n import t
        if reporter:
            reporter._line(t("batch.interrupted", resume="charmm-gui-cli sweep-resume " + shlex.quote(str(directory))))
        else:
            import sys
            print(t("batch.interrupted", resume="charmm-gui-cli sweep-status " + shlex.quote(str(directory))), file=sys.stderr)
        return 130
    finally:
        if reporter:
            reporter.close()


def _advance(args, directory, reporter):
    from pathlib import Path
    from charmm_gui_cli.cli import emit
    from ..store.auth import atomic_write
    from ..pipeline import sweep, batch as batching
    saved = sweep.read(directory)
    batch_directory = Path(saved["batch_directory"])
    if args.command == "sweep-status":
        report = batching.status(batch_directory, remote_check=args.remote_check, token_file=args.token_file)
        if args.csv:
            atomic_write(args.csv, batching.export_csv(report))
            report["csv"] = str(args.csv.resolve())
        emit({"sweep_directory": str(Path(directory).resolve()), **report}, command=args.command)
        return 0
    reporter.update(batching.status(batch_directory))
    report, code = batching.advance(batch_directory, max_active=args.max_active,
        submit_interval=args.submit_interval, wait=args.wait, interval=args.interval,
        max_wait=args.max_wait, token_file=args.token_file, cookies=args.cookies,
        grompp=args.grompp, no_grompp=args.no_grompp, gmx=args.gmx, no_preview=args.no_preview, post_timeout=args.post_timeout,
        progress=reporter, retry_failed_local=getattr(args, "retry_failed_local", False))
    reporter.close()
    emit({"sweep_directory": str(Path(directory).resolve()), **report}, command=args.command)
    return code
