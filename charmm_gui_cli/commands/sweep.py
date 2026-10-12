"""sweep command handler."""



def execute(args):
    from ..cli import (
        emit,
        atomic_write,
        Path,
    )
    from .. import sweep, batch as batching, managed
    if args.command == "sweep":
        directory = args.out or managed.new_batch_path()
        prepared = sweep.prepare(args.config, directory)
        if args.dry_run:
            emit(prepared)
            return 2 if prepared.get("failed") else 0
    else:
        directory = args.directory
    saved = sweep.read(directory)
    batch_directory = Path(saved["batch_directory"])
    if args.command == "sweep-status":
        report = batching.status(batch_directory, remote_check=args.remote_check, token_file=args.token_file)
        if args.csv:
            atomic_write(args.csv, batching.export_csv(report))
            report["csv"] = str(args.csv.resolve())
        emit({"sweep_directory": str(Path(directory).resolve()), **report})
        return 0
    report, code = batching.advance(batch_directory, max_active=args.max_active,
        submit_interval=args.submit_interval, wait=args.wait, interval=args.interval,
        max_wait=args.max_wait, token_file=args.token_file, cookies=args.cookies,
        grompp=args.grompp, no_grompp=args.no_grompp, gmx=args.gmx, no_preview=args.no_preview, post_timeout=args.post_timeout)
    emit({"sweep_directory": str(Path(directory).resolve()), **report})
    return code
