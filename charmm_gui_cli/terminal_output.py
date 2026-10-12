"""Allowlisted terminal summaries; never render credentials or server bodies."""

from pathlib import Path
import re

from .i18n import t


def _text(value):
    return "".join(c for c in str(value) if c.isprintable())


def _path(value):
    resolved = Path(str(value)).expanduser().resolve()
    try:
        return str(resolved.relative_to(Path.cwd())) or "."
    except ValueError:
        pass
    path = _text(resolved)
    home = str(Path.home())
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def format_login(value, *, email=None):
    account = " (" + _text(email) + ")" if email else ""
    auth = lambda name: t("output.authenticated") if value.get(name) is True else t("output.unconfirmed")
    lines = [t("output.login", account=account), t("output.api_session", state=auth("api_authenticated")),
             t("output.web_session", state=auth("web_authenticated"))]
    if value.get("saved_to"):
        lines.append(t("output.saved", path=_path(Path(value["saved_to"]).parent),
                       permissions="0600" if value.get("permissions") == "0600" else t("output.unconfirmed")))
    if value.get("cookie_file"):
        lines.append(t("output.cookie_path", path=_path(value["cookie_file"])))
    if value.get("notice"):
        lines.append(t("output.env_override"))
    lines.append(t("cli.next", action="charmm-gui-cli build --protein protein.pdb --ligand bound-ligand.sdf --name receptor-a"))
    return "\n".join(lines)


def format_build(value, **context):
    if value.get("background", {}).get("running"):
        background = value["background"]
        name = value.get("name") or _path(value.get("directory", ""))
        return "\n".join([t("output.background", name=_text(name), pid=background.get("pid", "—")),
                         t("output.log", path=_path(background["log"])),
                         t("cli.next", action="charmm-gui-cli jobs watch " + str(name))])
    from .progress import describe_progress
    from .auth import recovery_guidance
    state = value.get("state", value.get("status", "unknown"))
    label = describe_progress({"state": state}, localized=True)["label"]
    if state in ("done", "downloading", "downloaded_unverified", "ligands_present", "inputs_ready", "validated"):
        label = t("stage." + state)
    lines = [t("output.task", name=_text(value.get("name") or value.get("job_id") or "—")),
             t("output.state", state=label)]
    if value.get("remote_status") is not None:
        remote = value["remote_status"]
        remote_label = t("output.remote_done") if remote == "done" else describe_progress({"state": remote}, localized=True)["label"]
        lines.append(t("output.remote_state", state=remote_label))
    for field, key in (("source_job_id", "output.source"), ("build_job_id", "output.build_id"), ("job_id", "output.build_id")):
        identifier = value.get(field)
        if field == "job_id" and value.get("build_job_id"):
            continue
        if isinstance(identifier, str) and re.fullmatch(r"[0-9]{1,32}", identifier):
            lines.append(t(key, jobid=identifier))
    for field, key in (("directory", "output.directory"), ("archive", "output.archive"), ("report", "output.report")):
        if isinstance(value.get(field), str):
            lines.append(t(key, path=_path(value[field])))
    if not value.get("report") and isinstance(value.get("report_file"), str):
        lines.append(t("output.report", path=_path(value["report_file"])))
    passed = value.get("passed")
    validation = value.get("validation")
    if isinstance(validation, dict):
        passed = validation.get("passed", passed)
    if state == "validated" and passed is True:
        lines[1] = t("output.state", state=t("output.passed"))
    elif passed is False or state == "validation_failed":
        lines[1] = t("output.state", state=t("output.failed"))
    if value.get("validation_status") == "stale_or_unreadable":
        lines.append(t("output.stale"))
    compilation = value.get("compilation_status", "unknown")
    if value.get("gromacs_compiled") is True:
        compilation = "passed"
    lines.append(t("output.compilation", state=t("output." + compilation) if compilation in ("passed", "failed", "not_run") else t("output.unconfirmed")))
    if value.get("review_required") or isinstance(validation, dict) and validation.get("review_required"):
        lines.append(t("output.review"))
    recovery = value.get("recovery")
    recovery = recovery if isinstance(recovery, dict) else {}
    error = recovery.get("error", value.get("last_error"))
    error = error if isinstance(error, dict) else {}
    category = "submission_unknown" if state == "submission_unknown" else error.get("category", state)
    guidance = recovery_guidance(category, cause_category=error.get("cause_category"))
    if guidance:
        lines.append(guidance)
    name = value.get("name")
    if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
        action = "charmm-gui-cli jobs report " + name if state == "validated" and passed is True else "charmm-gui-cli jobs resume " + name
    else:
        action = "charmm-gui-cli jobs / charmm-gui-cli build-resume"
    if category in ("authentication", "auth_required", "paused_auth"):
        action = "charmm-gui-cli login; " + action
    elif category == "submission_unknown":
        action = t("output.inspect_next")
    lines.append(t("cli.next", action=action))
    return "\n".join(lines)


def format_check(value, **context):
    lines = [t("check.result", state=t("output.passed") if value.get("passed") else t("output.failed")),
             t("check.protein", residues=value.get("protein", {}).get("residues", value.get("protein_inventory", {}).get("protein_residues_all_models", "—")))]
    for component in value.get("chemistry", []):
        lines.append(t("check.ligand", name=component.get("resname", "—"), formula=component.get("molecular_formula", "—"), charge=component.get("net_formal_charge", "—")))
    for entry in value.get("automatic_repairs", []):
        lines.append("• " + t(entry["key"], **entry["params"]))
    for prediction in value.get("terminal_predictions", []):
        lines.append(t("check.terminal", chain=prediction["chain"], residue=prediction["first_resname"], patch=prediction["suggested_n_terminal"]))
    for image in value.get("preview", {}).get("images", []):
        if image.get("file"):
            lines.append(t("check.preview", path=_path(image["file"])))
    if value.get("check_file"):
        lines.append(t("output.report", path=_path(value["check_file"])))
    lines.append(t("cli.next", action="charmm-gui-cli build -h" if value.get("passed") else "charmm-gui-cli check -h"))
    return "\n".join(lines)


def format_jobs(value, **context):
    lines = [t("jobs.columns")]
    for job in value.get("jobs", []):
        background = job.get("background", {})
        lines.append(f"{_text(job.get('name') or _path(job['directory']))} | {_text(job.get('state', '—'))} | "
                     + t("jobs.running" if background.get("running") else "jobs.stopped") + f" | {background.get('elapsed_seconds', 0)}s")
    lines.append(t("cli.next", action="charmm-gui-cli jobs status NAME"))
    return "\n".join(lines)


def format_job_status(value, **context):
    display = dict(value)
    display.pop("background", None)
    result = format_build(display)
    background = value.get("background", {})
    result += "\n" + t("jobs.running" if background.get("running") else "jobs.stopped")
    result += " | " + t("progress.elapsed", elapsed=f"{background.get('elapsed_seconds', 0) // 60}:{background.get('elapsed_seconds', 0) % 60:02d}")
    for record in value.get("recent_stages", []):
        result += "\n" + _text(record)
    return result


def format_update(value, **context):
    lines = [t("update.versions", current=value["current_version"], latest=value["latest_version"])]
    key = {"update_available": "available", "up_to_date": "up_to_date", "local_newer": "local_newer", "updated": "updated"}[value["state"]]
    lines.append(t("update." + key))
    if value.get("command_path"):
        lines.append(t("update.command", path=_path(value["command_path"])))
    return "\n".join(lines)


FORMATTERS = {"update": format_update, "login": format_login, "build": format_build, "build-resume": format_build,
              "status": format_build, "jobs.status": format_job_status, "jobs.resume": format_build,
              "check": format_check, "jobs.list": format_jobs}
