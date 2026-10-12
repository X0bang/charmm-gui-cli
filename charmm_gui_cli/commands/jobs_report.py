"""jobs report handler."""


def execute(args):
    from ..cli import (
        emit,
        brief,
        report_summary,
        Client,
        ToolError,
        load_token,
        resolve_cookie_path,
        workflow,
        json,
    )
    from .. import managed
    if args.full and args.format != "json":
        raise ToolError(key="cli.full_json")
    if args.format == "html" and args.out is None:
        raise ToolError(key="cli.html_out")
    directory = managed.resolve_run(args.target)
    result = managed.inspect_run(directory)
    if result.get("validation_status") == "stale_or_unreadable":
        raise ToolError(key="cli.stale_report")
    path = directory / "results" / "validation.json"
    if not path.is_file():
        raise ToolError(key="cli.no_report")
    report = managed._read_json(path)
    summary = report_summary(report, result.get("name"), path)
    if args.format == "json":
        payload = report if args.full else summary
        if args.out:
            from ..reporting import write_new
            destination = write_new(args.out, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
            emit({"report": destination, "passed": report.get("passed"), "format": "json"})
        else:
            emit(payload)
    else:
        from ..reporting import display_result, render_text, render_html, verified_images, write_new
        displayed = display_result(summary, result, managed._read_json(directory / "full-run.json"), report)
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
