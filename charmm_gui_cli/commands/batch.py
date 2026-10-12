"""batch command handler."""



def execute(args):
    from ..cli import (
        emit,
        atomic_write,
    )
    from .. import batch as batching, managed
    if args.command == "batch-status":
        report = batching.status(args.directory, remote_check=args.remote_check, token_file=args.token_file)
        if args.csv:
            atomic_write(args.csv, batching.export_csv(report))
            report["csv"] = str(args.csv.resolve())
        emit(report)
        return 0
    if args.command == "batch":
        directory = args.out or managed.new_batch_path()
        report = batching.initialize(args.config, directory)
        if args.dry_run:
            emit(report)
            return 2 if report.get("failed", 0) else 0
    else:
        directory = args.directory
    report, code = batching.advance(directory, max_active=args.max_active,
        submit_interval=args.submit_interval, wait=args.wait, interval=args.interval,
        max_wait=args.max_wait, token_file=args.token_file, cookies=args.cookies,
        grompp=args.grompp, no_grompp=args.no_grompp, gmx=args.gmx, no_preview=args.no_preview, post_timeout=args.post_timeout)
    emit(report)
    return code
