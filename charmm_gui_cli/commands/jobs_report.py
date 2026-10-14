"""jobs report handler."""

import charmm_gui_cli.store.tasks as _task_store


def execute(args):
    from charmm_gui_cli.cli import emit
    from charmm_gui_cli.presentation.summaries import report_summary
    from charmm_gui_cli.store.auth import ToolError
    import json
    from charmm_gui_cli.pipeline import managed
    if args.full and args.format != "json":
        raise ToolError(key="cli.full_json")
    if args.format == "html" and args.out is None:
        raise ToolError(key="cli.html_out")
    directory = _task_store.resolve_run(args.target)
    result = managed.inspect_run(directory)
    if result.get("validation_status") == "stale_or_unreadable":
        import shlex
        raise ToolError(key="cli.stale_report", details=result.get("validation_issues"),
                        next_step="charmm-gui-cli jobs resume " + shlex.quote(result.get("name") or str(args.target)))
    path = directory / "results" / "validation.json"
    if not path.is_file():
        raise ToolError(key="cli.no_report")
    report = _task_store._read_json(path)
    summary = report_summary(report, result.get("name"), path)
    if args.format == "json":
        payload = report if args.full else summary
        if args.out:
            from charmm_gui_cli.presentation.reporting import write_new
            destination = write_new(args.out, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
            emit({"report": destination, "passed": report.get("passed"), "format": "json"})
        else:
            emit(payload)
    else:
        from charmm_gui_cli.presentation.reporting import display_result, render_text, render_html, verified_images, write_new
        displayed = display_result(summary, result, _task_store._read_json(directory / "full-run.json"), report)
        if args.format == "html":
            images, note = verified_images(directory, report)
            content = render_html(displayed, images, note)
        else:
            content = render_text(displayed)
        if args.out:
            destination = write_new(args.out, content)
            emit({"report": destination, "passed": report.get("passed"), "format": args.format})
        else:
            print(content, end="")
    return 0 if report.get("passed") else 2

    return 0
