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
        "molecular_formula", "net_formal_charge", "rdkit_sanitized", "graph_sha256", "implicit_hydrogen_atoms",
        "explicit_hydrogen_atoms", "chemistry_warnings") if key in component}
        for component in report.get("chemistry", [])]
    for key in ("automatic_repairs", "terminal_predictions", "input_normalization"):
        if key in report:
            result[key] = report[key]
    if "spatial_review" in report:
        result["spatial_review"] = report["spatial_review"].get("summary", {})
    result["next_step"] = t("check.next_passed" if report.get("passed") else "check.next_failed")
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

    def parse_args(self, args=None, namespace=None):
        args = list(sys.argv[1:] if args is None else args)
        if self.prog == "charmm-gui-cli":
            # Support global options on either side of a command. Preserve all
            # other arguments, and never interpret anything after literal --.
            before, rest = [], []
            index = 0
            while index < len(args):
                item = args[index]
                if item == "--":
                    rest.extend(args[index:])
                    break
                option = item.split("=", 1)[0]
                if option in ("--json", "--foreground", "--lang", "--token-file"):
                    before.append(item)
                    if "=" not in item and option in ("--lang", "--token-file") and index + 1 < len(args):
                        index += 1
                        before.append(args[index])
                else:
                    rest.append(item)
                index += 1
            args = before + rest
        return super().parse_args(args, namespace)


def parser():
    from .helptext import OVERVIEW, BUILD_GUIDE
    root = LocalizedParser(prog="charmm-gui-cli", description=OVERVIEW + "\n" + t("help.background_overview"),
                           formatter_class=LocalizedDescriptionFormatter)
    root.add_argument("--version", action="version", version=__version__, help=t("help.version"))
    root.add_argument("--lang", choices=("zh", "en"), help=t("help.lang"))
    root.add_argument("--json", action="store_true", help=t("help.json"))
    root.add_argument("--foreground", action="store_true", help=t("help.foreground"))
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
    job_watch = job_commands.add_parser("watch", help=t("help.watch"))
    job_watch.add_argument("target")
    job_export = job_commands.add_parser("export", help=t("help.export"))
    job_export.add_argument("target", help=t('help.text.27'))
    job_export.add_argument("--to", type=Path, help=t("help.export_to"))
    job_reconcile = job_commands.add_parser("reconcile", help=t("help.reconcile"))
    job_reconcile.add_argument("target")
    job_reconcile.add_argument("--confirm-not-submitted", action="store_true", help=t("help.confirm_not_submitted"))
    job_reconcile.add_argument("--source-jobid", help=t("help.source_jobid"))
    job_reconcile.add_argument("--cookies", type=Path)
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
    model.add_argument("--allow-unknown-lipids", action="store_true", default=None, help=t("help.unknown_lipids"))
    model.add_argument("--salt", choices=("KCl", "NaCl"), help=t('help.text.48'))
    model.add_argument("--salt-concentration", type=float, help=t('help.text.49'))
    model.add_argument("--margin", type=float, help=t('help.text.50'))
    model.add_argument("--water-padding", type=float, help=t('help.text.51'))
    model.add_argument("--orientation", choices=("ppm", "prepared"), help=t('help.text.52'))
    from .pdb_normalization import N_TERMINALS, C_TERMINALS
    model.add_argument("--n-terminal", help=t('help.terminal_mapping'))
    model.add_argument("--c-terminal", help=t('help.terminal_mapping'))
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
    precheck.add_argument("--open", action="store_true", help=t("help.open_preview"))
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
    from importlib import import_module
    handlers = {
        'update': 'update',
        'check': 'check',
        'preview': 'preview',
        'sweep': 'sweep',
        'sweep-resume': 'sweep_resume',
        'sweep-status': 'sweep_status',
        'doctor': 'doctor',
        'jobs': 'jobs',
        'batch': 'batch',
        'batch-resume': 'batch_resume',
        'batch-status': 'batch_status',
        'inputs': 'inputs',
        'split': 'split',
        'build': 'build',
        'build-resume': 'build_resume',
        'validate': 'validate',
        'web-login': 'web_login',
        'web-inspect': 'web_inspect',
        'web-upload': 'web_upload',
        'web-step': 'web_step',
        'prepare': 'prepare',
        'login': 'login',
        'plan': 'plan',
        'inspect': 'inspect',
        'auth': 'auth',
        'status': 'status',
        'download': 'download',
        'attach': 'attach',
        'run': 'run',
        'resume': 'resume',
    }
    module = import_module(".commands." + handlers[args.command], __package__)
    return module.execute(args)




def main(argv=None):
    global _JSON_OUTPUT
    set_language()
    if not (sys.argv[1:] if argv is None else argv):
        parser().print_help()
        return 0
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
        print(t("cli.next", action="charmm-gui-cli -h"), file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        from .auth import recovery_guidance
        print(t("update.interrupted") if args.command == "update" else t("cli.interrupted") + recovery_guidance("interrupted"), file=sys.stderr)
        return 130
