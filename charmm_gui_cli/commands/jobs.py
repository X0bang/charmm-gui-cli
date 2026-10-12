"""Named-task command dispatch, with one handler per subcommand."""


def execute(args):
    from importlib import import_module
    handlers = {
        'list': 'jobs_list',
        'export': 'jobs_export',
        'watch': 'jobs_watch',
        'reconcile': 'jobs_reconcile',
        'status': 'jobs_status',
        'show': 'jobs_show',
        'report': 'jobs_report',
        'resume': 'jobs_resume',
        'attach': 'jobs_attach',
    }
    module = import_module("." + handlers[args.jobs_command or "list"], __package__)
    return module.execute(args)
