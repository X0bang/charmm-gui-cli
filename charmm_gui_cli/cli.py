import argparse
from getpass import getpass
import json
import os
from pathlib import Path
import sys

import yaml

from . import __version__
from .api import Client
from .artifacts import inspect_archive
from .auth import ToolError, atomic_json, atomic_write, default_token_path, load_token, resolve_cookie_path, token_info
from .config import load_config, plan, validate_config
from . import workflow
from .i18n import set_language, t


_JSON_OUTPUT = False


def emit(value, command=None, **context):
    from .terminal_output import FORMATTERS
    formatter = FORMATTERS.get(command)
    if not _JSON_OUTPUT and sys.stdout.isatty() and formatter is not None:
        print(formatter(value, **context))
    else:
        # Preserve the exact original serialization for pipes and scripts.
        print(json.dumps(value, indent=2, ensure_ascii=False))


def brief(manifest):
    return {key: manifest[key] for key in ("state", "job_id", "remote_status", "validation") if key in manifest}


def precheck_summary(report):
    result = {key: report[key] for key in ("passed", "status", "directory", "check_file", "review_required",
        "server_submitted", "task_registered", "scientific_correctness_verified", "prepared_complex", "preview", "error") if key in report}
    inventory = report.get("protein_inventory", {})
    result["protein"] = {"residues": inventory.get("protein_residues_all_models"),
        "residues_missing_heavy_atoms": len(inventory.get("missing_heavy_atoms", [])),
        "numbering_gaps": len(inventory.get("numbering_gaps", [])),
        "sequence_alignment_performed": False, "repaired": False}
    result["chemistry"] = [{key: component[key] for key in ("resname", "atoms", "heavy_atoms", "bonds",
        "molecular_formula", "net_formal_charge", "rdkit_sanitized", "graph_sha256") if key in component}
        for component in report.get("chemistry", [])]
    if "spatial_review" in report:
        result["spatial_review"] = report["spatial_review"].get("summary", {})
    result["next_step"] = ("本地预检通过；确认审查项后，用同一组原始输入执行 charmm-gui-cli build。尚未创建远端任务。"
                           if report.get("passed") else "按报告修正原始输入后重新 check；已有诊断文件保留，尚未创建远端任务。")
    return result


def report_summary(report, name, path):
    """Keep the everyday report readable; --full retains all evidence paths."""
    acceptance = report.get("acceptance_validation", {})
    checks = acceptance.get("checks", {})
    penalty = checks.get("cgenff", {})
    minimization = checks.get("charmm_minimization", {})
    pose = report.get("bound_pose_validation", {})
    environment = report.get("environment_validation", {})
    environment_checks = environment.get("checks", {})
    concentration = environment_checks.get("concentration_direct_evidence", {})
    return {"state": report.get("status"), "passed": report.get("passed"), "name": name,
            "compilation": report.get("compilation"),
            "environment": {"passed": environment.get("passed"),
                "lipids": environment_checks.get("requested_lipids_present", {}).get("counts"),
                "water_molecules": environment_checks.get("water_present", {}).get("molecules"),
                "ions": environment_checks.get("ion_species"),
                "concentration_M": concentration.get("requested_M"),
                "concentration_verified": concentration.get("passed")},
            "bound_pose": {key: pose[key] for key in ("passed", "protein_ca_rmsd_A",
                "ligand_rmsd_after_protein_fit_A", "ligand_max_displacement_A") if key in pose},
            "components": {resname: {"passed": entry.get("passed"), "copies": entry.get("exact_copy_count"),
                "ligand_rmsd_A": entry.get("bound_pose", {}).get("ligand_rmsd_after_protein_fit_A"),
                "ligand_max_displacement_A": entry.get("bound_pose", {}).get("ligand_max_displacement_A"),
                "pose_error": entry.get("bound_pose", {}).get("error"),
                "cgenff": {key: acceptance.get("component_penalties", {}).get(resname, {}).get(key)
                    for key in ("status", "parameter_max", "charge_max", "review_required")}}
                for resname, entry in report.get("component_validation", {}).get("components", {}).items()},
            "acceptance": {"passed": acceptance.get("passed"), "status": acceptance.get("status"),
                "leaflets": checks.get("leaflets", {}).get("leaflets"),
                "cgenff": {key: penalty[key] for key in ("parameter_max", "charge_max", "review_required") if key in penalty},
                "charmm": {key: minimization[key] for key in ("normal_termination", "warning_counts",
                    "most_severe_warning_level", "minimization_convergence_verified", "equilibration_verified") if key in minimization},
                "warnings": acceptance.get("warnings", [])},
            "review_required": report.get("review_required", False),
            "scientific_correctness_verified": report.get("scientific_correctness_verified", False),
            "report": str(path)}


def add_build_runtime_options(command):
    command.add_argument("--post-timeout", type=float, default=900, help=t("help.post_timeout"))
    command.add_argument("--cookies", type=Path, help=t('help.text.00'))
    waiting = command.add_mutually_exclusive_group()
    waiting.add_argument("--wait", dest="wait", action="store_true", help=t('help.text.01'))
    waiting.add_argument("--no-wait", dest="wait", action="store_false", help=t('help.text.02'))
    command.set_defaults(wait=True)
    command.add_argument("--interval", type=int, default=30)
    command.add_argument("--max-wait", type=int, default=21600)
    checking = command.add_mutually_exclusive_group()
    checking.add_argument("--grompp", action="store_true", help=t('help.text.03'))
    checking.add_argument("--no-grompp", action="store_true", help=t('help.text.04'))
    command.add_argument("--gmx", default="gmx", help=t('help.text.05'))
    command.add_argument("--no-preview", action="store_true", help=t('help.text.06'))


class LocalizedHelpFormatter(argparse.HelpFormatter):
    def _format_usage(self, usage, actions, groups, prefix):
        return super()._format_usage(usage, actions, groups, t("help.usage") if prefix is None else prefix)


class LocalizedDescriptionFormatter(LocalizedHelpFormatter, argparse.RawDescriptionHelpFormatter):
    pass


class LocalizedParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("formatter_class", LocalizedHelpFormatter)
        super().__init__(*args, **kwargs)
        self._positionals.title = t("help.positionals")
        self._optionals.title = t("help.options")

    def add_argument(self, *args, **kwargs):
        if kwargs.get("help") == "show this help message and exit":
            kwargs["help"] = t("help.show")
        return super().add_argument(*args, **kwargs)

    def error(self, message):
        # argparse messages may reflect unknown arguments (including secrets).
        self.print_usage(sys.stderr)
        self.exit(2, t("cli.error", message=t("help.invalid_arguments")) + "\n")


def parser():
    from .helptext import OVERVIEW, BUILD_GUIDE
    root = LocalizedParser(prog="charmm-gui-cli", description=OVERVIEW,
                           formatter_class=LocalizedDescriptionFormatter)
    root.add_argument("--version", action="version", version=__version__, help=t("help.version"))
    root.add_argument("--lang", choices=("zh", "en"), help=t("help.lang"))
    root.add_argument("--json", action="store_true", help=t("help.json"))
    root.add_argument("--token-file", type=Path, help=t('help.text.07'))
    commands = root.add_subparsers(dest="command", required=True)
    updater = commands.add_parser("update", help=t("update.help"))
    updater.add_argument("--check", action="store_true", help=t("update.check_help"))
    updater.add_argument("--prefix", type=Path, help=t("update.prefix_help"))
    updater.add_argument("--bin-dir", type=Path, help=t("update.bin_help"))
    login = commands.add_parser("login", help=t('help.text.08'))
    login.add_argument("--email")
    login.add_argument("--save-to", type=Path, help=t('help.text.09'))
    login.add_argument("--web", action="store_true", help=argparse.SUPPRESS)
    login.add_argument("--cookies", type=Path, help=t('help.text.10'))
    web_login = commands.add_parser("web-login", help=t('help.text.11'))
    web_login.add_argument("--email")
    web_login.add_argument("--cookies", type=Path, help=t('help.text.12'))
    web_inspect = commands.add_parser("web-inspect", help=t('help.text.13'))
    web_inspect.add_argument("--doc", required=True)
    web_inspect.add_argument("--out", type=Path, required=True)
    web_inspect.add_argument("--cookies", type=Path)
    web_upload = commands.add_parser("web-upload", help=t('help.text.14'))
    web_upload.add_argument("pdb", type=Path)
    web_upload.add_argument("--out", type=Path, required=True)
    web_upload.add_argument("--project", choices=("membrane_bilayer", "pdbreader"), default="membrane_bilayer")
    web_upload.add_argument("--cookies", type=Path)
    web_step = commands.add_parser("web-step", help=t('help.text.15'))
    web_step.add_argument("snapshot", type=Path)
    web_step.add_argument("--out", type=Path, required=True)
    web_step.add_argument("--set", dest="fields", action="append", default=[], metavar="NAME=VALUE")
    web_step.add_argument("--file", dest="files", action="append", default=[], metavar="NAME=PATH")
    web_step.add_argument("--cookies", type=Path)
    auth = commands.add_parser("auth", help=t('help.text.16'))
    auth.add_argument("--check", action="store_true", help=t('help.text.17'))
    auth.add_argument("--jobid", help=t('help.text.18'))
    check = commands.add_parser("plan", help=t('help.text.19'))
    check.add_argument("config", type=Path)
    prepare = commands.add_parser("prepare", help=t('help.text.20'))
    prepare.add_argument("pdb", type=Path)
    prepare.add_argument("--ligand", action="append", required=True)
    prepare.add_argument("--out", required=True, type=Path)
    inputs = commands.add_parser("inputs", help=t('help.text.21'))
    inputs.add_argument("--protein", type=Path, required=True)
    inputs.add_argument("--ligand", type=Path, required=True)
    inputs.add_argument("--out", type=Path, required=True)
    inputs.add_argument("--ligand-resname", default="LIG")
    inputs.add_argument("--hydrogens", choices=("preserve", "add_missing"), default="preserve")
    split = commands.add_parser("split", help=t('help.text.22'))
    split.add_argument("pdb", type=Path)
    split.add_argument("--ligand", default="LIG", help=t('help.text.23'))
    split.add_argument("--out", type=Path, required=True)
    split.add_argument("--accept-conect-bond-orders", action="store_true", required=True)
    jobs = commands.add_parser("jobs", help=t('help.text.24'))
    job_commands = jobs.add_subparsers(dest="jobs_command")
    job_commands.add_parser("list", help=t('help.text.25'))
    job_status = job_commands.add_parser("status", aliases=("show",), help=t('help.text.26'))
    job_status.add_argument("target", help=t('help.text.27'))
    job_status.add_argument("--remote-check", action="store_true", help=t('help.text.28'))
    job_report = job_commands.add_parser("report", help=t('help.text.29'))
    job_report.add_argument("target", help=t('help.text.27'))
    job_report.add_argument("--full", action="store_true", help=t('help.text.30'))
    job_report.add_argument("--format", choices=("json", "text", "html"), default="json",
                            help=t('help.text.31'))
    job_report.add_argument("--out", type=Path, help=t('help.text.32'))
    job_resume = job_commands.add_parser("resume", help=t('help.text.33'))
    job_resume.add_argument("target", help=t('help.text.27'))
    add_build_runtime_options(job_resume)
    job_attach = job_commands.add_parser("attach", help=t('help.text.34'))
    job_attach.add_argument("target", help=t('help.text.27'))
    job_attach.add_argument("--jobid", required=True)
    doctor = commands.add_parser("doctor", help=t('help.text.35'))
    doctor.add_argument("--gmx", default="gmx")
    build = commands.add_parser("build", help=t('help.text.36'),
                                epilog=BUILD_GUIDE, formatter_class=LocalizedDescriptionFormatter)
    build.add_argument("config", nargs="?", type=Path, help=t('help.text.37'))
    structures = build.add_argument_group(t("help.input_group"))
    structures.add_argument("--protein", type=Path, help=t('help.text.38'))
    structures.add_argument("--ligand", type=Path, help=t('help.text.39'))
    structures.add_argument("--complex", dest="complex_pdb", type=Path, help=t('help.text.40'))
    structures.add_argument("--components", type=Path, help=t('help.text.41'))
    structures.add_argument("--ligand-resname", help=t('help.text.42'))
    structures.add_argument("--ligand-smiles", help=t('help.text.43'))
    structures.add_argument("--accept-conect-bond-orders", action="store_true", help=t('help.text.44'))
    structures.add_argument("--hydrogens", choices=("add_missing", "preserve"), help=t('help.text.45'))
    model = build.add_argument_group(t("help.model_group"))
    model.add_argument("--upper", help=t('help.text.46'))
    model.add_argument("--lower", help=t('help.text.47'))
    model.add_argument("--salt", choices=("KCl", "NaCl"), help=t('help.text.48'))
    model.add_argument("--salt-concentration", type=float, help=t('help.text.49'))
    model.add_argument("--margin", type=float, help=t('help.text.50'))
    model.add_argument("--water-padding", type=float, help=t('help.text.51'))
    model.add_argument("--orientation", choices=("ppm", "prepared"), help=t('help.text.52'))
    from .pdb_normalization import N_TERMINALS, C_TERMINALS
    model.add_argument("--n-terminal", choices=N_TERMINALS, help=t('help.text.53'))
    model.add_argument("--c-terminal", choices=C_TERMINALS, help=t('help.text.54'))
    build.add_argument("--out", type=Path, help=t('help.text.55'))
    build.add_argument("--name", help=t('help.text.56'))
    build.add_argument("--dry-run", action="store_true", help=t('help.text.57'))
    build_resume = commands.add_parser("build-resume", help=t('help.text.58'))
    build_resume.add_argument("directory", nargs="?", type=Path, help=t('help.text.59'))
    for command in (build, build_resume):
        add_build_runtime_options(command)
    batch = commands.add_parser("batch", help=t('help.text.60'))
    batch.add_argument("config", type=Path)
    batch.add_argument("--out", type=Path, help=t('help.text.61'))
    batch.add_argument("--dry-run", action="store_true", help=t('help.text.62'))
    batch_resume = commands.add_parser("batch-resume", help=t('help.text.63'))
    batch_resume.add_argument("directory", type=Path)
    for command in (batch, batch_resume):
        add_build_runtime_options(command)
        command.add_argument("--max-active", type=int, default=1, help=t('help.text.90'))
        command.add_argument("--submit-interval", type=int, default=30, help=t('help.text.91'))
    batch_status = commands.add_parser("batch-status", help=t('help.text.64'))
    batch_status.add_argument("directory", type=Path)
    batch_status.add_argument("--remote-check", action="store_true")
    batch_status.add_argument("--csv", type=Path, help=t('help.text.65'))
    sweep = commands.add_parser("sweep", help=t('help.text.66'))
    sweep.add_argument("config", type=Path)
    sweep.add_argument("--out", type=Path)
    sweep.add_argument("--dry-run", action="store_true", help=t('help.text.67'))
    sweep_resume = commands.add_parser("sweep-resume", help=t('help.text.68'))
    sweep_resume.add_argument("directory", type=Path)
    for command in (sweep, sweep_resume):
        add_build_runtime_options(command)
        command.add_argument("--max-active", type=int, default=1, help=t('help.text.92'))
        command.add_argument("--submit-interval", type=int, default=30)
    sweep_status = commands.add_parser("sweep-status", help=t('help.text.69'))
    sweep_status.add_argument("directory", type=Path)
    sweep_status.add_argument("--remote-check", action="store_true")
    sweep_status.add_argument("--csv", type=Path)
    precheck = commands.add_parser("check", help=t('help.text.70'))
    for name in ("protein", "ligand", "complex", "components"):
        precheck.add_argument("--" + name, dest="complex_pdb" if name == "complex" else name, type=Path)
    precheck.add_argument("--ligand-resname", default="LIG")
    precheck.add_argument("--ligand-smiles")
    precheck.add_argument("--accept-conect-bond-orders", action="store_true")
    precheck.add_argument("--hydrogens", choices=("preserve", "add_missing"), default="add_missing")
    precheck.add_argument("--out", type=Path, help=t('help.text.71'))
    precheck.add_argument("--no-preview", action="store_true")
    precheck.add_argument("--full", action="store_true", help=t('help.text.72'))
    preview = commands.add_parser("preview", help=t('help.text.73'))
    preview.add_argument("target", help=t('help.text.74'))
    preview.add_argument("--out", type=Path, help=t('help.text.75'))
    preview.add_argument("--ligand", action="append", help=t('help.text.76'))
    preview.add_argument("--width", type=int, default=1400, help=t('help.text.77'))
    preview.add_argument("--view", action="append", choices=("side", "top", "ligand"))
    validate = commands.add_parser("validate", help=t('help.text.78'))
    validate.add_argument("system", type=Path)
    validate.add_argument("--input-manifest", type=Path, required=True)
    validate.add_argument("--build-config", type=Path, help=t('help.text.79'))
    validate.add_argument("--reference-pdb", type=Path, help=t('help.text.80'))
    validate.add_argument("--out", type=Path, required=True, help=t('help.text.81'))
    validate.add_argument("--grompp", action="store_true")
    validate.add_argument("--gmx", default="gmx")
    run = commands.add_parser("run", help=t('help.text.82'))
    run.add_argument("config", type=Path)
    run.add_argument("--out", required=True, type=Path)
    resume = commands.add_parser("resume", help=t('help.text.83'))
    resume.add_argument("directory", type=Path)
    for command in (run, resume):
        command.add_argument("--wait", action="store_true")
        command.add_argument("--interval", type=int, default=30)
        command.add_argument("--max-wait", type=int, default=21600, help=t('help.text.93'))
        command.add_argument("--pdb-member", help=t('help.text.94'))
    status = commands.add_parser("status", help=t('help.text.84'))
    status.add_argument("jobid")
    status.add_argument("--diagnostic", action="store_true", help=t('help.text.85'))
    attach = commands.add_parser("attach", help=t('help.text.86'))
    attach.add_argument("directory", type=Path)
    attach.add_argument("--jobid", required=True)
    download = commands.add_parser("download", help=t('help.text.87'))
    download.add_argument("jobid")
    download.add_argument("--out", type=Path, required=True)
    inspect = commands.add_parser("inspect", help=t('help.text.88'))
    inspect.add_argument("archive", type=Path)
    inspect.add_argument("--ligand", action="append", required=True, help=t('help.text.89'))
    inspect.add_argument("--pdb-member")
    return root


def dispatch(args):
    if args.command == "update":
        from .updater import update
        result = update(check=args.check, prefix=args.prefix, bin_dir=args.bin_dir)
        emit(result, command="update")
        return 0
    if args.command == "check":
        from .precheck import check_inputs
        from .preview import render_model, new_preview_path
        report = check_inputs(out=args.out or new_preview_path("checks"), protein=args.protein,
            ligand=args.ligand, complex_pdb=args.complex_pdb, components=args.components,
            ligand_resname=args.ligand_resname, ligand_smiles=args.ligand_smiles,
            accept_conect_bond_orders=args.accept_conect_bond_orders, hydrogens=args.hydrogens)
        if report.get("passed") and not args.no_preview:
            try:
                report["preview"] = render_model(report["prepared_complex"], Path(report["directory"]) / "preview",
                    ligand_resnames=report["component_resnames"], scope="prepared_input")
            except (ToolError, OSError) as exc:
                report["preview"] = {"status": "failed", "message": str(exc)}
            atomic_json(Path(report["check_file"]), report)
        emit(report if args.full else precheck_summary(report))
        return 0 if report.get("passed") else 2
    if args.command == "preview":
        from .preview import render_model, preview_run, new_preview_path
        if Path(args.target).is_file():
            report = render_model(args.target, args.out or new_preview_path(),
                ligand_resnames=args.ligand or ("LIG",), width=args.width,
                views=args.view or ("side", "top", "ligand"))
        else:
            if args.ligand:
                raise ToolError("Task previews use the recorded component names; --ligand is only for a PDB file.")
            from .managed import resolve_run
            report = preview_run(resolve_run(args.target), out=args.out, width=args.width,
                                 views=args.view or ("side", "top", "ligand"))
        emit(report)
        return 0
    if args.command in ("sweep", "sweep-resume", "sweep-status"):
        from . import sweep, batch as batching, managed
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
            grompp=args.grompp, no_grompp=args.no_grompp, gmx=args.gmx, no_preview=args.no_preview)
        emit({"sweep_directory": str(Path(directory).resolve()), **report})
        return code
    if args.command == "doctor":
        from .doctor import inspect_installation
        report = inspect_installation(args.gmx)
        emit(report)
        return 0 if report["passed"] else 2
    if args.command == "jobs":
        from . import managed
        if args.jobs_command in (None, "list"):
            emit({"jobs": managed.list_runs(), "next_step": "charmm-gui-cli jobs resume NAME"})
        elif args.jobs_command in ("status", "show"):
            emit(managed.inspect_run(args.target, remote_check=args.remote_check, token_file=args.token_file), command="jobs.status")
        elif args.jobs_command == "report":
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
                    from .reporting import write_new
                    destination = write_new(args.out, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
                    emit({"report": destination, "passed": report.get("passed"), "format": "json"})
                else:
                    emit(payload)
            else:
                from .reporting import display_result, render_text, render_html, verified_images, write_new
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
        elif args.jobs_command == "resume":
            from .build_command import execute
            args.directory = managed.resolve_run(args.target)
            args.command = "build-resume"
            result, code = execute(args)
            emit(result, command="jobs.resume")
            return code
        elif args.jobs_command == "attach":
            directory = managed.resolve_run(args.target)
            token, _ = load_token(args.token_file)
            result = workflow.attach(Client(token), directory / "bilayer", args.jobid)
            managed.record_recovery(directory, state="submitted")
            emit({"directory": str(directory), **brief(result), "next_step": "charmm-gui-cli jobs resume " + args.target})
        return 0
    if args.command in ("batch", "batch-resume", "batch-status"):
        from . import batch as batching, managed
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
            grompp=args.grompp, no_grompp=args.no_grompp, gmx=args.gmx, no_preview=args.no_preview)
        emit(report)
        return code
    if args.command == "inputs":
        from .inputs import prepare_inputs
        emit(prepare_inputs(args.protein, args.ligand, args.out, ligand_resname=args.ligand_resname,
                            hydrogen_policy=args.hydrogens))
        return 0
    if args.command == "split":
        from .inputs import split_complex
        emit(split_complex(args.pdb, args.out, ligand_resname=args.ligand, accept_conect_bond_orders=True))
        return 0
    if args.command in ("build", "build-resume"):
        from .build_command import execute
        result, code = execute(args)
        emit(result, command=args.command)
        return code
    if args.command == "validate":
        from .validation import extract_for_validation, inspect_environment, validate_system
        build_config = None
        if args.build_config:
            from .build_config import load_build_config
            build_config = load_build_config(args.build_config)
        if args.out.exists():
            raise ToolError(key="cli.validation_exists")
        args.out.mkdir(parents=True)
        system = args.system
        if system.is_file():
            system = extract_for_validation(system, args.out / "extracted")
        report = validate_system(system, args.input_manifest, run_grompp=args.grompp,
                                 gmx=args.gmx, output_parent=args.out)
        if build_config is not None:
            coordinates = report.get("files", {}).get("coordinates")
            if not coordinates:
                environment = {"passed": False, "error": "No validated coordinate file was identified for environment checks."}
            else:
                lipids = sorted({name for leaflet in ("upper", "lower")
                                 for name in build_config["membrane"][leaflet].split("=", 1)[0].split(":")})
                try:
                    environment = inspect_environment(
                        coordinates, expected_lipids=lipids, ion_type=build_config["ions"]["type"],
                        concentration_M=build_config["ions"]["concentration_M"], system_dir=system)
                except ToolError as exc:
                    environment = {"passed": False, "error": str(exc)}
            report["environment_validation"] = environment
            report["passed"] = bool(report["passed"] and environment["passed"])
            from .acceptance import assess_acceptance
            acceptance = assess_acceptance(system, membrane=build_config["membrane"], ligand_resname=build_config["ligand_resname"])
            report.update(acceptance_validation=acceptance, review_required=acceptance.get("review_required", False),
                          review_warnings=acceptance.get("warnings", []))
            report["passed"] = bool(report["passed"] and acceptance["passed"])
        if args.reference_pdb is not None:
            from .pose_validation import validate_bound_pose
            pose = validate_bound_pose(system, args.input_manifest, args.reference_pdb)
            report["bound_pose_validation"] = pose
            report["passed"] = bool(report["passed"] and pose["passed"])
        atomic_json(args.out / "validation.json", report)
        emit(report)
        return 0 if report["passed"] else 2
    if args.command in ("web-login", "web-inspect", "web-upload", "web-step"):
        from .web import WebClient
        cookies = resolve_cookie_path(args.cookies)
        web_client = WebClient(None if args.command == "web-login" else cookies)
        if args.command == "web-login":
            result = web_client.login(args.email or input(t("prompt.email")), getpass(t("prompt.password")))
            web_client.cookie_path = cookies
            web_client.save_cookies()
        elif args.command == "web-upload":
            result = web_client.upload_structure(args.pdb, args.out, args.project)
        elif args.command == "web-step":
            try:
                fields = dict(value.split("=", 1) for value in args.fields)
                files = dict(value.split("=", 1) for value in args.files)
            except ValueError:
                raise ToolError(key="cli.fields") from None
            result = web_client.submit_snapshot(args.snapshot, args.out, fields, files)
        else:
            result = web_client.inspect(args.doc, args.out)
        emit(result)
        return 0
    if args.command == "prepare":
        config = validate_config({
            "version": 1, "source_job_id": None, "input_pdb": str(args.pdb.resolve()),
            "expected_ligands": args.ligand,
            "preparation": {"ligand_parameters_ready": False, "orientation": "ppm"},
            "membrane": {"upper": "POPC=1", "lower": "POPC=1", "margin_A": 20, "water_padding_A": 22.5},
            "ions": {"type": "NaCl", "concentration_M": 0.15},
        })
        proposal = plan(config)
        if proposal["local_pdb"]["missing_ligands"]:
            raise ToolError(key="cli.ligand_absent")
        try:
            args.out.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            raise ToolError(key="cli.preparation_exists") from None
        atomic_write(args.out / "system.yaml", yaml.safe_dump(config, sort_keys=False))
        atomic_json(args.out / "preparation.json", proposal)
        emit({"config": str(args.out / "system.yaml"), "report": str(args.out / "preparation.json"),
              "next_step": "Prepare the complex/ligand on the website, then fill source_job_id and preparation fields.",
              "defaults": "POPC, NaCl 0.15 M and PPM are editable placeholders, not recommendations for this protein."})
        return 0
    if args.command == "login":
        from .web import WebClient
        destination = (args.save_to or args.token_file or default_token_path()).expanduser()
        cookies = resolve_cookie_path(args.cookies, str(destination))
        if destination.resolve() == cookies.resolve():
            raise ToolError(key="cli.session_paths")
        email = args.email or input(t("prompt.email"))
        password = getpass(t("prompt.password"))
        token = Client().login(email, password)
        # Authenticate both transports before touching the saved credentials.
        # A failed website login must not leave a new JWT paired with old cookies.
        web_client = WebClient()
        web_result = web_client.login(email, password)
        web_client.cookie_path = cookies
        web_client.save_cookies()
        atomic_write(destination, token + "\n")
        result = {"logged_in": True, "api_authenticated": True, **web_result,
                  "saved_to": str(destination), "cookie_file": str(cookies), "permissions": "0600",
                  "next_step": "charmm-gui-cli build --protein protein.pdb --ligand bound-ligand.sdf --name receptor-a"}
        if os.environ.get("CHARMMGUI_TOKEN"):
            result["notice"] = "CHARMMGUI_TOKEN is set and overrides the saved login. Unset it to use this session by default."
        emit(result, command="login", email=email)
        return 0
    if args.command == "plan":
        proposal = plan(load_config(args.config))
        emit(proposal)
        return 0 if proposal["ready_to_submit"] else 2
    if args.command == "inspect":
        report = inspect_archive(args.archive, args.ligand, args.pdb_member)
        emit(report)
        return 0 if report["passed"] else 2
    token, source = load_token(args.token_file)
    client = Client(token)
    if args.command == "auth":
        info = {"source": source, **token_info(token), "server_checked": False}
        cookies = resolve_cookie_path(token_source=source)
        info.update(cookie_file=str(cookies), web_session_saved=cookies.is_file())
        if args.check or args.jobid:
            data = client.status(args.jobid)
            # A newly uploaded web job has status=null before computation,
            # yet a matching job record already proves API access to it.
            if "jobid" in data and data["jobid"] != args.jobid:
                raise ToolError(key="cli.jobid_mismatch")
            if "jobid" not in data:
                workflow.status_summary(data)
            info["job_access_confirmed"] = args.jobid
            info["remote_status"] = data.get("status")
            info["server_checked"] = True
        emit(info)
        return 2 if info["expired"] else 0
    if args.command == "status":
        data = client.status(args.jobid)
        if args.diagnostic:
            emit({"job_id": args.jobid, "status": data.get("status"),
                  "last_output_file": data.get("lastOutFile"), "last_input_step": data.get("lastInpStep"),
                  "archive_available": data.get("hasTarFile"),
                  "response_shape": {key: type(value).__name__ for key, value in data.items()}})
        else:
            emit({"job_id": args.jobid, **workflow.status_summary(data)}, command="status")
    elif args.command == "download":
        if workflow.status_summary(client.status(args.jobid))["status"] != "done":
            raise ToolError(key="cli.not_done")
        emit({"archive": str(client.download(args.jobid, args.out))})
    elif args.command == "attach":
        emit(brief(workflow.attach(client, args.directory, args.jobid)))
    elif args.command in ("run", "resume"):
        if args.interval < 10 or args.max_wait <= 0:
            raise ToolError(key="cli.polling")
        directory = args.out if args.command == "run" else args.directory
        if args.command == "run":
            result = workflow.start(client, load_config(args.config), directory)
            if not args.wait:
                emit(brief(result))
                return 0
        emit(brief(workflow.resume(client, directory, wait=args.wait, interval=args.interval,
                                   max_wait=args.max_wait, pdb_member=args.pdb_member)))
    return 0


def main(argv=None):
    global _JSON_OUTPUT
    set_language()
    bootstrap = LocalizedParser(add_help=False, allow_abbrev=False)
    bootstrap.add_argument("--lang")
    early, _ = bootstrap.parse_known_args(sys.argv[1:] if argv is None else argv)
    if early.lang in ("zh", "en"):
        set_language(early.lang)
    args = parser().parse_args(argv)
    _JSON_OUTPUT = args.json
    try:
        return dispatch(args)
    except ToolError as exc:
        print(t("cli.error", message=exc.localized()), file=sys.stderr)
        if getattr(exc, "next_step", None):
            print(t("cli.next", action=exc.next_step), file=sys.stderr)
        return 2
    except OSError:
        print(t("cli.error", message=t("cli.filesystem")), file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        from .auth import recovery_guidance
        print(t("update.interrupted") if args.command == "update" else t("cli.interrupted") + recovery_guidance("interrupted"), file=sys.stderr)
        return 130
