"""Build orchestration; command adapters delegate here without owning state."""

import charmm_gui_cli.store.tasks as _task_store

from charmm_gui_cli.store.layout import modern, task_root, inputs_path, log_path, full_archive_path

from pathlib import Path
import shutil
import sys
import math
import os
import time

from charmm_gui_cli.remote.api import Client
from charmm_gui_cli.store.auth import ToolError, load_token, resolve_cookie_path
from charmm_gui_cli.locales.i18n import t
from charmm_gui_cli.presentation.progress import emit_progress, ProgressReporter, result_presentation


def progress(event):
    """Keep machine-readable JSON on stdout; send safe progress to stderr."""
    emit_progress(event)


def failed_checks(report):
    failed = [key for key, value in report.get("checks", {}).items() if isinstance(value, dict) and value.get("passed") is not True]
    for section in ("environment_validation", "bound_pose_validation", "acceptance_validation"):
        value = report.get(section, {})
        if value and value.get("passed") is not True:
            failed.append(section)
        for key, check in value.get("checks", {}).items():
            if isinstance(check, dict) and check.get("blocking", True) and check.get("passed") is not True:
                failed.append(section + "." + key)
    return failed


def execute(args):
    from charmm_gui_cli.pipeline import managed
    holder = []
    original_stderr = sys.stderr
    execution_code = 2
    try:
        result, execution_code = _execute(args, holder)
        if sys.stderr is not original_stderr and (not getattr(args, "_progress_callback", None) or result.get("state") in ("validated", "validation_failed")):
            from charmm_gui_cli.presentation.terminal_output import format_build
            sys.stderr.log.write(format_build(result) + "\n")
        return result, execution_code
    except ToolError as exc:
        if holder and (holder[0] / "full-run.json").is_file():
            exc.for_task(_task_store._job_metadata(holder[0]).get("name") or str(task_root(holder[0])))
        if sys.stderr is not original_stderr:
            if holder:
                try:
                    saved = _task_store._read_json(holder[0] / "full-run.json")
                    from charmm_gui_cli.presentation.progress import describe_progress
                    stage = describe_progress({"state": saved.get("state")}, localized=True)["label"]
                    ids = {key: value for key, value in saved.items() if key in ("source_job_id", "build_job_id")
                           and isinstance(value, str) and value.isdecimal() and len(value) <= 32}
                    sys.stderr.log.write(t("error.context", stage=stage, source=ids.get("source_job_id", "—"), build=ids.get("build_job_id", "—")) + "\n")
                except (ToolError, OSError):
                    pass
            sys.stderr.log.write(t("cli.error", message=exc.localized()) + "\n")
            sys.stderr.log.write(t("cli.next", action=exc.next_step) + "\n")
        if exc.key != "background.running" and exc.category != "lock_busy" and holder and (holder[0] / "full-run.json").is_file():
            try:
                _task_store.record_recovery(holder[0], exc)
            except (ToolError, OSError):
                pass
        raise
    except KeyboardInterrupt:
        execution_code = 130
        if holder and (holder[0] / "full-run.json").is_file():
            try:
                _task_store.record_recovery(holder[0], ToolError(
                    "Local process interrupted; recorded remote jobs are retained.",
                    category="interrupted", retryable=True,
                    next_step="Use jobs resume or build-resume for the existing task."))
            except (ToolError, OSError):
                pass
        raise
    except OSError:
        if holder and (holder[0] / "full-run.json").is_file():
            try:
                _task_store.record_recovery(holder[0], ToolError(key="cli.filesystem", category="filesystem_error"))
            except (ToolError, OSError):
                pass
        raise
    finally:
        if getattr(args, "_reporter", None) is not None:
            args._reporter.close()
            args._reporter = None
        if getattr(args, "_run_lock_fd", None) is not None:
            from charmm_gui_cli.store.workers import metadata, write_lock
            from charmm_gui_cli.store.locking import now
            record = metadata(holder[-1])
            record.update(finished_at=now(), exit_code=execution_code)
            write_lock(args._run_lock_fd, record)
            os.close(args._run_lock_fd)
            args._run_lock_fd = None
        if sys.stderr is not original_stderr:
            sys.stderr.close()
            sys.stderr = original_stderr


def _execute(args, holder):
    from charmm_gui_cli.pipeline import managed
    from charmm_gui_cli.pipeline.full_build import initialize, advance
    from charmm_gui_cli.remote.web import WebClient

    reporter = getattr(args, "_progress_callback", None) or ProgressReporter()
    args._reporter = reporter
    dry_run = getattr(args, "dry_run", False)
    timeout = getattr(args, "post_timeout", 900)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ToolError(key="web.timeout_invalid", next_step="charmm-gui-cli build -h")
    try:
        retry_window = float(os.environ.get("CHARMM_GUI_GET_RETRY_SECONDS", "1200"))
        if not math.isfinite(retry_window) or not 0 <= retry_window <= 1800:
            raise ValueError
    except ValueError:
        raise ToolError(key="network.retry_invalid", next_step="unset CHARMM_GUI_GET_RETRY_SECONDS") from None
    if args.interval < 10 or args.max_wait <= 0:
        raise ToolError("--interval must be >=10 seconds and --max-wait must be positive.")
    if args.grompp and not shutil.which(args.gmx):
        raise ToolError("--grompp requires GROMACS. Install gmx or supply --gmx /path/to/gmx; no job was submitted.")
    if args.command == "build":
        components = getattr(args, "components", None)
        from ..chem.residues import INPUT_FIELDS
        source_fields = ("protein", "ligand", "complex_pdb", "ligand_resname", "ligand_smiles", "hydrogens") + INPUT_FIELDS
        model_fields = ("upper", "lower", "salt", "salt_concentration", "margin", "water_padding",
                        "orientation", "n_terminal", "c_terminal", "allow_unknown_lipids")
        if args.config and (components or any(getattr(args, key, None) is not None for key in source_fields + model_fields)
                            or args.accept_conect_bond_orders):
            raise ToolError("Choose either a YAML configuration or direct structure/model options, not both. See build -h.")
        if components and (not args.complex_pdb or any(getattr(args, key) is not None for key in
                           ("protein", "ligand", "ligand_resname", "ligand_smiles")) or args.accept_conect_bond_orders):
            raise ToolError("--components requires --complex and cannot be mixed with --protein, --ligand, --ligand-resname, --ligand-smiles or --accept-conect-bond-orders.")
        if not args.config and not (args.complex_pdb or (args.protein and args.ligand)):
            raise ToolError("Provide --protein protein.pdb --ligand bound.sdf, or --complex complex.pdb. See charmm-gui-cli build -h.")

    directory = None
    if args.command == "build-resume":
        directory = _task_store.resolve_run(args.directory) if args.directory is not None else managed.latest_run()
        holder.append(Path(directory).resolve())
        if not getattr(args, "_worker", False):
            from charmm_gui_cli.store.workers import metadata
            if metadata(directory)["running"]:
                raise ToolError(key="background.running", next_step="charmm-gui-cli jobs list")
    if args.command == "build" and getattr(args, "name", None):
        _task_store.check_name_available(args.name)
        if args.out is None:
            args.out = _task_store.new_run_path(args.name)
    if args.command == "build" and args.out is not None:
        from charmm_gui_cli.store.layout import task_path
        args.out = task_path(args.out)
    local_complete = False
    if directory is not None and (directory / "full-run.json").is_file() and (full_archive_path(directory)).is_file():
        local_manifest = _task_store._read_json(directory / "full-run.json")
        local_complete = local_manifest.get("state") in ("ligands_present", "downloaded_unverified", "validation_failed", "validated", "complete", "completed")
    api_only = directory is not None and (directory / "bilayer" / "run.json").is_file()
    if not api_only and directory is not None and (directory / "full-run.json").is_file():
        api_only = "reused_preparation" in _task_store._read_json(directory / "full-run.json")
    offline_recovery = False
    if directory is not None and api_only and not local_complete and (directory / "bilayer" / "run.json").is_file() and (full_archive_path(directory)).is_file():
        bilayer = _task_store._read_json(directory / "bilayer" / "run.json")
        full = _task_store._read_json(directory / "full-run.json")
        offline_recovery = (bilayer.get("schema_version") == 1 and bool(bilayer.get("job_id"))
                            and bilayer.get("state") not in ("submission_unknown", "submitting", "remote_error")
                            and full.get("state") not in ("submission_unknown", "remote_error", "preparation_failed"))
    token = cookies = source = None
    if not dry_run and not local_complete and not offline_recovery:
        token, source = load_token(args.token_file)
        from ..remote.sessions import warn_expiry, session_cookie_path
        cookies = session_cookie_path(args.cookies, source)
        try:
            warn_expiry(token, source)
        except ToolError:
            pass  # Actual transport validation remains authoritative.
        if not api_only and not cookies.is_file():
            next_step = "charmm-gui-cli login; then resume this existing task." if directory is not None else "charmm-gui-cli login; then repeat build."
            raise ToolError("The saved website session is missing. Run charmm-gui-cli login; custom token files need matching --cookies or a Cookie file beside the token.",
                            category="authentication", next_step=next_step)

    if args.command == "build":
        reporter({"state": "preparing_inputs (local)"})
        if args.config:
            directory = args.out or _task_store.new_run_path()
            initialize(args.config, directory)
        else:
            default = lambda value, fallback: fallback if value is None else value
            from ..chem.residues import options_from_args
            common = dict(output=args.out, input_options=options_from_args(args),
                membrane={"upper": default(args.upper, "POPC=1"), "lower": default(args.lower, "POPC=1"),
                          "margin_A": default(args.margin, 20), "water_padding_A": default(args.water_padding, 22.5),
                          **({"allow_unknown_lipids": True} if getattr(args, "allow_unknown_lipids", None) else {})},
                ions={"type": default(args.salt, "KCl"), "concentration_M": default(args.salt_concentration, 0.15)},
                preparation={"orientation": default(args.orientation, "ppm"),
                             "n_terminal": default(args.n_terminal, "NTER"), "c_terminal": default(args.c_terminal, "CTER")},
                hydrogens=default(args.hydrogens, "add_missing"))
            if getattr(args, "terminals", None) is not None:
                common["preparation"]["terminals"] = args.terminals
            if components:
                directory = managed.create_component_build(components_config=components, complex_pdb=args.complex_pdb, **common)
            else:
                directory = managed.create_managed_build(protein=args.protein, ligand=args.ligand, complex_pdb=args.complex_pdb,
                    ligand_resname=default(args.ligand_resname, "LIG"), ligand_smiles=args.ligand_smiles,
                    accept_conect_bond_orders=args.accept_conect_bond_orders, **common)
    directory = Path(directory).resolve()
    holder.append(directory)
    if args.command == "build" and getattr(args, "_managed_cli", False) and not getattr(args, "name", None) and (directory / "full-run.json").is_file():
        try:
            _task_store.validate_name(task_root(directory).name)
        except ToolError:
            pass
        else:
            args.name = task_root(directory).name
            try:
                _task_store.check_name_available(args.name, directory)
            except ToolError:
                import uuid
                args.name = task_root(directory).name[:110] + "-" + uuid.uuid4().hex[:10]
    if getattr(args, "name", None) and (directory / "full-run.json").is_file():
        _task_store.assign_name(directory, args.name)
    if (directory / "full-run.json").is_file():
        _task_store.remember_run(directory)
        if args.command == "build":
            from charmm_gui_cli.store.result_files import check_destination
            check_destination(directory)
    if dry_run:
        prepared_manifest = (_task_store._read_json(directory / "full-run.json").get("inputs", {}).get("manifest", {})
                             if (directory / "full-run.json").is_file() else {})
        return {"directory": str(directory), "state": "inputs_ready", "server_submitted": False,
                "automatic_repairs": prepared_manifest.get("automatic_repairs", []),
                "structure_inventory": prepared_manifest.get("input_normalization", {}).get("structure_inventory", {}),
                "review_required": prepared_manifest.get("ligand_selection", {}).get("conversion", {}).get("review_required", False),
                "chemistry_warnings": prepared_manifest.get("ligand", {}).get("chemistry_warnings", []),
                **({"name": args.name} if getattr(args, "name", None) else {}),
                "next_step": "charmm-gui-cli login; charmm-gui-cli build-resume"}, 0
    if directory.is_dir() and not getattr(args, "_worker", False):
        from charmm_gui_cli.store.workers import claim, write_lock
        from charmm_gui_cli.store.locking import now
        args._run_lock_fd = claim(directory)
        write_lock(args._run_lock_fd, {"pid": os.getpid(), "started_at": now()})
        from charmm_gui_cli.store.run_logging import LogStream
        sys.stderr = LogStream(sys.stderr, log_path(directory))
    if not getattr(args, "_progress_callback", None):
        print(f"[charmm-gui-cli] run={directory}", file=sys.stderr, flush=True)
    if (directory / "full-run.json").is_file() and not getattr(args, "_progress_callback", None):
        saved = _task_store._read_json(directory / "full-run.json")
        reporter.close()
        reporter = ProgressReporter(source_job_id=saved.get("source_job_id"), build_job_id=saved.get("build_job_id"))
        args._reporter = reporter
    if local_complete:
        result = local_manifest
    else:
        if (directory / "full-run.json").is_file():
            _task_store.record_recovery(directory, state="running")
        from ..remote.http_retry import long_get_retries
        def waiting(event):
            from ..store.auth import atomic_json
            from charmm_gui_cli.store.locking import now
            if event["event"] == "waiting":
                atomic_json(directory / "network-wait.json", {**event, "active": True, "updated_at": now(),
                            "next_retry_unix": time.time() + event["retry_in_seconds"]})
                reporter(event)
            else:
                atomic_json(directory / "network-wait.json", {"active": False, "updated_at": now()})
                reporter({"state": _task_store._read_json(directory / "full-run.json").get("state", "running")})
        with long_get_retries(retry_window, on_wait=waiting):
            from ..remote.sessions import bind
            api = Client(token)
            manager = bind(api, source, cookies) if source is not None else None
            web = None if api_only else WebClient(cookies, post_timeout=getattr(args, "post_timeout", 900))
            if web is not None and source is not None:
                bind(web, source, cookies, manager)
            result = advance(directory, web, api, wait=args.wait,
                             interval=args.interval, max_wait=args.max_wait, progress=reporter)
    summary = {"directory": str(directory), **{key: result[key] for key in
               ("state", "source_job_id", "build_job_id") if key in result}}
    summary.update(_task_store._job_metadata(directory))
    if result.get("state") in ("ligands_present", "validation_failed", "downloaded_unverified", "validated") or local_complete:
        reporter({"state": "validating_final_archive (local)",
                  **{key: result[key] for key in ("source_job_id", "build_job_id") if key in result}})
        use_grompp = args.grompp or (not args.no_grompp and shutil.which(args.gmx) is not None)
        report = managed.finalize_managed_build(directory, grompp=use_grompp, gmx=args.gmx)
        summary.update(state="validated" if report.get("passed") else "validation_failed",
                       validation={"passed": report.get("passed"),
                                   "environment_passed": report.get("environment_validation", {}).get("passed"),
                                   "bound_pose_passed": report.get("bound_pose_validation", {}).get("passed"),
                                   "acceptance_passed": report.get("acceptance_validation", {}).get("passed"),
                                   "review_required": report.get("review_required", False),
                                   "failed_checks": failed_checks(report),
                                   "scientific_correctness_verified": False},
                       report=str(directory / "results" / "validation.json"),
                       archive=str(full_archive_path(directory)))
        summary["gromacs_compiled"] = bool(report.get("checks", {}).get("grompp", {}).get("passed"))
        if not use_grompp:
            summary["gromacs_note"] = "GROMACS compilation was not run; install gmx and build-resume --grompp to add it."
        if (directory / "full-run.json").is_file():
            _task_store.record_recovery(directory, state=summary["state"])
        summary["presentation"] = result_presentation(summary, report)
        summary["official_links"] = summary["presentation"]["official_links"]
        if getattr(args, "no_preview", False):
            summary["preview"] = {"status": "disabled"}
        else:
            from charmm_gui_cli.pipeline.preview import preview_run
            try:
                summary["preview"] = preview_run(directory, report=report)
            except (ToolError, OSError) as error:
                summary["preview"] = {"status": "failed", "message": str(error) if isinstance(error, ToolError) else "Local preview output could not be generated."}
        if report.get("validation_complete") is True:
            from charmm_gui_cli.pipeline.delivery import auto_export
            summary["delivery"] = auto_export(directory)
        return summary, 0 if report.get("passed") else 2
    summary["next_step"] = "charmm-gui-cli build-resume"
    if (directory / "full-run.json").is_file():
        _task_store.record_recovery(directory, state=summary.get("state"))
    summary["presentation"] = result_presentation(summary)
    summary["official_links"] = summary["presentation"]["official_links"]
    return summary, 0
