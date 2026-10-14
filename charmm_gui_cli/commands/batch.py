"""batch command handler."""

import charmm_gui_cli.store.tasks as _task_store



def execute(args):
    from charmm_gui_cli.cli import emit
    from charmm_gui_cli.store.auth import atomic_write
    from charmm_gui_cli.pipeline import batch as batching
    pass
    if args.command == "batch-status":
        report = batching.status(args.directory, remote_check=args.remote_check, token_file=args.token_file)
        if args.csv:
            atomic_write(args.csv, batching.export_csv(report))
            report["csv"] = str(args.csv.resolve())
        emit(report, command=args.command)
        return 0
    if args.command == "batch":
        name = args.name or batching.load_batch(args.config).get("name")
        if name is not None:
            _task_store.validate_name(name)
        directory = args.out or _task_store.new_batch_path(name, source=args.config)
    else:
        directory = args.directory
    from ..pipeline.batch_progress import BatchReporter
    reporter = BatchReporter(directory)
    try:
        if args.command == "batch":
            report = batching.initialize(args.config, directory, register=False, progress=reporter, name=args.name)
            reporter.update(report)
            if args.dry_run:
                reporter.close()
                emit(report, command=args.command, dry_run=True)
                return 2 if report.get("failed", 0) else 0
        else:
            reporter.update(batching.status(directory))
        report, code = batching.advance(directory, max_active=args.max_active,
            submit_interval=args.submit_interval, wait=args.wait, interval=args.interval,
            max_wait=args.max_wait, token_file=args.token_file, cookies=args.cookies,
            grompp=args.grompp, no_grompp=args.no_grompp, gmx=args.gmx, no_preview=args.no_preview, post_timeout=args.post_timeout,
            progress=reporter, retry_failed_local=getattr(args, "retry_failed_local", False))
        reporter.close()
        emit(report, command=args.command)
        return code
    except KeyboardInterrupt:
        import shlex
        from ..locales.i18n import t
        reporter._line(t("batch.interrupted", resume="charmm-gui-cli batch-resume " + shlex.quote(str(directory))))
        return 130
    finally:
        reporter.close()
